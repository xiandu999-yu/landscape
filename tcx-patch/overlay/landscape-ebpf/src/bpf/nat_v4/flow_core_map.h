/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_FLOW_CORE_MAP_H
#define LAND_FLOW_CORE_MAP_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "nat_v4/flow_core.h"
#include "nat_v4/flow_ver.h"

/*
 * The core table plus its counters.
 *
 * Kept apart from flow_core_tbl.h so the NAT object (which owns napt_from_wan
 * itself) can include just this header - including the full tbl header would
 * redefine napt_from_wan with a different type.
 */

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 32768);
    __type(key, struct flow_core_key);
    __type(value, struct flow_core_value);
} flow_core SEC(".maps");

/* Control slots (global): [0]=ARP change counter [7]=last exit reason. */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 8);
    __type(key, __u32);
    __type(value, __u64);
} nat_wan_ctl SEC(".maps");

/* Per-CPU counters: [1]=recorded [2]=hit [3]=expired [4]=tries
 * [5]=parsed [6]=miss.  These are updated on every packet, so they must not
 * share one global cacheline across CPUs. */
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 8);
    __type(key, __u32);
    __type(value, __u64);
} nat_wan_stats SEC(".maps");

static __always_inline void flow_core_ctl_add(__u32 idx) {
    __u64 *slot = bpf_map_lookup_elem(&nat_wan_stats, &idx);
    if (slot) (*slot)++;
}

static __always_inline int flow_core_ctl_set(__u32 idx, __u64 val) {
    __u64 *slot = bpf_map_lookup_elem(&nat_wan_ctl, &idx);
    if (slot) *slot = val;
    return TC_ACT_OK;
}

#endif /* LAND_FLOW_CORE_MAP_H */
