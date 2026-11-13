<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useMessage } from "naive-ui";

import {
  get_iface_qos,
  update_iface_qos,
  type IfaceQosRule,
} from "@/api/fork";

const props = defineProps<{
  iface_name: string;
}>();

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const enabled = ref(false);
const upload_mbps = ref(0);
const download_mbps = ref(0);

function to_mbps(kbit: number) {
  return Math.round((kbit ?? 0) / 1000);
}

async function load() {
  loading.value = true;
  try {
    const rules = await get_iface_qos();
    const rule = rules.find((item) => item.name === props.iface_name);
    enabled.value = rule?.enabled ?? false;
    upload_mbps.value = to_mbps(rule?.upload_kbit ?? 0);
    download_mbps.value = to_mbps(rule?.download_kbit ?? 0);
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
    const rule: IfaceQosRule = {
      name: props.iface_name,
      enabled: enabled.value,
      upload_kbit: Math.max(0, Math.round(upload_mbps.value * 1000)),
      download_kbit: Math.max(0, Math.round(download_mbps.value * 1000)),
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
          本网口同时配置上传/下载，类似 SQM
        </n-text>
      </n-form-item>
      <n-form-item label="本网口上行 Mbps">
        <n-input-number
          v-model:value="upload_mbps"
          :min="0"
          :max="100000"
          :step="10"
          :disabled="!enabled"
          style="width: 180px"
        />
      </n-form-item>
      <n-form-item label="本网口下行 Mbps">
        <n-input-number
          v-model:value="download_mbps"
          :min="0"
          :max="100000"
          :step="10"
          :disabled="!enabled"
          style="width: 180px"
        />
      </n-form-item>
      <n-alert type="info" :bordered="false">
        上行在网口 egress 整形；下行通过 ingress redirect 到 ifb-网口 再整形
        （SQM 方式）。与 TCX/传统 TC 无关，快速路径流量同样经过 qdisc。
      </n-alert>
      <n-flex justify="end">
        <n-button size="small" type="primary" :loading="saving" @click="save">
          保存网口 QoS
        </n-button>
      </n-flex>
    </n-form>
</template>
