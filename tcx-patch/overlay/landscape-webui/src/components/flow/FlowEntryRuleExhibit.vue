<script setup lang="ts">
import { computed } from "vue";
import type { FlowEntryRule } from "@landscape-router/types/api/schemas";
import { useEnrolledDeviceStore } from "@/stores/enrolled_device";
import { useI18n } from "vue-i18n";

interface Prop {
  rule: FlowEntryRule;
}

const enrolledDeviceStore = useEnrolledDeviceStore();
const { t } = useI18n();
const props = defineProps<Prop>();

const tagType = computed(() => {
  switch (props.rule.mode.t) {
    case "ip":
      return "info";
    case "device":
      return "success";
    case "mac":
      return "warning";
    default:
      return "default";
  }
});

const label = computed(() => {
  switch (props.rule.mode.t) {
    case "ip":
      return enrolledDeviceStore.GET_NAME_WITH_FALLBACK(
        props.rule.mode.ip,
        `${props.rule.mode.ip}/${props.rule.mode.prefix_len}`,
      );
    case "device":
      return enrolledDeviceStore.GET_DISPLAY_NAME_BY_ID(
        props.rule.mode.device_id,
      );
    case "mac":
      return enrolledDeviceStore.GET_NAME_WITH_FALLBACK(
        props.rule.mode.mac_addr,
      );
    case "local": {
      const filter = props.rule.mode.process;
      if (!filter || filter.names.length === 0) {
        return t("flow.match_rule.local_all");
      }
      const prefix =
        String(filter.mode) === "exclude"
          ? t("flow.match_rule.local_exclude")
          : t("flow.match_rule.local_include");
      return `${prefix}: ${filter.names.join(", ")}`;
    }
    default:
      return "";
  }
});
</script>

<template>
  <n-tag :bordered="false" :type="tagType">
    {{ label }}
  </n-tag>
</template>
