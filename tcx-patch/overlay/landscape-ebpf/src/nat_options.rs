//! Runtime options for Landscape's own IPv4 NAT (`nat_v4`).
//!
//! Stored as `$HOME/.landscape-router/nat-options.json` so the WebUI can edit
//! it. Environment variables still win, for scripted setups.
//!
//! Defaults follow einat's NAT timers (tcp_trans=2m, tcp_est=24h,
//! udp_default=10m, icmp=1m) so a migrated setup keeps the same behavior.
//!
//! ```json
//! { "full_cone": true, "tcp_syn_timeout_secs": 120, "tcp_timeout_secs": 86400,
//!   "udp_timeout_secs": 600, "icmp_timeout_secs": 60 }
//! ```

use std::path::PathBuf;
use std::sync::{Mutex, OnceLock, RwLock};

const NS: u64 = 1_000_000_000;

/// Dup'd fds of every attached `land_nat_dyn` map (one per WAN link: each link
/// loads its own NAT skeleton). Held so `reload()` can push new values into the
/// live programs; `stages::nat4` appends on every attach.
static CFG_MAP_FDS: OnceLock<Mutex<Vec<i32>>> = OnceLock::new();

/// Upper bound on remembered maps; stale entries only keep a tiny detached map
/// alive, so a small cap is enough for any multi-WAN setup.
const CFG_MAP_FD_MAX: usize = 16;

fn cfg_fds() -> &'static Mutex<Vec<i32>> {
    CFG_MAP_FDS.get_or_init(|| Mutex::new(Vec::new()))
}

/// Mirror of `struct land_nat_dyn_cfg` in `nat_v4/tc_nat4_rodata.h`.
#[repr(C)]
struct NatDynCfg {
    report_interval_ns: u64,
    event_driven: u8,
    pad: [u8; 7],
}

/// Bind the loaded `land_nat_dyn` map and push the current options into it.
pub fn bind_cfg_map(fd: i32) {
    if fd < 0 {
        return;
    }
    let dup = unsafe { libc::dup(fd) };
    if dup < 0 {
        tracing::warn!("failed to dup land_nat_dyn map fd: {}", std::io::Error::last_os_error());
        return;
    }
    {
        let mut fds = match cfg_fds().lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        if fds.len() >= CFG_MAP_FD_MAX {
            let old = fds.remove(0);
            unsafe { libc::close(old) };
        }
        fds.push(dup);
    }
    push_cfg();
}

/// Write the timer-model knobs into every attached NAT program (no-op before
/// the first attach).
fn push_cfg() {
    let fds: Vec<i32> = match cfg_fds().lock() {
        Ok(guard) => guard.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    };
    if fds.is_empty() {
        return;
    }
    let opts = current();
    let cfg = NatDynCfg {
        report_interval_ns: opts.report_interval_ns,
        event_driven: u8::from(opts.event_driven),
        pad: [0; 7],
    };
    let key: u32 = 0;
    for fd in fds {
        let ret = unsafe {
            libbpf_rs::libbpf_sys::bpf_map_update_elem(
                fd,
                (&key as *const u32).cast(),
                (&cfg as *const NatDynCfg).cast(),
                0,
            )
        };
        if ret != 0 {
            tracing::warn!(ret, fd, "failed to push nat runtime cfg into land_nat_dyn");
        }
    }
}

#[derive(Clone, Debug)]
pub struct NatOptions {
    /// Endpoint-independent filtering (full cone) for dynamic mappings.
    pub full_cone: bool,
    /// TCP half-open (SYN, or not fully established) timeout.
    pub tcp_syn_timeout_ns: u64,
    /// TCP established idle timeout.
    pub tcp_timeout_ns: u64,
    /// UDP timeout before any reply is seen (einat's `timeout_udp_min`).
    pub udp_min_timeout_ns: u64,
    /// UDP idle timeout once the peer has answered.
    pub udp_timeout_ns: u64,
    /// ICMP idle timeout.
    pub icmp_timeout_ns: u64,
    /// CT timer / metric report interval (the state machine tick).
    pub report_interval_ns: u64,
    /// Event-driven CT timers: no periodic tick, timer re-armed with the idle
    /// timeout on every packet. Disables per-CT metric updates.
    pub event_driven: bool,
}

