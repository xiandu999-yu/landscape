//! TCX (tc express) attach backend for Landscape's SCHED_CLS hook programs.
//!
//! Baseline: landscape v0.27.2. Requires kernel >= 6.6 and libbpf >= 1.3.
//!
//! Ordering semantics (verified against kernel `bpf_mprog_attach()`):
//!   * no flags / no relative id  -> append (chain tail)
//!   * BPF_F_BEFORE, no relative  -> prepend (chain head)
//!   * BPF_F_ID | BPF_F_BEFORE    -> insert before program id
//!   * BPF_F_ID | BPF_F_AFTER     -> insert after program id
//!
//! The relative object is a *program* id (BPF_F_ID without BPF_F_LINK), which
//! is what `bpf_mprog_tuple_relative()` resolves via `bpf_prog_by_id()`.

use std::ffi::{c_long, c_void};
use std::mem::size_of;
use std::sync::{OnceLock, RwLock};

use libbpf_rs::AsRawLibbpf as _;
use libbpf_rs::Program;
use libbpf_sys as sys;

pub const BPF_F_BEFORE: u32 = 1 << 3;
pub const BPF_F_AFTER: u32 = 1 << 4;
pub const BPF_F_ID: u32 = 1 << 5;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Use TCX when the kernel supports it, otherwise clsact.
    Auto,
    /// Always use the legacy clsact path.
    Off,
    /// Always use TCX; failures are fatal when `strict` is set.
    Require,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Order {
    First,
    Last,
    Position(u32),
    Before(String),
    After(String),
}

#[derive(Clone, Debug)]
pub struct Config {
    pub mode: Mode,
    pub order: Order,
    pub strict: bool,
    pub clsact_prio: u32,
    pub clsact_handle: u32,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            mode: Mode::Auto,
            order: Order::Last,
            strict: false,
            clsact_prio: 1,
            clsact_handle: 1,
        }
    }
}

fn env_u32(name: &str, default: u32) -> u32 {
    std::env::var(name).ok().and_then(|v| v.trim().parse().ok()).unwrap_or(default)
}

impl Config {
    pub fn from_env() -> Self {
        let mut cfg = Config::default();

        if let Ok(v) = std::env::var("LANDSCAPE_TCX_MODE") {
            cfg.mode = match v.trim().to_ascii_lowercase().as_str() {
                "off" | "clsact" => Mode::Off,
                "require" | "on" | "tcx" => Mode::Require,
                _ => Mode::Auto,
            };
        }

        cfg.strict = std::env::var("LANDSCAPE_TCX_STRICT")
            .map(|v| v.trim() == "1" || v.trim().eq_ignore_ascii_case("true"))
            .unwrap_or(false);

        cfg.clsact_prio = env_u32("LANDSCAPE_TC_PRIO", cfg.clsact_prio);
        cfg.clsact_handle = env_u32("LANDSCAPE_TC_HANDLE", cfg.clsact_handle);

        let order = std::env::var("LANDSCAPE_TCX_ORDER").unwrap_or_else(|_| "last".to_string());
        cfg.order = match order.trim().to_ascii_lowercase().as_str() {
            "first" => Order::First,
            "position" => Order::Position(env_u32("LANDSCAPE_TCX_POSITION", 0)),
            "before" => Order::Before(std::env::var("LANDSCAPE_TCX_RELATIVE").unwrap_or_default()),
            "after" => Order::After(std::env::var("LANDSCAPE_TCX_RELATIVE").unwrap_or_default()),
            _ => Order::Last,
        };

        cfg
    }
}

static CONFIG: OnceLock<RwLock<Config>> = OnceLock::new();

fn slot() -> &'static RwLock<Config> {
    CONFIG.get_or_init(|| RwLock::new(Config::from_env()))
}

/// Initialize the global config from environment variables (idempotent).
pub fn init_from_env() {
    let _ = slot();
}

pub fn config() -> Config {
    slot().read().map(|c| c.clone()).unwrap_or_default()
}

/// Replace the runtime config; takes effect for subsequently attached hooks.
pub fn set_config(cfg: Config) {
    if let Ok(mut guard) = slot().write() {
        *guard = cfg;
    }
}

/// Kernel version gate for TCX (introduced in 6.6).
pub fn kernel_supports_tcx() -> bool {
    let Ok(release) = std::fs::read_to_string("/proc/sys/kernel/osrelease") else {
        return false;
    };
    let mut nums = release.trim().split(|c: char| !c.is_ascii_digit()).filter(|s| !s.is_empty());
    let major: u32 = nums.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let minor: u32 = nums.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    (major, minor) >= (6, 6)
}

