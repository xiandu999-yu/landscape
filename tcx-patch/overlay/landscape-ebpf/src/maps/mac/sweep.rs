//! Periodic validation of the WAN->LAN fast-path table.
//!
//! Each entry was written by the route stage and carries the LAN egress
//! ifindex plus both MACs. `ip_mac_v4` is the source of truth for all three
//! (fed by the neigh_update kprobe and the 10s ARP reconcile), so one lookup
//! per entry decides whether the cached decision is still valid. A mismatch
//! deletes the entry and the next packet re-records it through the slow path.
//!
//! Scheduling: the caller ticks every `FAST_SWEEP_INTERVAL_SECS` (5s); a pass
//! stops after `FAST_SWEEP_BUDGET_MS` (2s) so it cannot stall the router, and
//! whatever it did not reach is covered by the next tick.

use crate::LandscapeMapPath;
use libbpf_rs::MapCore as _;

/// Hard cap on the wall time of one pass.
pub(crate) const FAST_SWEEP_BUDGET_MS: u128 = 2000;

// `nat_fast_wan_value` offsets (bpf/nat_v4/nat_fast_wan.h).
const OFF_INTERNAL_ADDR: usize = 0;
const OFF_OUT_IFINDEX: usize = 4;
const OFF_DST_MAC: usize = 24;
const OFF_SRC_MAC: usize = 30;
const VALUE_MIN_LEN: usize = 36;

// `MacValueV4` offsets: ifindex, mac, dev_mac, proto, pad.
const M_IFINDEX: usize = 0;
const M_MAC: usize = 4;
const M_DEV_MAC: usize = 10;
const MAC_VALUE_MIN_LEN: usize = 16;

fn u32_at(buf: &[u8], off: usize) -> u32 {
    u32::from_ne_bytes([buf[off], buf[off + 1], buf[off + 2], buf[off + 3]])
}

/// Validates every entry it can reach within the budget. Returns
/// `(checked, removed)`.
pub(crate) fn sweep_fast_wan(paths: &LandscapeMapPath) -> (usize, usize) {
    use std::time::Instant;

    let fast = match libbpf_rs::MapHandle::from_pinned_path(&paths.fa4_fast_wan) {
        Ok(map) => map,
        Err(_) => return (0, 0),
    };
    let mac = match libbpf_rs::MapHandle::from_pinned_path(&paths.ip_mac_v4) {
        Ok(map) => map,
        Err(_) => return (0, 0),
    };

    let start = Instant::now();
    let mut checked = 0usize;
    let mut removed = 0usize;

    for key in fast.keys() {
        if start.elapsed().as_millis() > FAST_SWEEP_BUDGET_MS {
            break;
        }
        checked += 1;

        let value = match fast.lookup(&key, libbpf_rs::MapFlags::ANY) {
            Ok(Some(value)) if value.len() >= VALUE_MIN_LEN => value,
            _ => continue,
        };

        let internal = &value[OFF_INTERNAL_ADDR..OFF_INTERNAL_ADDR + 4];
        let out_ifindex = u32_at(&value, OFF_OUT_IFINDEX);
        let dst_mac = &value[OFF_DST_MAC..OFF_DST_MAC + 6];
        let src_mac = &value[OFF_SRC_MAC..OFF_SRC_MAC + 6];

        let keep = match mac.lookup(internal, libbpf_rs::MapFlags::ANY) {
            Ok(Some(m)) if m.len() >= MAC_VALUE_MIN_LEN => {
                u32_at(&m, M_IFINDEX) == out_ifindex
                    && &m[M_MAC..M_MAC + 6] == dst_mac
                    && &m[M_DEV_MAC..M_DEV_MAC + 6] == src_mac
            }
            _ => false,
        };

        if !keep {
            let _ = fast.delete(&key);
            removed += 1;
        }
    }

    if removed > 0 {
        tracing::debug!(checked, removed, "fast wan sweep removed stale entries");
    }
    (checked, removed)
}
