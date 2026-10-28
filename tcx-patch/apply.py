#!/usr/bin/env python3
"""Apply the Landscape TCX overlay onto a checked-out upstream tree.

Usage:
    python3 tcx-patch/apply.py <path-to-landscape-checkout>

The script is idempotent and fails loudly when an anchor is missing, which
happens if the upstream baseline moved. In that case update the anchors below.
"""

import pathlib
import shutil
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
HERE = pathlib.Path(__file__).resolve().parent
OVERLAY = HERE / "overlay"


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def patch(path: str, old: str, new: str) -> None:
    p = ROOT / path
    if not p.is_file():
        fail(f"missing file: {path}")
    text = p.read_text(encoding="utf-8")
    if new in text:
        print(f"skip (already patched): {path}")
        return
    if old not in text:
        fail(f"anchor not found in {path}:\n---\n{old}\n---")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"patched: {path}")


def main() -> None:
    if not (ROOT / "Cargo.toml").is_file():
        fail(f"{ROOT} does not look like the landscape checkout")

    # 1) Overlay files (new module + rewritten hook proxy).
    required = [
        "landscape-ebpf/src/tcx.rs",
        "landscape-ebpf/src/landscape.rs",
    ]
    missing = [rel for rel in required if not (OVERLAY / rel).is_file()]
    if missing:
        fail(
            f"overlay is incomplete under {OVERLAY}; missing: {', '.join(missing)}\n"
            "expected layout next to apply.py: overlay/landscape-ebpf/src/{tcx.rs,landscape.rs}"
        )

    for src in sorted(OVERLAY.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(OVERLAY)
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"copied: {rel}")

    # 2) Sanity check that the copied files are in place before patching.
    for rel in required:
        if not (ROOT / rel).is_file():
            fail(f"overlay copy did not land: {rel}")

    # 2b) Rename the attached entry programs from the generic `tc_*` prefix to
    #     `land_*`, so `bpftool net show` identifies them like sing-box's `sb_*`
    #     instead of an anonymous `tc_*` (matching the landscape router name).
    #     C side: rename the function definition (the BPF program name).
    #     Rust side: rename only `progs.<name>` accesses; skeleton module names
    #     come from the .bpf.c file stem and must stay untouched.
    renames = {
        "tc_wan_ingress_intro": "land_wan_ingress_intro",
        "tc_wan_egress_route": "land_wan_egress_route",
        "tc_lan_ingress_route": "land_lan_ingress_route",
        "tc_lan_dao": "land_lan_dao",
    }
    renamed_c = 0
    for path in (ROOT / "landscape-ebpf/src").rglob("*"):
        if not path.is_file() or path.suffix not in {".c", ".h"}:
            continue
        text = path.read_text(encoding="utf-8")
        new = text
        for old, rep in renames.items():
            new = new.replace(old, rep)
        if new != text:
            path.write_text(new, encoding="utf-8")
            renamed_c += 1
    renamed_rs = 0
    for path in (ROOT / "landscape-ebpf/src").rglob("*.rs"):
        text = path.read_text(encoding="utf-8")
        new = text
        for old, rep in renames.items():
            new = new.replace(f"progs.{old}", f"progs.{rep}")
        if new != text:
            path.write_text(new, encoding="utf-8")
            renamed_rs += 1
    print(f"renamed entry programs: {renamed_c} C/H files, {renamed_rs} Rust files")

    # 3) Register the new module.
    patch(
        "landscape-ebpf/src/lib.rs",
        "pub mod landscape;\n",
        "pub mod landscape;\npub mod tcx;\n",
    )

    # 4) Read env configuration before any service attaches hooks.
    patch(
        "landscape-ebpf/src/lib.rs",
        "pub fn setting_libbpf_log() {\n    bump_memlock_rlimit();\n",
        "pub fn setting_libbpf_log() {\n    bump_memlock_rlimit();\n    crate::tcx::init_from_env();\n",
    )

    # 5) WebUI/API: per-interface TCX order editor.
    #    5a) backend module + route registration
    patch(
        "landscape-webserver/src/system/mod.rs",
        "pub mod metric_config;\n",
        "pub mod metric_config;\npub mod tcx_order;\n",
    )
    patch(
        "landscape-webserver/src/system/config.rs",
        "        .routes(routes!(super::auth_config::update_auth_config))",
        "        .routes(routes!(super::tcx_order::get_tcx_order, super::tcx_order::update_tcx_order))\n"
        "        .routes(routes!(super::auth_config::update_auth_config))",
    )

    #    5b) embed the editor under the WAN/LAN route service cards
    wan_modal = "landscape-webui/src/components/route/wan/RouteWanServiceEditModal.vue"
    lan_modal = "landscape-webui/src/components/route/lan/RouteLanServiceEditModal.vue"
    import_anchor = 'import ConfigModal from "@/components/common/ConfigModal.vue";\n'
    import_line = (
        'import ConfigModal from "@/components/common/ConfigModal.vue";\n'
        'import TcxOrderSection from "@/components/tcx/TcxOrderSection.vue";\n'
    )
    footer_anchor = "    <template #footer>"
    for modal, egress in ((wan_modal, "true"), (lan_modal, "false")):
        patch(modal, import_anchor, import_line)
        patch(
            modal,
            footer_anchor,
            f'    <TcxOrderSection :iface_name="iface_info.iface_name" :egress="{egress}" />\n\n'
            f"{footer_anchor}",
        )

    print(f"TCX overlay applied (overlay={OVERLAY})")


if __name__ == "__main__":
    main()
