# Landscape TCX patch (baseline v0.27.2)

把 `landscape-ebpf` 的 TC 入口（`tc_wan_ingress_intro` / `tc_wan_egress_route` /
`tc_lan_ingress_route` / `tc_lan_dao`）从 clsact 过滤器换成可选的 **TCX bpf_link**，
并提供顺序控制与 clsact 回退。数据面内部的 `tc_pipe_root_progs` 链保持不动。

## 目录结构

```
.github/workflows/build-tcx.yml     # 由 GitHub Actions 应用补丁并编译
tcx-patch/apply.py                  # 幂等打补丁脚本（锚点缺失会显式报错）
tcx-patch/overlay/landscape-ebpf/src/tcx.rs        # 新增 TCX 后端
tcx-patch/overlay/landscape-ebpf/src/landscape.rs  # TcHookProxy 双后端
```

## 产物

| job | 产物 | 适用系统 |
|---|---|---|
| `build`（gnu） | `landscape-webserver-<arch>-tcx`、`redirect_pkg_handler-<arch>-tcx` | glibc 发行版（Debian/Ubuntu 等） |
| `musl`（静态） | `landscape-webserver-<arch>-static`、`redirect_pkg_handler-<arch>-static` | **OpenWrt / Alpine / 任意 musl 系统** |

OpenWrt 必须用 `musl` job 的 `-static` 产物；gnu 产物在 musl 上会报
`not found` 或 `Exec format error`。

### 部署到 OpenWrt（aarch64）

```sh
# 1) 备份
cp /usr/bin/landscape-webserver /usr/bin/landscape-webserver.bak 2>/dev/null || true

# 2) 停服务（按你的 init 脚本名调整）
/etc/init.d/landscape stop 2>/dev/null || service landscape stop 2>/dev/null || true

# 3) 替换二进制（从 Actions artifact 下载后 scp 到 /tmp）
chmod +x /tmp/landscape-webserver-aarch64-static
mv /tmp/landscape-webserver-aarch64-static /usr/bin/landscape-webserver

# 4) 验证是静态链接
file /usr/bin/landscape-webserver        # 应含 "statically linked"
ldd  /usr/bin/landscape-webserver        # 应报 "not a dynamic executable"

# 5) 起服务
/etc/init.d/landscape start
```

配置与数据库在 `/mnt/emmc/.landscape-router/`，换二进制不需要动它们。

## 使用方式

1. Fork `ThisSeanZhang/landscape`（或者新建一个只放本目录内容的仓库）。
2. **把本目录的内容拷到仓库根目录**（不是把外层文件夹整个拷进去），最终仓库应长这样：

   ```
   <repo>/
   ├── .github/workflows/build-tcx.yml
   └── tcx-patch/
       ├── apply.py
       ├── README.md
       └── overlay/landscape-ebpf/src/{tcx.rs,landscape.rs}
   ```

   常见错误：拷成 `tcx-patch/tcx-patch/apply.py`（多一层），workflow 会报
   `can't open file '.../patch/tcx-patch/apply.py'`。新版 workflow 已用
   `find` 自动定位 `apply.py`，但仍建议保持上面的标准目录。

3. 在 Actions 里手动运行 `build-tcx`，`baseline` 填 `v0.27.2`。
4. 产物：`landscape-webserver-<arch>-tcx`、`redirect_pkg_handler-<arch>-tcx`。

工作流会：checkout 上游 tag → 应用 overlay → `scripts/setup_ubuntu_build.sh`
→ `cargo build --locked --release --features metric-persistent` → 上传产物。

### 上传前自检

```bash
git ls-files | grep -E 'tcx-patch|build-tcx'
```

期望输出（且只有这些）：

```
.github/workflows/build-tcx.yml
tcx-patch/apply.py
tcx-patch/README.md
tcx-patch/overlay/landscape-ebpf/src/landscape.rs
tcx-patch/overlay/landscape-ebpf/src/tcx.rs
```

