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


def drop_if_block(path: str, marker: str) -> None:
    """Remove the `if (...) { ... }` block that starts at `marker`.

    Brace matching keeps this robust against upstream re-indentation, which a
    literal anchor would not survive.
    """
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    idx = text.find(marker)
    if idx < 0:
        fail(f"if-block marker not found in {path}: {marker}")
    brace = text.find("{", idx)
    if brace < 0:
        fail(f"no opening brace after marker in {path}")
    depth = 0
    i = brace
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    end = i + 1
    if end < len(text) and text[end] == "\n":
        end += 1
    text = text[:idx] + text[end:]
    p.write_text(text, encoding="utf-8")
    print(f"dropped if-block: {path}")


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
    #    6b) timeouts become rodata-driven, defaulting to einat's NAT timers:
    #        tcp_trans=2m, tcp_est=24h, udp_default=10m, icmp=1m.
    patch(
        nat_timer,
        "#define NAPT_TCP_SYN_TIMEOUT_NS (6ULL * NAPT_NSEC_PER_SEC)\n"
        "#define NAPT_TCP_TIMEOUT_NS (10ULL * 60ULL * NAPT_NSEC_PER_SEC)\n"
        "#define NAPT_UDP_TIMEOUT_NS (5ULL * 60ULL * NAPT_NSEC_PER_SEC)\n",
        "/* Timeouts come from rodata so userspace can tune them. */\n"
        "#define NAPT_TCP_SYN_TIMEOUT_NS land_nat_tcp_syn_timeout_ns\n"
        "#define NAPT_TCP_TIMEOUT_NS land_nat_tcp_timeout_ns\n"
        "#define NAPT_UDP_TIMEOUT_NS land_nat_udp_timeout_ns\n"
        "#define NAPT_UDP_MIN_TIMEOUT_NS land_nat_udp_min_timeout_ns\n"
        "#define NAPT_ICMP_TIMEOUT_NS land_nat_icmp_timeout_ns\n",
    )
    #        UDP becomes two-stage (einat style): short timeout until a reply
    #        is seen from the WAN side, then the long UDP timeout.
    patch(
        nat_timer,
        "    if (key->proto == IPV4_PROTO_UDP) return NAPT_UDP_TIMEOUT_NS;\n\n"
        "    if (key->proto == IPV4_PROTO_TCP) {\n",
        "    if (key->proto == IPV4_PROTO_UDP) {\n"
        "        /* Two-stage UDP timer: keep P2P/QUIC flows short until the\n"
        "         * peer answers, then switch to the established timeout. */\n"
        "        if (value->server_status == NAPT_TCP_STATE_LESS_EST)\n"
        "            return NAPT_UDP_TIMEOUT_NS;\n"
        "        return NAPT_UDP_MIN_TIMEOUT_NS;\n"
        "    }\n"
        "    if (key->proto == IPV4_PROTO_ICMP) return NAPT_ICMP_TIMEOUT_NS;\n\n"
        "    if (key->proto == IPV4_PROTO_TCP) {\n",
    )
    #        TCP: only a fully established flow (both directions carried data)
    #        keeps the long idle timeout; SYN/FIN/RST are transitory.
    patch(
        nat_timer,
        "    if (key->proto == IPV4_PROTO_TCP) {\n"
        "        if (value->client_status == NAPT_TCP_STATE_SYN &&\n"
        "            value->server_status == NAPT_TCP_STATE_SYN)\n"
        "            return NAPT_TCP_TIMEOUT_NS;\n"
        "        return NAPT_TCP_SYN_TIMEOUT_NS;\n"
        "    }\n",
        "    if (key->proto == IPV4_PROTO_TCP) {\n"
        "        if (value->client_status == NAPT_TCP_STATE_LESS_EST &&\n"
        "            value->server_status == NAPT_TCP_STATE_LESS_EST)\n"
        "            return NAPT_TCP_TIMEOUT_NS;\n"
        "        return NAPT_TCP_SYN_TIMEOUT_NS;\n"
        "    }\n",
    )
    nat_rodata = "landscape-ebpf/src/bpf/nat_v4/tc_nat4_rodata.h"
    patch(
        nat_rodata,
        "const volatile __u16 land_nat_tcp_range_end = 0;\n",
        "const volatile __u16 land_nat_tcp_range_end = 0;\n\n"
        "/* CT timeouts in nanoseconds (set from userspace). Defaults mirror\n"
        " * einat: tcp_trans=2m, tcp_est=24h, udp=10m, icmp=1m. */\n"
        "const volatile __u64 land_nat_tcp_syn_timeout_ns = 120000000000ULL;\n"
        "const volatile __u64 land_nat_tcp_timeout_ns = 86400000000000ULL;\n"
        "const volatile __u64 land_nat_udp_timeout_ns = 600000000000ULL;\n"
        "const volatile __u64 land_nat_udp_min_timeout_ns = 120000000000ULL;\n"
        "const volatile __u64 land_nat_icmp_timeout_ns = 60000000000ULL;\n\n"
        "/* 1 = endpoint-independent filtering (full cone) for dynamic mappings. */\n"
        "const volatile __u8 land_nat_full_cone = 1;\n\n"
        "/* 1 = try to keep the client's source port (opt-in, default off). */\n"
        "const volatile __u8 land_nat_port_preserve = 0;\n",
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
        "        rodata.land_nat_udp_min_timeout_ns = nat_opts.udp_min_timeout_ns;\n"
        "        rodata.land_nat_icmp_timeout_ns = nat_opts.icmp_timeout_ns;\n"
        "        rodata.land_nat_full_cone = if nat_opts.full_cone { 1 } else { 0 };\n"
        "        rodata.land_nat_port_preserve = if nat_opts.port_preserve { 1 } else { 0 };\n"
        "        tracing::info!(\n"
        "            full_cone = nat_opts.full_cone,\n"
        "            port_preserve = nat_opts.port_preserve,\n"
        "            \"nat4 runtime options\"\n"
        "        );\n",
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
        "  tcp_syn_timeout_secs: 120,\n"
        "  tcp_timeout_secs: 86400,\n"
        "  udp_min_timeout_secs: 120,\n"
        "  udp_timeout_secs: 600,\n"
        "  icmp_timeout_secs: 60,\n"
        "  port_preserve: false,\n"
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
        '      <n-divider style="margin: 8px 0">超时与过滤</n-divider>\n'
        '      <n-grid :cols="2" :x-gap="12">\n'
        '        <n-gi>\n'
        '          <n-form-item label="TCP 半开超时 (秒)">\n'
        '            <n-input-number v-model:value="nat_options.tcp_syn_timeout_secs" :min="1" :max="86400" style="width: 100%" />\n'
        '          </n-form-item>\n'
        '        </n-gi>\n'
        '        <n-gi>\n'
        '          <n-form-item label="TCP 空闲超时 (秒)">\n'
        '            <n-input-number v-model:value="nat_options.tcp_timeout_secs" :min="1" :max="86400" style="width: 100%" />\n'
        '          </n-form-item>\n'
        '        </n-gi>\n'
        '        <n-gi>\n'
        '          <n-form-item label="UDP 半开超时 (秒)">\n'
        '            <n-input-number v-model:value="nat_options.udp_min_timeout_secs" :min="1" :max="86400" style="width: 100%" />\n'
        '          </n-form-item>\n'
        '        </n-gi>\n'
        '        <n-gi>\n'
        '          <n-form-item label="UDP 超时 (秒)">\n'
        '            <n-input-number v-model:value="nat_options.udp_timeout_secs" :min="1" :max="86400" style="width: 100%" />\n'
        '          </n-form-item>\n'
        '        </n-gi>\n'
        '        <n-gi>\n'
        '          <n-form-item label="ICMP 超时 (秒)">\n'
        '            <n-input-number v-model:value="nat_options.icmp_timeout_secs" :min="1" :max="86400" style="width: 100%" />\n'
        '          </n-form-item>\n'
        '        </n-gi>\n'
        '      </n-grid>\n'
        '      <n-grid :cols="2" :x-gap="12">\n'
        '        <n-gi>\n'
        '          <n-form-item label="Full Cone (EIM/EIF)">\n'
        '            <n-switch :value="nat_options.full_cone" @update:value="(v: boolean) => (nat_options.full_cone = v)" />\n'
        '          </n-form-item>\n'
        '        </n-gi>\n'
        '        <n-gi>\n'
        '          <n-form-item label="端口保持 (Port Preserve)">\n'
        '            <n-switch :value="nat_options.port_preserve === true" @update:value="(v: boolean) => (nat_options.port_preserve = v)" />\n'
        '          </n-form-item>\n'
        '        </n-gi>\n'
        '      </n-grid>\n',
    )

    # 8) NAT4 port preservation (opt-in via LANDSCAPE_NAT_PORT_PRESERVE=1).
    nat_tc = "landscape-ebpf/src/bpf/nat_v4/tc_nat4.h"
    patch(
        nat_tc,
        '#include "nat_v4/tc_nat4_map.h"\n',
        '#include "nat_v4/tc_nat4_map.h"\n#include "nat_v4/tc_nat4_rodata.h"\n',
    )
    patch(
        nat_tc,
        "#define NAPT_IP_MULTICAST_MASK bpf_ntohl(0xF0000000)\n",
        "/* Claim a free WAN slot for the client's original source port. Free slots\n"
        " * are zeroed by napt_wan_clear(), so generation 0 -> sentinel CAS wins\n"
        " * exactly once even when two CPUs race on the same internal port. */\n"
        "static __always_inline int napt_try_preserve_port(__u8 proto, __be16 internal_port,\n"
        "                                                  __be16 *out_port, __u32 *out_gen) {\n"
        "    struct napt_from_wan_value *slot;\n"
        "    __u16 host_port = bpf_ntohs(internal_port);\n"
        "    __u16 range_start;\n"
        "    __u16 range_end;\n"
        "    __u64 expected_state = 0;\n\n"
        "    if (!land_nat_port_preserve) return -1;\n"
        "    if (proto == IPV4_PROTO_TCP) {\n"
        "        range_start = land_nat_tcp_range_start;\n"
        "        range_end = land_nat_tcp_range_end;\n"
        "    } else if (proto == IPV4_PROTO_UDP) {\n"
        "        range_start = land_nat_udp_range_start;\n"
        "        range_end = land_nat_udp_range_end;\n"
        "    } else {\n"
        "        return -1;\n"
        "    }\n"
        "    if (host_port < range_start || host_port >= range_end) return -1;\n\n"
        "    slot = napt_wan_lookup(proto, internal_port);\n"
        "    if (!slot) return -1;\n\n"
        "    /* 64-bit CAS only: the BPF backend rejects 32-bit cmpxchg. Free\n"
        "     * slots are zeroed by napt_wan_clear(), and BIT(56..63)=0xFF is\n"
        "     * never a valid state, so this claims the slot exactly once. */\n"
        "    if (!__sync_bool_compare_and_swap(&slot->state_ref, expected_state,\n"
        "                                      0xFFFFFFFFFFFFFFFFULL))\n"
        "        return -1;\n\n"
        "    *out_port = internal_port;\n"
        "    *out_gen = (__u32)bpf_ktime_get_ns() | 1U;\n"
        "    return 0;\n"
        "}\n\n"
        "static __always_inline int napt_create_preserved_mapping(\n"
        "    __u8 proto, __be32 internal_addr, __be16 internal_port, __be32 remote_addr,\n"
        "    __be16 remote_port, __be16 external_port, __u32 gen, __u8 allow_reuse) {\n"
        "    struct napt_from_wan_value *slot = napt_wan_lookup(proto, external_port);\n"
        "    struct napt_from_lan_value lan = {};\n\n"
        "    if (!slot || slot->state_ref != 0xFFFFFFFFFFFFFFFFULL) return -1;\n\n"
        "    slot->internal_addr = internal_addr;\n"
        "    slot->internal_port = internal_port;\n"
        "    slot->allow_reuse = allow_reuse;\n"
        "    slot->trigger_addr = remote_addr;\n"
        "    slot->trigger_port = remote_port;\n"
        "    slot->generation = gen;\n"
        "    slot->state_ref = napt_state_make(NAPT_WAN_STATE_ACTIVE, 0);\n"
        "    slot->kind = NAPT_MAPPING_DYNAMIC;\n\n"
        "    lan.external_port = external_port;\n"
        "    lan.kind = NAPT_MAPPING_DYNAMIC;\n"
        "    lan.allow_reuse = allow_reuse;\n"
        "    lan.generation = gen;\n"
        "    lan.trigger_addr = remote_addr;\n"
        "    lan.trigger_port = remote_port;\n\n"
        "    if (napt_lan_insert(internal_addr, internal_port, proto, &lan) != 0) {\n"
        "        napt_wan_clear(proto, external_port);\n"
        "        return -1;\n"
        "    }\n"
        "    return 0;\n"
        "}\n\n"
        "#define NAPT_IP_MULTICAST_MASK bpf_ntohl(0xF0000000)\n",
    )
    nat_packet2 = "landscape-ebpf/src/bpf/nat_v4/tc_nat4_packet.h"
    patch(
        nat_packet2,
        "        if (napt_queue_pop(proto, &external_port, &gen) != 0) return TC_ACT_SHOT;\n",
        "        __u8 port_preserved = 0;\n"
        "        if (land_nat_port_preserve &&\n"
        "            (proto == IPV4_PROTO_TCP || proto == IPV4_PROTO_UDP) &&\n"
        "            napt_try_preserve_port(proto, internal_port, &external_port, &gen) == 0) {\n"
        "            port_preserved = 1;\n"
        "        } else {\n"
        "            int created = -1;\n"
        "            /* A preserved port is still sitting in the queue; skip such\n"
        "             * entries instead of failing the flow. */\n"
        "#pragma unroll\n"
        "            for (int attempt = 0; attempt < 8; attempt++) {\n"
        "                if (napt_queue_pop(proto, &external_port, &gen) != 0) break;\n"
        "                struct napt_from_wan_value *slot = napt_wan_lookup(proto, external_port);\n"
        "                if (slot && slot->kind != NAPT_MAPPING_FREE) {\n"
        "                    napt_queue_push(proto, external_port, gen);\n"
        "                    continue;\n"
        "                }\n"
        "                created = napt_create_dynamic_mapping(proto, src_addr, internal_port, dst_addr,\n"
        "                                                      remote_port, external_port, gen, allow_reuse);\n"
        "                if (created == 0) break;\n"
        "                napt_queue_push(proto, external_port, gen);\n"
        "            }\n"
        "            if (created != 0) return TC_ACT_SHOT;\n"
        "        }\n"
        "        if (port_preserved &&\n"
        "            napt_create_preserved_mapping(proto, src_addr, internal_port, dst_addr, remote_port,\n"
        "                                          external_port, gen, allow_reuse) != 0)\n"
        "            return TC_ACT_SHOT;\n",
    )
    # The dynamic-create call moved into the retry loop above; drop the old
    # single-shot block (brace matching, so upstream indentation can change).
    drop_if_block(
        nat_packet2,
        "        if (napt_create_dynamic_mapping(proto, src_addr, internal_port, dst_addr,",
    )
    print(f"TCX overlay applied (overlay={OVERLAY})")


if __name__ == "__main__":
    main()
