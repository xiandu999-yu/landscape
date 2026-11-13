/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_NAT_NAT4_PROTO_H
#define LAND_NAT_NAT4_PROTO_H

#include <vmlinux.h>

/*
 * Protocols without L4 ports.  They share the WAN address through a
 * per-protocol mapping (one internal host per protocol unless static
 * 1:1 mappings are added later):
 *
 *   IPIP (4)  - outer IPv4 only, no transport checksum
 *   GRE  (47) - PPTP / GRE; GRE checksum does not cover the outer IP header
 *   ESP  (50) - IPsec transport/tunnel; ESP integrity does not cover outer IP
 *   L2TP (115) - raw L2TP over IP
 *
 * AH (51) is deliberately not listed: its ICV covers the outer IPv4
 * addresses, so NAPT would invalidate the packet.
 */
#define NAPT_IPPROTO_IPIP 4
#define NAPT_IPPROTO_GRE 47
#define NAPT_IPPROTO_ESP 50
#define NAPT_IPPROTO_L2TP 115

static __always_inline int napt_is_nonport_protocol(__u8 proto) {
    return proto == NAPT_IPPROTO_IPIP || proto == NAPT_IPPROTO_GRE ||
           proto == NAPT_IPPROTO_ESP || proto == NAPT_IPPROTO_L2TP;
}

#endif /* LAND_NAT_NAT4_PROTO_H */
