<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useMessage } from "naive-ui";

import {
  get_tcx_order,
  update_tcx_order,
  type TcxIfaceRule,
} from "@/api/tcx_order";

const props = withDefaults(
  defineProps<{
    iface_name: string;
    egress?: boolean;
  }>(),
  { egress: false },
);

const message = useMessage();
const loading = ref(false);
const saving = ref(false);

type Mode = "inherit" | "first" | "last" | "position" | "before" | "after";

const mode_options = [
  { label: "继承全局", value: "inherit" },
  { label: "链首 (first)", value: "first" },
  { label: "链尾 (last)", value: "last" },
  { label: "指定位置 (position)", value: "position" },
  { label: "在程序之前 (before)", value: "before" },
  { label: "在程序之后 (after)", value: "after" },
];

const ingress_mode = ref<Mode>("inherit");
const ingress_pos = ref<number>(0);
const ingress_prog = ref<string>("");
const egress_mode = ref<Mode>("inherit");
const egress_pos = ref<number>(0);
const egress_prog = ref<string>("");
const global_order = ref<string>("last");

function parse_spec(
  spec: string | undefined,
): [Mode, number, string] {
  const v = (spec ?? "").trim();
  if (v === "") return ["inherit", 0, ""];
  const lower = v.toLowerCase();
  if (lower === "first") return ["first", 0, ""];
  if (lower === "last") return ["last", 0, ""];
  if (lower.startsWith("position:")) {
    const n = Number.parseInt(lower.slice("position:".length), 10);
    return ["position", Number.isFinite(n) && n >= 0 ? n : 0, ""];
  }
  if (lower.startsWith("before:")) return ["before", 0, v.slice("before:".length)];
  if (lower.startsWith("after:")) return ["after", 0, v.slice("after:".length)];
  return ["inherit", 0, ""];
}

function compose_spec(mode: Mode, pos: number, prog: string): string {
  const p = prog.trim();
  switch (mode) {
    case "first":
    case "last":
      return mode;
    case "position":
      return Number.isFinite(pos) && pos >= 0 ? `position:${pos}` : "";
    case "before":
    case "after":
      return p === "" ? "" : `${mode}:${p}`;
    default:
      return "";
  }
}

async function load() {
  loading.value = true;
  try {
    const resp = await get_tcx_order();
    global_order.value = resp.global_order;
    const rule = resp.rules.find((r) => r.name === props.iface_name);
    const [im, ip, iprog] = parse_spec(rule?.ingress);
    ingress_mode.value = im;
    ingress_pos.value = ip;
    ingress_prog.value = iprog;
    const [em, ep, eprog] = parse_spec(rule?.egress);
    egress_mode.value = em;
    egress_pos.value = ep;
    egress_prog.value = eprog;
  } catch {
    // keep defaults; the surrounding modal reports its own errors
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const resp = await get_tcx_order();
    const rules: TcxIfaceRule[] = resp.rules.filter(
      (r) => r.name !== props.iface_name,
    );
    const ingress = compose_spec(
      ingress_mode.value,
      ingress_pos.value,
      ingress_prog.value,
    );
    const egress = props.egress
      ? compose_spec(egress_mode.value, egress_pos.value, egress_prog.value)
      : "";
    if (ingress !== "" || egress !== "") {
      rules.push({ name: props.iface_name, ingress, egress });
    }
    await update_tcx_order(rules);
    message.success(
      "TCX 顺序已保存；重启该接口的路由转发服务（重新点“更新”）后生效",
    );
  } catch {
    message.error("保存 TCX 顺序失败");
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <n-divider style="margin: 12px 0">TCX 顺序（{{ iface_name }}）</n-divider>
  <n-form label-placement="left" label-width="120" :disabled="loading">
    <n-form-item label="Ingress 顺序">
      <n-select
        v-model:value="ingress_mode"
        :options="mode_options"
        style="width: 220px"
      />
      <n-input-number
        v-if="ingress_mode === 'position'"
        v-model:value="ingress_pos"
        :min="0"
        placeholder="下标"
        style="width: 110px; margin-left: 8px"
      />
      <n-input
        v-if="ingress_mode === 'before' || ingress_mode === 'after'"
        v-model:value="ingress_prog"
        placeholder="程序名，如 egress_snat"
        style="width: 220px; margin-left: 8px"
      />
    </n-form-item>

    <n-form-item v-if="egress" label="Egress 顺序">
      <n-select
        v-model:value="egress_mode"
        :options="mode_options"
        style="width: 220px"
      />
      <n-input-number
        v-if="egress_mode === 'position'"
        v-model:value="egress_pos"
        :min="0"
        placeholder="下标"
        style="width: 110px; margin-left: 8px"
      />
      <n-input
        v-if="egress_mode === 'before' || egress_mode === 'after'"
        v-model:value="egress_prog"
        placeholder="程序名，如 egress_snat"
        style="width: 220px; margin-left: 8px"
      />
    </n-form-item>

    <n-form-item label="全局默认">
      <n-text depth="3">当前 {{ global_order }}（未单独配置的接口使用它）</n-text>
    </n-form-item>

    <n-flex justify="end">
      <n-button size="small" type="primary" :loading="saving" @click="save">
        保存 TCX 顺序
      </n-button>
    </n-flex>
  </n-form>
</template>
