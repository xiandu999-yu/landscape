//! Apply the forwarding-only HTB classes used by the eBPF QoS priority.

use std::process::Command;

fn run_tc(args: &[String]) -> Result<(), String> {
    let output = Command::new("tc")
        .args(args)
        .output()
        .map_err(|error| format!("tc: {error}"))?;
    if output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        Err(if stderr.is_empty() {
            format!("tc exited with {}", output.status)
        } else {
            stderr
        })
    }
}

fn tc(args: &[&str]) -> Result<(), String> {
    run_tc(&args.iter().map(|arg| arg.to_string()).collect::<Vec<_>>())
}

fn replace_root(iface: &str) -> Result<(), String> {
    let args = [
        "qdisc", "replace", "dev", iface, "root", "handle", "1:", "htb", "default", "999",
    ];
    if tc(&args).is_ok() {
        return Ok(());
    }
    let _ = tc(&["qdisc", "del", "dev", iface, "root"]);
    tc(&args)
}

pub fn apply_iface(
    iface: &str,
    rate_kbit: u32,
    direction: &str,
    qdisc: &str,
) -> Result<(), String> {
    if iface.is_empty() {
        return Ok(());
    }
    if rate_kbit == 0 {
        let _ = tc(&["qdisc", "del", "dev", iface, "root"]);
        return Ok(());
    }

    replace_root(iface)?;
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

    // Replace the leaf so switching CAKE <-> fq_codel does not leave the old
    // qdisc attached.
    let _ = tc(&["qdisc", "del", "dev", iface, "parent", "1:10"]);
    let leaf = if qdisc == "cake" {
        let args = [
            "qdisc", "replace", "dev", iface, "parent", "1:10", "cake", "bandwidth", rate,
            "diffserv4", "triple-isolate",
        ];
        match tc(&args) {
            Ok(()) => {
                tracing::info!(%iface, %direction, %rate, "qos shaper applied (cake)");
                return Ok(());
            }
            Err(error) => {
                tracing::warn!(%iface, %error, "cake unavailable, falling back to fq_codel");
                "fq_codel"
            }
        }
    } else {
        "fq_codel"
    };
    tc(&[
        "qdisc", "replace", "dev", iface, "parent", "1:10", leaf,
    ])?;
    tracing::info!(%iface, %direction, %rate, %leaf, "qos shaper applied");
    Ok(())
}

pub fn clear_iface(iface: &str) {
    if !iface.is_empty() {
        let _ = tc(&["qdisc", "del", "dev", iface, "root"]);
    }
}
