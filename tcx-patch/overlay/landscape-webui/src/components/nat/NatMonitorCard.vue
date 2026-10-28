<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useMessage } from "naive-ui";

import { get_nat_monitor, type NatMonitorResponse } from "@/api/nat_monitor";

const message = useMessage();
const loading = ref(false);
const data = ref<NatMonitorResponse | null>(null);
let timer: ReturnType<typeof setInterval> | undefined;

async function refresh() {
  loading.value = true;
  try {
    data.value = await get_nat_monitor();
  } catch {
    // keep the previous snapshot; the page shows "不可用" when available=false
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void refresh();
  timer = setInterval(() => void refresh(), 5000);
});

onUnmounted(() => {
  if (timer !== undefined) clearInterval(timer);
});

function onManualRefresh() {
  void refresh();
  message.success("已刷新 NAT 计数");
}
</script>

<template>
  <n-card
    size="small"
    :bordered="false"
    style="margin-bottom: 12px; background-color: #f9f9f910"
  >
    <n-flex align="center" justify="space-between" style="margin-bottom: 8px">
      <span style="font-weight: bold">NAT 运行状态（nat_v4）</span>
      <n-button size="tiny" :loading="loading" @click="onManualRefresh">
        刷新
      </n-button>
    </n-flex>

    <n-alert v-if="data !== null && !data.available" type="warning" :bordered="false">
      NAT 计数不可用：请确认 Landscape 自己的 NAT 已启用，且系统里有 `bpftool`。
    </n-alert>

    <n-grid v-else :cols="4" :x-gap="12">
      <n-gi>
        <n-statistic label="端口耗尽" :value="data?.counters.port_exhausted ?? 0" />
      </n-gi>
      <n-gi>
        <n-statistic label="指标丢弃" :value="data?.counters.metric_drop ?? 0" />
      </n-gi>
      <n-gi>
        <n-statistic label="端口保持成功" :value="data?.counters.preserve_ok ?? 0" />
      </n-gi>
      <n-gi>
        <n-statistic label="CT 上限" :value="data?.ct_max_entries ?? 0" />
      </n-gi>
    </n-grid>
  </n-card>
</template>
