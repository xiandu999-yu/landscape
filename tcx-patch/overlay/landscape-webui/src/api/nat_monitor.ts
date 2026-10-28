import axios from "axios";
import { applyInterceptors } from "@/api";

export interface NatCounters {
  port_exhausted: number;
  metric_drop: number;
  preserve_ok: number;
}

export interface NatMonitorResponse {
  available: boolean;
  counters: NatCounters;
  ct_max_entries: number;
  raw: string;
}

const natApi = applyInterceptors(
  axios.create({ baseURL: "/api/v1/system", timeout: 15000 }),
);

/** GET /api/v1/system/nat/monitor */
export async function get_nat_monitor(): Promise<NatMonitorResponse> {
  const resp = (await natApi.get("/nat/monitor")) as unknown as {
    data: NatMonitorResponse;
  };
  return resp.data;
}
