import axios from "axios";
import { applyInterceptors } from "@/api";

export interface NatOptions {
  full_cone: boolean;
  tcp_syn_timeout_secs: number;
  tcp_timeout_secs: number;
  udp_timeout_secs: number;
  icmp_timeout_secs: number;
}

const natApi = applyInterceptors(
  axios.create({ baseURL: "/api/v1/system", timeout: 30000 }),
);

/** GET /api/v1/system/nat/options */
export async function get_nat_options(): Promise<NatOptions> {
  const resp = (await natApi.get("/nat/options")) as unknown as {
    data: NatOptions;
  };
  return resp.data;
}

/** PUT /api/v1/system/nat/options */
export async function update_nat_options(options: NatOptions): Promise<void> {
  await natApi.put("/nat/options", options);
}
