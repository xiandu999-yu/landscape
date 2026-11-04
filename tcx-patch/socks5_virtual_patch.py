"""Wire the singleton SOCKS5 virtual device into the web app.

The device itself and its API live in overlay files; these patches register
the type, the service field, the routes and the small WebUI client.
"""


def apply_socks5_virtual_patch(patch):
    # 1) Re-export the new config types.
    patch(
        "landscape-common/src/config_service/iface/mod.rs",
        "pub mod config;\n"
        "\n"
        "use serde::Serialize;\n"
        "\n"
        "pub use config::*;\n",
        "pub mod config;\n"
        "pub mod socks5;\n"
        "\n"
        "use serde::Serialize;\n"
        "\n"
        "pub use config::*;\n"
        "pub use socks5::*;\n",
    )

    # 2) Register the webserver module.
    patch(
        "landscape-webserver/src/main.rs",
        "mod services;\n"
        "mod system;\n",
        "mod services;\n"
        "mod socks5_virtual;\n"
        "mod system;\n",
    )

    # 3) Keep the singleton service on LandscapeApp.
    patch(
        "landscape-webserver/src/app.rs",
        "    /// Iface Config\n"
        "    pub(crate) iface_config_service: IfaceManagerService,\n",
        "    /// Iface Config\n"
        "    pub(crate) iface_config_service: IfaceManagerService,\n"
        "    /// Singleton SOCKS5 virtual device (veth/netkit + netns).\n"
        "    pub(crate) socks5_virtual_service: crate::socks5_virtual::Socks5VirtualService,\n",
    )

    # 4) Construct it from the app home path.
    patch(
        "landscape-webserver/src/main.rs",
        "    let auth_share = Arc::new(ArcSwap::from_pointee(config.auth.clone()));\n"
        "    let landscape_app_status = LandscapeApp {\n",
        "    let auth_share = Arc::new(ArcSwap::from_pointee(config.auth.clone()));\n"
        "    let socks5_virtual_service =\n"
        "        crate::socks5_virtual::Socks5VirtualService::new(home_path.clone());\n"
        "    let landscape_app_status = LandscapeApp {\n",
    )
    patch(
        "landscape-webserver/src/main.rs",
        "        iface_config_service,\n"
        "        wifi_service,\n",
        "        iface_config_service,\n"
        "        socks5_virtual_service,\n"
        "        wifi_service,\n",
    )

    # 5) Expose the handlers on the interface router.
    patch(
        "landscape-webserver/src/interfaces.rs",
        "use crate::api::JsonBody;\n"
        "use crate::{LandscapeApp, api::LandscapeApiResp, error::LandscapeApiResult};\n",
        "use crate::api::JsonBody;\n"
        "use crate::socks5_virtual::{\n"
        "    __path_create_socks5_virtual, __path_delete_socks5_virtual, __path_get_socks5_virtual,\n"
        "    create_socks5_virtual, delete_socks5_virtual, get_socks5_virtual,\n"
        "};\n"
        "use crate::{LandscapeApp, api::LandscapeApiResp, error::LandscapeApiResult};\n",
    )
    patch(
        "landscape-webserver/src/interfaces.rs",
        "        .routes(routes!(create_bridge))\n"
        "        .routes(routes!(delete_bridge))\n",
        "        .routes(routes!(create_bridge))\n"
        "        .routes(routes!(delete_bridge))\n"
        "        .routes(routes!(\n"
        "            get_socks5_virtual,\n"
        "            create_socks5_virtual,\n"
        "            delete_socks5_virtual\n"
        "        ))\n",
    )

    # 6) WebUI client.
    patch(
        "landscape-webui/src/api/network.ts",
        "export async function change_iface_boot_status(\n"
        "  iface_name: string,\n"
        "  enable_in_boot: boolean,\n"
        ") {\n"
        "  return networkAxios.post(\n"
        "    `/${encodeURIComponent(iface_name)}/boot/${enable_in_boot}`,\n"
        "  );\n"
        "}\n",
        "export async function change_iface_boot_status(\n"
        "  iface_name: string,\n"
        "  enable_in_boot: boolean,\n"
        ") {\n"
        "  return networkAxios.post(\n"
        "    `/${encodeURIComponent(iface_name)}/boot/${enable_in_boot}`,\n"
        "  );\n"
        "}\n"
        "\n"
        'export type Socks5LinkKind = "auto" | "veth" | "netkit";\n'
        "\n"
        "export interface Socks5VirtualConfig {\n"
        "  software: string;\n"
        "  server: string;\n"
        "  port: number;\n"
        "  username?: string | null;\n"
        "  password?: string | null;\n"
        "  kind: Socks5LinkKind;\n"
        "}\n"
        "\n"
        "export async function get_socks5_virtual(): Promise<Socks5VirtualConfig | null> {\n"
        "  const resp: any = await networkAxios.get(\"/socks5-virtual\");\n"
        "  return resp?.data ?? null;\n"
        "}\n"
        "\n"
        "export async function create_socks5_virtual(config: Socks5VirtualConfig) {\n"
        "  const resp: any = await networkAxios.post(\"/socks5-virtual\", config);\n"
        "  return resp?.data ?? null;\n"
        "}\n"
        "\n"
        "export async function delete_socks5_virtual() {\n"
        "  await networkAxios.delete(\"/socks5-virtual\");\n"
        "}\n",
    )

    # 7) i18n (zh).
    patch(
        "landscape-webui/src/i18n/zh/interface/index.ts",
        '  console_save_config_failed: "保存配置失败:",\n'
        "};\n",
        '  console_save_config_failed: "保存配置失败:",\n'
        '  socks5_title: "SOCKS5 虚拟设备（单例）",\n'
        '  socks5_exists: "已创建：{software} → {server}:{port}",\n'
        '  socks5_software: "软件名",\n'
        '  socks5_software_required: "请填写软件名",\n'
        '  socks5_server: "SOCKS5 服务器",\n'
        '  socks5_server_required: "请填写 SOCKS5 服务器",\n'
        '  socks5_port: "端口",\n'
        '  socks5_username: "用户名",\n'
        '  socks5_password: "密码",\n'
        '  socks5_kind: "链路类型",\n'
        '  socks5_kind_auto: "自动（netkit 优先）",\n'
        '  socks5_kind_veth: "veth",\n'
        '  socks5_kind_netkit: "netkit",\n'
        '  socks5_create: "创建 SOCKS5 虚拟设备",\n'
        '  socks5_delete: "删除",\n'
        '  socks5_delete_confirm: "删除后设备、netns 和网卡配置都会移除，确定？",\n'
        '  socks5_name_hint: "设备名/peer/netns 由软件名生成，例如 mihomo / mihomo-p / landscape-mihomo",\n'
        '  socks5_created: "SOCKS5 虚拟设备已创建",\n'
        '  socks5_deleted: "SOCKS5 虚拟设备已删除",\n'
        "};\n",
    )

    # 8) i18n (en).
    patch(
        "landscape-webui/src/i18n/en/interface/index.ts",
        '  console_save_config_failed: "Failed to save config:",\n'
        "};\n",
        '  console_save_config_failed: "Failed to save config:",\n'
        '  socks5_title: "SOCKS5 virtual device (singleton)",\n'
        '  socks5_exists: "Created: {software} → {server}:{port}",\n'
        '  socks5_software: "Software name",\n'
        '  socks5_software_required: "Software name is required",\n'
        '  socks5_server: "SOCKS5 server",\n'
        '  socks5_server_required: "SOCKS5 server is required",\n'
        '  socks5_port: "Port",\n'
        '  socks5_username: "Username",\n'
        '  socks5_password: "Password",\n'
        '  socks5_kind: "Link type",\n'
        '  socks5_kind_auto: "Auto (netkit first)",\n'
        '  socks5_kind_veth: "veth",\n'
        '  socks5_kind_netkit: "netkit",\n'
        '  socks5_create: "Create SOCKS5 virtual device",\n'
        '  socks5_delete: "Delete",\n'
        '  socks5_delete_confirm: "This removes the device, netns and interface config. Continue?",\n'
        '  socks5_name_hint: "Device/peer/netns names come from the software name, e.g. mihomo / mihomo-p / landscape-mihomo",\n'
        '  socks5_created: "SOCKS5 virtual device created",\n'
        '  socks5_deleted: "SOCKS5 virtual device deleted",\n'
        "};\n",
    )
