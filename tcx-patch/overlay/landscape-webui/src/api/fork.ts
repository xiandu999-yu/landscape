import axios from "axios";
import { applyInterceptors } from "@/api";

export interface ForkConfig {
  qos_enabled: boolean;
  fastpath_monitor: boolean;
  qos_upload_kbit: number;
  qos_download_kbit: number;
  qos_qdisc: "cake" | "fq_codel";
  qos_mode: "htb" | "mq" | "ifb";
  tcx_mode: "auto" | "tcx" | "tc";
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
