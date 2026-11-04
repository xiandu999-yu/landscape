<script setup lang="ts">
import { computed, reactive } from "vue";
import type { FlowEntryRule } from "@landscape-router/types/api/schemas";
import { useFrontEndStore } from "@/stores/front_end_config";
import { useEnrolledDeviceStore } from "@/stores/enrolled_device";
import { is_ipv4, is_ipv6 } from "@/lib/common";
import { formatMacAddress } from "@/lib/util";
import { useI18n } from "vue-i18n";

const frontEndStore = useFrontEndStore();
const enrolledDeviceStore = useEnrolledDeviceStore();
const { t } = useI18n();
const match_rules = defineModel<any[]>("match_rules", {
  required: true,
});

type InputMode = "device" | "mac" | "ip" | "local";
const inputModes = reactive(new Map<number, InputMode>());

function mode_of(rule: any): InputMode {
  switch (rule?.mode.t) {
    case "ip":
      return "ip";
    case "local":
      return "local";
    case "device":
      return "device";
    default:
      return "mac";
  }
}

function getInputMode(index: number): InputMode {
  return inputModes.get(index) ?? mode_of(match_rules.value[index]);
}

const modeOptions = computed(() => [
  { label: t("flow.match_rule.mode_device"), value: "device" },
  { label: t("flow.match_rule.mode_mac"), value: "mac" },
  { label: t("flow.match_rule.mode_ip"), value: "ip" },
  { label: t("flow.match_rule.mode_local"), value: "local" },
]);

const processModeOptions = computed(() => [
  { label: t("flow.match_rule.local_whitelist"), value: "whitelist" },
  { label: t("flow.match_rule.local_blacklist"), value: "blacklist" },
]);

const deviceOptions = computed(() =>
  enrolledDeviceStore.bindings.map((d) => ({
    label: d.name,
    value: d.id!,
  })),
);

function onCreate(): FlowEntryRule {
  const index = match_rules.value.length;
  inputModes.set(index, "device");
  return {
    qos: null,
    mode: {
      t: "device",
      device_id: "",
    },
  };
}

function getDefaultPrefixLen(ip: string): number {
  if (is_ipv6(ip) || ip.includes(":")) {
    return 128;
  }
  if (is_ipv4(ip)) {
    return 32;
  }
  return 32;
}

function set_mode(value: any, index: number, mode: InputMode) {
  inputModes.set(index, mode);
  const qos = value.qos;
  if (mode === "device") {
    value.mode = { t: "device", device_id: "" };
  } else if (mode === "mac") {
    value.mode = { t: "mac", mac_addr: "" };
  } else if (mode === "ip") {
    value.mode = { t: "ip", ip: "", prefix_len: 32 };
  } else {
    value.mode = {
      t: "local",
      blacklist: false,
      bypass_process: [],
    };
  }
  value.qos = qos;
}

function local_filter(value: any): {
  blacklist: boolean;
  bypass_process: string[];
} {
  if (value.mode.t !== "local") {
    return { blacklist: false, bypass_process: [] };
  }
  return {
    blacklist: !!value.mode.blacklist,
    bypass_process: value.mode.bypass_process ?? [],
  };
}

function set_process_blacklist(value: any, blacklist: boolean) {
  const filter = local_filter(value);
  value.mode = {
    t: "local",
    blacklist,
    bypass_process: filter.bypass_process,
  };
}

function set_process_names(value: any, names: string[]) {
  const filter = local_filter(value);
  value.mode = {
    t: "local",
    blacklist: filter.blacklist,
    bypass_process: names,
  };
}

function add_process(value: any) {
  const filter = local_filter(value);
  set_process_names(value, [...filter.bypass_process, ""]);
}

function remove_process(value: any, index: number) {
  const filter = local_filter(value);
  set_process_names(
    value,
    filter.bypass_process.filter((_, i) => i !== index),
  );
}

function set_process_name(value: any, index: number, name: string) {
  const filter = local_filter(value);
  const names = [...filter.bypass_process];
  names[index] = name;
  set_process_names(value, names);
}
</script>

