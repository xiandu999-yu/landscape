<script setup lang="ts">
import { ref } from "vue";
import { useMessage } from "naive-ui";

import { get_fork_config, update_fork_config, type ForkConfig } from "@/api/fork";
import { get_tcx_order } from "@/api/tcx_order";

const show_model = defineModel<boolean>("show", { required: true });
const emit = defineEmits<{ saved: [] }>();
const message = useMessage();

const loading = ref(false);
const saving = ref(false);
const upload_mbps = ref(0);
const download_mbps = ref(0);
const qdisc = ref<ForkConfig["qos_qdisc"]>("cake");
const mode = ref<ForkConfig["qos_mode"]>("htb");
const tcx_mode = ref("auto");

function to_mbps(kbit: number) {
  return Math.round((kbit ?? 0) / 1000);
}

async function load() {
  loading.value = true;
  try {
    const config = await get_fork_config();
    upload_mbps.value = to_mbps(config.qos_upload_kbit);
    download_mbps.value = to_mbps(config.qos_download_kbit);
    qdisc.value = config.qos_qdisc === "fq_codel" ? "fq_codel" : "cake";
    mode.value = config.qos_mode ?? "htb";
    tcx_mode.value = (await get_tcx_order()).mode ?? "auto";
  } catch (error) {
    console.error("load fork qos failed", error);
    message.error("读取 QoS 配置失败");
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const current = await get_fork_config();
    await update_fork_config({
      ...current,
      qos_upload_kbit: Math.max(0, Math.round(upload_mbps.value * 1000)),
      qos_download_kbit: Math.max(0, Math.round(download_mbps.value * 1000)),
      qos_qdisc: qdisc.value,
      qos_mode: mode.value,
    });
    message.success("QoS 配置已保存");
    emit("saved");
    show_model.value = false;
  } catch (error) {
    console.error("save fork qos failed", error);
    message.error("保存 QoS 配置失败");
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <n-modal v-model:show="show_model" :auto-focus="false" @after-enter="load">
    <n-card
      style="width: 560px"
      title="QoS 转发限速"
      :bordered="false"
      size="small"
      role="dialog"
      aria-modal="true"
    >
      <n-spin :show="loading">
        <n-flex vertical>
          <n-alert v-if="tcx_mode === 'tc'" type="warning" :bordered="false">
            TCX 模式当前是 TC（传统 clsact）。QoS 依赖 TCX，TC 模式下无法启用，
            请先在“路由转发服务 -> TCX 模式”改为 Auto 或 TCX。
          </n-alert>
          <n-alert type="info" :bordered="false">
            只作用于 Landscape 转发的流量。本机、LAN-to-LAN 不进入 QoS。
            当前仅支持单 WAN；多 LAN 会全部应用同一下行策略。
            整形全部交给 qdisc，eBPF 只标记转发包，不做 token bucket 丢包。
            HTB 精确但单队列；MQ 保留硬件多队列；IFB 实验模式。
            数值单位 Mbps；0 表示不限制。
          </n-alert>

          <n-form label-placement="left" label-width="130">
            <n-form-item label="全局上行 Mbps">
              <n-input-number
                v-model:value="upload_mbps"
                :min="0"
                :max="100000"
                :step="10"
                style="width: 180px"
              />
            </n-form-item>
            <n-form-item label="全局下行 Mbps">
              <n-input-number
                v-model:value="download_mbps"
                :min="0"
                :max="100000"
                :step="10"
                style="width: 180px"
              />
            </n-form-item>
            <n-form-item label="队列模式">
              <n-select
                v-model:value="mode"
                :options="[
                  { label: 'HTB 单队列（精确）', value: 'htb' },
                  { label: 'MQ 多队列（保留硬件队列）', value: 'mq' },
                  { label: 'IFB 全局（实验）', value: 'ifb' },
                ]"
                style="width: 280px"
              />
            </n-form-item>
            <n-form-item label="队列算法">
              <n-select
                v-model:value="qdisc"
                :options="[
                  { label: 'CAKE（推荐）', value: 'cake' },
                  { label: 'fq_codel（仅公平队列）', value: 'fq_codel' },
                ]"
                style="width: 280px"
              />
            </n-form-item>
          </n-form>
        </n-flex>
      </n-spin>

      <template #footer>
        <n-flex justify="end" :size="12">
          <n-button @click="show_model = false">取消</n-button>
          <n-button
            type="primary"
            :loading="saving"
            :disabled="tcx_mode === 'tc'"
            @click="save"
          >
            保存 QoS
          </n-button>
        </n-flex>
      </template>
    </n-card>
  </n-modal>
</template>
