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

    print(f"TCX overlay applied (overlay={OVERLAY})")


if __name__ == "__main__":
    main()
