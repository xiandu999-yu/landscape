/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_FLOW_CORE_TBL_H
#define LAND_FLOW_CORE_TBL_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "nat_v4/flow_core.h"
#include "nat_v4/flow_ver.h"
#include "nat_v4/tc_modify_head.h"

/*
 * The core table. Only the objects that read or write it include this header
 * (ingress route + ingress root, and the NAT stage for teardown), so only they
 * define the map; all of them pin it, so one instance is shared.
 */

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 32768);
    __type(key, struct flow_core_key);
    __type(value, struct flow_core_value);
} flow_core SEC(".maps");

/* [0]=generation [1]=recorded [2]=hit [3]=expired [4]=tries [5]=parsed
 * [6]=miss [7]=last exit reason */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 8);
    __type(key, __u32);
    __type(value, __u64);
} nat_wan_ctl SEC(".maps");

static __always_inline void flow_core_ctl_add(__u32 idx) {
    __u64 *slot = bpf_map_lookup_elem(&nat_wan_ctl, &idx);
    if (slot) __sync_fetch_and_add(slot, 1);
}

static __always_inline int flow_core_ctl_set(__u32 idx, __u64 val) {
    __u64 *slot = bpf_map_lookup_elem(&nat_wan_ctl, &idx);
    if (slot) *slot = val;
    return TC_ACT_OK;
}

/* WAN2LAN record: written by the route stage once it knows the destination. */
static __always_inline void flow_core_record_wan(__be32 remote_addr, __be16 remote_port,
                                                 __be32 wan_addr, __be16 external_port,
                                                 __u8 proto, __be32 internal_addr,
                                                 __be16 internal_port, __u32 out_ifindex,
                                                 const __u8 *dst_mac, const __u8 *src_mac) {
    struct flow_core_key key = {};
    key.saddr = remote_addr;
    key.daddr = wan_addr;
    key.sport = remote_port;
    key.dport = external_port;
    key.proto = proto;
    key.dir = FLOW_CORE_DIR_WAN2LAN;

    struct flow_core_value value = {};
    value.internal_addr = internal_addr;
    value.internal_port = internal_port;
    value.out_ifindex = out_ifindex;
    __builtin_memcpy(value.dst_mac, dst_mac, 6);
    __builtin_memcpy(value.src_mac, src_mac, 6);
    value.deadline_ns = bpf_ktime_get_ns() + flow_core_ttl(proto, external_port);
    value.ver = (__u32)flow_core_ver_get(out_ifindex);
    value.state = FLOW_CORE_STATE_COMPLETE;

    bpf_map_update_elem(&flow_core, &key, &value, BPF_ANY);
    flow_core_ctl_add(1);
}

#define FLOW_CORE_EXIT_HDR 1
#define FLOW_CORE_EXIT_VER 2
#define FLOW_CORE_EXIT_IHL 3
#define FLOW_CORE_EXIT_FRAG 4
#define FLOW_CORE_EXIT_PROTO 5
#define FLOW_CORE_EXIT_L4 6
#define FLOW_CORE_EXIT_PULL 7
#define FLOW_CORE_EXIT_MAC 8


/* ------------------------------------------------------------------ *
 * Mapping check: the record must still agree with the external-port
 * mapping. `napt_from_wan` is an ARRAY indexed by proto base + port, so
 * the check is a single array lookup (~40ns) and needs no hash.
 * ------------------------------------------------------------------ */
#define LAND_NAPT_FROM_WAN_ENTRIES (3U * 65536U)
#define LAND_NAPT_FROM_WAN_UDP_BASE 0U
#define LAND_NAPT_FROM_WAN_TCP_BASE 65536U

/* Layout must match struct napt_from_wan_value (32 bytes). */
struct flow_wan_map_value {
    __be32 internal_addr;
    __be16 internal_port;
    __u8 kind;
    __u8 allow_reuse;
    __be32 trigger_addr;
    __be16 trigger_port;
    __u16 pad0;
    __u32 generation;
    __u32 pad1;
    __u64 state_ref;
};

_Static_assert(sizeof(struct flow_wan_map_value) == 32, "flow_wan_map_value must be 32");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, LAND_NAPT_FROM_WAN_ENTRIES);
    __type(key, __u32);
    __type(value, struct flow_wan_map_value);
} napt_from_wan SEC(".maps");

