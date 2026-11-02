# 端口保持移除 + WAN ingress 兜底修复 + 指标热切换 + fast path + 合并 stage —— 替换说明

这一版把 **端口保持** 整段删掉了（数据面 + 配置 + UI），并保留了之前的
UDP 定时器修复和 einat 入向兜底。

> **新增（2026-10-30）合并 stage（实验，默认关闭）**：
> - `LANDSCAPE_NAT_MERGE_MSS=1`：不再挂 `tc_mss` 阶段，NAT 程序自己钳 MSS，
>   MTU 通过 pinned `land_mss_cfg`（ifindex → mtu）传递；
> - `LANDSCAPE_NAT_MERGE_FIREWALL=1`：只跳过 **v4** 防火墙阶段，NAT 程序查同一张
>   pinned `firewall_block_ip4_map`（出向查 dst、入向查 src），v6 阶段照常挂。
> 两者合计每包省 3~4µs。**利害**：v4 防火墙从此依赖 NAT 程序存在（NAT 停掉 = v4 黑名单失效），
> 且只覆盖 IPv4；细节见 `tcx-patch/README.md` 的「合并 v4 防火墙」一节。

> **新增（2026-10-30）出向 NAT fast path（实验，默认关闭）**：类比 xt_FLOWOFFLOAD / SFE，
> `tc_v4_from_lan` 完成 NAT 后把决策写进 `fa4_fast`，`tc_wan_egress_route` 命中后直接改写 +
> `TC_ACT_OK`，跳过整条 egress 链。条目硬过期（不刷新），保证每流定期有一个包走慢路径
> 去 `napt_ct_advance` 续命 CT。只在事件驱动模式（指标关闭）记录。
> 开关：`LANDSCAPE_NAT_FASTPATH=1`。
> 限制：仅 IPv4/TCP/UDP/非分片/以太网 WAN、只作用于出向、跳过 firewall 黑名单、
> TCP SYN/FIN/RST 永远走慢路径。

> **已移除（2026-10-30）按协议分类的 egress 管道**：之前那套 HTB/UDP-ICMP-TCP
> 分类（`qos_pipes.rs`）已整段删除——实测对家用低负载线路没有收益，
> 还接管了 WAN 的 root qdisc。环境变量 `LANDSCAPE_QOS_PIPES` / `LANDSCAPE_QOS_RATE`
> 不再有任何作用，WAN 回到内核默认的 `mq + fq_codel`。

> **新增（2026-10-29 晚）指标模式热切换**：`event_driven` / `report_interval` 原来只存在
> `.rodata`（libbpf 加载后冻结），WebUI 切 `[metric] mode` 只换了消费者，NAT 仍按旧模式跑，
> 页面永远 0。现在这两个值同时写进可写的 `land_nat_dyn` ARRAY map，`apply_runtime_config`
> 会调 `nat_options::reload()` 推进去——**改模式不再需要重启 landscape、也不需要重挂 NAT 段**。
> 切换方向为“→事件驱动”时，老 CT 会先用空闲超时重挂一次（`pad1` 标记），不会瞬间拆流。

> **热修（2026-10-29）**：einat 入向兜底原来写成
> `skb->ingress_ifindex ? skb->ingress_ifindex : skb->ifindex`，clang 把两次 ctx 读合并成
> 变址访问，verifier 拒绝（`dereference of modified ctx ptr R3 off=40 disallowed`），
> 表现为 `land_wan_ingress_intro` 加载失败（`-EACCES`）、`failed to start tc wan route`，
> **WAN 入向整条链挂不上**。现已改成只读 `skb->ingress_ifindex`。
> 只要换掉 `apply.py` 重新编译即可。

## 替换哪些文件（6 个）

