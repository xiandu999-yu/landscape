/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_WAN_LOCAL_SKIP_H
#define LAND_WAN_LOCAL_SKIP_H

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>

/*
 * Per-WAN-interface "skip router-originated traffic" switch.
 *
 * The WAN route service records `RouteWanServiceConfig.skip_tc_xdp` here when
 * it attaches, and the NAT stages read it.  Router-originated flows that
 * bypassed the WAN route chain are not NATed, so their replies carry no
 * `napt_from_wan` mapping; the WAN ingress NAT stage must deliver those
 * packets to the local stack instead of dropping them as unknown mappings
 * (otherwise enabling NAT turns the bypass into a block).
 */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, u32);
    __type(value, u8);
} wan_local_skip SEC(".maps");

static __always_inline bool wan_local_skip_get(u32 ifindex) {
    __u8 *value = bpf_map_lookup_elem(&wan_local_skip, &ifindex);
    return value != NULL && *value != 0;
}

#endif /* LAND_WAN_LOCAL_SKIP_H */
