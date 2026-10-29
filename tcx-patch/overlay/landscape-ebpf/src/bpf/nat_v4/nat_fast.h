/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_NAT_FAST_H
#define LAND_NAT_FAST_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "nat_v4/tc_modify_head.h"

/*
 * Egress NAT fast path (flow-offload style, opt-in via LANDSCAPE_NAT_FASTPATH).
 *
 * The first packet of a flow walks the whole WAN egress chain
 * (route -> mss -> firewall -> nat -> exit).  tc_v4_from_lan records the NAT
 * decision here; later packets are matched in tc_wan_egress_route *before* the
 * chain tail call and skip the chain entirely.
 *
 * Deadlines, not timers:
 *   - an entry lives for NAT_FAST_TTL_NS (5s) from the moment it was recorded
 *     and the deadline is NOT refreshed on hits;
 *   - so at least one packet per flow per 5s takes the slow path, which runs
 *     napt_ct_advance() and re-arms the conntrack timer, and re-records the
 *     entry;
 *   - that is what keeps the CT (and therefore the port mapping) alive without
 *     the egress object having to touch napt_ct, and it also bounds how long a
 *     stale decision can survive a route/MAC/port change.
 *
 * Only recorded in event-driven NAT mode: periodic mode needs
 * napt_ct_advance() on every packet to move ACTIVE->IDLE->RELEASE, which the
 * fast path deliberately skips.
 */

#define NAT_FAST_TTL_NS (30ULL * 1000000000ULL)

struct nat_fast_key {
    __be32 saddr;
    __be32 daddr;
    __be16 sport;
    __be16 dport;
    __u8 proto;
    __u8 pad[3];
};

struct nat_fast_value {
    __be32 external_addr;
    __be16 external_port;
    __u16 pad;
    __u64 deadline_ns;
};

_Static_assert(sizeof(struct nat_fast_key) == 16, "nat_fast_key must be 16 bytes");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 32768);
    __type(key, struct nat_fast_key);
    __type(value, struct nat_fast_value);
} fa4_fast SEC(".maps");

static __always_inline void nat_fast_record(__be32 saddr, __be16 sport, __be32 daddr,
                                            __be16 dport, __u8 proto, __be32 external_addr,
                                            __be16 external_port) {
    struct nat_fast_key key = {};
    key.saddr = saddr;
    key.daddr = daddr;
    key.sport = sport;
    key.dport = dport;
    key.proto = proto;

    struct nat_fast_value value = {};
    value.external_addr = external_addr;
    value.external_port = external_port;
    value.deadline_ns = bpf_ktime_get_ns() + NAT_FAST_TTL_NS;

    bpf_map_update_elem(&fa4_fast, &key, &value, BPF_ANY);
}

/*
 * Returns 1 when the packet was rewritten and can be transmitted as-is,
 * 0 when it must take the regular chain.
 */
static __always_inline int nat_fast_try(struct __sk_buff *skb, __u32 l3_off) {
    char *data = (char *)(long)skb->data;
    char *data_end = (char *)(long)skb->data_end;
    struct iphdr *iph = (void *)(data + l3_off);
    if ((char *)(iph + 1) > data_end) return 0;
    if (iph->version != 4) return 0;

    __u32 ihl = ((__u32)iph->ihl) * 4U;
    if (ihl < IPV4_HDR_MIN_LEN || ihl > IPV4_HDR_MAX_LEN) return 0;
    if (iph->frag_off & bpf_htons(0x3FFF)) return 0;

    __u8 proto = iph->protocol;
    if (proto != IPV4_PROTO_TCP && proto != IPV4_PROTO_UDP) return 0;

    __u32 l4_off = l3_off + ihl;
    __be16 sport;
    __be16 dport;

    if (proto == IPV4_PROTO_TCP) {
        struct tcphdr *th = (void *)(data + l4_off);
        if ((char *)(th + 1) > data_end) return 0;
        /* SYN/FIN/RST always walk the chain: the SYN needs MSS clamping and
         * the flags drive the conntrack state machine. */
        if (th->syn || th->fin || th->rst) return 0;
        sport = th->source;
        dport = th->dest;
    } else {
        struct udphdr *uh = (void *)(data + l4_off);
        if ((char *)(uh + 1) > data_end) return 0;
        sport = uh->source;
        dport = uh->dest;
    }

    struct nat_fast_key key = {};
    key.saddr = iph->saddr;
    key.daddr = iph->daddr;
    key.sport = sport;
    key.dport = dport;
    key.proto = proto;

    struct nat_fast_value *value = bpf_map_lookup_elem(&fa4_fast, &key);
    if (!value) return 0;
    if (bpf_ktime_get_ns() > value->deadline_ns) {
        bpf_map_delete_elem(&fa4_fast, &key);
        return 0;
    }

    __be32 new_addr = value->external_addr;
    __be16 new_port = value->external_port;

    if (napt_modify(skb, new_addr, new_port, LAND_FRAG_FROM_LAN, l3_off) != TC_ACT_OK) return 0;
    return 1;
}

#endif /* LAND_NAT_FAST_H */
