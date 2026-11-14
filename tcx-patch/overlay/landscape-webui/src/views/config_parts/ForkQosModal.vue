<script setup lang="ts">
import { ref } from "vue";
import { useMessage } from "naive-ui";

import {
  get_iface_qos,
  get_qos_targets,
  update_iface_qos,
  type IfaceQosRule,
  type QosTarget,
} from "@/api/fork";

const show_model = defineModel<boolean>("show", { required: true });
const emit = defineEmits<{ saved: [] }>();
const message = useMessage();

const loading = ref(false);
const saving = ref(false);
const iface_edits = ref<
  Array<
    QosTarget & {
      enabled: boolean;
      upload_mbps: number;
      download_mbps: number;
      qdisc: "cake" | "fq_codel";
      mode: "htb" | "mq" | "ifb";
    }
  >
>([]);

function to_mbps(kbit: number) {
  return Math.round((kbit ?? 0) / 1000);
}

async function load() {
  loading.value = true;
  try {
    const [targets, rules] = await Promise.all([
      get_qos_targets(),
      get_iface_qos(),
    ]);
    const by_name = new Map<string, IfaceQosRule>(
      rules.map((rule) => [rule.name, rule]),
    );
    iface_edits.value = targets.map((target) => {
      const rule = by_name.get(target.name);
      return {
        ...target,
        enabled: rule?.enabled ?? false,
        upload_mbps: to_mbps(rule?.upload_kbit ?? 0),
        download_mbps: to_mbps(rule?.download_kbit ?? 0),
        qdisc: rule?.qdisc ?? "cake",
        mode: rule?.mode ?? "htb",
      };
    });
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
    if (iface_edits.value.length === 0) {
      message.error("没有可配置的 WAN/LAN 网口");
      return;
    }
    for (const edit of iface_edits.value) {
      await update_iface_qos({
        name: edit.name,
        enabled: edit.enabled,
        upload_kbit: Math.max(0, Math.round(edit.upload_mbps * 1000)),
        download_kbit: Math.max(0, Math.round(edit.download_mbps * 1000)),
        qdisc: edit.qdisc,
        mode: edit.mode,
      });
    }
    message.success("QoS 已保存");
    emit("saved");
    show_model.value = false;
  } catch (error) {
    console.error("save fork qos failed", error);
    message.error("保存 QoS 失败");
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <n-modal v-model:show="show_model" :auto-focus="false" @after-enter="load">
    <n-card
      style="width: 640px"
      title="QoS 转发限速（SQM）"
      :bordered="false"
      size="small"
      role="dialog"
      aria-modal="true"
    >
      <n-spin :show="loading">
        <n-flex vertical>
          <n-alert type="info" :bordered="false">
            每个网口独立设置上传/下行；上传在本网口 egress 整形，下行通过 ingress
            redirect 到 ifb-网口 再整形。和 TCX/传统 TC 无关，快速路径流量同样经过
            qdisc。
          </n-alert>

          <n-form label-placement="left" label-width="150" :disabled="loading">
            <n-form-item
              v-for="edit in iface_edits"
              :key="edit.name"
              :label="`${edit.name}（${edit.zone.toUpperCase()}）`"
            >
              <n-switch v-model:value="edit.enabled" />
              <n-text depth="3" style="margin-left: 12px">上行</n-text>
              <n-input-number
                v-model:value="edit.upload_mbps"
                :min="0"
                :max="100000"
                :step="10"
                :disabled="!edit.enabled"
                style="width: 110px; margin-left: 6px"
              />
              <n-text depth="3" style="margin-left: 12px">下行</n-text>
              <n-input-number
                v-model:value="edit.download_mbps"
                :min="0"
                :max="100000"
                :step="10"
                :disabled="!edit.enabled"
                style="width: 110px; margin-left: 6px"
              />
              <n-text depth="3" style="margin-left: 6px">Mbps</n-text>
            </n-form-item>
          </n-form>
        </n-flex>
      </n-spin>

      <template #footer>
        <n-flex justify="end" :size="12">
          <n-button @click="show_model = false">取消</n-button>
          <n-button type="primary" :loading="saving" @click="save">
            保存 QoS
          </n-button>
        </n-flex>
      </template>
    </n-card>
  </n-modal>
</template>
