//! Per-protocol egress pipes (UDP / ICMP / TCP) for Landscape-forwarded WAN
//! traffic.
//!
//! Landscape forwards outside netfilter and rewrites headers in TC eBPF, so two
//! of the usual knobs do not work:
//!   * nftables marks (`meta l4proto udp ... meta mark set`) never see the
//!     forwarded packets, because they do not enter the netfilter hooks;
//!   * TC BPF cannot write `skb->priority` (the verifier rejects the store), so
//!     a program cannot choose a qdisc band either.
//!
//! What does work: `bpf_redirect()` ends in `dev_queue_xmit()`, so the WAN root
//! qdisc still sees every forwarded packet. This module installs an HTB tree
//! with three priority classes and two `u32` classifiers:
//!
//! ```text
//! root 1: htb default 30
//!   `- 1:1  rate=<line rate>
//!        |- 1:10  UDP   prio 0  + fq_codel
//!        |- 1:20  ICMP  prio 1  + fq_codel
//!        `- 1:30  other prio 2  + fq_codel   (TCP by default)
//! ```
//!
//! Opt-in through the environment only:
//!
//! ```sh
//! LANDSCAPE_QOS_PIPES=1
//! LANDSCAPE_QOS_RATE=900mbit      # ~90% of the real line rate
//! ```
//!
//! Both are read by the WAN route service when the link comes up, so a change
//! takes effect after the landscape process (not just the link) is restarted.
//!
//! Requires the `tc` binary at runtime (OpenWrt: `iproute2-tc`). When it is
//! missing or a command fails, the pipes are not installed and the WAN link
//! keeps working as before.

use std::process::Command;

/// UDP / ICMP / default class ids and their HTB priorities.
const UDP_CLASS: &str = "1:10";
const ICMP_CLASS: &str = "1:20";
const DEFAULT_CLASS: &str = "1:30";

#[derive(Clone, Debug)]
pub struct QosPipesConfig {
    pub enable: bool,
    pub rate: String,
}

impl Default for QosPipesConfig {
    fn default() -> Self {
        Self { enable: false, rate: String::new() }
    }
}

fn load_config() -> QosPipesConfig {
    let mut cfg = QosPipesConfig::default();

    if let Ok(v) = std::env::var("LANDSCAPE_QOS_PIPES") {
        cfg.enable =
            matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "on" | "yes");
    }
    if let Ok(v) = std::env::var("LANDSCAPE_QOS_RATE") {
        cfg.rate = v.trim().to_string();
    }

    cfg
}

fn run_tc(args: &[&str]) -> bool {
    match Command::new("tc").args(args).output() {
        Ok(out) if out.status.success() => true,
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            tracing::warn!(args = ?args, stderr = stderr.trim(), "qos-pipes: tc command failed");
            false
        }
        Err(err) => {
            tracing::warn!(%err, "qos-pipes: cannot execute tc (install iproute2-tc?)");
            false
        }
    }
}

/// Installs the pipes for `iface`. The returned guard removes them again when
/// dropped, i.e. when the WAN route service stops or the link restarts.
pub struct QosPipesGuard {
    dev: String,
    active: bool,
}

impl Drop for QosPipesGuard {
    fn drop(&mut self) {
        if self.active {
            let dev = self.dev.as_str();
            run_tc(&["qdisc", "del", "dev", dev, "root"]);
            tracing::info!(dev = %dev, "qos-pipes: removed");
        }
    }
}

pub fn apply(iface: &str) -> QosPipesGuard {
    let inactive = QosPipesGuard { dev: iface.to_string(), active: false };

    let cfg = load_config();
    if !cfg.enable {
        return inactive;
    }
    if cfg.rate.is_empty() {
        tracing::warn!(
            dev = %iface,
            "qos-pipes: enabled but LANDSCAPE_QOS_RATE is empty (e.g. 900mbit)"
        );
        return inactive;
    }

    let dev = iface;
    let rate = cfg.rate.as_str();

    // Root HTB; unmatched traffic lands in the TCP class.
    if !run_tc(&["qdisc", "replace", "dev", dev, "root", "handle", "1:", "htb", "default", "30"]) {
        return inactive;
    }
    // Parent class carries the line rate, children borrow from it by priority.
    if !run_tc(&[
        "class", "replace", "dev", dev, "parent", "1:", "classid", "1:1", "htb", "rate", rate,
        "ceil", rate,
    ]) {
        run_tc(&["qdisc", "del", "dev", dev, "root"]);
        return inactive;
    }
    for (classid, prio) in [(UDP_CLASS, "0"), (ICMP_CLASS, "1"), (DEFAULT_CLASS, "2")] {
        if !run_tc(&[
            "class", "replace", "dev", dev, "parent", "1:1", "classid", classid, "htb", "rate",
            "1mbit", "ceil", rate, "prio", prio,
        ]) {
            run_tc(&["qdisc", "del", "dev", dev, "root"]);
            return inactive;
        }
        if !run_tc(&["qdisc", "replace", "dev", dev, "parent", classid, "fq_codel"]) {
            run_tc(&["qdisc", "del", "dev", dev, "root"]);
            return inactive;
        }
    }
    // Two classifiers only; TCP is covered by the HTB default class.
    let mut ok = run_tc(&[
        "filter", "replace", "dev", dev, "parent", "1:", "protocol", "ip", "prio", "1", "u32",
        "match", "ip", "protocol", "17", "0xff", "flowid", UDP_CLASS,
    ]);
    ok &= run_tc(&[
        "filter", "replace", "dev", dev, "parent", "1:", "protocol", "ip", "prio", "1", "u32",
        "match", "ip", "protocol", "1", "0xff", "flowid", ICMP_CLASS,
    ]);
    if !ok {
        run_tc(&["qdisc", "del", "dev", dev, "root"]);
        return inactive;
    }

    tracing::info!(dev = %dev, rate = %rate, "qos-pipes: UDP/ICMP/TCP pipes installed");
    QosPipesGuard { dev: dev.to_string(), active: true }
}
