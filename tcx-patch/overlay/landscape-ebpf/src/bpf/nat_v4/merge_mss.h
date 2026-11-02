/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_NAT_MERGE_MSS_H
#define LAND_NAT_MERGE_MSS_H

#include <vmlinux.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#include "base/landscape.h"
#include "base/proto_def.h"
#include "reader/tc_mss4.h"

/*
 * Optional MSS clamp merged into the NAT stage (LANDSCAPE_NAT_MERGE_MSS=1).
 *
 * When enabled, userspace does not attach the standalone tc_mss stage; it only
 * publishes the link MTU in `land_mss_cfg` (ifindex -> mtu) and the NAT
 * program clamps TCP SYN/SYN-ACK itself.  That removes one program (and one
 * tail call + packet parse) from both the WAN ingress and WAN egress chains.
 *
 * The clamp is a verbatim port of tc_mss.bpf.c's v4 path.
 */

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, __u32);
    __type(value, __u16);
} land_mss_cfg SEC(".maps");

#define NAT_MSS_OPT_EOL 0
#define NAT_MSS_OPT_NOP 1
#define NAT_MSS_OPT_MSS 2
#define NAT_MSS_TCP_HDR_LEN 20

static __always_inline void nat_mss_do_clamp(struct __sk_buff *skb, u32 offset, u8 tcp_doff,
                                             u16 mss_value) {
    u8 tcp_size = tcp_doff * 4;
    if (tcp_size <= NAT_MSS_TCP_HDR_LEN) return;
    u8 options_len = tcp_size - NAT_MSS_TCP_HDR_LEN;
    u8 option_pos = 0;

    for (int i = 0; i < 40; i++) {
        if (option_pos >= options_len) return;

        u8 remaining = options_len - option_pos;
        u32 option_offset = offset + NAT_MSS_TCP_HDR_LEN + option_pos;
        u8 kind_val;
        if (bpf_skb_load_bytes(skb, option_offset, &kind_val, sizeof(kind_val))) return;

        if (kind_val == NAT_MSS_OPT_EOL) return;
        if (kind_val == NAT_MSS_OPT_NOP) {
            option_pos += 1;
            continue;
        }
        if (remaining < 2) return;

        u8 opt_len_val;
        if (bpf_skb_load_bytes(skb, option_offset + 1, &opt_len_val, sizeof(opt_len_val))) return;
        if (opt_len_val < 2 || opt_len_val > remaining) return;

        if (kind_val == NAT_MSS_OPT_MSS) {
            if (opt_len_val != 4) return;
            __be16 mss_val;
            if (bpf_skb_load_bytes(skb, option_offset + 2, &mss_val, sizeof(mss_val))) return;
            if (bpf_ntohs(mss_val) > mss_value) {
                __be16 target_mss = bpf_htons(mss_value);
                if (bpf_l4_csum_replace(skb, offset + offsetof(struct tcphdr, check), mss_val,
                                        target_mss, 2)) {
                    return;
                }
                bpf_skb_store_bytes(skb, option_offset + 2, &target_mss, sizeof(target_mss), 0);
            }
            return;
        }

        option_pos += opt_len_val;
    }
}

static __always_inline void nat_merge_clamp_mss(struct __sk_buff *skb, u32 l3_off) {
    __u32 key = skb->ifindex;
    __u16 *mtu = bpf_map_lookup_elem(&land_mss_cfg, &key);
    if (!mtu) return;

    struct tc_mss4_view view;
    if (!tc_mss4_read(skb, l3_off, &view)) return;

    u32 ip_hdr_len = view.l4_off - l3_off;
    if (ip_hdr_len + NAT_MSS_TCP_HDR_LEN >= *mtu) return;
    nat_mss_do_clamp(skb, view.l4_off, view.doff, *mtu - ip_hdr_len - NAT_MSS_TCP_HDR_LEN);
}

#endif /* LAND_NAT_MERGE_MSS_H */
