/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_FLOW_VER_H
#define LAND_FLOW_VER_H

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>

/*
 * Per-LAN-interface generation for the flow_core fast path.
 *
 * A record stores the generation of the interface it points at; when a
 * neighbour on that interface changes, neigh_update's kprobe bumps only that
 * slot, so records of other LAN interfaces stay valid.
 *
 * Kept in its own header so neigh_update.bpf.c can bump it without pulling in
 * the whole flow_core table.
 */

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1024);
    __type(key, __u32);
    __type(value, __u64);
} flow_core_ver SEC(".maps");

static __always_inline __u64 flow_core_ver_get(__u32 ifindex) {
    __u64 *slot = bpf_map_lookup_elem(&flow_core_ver, &ifindex);
    return slot ? *slot : 0;
}

static __always_inline void flow_core_ver_bump(__u32 ifindex) {
    __u64 *slot = bpf_map_lookup_elem(&flow_core_ver, &ifindex);
    if (slot) __sync_fetch_and_add(slot, 1);
}

#endif /* LAND_FLOW_VER_H */
