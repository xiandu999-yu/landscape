<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useMessage } from "naive-ui";

import {
  get_iface_qos,
  update_iface_qos,
  type IfaceQosRule,
} from "@/api/fork";

const props = withDefaults(
  defineProps<{
    iface_name: string;
    direction?: "upload" | "download";
  }>(),
  { direction: "upload" },
);

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const enabled = ref(false);
const rate_mbps = ref(0);
const qdisc = ref<"cake" | "fq_codel">("cake");
const mode = ref<"htb" | "mq" | "cake">("htb");
const mode_locked = computed(() => props.direction === "download" && rate_mbps.value > 0);
watch(rate_mbps, (value) => { if (props.direction === "download" && value > 0) mode.value = "htb"; });

function to_mbps(kbit: number) {
  return Math.round((kbit ?? 0) / 1000);
}

async function load() {
  loading.value = true;
  try {
    const rules = await get_iface_qos();
    const rule = rules.find((item) => item.name === props.iface_name);
    enabled.value = rule?.enabled ?? false;
    rate_mbps.value = to_mbps(
      (props.direction === "upload" ? rule?.upload_kbit : rule?.download_kbit) ?? 0,
    );
    qdisc.value = rule?.qdisc === "fq_codel" ? "fq_codel" : "cake";
    mode.value = rule?.mode === "mq" || rule?.mode === "cake" ? rule.mode : "htb";
  } catch (error) {
    console.error("load iface qos failed", error);
    message.error("读取网口 QoS 失败");
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const rate = Math.max(0, Math.round(rate_mbps.value * 1000));
    const rule: IfaceQosRule = {
      name: props.iface_name,
      enabled: enabled.value,
      upload_kbit: props.direction === "upload" ? rate : 0,
      download_kbit: props.direction === "download" ? rate : 0,
      qdisc: qdisc.value,
      mode:
        props.direction === "download" && rate > 0 ? "htb" : mode.value,
    };
    await update_iface_qos(rule);
    message.success("网口 QoS 已保存");
    await load();
  } catch (error) {
    console.error("save iface qos failed", error);
    message.error("保存网口 QoS 失败");
  } finally {
    saving.value = false;
  }
}

function on_mode_changed() {
  void load();
}

onMounted(() => {
  void load();
  window.addEventListener("landscape-tcx-mode-changed", on_mode_changed);
});
onUnmounted(() => {
  window.removeEventListener("landscape-tcx-mode-changed", on_mode_changed);
});
</script>

<template>
  <n-divider style="margin: 12px 0">独立 QoS（{{ iface_name }}）</n-divider>
  <n-form label-placement="left" label-width="150" :disabled="loading">
    <n-form-item label="启用独立 QoS">
      <n-switch v-model:value="enabled" />
      <n-text depth="3" style="margin-left: 10px">
        只整形转发流量；本机与 LAN-to-LAN 不进入 1:10 限速类
      </n-text>
    </n-form-item>
    <n-form-item
      :label="
        direction === 'upload'
          ? '本网口上行 Mbps（WAN egress）'
          : '本网口下行 Mbps（LAN egress）'
      "
    >
      <n-input-number
        v-model:value="rate_mbps"
        :min="0"
        :max="100000"
        :step="10"
        :disabled="!enabled || (direction === 'download' && mode !== 'htb')"
        style="width: 180px"
      />
    </n-form-item>
    <n-form-item label="队列模式">
      <n-select
        v-model:value="mode"
        :options="[
          { label: 'HTB（限速，仅转发）', value: 'htb' },
          { label: 'MQ（多队列 CAKE，不限速）', value: 'mq' },
          { label: 'CAKE（单队列，不限速）', value: 'cake' },
        ]"
        :disabled="!enabled || mode_locked"
        style="width: 260px"
      />
      <n-text v-if="mode_locked" depth="3" style="margin-left: 10px">
        LAN 下行速率 &gt; 0 时锁定 HTB；设为 0 后可自由选择 MQ / CAKE
      </n-text>
    </n-form-item>
    <n-form-item label="队列规则">
      <n-select
        v-model:value="qdisc"
        :options="[
          { label: 'CAKE（推荐，nat + ecn）', value: 'cake' },
          { label: 'fq_codel（公平队列）', value: 'fq_codel' },
        ]"
        :disabled="!enabled"
        style="width: 260px"
      />
      <n-text depth="3" style="margin-left: 10px">
        CAKE 使用 diffserv4 + triple-isolate + nat + ecn；失败自动回退 fq_codel
      </n-text>
    </n-form-item>
    <n-alert type="info" :bordered="false">
      <template v-if="direction === 'upload'">
        WAN 不锁模式：HTB 只对转发包走 1:10；MQ 每队列 CAKE（可按速率分摊）；
        CAKE 单队列 root CAKE（可带 bandwidth）。没有 LAN-to-LAN 需要排除。
      </template>
      <template v-else>
        LAN 下行速率 > 0 锁 HTB：只对 WAN->LAN 转发包走 1:10，LAN-to-LAN 与本机流量 1:999。
        速率 = 0 时可选 MQ（每队列 CAKE）或 CAKE（单队列 root CAKE），不限速。
      </template>
    </n-alert>
    <n-flex justify="end">
      <n-button size="small" type="primary" :loading="saving" @click="save">
        保存网口 QoS
      </n-button>
    </n-flex>
  </n-form>
</template>
