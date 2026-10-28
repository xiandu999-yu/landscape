import axios from "axios";
import { applyInterceptors } from "@/api";

export interface TcxIfaceRule {
  name: string;
  ingress: string;
  egress: string;
}

export interface TcxOrderResponse {
  rules: TcxIfaceRule[];
  ifaces: string[];
  global_order: string;
  mode: string;
}

const tcxApi = applyInterceptors(
  axios.create({ baseURL: "/api/v1/system", timeout: 30000 }),
);

/** GET /api/v1/system/tcx/order */
export async function get_tcx_order(): Promise<TcxOrderResponse> {
  const resp = (await tcxApi.get("/tcx/order")) as unknown as {
    data: TcxOrderResponse;
  };
  return resp.data;
}

/** PUT /api/v1/system/tcx/order */
export async function update_tcx_order(rules: TcxIfaceRule[]): Promise<void> {
  await tcxApi.put("/tcx/order", { rules });
}
