"""Wire the local-process matcher into the runtime.

The BPF object and its Rust owner are overlay files; this module patches the
shared runtime types, the flow runtime builder, the eBPF runtime and the
flow-dataplane sync hook.
"""


def apply_local_proc_dataplane_patch(patch):
    # 1) Runtime flow config carries the local-process rules alongside the
    #    address-based LPM rules.
    patch(
        "landscape-common/src/flow/runtime.rs",
        "#[derive(Clone, Debug, PartialEq, Eq)]\n"
        "pub struct RuntimeFlowConfig {\n"
        "    pub flow_id: u32,\n"
        "    pub flow_match_rules: Vec<ResolvedFlowEntryRule>,\n"
        "}\n",
        "#[derive(Clone, Debug, PartialEq, Eq)]\n"
        "pub struct RuntimeLocalProcessRule {\n"
        "    pub flow_id: u32,\n"
        "    pub blacklist: bool,\n"
        "    pub bypass_process: Vec<String>,\n"
        "}\n"
        "\n"
        "#[derive(Clone, Debug, PartialEq, Eq)]\n"
        "pub struct RuntimeFlowConfig {\n"
        "    pub flow_id: u32,\n"
        "    pub flow_match_rules: Vec<ResolvedFlowEntryRule>,\n"
        "    pub local_process_rules: Vec<RuntimeLocalProcessRule>,\n"
        "}\n",
    )

    # 2) Import the new runtime rule in the repository.
    patch(
        "landscape-database/src/flow_rule/repository.rs",
        "use landscape_common::flow::{\n"
        "    FlowEntryMatchMode, FlowEntryRule, FlowRuleError, FlowTarget, ResolvedFlowEntryMatchMode,\n"
        "    ResolvedFlowEntryRule, RuntimeFlowConfig,\n"
        "};\n",
        "use landscape_common::flow::{\n"
        "    FlowEntryMatchMode, FlowEntryRule, FlowRuleError, FlowTarget, ResolvedFlowEntryMatchMode,\n"
        "    ResolvedFlowEntryRule, RuntimeFlowConfig, RuntimeLocalProcessRule,\n"
        "};\n",
    )

    # 3) Build the local-process rules while resolving runtime configs.
    patch(
        "landscape-database/src/flow_rule/repository.rs",
        "        for config in configs.into_iter().filter(|config| config.enable) {\n"
        "            let flow_match_rules = config\n"
        "                .flow_match_rules\n"
        "                .into_iter()\n"
        "                .filter_map(|rule| resolve_flow_entry_rule(rule, &devices))\n"
        "                .collect();\n"
        "\n"
        "            result.push(RuntimeFlowConfig { flow_id: config.flow_id, flow_match_rules });\n"
        "        }\n",
        "        for config in configs.into_iter().filter(|config| config.enable) {\n"
        "            let local_process_rules = config\n"
        "                .flow_match_rules\n"
        "                .iter()\n"
        "                .filter_map(|rule| match &rule.mode {\n"
        "                    FlowEntryMatchMode::Local { blacklist, bypass_process } => {\n"
        "                        Some(RuntimeLocalProcessRule {\n"
        "                            flow_id: config.flow_id,\n"
        "                            blacklist: *blacklist,\n"
        "                            bypass_process: bypass_process.clone(),\n"
        "                        })\n"
        "                    }\n"
        "                    _ => None,\n"
        "                })\n"
        "                .collect();\n"
        "            let flow_match_rules = config\n"
        "                .flow_match_rules\n"
        "                .into_iter()\n"
        "                .filter_map(|rule| resolve_flow_entry_rule(rule, &devices))\n"
        "                .collect();\n"
        "\n"
        "            result.push(RuntimeFlowConfig {\n"
        "                flow_id: config.flow_id,\n"
        "                flow_match_rules,\n"
        "                local_process_rules,\n"
        "            });\n"
        "        }\n",
    )

    # 4) Register the generated skeleton module.
    patch(
        "landscape-ebpf/src/skel/mod.rs",
        '#[rustfmt::skip]\n'
        '#[path = "../bpf_rs/sk_dns_dispatcher.skel.rs"]\n'
        "pub mod sk_dns_dispatcher;\n",
        '#[rustfmt::skip]\n'
        '#[path = "../bpf_rs/proc_match.skel.rs"]\n'
        "pub mod proc_match;\n"
        '#[rustfmt::skip]\n'
        '#[path = "../bpf_rs/sk_dns_dispatcher.skel.rs"]\n'
        "pub mod sk_dns_dispatcher;\n",
    )

    # 5) Register the Rust owner module.
    patch(
        "landscape-ebpf/src/lib.rs",
        "pub mod pppoe;\n",
        "pub mod pppoe;\npub mod proc_match;\n",
    )

    # 6) Keep the loaded cgroup object on the runtime.
    patch(
        "landscape-ebpf/src/runtime.rs",
        "    pub(crate) xdp_attach: Arc<XdpAttach>,\n"
        "    pub(crate) try_native_xdp: Option<Vec<i32>>,\n"
        "    static_nat4_tx: broadcast::Sender<()>,\n",
        "    pub(crate) xdp_attach: Arc<XdpAttach>,\n"
        "    pub(crate) try_native_xdp: Option<Vec<i32>>,\n"
        "    /// Local-process matcher; `None` when cgroup2/BPF attach is not\n"
        "    /// available (the rest of the dataplane keeps working).\n"
        "    pub(crate) proc_match: Option<Arc<crate::proc_match::ProcMatch>>,\n"
        "    static_nat4_tx: broadcast::Sender<()>,\n",
    )

    # 7) Initialize it after the XDP coordinator.
    patch(
        "landscape-ebpf/src/runtime.rs",
        "        let xdp_attach = Arc::new(XdpAttach::new(paths.clone(), try_native_xdp.clone()));\n"
        "        let (static_nat4_tx, _) = broadcast::channel(16);\n"
        "        Ok(Self {\n"
        "            paths,\n"
        "            backbone,\n"
        "            xdp_attach,\n"
        "            try_native_xdp,\n"
        "            static_nat4_tx,\n"
        "        })\n",
        "        let xdp_attach = Arc::new(XdpAttach::new(paths.clone(), try_native_xdp.clone()));\n"
        "        let proc_match = match crate::proc_match::ProcMatch::new() {\n"
        "            Ok(proc_match) => Some(Arc::new(proc_match)),\n"
        "            Err(error) => {\n"
        "                tracing::warn!(\"local process matcher unavailable: {error}\");\n"
        "                None\n"
        "            }\n"
        "        };\n"
        "        let (static_nat4_tx, _) = broadcast::channel(16);\n"
        "        Ok(Self {\n"
        "            paths,\n"
        "            backbone,\n"
        "            xdp_attach,\n"
        "            try_native_xdp,\n"
        "            proc_match,\n"
        "            static_nat4_tx,\n"
        "        })\n",
    )

    # 8) Accessor used by the flow dataplane.
    patch(
        "landscape-ebpf/src/runtime.rs",
        "    /// Native XDP attach configuration this runtime was built with.\n"
        "    pub fn try_native_xdp(&self) -> Option<&Vec<i32>> {\n"
        "        self.try_native_xdp.as_ref()\n"
        "    }\n",
        "    /// Native XDP attach configuration this runtime was built with.\n"
        "    pub fn try_native_xdp(&self) -> Option<&Vec<i32>> {\n"
        "        self.try_native_xdp.as_ref()\n"
        "    }\n"
        "\n"
        "    /// Local-process matcher, when cgroup2 attach succeeded.\n"
        "    pub fn proc_match(&self) -> Option<&Arc<crate::proc_match::ProcMatch>> {\n"
        "        self.proc_match.as_ref()\n"
        "    }\n",
    )

    # 9) Rebuild the process maps whenever flow configs change.
    patch(
        "landscape-ebpf/src/runtime_impls.rs",
        "    fn sync_flow_matches(&self, configs: &[landscape_common::flow::RuntimeFlowConfig]) {\n"
        "        if let Err(e) = maps::flow::reconcile_flow_match_map(&self.rt.paths, configs) {\n"
        "            tracing::error!(\"reconcile flow match map error: {e:?}\");\n"
        "        }\n"
        "        // The split decision is cached per flow; drop it so new rules apply now.\n"
        "        maps::nat::flush_fast_caches(&self.rt.paths);\n"
        "    }\n",
        "    fn sync_flow_matches(&self, configs: &[landscape_common::flow::RuntimeFlowConfig]) {\n"
        "        if let Err(e) = maps::flow::reconcile_flow_match_map(&self.rt.paths, configs) {\n"
        "            tracing::error!(\"reconcile flow match map error: {e:?}\");\n"
        "        }\n"
        "        // The split decision is cached per flow; drop it so new rules apply now.\n"
        "        maps::nat::flush_fast_caches(&self.rt.paths);\n"
        "        if let Some(proc_match) = self.rt.proc_match() {\n"
        "            if let Err(e) = proc_match.sync(configs) {\n"
        "                tracing::error!(\"sync local process flow map error: {e:?}\");\n"
        "            }\n"
        "        }\n"
        "    }\n",
    )