如果出现 `landscape/patch/tcx-patch/...` 或 `tcx-patch/tcx-patch/...`，说明多套了目录；
如果少了 `overlay/...` 两个文件，`apply.py` 现在会直接报
`overlay is incomplete ... missing: landscape-ebpf/src/tcx.rs`，不会再“只打 lib.rs 就假成功”。

## 运行时开关（环境变量）

| 变量 | 取值 | 默认 | 作用 |
|---|---|---|---|
| `LANDSCAPE_TCX_MODE` | `auto` / `off` / `require` | `auto` | `auto`：内核 ≥6.6 用 TCX；`off`：只用 clsact；`require`：必须 TCX |
| `LANDSCAPE_TCX_ORDER` | `first` / `last` / `position` / `before` / `after` | `last` | 全局默认位置 |
| `LANDSCAPE_TCX_POSITION` | 整数 | `0` | `order=position` 时的目标下标（0=链首） |
| `LANDSCAPE_TCX_RELATIVE` | 程序名 | 空 | `order=before/after` 时的目标程序名，如 `egress_snat` |
| `LANDSCAPE_TCX_STRICT` | `0` / `1` | `0` | TCX 失败时是否让服务失败（否则回退 clsact） |
| `LANDSCAPE_TC_PRIO` | 1..65535 | `1` | clsact 回退时的 priority |
| `LANDSCAPE_TC_HANDLE` | u32 | `1` | clsact 回退时的 handle |
| `LANDSCAPE_TCX_IFACE_ORDER` | `iface:dir=order;...` | 空 | **按接口/方向覆盖顺序**（见下） |
| `LANDSCAPE_TCX_RULES_FILE` | 路径 | `$HOME/.landscape-router/tcx-order.json` | 按接口顺序的 JSON 规则文件 |
| `LANDSCAPE_WAN_INGRESS_IFX_FALLBACK` | `0` / `1` | `0` | WAN ingress 的目的地址查不到链路链时，改用 `(0x02\|ingress_ifindex)` 兜底分发。einat 先做 DNAT 时必须开；该路径用 `bpf_redirect` 转发到 LAN，**不经过 netfilter forward**，只在 einat 是唯一 NAT 所有者时开 |

### 按接口覆盖顺序

```sh
# 语法：<接口名或 ifindex>:<in|out>=<first|last|position:N|before:PROG|after:PROG>
LANDSCAPE_TCX_IFACE_ORDER="eth0:out=after:egress_snat;eth0:in=last;eth1:in=first"
```

也可以写 JSON 文件（`$HOME/.landscape-router/tcx-order.json`），便于脚本/前端生成：

```json
{
  "ifaces": [
    { "name": "eth0", "ingress": "last", "egress": "after:egress_snat" },
    { "name": "eth2", "ingress": "first" }
  ]
}
```

优先级：`LANDSCAPE_TCX_IFACE_ORDER`（env） > JSON 文件 > 全局 `LANDSCAPE_TCX_ORDER`。
未命中的接口/方向使用全局默认值；环境变量在进程启动时解析一次。

## 程序命名

补丁会把四个入口程序从通用的 `tc_*` 前缀改成项目前缀 **`land_*`**（与 sing-box 的
`sb_*` 对齐），`bpftool net show` 里会看到：

```
eth0(2) tcx/ingress land_wan_ingress_intro ...
eth0(2) tcx/egress  land_wan_egress_route  ...
eth1(3) tcx/ingress land_lan_ingress_route ...
```

实现方式：`apply.py` 重命名 C 侧函数名（即 BPF 程序名）+ Rust 侧 `progs.<name>` 引用；
skeleton 模块名来自 `.bpf.c` 文件名，保持不动，因此不会牵连 `crate::skel::*` 路径。


示例：

