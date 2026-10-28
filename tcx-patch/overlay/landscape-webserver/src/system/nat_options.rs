//! Landscape NAT4 runtime options (full cone + CT timeouts), editable in WebUI.

use std::io::Write;
use std::path::PathBuf;

use landscape_common::database::error::DbError;
use serde::{Deserialize, Serialize};

use crate::api::{JsonBody, LandscapeApiResp};
use crate::error::LandscapeApiResult;

#[derive(Debug, Clone, Serialize, Deserialize, utoipa::ToSchema)]
pub struct NatOptionsDto {
    /// Endpoint-independent filtering (full cone) for dynamic mappings.
    pub full_cone: bool,
    #[serde(default = "default_tcp_syn")]
    pub tcp_syn_timeout_secs: u64,
    #[serde(default = "default_tcp")]
    pub tcp_timeout_secs: u64,
    #[serde(default = "default_udp_min")]
    pub udp_min_timeout_secs: u64,
    #[serde(default = "default_udp")]
    pub udp_timeout_secs: u64,
    #[serde(default = "default_icmp")]
    pub icmp_timeout_secs: u64,
}

fn default_tcp_syn() -> u64 {
    120
}
fn default_tcp() -> u64 {
    86400
}
fn default_udp_min() -> u64 {
    120
}
fn default_udp() -> u64 {
    600
}
fn default_icmp() -> u64 {
    60
}

impl Default for NatOptionsDto {
    fn default() -> Self {
        Self {
            full_cone: true,
            tcp_syn_timeout_secs: default_tcp_syn(),
            tcp_timeout_secs: default_tcp(),
            udp_min_timeout_secs: default_udp_min(),
            udp_timeout_secs: default_udp(),
            icmp_timeout_secs: default_icmp(),
        }
    }
}

fn options_path() -> PathBuf {
    if let Ok(p) = std::env::var("LANDSCAPE_NAT_OPTIONS_FILE") {
        if !p.trim().is_empty() {
            return PathBuf::from(p);
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".to_string());
    PathBuf::from(home).join(".landscape-router").join("nat-options.json")
}

fn read_options() -> NatOptionsDto {
    let path = options_path();
    let Ok(raw) = std::fs::read_to_string(&path) else { return NatOptionsDto::default() };
    serde_json::from_str(&raw).unwrap_or_else(|e| {
        tracing::warn!(path = %path.display(), error = %e, "invalid nat-options.json");
        NatOptionsDto::default()
    })
}

fn write_options(dto: &NatOptionsDto) -> std::io::Result<()> {
    let path = options_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut tmp = tempfile::NamedTempFile::new_in(
        path.parent().unwrap_or_else(|| std::path::Path::new("/tmp")),
    )?;
    tmp.write_all(serde_json::to_string_pretty(dto)?.as_bytes())?;
    tmp.write_all(b"\n")?;
    tmp.persist(&path).map_err(|e| e.error)?;
    Ok(())
}

fn validate(dto: &NatOptionsDto) -> Result<(), DbError> {
    for (name, value) in [
        ("tcp_syn_timeout_secs", dto.tcp_syn_timeout_secs),
        ("tcp_timeout_secs", dto.tcp_timeout_secs),
        ("udp_min_timeout_secs", dto.udp_min_timeout_secs),
        ("udp_timeout_secs", dto.udp_timeout_secs),
        ("icmp_timeout_secs", dto.icmp_timeout_secs),
    ] {
        if value == 0 || value > 86_400 {
            return Err(DbError::Internal(format!(
                "{name} must be within 1..=86400 (got {value})"
            )));
        }
    }
    Ok(())
}

#[utoipa::path(
    get,
    path = "/nat/options",
    tag = "System Config",
    operation_id = "get_nat_options",
    responses((status = 200, description = "Success", body = NatOptionsDto))
)]
pub async fn get_nat_options() -> LandscapeApiResult<NatOptionsDto> {
    LandscapeApiResp::success(read_options())
}

#[utoipa::path(
    put,
    path = "/nat/options",
    tag = "System Config",
    operation_id = "update_nat_options",
    request_body = NatOptionsDto,
    responses((status = 200, description = "Success"))
)]
pub async fn update_nat_options(JsonBody(dto): JsonBody<NatOptionsDto>) -> LandscapeApiResult<()> {
    validate(&dto)?;
    write_options(&dto).map_err(|e| DbError::Internal(format!("write nat-options.json: {e}")))?;
    landscape_ebpf::nat_options::reload();
    LandscapeApiResp::success(())
}
