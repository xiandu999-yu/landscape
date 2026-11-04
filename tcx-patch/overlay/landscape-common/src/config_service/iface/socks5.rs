use serde::{Deserialize, Serialize};

/// Link type used for the single SOCKS5 virtual device.
#[derive(Serialize, Deserialize, Clone, Copy, Debug, Default, PartialEq, Eq)]
#[cfg_attr(feature = "openapi", derive(utoipa::ToSchema))]
#[serde(rename_all = "snake_case")]
pub enum Socks5LinkKind {
    /// Try netkit first, fall back to veth on kernels without netkit.
    #[default]
    Auto,
    Veth,
    Netkit,
}

/// Singleton SOCKS5 virtual-device configuration.
///
/// `software` is also the host-side interface name (for example `mihomo`,
/// `sing-box` or `daed`), so the peer/netns are derived from it.
#[derive(Serialize, Deserialize, Clone, Debug)]
#[cfg_attr(feature = "openapi", derive(utoipa::ToSchema))]
pub struct Socks5VirtualConfig {
    pub software: String,
    pub server: String,
    pub port: u16,
    #[serde(default)]
    pub username: Option<String>,
    #[serde(default)]
    pub password: Option<String>,
    #[serde(default)]
    pub kind: Socks5LinkKind,
}

impl Socks5VirtualConfig {
    /// Host-side link name.  Keeping this <= 12 also keeps the peer
    /// (`<software>-p`) within Linux's 15-byte interface-name limit.
    pub fn host_name(&self) -> &str {
        self.software.trim()
    }

    pub fn peer_name(&self) -> String {
        format!("{}-p", self.host_name())
    }

    pub fn netns_name(&self) -> String {
        format!("landscape-{}", self.host_name())
    }

    pub fn validate(&self) -> Result<(), String> {
        let software = self.host_name();
        if software.is_empty() {
            return Err("software name is required".to_string());
        }
        if software.len() > 12 {
            return Err("software name must be at most 12 bytes".to_string());
        }
        if !software
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            return Err("software name may only contain ASCII letters, digits, '-' and '_'".to_string());
        }
        if software.eq_ignore_ascii_case("lo") {
            return Err("'lo' cannot be used as the software name".to_string());
        }
        if software.eq_ignore_ascii_case("socks5") {
            return Err("use the proxy software name (mihomo/sing-box/daed/...), not 'socks5'".to_string());
        }
        if self.server.trim().is_empty() {
            return Err("SOCKS5 server is required".to_string());
        }
        if self.port == 0 {
            return Err("SOCKS5 port must be 1..65535".to_string());
        }
        Ok(())
    }
}
