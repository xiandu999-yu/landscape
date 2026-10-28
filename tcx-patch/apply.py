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

    # 6) NAT4 optimizations (Landscape's own NAPT, nat_v4/).
    #    6a) metrics must not stall the CT state machine: a full/lagging
    #        nat_metric_events ringbuf used to freeze ACTIVE entries forever,
    #        so they never reached RELEASE and external ports never returned.
    nat_timer = "landscape-ebpf/src/bpf/nat_v4/nat4_ct_timer.h"
    patch(
        nat_timer,
        '#include "nat_v4/tc_nat4_map.h"\n',
        '#include "nat_v4/tc_nat4_map.h"\n#include "nat_v4/tc_nat4_rodata.h"\n',
    )
    patch(
        nat_timer,
        "    __u64 next = current;\n    int ret;\n",
        "    __u64 next = current;\n",
    )
    patch(
        nat_timer,
        "        ret = napt_metric_try_report(key, value, NAPT_METRIC_DELETE);\n",
        "        (void)napt_metric_try_report(key, value, NAPT_METRIC_DELETE);\n",
    )
    patch(
        nat_timer,
        "    ret = napt_metric_try_report(key, value, NAPT_METRIC_ACTIVE);\n"
        "    if (ret) {\n"
        "        *next_timeout = NAPT_CT_REPORT_INTERVAL_NS;\n"
        "        return NAPT_TIMER_STEP_RESTART;\n"
        "    }\n",
        "    /* Metrics are advisory: a full/lagging ringbuf or metric mode=off must\n"
        "     * never freeze the CT state machine, otherwise entries never reach\n"
        "     * RELEASE and external ports are never recycled. */\n"
        "    (void)napt_metric_try_report(key, value, NAPT_METRIC_ACTIVE);\n",
    )
    #    6b) timeouts become rodata-driven (userspace-tunable, defaults kept).
    patch(
        nat_timer,
        "#define NAPT_TCP_SYN_TIMEOUT_NS (6ULL * NAPT_NSEC_PER_SEC)\n"
        "#define NAPT_TCP_TIMEOUT_NS (10ULL * 60ULL * NAPT_NSEC_PER_SEC)\n"
        "#define NAPT_UDP_TIMEOUT_NS (5ULL * 60ULL * NAPT_NSEC_PER_SEC)\n",
        "/* Timeouts come from rodata so userspace can tune them. */\n"
        "#define NAPT_TCP_SYN_TIMEOUT_NS land_nat_tcp_syn_timeout_ns\n"
        "#define NAPT_TCP_TIMEOUT_NS land_nat_tcp_timeout_ns\n"
        "#define NAPT_UDP_TIMEOUT_NS land_nat_udp_timeout_ns\n",
    )
    nat_rodata = "landscape-ebpf/src/bpf/nat_v4/tc_nat4_rodata.h"
    patch(
        nat_rodata,
        "const volatile __u16 land_nat_tcp_range_end = 0;\n",
        "const volatile __u16 land_nat_tcp_range_end = 0;\n\n"
        "/* CT timeouts in nanoseconds (set from userspace; defaults match the\n"
        " * historical hardcoded 6s / 10min / 5min). */\n"
        "const volatile __u64 land_nat_tcp_syn_timeout_ns = 6000000000ULL;\n"
        "const volatile __u64 land_nat_tcp_timeout_ns = 600000000000ULL;\n"
        "const volatile __u64 land_nat_udp_timeout_ns = 300000000000ULL;\n\n"
        "/* 1 = endpoint-independent filtering (full cone) for dynamic mappings. */\n"
        "const volatile __u8 land_nat_full_cone = 1;\n",
    )
    #    6c) full-cone by default: allow_reuse is OR-ed with the rodata switch.
    nat_packet = "landscape-ebpf/src/bpf/nat_v4/tc_nat4_packet.h"
    text = (ROOT / nat_packet).read_text(encoding="utf-8")
    old = "get_flow_allow_reuse_port(skb_mark(skb)) ? 1 : 0"
    new = "(land_nat_full_cone || get_flow_allow_reuse_port(skb_mark(skb))) ? 1 : 0"
    if old not in text:
        fail(f"anchor not found in {nat_packet}: {old}")
    (ROOT / nat_packet).write_text(text.replace(old, new), encoding="utf-8")
    print(f"patched: {nat_packet} (full cone default)")

    #    6d) Rust side: read nat_options for rodata + randomize port seeding.
    nat_rs = "landscape-ebpf/src/stages/nat4.rs"
    patch(
        nat_rs,
        "fn seed_port_queue<M>(map: &M, start: u16, end: u16, reserved: &HashSet<(u8, u16)>, proto: u8)\n",
        "/// Cheap per-process/per-proto PRNG seed for port-order shuffling.\n"
        "fn queue_seed(proto: u8) -> u64 {\n"
        "    let now = std::time::SystemTime::now()\n"
        "        .duration_since(std::time::UNIX_EPOCH)\n"
        "        .map(|d| d.as_nanos() as u64)\n"
        "        .unwrap_or(0x9E37_79B9_7F4A_7C15);\n"
        "    now ^ ((std::process::id() as u64) << 32) ^ ((proto as u64) << 8) ^ 0x9E37_79B9_7F4A_7C15\n"
        "}\n\n"
        "fn seed_port_queue<M>(map: &M, start: u16, end: u16, reserved: &HashSet<(u8, u16)>, proto: u8)\n",
    )
    patch(
        nat_rs,
        "    for port in start..=end {\n"
        "        if reserved.contains(&(proto, port)) {\n"
        "            continue;\n"
        "        }\n",
        "    // Randomize allocation order: sequential seeding made external port\n"
        "    // assignment predictable (NAT port prediction / scanning).\n"
        "    let mut ports: Vec<u16> =\n"
        "        (start..=end).filter(|p| !reserved.contains(&(proto, *p))).collect();\n"
        "    let mut seed = queue_seed(proto);\n"
        "    for i in (1..ports.len()).rev() {\n"
        "        seed ^= seed << 13;\n"
        "        seed ^= seed >> 7;\n"
        "        seed ^= seed << 17;\n"
        "        let j = (seed % (i as u64 + 1)) as usize;\n"
        "        ports.swap(i, j);\n"
        "    }\n\n"
        "    for port in ports {\n",
    )
    patch(
        nat_rs,
        "        rodata.land_nat_tcp_range_end = config.tcp_range.end;\n",
        "        rodata.land_nat_tcp_range_end = config.tcp_range.end;\n"
        "        let nat_opts = crate::nat_options::current();\n"
        "        rodata.land_nat_tcp_syn_timeout_ns = nat_opts.tcp_syn_timeout_ns;\n"
        "        rodata.land_nat_tcp_timeout_ns = nat_opts.tcp_timeout_ns;\n"
        "        rodata.land_nat_udp_timeout_ns = nat_opts.udp_timeout_ns;\n"
        "        rodata.land_nat_full_cone = if nat_opts.full_cone { 1 } else { 0 };\n",
    )

    # 7) NAT options plumbing (full cone switch in the NAT modal).
    #    7a) ebpf module + init
    patch(
        "landscape-ebpf/src/lib.rs",
        "pub mod landscape;\npub mod tcx;\n",
        "pub mod landscape;\npub mod tcx;\npub mod nat_options;\n",
    )
    patch(
        "landscape-ebpf/src/lib.rs",
        "    crate::tcx::init_from_env();\n",
        "    crate::tcx::init_from_env();\n    crate::nat_options::init();\n",
    )
    #    7b) backend module + routes
    patch(
        "landscape-webserver/src/system/mod.rs",
        "pub mod tcx_order;\n",
        "pub mod tcx_order;\npub mod nat_options;\n",
    )
    patch(
        "landscape-webserver/src/system/config.rs",
        "        .routes(routes!(super::tcx_order::get_tcx_order, super::tcx_order::update_tcx_order))\n",
        "        .routes(routes!(super::tcx_order::get_tcx_order, super::tcx_order::update_tcx_order))\n"
        "        .routes(routes!(super::nat_options::get_nat_options, super::nat_options::update_nat_options))\n",
    )

    #    7c) NAT modal: load/save options + full-cone switch
    nat_modal = "landscape-webui/src/components/nat/NATEditModal.vue"
    patch(
        nat_modal,
        'import Range from "@/components/PortRange.vue";\n',
        'import Range from "@/components/PortRange.vue";\n'
        'import {\n'
        '  get_nat_options,\n'
        '  update_nat_options,\n'
        '  type NatOptions,\n'
        '} from "@/api/nat_options";\n',
    )
    patch(
        nat_modal,
        "const link = ref<WanLink>(default_ethernet_link(iface_info.iface_name));\n",
        "const link = ref<WanLink>(default_ethernet_link(iface_info.iface_name));\n"
        "const nat_options = ref<NatOptions>({\n"
        "  full_cone: true,\n"
        "  tcp_syn_timeout_secs: 6,\n"
        "  tcp_timeout_secs: 600,\n"
        "  udp_timeout_secs: 300,\n"
        "});\n",
    )
    patch(
        nat_modal,
        "    default_ethernet_link(iface_info.iface_name);\n}\n",
        "    default_ethernet_link(iface_info.iface_name);\n"
        "  try {\n"
        "    nat_options.value = await get_nat_options();\n"
        "  } catch {\n"
        "    // keep defaults\n"
        "  }\n"
        "}\n",
    )
    patch(
        nat_modal,
        "  await wanLinkStore.UPDATE_INFO();\n  emit(\"refresh\");\n",
        "  await update_nat_options(nat_options.value);\n"
        "  await wanLinkStore.UPDATE_INFO();\n"
        "  emit(\"refresh\");\n",
    )
    patch(
        nat_modal,
        '      <n-form-item :label="t(\'nat.service_edit.icmp_id_range\')">\n'
        '        <Range v-model:range="link.nat.icmp_in_range"> </Range>\n'
        '      </n-form-item>\n',
        '      <n-form-item :label="t(\'nat.service_edit.icmp_id_range\')">\n'
        '        <Range v-model:range="link.nat.icmp_in_range"> </Range>\n'
        '      </n-form-item>\n'
        '      <n-form-item label="TCP 半开超时 (秒)">\n'
        '        <n-input-number\n'
        '          v-model:value="nat_options.tcp_syn_timeout_secs"\n'
        '          :min="1"\n'
        '          :max="86400"\n'
        '          style="width: 160px"\n'
        '        />\n'
        '      </n-form-item>\n'
        '      <n-form-item label="TCP 空闲超时 (秒)">\n'
        '        <n-input-number\n'
        '          v-model:value="nat_options.tcp_timeout_secs"\n'
        '          :min="1"\n'
        '          :max="86400"\n'
        '          style="width: 160px"\n'
        '        />\n'
        '      </n-form-item>\n'
        '      <n-form-item label="UDP/ICMP 超时 (秒)">\n'
        '        <n-input-number\n'
        '          v-model:value="nat_options.udp_timeout_secs"\n'
        '          :min="1"\n'
        '          :max="86400"\n'
        '          style="width: 160px"\n'
        '        />\n'
        '      </n-form-item>\n'
        '      <n-form-item label="Full Cone (EIM/EIF)">\n'
        '        <n-switch\n'
        '          :value="nat_options.full_cone"\n'
        '          @update:value="(v: boolean) => (nat_options.full_cone = v)"\n'
        '        />\n'
        '      </n-form-item>\n',
    )

    print(f"TCX overlay applied (overlay={OVERLAY})")


if __name__ == "__main__":
    main()
