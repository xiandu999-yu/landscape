//! Singleton SOCKS5 virtual device.
//!
//! Creating the SOCKS5 configuration also creates its veth/netkit pair and
//! netns.  Only one device may exist; a second create is rejected.  No Docker
//! or OpenWrt configuration is touched.

use std::{
    ffi::CString,
    fs::File,
    mem::MaybeUninit,
    net::Ipv4Addr,
    os::fd::AsRawFd,
    path::PathBuf,
    process::Command,
    sync::{Arc, mpsc},
    thread::JoinHandle,
    time::Duration,
};

use landscape_ebpf::{
    landscape::TcHookProxy,
    skel::socks5_tproxy::Socks5TproxySkelBuilder,
};
use libbpf_rs::TC_INGRESS;
use libbpf_rs::skel::{OpenSkel, SkelBuilder};
use tokio::{
    net::TcpListener,
    sync::{Mutex, oneshot},
    task::JoinHandle as TokioJoinHandle,
};

use axum::extract::State;
use landscape::{
    config_service::iface_service::IfaceManagerService,
    get_iface_by_name,
    sys_service::route::IpRouteService,
};
use landscape_common::{
    config_service::iface::{IfaceZoneType, Socks5LinkKind, Socks5VirtualConfig},
    database::error::DbError,
    dev::iface::ChangeZone,
    service::controller::ConfigStoreController,
    sys_service::route_service::{RouteOwner, RouteTargetInfo, WanRouteTarget},
};
use crate::{
    LandscapeApp,
    api::{JsonBody, LandscapeApiResp},
    error::LandscapeApiResult,
};

const SOCKS5_CONFIG_FILE: &str = "socks5-virtual.json";
const HOST_ADDR: &str = "10.255.0.1/24";
const PEER_ADDR: &str = "10.255.0.2/24";
const PEER_GATEWAY: &str = "10.255.0.1";
const PROXY_PORT: u16 = 1080;
const LOOPBACK_RELAY_PORT: u16 = 1082;

struct ProxyThread {
    shutdown: Option<oneshot::Sender<()>>,
    join: JoinHandle<()>,
}

struct ProxyRuntime {
    thread: ProxyThread,
    relay: Option<TokioJoinHandle<()>>,
}

#[derive(Clone)]
pub struct Socks5VirtualService {
    home_path: Arc<PathBuf>,
    inner: Arc<Mutex<Option<Socks5VirtualConfig>>>,
    proxy_thread: Arc<Mutex<Option<ProxyRuntime>>>,
}

impl Socks5VirtualService {
    pub fn new(home_path: PathBuf) -> Self {
        let path = home_path.join(SOCKS5_CONFIG_FILE);
        let inner: Option<Socks5VirtualConfig> = std::fs::read(&path)
            .ok()
            .and_then(|bytes| match serde_json::from_slice(&bytes) {
                Ok(config) => Some(config),
                Err(error) => {
                    tracing::warn!(
                        path = %path.display(),
                        error = %error,
                        "invalid socks5-virtual.json"
                    );
                    None
                }
            });
        Self {
            home_path: Arc::new(home_path),
            inner: Arc::new(Mutex::new(inner)),
            proxy_thread: Arc::new(Mutex::new(None)),
        }
    }

    pub async fn get(&self) -> Option<Socks5VirtualConfig> {
        self.inner.lock().await.as_ref().cloned()
    }

