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

    # 3) `libc` for SO_ORIGINAL_DST / IP_TRANSPARENT.
    patch(
        "landscape-webserver/Cargo.toml",
        "tokio = { workspace = true, features = [\"full\"] }\n"
        "clap = { workspace = true }\n",
        "tokio = { workspace = true, features = [\"full\"] }\n"
        "clap = { workspace = true }\n"
        "libc = { workspace = true }\n"
        "socket2 = { workspace = true }\n"
        "libbpf-rs = { workspace = true }\n",
    )