<template>
  <n-dynamic-input v-model:value="match_rules" :on-create="onCreate">
    <template #create-button-default>
      {{ t("flow.match_rule.add_entry_rule") }}
    </template>
    <template #default="{ value, index }">
      <n-flex style="flex: 1" :wrap="false">
        <n-select
          :style="{ width: '120px' }"
          :value="getInputMode(index)"
          :options="modeOptions"
          @update:value="(v: any) => set_mode(value, index, v)"
        />

        <n-select
          v-if="getInputMode(index) === 'device'"
          :options="deviceOptions"
          :value="
            value.mode.t === 'device' ? value.mode.device_id || null : null
          "
          @update:value="
            (v: string) => {
              value.mode = { t: 'device', device_id: v };
            }
          "
          :placeholder="t('flow.match_rule.select_device_placeholder')"
          clearable
          filterable
          :style="{ minWidth: '140px', flex: 1 }"
        />
        <n-input
          v-else-if="getInputMode(index) === 'mac'"
          :type="frontEndStore.presentation_mode ? 'password' : 'text'"
          :value="value.mode.t === 'mac' ? value.mode.mac_addr : ''"
          @update:value="
            (v: string) => {
              value.mode = { t: 'mac', mac_addr: formatMacAddress(v) };
            }
          "
          :placeholder="t('flow.match_rule.mac_placeholder')"
        />
        <n-input-group v-else-if="getInputMode(index) === 'ip'">
          <n-input
            :type="frontEndStore.presentation_mode ? 'password' : 'text'"
            :value="value.mode.t === 'ip' ? value.mode.ip : ''"
            @update:value="
              (v: string) => {
                const prefixLen =
                  value.mode.t === 'ip'
                    ? value.mode.ip === '' ||
                      value.mode.prefix_len ===
                        getDefaultPrefixLen(value.mode.ip)
                      ? getDefaultPrefixLen(v)
                      : value.mode.prefix_len
                    : getDefaultPrefixLen(v);
                value.mode = { t: 'ip', ip: v, prefix_len: prefixLen };
              }
            "
            :placeholder="t('flow.match_rule.ip_placeholder')"
          />
          <n-input-group-label>/</n-input-group-label>
          <n-input-number
            :value="
              value.mode.t === 'ip'
                ? value.mode.prefix_len
                : getDefaultPrefixLen('')
            "
            @update:value="
              (v: number | null) => {
                const ip = value.mode.t === 'ip' ? value.mode.ip : '';
                value.mode = {
                  t: 'ip',
                  ip,
                  prefix_len: v ?? getDefaultPrefixLen(ip),
                };
              }
            "
            :style="{ width: '60px' }"
            :placeholder="t('flow.match_rule.prefix_placeholder')"
            :show-button="false"
          />
        </n-input-group>
        <n-flex v-else vertical style="flex: 1" :size="[6, 6]">
          <n-select
            size="small"
            :style="{ width: '104px' }"
            :value="local_filter(value).blacklist ? 'blacklist' : 'whitelist'"
            :options="processModeOptions"
            :title="t('flow.match_rule.local_hint')"
            @update:value="
              (v: any) => set_process_blacklist(value, v === 'blacklist')
            "
          />
          <n-flex
            v-if="local_filter(value).bypass_process.length"
            vertical
            :size="[4, 4]"
          >
            <n-flex
              v-for="(name, pindex) in local_filter(value).bypass_process"
              :key="pindex"
              :wrap="false"
              :size="[4, 0]"
            >
              <n-input
                size="small"
                style="flex: 1"
                :value="name"
                :placeholder="t('flow.match_rule.process_placeholder')"
                @update:value="
                  (v: string) => set_process_name(value, pindex, v)
                "
              />
              <n-button
                size="small"
                quaternary
                @click="remove_process(value, pindex)"
              >
                −
              </n-button>
            </n-flex>
          </n-flex>
          <n-button
            size="small"
            dashed
            style="align-self: flex-start"
            @click="add_process(value)"
          >
            + {{ t("flow.match_rule.add_process") }}
          </n-button>
        </n-flex>
      </n-flex>
    </template>
  </n-dynamic-input>
</template>
