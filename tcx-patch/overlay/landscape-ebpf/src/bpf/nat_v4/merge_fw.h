/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_NAT_MERGE_FW_H
#define LAND_NAT_MERGE_FW_H

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>

#include "base/landscape.h"

/*
 * Optional IPv4 block-list check merged into the NAT stage
 * (LANDSCAPE_NAT_MERGE_FIREWALL=1).
 *
 * When enabled, userspace does not inject the v4 firewall stage; the NAT
 * programs do the same LPM lookup before translating:
 *   - WAN egress : match the destination (remote) address
 *   - WAN ingress: match the source (remote) address
 *
 * The map below is the same pinned `firewall_block_ip4_map` the firewall stage
 * uses (pinned explicitly at `paths.firewall_ipv4_block`), so rules stay live:
 * a block added in the UI takes effect on the next packet, no reload needed.
 *
 * IPv6 is NOT covered here: the v6 firewall stage is still attached, only the
 * v4 program is skipped.
 */

/* Same layout as firewall/share's `struct firewall_action`. */
struct nat_fw_action {
    __u32 mark;
};

struct {
    __uint(type, BPF_MAP_TYPE_LPM_TRIE);
    __type(key, struct ipv4_lpm_key);
    __type(value, struct nat_fw_action);
    __uint(max_entries, 65535);
    __uint(map_flags, BPF_F_NO_PREALLOC);
    __uint(pinning, LIBBPF_PIN_BY_NAME);
} firewall_block_ip4_map SEC(".maps");

static __always_inline int nat_merge_fw4(struct __sk_buff *skb, __be32 addr) {
    struct ipv4_lpm_key key = {
        .prefixlen = 32,
        .addr = addr,
    };
    return bpf_map_lookup_elem(&firewall_block_ip4_map, &key) ? TC_ACT_SHOT : TC_ACT_OK;
}

#endif /* LAND_NAT_MERGE_FW_H */
