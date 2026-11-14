//! Forwarding QoS qdiscs.
//!
//! Three modes are supported:
//!   htb - one precise HTB root (default)
//!   mq  - keep hardware TX queues and put a qdisc on every queue
//!   ifb - redirect egress through an IFB for one global qdisc (experimental)

use std::process::Command;

const IFB_MARK: &str = "0x40000000";

fn run(cmd: &str, args: &[String]) -> Result<(), String> {
    let output = Command::new(cmd)
        .args(args)
        .output()
        .map_err(|error| format!("{cmd}: {error}"))?;
    if output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        Err(if stderr.is_empty() {
            format!("{cmd} exited with {}", output.status)
        } else {
            stderr
        })
    }
}

fn tc(args: &[&str]) -> Result<(), String> {
    run("tc", &args.iter().map(|arg| arg.to_string()).collect::<Vec<_>>())
}

fn ip(args: &[&str]) -> Result<(), String> {
    run("ip", &args.iter().map(|arg| arg.to_string()).collect::<Vec<_>>())
}

fn replace_root_htb(iface: &str) -> Result<(), String> {
    let args = [
        "qdisc", "replace", "dev", iface, "root", "handle", "1:", "htb", "default", "999",
    ];
    if tc(&args).is_ok() {
        return Ok(());
    }
    let _ = tc(&["qdisc", "del", "dev", iface, "root"]);
    tc(&args)
}

fn replace_root_mq(iface: &str) -> Result<(), String> {
    let args = ["qdisc", "replace", "dev", iface, "root", "handle", "1:", "mq"];
    if tc(&args).is_ok() {
        return Ok(());
    }
    let _ = tc(&["qdisc", "del", "dev", iface, "root"]);
    tc(&args)
}

fn replace_root_clsact(iface: &str) -> Result<(), String> {
    let args = ["qdisc", "replace", "dev", iface, "root", "clsact"];
    if tc(&args).is_ok() {
        return Ok(());
    }
    let _ = tc(&["qdisc", "del", "dev", iface, "root"]);
    tc(&args)
}

fn tx_queue_count(iface: &str) -> u32 {
    let path = format!("/sys/class/net/{iface}/queues");
    let Ok(entries) = std::fs::read_dir(path) else {
        return 1;
    };
    let count = entries
        .flatten()
        .filter(|entry| entry.file_name().to_string_lossy().starts_with("tx-"))
        .count();
    count.max(1) as u32
}

fn apply_leaf(
    iface: &str,
    parent: &str,
    rate_kbit: u32,
    qdisc: &str,
    direction: &str,
) -> Result<(), String> {
    let rate = format!("{rate_kbit}kbit");
    let rate = rate.as_str();
    let _ = tc(&["qdisc", "del", "dev", iface, "parent", parent]);

    if qdisc == "cake" {
        let args = [
            "qdisc", "replace", "dev", iface, "parent", parent, "cake", "bandwidth", rate,
            "diffserv4", "triple-isolate", "nat", "ecn",
        ];
        match tc(&args) {
            Ok(()) => {
                tracing::info!(%iface, %parent, %direction, %rate, "qos leaf applied (cake nat ecn)");
                return Ok(());
            }
            Err(error) => {
                tracing::warn!(%iface, %parent, %error, "cake nat unavailable, retrying without nat");
            }
        }
        let no_nat = [
            "qdisc", "replace", "dev", iface, "parent", parent, "cake", "bandwidth", rate,
            "diffserv4", "triple-isolate", "ecn",
        ];
        match tc(&no_nat) {
            Ok(()) => {
                tracing::info!(%iface, %parent, %direction, %rate, "qos leaf applied (cake ecn)");
                return Ok(());
            }
            Err(error) => {
                tracing::warn!(%iface, %parent, %error, "cake unavailable, falling back to fq_codel");
            }
        }
    }

    if tc(&[
        "qdisc", "replace", "dev", iface, "parent", parent, "fq_codel", "ecn",
    ])
    .is_ok()
    {
        tracing::info!(%iface, %parent, %direction, %rate, "qos leaf applied (fq_codel ecn)");
        return Ok(());
    }
    let _ = tc(&["qdisc", "del", "dev", iface, "parent", parent]);
    tc(&[
        "qdisc", "replace", "dev", iface, "parent", parent, "fq_codel",
    ])?;
    tracing::info!(%iface, %parent, %direction, %rate, "qos leaf applied (fq_codel)");
    Ok(())
}

fn apply_htb(
    iface: &str,
    rate_kbit: u32,
    direction: &str,
    qdisc: &str,
) -> Result<(), String> {
    replace_root_htb(iface)?;
    tc(&[
        "class", "replace", "dev", iface, "parent", "1:", "classid", "1:999", "htb", "rate",
        "10gbit",
    ])?;
    let rate = format!("{rate_kbit}kbit");
    let rate = rate.as_str();
    tc(&[
        "class", "replace", "dev", iface, "parent", "1:", "classid", "1:10", "htb", "rate",
        rate, "ceil", rate,
    ])?;
    apply_leaf(iface, "1:10", rate_kbit, qdisc, direction)
}

fn apply_mq(
    iface: &str,
    rate_kbit: u32,
    direction: &str,
    qdisc: &str,
) -> Result<(), String> {
    replace_root_mq(iface)?;
    let queues = tx_queue_count(iface);
    let per_queue = (rate_kbit / queues).max(1);
    for index in 1..=queues {
        let parent = format!("1:{index}");
        apply_leaf(iface, parent.as_str(), per_queue, qdisc, direction)?;
    }
    tracing::info!(%iface, %direction, queues, per_queue, "qos mq applied");
    Ok(())
}

