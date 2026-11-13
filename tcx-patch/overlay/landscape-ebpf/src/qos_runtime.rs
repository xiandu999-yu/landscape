//! Push the WebUI global forwarding QoS policy into the pinned eBPF map.

use std::path::Path;

use libbpf_rs::{MapCore, MapFlags, MapHandle};

#[derive(Debug, Clone, Copy)]
pub struct QosRuntime {
    pub enabled: bool,
    pub upload_bps: u64,
    pub download_bps: u64,
}

fn qos_value(config: QosRuntime) -> [u8; 24] {
    let mut value = [0u8; 24];
    value[0] = u8::from(config.enabled);
    let cpus = std::thread::available_parallelism()
        .map(|value| value.get() as u32)
        .unwrap_or(1);
    value[4..8].copy_from_slice(&cpus.to_ne_bytes());
    value[8..16].copy_from_slice(&config.upload_bps.to_ne_bytes());
    value[16..24].copy_from_slice(&config.download_bps.to_ne_bytes());
    value
}

pub fn update(root: &Path, config: QosRuntime) -> Result<(), String> {
    let path = root.join("qos_forward_cfg");
    let map = MapHandle::from_pinned_path(&path)
        .map_err(|error| format!("open {}: {error}", path.display()))?;
    map.update(&0u32.to_ne_bytes(), &qos_value(config), MapFlags::ANY)
        .map_err(|error| format!("update qos_forward_cfg: {error}"))?;
    Ok(())
}
