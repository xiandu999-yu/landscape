//! Local-process flow matcher.
//!
//! The cgroup/skb egress program stamps `skb->mark` with the selected flow id
//! before the WAN route program runs.  This module owns the skeleton, the
//! cgroup attachment and the map contents (process list + flow metadata).

use std::os::fd::AsFd;

use landscape_common::flow::{RuntimeFlowConfig, RuntimeLocalProcessRule};
use libbpf_rs::skel::{OpenSkel, SkelBuilder};
use libbpf_rs::{MapCore, MapFlags};

use crate::bpf_error::LdEbpfResult;
use crate::landscape::OwnedOpenObject;
use crate::maps::{RawEbpfMapEntries, apply_raw_map_diff, diff_raw_map, snapshot_raw_map};
use crate::skel::proc_match::*;

/// Must match `PROC_LOCAL_FLOW_MAX` in `proc_match.bpf.c`.
const LOCAL_FLOW_MAX: usize = 32;
const PROC_COMM_LEN: usize = 16;

pub struct ProcMatch {
    /// Dropped first: detaches the cgroup program while the skeleton is alive.
    _link: libbpf_rs::Link,
    _skel: ProcMatchSkel<'static>,
    _backing: OwnedOpenObject,
}

// The skeleton only owns libbpf handles / mmap state; map updates and link
// lifetime are safe to share across the tokio workers that sync flow configs.
unsafe impl Send for ProcMatch {}
unsafe impl Sync for ProcMatch {}

impl ProcMatch {
    pub fn new() -> LdEbpfResult<Self> {
        let builder = ProcMatchSkelBuilder::default();
        let (backing, obj) = OwnedOpenObject::new();
        let mut open_skel =
            crate::bpf_ctx!(builder.open(obj), "open proc_match skeleton")?;
        let skel: ProcMatchSkel<'static> =
            crate::bpf_ctx!(open_skel.load(), "load proc_match skeleton")?;

        // cgroup v2 root.  Attaching cgroup/skb programs requires cgroup2;
        // the caller treats a failure as "process matching unavailable" and
        // leaves the rest of the dataplane untouched.
        let cgroup = std::fs::File::open("/sys/fs/cgroup")?;
        let link = crate::bpf_ctx!(
            skel.progs.proc_match_egress.attach_cgroup(cgroup.as_fd()),
            "attach proc_match cgroup egress"
        )?;

        Ok(Self { _link: link, _skel: skel, _backing: backing })
    }

    pub fn sync(&self, configs: &[RuntimeFlowConfig]) -> LdEbpfResult<()> {
        let mut rules: Vec<&RuntimeLocalProcessRule> = configs
            .iter()
            .flat_map(|config| config.local_process_rules.iter())
            .filter(|rule| rule.flow_id < 256)
            .collect();
        rules.sort_by_key(|rule| rule.flow_id);
        if rules.len() > LOCAL_FLOW_MAX {
            tracing::warn!(
                "too many local process flows ({}), only the first {} by flow_id are active",
                rules.len(),
                LOCAL_FLOW_MAX
            );
        }
        rules.truncate(LOCAL_FLOW_MAX);

        // Ordered flow list.  A zero entry terminates the BPF loop.
        for index in 0..LOCAL_FLOW_MAX {
            let flow_id = rules.get(index).map(|rule| rule.flow_id).unwrap_or(0);
            self._skel.maps.proc_local_flows.update(
                &(index as u32).to_ne_bytes(),
                &flow_id.to_ne_bytes(),
                MapFlags::ANY,
            )?;
        }

        // Per-flow mode: 1 = whitelist, 2 = blacklist, 0 = not a local rule.
        // The array is indexed by flow_id, so update every slot to clear
        // stale flags from a previous configuration.
        for flow_id in 0u32..256 {
            let flag = rules
                .iter()
                .find(|rule| rule.flow_id == flow_id)
                .map(|rule| if rule.blacklist { 2u8 } else { 1u8 })
                .unwrap_or(0);
            self._skel.maps.proc_local_meta.update(
                &flow_id.to_ne_bytes(),
                &[flag],
                MapFlags::ANY,
            )?;
        }

        let mut desired = RawEbpfMapEntries::new();
        for rule in &rules {
            for name in &rule.bypass_process {
                let name = name.trim();
                if name.is_empty() {
                    continue;
                }
                desired.insert(proc_key(rule.flow_id, name), vec![1u8]);
            }
        }

        let current = snapshot_raw_map(&self._skel.maps.proc_match_map)?;
        let diff = diff_raw_map(&current, &desired);
        apply_raw_map_diff(&self._skel.maps.proc_match_map, diff)?;
        Ok(())
    }
}

fn proc_key(flow_id: u32, name: &str) -> Vec<u8> {
    let mut key = Vec::with_capacity(4 + PROC_COMM_LEN);
    key.extend_from_slice(&flow_id.to_ne_bytes());

    let mut comm = [0u8; PROC_COMM_LEN];
    let bytes = name.as_bytes();
    let len = bytes.len().min(PROC_COMM_LEN - 1);
    comm[..len].copy_from_slice(&bytes[..len]);
    key.extend_from_slice(&comm);
    key
}