pub(crate) fn ifb_name(iface: &str) -> String {
    let safe: String = iface
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '_' })
        .collect();
    let name = format!("ifb-{safe}");
    name.chars().take(15).collect()
}

fn ensure_ifb(iface: &str) -> Result<String, String> {
    let name = ifb_name(iface);
    if ip(&["link", "show", name.as_str()]).is_err() {
        ip(&["link", "add", "name", name.as_str(), "type", "ifb"])?;
    }
    ip(&["link", "set", name.as_str(), "up"])?;
    Ok(name)
}

fn apply_ifb(
    iface: &str,
    rate_kbit: u32,
    direction: &str,
    qdisc: &str,
) -> Result<(), String> {
    let ifb = ensure_ifb(iface)?;
    replace_root_clsact(iface)?;
    tc(&[
        "filter", "replace", "dev", iface, "egress", "protocol", "all", "prio", "1", "flower",
        "mark", "0", "action", "skbedit", "mark", IFB_MARK, "action", "mirred", "egress",
        "redirect", "dev", ifb.as_str(),
    ])?;
    apply_htb(ifb.as_str(), rate_kbit, direction, qdisc)?;
    tracing::info!(%iface, %ifb, %direction, "qos ifb applied");
    Ok(())
}

fn apply_ingress_redirect(iface: &str, ifb: &str) -> Result<(), String> {
    tc(&[
        "qdisc", "replace", "dev", iface, "handle", "ffff:", "ingress",
    ])?;
    tc(&[
        "filter", "replace", "dev", iface, "parent", "ffff:", "protocol", "all", "prio", "1",
        "u32", "match", "u32", "0", "0", "action", "mirred", "egress", "redirect", "dev", ifb,
    ])
}

/// SQM-style per-interface shaping: upload on the interface egress, download
/// on an IFB fed by the interface ingress redirect.
pub fn apply_sqm(
    iface: &str,
    upload_kbit: u32,
    download_kbit: u32,
    qdisc: &str,
    mode: &str,
) -> Result<(), String> {
    if iface.is_empty() {
        return Ok(());
    }

    if upload_kbit > 0 {
        // ifb mode uses the same ifb-<iface> device as the ingress path, so
        // it cannot coexist with a download shape; fall back to htb then.
        let upload_mode = if mode == "ifb" && download_kbit > 0 { "htb" } else { mode };
        let result = match upload_mode {
            "mq" => apply_mq(iface, upload_kbit, "upload", qdisc),
            "ifb" => apply_ifb(iface, upload_kbit, "upload", qdisc),
            _ => apply_htb(iface, upload_kbit, "upload", qdisc),
        };
        if let Err(error) = result {
            tracing::warn!(%iface, %error, "sqm upload mode failed, falling back to htb");
            apply_htb(iface, upload_kbit, "upload", qdisc)?;
        }
    } else {
        let _ = tc(&["qdisc", "del", "dev", iface, "root"]);
    }

    if download_kbit > 0 {
        let ifb = ensure_ifb(iface)?;
        apply_ingress_redirect(iface, &ifb)?;
        apply_htb(&ifb, download_kbit, "download", qdisc)?;
        tracing::info!(%iface, %ifb, download_kbit, "sqm download applied");
    } else {
        let _ = tc(&["qdisc", "del", "dev", iface, "ingress"]);
        let ifb = ifb_name(iface);
        let _ = tc(&["qdisc", "del", "dev", ifb.as_str(), "root"]);
    }
    Ok(())
}

pub fn apply_iface(
    iface: &str,
    rate_kbit: u32,
    direction: &str,
    qdisc: &str,
    mode: &str,
) -> Result<(), String> {
    if iface.is_empty() {
        return Ok(());
    }
    if rate_kbit == 0 {
        clear_iface(iface);
        return Ok(());
    }

    let result = match mode {
        "mq" => apply_mq(iface, rate_kbit, direction, qdisc),
        "ifb" => apply_ifb(iface, rate_kbit, direction, qdisc),
        _ => apply_htb(iface, rate_kbit, direction, qdisc),
    };
    if let Err(error) = &result {
        if mode != "htb" {
            tracing::warn!(%iface, %error, "qos mode failed, falling back to htb");
            return apply_htb(iface, rate_kbit, direction, qdisc);
        }
    }
    result
}

pub fn clear_ingress(iface: &str) -> Result<(), String> {
    tc(&["qdisc", "del", "dev", iface, "ingress"])
}

pub fn clear_iface(iface: &str) {
    if iface.is_empty() {
        return;
    }
    let _ = tc(&["qdisc", "del", "dev", iface, "root"]);
    let _ = tc(&["qdisc", "del", "dev", iface, "ingress"]);
    let ifb = ifb_name(iface);
    let _ = tc(&["qdisc", "del", "dev", ifb.as_str(), "root"]);
    let _ = ip(&["link", "del", ifb.as_str()]);
}

/// Remove stale `ifb-*` devices except those still needed by an active
/// download SQM rule.
pub fn cleanup_ifb(keep: &[String]) {
    let Ok(entries) = std::fs::read_dir("/sys/class/net") else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if !name.starts_with("ifb-") || keep.iter().any(|k| k == &name) {
            continue;
        }
        let _ = ip(&["link", "del", name.as_str()]);
        tracing::info!(%name, "removed stale ifb device");
    }
}