    pub async fn create(
        &self,
        config: Socks5VirtualConfig,
        iface: &IfaceManagerService,
        route: &IpRouteService,
    ) -> Result<Socks5VirtualConfig, DbError> {
        config.validate().map_err(DbError::Internal)?;

        let mut guard = self.inner.lock().await;
        if guard.is_some() {
            return Err(DbError::Conflict);
        }

        let host = config.host_name().to_string();
        if get_iface_by_name(&host).await.is_some() {
            return Err(DbError::Internal(format!(
                "interface '{host}' already exists; choose a different software name"
            )));
        }

        if let Err(error) = create_link(&config) {
            cleanup_link(&config);
            return Err(DbError::Internal(error));
        }

        iface.manage_dev(host.clone()).await;
        iface
            .change_zone(ChangeZone { iface_name: host.clone(), zone: IfaceZoneType::Virtual })
            .await;

        if let Err(error) = self.write_config(&config) {
            cleanup_link(&config);
            let _ = iface.delete(host).await;
            return Err(DbError::Internal(error));
        }

        // Register the virtual device as a WAN route target.  The flow target
        // `virtual` maps to this same `RouteOwner::Netns(name)` key.
        if let Some(iface_info) = get_iface_by_name(&host).await {
            // Netkit L3 has no Ethernet MAC; netlink reports all-zero.  Treat
            // that as "no MAC" so the eBPF target uses the dev-only redirect.
            let mac = iface_info.mac.filter(|mac| !mac.is_zero());
            let target = RouteTargetInfo::new_gateway_v4(
                iface_info.index,
                &host,
                mac,
                Ipv4Addr::new(10, 255, 0, 1),
                Ipv4Addr::new(10, 255, 0, 2),
                false,
            );
            route
                .insert_ipv4_wan_route(
                    &RouteOwner::Netns(host.clone()),
                    WanRouteTarget::netns(target),
                )
                .await;
        } else {
            tracing::warn!(device = host, "SOCKS5 virtual device is not visible in netlink; route target not registered");
        }

        let proxy = match start_proxy(&config).await {
            Ok(child) => child,
            Err(error) => {
                route
                    .remove_ipv4_wan_route(&RouteOwner::Netns(host.clone()))
                    .await;
                cleanup_link(&config);
                let _ = iface.delete(host).await;
                let _ = std::fs::remove_file(self.config_path());
                return Err(DbError::Internal(error));
            }
        };
        *self.proxy_thread.lock().await = Some(proxy);

        *guard = Some(config.clone());
        Ok(config)
    }

    pub async fn delete(
        &self,
        iface: &IfaceManagerService,
        route: &IpRouteService,
    ) -> Result<(), DbError> {
        let mut guard = self.inner.lock().await;
        let Some(config) = guard.take() else {
            return Ok(());
        };

        if let Some(mut runtime) = self.proxy_thread.lock().await.take() {
            if let Some(relay) = runtime.relay.take() {
                relay.abort();
            }
            if let Some(shutdown) = runtime.thread.shutdown.take() {
                let _ = shutdown.send(());
            }
            let _ = runtime.thread.join.join();
        }

        route
            .remove_ipv4_wan_route(&RouteOwner::Netns(config.host_name().to_string()))
            .await;
        cleanup_link(&config);
        let _ = iface.delete(config.host_name().to_string()).await;
        let _ = std::fs::remove_file(self.config_path());
        Ok(())
    }

    fn config_path(&self) -> PathBuf {
        self.home_path.join(SOCKS5_CONFIG_FILE)
    }

    fn write_config(&self, config: &Socks5VirtualConfig) -> Result<(), String> {
        let path = self.config_path();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let bytes = serde_json::to_vec_pretty(config).map_err(|error| error.to_string())?;
        std::fs::write(&path, bytes).map_err(|error| error.to_string())
    }
}

fn is_loopback_server(server: &str) -> bool {
    let server = server.trim();
    if server.eq_ignore_ascii_case("localhost") {
        return true;
    }
    server
        .parse::<std::net::IpAddr>()
        .map(|ip| ip.is_loopback())
        .unwrap_or(false)
}

async fn start_proxy(config: &Socks5VirtualConfig) -> Result<ProxyRuntime, String> {
    let mut proxy_config = config.clone();
    let relay = if is_loopback_server(&config.server) {
        let listener = TcpListener::bind((Ipv4Addr::new(10, 255, 0, 1), LOOPBACK_RELAY_PORT))
            .await
            .map_err(|error| format!("failed to bind SOCKS5 loopback relay: {error}"))?;
        let upstream_port = config.port;
        let task = tokio::spawn(async move {
            if let Err(error) =
                crate::socks5_proxy::serve_tcp_relay(listener, upstream_port).await
            {
                tracing::error!(%error, "SOCKS5 loopback relay stopped");
            }
        });
        proxy_config.server = "10.255.0.1".to_string();
        proxy_config.port = LOOPBACK_RELAY_PORT;
        Some(task)
    } else {
        None
    };

    let thread = match spawn_proxy_thread(&proxy_config) {
        Ok(thread) => thread,
        Err(error) => {
            if let Some(relay) = relay.as_ref() {
                relay.abort();
            }
            return Err(error);
        }
    };
    Ok(ProxyRuntime { thread, relay })
}

