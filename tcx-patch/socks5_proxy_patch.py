"""Register the in-process SOCKS5 proxy child mode."""


def apply_socks5_proxy_patch(patch):
    # 1) The webserver binary doubles as the netns proxy child.
    patch(
        "landscape-webserver/src/main.rs",
        "mod socks5_virtual;\n"
        "mod system;\n",
        "mod socks5_proxy;\n"
        "mod socks5_virtual;\n"
        "mod system;\n",
    )
    # 2) Generated skeleton for the TC ingress program.
    patch(
        "landscape-ebpf/src/skel/mod.rs",
        '#[rustfmt::skip]\n'
        '#[path = "../bpf_rs/tproxy.skel.rs"]\n'
        "pub mod tproxy;\n",
        '#[rustfmt::skip]\n'
        '#[path = "../bpf_rs/socks5_tproxy.skel.rs"]\n'
        "pub mod socks5_tproxy;\n"
        '#[rustfmt::skip]\n'
        '#[path = "../bpf_rs/tproxy.skel.rs"]\n'
        "pub mod tproxy;\n",
    )

    # 3) Re-export the libbpf/libc bits used by the webserver so it needs no
    # new direct dependencies (which would change Cargo.lock).
    patch(
        "landscape-ebpf/src/lib.rs",
        "const LANDSCAPE_IPV4_TYPE: u8 = 0;\n",
        "pub use libbpf_rs::{\n"
        "    skel::{OpenSkel, SkelBuilder},\n"
        "    TC_INGRESS,\n"
        "};\n"
        "pub use libc;\n"
        "\n"
        "const LANDSCAPE_IPV4_TYPE: u8 = 0;\n",
    )
