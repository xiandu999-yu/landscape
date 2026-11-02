/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_FLOW_CORE_H
#define LAND_FLOW_CORE_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "base/ipv4.h"

/*
 * Core flow record (WAN <-> LAN fast path).
 *
 * One table is the single source of truth: the stages that own a piece of the
 * decision write it into the record, and the fast path only uses records whose
 * state is COMPLETE and whose ver still matches the current generation. An
 * incomplete / stale record means "fall back to the normal slow path".
 *
 *   dir = FLOW_CORE_DIR_LAN2WAN : key uses the pre-NAT LAN tuple,
 *                                 value carries the NAT result;
 *   dir = FLOW_CORE_DIR_WAN2LAN : key uses the WAN-side tuple,
 *                                 value carries the LAN destination.
 *
 * This header defines no maps (it is included by objects that must not create
 * private copies); the table lives in flow_core_tbl.h.
 */

#define FLOW_CORE_MAGIC 0xFAC1u

#define FLOW_CORE_DIR_LAN2WAN 0
#define FLOW_CORE_DIR_WAN2LAN 1

#define FLOW_CORE_STATE_EMPTY 0
#define FLOW_CORE_STATE_COMPLETE 1

struct flow_core_key {
    __be32 saddr;
    __be32 daddr;
    __be16 sport;
    __be16 dport;
    __u8 proto;
    __u8 dir;
    __u8 pad[2];
};

struct flow_core_value {
    __be32 internal_addr; /* 0  */
    __u32 out_ifindex;    /* 4  */
    __u64 deadline_ns;    /* 8  */
    __be16 internal_port; /* 16 */
    __u16 pad;            /* 18 */
    __u32 ver;            /* 20 */
    __u8 dst_mac[6];      /* 24 */
    __u8 src_mac[6];      /* 30 */
    __u8 state;           /* 36 */
    __u8 pad2[3];         /* 37 */
};

_Static_assert(sizeof(struct flow_core_key) == 16, "flow_core_key must be 16");
_Static_assert(sizeof(struct flow_core_value) == 40, "flow_core_value must be 40");

/* NAT stage: hand the pre-NAT tuple to the route stage. */
static __always_inline void flow_core_stash(struct __sk_buff *skb, __be16 sport,
                                            __be16 dport, __be32 wan_addr, __u8 proto) {
    skb->cb[2] = ((__u32)sport << 16) | (__u32)dport;
    skb->cb[3] = FLOW_CORE_MAGIC | ((__u32)proto << 16);
    skb->cb[4] = wan_addr;
}

static __always_inline int flow_core_take(struct __sk_buff *skb, __be16 *sport, __be16 *dport,
                                          __be32 *wan_addr, __u8 *proto) {
    if ((skb->cb[3] & 0xFFFFu) != FLOW_CORE_MAGIC) return 0;
    *sport = (__be16)(skb->cb[2] >> 16);
    *dport = (__be16)(skb->cb[2] & 0xFFFFu);
    *wan_addr = skb->cb[4];
    *proto = (__u8)(skb->cb[3] >> 16);
    return 1;
}

static __always_inline __u64 flow_core_ttl(__u8 proto, __be16 dport) {
    if (proto == IPV4_PROTO_TCP) return 15ULL * 1000000000ULL;
    if (dport == bpf_htons(443)) return 10ULL * 1000000000ULL; /* QUIC */
    return 5ULL * 1000000000ULL;
}

/* Destination port as it is *now* (post-NAT): the internal port. */
static __always_inline __be16 flow_core_dport(struct __sk_buff *skb, __u32 l3_off) {
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

#endif /* LAND_FLOW_CORE_H */
