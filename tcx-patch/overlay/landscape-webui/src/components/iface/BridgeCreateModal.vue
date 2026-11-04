<script setup lang="ts">
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  create_bridge,
  create_socks5_virtual,
  delete_socks5_virtual,
  get_socks5_virtual,
  type Socks5LinkKind,
  type Socks5VirtualConfig,
} from "@/api/network";
import { useIfaceNodeStore } from "@/stores/iface_node";

const showModal = defineModel<boolean>("show", { required: true });
const ifaceStore = useIfaceNodeStore();
const { t } = useI18n();

const bridge_name = ref("");
const socks5 = ref<Socks5VirtualConfig | null>(null);
const socks5_loading = ref(false);
const socks5_form = ref({
  software: "landscape",
  server: "",
  port: 1080 as number | null,
  username: "",
  password: "",
  kind: "auto" as string,
});

const kind_options = computed(() => [
  { label: t("interface.socks5_kind_auto"), value: "auto" },
  { label: t("interface.socks5_kind_veth"), value: "veth" },
  { label: t("interface.socks5_kind_netkit"), value: "netkit" },
  { label: t("interface.socks5_kind_netkit_l3"), value: "netkit_l3" },
]);

const software_options = [
  { label: "landscape", value: "landscape" },
  { label: "mihomo", value: "mihomo" },
  { label: "sing-box", value: "sing-box" },
  { label: "daed", value: "daed" },
  { label: "dae", value: "dae" },
];

async function load_socks5() {
  socks5.value = await get_socks5_virtual();
}

async function add_socks5() {
  const software = (socks5_form.value.software ?? "").trim();
  if (!software) {
    window.$message?.warning(t("interface.socks5_software_required"));
    return;
  }
  if (!socks5_form.value.server.trim()) {
    window.$message?.warning(t("interface.socks5_server_required"));
    return;
  }
  socks5_loading.value = true;
  try {
    const config = await create_socks5_virtual({
      software,
      server: socks5_form.value.server.trim(),
      port: socks5_form.value.port ?? 0,
      username: socks5_form.value.username.trim() || null,
      password: socks5_form.value.password || null,
      kind: socks5_form.value.kind as Socks5LinkKind,
    });
    socks5.value = config;
    await ifaceStore.UPDATE_INFO();
    window.$message?.success(t("interface.socks5_created"));
  } finally {
    socks5_loading.value = false;
  }
}

async function remove_socks5() {
  await delete_socks5_virtual();
  socks5.value = null;
  await ifaceStore.UPDATE_INFO();
  window.$message?.success(t("interface.socks5_deleted"));
}

async function add_bridge() {
  await create_bridge(bridge_name.value);
  ifaceStore.UPDATE_INFO();
  showModal.value = false;
  bridge_name.value = "";
}
</script>

<template>
  <n-modal
    v-model:show="showModal"
    preset="card"
    style="width: 620px"
    :title="t('interface.iface.bridge')"
    @after-enter="load_socks5"
  >
    <n-flex vertical :size="[12, 12]">
      <n-input-group>
        <n-input
          v-model:value="bridge_name"
          :placeholder="t('interface.iface.bridge_name')"
        />
        <n-button type="primary" @click="add_bridge">
          {{ t("interface.iface.create_bridge") }}
        </n-button>
      </n-input-group>

      <n-divider style="margin: 4px 0">
        {{ t("interface.socks5_title") }}
      </n-divider>

      <n-alert v-if="socks5" type="success" :show-icon="false">
        <n-flex justify="space-between" align="center">
          <span>
            {{
              t("interface.socks5_exists", {
                software: socks5.software,
                server: socks5.server,
                port: socks5.port,
              })
            }}
          </span>
          <n-popconfirm @positive-click="remove_socks5">
            <template #trigger>
              <n-button size="small" type="error" quaternary>
                {{ t("interface.socks5_delete") }}
              </n-button>
            </template>
            {{ t("interface.socks5_delete_confirm") }}
          </n-popconfirm>
        </n-flex>
      </n-alert>

      <n-form v-else label-placement="left" label-width="108" :show-feedback="false">
        <n-grid :cols="2" :x-gap="12">
          <n-form-item-gi :label="t('interface.socks5_software')">
            <n-select
              v-model:value="socks5_form.software"
              :options="software_options"
              filterable
              tag
              placeholder="landscape / mihomo / sing-box"
            />
          </n-form-item-gi>
          <n-form-item-gi :label="t('interface.socks5_kind')">
            <n-select
              v-model:value="socks5_form.kind"
              :options="kind_options"
            />
          </n-form-item-gi>
          <n-form-item-gi :label="t('interface.socks5_server')">
            <n-input
              v-model:value="socks5_form.server"
              placeholder="127.0.0.1"
            />
          </n-form-item-gi>
          <n-form-item-gi :label="t('interface.socks5_port')">
            <n-input-number
              v-model:value="socks5_form.port"
              :min="1"
              :max="65535"
              style="width: 100%"
            />
          </n-form-item-gi>
          <n-form-item-gi :label="t('interface.socks5_username')">
            <n-input v-model:value="socks5_form.username" />
          </n-form-item-gi>
          <n-form-item-gi :label="t('interface.socks5_password')">
            <n-input
              v-model:value="socks5_form.password"
              type="password"
              show-password-on="click"
            />
          </n-form-item-gi>
        </n-grid>
        <n-flex justify="space-between" align="center">
          <n-text depth="3" style="font-size: 12px">
            {{ t("interface.socks5_name_hint") }}
          </n-text>
          <n-button
            type="primary"
            :loading="socks5_loading"
            @click="add_socks5"
          >
            {{ t("interface.socks5_create") }}
          </n-button>
        </n-flex>
      </n-form>
    </n-flex>
  </n-modal>
</template>
