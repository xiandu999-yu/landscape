// Local-process flow matcher.
//
// A cgroup/skb egress program runs before tc egress for locally generated
// packets.  It looks up the sending process' `comm` in the per-flow process
// list and stamps the packet mark with the selected flow id, so the existing
// WAN route program can pick the normal target slots without knowing anything
// about processes.
//
// Whitelist (`proc_local_meta[flow] == 1`): the process must be present.
// Blacklist (`proc_local_meta[flow] == 2`): the process must be absent.
// Flows are evaluated in flow_id order; the first match wins.

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>

#include "base/mark.h"

#define PROC_COMM_LEN 16
#define PROC_LOCAL_FLOW_MAX 32

struct proc_match_key {
    __u32 flow_id;
    char comm[PROC_COMM_LEN];
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, PROC_LOCAL_FLOW_MAX);
    __type(key, __u32);
    __type(value, __u32);
} proc_local_flows SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 256);
    __type(key, __u32);
    __type(value, __u8);
} proc_local_meta SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, struct proc_match_key);
    __type(value, __u8);
} proc_match_map SEC(".maps");

SEC("cgroup_skb/egress")
int proc_match_egress(struct __sk_buff *skb) {
    /* Preserve explicit SO_MARK / other landscape marks (e.g. DNS). */
    if (skb_mark(skb) != 0) {
        return 1;
    }

    char comm[PROC_COMM_LEN] = {0};
    if (bpf_get_current_comm(&comm, sizeof(comm)) != 0) {
        return 1;
    }

    /* Fully unrolled on purpose: the verifier rejected the compiler's loop
     * shape (it jumped back to the condition without incrementing `i`).  A
     * macro expansion is straight-line code, so there is no back edge. */
#define PROC_MATCH_STEP(I)                                                         \
    do {                                                                           \
        __u32 index = (I);                                                         \
        __u32 *flow = bpf_map_lookup_elem(&proc_local_flows, &index);              \
        if (flow && *flow != 0) {                                                  \
            __u32 flow_id = *flow;                                                 \
            __u8 *flag = bpf_map_lookup_elem(&proc_local_meta, &flow_id);          \
            if (flag && *flag != 0) {                                              \
                struct proc_match_key key = {0};                                   \
                key.flow_id = flow_id;                                             \
                __builtin_memcpy(key.comm, comm, sizeof(key.comm));                \
                bool listed = bpf_map_lookup_elem(&proc_match_map, &key) != NULL; \
                if ((*flag == 1 && listed) || (*flag == 2 && !listed)) {          \
                    skb_set_mark(skb, (FLOW_REDIRECT << 8) | (flow_id & FLOW_ID_MASK)); \
                    return 1;                                                      \
                }                                                                  \
            }                                                                      \
        }                                                                          \
    } while (0)

    PROC_MATCH_STEP(0);
    PROC_MATCH_STEP(1);
    PROC_MATCH_STEP(2);
    PROC_MATCH_STEP(3);
    PROC_MATCH_STEP(4);
    PROC_MATCH_STEP(5);
    PROC_MATCH_STEP(6);
    PROC_MATCH_STEP(7);
    PROC_MATCH_STEP(8);
    PROC_MATCH_STEP(9);
    PROC_MATCH_STEP(10);
    PROC_MATCH_STEP(11);
    PROC_MATCH_STEP(12);
    PROC_MATCH_STEP(13);
    PROC_MATCH_STEP(14);
    PROC_MATCH_STEP(15);
    PROC_MATCH_STEP(16);
    PROC_MATCH_STEP(17);
    PROC_MATCH_STEP(18);
    PROC_MATCH_STEP(19);
    PROC_MATCH_STEP(20);
    PROC_MATCH_STEP(21);
    PROC_MATCH_STEP(22);
    PROC_MATCH_STEP(23);
    PROC_MATCH_STEP(24);
    PROC_MATCH_STEP(25);
    PROC_MATCH_STEP(26);
    PROC_MATCH_STEP(27);
    PROC_MATCH_STEP(28);
    PROC_MATCH_STEP(29);
    PROC_MATCH_STEP(30);
    PROC_MATCH_STEP(31);

    return 1;
}

char LICENSE[] SEC("license") = "GPL";
