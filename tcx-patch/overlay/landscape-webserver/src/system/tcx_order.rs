//! Per-interface TCX chain-order and attach-mode configuration.

use std::io::Write;
use std::path::PathBuf;

use landscape_common::database::error::DbError;
use serde::{Deserialize, Serialize};

use crate::api::{JsonBody, LandscapeApiResp};
use crate::error::LandscapeApiResult;

#[derive(Debug, Clone, Serialize, Deserialize, utoipa::ToSchema)]
pub struct TcxIfaceRule {
    pub name: String,
    #[serde(default)]
    pub ingress: String,
    #[serde(default)]
    pub egress: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, utoipa::ToSchema)]
pub struct TcxOrderResponse {
    pub rules: Vec<TcxIfaceRule>,
    pub ifaces: Vec<String>,
    pub global_order: String,
    /// "auto" | "tcx" | "tc"
    pub mode: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, utoipa::ToSchema)]
pub struct UpdateTcxOrderRequest {
    pub rules: Vec<TcxIfaceRule>,
    #[serde(default = "default_mode")]
    pub mode: String,
}

fn default_mode() -> String {
    "auto".to_string()
}

#[derive(Debug, Deserialize, Serialize, Default)]
struct RulesFile {
    #[serde(default)]
    ifaces: Vec<TcxIfaceRule>,
}

fn rules_path() -> PathBuf {
    if let Ok(p) = std::env::var("LANDSCAPE_TCX_RULES_FILE") {
        if !p.trim().is_empty() {
            return PathBuf::from(p);
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".to_string());
    PathBuf::from(home).join(".landscape-router").join("tcx-order.json")
}

fn read_file() -> RulesFile {
    let path = rules_path();
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return RulesFile::default();
    };
    match serde_json::from_str::<RulesFile>(&raw) {
        Ok(file) => file,
        Err(e) => {
            tracing::warn!(path = %path.display(), error = %e, "invalid tcx-order.json");
            RulesFile::default()
        }
    }
}

fn write_file(file: &RulesFile) -> std::io::Result<()> {
    let path = rules_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut tmp = tempfile::NamedTempFile::new_in(
        path.parent().unwrap_or_else(|| std::path::Path::new("/tmp")),
    )?;
    tmp.write_all(serde_json::to_string_pretty(file)?.as_bytes())?;
    tmp.write_all(b"\n")?;
    tmp.persist(&path).map_err(|e| e.error)?;
    Ok(())
}

fn normalize_mode(value: &str) -> Result<String, DbError> {
    match value.trim().to_ascii_lowercase().as_str() {
        "auto" => Ok("auto".to_string()),
        "tcx" => Ok("tcx".to_string()),
        "tc" => Ok("tc".to_string()),
        other => Err(DbError::Internal(format!(
            "unsupported tcx mode: {other:?} (allowed: auto|tcx|tc)"
        ))),
    }
}



fn list_ifaces() -> Vec<String> {
    let mut out: Vec<String> = std::fs::read_dir("/sys/class/net")
        .map(|d| {
            d.filter_map(|e| e.ok())
                .filter_map(|e| e.file_name().into_string().ok())
                .filter(|n| n != "lo")
                .collect()
        })
        .unwrap_or_default();
    out.sort();
    out
}

fn normalize_order(kind: &str, value: &str, iface: &str) -> Result<String, DbError> {
    let v = value.trim();
    if v.is_empty() {
        return Ok(String::new());
    }
    let lower = v.to_ascii_lowercase();
    let ok = match lower.as_str() {
        "first" | "last" => true,
        _ => {
            if let Some(n) = lower.strip_prefix("position:") {
                n.trim().parse::<u32>().is_ok()
            } else if lower.starts_with("before:") || lower.starts_with("after:") {
                let name = v.split_once(':').map(|(_, n)| n.trim()).unwrap_or("");
                !name.is_empty()
            } else {
                false
            }
        }
    };
    if !ok {
        return Err(DbError::Internal(format!(
            "invalid tcx order for {iface} {kind}: {v:?} (allowed: first|last|position:N|before:PROG|after:PROG)"
        )));
    }
    Ok(v.to_string())
}

#[utoipa::path(
    get,
    path = "/tcx/order",
    tag = "System Config",
    operation_id = "get_tcx_order",
    responses((status = 200, description = "Success", body = TcxOrderResponse))
)]
pub async fn get_tcx_order() -> LandscapeApiResult<TcxOrderResponse> {
    let file = read_file();
    let global_order = std::env::var("LANDSCAPE_TCX_ORDER").unwrap_or_else(|_| "last".to_string());
    let mode = crate::system::fork::current_tcx_mode();
    LandscapeApiResp::success(TcxOrderResponse {
        rules: file.ifaces,
        ifaces: list_ifaces(),
        global_order,
        mode,
    })
}

#[utoipa::path(
    put,
    path = "/tcx/order",
    tag = "System Config",
    operation_id = "update_tcx_order",
    request_body = UpdateTcxOrderRequest,
    responses((status = 200, description = "Success"))
)]
pub async fn update_tcx_order(
    JsonBody(req): JsonBody<UpdateTcxOrderRequest>,
) -> LandscapeApiResult<()> {
    let known = list_ifaces();
    let mut rules = Vec::new();
    for rule in req.rules {
        let name = rule.name.trim().to_string();
        if name.is_empty() {
            continue;
        }
        if !known.iter().any(|n| n == &name) {
            return Err(DbError::Internal(format!("unknown interface: {name}")).into());
        }
        let ingress = normalize_order("ingress", &rule.ingress, &name)?;
        let egress = normalize_order("egress", &rule.egress, &name)?;
        if ingress.is_empty() && egress.is_empty() {
            continue;
        }
        rules.push(TcxIfaceRule { name, ingress, egress });
    }

    let mode = normalize_mode(&req.mode)?;
    write_file(&RulesFile { ifaces: rules })
        .map_err(|e| DbError::Internal(format!("write tcx-order.json: {e}")))?;
    crate::system::fork::set_tcx_mode(&mode)?;
    LandscapeApiResp::success(())
}
