/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_NAT_FAST_WAN_H
#define LAND_NAT_FAST_WAN_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "base/ipv4.h"
#include "nat_v4/tc_modify_head.h"

/*
 * WAN -> LAN fast path (LANDSCAPE_NAT_FASTPATH_INGRESS), the mirror of
 * nat_fast.h for the ingress direction.
 *
 * Ownership:
 *   - NAT (tc_v4_from_wan) stashes the pre-NAT tuple in skb->cb[2..4] because
 *     after translation the route stage can no longer see the WAN address or
 *     the external port;
 *   - route (tc_route4_lan_redirect_check_in_wan, the branch that already has
 *     the egress ifindex and the destination MAC) writes the entry;
 *   - NAT deletes the entry from napt_ct_delete() together with the CT;
 *   - the LAN/MAC owner bumps nat_wan_ctl[0] (generation) whenever ip_mac_v4
 *     or the LAN interface set changes; a hit whose gen differs is dropped.
 *
 * The fast path never looks at napt_ct: lifetime is a fixed per-protocol TTL,
 * and expiry forces one packet back through the slow path, which re-records.
 */

#define NAT_FAST_WAN_MAGIC 0xFAC1u

struct nat_fast_wan_key {
    __be32 remote_addr;
    __be32 wan_addr;
    __be16 remote_port;
    __be16 external_port;
    __u8 proto;
    __u8 pad[3];
};

struct nat_fast_wan_value {
    __be32 internal_addr; /* 0 */
    __u32 out_ifindex;    /* 4 */
    __u64 deadline_ns;    /* 8 (keeps the u64 8-aligned) */
    __u16 internal_port;  /* 16 */
    __u16 pad;            /* 18 */
    __u32 gen;            /* 20 */
    __u8 dst_mac[6];      /* 24 */
    __u8 src_mac[6];      /* 30 */
    __u32 pad2;           /* 36 */
};

_Static_assert(sizeof(struct nat_fast_wan_key) == 16, "nat_fast_wan_key must be 16");
_Static_assert(sizeof(struct nat_fast_wan_value) == 40, "nat_fast_wan_value must be 40");

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

static __always_inline __u64 nat_wan_gen(void) {
    __u32 k = 0;
    __u64 *slot = bpf_map_lookup_elem(&nat_wan_ctl, &k);
    return slot ? *slot : 0;
}

/* NAT stage: hand the pre-NAT tuple to the route stage. */
static __always_inline void nat_fast_wan_stash(struct __sk_buff *skb, __be16 remote_port,
                                               __be16 external_port, __be32 wan_addr,
                                               __u8 proto) {
    skb->cb[2] = ((__u32)remote_port << 16) | (__u32)external_port;
    skb->cb[3] = NAT_FAST_WAN_MAGIC | ((__u32)proto << 16);
    skb->cb[4] = wan_addr;
}

static __always_inline int nat_fast_wan_take(struct __sk_buff *skb, __be16 *remote_port,
                                             __be16 *external_port, __be32 *wan_addr,
                                             __u8 *proto) {
    if ((skb->cb[3] & 0xFFFFu) != NAT_FAST_WAN_MAGIC) return 0;
    *remote_port = (__be16)(skb->cb[2] >> 16);
    *external_port = (__be16)(skb->cb[2] & 0xFFFFu);
    *wan_addr = skb->cb[4];
    *proto = (__u8)(skb->cb[3] >> 16);
    return 1;
}

/* Destination port as it is *now* (post-NAT): the internal port. */
static __always_inline __be16 nat_fast_wan_dport(struct __sk_buff *skb, __u32 l3_off) {
    char *data = (char *)(long)skb->data;
    char *data_end = (char *)(long)skb->data_end;
    struct iphdr *iph = (void *)(data + l3_off);
    if ((char *)(iph + 1) > data_end) return 0;
    __u32 ihl = ((__u32)iph->ihl) * 4U;
    if (ihl < IPV4_HDR_MIN_LEN) return 0;
    if (iph->protocol == IPV4_PROTO_TCP) {
        struct tcphdr *th = (void *)((char *)iph + ihl);
        if ((char *)(th + 1) > data_end) return 0;
        return th->dest;
    }
    if (iph->protocol == IPV4_PROTO_UDP) {
        struct udphdr *uh = (void *)((char *)iph + ihl);
        if ((char *)(uh + 1) > data_end) return 0;
        return uh->dest;
    }
    return 0;
}

