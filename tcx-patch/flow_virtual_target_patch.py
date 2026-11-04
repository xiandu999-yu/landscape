"""Add a `virtual` flow target for the singleton SOCKS5 virtual device.

The route target is registered under `RouteOwner::Netns(<device>)`, so this is
a thin mapping layer: no new RouteOwner, no DB migration.
"""


def apply_flow_virtual_target_patch(patch):
    # 1) Internal flow target.
    patch(
        "landscape-common/src/flow/config.rs",
        "    Netns {\n"
        "        container_name: String,\n"
        "    },\n"
        "}\n",
        "    Netns {\n"
        "        container_name: String,\n"
        "    },\n"
        "    Virtual {\n"
        "        name: String,\n"
        "    },\n"
        "}\n",
    )

    # 2) API view.
    patch(
        "landscape-common/src/flow/api.rs",
        "    Netns {\n"
        "        container_name: String,\n"
        "    },\n"
        "}\n",
        "    Netns {\n"
        "        container_name: String,\n"
        "    },\n"
        "    Virtual {\n"
        "        name: String,\n"
        "    },\n"
        "}\n",
    )

    # 3) FlowTarget -> ApiFlowTarget.
    patch(
        "landscape-common/src/flow/api.rs",
        "            FlowTarget::Netns { container_name } => ApiFlowTarget::Netns { container_name },\n"
        "        }\n",
        "            FlowTarget::Netns { container_name } => ApiFlowTarget::Netns { container_name },\n"
        "            FlowTarget::Virtual { name } => ApiFlowTarget::Virtual { name },\n"
        "        }\n",
    )

    # 4) ApiFlowTarget -> FlowTarget.
    patch(
        "landscape-common/src/flow/api.rs",
        "            ApiFlowTarget::Netns { container_name } => FlowTarget::Netns { container_name },\n"
        "        }\n",
        "            ApiFlowTarget::Netns { container_name } => FlowTarget::Netns { container_name },\n"
        "            ApiFlowTarget::Virtual { name } => FlowTarget::Virtual { name },\n"
        "        }\n",
    )

    # 5) Resolve the virtual target against the route registered by the
    #    SOCKS5 device service.
    patch(
        "landscape-core/src/route/flow_target.rs",
        "        FlowTarget::Netns { container_name } => {\n"
        "            wan_infos.get(&RouteOwner::Netns(container_name.clone()))\n"
        "        }\n",
        "        FlowTarget::Netns { container_name } => {\n"
        "            wan_infos.get(&RouteOwner::Netns(container_name.clone()))\n"
        "        }\n"
        "        FlowTarget::Virtual { name } => wan_infos.get(&RouteOwner::Netns(name.clone())),\n",
    )

    # 6) Egress rule editor: add the virtual-device target type.
    patch(
        "landscape-webui/src/components/flow/match/FlowTargetRule.vue",
        'import { get_docker_container_summarys } from "@/api/docker";\n'
        'import { get_all_wan_links } from "@/api/service_wan_link";\n',
        'import { get_docker_container_summarys } from "@/api/docker";\n'
        'import { get_socks5_virtual } from "@/api/network";\n'
        'import { get_all_wan_links } from "@/api/service_wan_link";\n',
    )
    patch(
        "landscape-webui/src/components/flow/match/FlowTargetRule.vue",
        "const wan_links = ref<WanLink[]>([]);\n"
        "const docker_containers = ref<any[]>([]);\n"
        "\n"
        "onMounted(async () => {\n"
        "  wan_links.value = await get_all_wan_links();\n"
        "  docker_containers.value = await get_docker_container_summarys();\n"
        "});\n",
        "const wan_links = ref<WanLink[]>([]);\n"
        "const docker_containers = ref<any[]>([]);\n"
        "const virtual_device = ref<any | null>(null);\n"
        "\n"
        "onMounted(async () => {\n"
        "  const [links, containers, virtual] = await Promise.all([\n"
        "    get_all_wan_links(),\n"
        "    get_docker_container_summarys(),\n"
        "    get_socks5_virtual(),\n"
        "  ]);\n"
        "  wan_links.value = links;\n"
        "  docker_containers.value = containers;\n"
        "  virtual_device.value = virtual;\n"
        "});\n",
    )
    patch(
        "landscape-webui/src/components/flow/match/FlowTargetRule.vue",
        "const docker_options = computed(() =>\n"
        "  docker_containers.value.map((e) => {\n"
        "    let name = e.Names[0] ?? \"\";\n"
        "    if (name.startsWith(\"/\")) {\n"
        "      name = name.slice(1);\n"
        "    }\n"
        "    return {\n"
        "      label: name,\n"
        "      value: name,\n"
        "    };\n"
        "  }),\n"
        ");\n",
        "const docker_options = computed(() =>\n"
        "  docker_containers.value.map((e) => {\n"
        "    let name = e.Names[0] ?? \"\";\n"
        "    if (name.startsWith(\"/\")) {\n"
        "      name = name.slice(1);\n"
        "    }\n"
        "    return {\n"
        "      label: name,\n"
        "      value: name,\n"
        "    };\n"
        "  }),\n"
        ");\n"
        "\n"
        "const virtual_options = computed(() => {\n"
        "  const device = virtual_device.value;\n"
        "  return device ? [{ label: device.software, value: device.software }] : [];\n"
        "});\n",
    )
    patch(
        "landscape-webui/src/components/flow/match/FlowTargetRule.vue",
        "enum FlowTargetEnum {\n"
        "  Interface = \"interface\",\n"
        "  NetNS = \"netns\",\n"
        "}\n",
        "enum FlowTargetEnum {\n"
        "  Interface = \"interface\",\n"
        "  NetNS = \"netns\",\n"
        "  Virtual = \"virtual\",\n"
        "}\n",
    )
    patch(
        "landscape-webui/src/components/flow/match/FlowTargetRule.vue",
        "    {\n"
        "      label: t(\"flow.target_rule.type_docker\"),\n"
        "      value: \"netns\",\n"
        "    },\n"
        "  ];\n",
        "    {\n"
        "      label: t(\"flow.target_rule.type_docker\"),\n"
        "      value: \"netns\",\n"
        "    },\n"
        "    {\n"
        "      label: t(\"flow.target_rule.type_virtual\"),\n"
        "      value: \"virtual\",\n"
        "    },\n"
        "  ];\n",
    )
    patch(
        "landscape-webui/src/components/flow/match/FlowTargetRule.vue",
        "  } else {\n"
        "    target_rules.value[index] = {\n"
        "      target: {\n"
        "        t: FlowTargetEnum.NetNS,\n"
        "        container_name: \"\",\n"
        "      },\n"
        "      weight,\n"
        "    };\n"
        "  }\n",
        "  } else if (value == FlowTargetEnum.Virtual) {\n"
        "    target_rules.value[index] = {\n"
        "      target: {\n"
        "        t: FlowTargetEnum.Virtual,\n"
        "        name: virtual_device.value?.software ?? \"\",\n"
        "      },\n"
        "      weight,\n"
        "    };\n"
        "  } else {\n"
        "    target_rules.value[index] = {\n"
        "      target: {\n"
        "        t: FlowTargetEnum.NetNS,\n"
        "        container_name: \"\",\n"
        "      },\n"
        "      weight,\n"
        "    };\n"
        "  }\n",
    )
    patch(
        "landscape-webui/src/components/flow/match/FlowTargetRule.vue",
        "        <n-select\n"
        "          v-else-if=\"value.target.t == 'netns'\"\n"
        "          v-model:value=\"value.target.container_name\"\n"
        "          :style=\"{ width: '56%' }\"\n"
        "          :options=\"docker_options\"\n"
        "          :placeholder=\"t('flow.target_rule.container_placeholder')\"\n"
        "        />\n",
        "        <n-select\n"
        "          v-else-if=\"value.target.t == 'netns'\"\n"
        "          v-model:value=\"value.target.container_name\"\n"
        "          :style=\"{ width: '56%' }\"\n"
        "          :options=\"docker_options\"\n"
        "          :placeholder=\"t('flow.target_rule.container_placeholder')\"\n"
        "        />\n"
        "        <n-select\n"
        "          v-else-if=\"value.target.t == 'virtual'\"\n"
        "          v-model:value=\"value.target.name\"\n"
        "          :style=\"{ width: '56%' }\"\n"
        "          :options=\"virtual_options\"\n"
        "          :placeholder=\"t('flow.target_rule.virtual_placeholder')\"\n"
        "        />\n",
    )

    # 7) Flow display labels.
    patch(
        "landscape-webui/src/components/flow/FlowConfigCard.vue",
        "function target_label(target: FlowConfig[\"flow_targets\"][number][\"target\"]) {\n"
        "  return target.t === \"netns\"\n"
        "    ? target.container_name\n"
        "    : link_label(wan_links.value, target.link_id) || t(\"common.deleted_link\");\n"
        "}\n",
        "function target_label(target: FlowConfig[\"flow_targets\"][number][\"target\"]) {\n"
        "  if (target.t === \"netns\") return target.container_name;\n"
        "  if (target.t === \"virtual\") return target.name;\n"
        "  return link_label(wan_links.value, target.link_id) || t(\"common.deleted_link\");\n"
        "}\n",
    )
    patch(
        "landscape-webui/src/components/flow/FlowExhibit.vue",
        "function target_label(target: FlowConfig[\"flow_targets\"][number][\"target\"]) {\n"
        "  return target.t === \"netns\"\n"
        "    ? target.container_name\n"
        "    : link_label(wan_links.value, target.link_id) || t(\"common.deleted_link\");\n"
        "}\n",
        "function target_label(target: FlowConfig[\"flow_targets\"][number][\"target\"]) {\n"
        "  if (target.t === \"netns\") return target.container_name;\n"
        "  if (target.t === \"virtual\") return target.name;\n"
        "  return link_label(wan_links.value, target.link_id) || t(\"common.deleted_link\");\n"
        "}\n",
    )

    # 8) Flow i18n.
    patch(
        "landscape-webui/src/i18n/zh/flow/index.ts",
        '    type_docker: "虚拟网卡",\n'
        '    type_interface: "网卡",\n'
        '    type_netns: "Docker 容器名称",\n'
        '    iface_placeholder: "网卡名称",\n'
        '    container_placeholder: "容器名称",\n',
        '    type_docker: "虚拟网卡",\n'
        '    type_virtual: "SOCKS5 虚拟网卡",\n'
        '    type_interface: "网卡",\n'
        '    type_netns: "Docker 容器名称",\n'
        '    iface_placeholder: "网卡名称",\n'
        '    container_placeholder: "容器名称",\n'
        '    virtual_placeholder: "虚拟网卡设备名",\n',
    )
    patch(
        "landscape-webui/src/i18n/en/flow/index.ts",
        '    type_docker: "Virtual NIC",\n'
        '    type_interface: "Interface",\n'
        '    type_netns: "Docker Container",\n'
        '    iface_placeholder: "Interface name",\n'
        '    container_placeholder: "Container name",\n',
        '    type_docker: "Virtual NIC",\n'
        '    type_virtual: "SOCKS5 Virtual NIC",\n'
        '    type_interface: "Interface",\n'
        '    type_netns: "Docker Container",\n'
        '    iface_placeholder: "Interface name",\n'
        '    container_placeholder: "Container name",\n'
        '    virtual_placeholder: "Virtual device name",\n',
    )
