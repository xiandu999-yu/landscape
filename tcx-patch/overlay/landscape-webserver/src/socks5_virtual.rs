//! Singleton SOCKS5 virtual device.
//!
//! Creating the SOCKS5 configuration also creates its veth/netkit pair and
//! netns.  Only one device may exist; a second create is rejected.  No Docker
//! or OpenWrt configuration is touched.

use std::{
    path::PathBuf,
    process::Command,
    sync::Arc,
};

use axum::extract::State;
use landscape::{
    config_service::iface_service::IfaceManagerService,
    get_iface_by_name,
};
use landscape_common::{
    config_service::iface::{IfaceZoneType, Socks5LinkKind, Socks5VirtualConfig},
    database::error::DbError,
    dev::iface::ChangeZone,
    service::controller::ConfigStoreController,
};
use tokio::sync::Mutex;

use crate::{
    LandscapeApp,
    api::{JsonBody, LandscapeApiResp},
    error::LandscapeApiResult,
};

const SOCKS5_CONFIG_FILE: &str = "socks5-virtual.json";
const HOST_ADDR: &str = "10.255.0.1/24";
const PEER_ADDR: &str = "10.255.0.2/24";
const PEER_GATEWAY: &str = "10.255.0.1";

#[derive(Clone)]
pub struct Socks5VirtualService {
    home_path: Arc<PathBuf>,
    inner: Arc<Mutex<Option<Socks5VirtualConfig>>>,
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
        }
    }

    pub async fn get(&self) -> Option<Socks5VirtualConfig> {
        self.inner.lock().await.as_ref().cloned()
    }

    pub async fn create(
        &self,
        config: Socks5VirtualConfig,
        iface: &IfaceManagerService,
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

        *guard = Some(config.clone());
        Ok(config)
    }

    pub async fn delete(&self, iface: &IfaceManagerService) -> Result<(), DbError> {
        let mut guard = self.inner.lock().await;
        let Some(config) = guard.take() else {
            return Ok(());
        };

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
        .create(config, &state.iface_config_service)
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
        .delete(&state.iface_config_service)
        .await?;
    LandscapeApiResp::success(())
}