```sh
# 用 TCX，并插到 einat 的 egress_snat 之后
LANDSCAPE_TCX_MODE=require \
LANDSCAPE_TCX_ORDER=after \
LANDSCAPE_TCX_RELATIVE=egress_snat \
landscape-webserver

# 想抢在 einat 之前（注意下面“返回码”一节）
LANDSCAPE_TCX_MODE=require LANDSCAPE_TCX_ORDER=first landscape-webserver
```

## 顺序语义（内核 `bpf_mprog_attach`）

- `order=last`：不带 relative，追加到链尾；
- `order=first`：`BPF_F_BEFORE` 不带 relative → 插到链首；
- `order=before/after:<name>`：按程序名解析 prog id，`BPF_F_ID|BPF_F_BEFORE/AFTER`；
- `order=position:N`：用 `bpf_prog_query` 取链序，把第 N 个程序的 id 作为相对目标。

## 重要：TCX 返回码会截断链

TCX 中 `TCX_NEXT(-1)` 才继续下一个程序，`TC_ACT_OK(0)` 等于 `TCX_PASS`，会
**终止整条链**。Landscape 现有程序大量返回 `TC_ACT_OK`，因此：

- Landscape 放在链尾最安全；
- 放在链首/中间时，排在它后面的程序（如 einat）不会执行；
- 若要让多个程序串联，需要把 Landscape 的出口程序改成返回 `TCX_NEXT`（未包含在本补丁中）。

## WebUI 中配置 TCX 顺序

打开拓扑 → 点 WAN 或 LAN 接口节点 → 在「Wan 路由转发服务」/「Lan 路由转发服务」卡片
的「更新」按钮**上方**，会多出 **TCX 顺序（接口名）** 一段：

- **Ingress 顺序**、**Egress 顺序**（LAN 只有 Ingress）：
  `继承全局` / `链首 first` / `链尾 last` / `指定位置 position` / `在程序之前 before` / `在程序之后 after`
- 选 `position` 时填下标，选 `before/after` 时填目标程序名（如 `egress_snat`）
- 点 **保存 TCX 顺序** 后写入 `$HOME/.landscape-router/tcx-order.json`，并立即 reload
  到 `landscape_ebpf::tcx` 的缓存
- **生效时机**：保存后需要重启该接口的路由转发服务（也就是再点一次卡片上的「更新」），
  或重启 Landscape；已经挂上的 TCX link 不会立刻重排

对应接口：

```
GET  /api/v1/system/tcx/order   -> { rules, ifaces, global_order, mode }
PUT  /api/v1/system/tcx/order   -> body { rules: [{ name, ingress, egress }] }
```

规则字符串格式与 env 一致：`first` / `last` / `position:N` / `before:PROG` / `after:PROG`，
空串表示继承全局。环境变量 `LANDSCAPE_TCX_IFACE_ORDER` 仍然优先于这个文件（WebUI 适合
不设 env 的场景）。

## Landscape 自身 NAT4 的优化（nat_v4）

本补丁同时修正/优化 Landscape 自己的 NAPT44（`landscape-ebpf/src/bpf/nat_v4/`）：

| 问题 | 修复 |
|---|---|
| **P0**：`nat_metric_events` ringbuf 满/无消费者（metric=off、DB 写慢）时，`napt_metric_try_report()` 失败会让 CT 状态机 `return RESTART`，永远停在 ACTIVE → 不超时、端口不回收、`napt_ct` 涨满后新连接静默丢 | 上报改为“尽力而为”：失败不再阻塞状态推进 |
| 端口分配可预测（队列按 1024…65535 顺序 pop，外部端口基本递增） | 启动时用 xorshift+时间/PID 种子打乱端口入队顺序（Fisher-Yates） |
| CT 超时写死 6s / 10min / 5min | 改为 rodata 可调，默认值对齐 einat（见下表） |
| Full cone 依赖流控 `allow_reuse_port` 标记，默认是 EDF | 新增 **WebUI 开关**（NAT 服务弹窗），默认开启 EIM+EIF（full cone） |

### Full Cone / 超时 开关在哪里

