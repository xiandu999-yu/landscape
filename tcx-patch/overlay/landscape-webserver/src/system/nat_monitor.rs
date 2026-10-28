//! Read-only NAT counters for the WebUI 监控分析 page.
//!
//! The `nat_v4` object exposes a small `napt_stats` array map. Instead of
//! plumbing BPF map FDs through the dataplane, this handler shells out to
//! `bpftool` (present on OpenWrt) and parses its JSON output. It is a
//! diagnostics endpoint, called on demand / every few seconds by the page.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::api::LandscapeApiResp;
use crate::error::LandscapeApiResult;

#[derive(Debug, Clone, Default, Serialize, Deserialize, utoipa::ToSchema)]
pub struct NatCounters {
    /// Port queue empty: a new flow could not get an external port.
    pub port_exhausted: u64,
    /// Metric ringbuf was full; the CT state machine kept going.
    pub metric_drop: u64,
    /// Port preservation succeeded.
    pub preserve_ok: u64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, utoipa::ToSchema)]
pub struct NatMonitorResponse {
    pub available: bool,
    pub counters: NatCounters,
    pub ct_max_entries: u64,
    /// Raw `bpftool` output, truncated; useful when parsing fails.
    pub raw: String,
}

fn bpftool_path() -> &'static str {
    for p in ["/sbin/bpftool", "/usr/sbin/bpftool", "/usr/bin/bpftool"] {
        if std::path::Path::new(p).exists() {
            return p;
        }
    }
    "bpftool"
}

async fn run_bpftool(args: Vec<String>) -> Option<String> {
    let path = bpftool_path().to_string();
    let handle = tokio::task::spawn_blocking(move || {
        std::process::Command::new(path).args(args).output().ok()
    });
    let output = tokio::time::timeout(Duration::from_secs(3), handle).await.ok()?.ok()??;
    if !output.status.success() {
        return None;
    }
    String::from_utf8(output.stdout).ok()
}

/// `bpftool -j` renders an integer map value either as a number or as a byte
/// array (`["0x2a","0x00",...]`), depending on the libbpf/bpftool version.
fn parse_value(v: &Value) -> u64 {
    match v {
        Value::Number(n) => n.as_u64().unwrap_or(0),
        Value::String(s) => {
            let s = s.trim();
            if let Some(hex) = s.strip_prefix("0x") {
                return u64::from_str_radix(hex, 16).unwrap_or(0);
            }
            s.parse::<u64>().unwrap_or(0)
        }
        Value::Array(items) => {
            let mut bytes = [0u8; 8];
            let mut n = 0usize;
            for item in items {
                let text = match item {
                    Value::String(s) => s.clone(),
                    Value::Number(num) => format!("0x{:x}", num.as_u64().unwrap_or(0)),
                    _ => continue,
                };
                if let Some(hex) = text.trim().strip_prefix("0x") {
                    if let Ok(b) = u8::from_str_radix(hex, 16) {
                        if n < bytes.len() {
                            bytes[n] = b;
                            n += 1;
                        }
                    }
                }
            }
            u64::from_le_bytes(bytes)
        }
        _ => 0,
    }
}

fn parse_counters(raw: &str) -> Option<NatCounters> {
    let entries: Vec<Value> = serde_json::from_str(raw).ok()?;
    let mut counters = NatCounters::default();
    for entry in entries {
        let key = entry.get("key").map(parse_value).unwrap_or(0);
        // Per-CPU maps wrap entries as {"values":[{"cpu":0,"value":N}, ...]}.
        let value = if let Some(values) = entry.get("values").and_then(|v| v.as_array()) {
            values.iter().map(|v| v.get("value").map(parse_value).unwrap_or(0)).sum()
        } else {
            entry.get("value").map(parse_value).unwrap_or(0)
        };
        match key {
            0 => counters.port_exhausted = value,
            1 => counters.metric_drop = value,
            2 => counters.preserve_ok = value,
            _ => {}
        }
    }
    Some(counters)
}

#[utoipa::path(
    get,
    path = "/nat/monitor",
    tag = "System Config",
    operation_id = "get_nat_monitor",
    responses((status = 200, description = "Success", body = NatMonitorResponse))
)]
pub async fn get_nat_monitor() -> LandscapeApiResult<NatMonitorResponse> {
    let mut resp = NatMonitorResponse::default();

    if let Some(raw) =
        run_bpftool(vec!["-j".into(), "map".into(), "dump".into(), "name".into(), "napt_stats".into()])
            .await
    {
        resp.available = true;
        if let Some(counters) = parse_counters(&raw) {
            resp.counters = counters;
        }
        resp.raw = raw.chars().take(4096).collect();
    }

    if let Some(raw) =
        run_bpftool(vec!["-j".into(), "map".into(), "show".into(), "name".into(), "napt_ct".into()])
            .await
    {
        if let Ok(maps) = serde_json::from_str::<Vec<Value>>(&raw) {
            resp.ct_max_entries = maps
                .first()
                .and_then(|m| m.get("max_entries"))
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
        }
    }

    LandscapeApiResp::success(resp)
}