fn spawn_proxy_thread(config: &Socks5VirtualConfig) -> Result<ProxyThread, String> {
    let netns = config.netns_name();
    let config = config.clone();
    let (ready_tx, ready_rx) = mpsc::channel();
    let (shutdown_tx, shutdown_rx) = oneshot::channel();
    let join = std::thread::spawn(move || {
        let mut ready_tx = Some(ready_tx);
        let result = (|| -> Result<(), String> {
            let fd = File::open(format!("/var/run/netns/{netns}"))
                .map_err(|error| format!("failed to open netns {netns}: {error}"))?;
            let changed = unsafe { libc::setns(fd.as_raw_fd(), libc::CLONE_NEWNET) };
            if changed != 0 {
                return Err(format!(
                    "failed to enter netns {netns}: {}",
                    std::io::Error::last_os_error()
                ));
            }

            let mut open_object = MaybeUninit::uninit();
            let builder = Socks5TproxySkelBuilder::default();
            let mut open_skel = builder
                .open(&mut open_object)
                .map_err(|error| format!("failed to open tproxy skeleton: {error}"))?;
            let rodata = open_skel
                .maps
                .rodata_data
                .as_deref_mut()
                .ok_or_else(|| "tproxy rodata is not mapped".to_string())?;
            rodata.proxy_port = PROXY_PORT.to_be();
            let skel = open_skel
                .load()
                .map_err(|error| format!("failed to load tproxy skeleton: {error}"))?;

            let peer = CString::new(config.peer_name())
                .map_err(|error| format!("invalid peer name: {error}"))?;
            let ifindex = unsafe { libc::if_nametoindex(peer.as_ptr()) };
            if ifindex == 0 {
                return Err(format!(
                    "peer interface {} not found in {netns}",
                    config.peer_name()
                ));
            }
            let mut hook = TcHookProxy::new(
                &skel.progs.socks5_tproxy_ingress,
                ifindex as i32,
                TC_INGRESS,
                1,
            );
            hook.attach();

            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|error| format!("failed to build proxy runtime: {error}"))?;
            runtime.block_on(async {
                let listener = crate::socks5_proxy::bind_transparent(PROXY_PORT)
                    .map_err(|error| format!("failed to bind proxy port {PROXY_PORT}: {error}"))?;
                if let Some(tx) = ready_tx.take() {
                    let _ = tx.send(Ok(()));
                }
                crate::socks5_proxy::serve(listener, config, shutdown_rx)
                    .await
                    .map_err(|error| format!("proxy serve failed: {error}"))
            })
        })();
        if let Err(error) = result {
            if let Some(tx) = ready_tx.take() {
                let _ = tx.send(Err(error));
            }
        }
    });

    match ready_rx.recv_timeout(Duration::from_secs(2)) {
        Ok(Ok(())) => Ok(ProxyThread { shutdown: Some(shutdown_tx), join }),
        Ok(Err(error)) => {
            let _ = join.join();
            Err(error)
        }
        Err(error) => {
            let _ = shutdown_tx.send(());
            let _ = join.join();
            Err(format!("SOCKS5 proxy did not start: {error}"))
        }
    }
}

