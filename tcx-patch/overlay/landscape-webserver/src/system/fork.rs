//! Fork-only runtime switches and global forwarding QoS policy.
//!
//! Features that bake values into BPF rodata or need a new TCX link (full
//! cone, ingress fast path, TCX layout) intentionally stay out of this file.

use std::io::Write;
use std::path::{Path, PathBuf};

use axum::extract::State;
use landscape_common::config_service::iface::{CreateDevType, IfaceZoneType};
use landscape_common::database::error::DbError;
use landscape_common::service::controller::ConfigStoreController;
use serde::{Deserialize, Serialize};

use crate::api::{JsonBody, LandscapeApiResp};
use crate::error::LandscapeApiResult;
use crate::LandscapeApp;

#[derive(Debug, Clone, Serialize, Deserialize, utoipa::ToSchema)]
pub struct ForkConfig {
    /// Runtime gate for the forwarding-path QoS work.
    #[serde(default)]
    pub qos_enabled: bool,
    /// Whether the monitoring page should expose fast-path counters.
    #[serde(default = "default_true")]
    pub fastpath_monitor: bool,
    /// Forwarded-traffic upload rate. 0 = no aggregate limit.
    #[serde(default)]
    pub qos_upload_kbit: u32,
    /// Forwarded-traffic download rate. 0 = no aggregate limit.
    #[serde(default)]
    pub qos_download_kbit: u32,
    /// `cake` or `fq_codel` (reserved for the qdisc stage).
    #[serde(default = "default_qdisc")]
    pub qos_qdisc: String,
}

fn default_true() -> bool {
    true
}
fn default_qdisc() -> String {
    "cake".to_string()
}

impl Default for ForkConfig {
    fn default() -> Self {
        Self {
            qos_enabled: false,
            fastpath_monitor: true,
            qos_upload_kbit: 0,
            qos_download_kbit: 0,
            qos_qdisc: default_qdisc(),
        }
    }
}

