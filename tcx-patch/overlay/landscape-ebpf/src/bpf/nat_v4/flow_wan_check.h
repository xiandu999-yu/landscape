/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_FLOW_WAN_CHECK_H
#define LAND_FLOW_WAN_CHECK_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "nat_v4/flow_core.h"

/*
 * Mapping check: the fast-path record must still agree with the external-port
 * mapping. `napt_from_wan` is an ARRAY indexed by proto base + port, so the
 * check is a single array lookup (~40ns) and needs no hash.
 *
 * This header is only included by the objects that need the check (ingress
 * route + ingress root). The NAT object defines napt_from_wan itself via
 * tc_nat4_map.h, so it must not include this file.
 */

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

#endif /* LAND_FLOW_WAN_CHECK_H */