拓扑 → 点 WAN 接口 → **NAT 服务** 弹窗，在三个端口段下面：

```
TCP 端口段
UDP 端口段
ICMP ID 段
TCP 半开超时 (秒)      [ 120 ]
TCP 空闲超时 (秒)      [ 86400 ]
UDP 半开超时 (秒)      [ 120 ]     ← 未见回包时用
UDP 超时 (秒)          [ 600 ]     ← 见到回包后切换
ICMP 超时 (秒)         [ 60 ]
Full Cone (EIM/EIF)   [开关]     ← 默认开
[更新]
```

默认值对齐 einat 的 NAT 计时器：

| Landscape 字段 | einat 配置 | 默认 |
|---|---|---|
| TCP 半开超时 | `timeout_tcp_trans = "2m"` | 120s |
| TCP 空闲超时 | `timeout_tcp_est = "24h"` | 86400s |
| UDP 半开超时 | `timeout_udp_min = "2m"` | 120s |
| UDP 超时 | `timeout_udp_default = "10m"` | 600s |
| ICMP 超时 | `timeout_icmp_default = "1m"` | 60s |

实现方式：`napt_ct_advance_pkt` 在收到 WAN 侧报文时把 `server_status` 置为
`NAPT_TCP_STATE_LESS_EST`；`napt_ct_wait_ns` 对 UDP 判断这个状态：
未见回包 → `UDP_MIN`（120s），见过回包 → `UDP`（600s）。与 einat 的
`INIT_OUT/INIT_IN -> ESTABLISHED` 两段式等价。

TCP 侧**不套用**这个判定：`napt_ct_advance_pkt()` 对 TCP 只会写 `SYN/FIN/INIT`
（数据包不写 `LESS_EST`），所以"双向 `LESS_EST` 才算已建立"是死代码。上游的
`client_status == SYN && server_status == SYN`（握手完成）才是"已建立"，
用长超时；SYN、FIN、RST 等过渡状态一律用 `TCP 半开超时`（120s），避免
半开/已关闭连接占用 24h。**2026-10-29 已把早先误改的 `LESS_EST` 判定回退**，
否则所有 TCP 流的空闲超时都会被压到 120s。

UI 上超时字段用两列 `n-grid` 排布（每行两个），比原来五列竖排省一半高度。

einat 的 `timeout_fragment = "5s"` 没有直接对应：Landscape 只用
`napt_is_non_initial()` 识别非首片，不做 einat 那样的 frag 端口记忆。

**补充（0.27.2 已有）**：`tc_nat4_v4.bpf.c` 里对 ICMP error 调用
`napt_modify_icmp_error()`（翻译内嵌包），对非首片调用 `napt_modify_fragment()`，
所以 traceroute/PMTU 和分片并不是缺失项；本补丁没有改动这两条路径。

**RST 快速回收**：TCP RST 到达时直接把 CT 置为 RELEASE（定时器 1ns），
端口立即走回收流程，不再等 120s 短超时。

保存时前端会先 `PUT /api/v1/system/nat/options` 写 `$HOME/.landscape-router/nat-options.json`，
再保存 WAN link；`landscape_ebpf::nat_options::reload()` 刷新缓存，下次 NAT attach 时生效。

API：

```
GET /api/v1/system/nat/options
PUT /api/v1/system/nat/options
   { "full_cone": true, "tcp_syn_timeout_secs": 120,
     "tcp_timeout_secs": 86400, "udp_min_timeout_secs": 120,
     "udp_timeout_secs": 600, "icmp_timeout_secs": 60 }
```

