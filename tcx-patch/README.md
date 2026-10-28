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
| `LANDSCAPE_TCX_ORDER` | `first` / `last` / `position` / `before` / `after` | `last` | 在 TCX 链中的插入位置 |
| `LANDSCAPE_TCX_POSITION` | 整数 | `0` | `order=position` 时的目标下标（0=链首） |
| `LANDSCAPE_TCX_RELATIVE` | 程序名 | 空 | `order=before/after` 时的目标程序名，如 `egress_snat` |
| `LANDSCAPE_TCX_STRICT` | `0` / `1` | `0` | TCX 失败时是否让服务失败（否则回退 clsact） |
| `LANDSCAPE_TC_PRIO` | 1..65535 | `1` | clsact 回退时的 priority |
| `LANDSCAPE_TC_HANDLE` | u32 | `1` | clsact 回退时的 handle |

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

## 已知限制 / 下一步

- 本补丁只做 **TCX attach + 环境变量 + CI 编译**，未包含 WebUI 配置卡片；
  配置面（`[tcx]` + `/api/v1/system/config/tcx` + Vue 卡片）建议在首轮 CI 绿后追加。
- `LANDSCAPE_TCX_ORDER=position` 依赖 `bpf_prog_query` 返回的链序；若内核/权限
  不允许查询，会退化为追加。
- 与 einat 的 ingress 兼容问题（0.27.x 按目的地址分发、einat 提前改写 dst）
  是独立问题，TCX 不能替代 ifindex 兜底补丁。
