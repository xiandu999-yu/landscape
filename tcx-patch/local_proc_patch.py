"""Local-process flow entries, v2 (DNS-like list shape).

The public shape is:

    {"t": "local", "blacklist": false, "bypass_process": ["mihomo", ...]}

`blacklist = false` means whitelist (only the listed processes match);
`blacklist = true` means blacklist (the listed processes are excluded).
Both fields live directly on the `Local` variant so the JSON matches the
DNS rule style rather than nesting under `process`.
"""


def apply_local_proc_patch(patch):
    # 1) `FlowEntryMatchMode::Local { inverse, bypass_process }`.
    patch(
        "landscape-common/src/flow/config.rs",
        "    Device {\n        device_id: Uuid,\n    },\n}\n",
        "    Device {\n        device_id: Uuid,\n    },\n"
        "    /// Locally generated traffic (the router's own sockets).\n"
        "    ///\n"
        "    /// `bypass_process` is the process list; `blacklist` selects whether\n"
        "    /// the list is a whitelist (false) or a blacklist (true).\n"
        "    Local {\n"
        "        #[serde(default)]\n"
        "        blacklist: bool,\n"
        "        #[serde(default)]\n"
        "        bypass_process: Vec<String>,\n"
        "    },\n"
        "}\n",
    )

    # 2) Validate process names (comm is limited to 15 bytes).
    patch(
        "landscape-common/src/flow/config.rs",
        "        }\n\n        Ok(())\n    }\n}\n\nimpl FlowEntryRule {",
        "        }\n\n"
        "        if let FlowEntryMatchMode::Local { bypass_process, .. } = self {\n"
        "            let mut seen = std::collections::HashSet::new();\n"
        "            for name in bypass_process {\n"
        "                let name = name.trim();\n"
        "                if name.is_empty() {\n"
        "                    return Err(ServiceConfigError::InvalidConfig {\n"
        '                        reason: "local process list contains an empty process name".to_string(),\n'
        "                    });\n"
        "                }\n"
        "                if name.len() > 15 {\n"
        "                    return Err(ServiceConfigError::InvalidConfig {\n"
        "                        reason: format!(\n"
        '                            "local process name \'{name}\' is {} bytes; Linux comm is limited to 15",\n'
        "                            name.len()\n"
        "                        ),\n"
        "                    });\n"
        "                }\n"
        "                if !seen.insert(name) {\n"
        "                    return Err(ServiceConfigError::InvalidConfig {\n"
        '                        reason: format!("duplicate local process name \'{name}\'"),\n'
        "                    });\n"
        "                }\n"
        "            }\n"
        "        }\n\n"
        "        Ok(())\n    }\n}\n\nimpl FlowEntryRule {",
    )

    # 3) Display.
    patch(
        "landscape-common/src/flow/config.rs",
        "            FlowEntryMatchMode::Device { device_id } => write!(f, \"Device {}\", device_id),\n"
        "        }\n"
        "    }\n"
        "}\n",
        "            FlowEntryMatchMode::Device { device_id } => write!(f, \"Device {}\", device_id),\n"
        "            FlowEntryMatchMode::Local { blacklist, bypass_process } => write!(\n"
        "                f,\n"
        "                \"Local({}: {:?})\",\n"
        "                if *blacklist { \"blacklist\" } else { \"whitelist\" },\n"
        "                bypass_process\n"
        "            ),\n"
        "        }\n"
        "    }\n"
        "}\n",
    )

    # 4) Tests.
    patch(
        "landscape-common/src/flow/config.rs",
        "    #[test]\n"
        "    fn accepts_ipv6_host_prefix() {\n"
        "        let result = FlowEntryMatchMode::Ip {\n"
        '            ip: "2001:db8::1".parse().unwrap(),\n'
        "            prefix_len: 128,\n"
        "        }\n"
        "        .validate();\n"
        "\n"
        "        assert!(result.is_ok());\n"
        "    }\n"
        "}\n",
        "    #[test]\n"
        "    fn accepts_ipv6_host_prefix() {\n"
        "        let result = FlowEntryMatchMode::Ip {\n"
        '            ip: "2001:db8::1".parse().unwrap(),\n'
        "            prefix_len: 128,\n"
        "        }\n"
        "        .validate();\n"
        "\n"
        "        assert!(result.is_ok());\n"
        "    }\n"
        "\n"
        "    #[test]\n"
        "    fn local_process_filter_roundtrips_and_validates() {\n"
        "        let mode = FlowEntryMatchMode::Local {\n"
        "            blacklist: true,\n"
        '            bypass_process: vec!["curl".to_string(), "wget".to_string()],\n'
        "        };\n"
        "\n"
        "        assert!(mode.validate().is_ok());\n"
        "        let json = serde_json::to_value(&mode).unwrap();\n"
        '        assert_eq!(json["t"].as_str(), Some("local"));\n'
        '        assert_eq!(json["blacklist"].as_bool(), Some(true));\n'
        '        assert_eq!(json["bypass_process"][0].as_str(), Some("curl"));\n'
        "        let decoded: FlowEntryMatchMode = serde_json::from_value(json).unwrap();\n"
        "        assert_eq!(decoded, mode);\n"
        "    }\n"
        "\n"
        "    #[test]\n"
        "    fn rejects_empty_and_oversized_local_process_names() {\n"
        "        let empty = FlowEntryMatchMode::Local {\n"
        "            blacklist: false,\n"
        '            bypass_process: vec!["  ".to_string()],\n'
        "        };\n"
        "        assert!(empty.validate().is_err());\n"
        "\n"
        "        let oversized = FlowEntryMatchMode::Local {\n"
        "            blacklist: false,\n"
        '            bypass_process: vec!["a".repeat(16)],\n'
        "        };\n"
        "        assert!(oversized.validate().is_err());\n"
        "    }\n"
        "}\n",
    )

    # 5) DB conflict lookup.
    patch(
        "landscape-database/src/flow_rule/repository.rs",
        "            FlowEntryMatchMode::Device { device_id } => (\n"
        "                \"json_extract(json_each.value, '$.mode.t') = 'device' AND json_extract(json_each.value, '$.mode.device_id') = ?\",\n"
        "                vec![sea_orm::Value::String(Some(Box::new(device_id.to_string())))],\n"
        "            ),\n"
        "        };\n",
        "            FlowEntryMatchMode::Device { device_id } => (\n"
        "                \"json_extract(json_each.value, '$.mode.t') = 'device' AND json_extract(json_each.value, '$.mode.device_id') = ?\",\n"
        "                vec![sea_orm::Value::String(Some(Box::new(device_id.to_string())))],\n"
        "            ),\n"
        "            FlowEntryMatchMode::Local { blacklist, bypass_process } => (\n"
        "                \"json_extract(json_each.value, '$.mode.t') = 'local' AND json_extract(json_each.value, '$.mode.blacklist') = ? AND COALESCE(json_extract(json_each.value, '$.mode.bypass_process'), '[]') = ?\",\n"
        "                vec![\n"
        "                    sea_orm::Value::Int(Some(if *blacklist { 1 } else { 0 })),\n"
        "                    sea_orm::Value::String(Some(Box::new(\n"
        "                        serde_json::to_string(bypass_process)\n"
        '                            .unwrap_or_else(|_| "[]".to_string()),\n'
        "                    ))),\n"
        "                ],\n"
        "            ),\n"
        "        };\n",
    )

    # 6) Runtime resolution: Local uses the process matcher, not flow_match_map.
    patch(
        "landscape-database/src/flow_rule/repository.rs",
        "        FlowEntryMatchMode::Ip { ip, prefix_len } => Some(ResolvedFlowEntryRule {\n"
        "            qos: rule.qos,\n"
        "            mode: ResolvedFlowEntryMatchMode::Ip { ip, prefix_len },\n"
        "        }),\n"
        "    }\n"
        "}\n",
        "        FlowEntryMatchMode::Ip { ip, prefix_len } => Some(ResolvedFlowEntryRule {\n"
        "            qos: rule.qos,\n"
        "            mode: ResolvedFlowEntryMatchMode::Ip { ip, prefix_len },\n"
        "        }),\n"
        "        // Local/process rules use the dedicated process matcher, not the\n"
        "        // address-based flow_match_map; they are added there in a later step.\n"
        "        FlowEntryMatchMode::Local { .. } => {\n"
        "            tracing::warn!(\n"
        "                \"local process flow rule configured, but the eBPF process matcher is not wired yet\"\n"
        "            );\n"
        "            None\n"
        "        }\n"
        "    }\n"
        "}\n",
    )
    patch(
        "landscape-database/src/flow_rule/repository.rs",
        "        FlowEntryMatchMode::Device { .. } => None,\n",
        "        FlowEntryMatchMode::Device { .. } | FlowEntryMatchMode::Local { .. } => None,\n",
    )

    # 7) Duplicate-entry detection in the modal.
    patch(
        "landscape-webui/src/components/flow/FlowEditModal.vue",
        "    } else if (rule.mode.t === \"ip\") {\n"
        "      key = `ip:${rule.mode.ip}/${rule.mode.prefix_len}`;\n"
        "    } else {\n"
        "      key = `device:${rule.mode.device_id}`;\n"
        "    }\n",
        "    } else if (rule.mode.t === \"ip\") {\n"
        "      key = `ip:${rule.mode.ip}/${rule.mode.prefix_len}`;\n"
        "    } else if (rule.mode.t === \"device\") {\n"
        "      key = `device:${rule.mode.device_id}`;\n"
        "    } else {\n"
        "      key = `local:${JSON.stringify({\n"
        "        blacklist: (rule.mode as any).blacklist ?? false,\n"
        "        bypass_process: (rule.mode as any).bypass_process ?? [],\n"
        "      })}`;\n"
        "    }\n",
    )

    # 8) i18n.
    patch(
        "landscape-webui/src/i18n/zh/flow/index.ts",
        "  match_rule: {\n"
        "    add_entry_rule: \"增加一条入口匹配规则\",\n"
        "    select_device_placeholder: \"选择已登记设备\",\n"
        "    mac_placeholder: \"手动输入 MAC 地址\",\n"
        "    ip_placeholder: \"IP 地址\",\n"
        "    prefix_placeholder: \"前缀长度\",\n"
        "  },\n",
        "  match_rule: {\n"
        "    add_entry_rule: \"增加一条入口匹配规则\",\n"
        "    mode_device: \"已登记设备\",\n"
        "    mode_mac: \"MAC 地址\",\n"
        "    mode_ip: \"IP 地址\",\n"
        "    mode_local: \"本机程序\",\n"
        "    local_whitelist: \"白名单\",\n"
        "    local_blacklist: \"黑名单\",\n"
        "    local_all: \"本机（全部进程）\",\n"
        "    local_hint: \"白名单=只匹配名单内进程；黑名单=排除名单内进程\",\n"
        "    process_placeholder: \"进程名（comm，最长 15 字符）\",\n"
        "    add_process: \"添加进程\",\n"
        "    select_device_placeholder: \"选择已登记设备\",\n"
        "    mac_placeholder: \"手动输入 MAC 地址\",\n"
        "    ip_placeholder: \"IP 地址\",\n"
        "    prefix_placeholder: \"前缀长度\",\n"
        "  },\n",
    )
    patch(
        "landscape-webui/src/i18n/en/flow/index.ts",
        "  match_rule: {\n"
        "    add_entry_rule: \"Add entry match rule\",\n"
        "    select_device_placeholder: \"Select an enrolled device\",\n"
        "    mac_placeholder: \"Enter MAC address\",\n"
        "    ip_placeholder: \"IP address\",\n"
        "    prefix_placeholder: \"Prefix length\",\n"
        "  },\n",
        "  match_rule: {\n"
        "    add_entry_rule: \"Add entry match rule\",\n"
        "    mode_device: \"Enrolled device\",\n"
        "    mode_mac: \"MAC address\",\n"
        "    mode_ip: \"IP address\",\n"
        "    mode_local: \"Local process\",\n"
        "    local_whitelist: \"Whitelist\",\n"
        "    local_blacklist: \"Blacklist\",\n"
        "    local_all: \"Local (all processes)\",\n"
        "    local_hint: \"Whitelist = listed processes only; blacklist = everything except listed\",\n"
        "    process_placeholder: \"Process name (comm, up to 15 chars)\",\n"
        "    add_process: \"Add process\",\n"
        "    select_device_placeholder: \"Select an enrolled device\",\n"
        "    mac_placeholder: \"Enter MAC address\",\n"
        "    ip_placeholder: \"IP address\",\n"
        "    prefix_placeholder: \"Prefix length\",\n"
        "  },\n",
    )
