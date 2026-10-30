/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_NAT_FAST_WAN_TBL_H
#define LAND_NAT_FAST_WAN_TBL_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "nat_v4/nat_fast_wan.h"
#include "nat_v4/tc_modify_head.h"

/*
 * Ingress fast-path table. Only the two objects that read/write it include
 * this header (tc_wan_ingress_route + tc_wan_ingress_root, guarded by
 * LAND_NAT_FAST_WAN), so only they define the maps; both pin them, so there is
 * exactly one instance shared with userspace.
 */

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 32768);
    __type(key, struct nat_fast_wan_key);
    __type(value, struct nat_fast_wan_value);
} fa4_fast_wan SEC(".maps");

/* [0]=generation [1]=recorded [2]=hit [3]=expired */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 8);
    __type(key, __u32);
    __type(value, __u64);
} nat_wan_ctl SEC(".maps");

static __always_inline void nat_wan_ctl_add(__u32 idx) {
    __u64 *slot = bpf_map_lookup_elem(&nat_wan_ctl, &idx);
    if (slot) __sync_fetch_and_add(slot, 1);
}

/* Stores the last early-exit reason (slot 7) and returns the program action. */
static __always_inline int nat_wan_ctl_set(__u32 idx, __u64 val) {
    __u64 *slot = bpf_map_lookup_elem(&nat_wan_ctl, &idx);
    if (slot) *slot = val;
    return TC_ACT_OK;
}

static __always_inline __u64 nat_wan_gen(void) {
    __u32 k = 0;
    __u64 *slot = bpf_map_lookup_elem(&nat_wan_ctl, &k);
    return slot ? *slot : 0;
}

static __always_inline void nat_fast_wan_record(__be32 remote_addr, __be16 remote_port,
                                                __be32 wan_addr, __be16 external_port,
                                                __u8 proto, __be32 internal_addr,
                                                __be16 internal_port, __u32 out_ifindex,
                                                const __u8 *dst_mac, const __u8 *src_mac) {
    struct nat_fast_wan_key key = {};
    key.remote_addr = remote_addr;
    key.wan_addr = wan_addr;
    key.remote_port = remote_port;
    key.external_port = external_port;
    key.proto = proto;

    struct nat_fast_wan_value value = {};
    value.internal_addr = internal_addr;
    value.internal_port = internal_port;
    value.out_ifindex = out_ifindex;
    __builtin_memcpy(value.dst_mac, dst_mac, 6);
    __builtin_memcpy(value.src_mac, src_mac, 6);
    value.deadline_ns = bpf_ktime_get_ns() + nat_fast_wan_ttl(proto, external_port);
    value.gen = (__u32)nat_wan_gen();

    bpf_map_update_elem(&fa4_fast_wan, &key, &value, BPF_ANY);
    nat_wan_ctl_add(1);
}

/* Returns TC_ACT_REDIRECT when served from the table, TC_ACT_OK otherwise. */
#define NAT_WAN_EXIT_HDR 1
#define NAT_WAN_EXIT_VER 2
#define NAT_WAN_EXIT_IHL 3
#define NAT_WAN_EXIT_FRAG 4
#define NAT_WAN_EXIT_PROTO 5
#define NAT_WAN_EXIT_L4 6
#define NAT_WAN_EXIT_PULL 7
#define NAT_WAN_EXIT_MAC 8

static __always_inline int nat_fast_wan_try(struct __sk_buff *skb, __u32 l3_off) {
    nat_wan_ctl_add(4);

    /* Helper-based parse: skb->data may not be linear for every packet
     * (large GSO segments, non-linear receive), which made the direct
     * data_end checks reject everything with NAT_WAN_EXIT_L4. */
    struct iphdr iph;
    if (bpf_skb_load_bytes(skb, l3_off, &iph, sizeof(iph)))
        return nat_wan_ctl_set(7, NAT_WAN_EXIT_HDR);
    if (iph.version != 4) return nat_wan_ctl_set(7, NAT_WAN_EXIT_VER);

    __u32 ihl = ((__u32)iph.ihl) * 4U;
    if (ihl < IPV4_HDR_MIN_LEN || ihl > IPV4_HDR_MAX_LEN)
        return nat_wan_ctl_set(7, NAT_WAN_EXIT_IHL);
    if (iph.frag_off & bpf_htons(0x3FFF)) return nat_wan_ctl_set(7, NAT_WAN_EXIT_FRAG);

    __u8 proto = iph.protocol;
    if (proto != IPV4_PROTO_TCP && proto != IPV4_PROTO_UDP)
        return nat_wan_ctl_set(7, NAT_WAN_EXIT_PROTO);

    __be16 ports[2] = {};
    if (bpf_skb_load_bytes(skb, l3_off + ihl, ports, sizeof(ports)))
        return nat_wan_ctl_set(7, NAT_WAN_EXIT_L4);
    __be16 sport = ports[0];
    __be16 dport = ports[1];

    nat_wan_ctl_add(5);
    struct nat_fast_wan_key key = {};
    key.remote_addr = iph.saddr;
    key.wan_addr = iph.daddr;
    key.remote_port = sport;
    key.external_port = dport;
    key.proto = proto;

    struct nat_fast_wan_value *value = bpf_map_lookup_elem(&fa4_fast_wan, &key);
    if (!value) {
        nat_wan_ctl_add(6);
        return TC_ACT_OK;
    }
    if (value->gen != (__u32)nat_wan_gen()) {
        bpf_map_delete_elem(&fa4_fast_wan, &key);
        return TC_ACT_OK;
    }
    if (bpf_ktime_get_ns() > value->deadline_ns) {
        bpf_map_delete_elem(&fa4_fast_wan, &key);
        nat_wan_ctl_add(3);
        return TC_ACT_OK;
    }

    /* napt_modify() rewrites the header through direct packet access, which
     * needs the bytes in the linear area (a non-linear skb makes it return
     * non-OK and the hit was silently dropped). */
    __u32 need = l3_off + ihl + 20;
    if (bpf_skb_pull_data(skb, need)) return nat_wan_ctl_set(7, NAT_WAN_EXIT_PULL);
    if (napt_modify(skb, value->internal_addr, value->internal_port, LAND_FRAG_FROM_WAN, l3_off) !=
        TC_ACT_OK)
        return nat_wan_ctl_set(7, NAT_WAN_EXIT_PULL);
    if (bpf_skb_store_bytes(skb, 0, value->dst_mac, 6, 0))
        return nat_wan_ctl_set(7, NAT_WAN_EXIT_MAC);
    if (bpf_skb_store_bytes(skb, 6, value->src_mac, 6, 0))
        return nat_wan_ctl_set(7, NAT_WAN_EXIT_MAC);

    nat_wan_ctl_add(2);
    int ret = bpf_redirect(value->out_ifindex, 0);
    return ret == TC_ACT_REDIRECT ? TC_ACT_REDIRECT : TC_ACT_OK;
}

#endif /* LAND_NAT_FAST_WAN_TBL_H */