环境变量（优先级高于 JSON，方便脚本/调试）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `LANDSCAPE_NAT_TCP_SYN_TIMEOUT_SECS` | `120` | TCP 半开超时 |
| `LANDSCAPE_NAT_TCP_TIMEOUT_SECS` | `86400` | TCP 已建立空闲超时 |
| `LANDSCAPE_NAT_UDP_MIN_TIMEOUT_SECS` | `120` | UDP 未见回包时的短超时 |
| `LANDSCAPE_NAT_UDP_TIMEOUT_SECS` | `600` | UDP 已见回包时的超时（QUIC/游戏建议 120 或更短） |
| `LANDSCAPE_NAT_ICMP_TIMEOUT_SECS` | `60` | ICMP 超时 |
| `LANDSCAPE_NAT_FULL_CONE` | 取 JSON/默认(1) | `0` 关闭 full cone，退回“只允许 trigger 对端”的 EDF |
| `LANDSCAPE_NAT_REPORT_INTERVAL_SECS` | `30` | CT 定时器/指标上报周期（原 5s）。调大省 CPU、指标更粗；范围 1–3600 |
| `LANDSCAPE_NAT_EVENT_DRIVEN` | `1` | `1`=事件驱动（无周期 tick）；`0`=周期上报模式。metric 模式非 off 时自动 `0` + 5s，但**显式设置本变量时以变量为准** |

### 端口保持（已移除）

早期的「端口保持 (Port Preserve)」已**整段删除**：它只做 WAN 槽 CAS 占位、不把端口从队列里
摘掉，释放时又 push 一次，队列里会出现重复项，最终 `push` 失败、CT 卡在 `PUSH_QUEUE`、
端口泄漏。且对「固定源端口」的游戏基本触发不了（端口通常不在 `32768..65535` 内），
实际收益有限。

需要「固定端口对外」的场景请用 **静态 NAT / 端口转发**（确定性映射，不经过分配器）。

### 失败计数（bpftool 可读）

`nat_v4` 新增了一个 per-CPU 计数 map `napt_stats`，不用重启、随时可读：

```sh
bpftool map dump name napt_stats
```

| 下标 | 含义 |
|---|---|
| 0 | `PORT_EXHAUSTED`：端口队列为空，新流分配不到外部端口 |
| 1 | `METRIC_DROP`：指标 ringbuf 满，上报被丢弃（不再影响 CT 状态机） |
| 2 | 保留（原 `PRESERVE_OK`，端口保持已移除，编号不复用以免诊断脚本错位） |
| 3 | `MAP_MISS`：WAN 入向找不到/不可用的映射 |
| 4 | `REMOTE_DENIED`：EDF 模式下远端不在白名单 |
| 5 | `CT_FULL`：`napt_ct` 满，新连接无法建 CT |
| 6 | `CT_BUSY`：报文命中的 CT 正在清理（旧 CT 没删完，重建被 `BPF_NOEXIST` 挡住 → 该包被丢） |

计数是普通 `ARRAY`（失败路径上原子加），`bpftool` 直接按 key 显示。


注意：

- **WAN 地址变化已自动处理**：nat4 section 的作用域是 `SectionScope::SessionAndLease`
  （`wan_link_service/mod.rs`），租约 v4 地址变化会重启 NAT 段并重新烘 rodata，
  DHCP 换地址不需要手工重启。
- **没有外网入站需求时建议关掉 Full Cone**：EDF（默认）只允许 trigger 对端，
  没有端口转发/P2P 入站需求时更安全；Full Cone 开着会接受任意远端主动连映射端口。
- **内存占用**：`napt_from_wan` 是 ARRAY，3 × 65536 × 32B ≈ **6.3 MB/链路**；
  三个端口队列 3 × 65536 × 8B ≈ 1.5 MB；`napt_from_lan` / `napt_ct` 各 196608 条
  （`BPF_F_NO_PREALLOC`）。多 WAN 时线性增长，后续可考虑 HASH/索引表优化。