fn create_link(config: &Socks5VirtualConfig) -> Result<(), String> {
    let host = config.host_name();
    let peer = config.peer_name();
    let netns = config.netns_name();

    // A leftover netns/interface from a crashed run would make the rest of
    // the sequence ambiguous.  Try to remove it first; ignore "not found".
    let _ = ip(&["netns", "del", netns.as_str()]);
    let _ = ip(&["link", "del", host]);

    ip(&["netns", "add", netns.as_str()])?;

    let result = match config.kind {
        Socks5LinkKind::Veth => {
            ip(&["link", "add", host, "type", "veth", "peer", "name", peer.as_str()])
        }
        Socks5LinkKind::Netkit => {
            ip(&["link", "add", host, "type", "netkit", "peer", "name", peer.as_str()])
        }
        Socks5LinkKind::NetkitL3 => ip(&[
            "link",
            "add",
            host,
            "type",
            "netkit",
            "mode",
            "l3",
            "peer",
            "name",
            peer.as_str(),
        ]),
        Socks5LinkKind::Auto => {
            let netkit =
                ip(&["link", "add", host, "type", "netkit", "peer", "name", peer.as_str()]);
            if netkit.is_err() {
                ip(&["link", "add", host, "type", "veth", "peer", "name", peer.as_str()])
            } else {
                netkit
            }
        }
    };
    result?;

    ip(&["link", "set", peer.as_str(), "netns", netns.as_str()])?;
    ip(&["link", "set", host, "up"])?;
    ip(&["-n", netns.as_str(), "link", "set", peer.as_str(), "up"])?;
    ip(&["-n", netns.as_str(), "link", "set", "lo", "up"])?;
    ip(&["addr", "replace", HOST_ADDR, "dev", host])?;
    ip(&["-n", netns.as_str(), "addr", "replace", PEER_ADDR, "dev", peer.as_str()])?;
    ip(&["-n", netns.as_str(), "route", "replace", "default", "via", PEER_GATEWAY])?;

    tracing::info!(
        software = host,
        peer = %peer,
        netns = %netns,
        kind = ?config.kind,
        "created SOCKS5 virtual device"
    );
    Ok(())
}

fn cleanup_link(config: &Socks5VirtualConfig) {
    let host = config.host_name();
    let netns = config.netns_name();
    let _ = ip(&["netns", "del", &netns]);
    let _ = ip(&["link", "del", host]);
}

fn ip(args: &[&str]) -> Result<(), String> {
    let output = Command::new("ip")
        .args(args)
        .output()
        .map_err(|error| format!("failed to run ip {}: {error}", args.join(" ")))?;
    if output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        Err(format!("ip {} failed: {stderr}", args.join(" ")))
    }
}

#[utoipa::path(
    get,
    path = "/socks5-virtual",
    tag = "Interfaces",
    operation_id = "get_socks5_virtual",
    responses((status = 200, description = "Success; null when not created", body = Socks5VirtualConfig))
)]
pub(crate) async fn get_socks5_virtual(
    State(state): State<LandscapeApp>,
) -> LandscapeApiResult<Option<Socks5VirtualConfig>> {
    LandscapeApiResp::success(state.socks5_virtual_service.get().await)
}

#[utoipa::path(
    post,
    path = "/socks5-virtual",
    tag = "Interfaces",
    operation_id = "create_socks5_virtual",
    request_body = Socks5VirtualConfig,
    responses((status = 200, description = "Success", body = Socks5VirtualConfig))
)]
pub(crate) async fn create_socks5_virtual(
    State(state): State<LandscapeApp>,
    JsonBody(config): JsonBody<Socks5VirtualConfig>,
) -> LandscapeApiResult<Socks5VirtualConfig> {
    let config = state
        .socks5_virtual_service
        .create(config, &state.iface_config_service, &state.route_service)
        .await?;
    LandscapeApiResp::success(config)
}

#[utoipa::path(
    delete,
    path = "/socks5-virtual",
    tag = "Interfaces",
    operation_id = "delete_socks5_virtual",
    responses((status = 200, description = "Success"))
)]
pub(crate) async fn delete_socks5_virtual(
    State(state): State<LandscapeApp>,
) -> LandscapeApiResult<()> {
    state
        .socks5_virtual_service
        .delete(&state.iface_config_service, &state.route_service)
        .await?;
    LandscapeApiResp::success(())
}
