/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_FLOW_CORE_TBL_H
#define LAND_FLOW_CORE_TBL_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "nat_v4/flow_core.h"
#include "nat_v4/flow_core_map.h"
#include "nat_v4/flow_ver.h"
#include "nat_v4/flow_wan_check.h"
#include "nat_v4/tc_modify_head.h"
#include "nat_v4/qos.h"

/*
 * Record/consume the WAN2LAN core record.
 *
 * Include this only from objects that own all four maps (ingress route and
 * ingress root). The NAT object includes flow_core_map.h instead, because it
 * already defines napt_from_wan itself.
 */

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

    flow_core_update_elem(proto, &key, &value);
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
#define FLOW_CORE_EXIT_MAPPING 9
#define FLOW_CORE_EXIT_TTL 10
#define FLOW_CORE_EXIT_MTU 11
#define FLOW_CORE_SAFE_L3_MTU 1500U

/* Returns TC_ACT_REDIRECT when served from the core table, TC_ACT_OK otherwise. */
static __always_inline int flow_core_try_wan(struct __sk_buff *skb, __u32 l3_off) {
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

    struct flow_core_key key = {};
    key.saddr = iph.saddr;
    key.daddr = iph.daddr;
    key.sport = sport;
    key.dport = dport;
    key.proto = proto;
    key.dir = FLOW_CORE_DIR_WAN2LAN;

    struct flow_core_value *value = flow_core_lookup_elem(proto, &key);
    if (value) {
        int mapping_ok =
            flow_wan_mapping_matches(proto, dport, value->internal_addr, value->internal_port);
        __u64 now = bpf_ktime_get_ns();
        if (value->state == FLOW_CORE_STATE_COMPLETE &&
            value->ver == (__u32)flow_core_ver_get(value->out_ifindex) &&
            now <= value->deadline_ns && mapping_ok) {
            /* Fast hits skip the napt_from_wan lookup used only for local
             * traffic filtering; the recorded mapping is the authority. */
            flow_core_ctl_add(4);
            flow_core_ctl_add(5);

            /* The ingress fast path skips the NAT stage, so it must apply the
             * download-side QoS here; otherwise fast-path hits bypass shaping. */
            if (qos_check(skb, QOS_DIR_DOWNLOAD, skb->len) != TC_ACT_OK) return TC_ACT_SHOT;

            /* A redirect from ingress bypasses the IP forwarding path.  Keep
             * TTL handling here instead of leaking an undecremented TTL.
             * TTL <= 1 must go back to the slow path so the kernel can emit
             * ICMP Time Exceeded. */
            if (iph.ttl <= 1) return flow_core_ctl_set(7, FLOW_CORE_EXIT_TTL);
            if (skb->len > l3_off + FLOW_CORE_SAFE_L3_MTU)
                return flow_core_ctl_set(7, FLOW_CORE_EXIT_MTU);

            /* napt_modify() rewrites through direct packet access, so the
             * bytes must be linear first. */
            __u32 need = l3_off + ihl + 20;
            if (bpf_skb_pull_data(skb, need))
                return flow_core_ctl_set(7, FLOW_CORE_EXIT_PULL);

            /* The TTL byte is adjacent to protocol; update both as one
             * network-order 16-bit word so the checksum stays valid. */
            __be16 old_ttl_proto;
            __builtin_memcpy(&old_ttl_proto, &iph.ttl, sizeof(old_ttl_proto));
            __be16 new_ttl_proto = old_ttl_proto - bpf_htons(0x0100);
            if (bpf_l3_csum_replace(skb, l3_off + __builtin_offsetof(struct iphdr, check),
                                    old_ttl_proto, new_ttl_proto, sizeof(__be16)))
                return flow_core_ctl_set(7, FLOW_CORE_EXIT_TTL);
            if (bpf_skb_store_bytes(skb, l3_off + __builtin_offsetof(struct iphdr, ttl),
                                    &new_ttl_proto, sizeof(new_ttl_proto), 0))
                return flow_core_ctl_set(7, FLOW_CORE_EXIT_TTL);

            if (napt_modify(skb, value->internal_addr, value->internal_port,
                            LAND_FRAG_FROM_WAN, l3_off) != TC_ACT_OK)
                return flow_core_ctl_set(7, FLOW_CORE_EXIT_PULL);
            if (bpf_skb_store_bytes(skb, 0, value->dst_mac, 6, 0))
                return flow_core_ctl_set(7, FLOW_CORE_EXIT_MAC);
            if (bpf_skb_store_bytes(skb, 6, value->src_mac, 6, 0))
                return flow_core_ctl_set(7, FLOW_CORE_EXIT_MAC);

            flow_core_ctl_add(2);
            int ret = bpf_redirect(value->out_ifindex, 0);
            return ret == TC_ACT_REDIRECT ? TC_ACT_REDIRECT : TC_ACT_OK;
        }

        if (now > value->deadline_ns) flow_core_ctl_add(3);
        if (!mapping_ok && value->state == FLOW_CORE_STATE_COMPLETE)
            flow_core_ctl_set(7, FLOW_CORE_EXIT_MAPPING);
        flow_core_delete_elem(proto, &key);
    }

    /* Only packets with an external-port mapping are WAN->LAN forwarding.
     * Router-local traffic has no mapping and must not pollute the fast-path
     * hit-rate denominator.  Fast hits above already proved the mapping. */
    if (!flow_wan_mapping_exists(proto, dport)) return TC_ACT_OK;

    flow_core_ctl_add(4);
    flow_core_ctl_add(5);
    flow_core_ctl_add(6);
    return TC_ACT_OK;
}

#endif /* LAND_FLOW_CORE_TBL_H */
