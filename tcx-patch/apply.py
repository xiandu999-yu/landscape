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
        "    if (napt_metric_try_report(key, value, NAPT_METRIC_ACTIVE))\n"
        "        napt_stat_inc(NAPT_STAT_METRIC_DROP);\n",
    )
    #        small per-CPU counters for the failure paths (read with bpftool)
    patch(
        "landscape-ebpf/src/bpf/nat_v4/tc_nat4_map.h",
        '} napt_icmp_ids SEC(".maps");\n',
        '} napt_icmp_ids SEC(".maps");\n\n'
        'enum napt_stat_idx {\n'
        '    NAPT_STAT_PORT_EXHAUSTED = 0,\n'
        '    NAPT_STAT_METRIC_DROP = 1,\n'
        '    /* 2 was PRESERVE_OK; port preserve was removed. */\n'
        '    NAPT_STAT_MAP_MISS = 3,\n'
        '    NAPT_STAT_REMOTE_DENIED = 4,\n'
        '    NAPT_STAT_CT_FULL = 5,\n'
        '    NAPT_STAT_CT_BUSY = 6,\n'
        '};\n\n'
        'struct {\n'
        '    __uint(type, BPF_MAP_TYPE_ARRAY);\n'
        '    __uint(max_entries, 8);\n'
        '    __type(key, __u32);\n'
        '    __type(value, __u64);\n'
        '} napt_stats SEC(".maps");\n\n'
        'static __always_inline void napt_stat_inc(__u32 idx) {\n'
        '    __u64 *slot = bpf_map_lookup_elem(&napt_stats, &idx);\n'
        '    if (slot) __sync_fetch_and_add(slot, 1);\n'
        '}\n',
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
    #        CT timer tick / metric report interval: 5s -> configurable (30s).
    patch(
        nat_timer,
        "#define NAPT_CT_REPORT_INTERVAL_NS (5ULL * NAPT_NSEC_PER_SEC)\n",
        "#define NAPT_CT_REPORT_INTERVAL_NS land_nat_report_interval_get()\n",
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
    #        NOTE: the TCP branch of napt_ct_wait_ns() is intentionally left at
    #        the upstream SYN/SYN test.  napt_ct_advance_pkt() only ever moves a
    #        TCP CT to SYN/FIN/INIT (data packets never set LESS_EST), so an
    #        "established == both LESS_EST" test would be dead code and every
    #        TCP flow would fall back to the 120s half-open timeout.
    #        shared timeout selector (used by the packet path in event-driven mode)
    patch(
        nat_timer,
        "static __always_inline __u64 napt_ct_wait_ns(const struct napt_ct_key *key,\n",
        "static __always_inline __u64 napt_ct_timeout_for(__u8 proto,\n"
        "                                                 const struct napt_ct_value *value) {\n"
        "    if (proto == IPV4_PROTO_UDP) {\n"
        "        if (value->server_status == NAPT_TCP_STATE_LESS_EST) return NAPT_UDP_TIMEOUT_NS;\n"
        "        return NAPT_UDP_MIN_TIMEOUT_NS;\n"
        "    }\n"
        "    if (proto == IPV4_PROTO_ICMP) return NAPT_ICMP_TIMEOUT_NS;\n"
        "    if (proto == IPV4_PROTO_TCP) {\n"
        "        if (value->client_status == NAPT_TCP_STATE_SYN &&\n"
        "            value->server_status == NAPT_TCP_STATE_SYN)\n"
        "            return NAPT_TCP_TIMEOUT_NS;\n"
        "        return NAPT_TCP_SYN_TIMEOUT_NS;\n"
        "    }\n"
        "    return NAPT_UDP_TIMEOUT_NS;\n"
        "}\n\n"
        "static __always_inline __u64 napt_ct_wait_ns(const struct napt_ct_key *key,\n",
    )
    #        event-driven mode: the timer is armed with the flow's idle timeout
    #        and refreshed from the packet path (6e). When it fires while the CT
    #        is still ACTIVE the flow is genuinely idle, so release it in this
    #        same callback. (The old code re-armed a 1ns timer and released on
    #        the second pass; that pass raced with a packet that had just
    #        refreshed the CT and could kill a live flow.)
    patch(
        nat_timer,
        "    if (current == NAPT_CT_RELEASE) {\n",
        "    if (land_nat_event_driven_get() && current == NAPT_CT_ACTIVE) {\n"
        "        if (!value->pad1) {\n"
        "            /* The tick that armed this timer was the periodic one, i.e. the\n"
        "             * mode was switched to event-driven after this flow started.\n"
        "             * Give it its idle timeout instead of tearing it down now. */\n"
        "            value->pad1 = 1;\n"
        "            *next_timeout = napt_ct_timeout_for(key->proto, value);\n"
        "            return NAPT_TIMER_STEP_RESTART;\n"
        "        }\n"
        "        if (!__sync_bool_compare_and_swap(&value->status, current, NAPT_CT_RELEASE)) {\n"
        "            *next_timeout = NAPT_CT_REPORT_INTERVAL_NS;\n"
        "            return NAPT_TIMER_STEP_RESTART;\n"
        "        }\n"
        "        current = NAPT_CT_RELEASE;\n"
        "    }\n\n"
        "    if (current == NAPT_CT_RELEASE) {\n",
    )
    #        packet path: in event-driven mode re-arm with the flow's idle timeout
    patch(
        nat_timer,
        "        if (prev == status) napt_ct_timer_start(value, NAPT_CT_REPORT_INTERVAL_NS);\n",
        "        if (prev == status) {\n"
        "            __u64 next_interval = land_nat_event_driven_get()\n"
        "                                      ? napt_ct_timeout_for(l4_protocol, value)\n"
        "                                      : NAPT_CT_REPORT_INTERVAL_NS;\n"
        "            napt_ct_timer_start(value, next_interval);\n"
        "            /* pad1 marks a timer armed with the flow's idle timeout (event\n"
        "             * mode), so a later mode switch can tell it apart from a tick. */\n"
        "            value->pad1 = land_nat_event_driven_get() ? 1 : 0;\n"
        "        }\n",
    )
    #        RST: tear the flow down immediately (ports return via RELEASE).
    patch(
        nat_timer,
        "            napt_ct_timer_start(value, next_interval);\n"
        "            /* pad1 marks a timer armed with the flow's idle timeout (event\n"
        "             * mode), so a later mode switch can tell it apart from a tick. */\n"
        "            value->pad1 = land_nat_event_driven_get() ? 1 : 0;\n"
        "        }\n"
        "    }\n"
        "\n"
        "    return TC_ACT_OK;\n"
        "}\n",
        "            napt_ct_timer_start(value, next_interval);\n"
        "            /* pad1 marks a timer armed with the flow's idle timeout (event\n"
        "             * mode), so a later mode switch can tell it apart from a tick. */\n"
        "            value->pad1 = land_nat_event_driven_get() ? 1 : 0;\n"
        "        }\n"
        "    }\n"
        "\n"
        "    if (l4_protocol == IPV4_PROTO_TCP && tcp_phase == NAPT_TCP_PHASE_RST) {\n"
        "        __u64 cur = value->status;\n"
        "        if ((cur == NAPT_CT_ACTIVE || cur == NAPT_CT_IDLE_1 || cur == NAPT_CT_IDLE_2) &&\n"
        "            __sync_bool_compare_and_swap(&value->status, cur, NAPT_CT_RELEASE))\n"
        "            napt_ct_timer_start(value, 1);\n"
        "    }\n"
        "\n"
        "    return TC_ACT_OK;\n"
        "}\n",
    )
    #        packet path: refresh the idle timer on every packet in event-driven
    #        mode. einat does exactly this from ct_state_transition() by calling
    #        ct_reset_timer() on each packet. Without it the timer keeps the
    #        interval armed at activation time -- before the first reply -- so a
    #        busy UDP flow was torn down after udp_min (120s) and came back with
    #        a brand-new external port.
    patch(
        nat_timer,
        "            napt_ct_timer_start(value, next_interval);\n"
        "            /* pad1 marks a timer armed with the flow's idle timeout (event\n"
        "             * mode), so a later mode switch can tell it apart from a tick. */\n"
        "            value->pad1 = land_nat_event_driven_get() ? 1 : 0;\n"
        "        }\n"
        "    }\n"
        "\n"
        "    if (l4_protocol == IPV4_PROTO_TCP && tcp_phase == NAPT_TCP_PHASE_RST) {\n",
        "            napt_ct_timer_start(value, next_interval);\n"
        "            /* pad1 marks a timer armed with the flow's idle timeout (event\n"
        "             * mode), so a later mode switch can tell it apart from a tick. */\n"
        "            value->pad1 = land_nat_event_driven_get() ? 1 : 0;\n"
        "        }\n"
        "    } else if (land_nat_event_driven_get() && status == NAPT_CT_ACTIVE) {\n"
        "        napt_ct_timer_start(value, napt_ct_timeout_for(l4_protocol, value));\n"
        "        value->pad1 = 1;\n"
        "    }\n"
        "\n"
        "    if (l4_protocol == IPV4_PROTO_TCP && tcp_phase == NAPT_TCP_PHASE_RST) {\n",
    )
    #        count packets that hit a CT which is still being torn down: the
    #        recreate is then blocked by BPF_NOEXIST and the packet is dropped.
    #        Increments while gaming mean the fail-closed teardown window is
    #        being hit (see the UDP timer fix above).
    patch(
        nat_timer,
        "    if (!napt_ct_status_usable(value->status)) return -1;\n",
        "    if (!napt_ct_status_usable(value->status)) {\n"
        "        napt_stat_inc(NAPT_STAT_CT_BUSY);\n"
        "        return -1;\n"
        "    }\n",
    )
    nat_rodata = "landscape-ebpf/src/bpf/nat_v4/tc_nat4_rodata.h"
    patch(
        nat_rodata,
        "#include <vmlinux.h>\n",
        "#include <vmlinux.h>\n#include <bpf/bpf_helpers.h>\n",
    )
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
        "const volatile __u64 land_nat_icmp_timeout_ns = 60000000000ULL;\n"
        "/* CT timer tick / metric report interval (default 30s, was 5s). */\n"
        "const volatile __u64 land_nat_report_interval_ns = 30000000000ULL;\n\n"
        "/* 1 = endpoint-independent filtering (full cone) for dynamic mappings. */\n"
        "const volatile __u8 land_nat_full_cone = 1;\n\n"
        "/* 1 = event-driven CT timers (no periodic tick; metrics stop). */\n"
        "const volatile __u8 land_nat_event_driven = 1;\n\n"
        "/* Runtime-writable NAT knobs. Everything above lands in .rodata, which\n"
        " * libbpf freezes at load; these two knobs live in a one-entry array so\n"
        " * switching the metric mode (event-driven timers <-> periodic reporting)\n"
        " * takes effect on the next timer tick without re-attaching the NAT\n"
        " * segment. Userspace writes the struct; a zero interval or a missing\n"
        " * entry falls back to the rodata defaults above. */\n"
        "struct land_nat_dyn_cfg {\n"
        "    __u64 report_interval_ns;\n"
        "    __u8 event_driven;\n"
        "    __u8 pad[7];\n"
        "};\n\n"
        "struct {\n"
        "    __uint(type, BPF_MAP_TYPE_ARRAY);\n"
        "    __uint(max_entries, 1);\n"
        "    __type(key, __u32);\n"
        "    __type(value, struct land_nat_dyn_cfg);\n"
        "} land_nat_dyn SEC(\".maps\");\n\n"
        "static __always_inline __u64 land_nat_report_interval_get(void) {\n"
        "    __u32 key = 0;\n"
        "    struct land_nat_dyn_cfg *cfg = bpf_map_lookup_elem(&land_nat_dyn, &key);\n"
        "    if (cfg && cfg->report_interval_ns) return cfg->report_interval_ns;\n"
        "    return land_nat_report_interval_ns;\n"
        "}\n\n"
        "static __always_inline int land_nat_event_driven_get(void) {\n"
        "    __u32 key = 0;\n"
        "    struct land_nat_dyn_cfg *cfg = bpf_map_lookup_elem(&land_nat_dyn, &key);\n"
        "    return cfg ? cfg->event_driven : land_nat_event_driven;\n"
        "}\n",
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
        "        rodata.land_nat_report_interval_ns = nat_opts.report_interval_ns;\n"
        "        rodata.land_nat_full_cone = if nat_opts.full_cone { 1 } else { 0 };\n"
        "        rodata.land_nat_event_driven = if nat_opts.event_driven { 1 } else { 0 };\n"
        "        tracing::info!(\n"
        "            full_cone = nat_opts.full_cone,\n"
        "            event_driven = nat_opts.event_driven,\n"
        "            report_interval_ns = nat_opts.report_interval_ns,\n"
        "            \"nat4 runtime options\"\n"
        "        );\n",
    )
    #    6e) make the timer model runtime-tunable: bind the writable
    #        `land_nat_dyn` map after load and push the current options into it,
    #        so a metric-mode switch does not need a NAT re-attach.
    patch(
        nat_rs,
        '    let tc_skel = bpf_ctx!(open.load(), "load tc_nat4_v4 skeleton")?;\n',
        '    let tc_skel = bpf_ctx!(open.load(), "load tc_nat4_v4 skeleton")?;\n'
        '    crate::nat_options::bind_cfg_map(tc_skel.maps.land_nat_dyn.as_fd().as_raw_fd());\n',
    )
    patch(
        nat_rs,
        "    crate::bpf_ctx!(\n"
        "        xdp_open.maps.napt_ct.reuse_fd(tc_skel.maps.napt_ct.as_fd()),\n"
        "        \"xdp_nat4_v4 reuse napt_ct\"\n"
        "    )?;\n",
        "    crate::bpf_ctx!(\n"
        "        xdp_open.maps.napt_ct.reuse_fd(tc_skel.maps.napt_ct.as_fd()),\n"
        "        \"xdp_nat4_v4 reuse napt_ct\"\n"
        "    )?;\n"
        "    crate::bpf_ctx!(\n"
        "        xdp_open.maps.land_nat_dyn.reuse_fd(tc_skel.maps.land_nat_dyn.as_fd()),\n"
        "        \"xdp_nat4_v4 reuse land_nat_dyn\"\n"
        "    )?;\n",
    )
    #    6f) metric mode drives the timer model: when the mode changes, re-read
    #        the NAT options and push them into the live programs.
    patch(
        "landscape/src/metric/mod.rs",
        "        if !matches!(self.current_mode(), MetricMode::Off) {\n"
        "            self.start_service_locked().await?;\n"
        "        }\n"
        "        Ok(())\n"
        "    }\n",
        "        if !matches!(self.current_mode(), MetricMode::Off) {\n"
        "            self.start_service_locked().await?;\n"
        "        }\n"
        "        // The metric mode gates the NAT timer model (event-driven timers vs\n"
        "        // periodic reporting). Re-read the NAT options and push them into the\n"
        "        // attached programs so the switch needs no NAT re-attach.\n"
        "        landscape_ebpf::nat_options::reload();\n"
        "        Ok(())\n"
        "    }\n",
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
        '      </n-grid>\n',
    )

    nat_packet2 = "landscape-ebpf/src/bpf/nat_v4/tc_nat4_packet.h"

    # 8b) failure counters on the WAN ingress path.
    patch(
        nat_packet2,
        "    if (!mapping || (mapping->kind != NAPT_MAPPING_DYNAMIC && mapping->kind != NAPT_MAPPING_STATIC))\n"
        "        return TC_ACT_SHOT;\n",
        "    if (!mapping || (mapping->kind != NAPT_MAPPING_DYNAMIC && mapping->kind != NAPT_MAPPING_STATIC)) {\n"
        "        napt_stat_inc(NAPT_STAT_MAP_MISS);\n"
        "        return TC_ACT_SHOT;\n"
        "    }\n",
    )
    patch(
        nat_packet2,
        "        if (!napt_dynamic_remote_allowed(mapping, src_addr, remote_port, proto)) return TC_ACT_SHOT;\n",
        "        if (!napt_dynamic_remote_allowed(mapping, src_addr, remote_port, proto)) {\n"
        "            napt_stat_inc(NAPT_STAT_REMOTE_DENIED);\n"
        "            return TC_ACT_SHOT;\n"
        "        }\n",
    )
    patch(
        nat_packet2,
        "        if (!ct) return TC_ACT_SHOT;\n",
        "        if (!ct) {\n"
        "            napt_stat_inc(NAPT_STAT_CT_FULL);\n"
        "            return TC_ACT_SHOT;\n"
        "        }\n",
    )
    # 9) einat coexistence: ingress dispatch fallback keyed by ingress ifindex.
    #    tc_wan_ingress_intro dispatches on the *destination* address through the
    #    wan_trie. A TC program in front of it that already rewrote daddr (einat
    #    ingress DNAT: wan_ip:port -> lan_ip:port) makes that lookup miss, and
    #    the intro then drops the packet (TC_ACT_SHOT). Restore the retired 0x02
    #    ifindex namespace and fall back to it on a dst miss, so einat can own
    #    NAT while landscape still routes the (already DNATed) packet to the LAN.
    wan_trie_h = "landscape-ebpf/src/bpf/map/wan_trie.h"
    patch(
        wan_trie_h,
        "#define WAN_TRIE_TAG_V4 0x00\n"
        "#define WAN_TRIE_TAG_PPP 0x03\n",
        "#define WAN_TRIE_TAG_V4 0x00\n"
        "/* Ingress-ifindex fallback namespace. Used when daddr no longer names a\n"
        " * WAN address because an earlier TC program (einat ingress DNAT) rewrote\n"
        " * it to the internal/LAN address. */\n"
        "#define WAN_TRIE_TAG_IFX 0x02\n"
        "#define WAN_TRIE_TAG_PPP 0x03\n",
    )
    patch(
        wan_trie_h,
        "static __always_inline u64 wan_trie_key_ppp(u32 ifindex, __be16 session) {\n"
        "    return WAN_TRIE_TAG_PPP | ((u64)bpf_htonl(ifindex) << 8) | ((u64)session << 40);\n"
        "}\n",
        "static __always_inline u64 wan_trie_key_ppp(u32 ifindex, __be16 session) {\n"
        "    return WAN_TRIE_TAG_PPP | ((u64)bpf_htonl(ifindex) << 8) | ((u64)session << 40);\n"
        "}\n\n"
        "/* [ 02 | i0 i1 i2 i3 | 0 0 0 ], plen 40: the ingress interface's WAN\n"
        " * chain. Exact match, five hops. */\n"
        "static __always_inline u64 wan_trie_key_ifx(u32 ifindex) {\n"
        "    return WAN_TRIE_TAG_IFX | ((u64)bpf_htonl(ifindex) << 8);\n"
        "}\n",
    )
    patch(
        "landscape-ebpf/src/bpf/tc_wan_ingress_intro.bpf.c",
        "static __always_inline void tc_intro_dispatch(struct __sk_buff *skb, u64 key) {\n"
        "    u32 root = wan_trie_resolve(&wan_trie_tbl, key);\n"
        "    if (root) {\n"
        "        bpf_tail_call(skb, &tc_pipe_root_progs, root);\n"
        "    }\n"
        "}\n",
        "static __always_inline void tc_intro_dispatch(struct __sk_buff *skb, u64 key) {\n"
        "    u32 root = wan_trie_resolve(&wan_trie_tbl, key);\n"
        "    if (unlikely(!root)) {\n"
        "        /* daddr no longer names a WAN address: a program in front of this\n"
        "         * one (einat ingress DNAT) already rewrote it to the LAN client, so\n"
        "         * dispatch by the receiving interface's link chain instead.\n"
        "         *\n"
        "         * Read skb->ingress_ifindex on its own: a `cond ? ingress_ifindex\n"
        "         * : ifindex` select lets clang fold both ctx loads into one\n"
        "         * variable-offset access, which the verifier rejects with\n"
        "         * \"dereference of modified ctx ptr ... disallowed\" (EACCES). */\n"
        "        u32 ifx = skb->ingress_ifindex;\n"
        "        root = wan_trie_resolve(&wan_trie_tbl, wan_trie_key_ifx(ifx));\n"
        "    }\n"
        "    if (root) {\n"
        "        bpf_tail_call(skb, &tc_pipe_root_progs, root);\n"
        "    }\n"
        "}\n",
    )
    # control plane: publish/drop the ifindex key together with the link's binds
    wan_builder = "landscape-ebpf/src/maps/wan_trie/builder.rs"
    patch(
        wan_builder,
        "pub(crate) const TAG_V4: u8 = 0x00;\n"
        "pub(crate) const TAG_PPP: u8 = 0x03;\n",
        "pub(crate) const TAG_V4: u8 = 0x00;\n"
        "/* ingress-ifindex fallback (see WAN_TRIE_TAG_IFX in the header) */\n"
        "pub(crate) const TAG_IFX: u8 = 0x02;\n"
        "pub(crate) const TAG_PPP: u8 = 0x03;\n",
    )
    patch(
        wan_builder,
        "/// IPv6 entry: the raw prefix64, plen 0..=64. The masked first byte must\n",
        "/// Ingress-ifindex fallback entry: `ifindex` at trie bytes 1..5 behind the\n"
        "/// tag, big-endian, exact match at plen 40. Published per WAN link so the\n"
        "/// ingress intro still dispatches after einat DNAT rewrote daddr to a LAN\n"
        "/// address (the dst key no longer matches any WAN prefix).\n"
        "pub(crate) fn ifx_entry(ifindex: u32, root: u32) -> PrefixEntry {\n"
        "    PrefixEntry {\n"
        "        prefix: [\n"
        "            TAG_IFX,\n"
        "            (ifindex >> 24) as u8,\n"
        "            (ifindex >> 16) as u8,\n"
        "            (ifindex >> 8) as u8,\n"
        "            ifindex as u8,\n"
        "            0,\n"
        "            0,\n"
        "            0,\n"
        "        ],\n"
        "        plen: 40,\n"
        "        root,\n"
        "    }\n"
        "}\n\n"
        "/// IPv6 entry: the raw prefix64, plen 0..=64. The masked first byte must\n",
    )
    wan_disp = "landscape-ebpf/src/maps/wan_trie/dispatcher.rs"
    patch(
        wan_disp,
        "    PrefixEntry, masked_key, ppp_entry, reference_lpm, v4_entry, v6_entry, wan_trie_publish,\n",
        "    PrefixEntry, ifx_entry, masked_key, ppp_entry, reference_lpm, v4_entry, v6_entry,\n"
        "    wan_trie_publish,\n",
    )
    patch(
        wan_disp,
        "    pub(crate) fn del_ppp_session(&self, ifindex: u32, session: u16) {\n"
        "        self.apply(&[Op::Del {\n"
        "            prefix: ppp_entry(ifindex, session, 0).prefix,\n"
        "            plen: 56,\n"
        "        }]);\n"
        "    }\n",
        "    pub(crate) fn del_ppp_session(&self, ifindex: u32, session: u16) {\n"
        "        self.apply(&[Op::Del {\n"
        "            prefix: ppp_entry(ifindex, session, 0).prefix,\n"
        "            plen: 56,\n"
        "        }]);\n"
        "    }\n\n"
        "    /// Ingress-ifindex fallback: which link chain owns this interface.\n"
        "    pub(crate) fn set_ifindex(&self, ifindex: u32, root: u32) {\n"
        "        self.apply(&[Op::Set(ifx_entry(ifindex, root))]);\n"
        "    }\n\n"
        "    pub(crate) fn del_ifindex(&self, ifindex: u32) {\n"
        "        self.apply(&[Op::Del { prefix: ifx_entry(ifindex, 0).prefix, plen: 40 }]);\n"
        "    }\n",
    )
    wan_link = "landscape-ebpf/src/chain/wan_link/mod.rs"
    patch(
        wan_link,
        "    fn publish_bind(&self, bind: &AddrBind) {\n"
        "        if self.id == 0 {\n"
        "            tracing::warn!(\"wan bind with unassigned chain 0; trie publish skipped\");\n"
        "            return;\n"
        "        }\n"
        "        match bind.addr {\n",
        "    fn publish_bind(&self, bind: &AddrBind) {\n"
        "        if self.id == 0 {\n"
        "            tracing::warn!(\"wan bind with unassigned chain 0; trie publish skipped\");\n"
        "            return;\n"
        "        }\n"
        "        // Ingress fallback key: keeps WAN ingress dispatchable after einat\n"
        "        // DNAT rewrote daddr to an internal address. OFF by default: the\n"
        "        // fallback forwards WAN->LAN through bpf_redirect, i.e. it bypasses\n"
        "        // the netfilter forward chain, so it must be an explicit choice.\n"
        "        if ingress_ifx_fallback_enabled() {\n"
        "            self.backbone.trie().set_ifindex(bind.ifindex, self.id as u32);\n"
        "        }\n"
        "        match bind.addr {\n",
    )
    patch(
        wan_link,
        "/// Wraps a PPPoE dataplane guard so its dispatch-trie session entry is\n",
        "/// `LANDSCAPE_WAN_INGRESS_IFX_FALLBACK=1` switches the WAN ingress dispatch\n"
        "/// from \"daddr must name a WAN address\" to \"fall back to the ingress\n"
        "/// interface's chain when it does not\". Required when a program in front of\n"
        "/// the intro (einat ingress DNAT) rewrote daddr to the LAN client.\n"
        "///\n"
        "/// Disabled by default: the fallback path forwards WAN->LAN with\n"
        "/// bpf_redirect, which does not traverse the kernel forward chain.\n"
        "fn ingress_ifx_fallback_enabled() -> bool {\n"
        "    static ENABLED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();\n"
        "    *ENABLED.get_or_init(|| {\n"
        "        std::env::var(\"LANDSCAPE_WAN_INGRESS_IFX_FALLBACK\")\n"
        "            .map(|v| matches!(v.trim().to_ascii_lowercase().as_str(), \"1\" | \"true\" | \"on\" | \"yes\"))\n"
        "            .unwrap_or(false)\n"
        "    })\n"
        "}\n\n"
        "/// Wraps a PPPoE dataplane guard so its dispatch-trie session entry is\n",
    )
    patch(
        wan_link,
        "        if let Some(bind) = inner.v4.take() {\n"
        "            maps::wan::del_ipv4_wan_ip(self.backbone.paths(), bind.ifindex);\n"
        "            self.unpublish_bind(&bind);\n"
        "        }\n"
        "        if let Some(bind) = inner.v6.take() {\n"
        "            maps::wan::del_ipv6_wan_ip(self.backbone.paths(), bind.ifindex);\n"
        "            self.unpublish_bind(&bind);\n"
        "        }\n",
        "        let mut bound_ifindex: Option<u32> = None;\n"
        "        if let Some(bind) = inner.v4.take() {\n"
        "            bound_ifindex = Some(bind.ifindex);\n"
        "            maps::wan::del_ipv4_wan_ip(self.backbone.paths(), bind.ifindex);\n"
        "            self.unpublish_bind(&bind);\n"
        "        }\n"
        "        if let Some(bind) = inner.v6.take() {\n"
        "            bound_ifindex = Some(bind.ifindex);\n"
        "            maps::wan::del_ipv6_wan_ip(self.backbone.paths(), bind.ifindex);\n"
        "            self.unpublish_bind(&bind);\n"
        "        }\n"
        "        if let Some(ifindex) = bound_ifindex {\n"
        "            self.backbone.trie().del_ifindex(ifindex);\n"
        "        }\n",
    )

    # 10) Egress NAT fast path (flow-offload style), opt-in via
    #     LANDSCAPE_NAT_FASTPATH=1.  First packet records the NAT decision in
    #     `fa4_fast`; later packets are matched in tc_wan_egress_route before
    #     the chain tail call and skip the chain.  Entries carry a hard 5s
    #     deadline (see nat_v4/nat_fast.h) so the CT stays alive.
    patch(
        "landscape-ebpf/src/bpf/nat_v4/tc_nat4_rodata.h",
        "const volatile __u16 land_nat_tcp_range_end = 0;\n",
        "const volatile __u16 land_nat_tcp_range_end = 0;\n"
        "const volatile __u8 land_nat_fastpath = 0;\n",
    )
    patch(
        "landscape-ebpf/src/bpf/tc_nat4_v4.bpf.c",
        '#include "reader/tc_nat_read4.h"\n#include "nat_v4/tc_nat4_packet.h"\n',
        '#include "reader/tc_nat_read4.h"\n'
        '#include "nat_v4/nat_fast.h"\n'
        '#define LAND_NAT_FAST 1\n'
        '#include "nat_v4/tc_nat4_packet.h"\n',
    )
    # Every CT teardown drops the matching fast-path entry; otherwise the table
    # only shrinks when a packet happens to hit an expired key, and ended flows
    # (especially the router's own short-lived DNS/UDP flows) leak entries.
    patch(
        "landscape-ebpf/src/bpf/nat_v4/nat4_ct_timer.h",
        "static __always_inline u32 napt_ct_delete(struct napt_ct_key *key) {\n"
        "    bpf_map_delete_elem(&napt_ct, key);\n"
        "    return NAPT_TIMER_STEP_DELETE_CT;\n"
        "}\n",
        "static __always_inline u32 napt_ct_delete(struct napt_ct_key *key) {\n"
        "    bpf_map_delete_elem(&napt_ct, key);\n"
        "#ifdef LAND_NAT_FAST\n"
        "    bpf_map_delete_elem(&fa4_fast, key);\n"
        "#endif\n"
        "    return NAPT_TIMER_STEP_DELETE_CT;\n"
        "}\n",
    )
    patch(
        "landscape-ebpf/src/bpf/tc_nat4_v4.bpf.c",
        "    if (rc == TC_ACT_SHOT) return TC_ACT_SHOT;\n"
        "    if (rc != TC_ACT_OK) goto chain;\n"
        "\n"
        "chain:\n",
        "    if (rc == TC_ACT_SHOT) return TC_ACT_SHOT;\n"
        "    if (rc != TC_ACT_OK) goto chain;\n"
        "\n"
        "    // Forwarded traffic only: the router's own flows (DNS/NTP/WebUI) never\n"
        "    // reach tc_wan_egress_route's forwarded branch, so recording them would\n"
        "    // only fill the table with entries nobody ever matches.\n"
        "    if (land_nat_fastpath && land_nat_event_driven_get() && skb->ingress_ifindex != 0 &&\n"
        "        view.l4_protocol != IPV4_PROTO_ICMP && !napt_is_icmp_error(&view) &&\n"
        "        !napt_is_non_initial(&view)) {\n"
        "        nat_fast_record(src_addr, view.sport, dst_addr, view.dport, view.l4_protocol,\n"
        "                        new_addr, new_port);\n"
        "    }\n"
        "\n"
        "chain:\n",
    )
    patch(
        "landscape-ebpf/src/bpf/tc_wan_egress_route.bpf.c",
        '#include "chain/tc_cb.h"\n',
        '#include "chain/tc_cb.h"\n'
        '#include "nat_v4/nat_fast.h"\n',
    )
    patch(
        "landscape-ebpf/src/bpf/tc_wan_egress_route.bpf.c",
        "    if (tc_cb_forwarded(skb)) {\n"
        "        u32 chain = tc_cb_chain(skb);\n"
        "        bpf_tail_call(skb, &tc_wan_egress_roots, chain);\n",
        "    if (tc_cb_forwarded(skb)) {\n"
        "        // Fast path: an established flow skips the whole egress chain.\n"
        "        if (nat_fast_try(skb, current_l3_offset)) return TC_ACT_OK;\n"
        "        u32 chain = tc_cb_chain(skb);\n"
        "        bpf_tail_call(skb, &tc_wan_egress_roots, chain);\n",
    )
    # Map plumbing: one pinned hash shared by the NAT stage and the egress route.
    patch(
        "landscape-ebpf/src/lib.rs",
        "    pub nat_metric_events: PathBuf,\n",
        "    pub nat_metric_events: PathBuf,\n"
        "    pub fa4_fast: PathBuf,\n",
    )
    patch(
        "landscape-ebpf/src/lib.rs",
        "            nat_metric_events: root.join(nat::NAT_METRIC_EVENTS_PIN),\n",
        "            nat_metric_events: root.join(nat::NAT_METRIC_EVENTS_PIN),\n"
        "            fa4_fast: root.join(nat::FA4_FAST_PIN),\n",
    )
    patch(
        "landscape-ebpf/src/maps/nat/init.rs",
        'pub(crate) const NAT_METRIC_EVENTS_PIN: &str = "nat_metric_events";\n',
        'pub(crate) const NAT_METRIC_EVENTS_PIN: &str = "nat_metric_events";\n'
        'pub(crate) const FA4_FAST_PIN: &str = "fa4_fast";\n',
    )
    patch(
        "landscape-ebpf/src/maps/nat/init.rs",
        "/// Create or reuse the pinned `nat_metric_events` ringbuf at `path`.\n"
        "pub fn init_nat_metric_events(path: &Path) -> LdEbpfResult<MapHandle> {\n"
        "    ensure_pinned_map(&NAT_METRIC_EVENTS_SPEC, path)\n"
        "}\n",
        "/// Create or reuse the pinned `nat_metric_events` ringbuf at `path`.\n"
        "pub fn init_nat_metric_events(path: &Path) -> LdEbpfResult<MapHandle> {\n"
        "    ensure_pinned_map(&NAT_METRIC_EVENTS_SPEC, path)\n"
        "}\n"
        "\n"
        "/// `fa4_fast`: egress NAT fast-path table (key 16, value 16).\n"
        "pub(crate) const FA4_FAST_SPEC: MapCreateSpec = MapCreateSpec {\n"
        "    map_type: MapType::Hash,\n"
        "    name: FA4_FAST_PIN,\n"
        "    key_size: 16,\n"
        "    value_size: 16,\n"
        "    max_entries: 32768,\n"
        "    map_flags: 0,\n"
        "    inner: None,\n"
        "};\n"
        "\n"
        "/// Create or reuse the pinned `fa4_fast` hash at `path`.\n"
        "pub fn init_fa4_fast(path: &Path) -> LdEbpfResult<MapHandle> {\n"
        "    ensure_pinned_map(&FA4_FAST_SPEC, path)\n"
        "}\n",
    )
    patch(
        "landscape-ebpf/src/maps/mod.rs",
        '    nat::init_nat_metric_events(&paths.nat_metric_events).expect("init nat_metric_events failed");\n',
        '    nat::init_nat_metric_events(&paths.nat_metric_events).expect("init nat_metric_events failed");\n'
        '    nat::init_fa4_fast(&paths.fa4_fast).expect("init fa4_fast failed");\n',
    )
    patch(
        "landscape-ebpf/src/maps/mod.rs",
        '    nat::init_nat_metric_events(&paths.nat_metric_events).expect("test init nat_metric_events");\n',
        '    nat::init_nat_metric_events(&paths.nat_metric_events).expect("test init nat_metric_events");\n'
        '    nat::init_fa4_fast(&paths.fa4_fast).expect("test init fa4_fast");\n',
    )
    patch(
        "landscape-ebpf/src/stages/nat4.rs",
        "        rodata.land_nat_tcp_range_end = config.tcp_range.end;\n",
        "        rodata.land_nat_tcp_range_end = config.tcp_range.end;\n"
        "        let fastpath = std::env::var(\"LANDSCAPE_NAT_FASTPATH\")\n"
        "            .map(|v| {\n"
        "                matches!(v.trim().to_ascii_lowercase().as_str(), \"1\" | \"true\" | \"on\" | \"yes\")\n"
        "            })\n"
        "            .unwrap_or(false);\n"
        "        rodata.land_nat_fastpath = if fastpath { 1 } else { 0 };\n",
    )
    patch(
        "landscape-ebpf/src/stages/nat4.rs",
        "    pin_and_reuse_map(&mut open.maps.nat_metric_events, &paths.nat_metric_events)?;\n",
        "    pin_and_reuse_map(&mut open.maps.nat_metric_events, &paths.nat_metric_events)?;\n"
        "    pin_and_reuse_map(&mut open.maps.fa4_fast, &paths.fa4_fast)?;\n",
    )

    # 11) Merge the MSS stage into the NAT programs (LANDSCAPE_NAT_MERGE_MSS=1).
    #     Saves one program + one tail call + one parse per direction; the MTU
    #     travels through the pinned `land_mss_cfg` (ifindex -> mtu) because the
    #     MSS stage is attached by the link service and NAT is attached later.
    patch(
        "landscape-ebpf/src/bpf/nat_v4/tc_nat4_rodata.h",
        "const volatile __u8 land_nat_fastpath = 0;\n",
        "const volatile __u8 land_nat_fastpath = 0;\n"
        "const volatile __u8 land_nat_merge_mss = 0;\n",
    )
    patch(
        "landscape-ebpf/src/bpf/tc_nat4_v4.bpf.c",
        "#define LAND_NAT_FAST 1\n",
        "#define LAND_NAT_FAST 1\n"
        '#include "nat_v4/merge_mss.h"\n',
    )
    patch(
        "landscape-ebpf/src/bpf/tc_nat4_v4.bpf.c",
        "    if (!tc_nat_read4(skb, &view, &src_addr, &dst_addr, current_l3_offset, LAND_FRAG_FROM_LAN))\n"
        "        goto chain;\n",
        "    if (!tc_nat_read4(skb, &view, &src_addr, &dst_addr, current_l3_offset, LAND_FRAG_FROM_LAN))\n"
        "        goto chain;\n"
        "\n"
        "    if (land_nat_merge_mss) nat_merge_clamp_mss(skb, current_l3_offset);\n",
    )
    patch(
        "landscape-ebpf/src/bpf/tc_nat4_v4.bpf.c",
        "    if (!tc_nat_read4(skb, &view, &src_addr, &dst_addr, current_l3_offset, LAND_FRAG_FROM_WAN))\n"
        "        goto chain;\n",
        "    if (!tc_nat_read4(skb, &view, &src_addr, &dst_addr, current_l3_offset, LAND_FRAG_FROM_WAN))\n"
        "        goto chain;\n"
        "\n"
        "    if (land_nat_merge_mss) nat_merge_clamp_mss(skb, current_l3_offset);\n",
    )
    patch(
        "landscape-ebpf/src/lib.rs",
        "    pub fa4_fast: PathBuf,\n",
        "    pub fa4_fast: PathBuf,\n"
        "    pub land_mss_cfg: PathBuf,\n",
    )
    patch(
        "landscape-ebpf/src/lib.rs",
        "            fa4_fast: root.join(nat::FA4_FAST_PIN),\n",
        "            fa4_fast: root.join(nat::FA4_FAST_PIN),\n"
        "            land_mss_cfg: root.join(nat::LAND_MSS_CFG_PIN),\n",
    )
    patch(
        "landscape-ebpf/src/maps/nat/init.rs",
        'pub(crate) const FA4_FAST_PIN: &str = "fa4_fast";\n',
        'pub(crate) const FA4_FAST_PIN: &str = "fa4_fast";\n'
        'pub(crate) const LAND_MSS_CFG_PIN: &str = "land_mss_cfg";\n',
    )
    patch(
        "landscape-ebpf/src/maps/nat/init.rs",
        "/// Create or reuse the pinned `fa4_fast` hash at `path`.\n"
        "pub fn init_fa4_fast(path: &Path) -> LdEbpfResult<MapHandle> {\n"
        "    ensure_pinned_map(&FA4_FAST_SPEC, path)\n"
        "}\n",
        "/// Create or reuse the pinned `fa4_fast` hash at `path`.\n"
        "pub fn init_fa4_fast(path: &Path) -> LdEbpfResult<MapHandle> {\n"
        "    ensure_pinned_map(&FA4_FAST_SPEC, path)\n"
        "}\n"
        "\n"
        "/// `land_mss_cfg`: ifindex -> MTU for the merged MSS clamp.\n"
        "pub(crate) const LAND_MSS_CFG_SPEC: MapCreateSpec = MapCreateSpec {\n"
        "    map_type: MapType::Hash,\n"
        "    name: LAND_MSS_CFG_PIN,\n"
        "    key_size: 4,\n"
        "    value_size: 2,\n"
        "    max_entries: 256,\n"
        "    map_flags: 0,\n"
        "    inner: None,\n"
        "};\n"
        "\n"
        "/// Create or reuse the pinned `land_mss_cfg` hash at `path`.\n"
        "pub fn init_land_mss_cfg(path: &Path) -> LdEbpfResult<MapHandle> {\n"
        "    ensure_pinned_map(&LAND_MSS_CFG_SPEC, path)\n"
        "}\n",
    )
    patch(
        "landscape-ebpf/src/maps/mod.rs",
        '    nat::init_fa4_fast(&paths.fa4_fast).expect("init fa4_fast failed");\n',
        '    nat::init_fa4_fast(&paths.fa4_fast).expect("init fa4_fast failed");\n'
        '    nat::init_land_mss_cfg(&paths.land_mss_cfg).expect("init land_mss_cfg failed");\n',
    )
    patch(
        "landscape-ebpf/src/maps/mod.rs",
        '    nat::init_fa4_fast(&paths.fa4_fast).expect("test init fa4_fast");\n',
        '    nat::init_fa4_fast(&paths.fa4_fast).expect("test init fa4_fast");\n'
        '    nat::init_land_mss_cfg(&paths.land_mss_cfg).expect("test init land_mss_cfg");\n',
    )
    patch(
        "landscape-ebpf/src/maps/nat/mod.rs",
        "pub use init::*;\n",
        "pub use init::*;\n"
        "\n"
        "/// Merged MSS mode: NAT clamps MSS itself and the standalone stage is not\n"
        "/// attached (`LANDSCAPE_NAT_MERGE_MSS=1`).\n"
        "pub(crate) fn merge_mss_enabled() -> bool {\n"
        "    std::env::var(\"LANDSCAPE_NAT_MERGE_MSS\")\n"
        "        .map(|v| {\n"
        "            matches!(v.trim().to_ascii_lowercase().as_str(), \"1\" | \"true\" | \"on\" | \"yes\")\n"
        "        })\n"
        "        .unwrap_or(false)\n"
        "}\n"
        "\n"
        "/// Publish the link MTU so the NAT programs can clamp MSS.\n"
        "pub(crate) fn publish_mss_cfg(path: &std::path::Path, ifindex: u32, mtu: u16) {\n"
        "    let map = match libbpf_rs::MapHandle::from_pinned_path(path) {\n"
        "        Ok(map) => map,\n"
        "        Err(err) => {\n"
        "            tracing::warn!(%err, path = %path.display(), \"land_mss_cfg open failed\");\n"
        "            return;\n"
        "        }\n"
        "    };\n"
        "    if let Err(err) = map.update(\n"
        "        &ifindex.to_ne_bytes(),\n"
        "        &mtu.to_ne_bytes(),\n"
        "        libbpf_rs::MapFlags::ANY,\n"
        "    ) {\n"
        "        tracing::warn!(%err, ifindex, mtu, \"land_mss_cfg update failed\");\n"
        "    }\n"
        "}\n",
    )
    patch(
        "landscape-ebpf/src/stages/nat4.rs",
        "        rodata.land_nat_fastpath = if fastpath { 1 } else { 0 };\n",
        "        rodata.land_nat_fastpath = if fastpath { 1 } else { 0 };\n"
        "        let merge_mss = std::env::var(\"LANDSCAPE_NAT_MERGE_MSS\")\n"
        "            .map(|v| {\n"
        "                matches!(v.trim().to_ascii_lowercase().as_str(), \"1\" | \"true\" | \"on\" | \"yes\")\n"
        "            })\n"
        "            .unwrap_or(false);\n"
        "        rodata.land_nat_merge_mss = if merge_mss { 1 } else { 0 };\n",
    )
    patch(
        "landscape-ebpf/src/stages/nat4.rs",
        "    pin_and_reuse_map(&mut open.maps.fa4_fast, &paths.fa4_fast)?;\n",
        "    pin_and_reuse_map(&mut open.maps.fa4_fast, &paths.fa4_fast)?;\n"
        "    pin_and_reuse_map(&mut open.maps.land_mss_cfg, &paths.land_mss_cfg)?;\n",
    )
    patch(
        "landscape-ebpf/src/stages/mss.rs",
        "pub fn init_mss(\n"
        "    dp: &Arc<WanLinkDataplane>,\n"
        "    mtu: u16,\n"
        "    has_eth_header: bool,\n"
        ") -> LdEbpfResult<MssHandle> {\n"
        "    Ok(MssHandle {\n"
        "        tc: Some(attach_tc_mss(dp, mtu, has_eth_header)?),\n"
        "        xdp: Some(init_xdp_mss(dp, mtu, has_eth_header)?),\n"
        "    })\n"
        "}\n",
        "pub fn init_mss(\n"
        "    dp: &Arc<WanLinkDataplane>,\n"
        "    ifindex: u32,\n"
        "    mtu: u16,\n"
        "    has_eth_header: bool,\n"
        ") -> LdEbpfResult<MssHandle> {\n"
        "    // Merged mode: the NAT stage clamps MSS itself, so publish the MTU\n"
        "    // and skip both the TC and XDP MSS stages.\n"
        "    if crate::maps::nat::merge_mss_enabled() {\n"
        "        crate::maps::nat::publish_mss_cfg(&dp.backbone().paths().land_mss_cfg, ifindex, mtu);\n"
        "        return Ok(MssHandle { tc: None, xdp: None });\n"
        "    }\n"
        "    Ok(MssHandle {\n"
        "        tc: Some(attach_tc_mss(dp, mtu, has_eth_header)?),\n"
        "        xdp: Some(init_xdp_mss(dp, mtu, has_eth_header)?),\n"
        "    })\n"
        "}\n",
    )
    patch(
        "landscape-ebpf/src/chain/wan_link/mod.rs",
        "        crate::stages::mss::init_mss(self, mtu, has_eth_header)\n",
        "        crate::stages::mss::init_mss(self, ifindex, mtu, has_eth_header)\n",
    )
    patch(
        "landscape-ebpf/src/bin/mss_clamp.rs",
        "    let mss_clamp = landscape_ebpf::stages::mss::init_mss(&dp, 1492, true).unwrap();\n",
        "    let mss_clamp = landscape_ebpf::stages::mss::init_mss(&dp, ifindex, 1492, true).unwrap();\n",
    )
    patch(
        "landscape-ebpf/src/chain/xdp_wan_route.rs",
        "    crate::bpf_ctx!(\n"
        "        pin_and_reuse_map(&mut egress_route_open_skel.maps.flow_match_map, &paths.flow_match_map),\n"
        "        \"tc_wan_egress_route pin flow_match_map\"\n"
        "    )?;\n",
        "    crate::bpf_ctx!(\n"
        "        pin_and_reuse_map(&mut egress_route_open_skel.maps.fa4_fast, &paths.fa4_fast),\n"
        "        \"tc_wan_egress_route pin fa4_fast\"\n"
        "    )?;\n"
        "    crate::bpf_ctx!(\n"
        "        pin_and_reuse_map(&mut egress_route_open_skel.maps.flow_match_map, &paths.flow_match_map),\n"
        "        \"tc_wan_egress_route pin flow_match_map\"\n"
        "    )?;\n",
    )
    patch(
        "landscape-ebpf/src/chain/tc_wan_route.rs",
        "    crate::bpf_ctx!(\n"
        "        pin_and_reuse_map(&mut open_skel.maps.flow_match_map, &paths.flow_match_map),\n"
        "        \"tc_wan_egress pin flow_match_map\"\n"
        "    )?;\n",
        "    crate::bpf_ctx!(\n"
        "        pin_and_reuse_map(&mut open_skel.maps.fa4_fast, &paths.fa4_fast),\n"
        "        \"tc_wan_egress pin fa4_fast\"\n"
        "    )?;\n"
        "    crate::bpf_ctx!(\n"
        "        pin_and_reuse_map(&mut open_skel.maps.flow_match_map, &paths.flow_match_map),\n"
        "        \"tc_wan_egress pin flow_match_map\"\n"
        "    )?;\n",
    )
    print(f"TCX overlay applied (overlay={OVERLAY})")


if __name__ == "__main__":
    main()
