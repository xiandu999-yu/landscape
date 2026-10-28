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

einat 的 `timeout_fragment = "5s"` 没有直接对应：Landscape 只用
`napt_is_non_initial()` 识别非首片，不做 einat 那样的 frag 端口记忆。

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
| `LANDSCAPE_NAT_PORT_PRESERVE` | `0` | `1` 开启端口保持：优先用客户端原源端口（在 range 内且 WAN 槽空闲时），失败再走队列分配 |

### 端口保持（opt-in）

`LANDSCAPE_NAT_PORT_PRESERVE=1` 时，新流先尝试用客户端的原源端口：

1. 端口必须落在该协议的 `tcp_range` / `udp_range` 内；
2. 用 `state_ref` 上的 **`0 → 0xFFFFFFFFFFFFFFFF` 64 位 CAS 原子占位**
   （BPF 后端不支持 32 位 cmpxchg）；`napt_wan_clear()` 会把整槽清零，所以这个
   CAS 只会成功一次，避免两个 CPU 同时抢同一个端口；
3. 占位成功后填 WAN 槽和 LAN 哈希；LAN 插入失败则 `napt_wan_clear()` 归还槽位；
4. 原端口后面还会被队列 pop 到，创建路径会跳过 `kind != FREE` 的槽位（最多重试 8 次），不会把流量打失败。

默认关闭；开启后需要重启 NAT 段（NAT 弹窗点「更新」）。

注意：

- **WAN 地址变化已自动处理**：nat4 section 的作用域是 `SectionScope::SessionAndLease`
  （`wan_link_service/mod.rs`），租约 v4 地址变化会重启 NAT 段并重新烘 rodata，
  DHCP 换地址不需要手工重启。
- **内存占用**：`napt_from_wan` 是 ARRAY，3 × 65536 × 32B ≈ **6.3 MB/链路**；
  三个端口队列 3 × 65536 × 8B ≈ 1.5 MB；`napt_from_lan` / `napt_ct` 各 196608 条
  （`BPF_F_NO_PREALLOC`）。多 WAN 时线性增长，后续可考虑 HASH/索引表优化。
- 超时/ full cone 的 rodata 在 **NAT 段 attach 时**写入（`stages/nat4.rs`），改了要重启 **WAN 链路服务**（或整个 Landscape）；
- XDP NAT 路径复用 TC 的 conntrack，但超时/ full cone 的 rodata 只对 TC 数据面设置，XDP 路径仍用 C 默认值（当前 `xdp:` 为空，不影响）；
- 以上改动只针对 **IPv4 NAT**；NAT6 的对应优化未包含。
- **TCP 空闲 24h 的代价**：每个 ACTIVE CT 仍按 5s 周期跑一次 `bpf_timer`（指标上报），
  CT 存活越久、同时在线的条目越多，timer 唤醒就越多。R6S 家宽一般够用；若发现
  `ksoftirqd`/CPU 偏高，把 TCP 空闲降到 1800~3600 即可。

## 已知限制 / 下一步

- TCX 顺序的保存不会实时重排已挂载的 link，需要重启对应路由服务/进程。
- `LANDSCAPE_TCX_ORDER=position` 依赖 `bpf_prog_query` 返回的链序；若内核/权限
  不允许查询，会退化为追加。
- 与 einat 的 ingress 兼容问题（0.27.x 按目的地址分发、einat 提前改写 dst）
  是独立问题，TCX 不能替代 ifindex 兜底补丁。
- NAT 侧仍缺：LRU/水位告警（`napt_ct` 满了仍然 fail-closed）、ALG（FTP/SIP）、
  NAT64、hairpin、每连接 timer 的 CPU 开销。
