import axios from "axios";
import { applyInterceptors } from "@/api";

export interface IfaceQosRule {
  name: string;
  enabled: boolean;
  upload_kbit: number;
  download_kbit: number;
}

export interface ForkConfig {
  qos_enabled: boolean;
  fastpath_monitor: boolean;
  qos_upload_kbit: number;
  qos_download_kbit: number;
  qos_qdisc: "cake" | "fq_codel";
  qos_mode: "htb" | "mq" | "ifb";
  tcx_mode: "auto" | "tcx" | "tc";
  iface_qos: IfaceQosRule[];
}

const forkApi = applyInterceptors(
  axios.create({ baseURL: "/api/v1/system", timeout: 30000 }),
);

export async function get_fork_config(): Promise<ForkConfig> {
  const resp = (await forkApi.get("/fork/config")) as unknown as {
    data: ForkConfig;
  };
  return resp.data;
}

export async function update_fork_config(config: ForkConfig): Promise<void> {
  await forkApi.put("/fork/config", config);
}

export async function get_iface_qos(): Promise<IfaceQosRule[]> {
  const resp = (await forkApi.get("/fork/iface-qos")) as unknown as {
    data: IfaceQosRule[];
  };
  return resp.data;
}

export async function update_iface_qos(rule: IfaceQosRule): Promise<void> {
  await forkApi.put("/fork/iface-qos", rule);
}