pub struct Link {
    ptr: *mut sys::bpf_link,
    #[allow(dead_code)]
    pub ifindex: i32,
    #[allow(dead_code)]
    pub egress: bool,
    #[allow(dead_code)]
    pub order: Order,
}

unsafe impl Send for Link {}
unsafe impl Sync for Link {}

impl Drop for Link {
    fn drop(&mut self) {
        if !self.ptr.is_null() {
            unsafe {
                let _ = sys::bpf_link__destroy(self.ptr);
            }
            self.ptr = std::ptr::null_mut();
        }
    }
}

fn prog_name(id: u32) -> Option<String> {
    let fd = unsafe { sys::bpf_prog_get_fd_by_id(id) };
    if fd < 0 {
        return None;
    }
    let mut info = sys::bpf_prog_info::default();
    let mut len = size_of::<sys::bpf_prog_info>() as u32;
    let rc = unsafe { sys::bpf_obj_get_info_by_fd(fd, &mut info as *mut _ as *mut c_void, &mut len) };
    unsafe {
        libc::close(fd);
    }
    if rc != 0 {
        return None;
    }
    let bytes: Vec<u8> = info.name.iter().take_while(|c| **c != 0).map(|c| *c as u8).collect();
    Some(String::from_utf8_lossy(&bytes).into_owned())
}

fn prog_id_by_name(want: &str) -> Option<u32> {
    if want.is_empty() {
        return None;
    }
    let mut id = 0u32;
    while unsafe { sys::bpf_prog_get_next_id(id, &mut id) } == 0 {
        if prog_name(id).as_deref() == Some(want) {
            return Some(id);
        }
    }
    None
}

/// Ordered program ids currently attached at `(ifindex, direction)`.
fn chain(ifindex: i32, egress: bool) -> Vec<u32> {
    let attach_type = if egress { sys::BPF_TCX_EGRESS } else { sys::BPF_TCX_INGRESS };
    let mut ids = vec![0u32; 64];
    let mut cnt = ids.len() as u32;
    let mut attach_flags = 0u32;
    let rc = unsafe {
        sys::bpf_prog_query(
            ifindex,
            attach_type as _,
            0,
            &mut attach_flags,
            ids.as_mut_ptr(),
            &mut cnt,
        )
    };
    if rc != 0 {
        return Vec::new();
    }
    ids.truncate(cnt as usize);
    ids
}

fn relative_id(ifindex: i32, egress: bool, order: &Order) -> Option<u32> {
    match order {
        Order::First | Order::Last => None,
        Order::Position(n) => chain(ifindex, egress).get(*n as usize).copied(),
        Order::Before(name) | Order::After(name) => prog_id_by_name(name),
    }
}

/// Attach `prog` at `(ifindex, direction)` as a TCX link.
pub fn attach(prog: &Program, ifindex: i32, egress: bool, order: &Order) -> Result<Link, String> {
    let mut opts = sys::bpf_tcx_opts::default();
    opts.sz = size_of::<sys::bpf_tcx_opts>() as _;

    match order {
        Order::First => opts.flags |= BPF_F_BEFORE,
        Order::Last => {}
        Order::Position(_) | Order::Before(_) | Order::After(_) => {
            match relative_id(ifindex, egress, order) {
                Some(id) => {
                    opts.flags |= BPF_F_ID;
                    opts.flags |= if matches!(order, Order::After(_)) { BPF_F_AFTER } else { BPF_F_BEFORE };
                    opts.relative_id = id;
                }
                None => {
                    tracing::warn!(
                        ifindex,
                        egress,
                        ?order,
                        "tcx relative target not found; appending to chain tail"
                    );
                }
            }
        }
    }

    let prog_ptr = prog.as_libbpf_object().as_ptr();
    let link = unsafe { sys::bpf_program__attach_tcx(prog_ptr, ifindex, &opts) };
    let err = unsafe { sys::libbpf_get_error(link as *const c_void) } as c_long;
    if err != 0 {
        return Err(format!(
            "bpf_program__attach_tcx(ifindex={ifindex}, egress={egress}, order={order:?}) failed: errno {}",
            -err
        ));
    }
    if link.is_null() {
        return Err("bpf_program__attach_tcx returned NULL link".to_string());
    }

    Ok(Link { ptr: link, ifindex, egress, order: order.clone() })
}