| 本文件夹 | 仓库路径 | 改了什么 |
|---|---|---|
| `tcx-patch/apply.py` | 同名 | 删除第 8 段（preserve 的 CAS 占位、`napt_create_preserved_mapping`、包路径重写）；删除 rodata 字段与 Rust 赋值；删除 NAT 弹窗里的「端口保持」开关；保留 `napt_stats[2]` 的编号不复用；新增指标热切换、fast path、合并 MSS/防火墙的接线 |
| `tcx-patch/README.md` | 同名 | 「端口保持（opt-in）」改为「已移除」；新增「出向 NAT fast path」「合并 MSS stage」「合并 v4 防火墙」三节；env 表、`napt_stats` 表、已知限制同步更新 |
| `tcx-patch/overlay/landscape-ebpf/src/bpf/nat_v4/nat_fast.h` | 同名（**新增文件**） | fast path 表结构、record/try 两个 inline 函数 |
| `tcx-patch/overlay/landscape-ebpf/src/bpf/nat_v4/merge_mss.h` | 同名（**新增文件**） | `land_mss_cfg` map + MSS 钳制（移植自 `tc_mss.bpf.c`） |
| `tcx-patch/overlay/landscape-ebpf/src/bpf/nat_v4/merge_fw.h` | 同名（**新增文件**） | `firewall_block_ip4_map` 复用声明 + `nat_merge_fw4()` |
| `tcx-patch/overlay/landscape-ebpf/src/nat_options.rs` | 同名 | 去掉 `port_preserve` 字段、默认值、JSON 解析、env 覆盖 |
| `tcx-patch/overlay/landscape-webserver/src/system/nat_options.rs` | 同名 | DTO 去掉 `port_preserve` |
| `tcx-patch/overlay/landscape-webui/src/api/nat_options.ts` | 同名 | 前端类型去掉 `port_preserve` |

其余文件（`tcx.rs`、`landscape.rs`、`build-tcx.yml` 等）不动。

## 三个实验开关一览

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `LANDSCAPE_NAT_FASTPATH` | 出向 NAT fast path（跳过整条 egress 链） | 关 |
| `LANDSCAPE_NAT_MERGE_MSS` | 不挂 `tc_mss` 阶段，NAT 自己钳 MSS | 关 |
| `LANDSCAPE_NAT_MERGE_FIREWALL` | 不挂 v4 防火墙阶段，NAT 自己查黑名单 | 关 |

## 行为变化

- NAT 弹窗里**不再有**「端口保持」开关；`nat-options.json` 里残留的 `"port_preserve": true`
  会被忽略（serde 默认忽略未知字段），不用手工清理。
- 新的外部端口分配回到上游的单次 `napt_queue_pop()` 路径：
  分配不到端口直接 `TC_ACT_SHOT`（可用 `napt_stats[0]` 观察），不再有"占位 + 8 次重试"。
- `bpftool map dump name napt_stats` 的**下标含义不变**：0 `PORT_EXHAUSTED`、
  1 `METRIC_DROP`、2 保留、3 `MAP_MISS`、4 `REMOTE_DENIED`、5 `CT_FULL`、6 `CT_BUSY`。
  （2 刻意不复用，避免你已有的看板/脚本错位。）
- 需要"固定端口对外"的场景改用**静态 NAT / 端口转发**，不再依赖分配器。

## 验证（本地已完成）

- `apply.py` 在干净的 v0.28.0 上完整跑通，无锚点失配；
- 生成的 `nat_v4/tc_nat4.h` 与上游**逐字节一致**（preserve 的函数和多余 include 都没了）；
- `tc_nat4_packet.h` 与上游的差异只剩 `full_cone` 默认值 + 4 个诊断计数；
- 生成树里 `land_nat_port_preserve` 出现 0 次。

## 部署

1. 覆盖上面 5 个文件；
2. Actions → `build-tcx` → `baseline: v0.28.0`；
3. 替换 `/usr/bin/landscape-webserver`；
4. **重启 WAN 链路服务**（NAT 段在 attach 时读 `nat-options.json` 与 rodata）。