- 超时/ full cone 的 rodata 在 **NAT 段 attach 时**写入（`stages/nat4.rs`），改了要重启 **WAN 链路服务**（或整个 Landscape）；
- XDP NAT 路径复用 TC 的 conntrack，但超时/ full cone 的 rodata 只对 TC 数据面设置，XDP 路径仍用 C 默认值（当前 `xdp:` 为空，不影响）；
- 以上改动只针对 **IPv4 NAT**；NAT6 的对应优化未包含。
- **事件驱动模式（指标关闭时默认开）**：`LANDSCAPE_NAT_EVENT_DRIVEN=1` 时，每个报文把 CT 定时器
  重置为**该流的空闲超时**（TCP 24h / UDP 600s / ICMP 60s），定时器到期直接进入
  RELEASE → 回收端口，**没有任何周期性 tick**。空闲连接的 CPU 开销接近零。
  - **指标联动**：`nat_options` 启动时会读 `$HOME/.landscape-router/landscape.toml` 的
    `[metric] mode`。只要不是 `off`（含缺省 `persistent`），就自动
    `event_driven=false` + `report_interval=5s`，保证活跃连接页面指标正常；
    只有 `mode = "off"` 才保持事件驱动。
  - **运行时热切换**（2026-10-29 起）：`event_driven` / `report_interval` 不再只存在于
    `.rodata`，而是同时写进可写的一维数组 `land_nat_dyn`（`struct land_nat_dyn_cfg`）。
    WebUI 改 `[metric] mode` 时 `MetricService::apply_runtime_config` 会调用
    `nat_options::reload()` 把新值推进去，**不需要重启 landscape、也不需要重挂 NAT 段**；
    已存在的 CT 在下一次 tick 生效（周期模式最长 30s）。
    - 切到事件驱动时，老 CT 的 `pad1=0` 表示“定时器是按 tick 武装的”，回调会先用该流的
      空闲超时重挂一次再释放，避免切模式瞬间把活跃流拆掉。
  - **显式 env 优先**：手动设了 `LANDSCAPE_NAT_EVENT_DRIVEN=1/0` 时
    不再被上面的指标联动覆盖，方便 init 脚本做 A/B。注意 event 模式不会周期性上报
    ACTIVE，NAT 会话页看不到活跃流（只有删除事件）。
  - 需要实时指标又想要事件驱动，属于矛盾需求；二选一。
- **TCP 空闲 24h 的代价（仅周期模式）**：`EVENT_DRIVEN=0` 时每个 ACTIVE CT 仍按
  `report_interval`（默认 **30s**，原 5s）跑一次 `bpf_timer`；调大间隔可成比例减少唤醒。

### UDP 周期性卡顿 / 换端口修复（2026-10-29）

**现象**：游戏 UDP 每 ~2 分钟换一次公网端口，换的瞬间约 0.6s 全丢包，
表现为周期性卡顿、人物瞬移、语音断续。

**根因**（事件驱动模式）：

1. `napt_ct_advance_pkt()` 只在 `napt_ct_status_can_activate()` 为真（CT 还不是 ACTIVE）
   时才 `bpf_timer_start()`。UDP 首包激活时 `server_status` 还是 INIT，定时器被打成
   `udp_min`（120s）；回包后 CT 进入 ACTIVE，之后**每个报文都不再续期**，所以
   `udp`（600s）这一档永远不生效——活跃流也会在 120s 到点被拆。
2. 到期后走 `RELEASE → 清 LAN 表 → 清 WAN 槽 → 还端口`（分步 100ms/500ms）。
   这期间 `napt_ct_status_usable()` 为假 → `napt_ct_resolve()` 失败；重建又因
   `BPF_NOEXIST` 撞上还没删完的旧 CT → `TC_ACT_SHOT`，即 ~0.6s 的 fail-closed 黑洞。
3. 旧代码"到期先转 RELEASE 再 arm 1ns 二次回调"还有竞态：报文在两次回调之间
   把 CT 刷新回 ACTIVE 后，第二次回调会把这个刚救活的流再拆掉。

**修复**（对齐 einat 的 `ct_state_transition() -> ct_reset_timer()`）：

- 事件驱动模式下**每个报文**都 `napt_ct_timer_start(value, napt_ct_timeout_for(...))`，
  空闲超时从最后一次报文起算，600s 两段式真正生效；
