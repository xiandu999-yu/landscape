<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useMessage } from "naive-ui";

import {
  get_iface_qos,
  update_iface_qos,
  type IfaceQosRule,
} from "@/api/fork";

const props = defineProps<{
  iface_name: string;
  direction: "upload" | "download";
}>();

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const enabled = ref(false);
const mbps = ref(0);

function to_mbps(kbit: number) {
  return Math.round((kbit ?? 0) / 1000);
}

async function load() {
  loading.value = true;
  try {
    const rules = await get_iface_qos();
    const rule = rules.find((item) => item.name === props.iface_name);
    enabled.value = rule?.enabled ?? false;
    mbps.value = to_mbps(
      (props.direction === "upload" ? rule?.upload_kbit : rule?.download_kbit) ?? 0,
    );
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
      upload_kbit:
        props.direction === "upload" ? Math.max(0, Math.round(mbps.value * 1000)) : 0,
      download_kbit:
        props.direction === "download" ? Math.max(0, Math.round(mbps.value * 1000)) : 0,
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

onMounted(load);
</script>

<template>
  <n-divider style="margin: 12px 0">独立 QoS（{{ iface_name }}）</n-divider>
  <n-form label-placement="left" label-width="120" :disabled="loading">
    <n-form-item label="启用独立 QoS">
      <n-switch v-model:value="enabled" />
      <n-text depth="3" style="margin-left: 10px">
        仅本网口生效；0 表示回退到全局 QoS 速率
      </n-text>
    </n-form-item>
    <n-form-item
      :label="direction === 'upload' ? '本网口上行 Mbps' : '本网口下行 Mbps'"
    >
      <n-input-number
        v-model:value="mbps"
        :min="0"
        :max="100000"
        :step="10"
        style="width: 180px"
      />
    </n-form-item>
    <n-alert v-if="!enabled" type="info" :bordered="false">
      未启用时使用全局 Fork QoS 配置。
    </n-alert>
    <n-flex justify="end">
      <n-button size="small" type="primary" :loading="saving" @click="save">
        保存网口 QoS
      </n-button>
    </n-flex>
  </n-form>
</template>