static __always_inline __u64 nat_fast_wan_ttl(__u8 proto, __be16 external_port) {
    if (proto == IPV4_PROTO_TCP) return 15ULL * 1000000000ULL;
    if (external_port == bpf_htons(443)) return 10ULL * 1000000000ULL; /* QUIC */
    return 5ULL * 1000000000ULL;
}

/* route stage: full entry (ifindex + MACs are only known here). */
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

/*
 * intro: returns TC_ACT_REDIRECT when the packet was served from the table,
 * TC_ACT_OK when it must take the regular chain.
 */
static __always_inline int nat_fast_wan_try(struct __sk_buff *skb, __u32 l3_off) {
    char *data = (char *)(long)skb->data;
    char *data_end = (char *)(long)skb->data_end;
    struct iphdr *iph = (void *)(data + l3_off);
    if ((char *)(iph + 1) > data_end) return TC_ACT_OK;
    if (iph->version != 4) return TC_ACT_OK;

    __u32 ihl = ((__u32)iph->ihl) * 4U;
    if (ihl < IPV4_HDR_MIN_LEN || ihl > IPV4_HDR_MAX_LEN) return TC_ACT_OK;
    if (iph->frag_off & bpf_htons(0x3FFF)) return TC_ACT_OK;

    __u8 proto = iph->protocol;
    if (proto != IPV4_PROTO_TCP && proto != IPV4_PROTO_UDP) return TC_ACT_OK;

    __u32 l4_off = l3_off + ihl;
    __be16 sport;
    __be16 dport;
    if (proto == IPV4_PROTO_TCP) {
        struct tcphdr *th = (void *)(data + l4_off);
        if ((char *)(th + 1) > data_end) return TC_ACT_OK;
        sport = th->source;
        dport = th->dest;
    } else {
        struct udphdr *uh = (void *)(data + l4_off);
        if ((char *)(uh + 1) > data_end) return TC_ACT_OK;
        sport = uh->source;
        dport = uh->dest;
    }

    struct nat_fast_wan_key key = {};
    key.remote_addr = iph->saddr;
    key.wan_addr = iph->daddr;
    key.remote_port = sport;
    key.external_port = dport;
    key.proto = proto;

    struct nat_fast_wan_value *value = bpf_map_lookup_elem(&fa4_fast_wan, &key);
    if (!value) return TC_ACT_OK;
    if (value->gen != (__u32)nat_wan_gen()) {
        bpf_map_delete_elem(&fa4_fast_wan, &key);
        return TC_ACT_OK;
    }
    if (bpf_ktime_get_ns() > value->deadline_ns) {
        bpf_map_delete_elem(&fa4_fast_wan, &key);
        nat_wan_ctl_add(3);
        return TC_ACT_OK;
    }

    /* Rewrite the destination address/port, then the L2 header and redirect. */
    if (napt_modify(skb, value->internal_addr, value->internal_port, LAND_FRAG_FROM_WAN, l3_off) !=
        TC_ACT_OK)
        return TC_ACT_OK;

    if (bpf_skb_store_bytes(skb, 0, value->dst_mac, 6, 0)) return TC_ACT_OK;
    if (bpf_skb_store_bytes(skb, 6, value->src_mac, 6, 0)) return TC_ACT_OK;

    nat_wan_ctl_add(2);
    int ret = bpf_redirect(value->out_ifindex, 0);
    return ret == TC_ACT_REDIRECT ? TC_ACT_REDIRECT : TC_ACT_OK;
}

#endif /* LAND_NAT_FAST_WAN_H */