- 到期时在**同一个** timer 回调里 `ACTIVE → RELEASE` 并继续清理，不再用 1ns 二次回调；
- 回退 TCP 的 `LESS_EST` 判定（见上一节）。

**注**：清理窗口本身仍是分步的，真正的空闲超时（例如 UDP 2 分钟没包）之后
仍会有一次短暂重建 + 换端口，这是 NAT 的正常行为；本修复消除的是
"**活跃流也被 120s 强拆**"。

**验证**：

```sh
# 事件驱动模式下，活跃 UDP 流的 external_port 应长期不变
bpftool map dump name napt_from_lan | grep -A6 '<内网端口>'
# CT 数量与失败计数
bpftool map dump name napt_stats
```

## 与 einat 分工（einat 做 NAT，Landscape 做路由）

目标分工：**入向 einat 优先**（先 DNAT，再交给 Landscape 路由到 LAN）；
**出向 Landscape 优先**（先路由/整形，最后 einat SNAT）。

```sh
LANDSCAPE_TCX_MODE=auto
LANDSCAPE_TCX_IFACE_ORDER="wan0:in=last;wan0:out=first"
LANDSCAPE_WAN_INGRESS_IFX_FALLBACK=1        # 入向兜底，见下
```

| 方向 | 链序 | 说明 |
|---|---|---|
| WAN ingress | `ingress_rev_snat`（einat）→ `land_wan_ingress_intro` | einat 用 `LinkOrder::first()` 自己抢链首；landscape `in=last` 追加在尾 |
| WAN egress | `land_wan_egress_route` → `egress_snat`（einat） | landscape `out=first`；egress 链尾 `tc_wan_egress_exit_redirect` 返回 `TC_ACT_UNSPEC`（= `TCX_NEXT`），链会继续到 einat |
| LAN ingress | `land_lan_ingress_route` | einat 不挂 LAN |

**入向必须开 `LANDSCAPE_WAN_INGRESS_IFX_FALLBACK=1`。** `tc_wan_ingress_intro` 是按
**目的地址**在 `wan_trie` 里查链路链的；einat 先 DNAT 之后 dst 已经变成 LAN 地址，
查不到就 `TC_ACT_SHOT`（入向全丢，这就是 0.27.x 的 einat ingress 兼容问题）。
打开后会用 `(0x02 | ingress_ifindex)` 兜底再查一次，命中该接口的链路链，后面 route
看到 dst 是 LAN 就 `bpf_redirect` 过去。

> **实现注意（2026-10-29 修复）**：兜底里只能读 `skb->ingress_ifindex` 一个字段。
> 早期写成 `skb->ingress_ifindex ? skb->ingress_ifindex : skb->ifindex`，clang 会把两次
> ctx 读合并成 `ctx + (cond ? 36 : 40)` 的变址访问，verifier 直接拒绝：
> `dereference of modified ctx ptr R3 off=40 disallowed`，表现为
> `prog 'land_wan_ingress_intro': BPF program load failed: -EACCES`、
> `failed to start tc wan route`，WAN 入向整条链都挂不上。

代价：这条兜底路径是 `bpf_redirect` 到 LAN，**不经过内核 forward 链（netfilter）**，
未 NAT 的 WAN→LAN 包也会被转过去。所以默认关闭，只在“einat 是唯一 NAT 所有者”的
机器上开，并配合 LAN 网段/主机防火墙。

**出向 `out=first` 的两个限制：**

1. einat 每次重新 attach（WAN flap、服务重启）都会 `LinkOrder::first()` 再抢一次链首，
   landscape 会掉到第二位。功能上仍能跑（只是 firewall 先看到的是 SNAT 后的地址），
   要恢复顺序就重启一次 WAN 链路服务（landscape 用 `first` 重新插到链首）。
