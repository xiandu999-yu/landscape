<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useMessage } from "naive-ui";

import {
  get_fastpath_stats,
  type FastPathStats,
} from "@/api/fastpath";

const message = useMessage();
const loading = ref(false);
const stats = ref<FastPathStats | null>(null);

const egress_hit_rate = computed(() => {
  if (!stats.value) return 0;
  const total = stats.value.egress_hit + stats.value.egress_expired;
  return total === 0 ? 0 : (stats.value.egress_hit * 100) / total;
});

const ingress_hit_rate = computed(() => {
  if (!stats.value) return 0;
  const total = stats.value.ingress_hit + stats.value.ingress_miss;
  return total === 0 ? 0 : (stats.value.ingress_hit * 100) / total;
});

function num(value: number | undefined | null) {
  return (value ?? 0).toLocaleString();
}

async function refresh() {
  loading.value = true;
  try {
    stats.value = await get_fastpath_stats();
    if (stats.value.error) {
      message.warning(stats.value.error);
    }
  } catch (error) {
    console.error("load fast path stats failed", error);
    message.error("读取快速路径统计失败");
  } finally {
    loading.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <div class="fastpath-page">
    <n-flex justify="space-between" align="center" style="margin-bottom: 18px">
      <div>
        <h2 style="margin: 0">快速路径</h2>
        <n-text depth="3">手动刷新，不做自动轮询</n-text>
      </div>
      <n-button type="primary" :loading="loading" @click="refresh">
        刷新
      </n-button>
    </n-flex>

    <n-alert v-if="stats && !stats.available" type="warning" style="margin-bottom: 16px">
      快速路径 map 尚未加载：{{ stats.error ?? "未知错误" }}
    </n-alert>

    <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen" item-responsive>
      <n-gi span="2 s:1">
        <n-card title="出向快速路径（LAN → WAN）" size="small">
          <n-grid :cols="2" :x-gap="12" :y-gap="12">
            <n-gi>
              <n-statistic label="记录数" :value="num(stats?.egress_record)" />
            </n-gi>
            <n-gi>
              <n-statistic label="命中数" :value="num(stats?.egress_hit)" />
            </n-gi>
            <n-gi>
              <n-statistic label="过期数" :value="num(stats?.egress_expired)" />
            </n-gi>
            <n-gi>
              <n-statistic label="命中率" :value="`${egress_hit_rate.toFixed(1)}%`" />
            </n-gi>
          </n-grid>
        </n-card>
      </n-gi>

      <n-gi span="2 s:1">
        <n-card title="入向快速路径（WAN → LAN）" size="small">
          <n-grid :cols="2" :x-gap="12" :y-gap="12">
            <n-gi>
              <n-statistic label="尝试数" :value="num(stats?.ingress_tries)" />
            </n-gi>
            <n-gi>
              <n-statistic label="解析成功数" :value="num(stats?.ingress_parsed)" />
            </n-gi>
            <n-gi>
              <n-statistic label="命中数" :value="num(stats?.ingress_hit)" />
            </n-gi>
            <n-gi>
              <n-statistic label="未命中" :value="num(stats?.ingress_miss)" />
            </n-gi>
            <n-gi>
              <n-statistic label="记录数" :value="num(stats?.ingress_recorded)" />
            </n-gi>
            <n-gi>
              <n-statistic label="过期数" :value="num(stats?.ingress_expired)" />
            </n-gi>
            <n-gi>
              <n-statistic label="命中率" :value="`${ingress_hit_rate.toFixed(1)}%`" />
            </n-gi>
          </n-grid>
        </n-card>
      </n-gi>

      <n-gi span="2">
        <n-card title="流表 / NAT 失败计数" size="small">
          <n-grid :cols="3" :x-gap="12" :y-gap="12" responsive="screen" item-responsive>
            <n-gi span="1 m:1">
              <n-statistic label="内网快速路径流表" :value="num(stats?.flow_core_entries)" />
            </n-gi>
            <n-gi span="1 m:1">
              <n-statistic label="NAT 快速映射表" :value="num(stats?.fa4_fast_entries)" />
            </n-gi>
            <n-gi span="1 m:1">
              <n-statistic label="端口耗尽" :value="num(stats?.napt_port_exhausted)" />
            </n-gi>
            <n-gi span="1 m:1">
              <n-statistic label="映射表未命中" :value="num(stats?.napt_map_miss)" />
            </n-gi>
            <n-gi span="1 m:1">
              <n-statistic label="连接跟踪表满" :value="num(stats?.napt_ct_full)" />
            </n-gi>
            <n-gi span="1 m:1">
              <n-statistic label="连接跟踪表忙" :value="num(stats?.napt_ct_busy)" />
            </n-gi>
          </n-grid>
        </n-card>
      </n-gi>
    </n-grid>
  </div>
</template>

<style scoped>
.fastpath-page {
  padding: 24px;
  width: 100%;
}
</style>
