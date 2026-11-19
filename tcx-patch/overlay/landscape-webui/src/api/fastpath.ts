import axios from "axios";
import { applyInterceptors } from "@/api";

export interface FastPathStats {
  available: boolean;
  error?: string | null;
  egress_record: number;
  egress_hit: number;
  egress_expired: number;
  ingress_recorded: number;
  ingress_hit: number;
  ingress_expired: number;
  ingress_tries: number;
  ingress_parsed: number;
  ingress_miss: number;
  flow_core_entries: number;
  fa4_fast_entries: number;
  napt_port_exhausted: number;
  napt_map_miss: number;
  napt_ct_full: number;
  napt_ct_busy: number;
}

const fastpathApi = applyInterceptors(
  axios.create({ baseURL: "/api/v1/system", timeout: 30000 }),
);

export async function get_fastpath_stats(): Promise<FastPathStats> {
  const resp = (await fastpathApi.get("/fastpath/stats")) as unknown as {
    data: FastPathStats;
  };
  return resp.data;
}
