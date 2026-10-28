use std::{mem::MaybeUninit, os::fd::AsFd, ptr::NonNull};

use libbpf_rs::{OpenMapMut, OpenObject, Program, TcAttachPoint, TcHook, TcHookBuilder};

use crate::tcx;

pub(crate) struct OwnedOpenObject {
    ptr: NonNull<MaybeUninit<OpenObject>>,
}

impl OwnedOpenObject {
    pub fn new() -> (Self, &'static mut MaybeUninit<OpenObject>) {
        let ptr = NonNull::from(Box::leak(Box::new(MaybeUninit::<OpenObject>::uninit())));
        let object = unsafe { ptr.as_ptr().as_mut().expect("backing open object pointer is null") };
        (Self { ptr }, object)
    }
}

impl Drop for OwnedOpenObject {
    fn drop(&mut self) {
        unsafe {
            drop(Box::from_raw(self.ptr.as_ptr()));
        }
    }
}

unsafe impl Send for OwnedOpenObject {}
unsafe impl Sync for OwnedOpenObject {}

pub(crate) fn pin_and_reuse_map(
    map: &mut OpenMapMut<'_>,
    path: &std::path::Path,
) -> crate::bpf_error::LdEbpfResult<()> {
    map.set_pin_path(path).map_err(|source| crate::bpf_error::LandscapeEbpfError::Context {
        context: "set_pin_path failed".to_string(),
        source,
    })?;
    map.reuse_pinned_map(path).map_err(|source| crate::bpf_error::LandscapeEbpfError::Context {
        context: "reuse_pinned_map failed".to_string(),
        source,
    })?;
    Ok(())
}

/// Per-hook attach handle.
///
/// Backend selection happens in [`TcHookProxy::new`]:
///  * `LANDSCAPE_TCX_MODE=off`     -> legacy clsact qdisc + filters
///  * `LANDSCAPE_TCX_MODE=auto`    -> TCX on kernel >= 6.6, else clsact
///  * `LANDSCAPE_TCX_MODE=require` -> TCX only (failure is fatal when strict)
///
/// With TCX the program is attached as a `bpf_link` in the netdev's tcx
/// multi-prog chain; the link is destroyed (detached) when this proxy drops.
pub struct TcHookProxy {
    /// Legacy clsact backend (mutually exclusive with `tcx`).
    clsact: Option<TcHook>,
    /// TCX backend; dropping the link detaches it.
    tcx: Option<tcx::Link>,
}

impl TcHookProxy {
    pub fn new(prog: &Program, ifindex: i32, attach: TcAttachPoint, priority: u32) -> TcHookProxy {
        let cfg = tcx::config();
        let egress = attach == libbpf_rs::TC_EGRESS;

        let want_tcx = match cfg.mode {
            tcx::Mode::Off => false,
            tcx::Mode::Auto => tcx::kernel_supports_tcx(),
            tcx::Mode::Require => true,
        };

        if want_tcx {
            let order = tcx::order_for(ifindex, egress);
            match tcx::attach(prog, ifindex, egress, &order) {
                Ok(link) => {
                    tracing::info!(
                        ifindex,
                        egress,
                        order = ?order,
                        "attached TC hook via TCX"
                    );
                    return Self { clsact: None, tcx: Some(link) };
                }
                Err(err) => {
                    if cfg.strict || cfg.mode == tcx::Mode::Require {
                        panic!("TCX attach failed (ifindex={ifindex}, egress={egress}): {err}");
                    }
                    tracing::warn!(
                        ifindex,
                        egress,
                        error = %err,
                        "TCX attach failed; falling back to clsact"
                    );
                }
            }
        }

        // Legacy clsact path. priority/handle are configurable via env so the
        // fixed 1/1 identity (which collides with other tc users) can be moved.
        let mut tc_builder = TcHookBuilder::new(prog.as_fd());
        let prio = if priority == 0 { cfg.clsact_prio } else { priority };
        tc_builder
            .ifindex(ifindex)
            .replace(true)
            .handle(cfg.clsact_handle)
            .priority(prio);
        let hook = tc_builder.hook(attach);
        Self { clsact: Some(hook), tcx: None }
    }

    pub fn attach(&mut self) {
        let Some(hook) = self.clsact.as_mut() else {
            // TCX link already attached in `new`.
            return;
        };

        let result = hook.query();
        tracing::debug!("1 - the hook is exist? {:?}", result);

        // libbpf-rs `create()` already maps EEXIST to Ok, so any error here is
        // a real failure (e.g. EPERM/ENODEV) and must not panic the daemon.
        if let Err(err) = hook.create() {
            tracing::warn!(error = %err, "tc clsact create failed");
        }

        let result = hook.query();
        tracing::debug!("2 - the hook is exist? {:?}", result);

        if let Err(err) = hook.attach() {
            tracing::error!(error = %err, "tc filter attach failed");
            return;
        }

        let result = hook.query();
        tracing::debug!("3 - the hook is exist? {:?}", result);
    }
}

impl Drop for TcHookProxy {
    fn drop(&mut self) {
        if let Some(link) = self.tcx.take() {
            tracing::debug!(
                ifindex = link.ifindex,
                egress = link.egress,
                order = ?link.order,
                "detaching TCX link"
            );
            // `tcx::Link::drop` destroys the bpf_link (detaches).
        }
        if let Some(mut hook) = self.clsact.take() {
            tracing::debug!("detach hook");
            if hook.query().is_ok() {
                tracing::debug!("start detach success");
                if let Err(e) = hook.detach() {
                    tracing::debug!("detach error: {:?}", e);
                } else {
                    tracing::debug!("detach success");
                }
            }
        }
    }
}

// TC operations are thread-safe as they are system calls on file descriptors
// and don't share mutable state across threads
unsafe impl Send for TcHookProxy {}
unsafe impl Sync for TcHookProxy {}
