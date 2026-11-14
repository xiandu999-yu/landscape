//! Read-only fast-path counters for the "Monitoring -> Fast Path" page.

use axum::extract::State;

use crate::api::LandscapeApiResp;
use crate::error::LandscapeApiResult;
use crate::LandscapeApp;

#[utoipa::path(
    get,
    path = "/fastpath/stats",
    tag = "System Config",
    operation_id = "get_fastpath_stats",
    responses((status = 200, description = "Success"))
)]
pub async fn get_fastpath_stats(
    State(app): State<LandscapeApp>,
) -> LandscapeApiResult<landscape_ebpf::fastpath_stats::FastPathStats> {
    LandscapeApiResp::success(landscape_ebpf::fastpath_stats::snapshot(
        &app.ebpf_paths.root,
    ))
}
