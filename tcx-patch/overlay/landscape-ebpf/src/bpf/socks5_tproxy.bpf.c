// TC ingress redirect for the SOCKS5 virtual device (no nftables).
//
// The program is attached to the device netns peer.  It looks up the
// userspace proxy's transparent TCP listener and assigns the skb to it with
// bpf_sk_assign; the kernel keeps the original destination in skb->dst, so
// the proxy can recover it from the accepted socket's local address.

#include <vmlinux.h>

#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "tproxy/tproxy_packet.h"

#define BPF_F_CURRENT_NETNS (-1L)
#define PACKET_HOST 0

volatile const __be32 proxy_addr = 0;
volatile const __be16 proxy_port = 0;

SEC("tc/ingress")
int socks5_tproxy_ingress(struct __sk_buff *skb) {
    struct tproxy_view view = {0};
    struct inet_pair ip_pair = {0};

    int ret = scan_tproxy_packet(skb, 14, &view, &ip_pair);
    if (ret) {
        return TC_ACT_SHOT;
    }
    if (view.l4_protocol != IPPROTO_TCP) {
        // TCP is the only supported transport for now; dropping here avoids
        // bouncing unsupported traffic back through the default route.
        return TC_ACT_SHOT;
    }

    ret = read_tproxy_packet_info(skb, &view, &ip_pair);
    if (ret) {
        return TC_ACT_SHOT;
    }

    struct bpf_sock_tuple server = {0};
    size_t tuple_len = sizeof(server.ipv4);
    server.ipv4.saddr = ip_pair.src_addr.in6_u.u6_addr32[0];
    server.ipv4.daddr = proxy_addr;
    server.ipv4.sport = ip_pair.src_port;
    server.ipv4.dport = proxy_port;

    struct bpf_sock *sk =
        bpf_skc_lookup_tcp(skb, &server, tuple_len, BPF_F_CURRENT_NETNS, 0);
    if (!sk) {
        return TC_ACT_SHOT;
    }
    if (sk->state != BPF_TCP_LISTEN) {
        bpf_sk_release(sk);
        return TC_ACT_SHOT;
    }

    bpf_skb_change_type(skb, PACKET_HOST);
    ret = bpf_sk_assign(skb, sk, 0);
    bpf_sk_release(sk);
    return ret == 0 ? TC_ACT_OK : TC_ACT_SHOT;
}

char LICENSE[] SEC("license") = "GPL";