static __always_inline int flow_wan_mapping_matches(__u8 proto, __be16 ext_port,
                                                    __be32 internal_addr,
                                                    __be16 internal_port) {
    __u32 base;
    if (proto == IPV4_PROTO_UDP)
        base = LAND_NAPT_FROM_WAN_UDP_BASE;
    else if (proto == IPV4_PROTO_TCP)
        base = LAND_NAPT_FROM_WAN_TCP_BASE;
    else
        return 1; /* ICMP and friends are not fast-pathed anyway */

    __u32 idx = base + (__u32)bpf_ntohs(ext_port);
    struct flow_wan_map_value *m = bpf_map_lookup_elem(&napt_from_wan, &idx);
    if (!m) return 0;
    if (m->kind == 0) return 0; /* NAPT_MAPPING_FREE */
    return m->internal_addr == internal_addr && m->internal_port == internal_port;
}

/* Returns TC_ACT_REDIRECT when served from the core table, TC_ACT_OK otherwise. */
static __always_inline int flow_core_try_wan(struct __sk_buff *skb, __u32 l3_off) {
    flow_core_ctl_add(4);

    struct iphdr iph;
    if (bpf_skb_load_bytes(skb, l3_off, &iph, sizeof(iph)))
        return flow_core_ctl_set(7, FLOW_CORE_EXIT_HDR);
    if (iph.version != 4) return flow_core_ctl_set(7, FLOW_CORE_EXIT_VER);

    __u32 ihl = ((__u32)iph.ihl) * 4U;
    if (ihl < IPV4_HDR_MIN_LEN || ihl > IPV4_HDR_MAX_LEN)
        return flow_core_ctl_set(7, FLOW_CORE_EXIT_IHL);
    if (iph.frag_off & bpf_htons(0x3FFF)) return flow_core_ctl_set(7, FLOW_CORE_EXIT_FRAG);

    __u8 proto = iph.protocol;
    if (proto != IPV4_PROTO_TCP && proto != IPV4_PROTO_UDP)
        return flow_core_ctl_set(7, FLOW_CORE_EXIT_PROTO);

    __be16 ports[2] = {};
    if (bpf_skb_load_bytes(skb, l3_off + ihl, ports, sizeof(ports)))
        return flow_core_ctl_set(7, FLOW_CORE_EXIT_L4);
    __be16 sport = ports[0];
    __be16 dport = ports[1];

    flow_core_ctl_add(5);

    struct flow_core_key key = {};
    key.saddr = iph.saddr;
    key.daddr = iph.daddr;
    key.sport = sport;
    key.dport = dport;
    key.proto = proto;
    key.dir = FLOW_CORE_DIR_WAN2LAN;

    struct flow_core_value *value = bpf_map_lookup_elem(&flow_core, &key);
    if (!value) {
        flow_core_ctl_add(6);
        return TC_ACT_OK;
    }
    /* Only a complete, current record may skip the chain. */
    if (value->state != FLOW_CORE_STATE_COMPLETE ||
        value->ver != (__u32)flow_core_ver_get(value->out_ifindex)) {
        bpf_map_delete_elem(&flow_core, &key);
        return TC_ACT_OK;
    }
    if (bpf_ktime_get_ns() > value->deadline_ns) {
        bpf_map_delete_elem(&flow_core, &key);
        flow_core_ctl_add(3);
        return TC_ACT_OK;
    }

    /* The mapping must still point at the same host (guards the case where a
     * CT disappeared without going through napt_ct_delete). */
    if (!flow_wan_mapping_matches(proto, dport, value->internal_addr, value->internal_port)) {
        bpf_map_delete_elem(&flow_core, &key);
        return flow_core_ctl_set(7, 9);
    }

    /* napt_modify() rewrites through direct packet access, so the bytes must be
     * linear first. */
    __u32 need = l3_off + ihl + 20;
    if (bpf_skb_pull_data(skb, need)) return flow_core_ctl_set(7, FLOW_CORE_EXIT_PULL);
    if (napt_modify(skb, value->internal_addr, value->internal_port, LAND_FRAG_FROM_WAN, l3_off) !=
        TC_ACT_OK)
        return flow_core_ctl_set(7, FLOW_CORE_EXIT_PULL);
    if (bpf_skb_store_bytes(skb, 0, value->dst_mac, 6, 0))
        return flow_core_ctl_set(7, FLOW_CORE_EXIT_MAC);
    if (bpf_skb_store_bytes(skb, 6, value->src_mac, 6, 0))
        return flow_core_ctl_set(7, FLOW_CORE_EXIT_MAC);

    flow_core_ctl_add(2);
    int ret = bpf_redirect(value->out_ifindex, 0);
    return ret == TC_ACT_REDIRECT ? TC_ACT_REDIRECT : TC_ACT_OK;
}

#endif /* LAND_FLOW_CORE_TBL_H */
