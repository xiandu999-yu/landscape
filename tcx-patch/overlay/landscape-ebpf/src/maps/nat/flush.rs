//! Fast-path cache invalidation for policy changes.
//!
//! `flow_core` (WAN->LAN) and `fa4_fast` (LAN->WAN) cache the *result* of the
//! split decision, so a rule change would only take effect for new flows until
//! the cached entries expire (60-120s inbound, up to 300s outbound). These
//! helpers flush both caches so a rule update applies immediately.
//!
//! Called from the dataplane hooks that already run when routing inputs change
//! (`sync_flow_matches`, `set_dst_ip_marks`).

use crate::LandscapeMapPath;
use libbpf_rs::MapCore as _;

/// `flow_core_ver` is an ARRAY of 1024 u64 slots indexed by LAN ifindex.
const FLOW_CORE_VER_SLOTS: u32 = 1024;

/// Drop every cached fast-path decision.
pub fn flush_fast_caches(paths: &LandscapeMapPath) {
    flush_egress(paths);
    bump_ingress_generations(paths);
}

/// Egress cache: entries carry the post-NAT external address/port, so they must
/// be removed rather than invalidated.
fn flush_egress(paths: &LandscapeMapPath) {
    let Ok(map) = libbpf_rs::MapHandle::from_pinned_path(&paths.fa4_fast) else {
        return;
    };
    // Snapshot first: deleting while iterating bpf_map_get_next_key may skip keys.
    let keys: Vec<Vec<u8>> = map.keys().collect();
    let mut removed = 0usize;
    for key in keys {
        if map.delete(&key).is_ok() {
            removed += 1;
        }
    }
    if removed > 0 {
        tracing::debug!(removed, "flushed fa4_fast after policy change");
    }
}

/// Ingress cache: bumping the per-interface generation makes every record of
/// that interface stale, so the next packet revalidates through the slow path.
fn bump_ingress_generations(paths: &LandscapeMapPath) {
    let Ok(map) = libbpf_rs::MapHandle::from_pinned_path(&paths.flow_core_ver) else {
        return;
    };
    let mut bumped = 0usize;
    for idx in 0..FLOW_CORE_VER_SLOTS {
        let key = idx.to_ne_bytes();
        let Ok(Some(value)) = map.lookup(&key, libbpf_rs::MapFlags::ANY) else {
            continue;
        };
        if value.len() < 8 {
            continue;
        }
        let cur = u64::from_ne_bytes([
            value[0], value[1], value[2], value[3], value[4], value[5], value[6], value[7],
        ]);
        if map
            .update(&key, &cur.wrapping_add(1).to_ne_bytes(), libbpf_rs::MapFlags::ANY)
            .is_ok()
        {
            bumped += 1;
        }
    }
    tracing::debug!(bumped, "bumped flow_core_ver after policy change");
}
