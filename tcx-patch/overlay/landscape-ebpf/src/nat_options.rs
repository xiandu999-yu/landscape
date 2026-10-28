//! Runtime options for Landscape's own IPv4 NAT (`nat_v4`).
//!
//! Stored as `$HOME/.landscape-router/nat-options.json` so the WebUI can edit
//! it. Environment variables still win, for scripted setups.
//!
//! ```json
//! { "full_cone": true, "tcp_syn_timeout_secs": 6, "tcp_timeout_secs": 600, "udp_timeout_secs": 300 }
//! ```

use std::path::PathBuf;
use std::sync::{OnceLock, RwLock};

const NS: u64 = 1_000_000_000;

#[derive(Clone, Debug)]
pub struct NatOptions {
    /// Endpoint-independent filtering (full cone) for dynamic mappings.
    pub full_cone: bool,
    pub tcp_syn_timeout_ns: u64,
    pub tcp_timeout_ns: u64,
    pub udp_timeout_ns: u64,
}

impl Default for NatOptions {
    fn default() -> Self {
        Self {
            full_cone: true,
            tcp_syn_timeout_ns: 6 * NS,
            tcp_timeout_ns: 600 * NS,
            udp_timeout_ns: 300 * NS,
        }
    }
}

fn options_path() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("LANDSCAPE_NAT_OPTIONS_FILE") {
        if !p.trim().is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    std::env::var("HOME")
        .ok()
        .map(|h| PathBuf::from(h).join(".landscape-router").join("nat-options.json"))
}

fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name).ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(default)
}

fn env_bool(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(v) => matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "on" | "yes"),
        Err(_) => default,
    }
}

fn secs(value: u64, default_ns: u64) -> u64 {
    if value == 0 { default_ns } else { value * NS }
}

impl NatOptions {
    pub fn load() -> Self {
        let mut opts = Self::default();

        if let Some(path) = options_path() {
            if let Ok(raw) = std::fs::read_to_string(&path) {
                match serde_json::from_str::<serde_json::Value>(&raw) {
                    Ok(doc) => {
                        if let Some(v) = doc.get("full_cone").and_then(|v| v.as_bool()) {
                            opts.full_cone = v;
                        }
                        for (key, dst) in [
                            ("tcp_syn_timeout_secs", &mut opts.tcp_syn_timeout_ns),
                            ("tcp_timeout_secs", &mut opts.tcp_timeout_ns),
                            ("udp_timeout_secs", &mut opts.udp_timeout_ns),
                        ] {
                            if let Some(v) = doc.get(key).and_then(|v| v.as_u64()) {
                                *dst = secs(v, *dst);
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!(path = %path.display(), error = %e, "invalid nat-options.json");
                    }
                }
            }
        }

        // Env overrides (scripted setups / one-off debugging).
        opts.full_cone = env_bool("LANDSCAPE_NAT_FULL_CONE", opts.full_cone);
        opts.tcp_syn_timeout_ns =
            secs(env_u64("LANDSCAPE_NAT_TCP_SYN_TIMEOUT_SECS", opts.tcp_syn_timeout_ns / NS),
                 opts.tcp_syn_timeout_ns);
        opts.tcp_timeout_ns =
            secs(env_u64("LANDSCAPE_NAT_TCP_TIMEOUT_SECS", opts.tcp_timeout_ns / NS),
                 opts.tcp_timeout_ns);
        opts.udp_timeout_ns =
            secs(env_u64("LANDSCAPE_NAT_UDP_TIMEOUT_SECS", opts.udp_timeout_ns / NS),
                 opts.udp_timeout_ns);

        opts
    }
}

static OPTIONS: OnceLock<RwLock<NatOptions>> = OnceLock::new();

fn slot() -> &'static RwLock<NatOptions> {
    OPTIONS.get_or_init(|| RwLock::new(NatOptions::load()))
}

/// Initialize from disk/env before the first NAT attach.
pub fn init() {
    let _ = slot();
}

/// Current options (used by `stages::nat4` when baking rodata).
pub fn current() -> NatOptions {
    slot().read().map(|g| g.clone()).unwrap_or_default()
}

/// Re-read disk/env after the WebUI writes `nat-options.json`.
pub fn reload() {
    if let Ok(mut guard) = slot().write() {
        *guard = NatOptions::load();
    }
}
