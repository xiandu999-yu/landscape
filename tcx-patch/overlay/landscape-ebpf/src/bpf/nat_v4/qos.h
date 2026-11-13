/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LAND_NAT_QOS_H
#define LAND_NAT_QOS_H

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>

/*
 * Forwarding-only global token bucket.
 *
 * Only NAT/forwarding paths call this helper, so local router traffic and
 * LAN-to-LAN traffic never enter the bucket.  One global ARRAY entry keeps
 * the aggregate rate accurate; atomics avoid a BTF/spinlock requirement for
 * userspace-created maps.
 */
#define QOS_DIR_UPLOAD 0
#define QOS_DIR_DOWNLOAD 1
/* HTB root handle 1:, class 1:10.  Only forwarding paths set this. */
#define QOS_CLASS_PRIORITY 0x0001000AU

struct qos_forward_cfg {
    __u8 enabled;
    __u8 pad[3];
    __u32 cpu_count;
    __u64 upload_bps;
    __u64 download_bps;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct qos_forward_cfg);
} qos_forward_cfg SEC(".maps");

/* Kept pinned for compatibility with the existing map plumbing.  The data
 * path no longer reads or updates it; there is no eBPF token bucket. */
struct qos_bucket {
    __u64 tokens;
    __u64 last_ns;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 2);
    __type(key, __u32);
    __type(value, struct qos_bucket);
} qos_bucket SEC(".maps");

static __always_inline int qos_check(struct __sk_buff *skb, __u32 dir, __u32 len) {
    __u32 cfg_key = 0;
    struct qos_forward_cfg *cfg = bpf_map_lookup_elem(&qos_forward_cfg, &cfg_key);
    if (!cfg || !cfg->enabled) return TC_ACT_OK;

    /* Mark this as a forwarded packet for the HTB default/1:10 classes.
     * Shaping is done entirely by the qdisc; no eBPF policing here. */
    skb->priority = QOS_CLASS_PRIORITY;
    (void)dir;
    (void)len;
    return TC_ACT_OK;
}

#endif /* LAND_NAT_QOS_H */