2. **landscape 自己管理的 PPPoE 不能用 `out=first`。** PPPoE 封装是 egress 链里的一个
   stage（`FromLanStageType::Pppoe`），跑到链尾时已经是 PPPoE 帧，einat 读不到 IPv4
   （`eth->h_proto == 0x8864`）会直接 passthrough，**不会 SNAT**。PPPoE WAN 必须保持
   einat 在前（`out=last`）。

验证：

```sh
bpftool net show dev wan0
# ingress: ingress_rev_snat      → land_wan_ingress_intro
# egress : land_wan_egress_route → egress_snat
```

## 出向 NAT fast path（实验，`LANDSCAPE_NAT_FASTPATH=1`）

类比 xt_FLOWOFFLOAD / SFE 的“首包建表、后续包直发”，但实现在 eBPF 里。

- **记录**：`tc_v4_from_lan` 完成 NAT 改写后，把 `(内网 saddr/sport, 远端 daddr/dport, proto)`
  → `(external_addr, external_port, deadline)` 写进 `fa4_fast`；**只记录转发流量**
  （`skb->ingress_ifindex != 0`），路由器自身的 DNS/NTP/WebUI 流不落表；
- **命中**：`tc_wan_egress_route` 在 `tc_cb_forwarded` 分支里先查这张表，命中就直接
  `napt_modify()` 改写 + 返回 `TC_ACT_OK`，**整条 egress 链（mss/firewall/nat/exit）都不走**；
- **5 秒硬过期（不刷新）**：每条流每 5 秒至少有一个包走慢路径，顺带 `napt_ct_advance()`
  续命 CT 并重新落表。这样 fast path 不需要碰 `napt_ct`，也不会让 CT 被提前释放；
- **只在事件驱动模式记录**（`land_nat_event_driven_get()`）：周期模式下 CT 状态机需要逐包推进，
  跳过会导致流被拆；
- 表是 pinned map，`maps::nat::init_fa4_fast` 在任何程序加载前创建，NAT 与 egress route
  两个对象各自 `pin_and_reuse_map`。

限制（重要）：

- 只处理 **IPv4、TCP/UDP、非分片、以太网封装**；TCP 的 SYN/FIN/RST 一律走慢路径（MSS 钳制、
  CT 状态机）；
- 只作用于 **出向**（LAN→WAN），入向仍然是完整链；
- **不做网卡防火墙/黑名单检查**：命中 fast path 的包跳过 firewall stage；
- CT 被删除（空闲超时、flush）时，同 key 的 fast 条目在 `napt_ct_delete()` 里一起删掉
  （`#ifdef LAND_NAT_FAST`，只在 TC 对象里生效）；理论上仍然残留的只有 XDP NAT 创建的 CT
  （本机没开 XDP，不受影响），以及 5 秒内还没被任何包命中的条目；
- 不走 landscape 自己 eBPF 封装的 PPPoE 链路（eth 头/出口封装由 exit stage 完成，跳过会发错帧）。

验证：

```sh
bpftool map dump name fa4_fast | head        # 有流量时能看到条目
tc -s class ...                               # 不适用；看 napt_ct 计数或 CPU
bpftool prog show | grep tc_wan_egress_route  # run_time 应下降
```

## 已知限制 / 下一步

- TCX 顺序的保存不会实时重排已挂载的 link，需要重启对应路由服务/进程。
- `LANDSCAPE_TCX_ORDER=position` 依赖 `bpf_prog_query` 返回的链序；若内核/权限
  不允许查询，会退化为追加。
- 与 einat 的 ingress 兼容问题（0.27.x 按目的地址分发、einat 提前改写 dst）已由
  `LANDSCAPE_WAN_INGRESS_IFX_FALLBACK`（`0x02` ifindex 兜底）解决；不开兜底时
  einat 必须在 landscape 之后，否则入向会被 `tc_wan_ingress_intro` 丢掉。
- NAT 侧仍缺：LRU/水位告警（`napt_ct` 满了仍然 fail-closed）、ALG（FTP/SIP）、
  NAT64、hairpin、每连接 timer 的 CPU 开销。
