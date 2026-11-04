//! Minimal TCP SOCKS5 transparent proxy for the singleton virtual device.
//!
//! The proxy runs in the device netns.  TC eBPF assigns the original packet
//! to the transparent listener; `local_addr()` (or SO_ORIGINAL_DST) recovers
//! the remote address, then the proxy CONNECTs to the upstream SOCKS5 server
//! and relays bytes.  Landscape's WAN NAT handles the upstream SNAT.

use std::{
    io::{self, ErrorKind},
    net::{Ipv4Addr, SocketAddr, SocketAddrV4},
    os::fd::{AsRawFd, FromRawFd},
};

use landscape_common::config_service::iface::Socks5VirtualConfig;
use landscape_ebpf::libc;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt, copy_bidirectional},
    net::{TcpListener, TcpStream},
    sync::oneshot,
};

const SO_ORIGINAL_DST: libc::c_int = 80;
const IP_TRANSPARENT: libc::c_int = 19;

fn set_bool_sockopt(fd: libc::c_int, level: libc::c_int, name: libc::c_int) -> io::Result<()> {
    let enable: libc::c_int = 1;
    let result = unsafe {
        libc::setsockopt(
            fd,
            level,
            name,
            &enable as *const _ as *const libc::c_void,
            std::mem::size_of::<libc::c_int>() as libc::socklen_t,
        )
    };
    if result != 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

pub fn bind_transparent(port: u16) -> io::Result<TcpListener> {
    let fd = unsafe {
        libc::socket(
            libc::AF_INET,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }

    let configured = (|| -> io::Result<()> {
        set_bool_sockopt(fd, libc::SOL_SOCKET, libc::SO_REUSEADDR)?;
        set_bool_sockopt(fd, libc::SOL_IP, IP_TRANSPARENT)?;

        let addr = libc::sockaddr_in {
            sin_family: libc::AF_INET as libc::sa_family_t,
            sin_port: port.to_be(),
            sin_addr: libc::in_addr { s_addr: libc::INADDR_ANY },
            sin_zero: [0; 8],
        };
        let bound = unsafe {
            libc::bind(
                fd,
                &addr as *const _ as *const libc::sockaddr,
                std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t,
            )
        };
        if bound != 0 {
            return Err(io::Error::last_os_error());
        }
        if unsafe { libc::listen(fd, 1024) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    })();

    if let Err(error) = configured {
        unsafe { libc::close(fd) };
        return Err(error);
    }

    let listener = unsafe { std::net::TcpListener::from_raw_fd(fd) };
    TcpListener::from_std(listener)
}

/// Plain TCP relay for a loopback upstream (e.g. mihomo on 127.0.0.1).
///
/// The netns cannot reach the host loopback, so it talks to this relay on
/// 10.255.0.1; the relay dials 127.0.0.1 from the host namespace.  It is a
/// transparent byte pipe, so the SOCKS5 handshake/auth still goes end-to-end.
pub async fn serve_tcp_relay(listener: TcpListener, upstream_port: u16) -> io::Result<()> {
    tracing::info!(
        local = ?listener.local_addr().ok(),
        upstream_port,
        "SOCKS5 loopback relay listening"
    );
    loop {
        let (mut client, peer) = listener.accept().await?;
        tokio::spawn(async move {
            match TcpStream::connect((Ipv4Addr::LOCALHOST, upstream_port)).await {
                Ok(mut upstream) => {
                    if let Err(error) = copy_bidirectional(&mut client, &mut upstream).await {
                        tracing::warn!(%peer, %error, "SOCKS5 loopback relay copy failed");
                    }
                }
                Err(error) => {
                    tracing::warn!(%peer, %error, "SOCKS5 loopback relay connect failed");
                }
            }
        });
    }
}

pub async fn serve(
    listener: TcpListener,
    config: Socks5VirtualConfig,
    mut shutdown: oneshot::Receiver<()>,
) -> io::Result<()> {
    tracing::info!(
        local = ?listener.local_addr().ok(),
        "SOCKS5 transparent proxy listening"
    );
    loop {
        tokio::select! {
            _ = &mut shutdown => return Ok(()),
            accepted = listener.accept() => {
                let (stream, peer) = accepted?;
                let config = config.clone();
                tokio::spawn(async move {
                    if let Err(error) = handle_connection(stream, &config).await {
                        tracing::warn!(%peer, %error, "SOCKS5 proxy connection failed");
                    }
                });
            }
        }
    }
}

async fn handle_connection(
    mut stream: TcpStream,
    config: &Socks5VirtualConfig,
) -> io::Result<()> {
    // TPROXY keeps the original destination as the accepted socket's local
    // address.  Fall back to SO_ORIGINAL_DST for a REDIRECT-style setup.
    let destination = match stream.local_addr() {
        Ok(addr) if !addr.ip().is_unspecified() => addr,
        _ => original_dst(&stream)?,
    };
    if destination.ip().is_unspecified() {
        return Ok(());
    }

    // Landscape's existing WAN NAT SNATs the 10.255.0.2 source when this
    // upstream connection leaves the WAN link; no extra relay or nft rule.
    let mut upstream = TcpStream::connect((config.server.as_str(), config.port)).await?;
    socks5_connect(
        &mut upstream,
        destination,
        config.username.as_deref(),
        config.password.as_deref().unwrap_or(""),
    )
    .await?;
    copy_bidirectional(&mut stream, &mut upstream).await?;
    Ok(())
}

fn original_dst(stream: &TcpStream) -> io::Result<SocketAddr> {
    let mut addr: libc::sockaddr_in = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t;
    let result = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_IP,
            SO_ORIGINAL_DST,
            &mut addr as *mut _ as *mut libc::c_void,
            &mut len,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    if addr.sin_family != libc::AF_INET as libc::sa_family_t {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "original destination is not IPv4",
        ));
    }
    let ip = Ipv4Addr::from(u32::from_be(addr.sin_addr.s_addr));
    let port = u16::from_be(addr.sin_port);
    Ok(SocketAddr::V4(SocketAddrV4::new(ip, port)))
}

async fn socks5_connect(
    upstream: &mut TcpStream,
    destination: SocketAddr,
    username: Option<&str>,
    password: &str,
) -> io::Result<()> {
    let username = username.filter(|value| !value.is_empty());

    if username.is_some() {
        upstream.write_all(&[0x05, 0x02, 0x00, 0x02]).await?;
    } else {
        upstream.write_all(&[0x05, 0x01, 0x00]).await?;
    }

    let mut method = [0u8; 2];
    upstream.read_exact(&mut method).await?;
    if method[0] != 0x05 {
        return Err(io::Error::new(ErrorKind::InvalidData, "bad SOCKS5 version"));
    }
    match method[1] {
        0x00 => {}
        0x02 => {
            let username = username.ok_or_else(|| {
                io::Error::new(ErrorKind::InvalidData, "server requires authentication")
            })?;
            let user = username.as_bytes();
            let pass = password.as_bytes();
            if user.len() > 255 || pass.len() > 255 {
                return Err(io::Error::new(
                    ErrorKind::InvalidInput,
                    "SOCKS5 credentials are too long",
                ));
            }
            let mut auth = Vec::with_capacity(3 + user.len() + pass.len());
            auth.push(0x01);
            auth.push(user.len() as u8);
            auth.extend_from_slice(user);
            auth.push(pass.len() as u8);
            auth.extend_from_slice(pass);
            upstream.write_all(&auth).await?;

            let mut auth_reply = [0u8; 2];
            upstream.read_exact(&mut auth_reply).await?;
            if auth_reply[1] != 0x00 {
                return Err(io::Error::new(
                    ErrorKind::PermissionDenied,
                    "SOCKS5 authentication failed",
                ));
            }
        }
        0xff => {
            return Err(io::Error::new(
                ErrorKind::PermissionDenied,
                "no acceptable SOCKS5 authentication method",
            ));
        }
        _ => {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "unsupported SOCKS5 authentication method",
            ));
        }
    }

    let mut request = vec![0x05, 0x01, 0x00];
    match destination {
        SocketAddr::V4(addr) => {
            request.push(0x01);
            request.extend_from_slice(&addr.ip().octets());
        }
        SocketAddr::V6(addr) => {
            request.push(0x04);
            request.extend_from_slice(&addr.ip().octets());
        }
    }
    request.extend_from_slice(&destination.port().to_be_bytes());
    upstream.write_all(&request).await?;

    let mut reply = [0u8; 4];
    upstream.read_exact(&mut reply).await?;
    if reply[0] != 0x05 || reply[1] != 0x00 {
        return Err(io::Error::new(
            ErrorKind::ConnectionRefused,
            format!("SOCKS5 CONNECT failed: {reply:?}"),
        ));
    }
    let address_len = match reply[3] {
        0x01 => 4,
        0x04 => 16,
        0x03 => {
            let mut length = [0u8; 1];
            upstream.read_exact(&mut length).await?;
            length[0] as usize
        }
        _ => {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "bad SOCKS5 reply address type",
            ));
        }
    };
    let mut rest = vec![0u8; address_len + 2];
    upstream.read_exact(&mut rest).await?;
    Ok(())
}
