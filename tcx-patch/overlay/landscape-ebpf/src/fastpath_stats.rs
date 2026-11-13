//! Read-only fast-path counters for the WebUI.
//!
//! All maps are pinned under `LandscapeMapPath::root`; this module only reads
//! them, so it is safe to call while traffic is flowing.

use std::path::Path;

use libbpf_rs::{MapCore, MapFlags, MapHandle};
use serde::Serialize;

#[derive(Debug, Clone, Default, Serialize)]
pub struct FastPathStats {
    pub available: bool,
    pub error: Option<String>,

    pub egress_record: u64,
    pub egress_hit: u64,
    pub egress_expired: u64,

    pub ingress_recorded: u64,
    pub ingress_hit: u64,
    pub ingress_expired: u64,
    pub ingress_tries: u64,
    pub ingress_parsed: u64,
    pub ingress_miss: u64,

    pub flow_core_entries: u64,
    pub fa4_fast_entries: u64,

    pub napt_port_exhausted: u64,
    pub napt_map_miss: u64,
    pub napt_ct_full: u64,
    pub napt_ct_busy: u64,
}

fn sum_lookup(map: &MapHandle, key: u32) -> Option<u64> {
    let key = key.to_ne_bytes();
    if map.map_type().is_percpu() {
        let values = map.lookup_percpu(&key, MapFlags::ANY).ok()??;
        Some(
            values
                .iter()
                .filter_map(|value| value.as_slice().try_into().ok())
                .map(u64::from_ne_bytes)
                .sum(),
        )
    } else {
        let raw = map.lookup(&key, MapFlags::ANY).ok()??;
        Some(
            raw.chunks_exact(8)
                .map(|chunk| u64::from_ne_bytes(chunk.try_into().unwrap()))
                .sum(),
        )
    }
}

fn array_sum(map: &MapHandle, keys: &[u32]) -> u64 {
    keys.iter()
        .filter_map(|key| sum_lookup(map, *key))
        .sum()
}

fn hash_len(map: &MapHandle) -> u64 {
    map.keys().count() as u64
}

fn open(root: &Path, name: &str) -> Result<MapHandle, String> {
    let path = root.join(name);
    MapHandle::from_pinned_path(&path)
        .map_err(|error| format!("open {}: {error}", path.display()))
}

pub fn snapshot(root: &Path) -> FastPathStats {
    let mut stats = FastPathStats::default();
    let mut errors = Vec::new();

    match open(root, "fa4_stats") {
        Ok(map) => {
            stats.egress_record = array_sum(&map, &[0]);
            stats.egress_hit = array_sum(&map, &[1]);
            stats.egress_expired = array_sum(&map, &[2]);
            stats.available = true;
        }
        Err(error) => errors.push(error),
    }

    match open(root, "nat_wan_stats") {
        Ok(map) => {
            stats.ingress_recorded = array_sum(&map, &[1]);
            stats.ingress_hit = array_sum(&map, &[2]);
            stats.ingress_expired = array_sum(&map, &[3]);
            stats.ingress_tries = array_sum(&map, &[4]);
            stats.ingress_parsed = array_sum(&map, &[5]);
            stats.ingress_miss = array_sum(&map, &[6]);
            stats.available = true;
        }
        Err(error) => errors.push(error),
    }

    match open(root, "flow_core") {
        Ok(map) => {
            stats.flow_core_entries = hash_len(&map);
            stats.available = true;
        }
        Err(error) => errors.push(error),
    }

    match open(root, "fa4_fast") {
        Ok(map) => {
            stats.fa4_fast_entries = hash_len(&map);
            stats.available = true;
        }
        Err(error) => errors.push(error),
    }

    match open(root, "napt_stats") {
        Ok(map) => {
            stats.napt_port_exhausted = array_sum(&map, &[0]);
            stats.napt_map_miss = array_sum(&map, &[3]);
            stats.napt_ct_full = array_sum(&map, &[5]);
            stats.napt_ct_busy = array_sum(&map, &[6]);
            stats.available = true;
        }
        Err(error) => errors.push(error),
    }

    if !stats.available {
        stats.error = Some(errors.join("; "));
    }
    stats
}