fn config_path() -> PathBuf {
    if let Ok(path) = std::env::var("LANDSCAPE_FORK_CONFIG_FILE") {
        if !path.trim().is_empty() {
            return PathBuf::from(path);
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".to_string());
    PathBuf::from(home).join(".landscape-router").join("fork.json")
}

fn read_config() -> ForkConfig {
    let path = config_path();
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return ForkConfig::default();
    };
    serde_json::from_str(&raw).unwrap_or_else(|error| {
        tracing::warn!(path = %path.display(), %error, "invalid fork.json");
        ForkConfig::default()
    })
}

fn write_config(config: &ForkConfig) -> std::io::Result<()> {
    let path = config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut tmp = tempfile::NamedTempFile::new_in(
        path.parent().unwrap_or_else(|| Path::new("/tmp")),
    )?;
    tmp.write_all(serde_json::to_string_pretty(config)?.as_bytes())?;
    tmp.write_all(b"\n")?;
    tmp.persist(&path).map_err(|error| error.error)?;
    Ok(())
}

fn validate(config: &ForkConfig) -> Result<(), DbError> {
    if config.qos_upload_kbit > 100_000_000 || config.qos_download_kbit > 100_000_000 {
        return Err(DbError::Internal(
            "QoS rate must be <= 100000000 kbit/s".to_string(),
        ));
    }
    if config.qos_qdisc != "cake" && config.qos_qdisc != "fq_codel" {
        return Err(DbError::Internal(format!(
            "unsupported qdisc: {}",
            config.qos_qdisc
        )));
    }
    Ok(())
}

async fn apply_shaper(app: &LandscapeApp, config: &ForkConfig) {
    // QoS is intentionally single-WAN: the first configured WAN is used.
    let wan = app
        .iface_config_service
        .get_all_wan_iface_config()
        .await
        .into_iter()
        .next()
        .map(|iface| iface.name);
    let lan_configs = app
        .iface_config_service
        .list()
        .await
        .unwrap_or_default()
        .into_iter()
        .filter(|iface| iface.zone_type == IfaceZoneType::Lan)
        .collect::<Vec<_>>();

    // Traffic normally leaves through a bridge. If no bridge exists, shape
    // every LAN port; otherwise shape the bridges and clear the slaves' old
    // qdiscs.
    let bridges = lan_configs
        .iter()
        .filter(|iface| iface.create_dev_type == CreateDevType::Bridge)
        .map(|iface| iface.name.clone())
        .collect::<Vec<_>>();
    let targets = if bridges.is_empty() {
        lan_configs
            .iter()
            .map(|iface| iface.name.clone())
            .collect::<Vec<_>>()
    } else {
        bridges
    };

    if !config.qos_enabled {
        if let Some(iface) = wan.as_deref() {
            super::qos_shaper::clear_iface(iface);
        }
        for iface in &lan_configs {
            super::qos_shaper::clear_iface(&iface.name);
        }
        return;
    }

    for iface in &lan_configs {
        if !targets.iter().any(|target| target == &iface.name) {
            super::qos_shaper::clear_iface(&iface.name);
        }
    }

    if let Some(iface) = wan.as_deref() {
        if let Err(error) = super::qos_shaper::apply_iface(
            iface,
            config.qos_upload_kbit,
            "upload",
            &config.qos_qdisc,
        ) {
            tracing::warn!(%iface, %error, "qos shaper: upload failed");
        }
    }
    for iface in &targets {
        if let Err(error) = super::qos_shaper::apply_iface(
            iface,
            config.qos_download_kbit,
            "download",
            &config.qos_qdisc,
        ) {
            tracing::warn!(%iface, %error, "qos shaper: download failed");
        }
    }
}

fn runtime(config: &ForkConfig) -> landscape_ebpf::qos_runtime::QosRuntime {
    landscape_ebpf::qos_runtime::QosRuntime {
        enabled: config.qos_enabled,
        upload_bps: u64::from(config.qos_upload_kbit) * 1000 / 8,
        download_bps: u64::from(config.qos_download_kbit) * 1000 / 8,
    }
}

#[utoipa::path(
    get,
    path = "/fork/config",
    tag = "System Config",
    operation_id = "get_fork_config",
    responses((status = 200, description = "Success", body = ForkConfig))
)]
pub async fn get_fork_config() -> LandscapeApiResult<ForkConfig> {
    LandscapeApiResp::success(read_config())
}

/// Re-apply the saved qdiscs after the interface services have started.
pub async fn restore_shaper(app: &LandscapeApp) {
    let config = read_config();
    apply_shaper(app, &config).await;
}

/// Remove this fork's HTB roots on shutdown. eBPF maps stay pinned and are
/// reused on the next start.
pub async fn clear_shaper(app: &LandscapeApp) {
    let mut config = read_config();
    config.qos_enabled = false;
    apply_shaper(app, &config).await;
}

/// Re-apply the saved forwarding rates after the eBPF maps are pinned.
pub fn apply_saved(root: &Path) {
    let config = read_config();
    if let Err(error) = landscape_ebpf::qos_runtime::update(root, runtime(&config)) {
        tracing::warn!(%error, "qos: restore saved policy failed");
    }
}

#[utoipa::path(
    put,
    path = "/fork/config",
    tag = "System Config",
    operation_id = "update_fork_config",
    request_body = ForkConfig,
    responses((status = 200, description = "Success"))
)]
pub async fn update_fork_config(
    State(app): State<LandscapeApp>,
    JsonBody(config): JsonBody<ForkConfig>,
) -> LandscapeApiResult<()> {
    validate(&config)?;
    write_config(&config)
        .map_err(|error| DbError::Internal(format!("write fork.json: {error}")))?;

    if let Err(error) = landscape_ebpf::qos_runtime::update(&app.ebpf_paths.root, runtime(&config))
    {
        tracing::warn!(%error, "qos: runtime map update failed");
    }
    apply_shaper(&app, &config).await;
    LandscapeApiResp::success(())
}
