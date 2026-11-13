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
    /// `cake` or `fq_codel` (leaf qdisc).
    #[serde(default = "default_qdisc")]
    pub qos_qdisc: String,
    /// `htb` (single precise), `mq` (per-hardware-queue) or `ifb` (redirect).
    #[serde(default = "default_mode")]
    pub qos_mode: String,
    /// TCX attach mode: `auto`, `tcx` or `tc`.
    #[serde(default = "default_tcx_mode")]
    pub tcx_mode: String,
    /// Per-interface overrides. Empty entries inherit the global policy.
    #[serde(default)]
    pub iface_qos: Vec<IfaceQosRule>,
}

#[derive(Debug, Clone, Serialize, Deserialize, utoipa::ToSchema)]
pub struct IfaceQosRule {
    pub name: String,
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub upload_kbit: u32,
    #[serde(default)]
    pub download_kbit: u32,
}

fn default_true() -> bool {
    true
}
fn default_qdisc() -> String {
    "cake".to_string()
}
fn default_mode() -> String {
    "htb".to_string()
}
fn default_tcx_mode() -> String {
    "auto".to_string()
}

impl Default for ForkConfig {
    fn default() -> Self {
        Self {
            qos_enabled: false,
            fastpath_monitor: true,
            qos_upload_kbit: 0,
            qos_download_kbit: 0,
            qos_qdisc: default_qdisc(),
            qos_mode: default_mode(),
            tcx_mode: default_tcx_mode(),
            iface_qos: Vec::new(),
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

pub(crate) fn current_tcx_mode() -> String {
    read_config().tcx_mode
}

pub(crate) fn set_tcx_mode(mode: &str) -> Result<(), DbError> {
    let mut config = read_config();
    config.tcx_mode = mode.to_string();
    // Selecting legacy TC invalidates QoS. Disable it in the same save so the
    // mode switch itself never fails with "QoS requires TCX".
    if mode == "tc" && config.qos_enabled {
        config.qos_enabled = false;
        tracing::warn!("tcx mode changed to tc; global QoS disabled");
    }
    validate(&config)?;
    write_config(&config)
        .map_err(|error| DbError::Internal(format!("write fork.json: {error}")))?;
    landscape_ebpf::tcx::reload_from_env();
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
    if config.qos_mode != "htb" && config.qos_mode != "mq" && config.qos_mode != "ifb" {
        return Err(DbError::Internal(format!(
            "unsupported qos mode: {}",
            config.qos_mode
        )));
    }
    if config.tcx_mode != "auto" && config.tcx_mode != "tcx" && config.tcx_mode != "tc" {
        return Err(DbError::Internal(format!(
            "unsupported tcx mode: {}",
            config.tcx_mode
        )));
    }
    for rule in &config.iface_qos {
        if rule.name.trim().is_empty() {
            return Err(DbError::Internal("iface QoS name is empty".to_string()));
        }
        if rule.upload_kbit > 100_000_000 || rule.download_kbit > 100_000_000 {
            return Err(DbError::Internal(format!(
                "iface QoS rate too large: {}",
                rule.name
            )));
        }
    }
    let effective_tcx = if config.tcx_mode == "tc" {
        landscape_ebpf::tcx::Mode::Off
    } else {
        landscape_ebpf::tcx::current_mode()
    };
    if effective_tcx == landscape_ebpf::tcx::Mode::Off && config.qos_enabled {
        return Err(DbError::Internal(
            "global QoS requires TCX; use per-interface QoS in TC mode".to_string(),
        ));
    }
    Ok(())
}

async fn apply_shaper(app: &LandscapeApp, config: &ForkConfig) {
    let wan_configs = app
        .iface_config_service
        .get_all_wan_iface_config()
        .await;
    let wans = wan_configs.iter().map(|iface| iface.name.clone()).collect::<Vec<_>>();
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

    // Scope rule: any WAN on TCX -> one global policy. Only when every WAN
    // is legacy TC do the per-interface rules take over.
    let any_tcx = if wan_configs.is_empty() {
        match config.tcx_mode.as_str() {
            "tc" => false,
            "tcx" => true,
            _ => landscape_ebpf::tcx::current_mode() != landscape_ebpf::tcx::Mode::Off,
        }
    } else {
        wan_configs.iter().any(|iface| {
            landscape_ebpf::tcx::ifindex_of_name(&iface.name)
                .is_some_and(landscape_ebpf::tcx::wants_tcx)
        })
    };

    let rule_for = |name: &str| config.iface_qos.iter().find(|r| r.name == name);
    for iface in &lan_configs {
        if !targets.iter().any(|target| target == &iface.name) {
            super::qos_shaper::clear_iface(&iface.name);
        }
    }

    if any_tcx {
        // Global mode ignores per-interface rules.
        if !config.qos_enabled {
            for iface in &wans {
                super::qos_shaper::clear_iface(iface);
            }
            for iface in &targets {
                super::qos_shaper::clear_iface(iface);
            }
            return;
        }

        for iface in &wans {
            if let Err(error) = super::qos_shaper::apply_iface(
                iface,
                config.qos_upload_kbit,
                "upload",
                &config.qos_qdisc,
                &config.qos_mode,
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
                &config.qos_mode,
            ) {
                tracing::warn!(%iface, %error, "qos shaper: download failed");
            }
        }
        return;
    }

    // All WAN are legacy TC: independent per-interface policy only.
    for iface in &wans {
        let rate = rule_for(iface)
            .filter(|rule| rule.enabled)
            .map(|rule| rule.upload_kbit)
            .unwrap_or(0);
        if rate > 0 {
            if let Err(error) = super::qos_shaper::apply_iface(
                iface,
                rate,
                "upload",
                &config.qos_qdisc,
                &config.qos_mode,
            ) {
                tracing::warn!(%iface, %error, "iface qos: upload failed");
            }
        } else {
            super::qos_shaper::clear_iface(iface);
        }
    }
    for iface in &targets {
        let rate = rule_for(iface)
            .filter(|rule| rule.enabled)
            .map(|rule| rule.download_kbit)
            .unwrap_or(0);
        if rate > 0 {
            if let Err(error) = super::qos_shaper::apply_iface(
                iface,
                rate,
                "download",
                &config.qos_qdisc,
                &config.qos_mode,
            ) {
                tracing::warn!(%iface, %error, "iface qos: download failed");
            }
        } else {
            super::qos_shaper::clear_iface(iface);
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

#[utoipa::path(
    get,
    path = "/fork/iface-qos",
    tag = "System Config",
    operation_id = "get_iface_qos",
    responses((status = 200, description = "Success", body = Vec<IfaceQosRule>))
)]
pub async fn get_iface_qos() -> LandscapeApiResult<Vec<IfaceQosRule>> {
    LandscapeApiResp::success(read_config().iface_qos)
}

#[utoipa::path(
    put,
    path = "/fork/iface-qos",
    tag = "System Config",
    operation_id = "update_iface_qos",
    request_body = IfaceQosRule,
    responses((status = 200, description = "Success"))
)]
pub async fn update_iface_qos(
    State(app): State<LandscapeApp>,
    JsonBody(rule): JsonBody<IfaceQosRule>,
) -> LandscapeApiResult<()> {
    let name = rule.name.trim().to_string();
    if name.is_empty() {
        return Err(DbError::Internal("iface QoS name is empty".to_string()).into());
    }
    if rule.upload_kbit > 100_000_000 || rule.download_kbit > 100_000_000 {
        return Err(DbError::Internal("iface QoS rate too large".to_string()).into());
    }
    let config = read_config();
    let _ = &config;

    let mut next = config.clone();
    next.iface_qos.retain(|r| r.name != name);
    if rule.enabled || rule.upload_kbit > 0 || rule.download_kbit > 0 {
        next.iface_qos.push(IfaceQosRule { name, ..rule });
    }
    validate(&next)?;
    write_config(&next)
        .map_err(|error| DbError::Internal(format!("write fork.json: {error}")))?;
    apply_shaper(&app, &next).await;
    LandscapeApiResp::success(())
}

/// Re-apply the saved qdiscs after the interface services have started.
pub async fn restore_shaper(app: &LandscapeApp) {
    let config = read_config();
    apply_shaper(app, &config).await;
}

/// Re-read fork.json and re-apply qdiscs (used after TCX mode changes).
pub async fn refresh_shaper(app: &LandscapeApp) {
    let config = read_config();
    apply_shaper(app, &config).await;
}

/// Remove this fork's HTB roots on shutdown. eBPF maps stay pinned and are
/// reused on the next start.
pub async fn clear_shaper(app: &LandscapeApp) {
    let mut config = read_config();
    config.qos_enabled = false;
    config.iface_qos.clear();
    apply_shaper(app, &config).await;
}

/// Re-apply the saved forwarding rates after the eBPF maps are pinned.
pub fn apply_saved(root: &Path) {
    let config = read_config();
    if let Err(error) = landscape_ebpf::qos_runtime::update(root, runtime(&config)) {
        tracing::warn!(%error, "qos: restore saved policy failed");
    }
}

async fn any_wan_tcx(app: &LandscapeApp) -> bool {
    app.iface_config_service
        .get_all_wan_iface_config()
        .await
        .iter()
        .any(|iface| {
            landscape_ebpf::tcx::ifindex_of_name(&iface.name)
                .is_some_and(landscape_ebpf::tcx::wants_tcx)
        })
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
    if config.qos_enabled && !any_wan_tcx(&app).await {
        return Err(DbError::Internal(
            "global QoS requires at least one WAN on TCX; use per-interface QoS when all WAN are TC"
                .to_string(),
        )
        .into());
    }
    validate(&config)?;
    write_config(&config)
        .map_err(|error| DbError::Internal(format!("write fork.json: {error}")))?;

    if let Err(error) = landscape_ebpf::qos_runtime::update(&app.ebpf_paths.root, runtime(&config))
    {
        tracing::warn!(%error, "qos: runtime map update failed");
    }
    landscape_ebpf::tcx::reload_from_env();
    apply_shaper(&app, &config).await;
    LandscapeApiResp::success(())
}
