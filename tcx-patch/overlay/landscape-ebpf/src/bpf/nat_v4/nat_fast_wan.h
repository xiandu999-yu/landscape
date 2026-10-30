/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_NAT_FAST_WAN_H
#define LAND_NAT_FAST_WAN_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "base/ipv4.h"

/*
 * WAN -> LAN fast path, shared types only.
 *
 * This header deliberately defines NO maps: it is included by every object
 * that touches route4_path.h (NAT, ingress intro/root/route, egress route),
 * and a map definition here would make each of them create a private copy
 * instead of sharing the pinned one. The maps and the record/try helpers live
 * in nat_fast_wan_tbl.h, which is included only by the two objects that
 * actually use them (ingress route and ingress root), guarded by
 * LAND_NAT_FAST_WAN.
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
    __u64 deadline_ns;    /* 8 */
    __u16 internal_port;  /* 16 */
    __u16 pad;            /* 18 */
    __u32 gen;            /* 20 */
    __u8 dst_mac[6];      /* 24 */
    __u8 src_mac[6];      /* 30 */
    __u32 pad2;           /* 36 */
};

_Static_assert(sizeof(struct nat_fast_wan_key) == 16, "nat_fast_wan_key must be 16");
_Static_assert(sizeof(struct nat_fast_wan_value) == 40, "nat_fast_wan_value must be 40");

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

static __always_inline __u64 nat_fast_wan_ttl(__u8 proto, __be16 external_port) {
    if (proto == IPV4_PROTO_TCP) return 15ULL * 1000000000ULL;
    if (external_port == bpf_htons(443)) return 10ULL * 1000000000ULL; /* QUIC */
    return 5ULL * 1000000000ULL;
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

#endif /* LAND_NAT_FAST_WAN_H */