impl Default for NatOptions {
    fn default() -> Self {
        Self {
            full_cone: true,
            tcp_syn_timeout_ns: 120 * NS,
            tcp_timeout_ns: 24 * 3600 * NS,
            udp_min_timeout_ns: 120 * NS,
            udp_timeout_ns: 600 * NS,
            icmp_timeout_ns: 60 * NS,
            report_interval_ns: 30 * NS,
            event_driven: true,
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

/// Whether Landscape's metric service is configured as `off` in
/// `$HOME/.landscape-router/landscape.toml`. Missing config means the default
/// mode (`Persistent`), i.e. metrics on.
fn metric_mode_off() -> bool {
    let Ok(home) = std::env::var("HOME") else { return false };
    let path = std::path::Path::new(&home)
        .join(landscape_common::LANDSCAPE_CONFIG_DIR_NAME)
        .join(landscape_common::LAND_CONFIG);
    let Ok(text) = std::fs::read_to_string(&path) else { return false };

    let mut in_metric = false;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            in_metric = line == "[metric]";
            continue;
        }
        if !in_metric {
            continue;
        }
        if let Some((key, value)) = line.split_once('=') {
            if key.trim() == "mode" {
                let value = value.trim().trim_matches('"').to_ascii_lowercase();
                return value == "off";
            }
        }
    }
    false
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
                        if let Some(v) = doc.get("event_driven").and_then(|v| v.as_bool()) {
                            opts.event_driven = v;
                        }
                        for (key, dst) in [
                            ("tcp_syn_timeout_secs", &mut opts.tcp_syn_timeout_ns),
                            ("tcp_timeout_secs", &mut opts.tcp_timeout_ns),
                            ("udp_min_timeout_secs", &mut opts.udp_min_timeout_ns),
                            ("udp_timeout_secs", &mut opts.udp_timeout_ns),
                            ("icmp_timeout_secs", &mut opts.icmp_timeout_ns),
                            ("report_interval_secs", &mut opts.report_interval_ns),
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
        // An explicit LANDSCAPE_NAT_EVENT_DRIVEN wins over the metric-mode
        // fallback below: it lets an init script pick the timer model without
        // editing landscape.toml (event mode keeps the NAT-session page empty
        // for active flows because it never emits periodic ACTIVE reports).
        let event_driven_env = std::env::var("LANDSCAPE_NAT_EVENT_DRIVEN").ok().map(|v| {
            matches!(
                v.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "on" | "yes"
            )
        });
        if let Some(v) = event_driven_env {
            opts.event_driven = v;
        }
        opts.tcp_syn_timeout_ns =
            secs(env_u64("LANDSCAPE_NAT_TCP_SYN_TIMEOUT_SECS", opts.tcp_syn_timeout_ns / NS),
                 opts.tcp_syn_timeout_ns);
        opts.tcp_timeout_ns =
            secs(env_u64("LANDSCAPE_NAT_TCP_TIMEOUT_SECS", opts.tcp_timeout_ns / NS),
                 opts.tcp_timeout_ns);
        opts.udp_min_timeout_ns =
            secs(env_u64("LANDSCAPE_NAT_UDP_MIN_TIMEOUT_SECS", opts.udp_min_timeout_ns / NS),
                 opts.udp_min_timeout_ns);
        opts.udp_timeout_ns =
            secs(env_u64("LANDSCAPE_NAT_UDP_TIMEOUT_SECS", opts.udp_timeout_ns / NS),
                 opts.udp_timeout_ns);
        opts.icmp_timeout_ns =
            secs(env_u64("LANDSCAPE_NAT_ICMP_TIMEOUT_SECS", opts.icmp_timeout_ns / NS),
                 opts.icmp_timeout_ns);
        opts.report_interval_ns =
            secs(env_u64("LANDSCAPE_NAT_REPORT_INTERVAL_SECS", opts.report_interval_ns / NS),
                 opts.report_interval_ns);

        // Metric mode drives the timer model: an enabled metric service needs
        // periodic 5s reporting, so fall back from event-driven mode. Only a
        // literal `[metric] mode = "off"` (or an explicit env override) keeps
        // event-driven timers.
        if !metric_mode_off() && event_driven_env.is_none() {
            opts.event_driven = false;
            opts.report_interval_ns = 5 * NS;
        }

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

/// Re-read disk/env after the WebUI writes `nat-options.json`, or after the
/// metric mode changes (it drives `event_driven` / `report_interval_ns`).
pub fn reload() {
    if let Ok(mut guard) = slot().write() {
        *guard = NatOptions::load();
    }
    push_cfg();
}
