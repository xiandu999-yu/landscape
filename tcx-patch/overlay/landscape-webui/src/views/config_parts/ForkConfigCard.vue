<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useMessage } from "naive-ui";

import ForkQosModal from "@/views/config_parts/ForkQosModal.vue";
import {
  get_fork_config,
  update_fork_config,
  type ForkConfig,
} from "@/api/fork";

const message = useMessage();
const loading = ref(false);
const saving = ref(false);
const show_qos = ref(false);
const config = ref<ForkConfig>({
  qos_enabled: false,
  fastpath_monitor: true,
  qos_upload_kbit: 0,
  qos_download_kbit: 0,
  qos_qdisc: "cake",
});

async function load() {
  loading.value = true;
  try {
    config.value = await get_fork_config();
  } catch (error) {
    console.error("load fork config failed", error);
    message.error("读取 Fork 配置失败");
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    await update_fork_config(config.value);
    message.success("Fork 配置已保存");
  } catch (error) {
    console.error("save fork config failed", error);
    message.error("保存 Fork 配置失败");
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <n-card id="fork-config" title="Fork 功能" size="small" :bordered="false">
    <n-spin :show="loading">
      <n-flex vertical>
        <n-alert type="info" :bordered="false">
          此区域只放运行时能生效的开关。TCX 模式、Full Cone、ingress fast
          path 等需要重挂 NAT/重启链路的选项不放在这里。
        </n-alert>

        <n-form label-placement="left" label-width="180">
          <n-form-item label="QoS 转发限速">
            <n-switch v-model:value="config.qos_enabled" />
            <n-button
              size="small"
              ghost
              type="primary"
              style="margin-left: 12px"
              @click="show_qos = true"
            >
              QoS 配置
            </n-button>
            <n-text depth="3" style="margin-left: 10px">
              仅作用于 Landscape 转发流；本机和 LAN-to-LAN 不进入 QoS
            </n-text>
            <n-text depth="3" style="margin-left: 10px">
              仅支持单 WAN；多 LAN 会全部应用同一下行策略
            </n-text>
          </n-form-item>
          <n-form-item label="快速路径监控">
            <n-switch v-model:value="config.fastpath_monitor" />
            <n-text depth="3" style="margin-left: 10px">
              在“监控分析 -> 快速路径”显示计数器
            </n-text>
          </n-form-item>
        </n-form>

        <n-flex justify="end">
          <n-button type="primary" :loading="saving" @click="save">
            保存设置
          </n-button>
        </n-flex>
      </n-flex>
    </n-spin>

    <ForkQosModal v-model:show="show_qos" @saved="load" />
  </n-card>
</template>
