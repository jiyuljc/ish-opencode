# iSH-ARM64 + 预装 opencode（未签名 IPA）

一个 GitHub Actions 工作流：把已发布的 opencode（aarch64/musl）预装进 iSH-ARM64 的
rootfs，编译出可交给 AltStore / SideStore 侧载的未签名 IPA。

```
.github/workflows/build-ipa.yml   # 全部构建逻辑（16 步）
integration/apply_patches.py      # 两处上游必须修的补丁（见下）
integration/inject_rootfs.py      # 往 rootfs 里装 opencode（纯 Python，含单测）
integration/test_inject_rootfs.py # 4 个单测，本地可跑
integration/verify_app.sh         # 产物自检（arm64? rootfs 里有 opencode?）
app/Local.xcconfig                # 本地 Xcode 构建用
```

参考资料：[jiyuljc/dsh-ios](https://github.com/jiyuljc/dsh-ios) —— 同样是 iSH-ARM64 外壳、
同样打包未签名 IPA。本方案从它那里修正了**三个真实缺陷**和一处构建参数。

## 为什么不是"合并两个仓库"

opencode 是 Alpine guest 里的一个 ELF 程序，iSH 是 iOS 外壳，源码层没有接缝，
唯一接缝是 **rootfs**。而 rootfs 不在仓库里，是构建阶段从 `ROOTFS_URL` 下载的
`root.tar.gz`：

```xcconfig
// app/GuestARM64.xcconfig
ROOTFS_URL = dl-cdn.alpinelinux.org/alpine/v3.21/releases/aarch64/alpine-minirootfs-3.21.0-aarch64.tar.gz
```

所以"预装" = 重写这个 tar.gz，再起本地 HTTP 服务顶掉 `ROOTFS_URL`。

没有用 `app/RootfsPatch.bundle`：它是 iSH 自带的 overlay 机制，但 `project.pbxproj`
里 **0 处引用**，`NSBundle URLForResource:@"RootfsPatch"` 永远返回 nil，运行时打印
"bundle not found" 后跳过。（dsh-ios 也是在构建 rootfs 时手动 `cp` 这些 polyfill 进去，
并没有依赖运行时 overlay —— 同一结论。）

## 两个必须打的上游补丁

`integration/apply_patches.py`，锚点匹配、幂等、缺失即报错退出（已测）。

**① `kernel/uname.c`：宿主主机名冲爆 guest 字段，SIGTRAP 杀掉模拟器**

```c
struct utsname real_uname;        // 没初始化
uname(&real_uname);               // 返回值没查
strcpy(uts->hostname, hostname);  // 目标只有 65 字节（UNAME_LENGTH）
```

宿主机名 ≥65 字符、或 `uname()` 失败留下栈垃圾时，fortified libc 直接
`__chk_fail_overflow` → SIGTRAP，**死的是宿主 ish 进程**，而 guest 第一次调
`uname()`（`/bin/sh` 启动时）就会触发。改成 `snprintf` 截断 —— 这本来也是 Linux 语义。

**② `app/hook.c`：任何重签名版本都会被 iOS 杀掉**

`hook()` 对自己不拥有的线程 `thread_set_state(ARM_DEBUG_STATE64)`，需要
`task_for_pid-allow` entitlement。AltStore/免费 Apple ID 签出来的包**没有任何
entitlement**，于是被系统终止；而 `accessibilityfixes_init()` 是
`__attribute__((constructor))`，开启旁白时开机即走这条路径。改成记录日志并降级
（非 arm64 分支本来就返回 false）。

> 修正了上一版的一个错判：我先前以为 **Blocks（`^{}` 语法）**是阻断点。
> 实际上 iSH 里 Block 语法在 ARC / Apple Clang 下本就支持，`app/` 里到处在用，
> 不是问题。

## 构建参数的三处修正

| 上一版 | 修正 | 原因 |
|---|---|---|
| `-scheme iSH` | `-scheme iSH-ARM64` → `iSH ARM64.app` | 产物必须是 arm64 外壳 |
| `xcodebuild build -sdk iphoneos` | `xcodebuild archive -destination 'generic/platform=iOS'` | dsh-ios 实测用的就是 archive；`build` 对设备架构常挑错 |
| 只加 `CODE_SIGNING_*` | 另加 `CODE_SIGN_ENTITLEMENTS=""`、`CODE_SIGN_STYLE=Manual`、`DEVELOPMENT_TEAM=""` | 避免工程里的 entitlements 干扰无签名归档 |

## 注入的四个硬约束

1. **必须与 `/lib` 同文件系统** → `/usr/local/bin/`。opencode 是 dynamic ELF
   （`PT_INTERP = /lib/ld-musl-aarch64.so.1`）；`/mnt` 是 bind-mount 到 iOS 容器目录的
   跨文件系统路径，loader 解析不到。
2. **必须用 musl 构建**：`opencode-linux-arm64-musl.tar.gz`，工作流会校验 ELF 头
   （AArch64 + ET_*），装错架构当场失败。
3. **Bun 需要关 JIT**：wrapper 里 `export OPENCODE_DISABLE_JIT=1`。只能放 wrapper，
   不能放二进制内部 —— loader 在程序跑起来之前就解析 `PT_INTERP`。
4. **不依赖 guest 里的 node**：Bun 已编进二进制，但代价是 186 MB → IPA 约 67 MB。

## 跑起来

1. 推到自己的 GitHub（**公开仓库**，否则 macOS runner 计费 $0.08/分钟）。
2. Actions → *Build unsigned IPA (iSH-ARM64 + opencode)* → Run workflow。
3. 下载 artifact，或用 `release: true` 直接生成 Release 链接。
4. 侧载后进 App，`opencode --version` 直接可用，无需联网。

本地验证（不需要 Mac）：

```bash
curl -O https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/aarch64/alpine-minirootfs-3.21.0-aarch64.tar.gz
python integration/test_inject_rootfs.py
python integration/inject_rootfs.py --rootfs alpine-minirootfs-3.21.0-aarch64.tar.gz \
  --opencode opencode --out build/root/root.tar.gz
python integration/apply_patches.py --ish-src /path/to/ish-arm64
```

## 已在本机验证

- 注入：真实 Alpine rootfs 522 = 519 + 3 条，**原条目一条不丢**；二进制 SHA256 与上游
  一致；`root.tar.gz` 66.5 MB
- 补丁：从 upstream `c5c264d` 取真实 `uname.c` / `hook.c`，应用成功、二次运行幂等、
  锚点被改动时**报错退出**而不是静默跳过
- 工作流 YAML 可解析，16 步

**没有验证的**：Xcode 归档能否成功、Bun 能否在 iSH 里真正跑起来。这两件事只能在
macOS runner + 真机上确认。

## 剩余风险

- **Bun 在 iSH 下的启动**仍是最大未知。iSH 对 Node/V8 做过适配（guard page、二进制
  patch、`--jitless`），Bun 是另一套 JSC 内存行为。若 IPA 装好、终端正常、但
  `opencode` 无输出，问题在这里。
- **rootfs 被"钉住"**：用户 `apk upgrade` 后，新 IPA 不会覆盖其根文件系统。
- **App Group 在侧载版可能不可用**（重签名不带 entitlements）。
- **冷构建 60–90 分钟**：递归子模块 + 186 MB 二进制 + archive。
- `app/Local.xcconfig` 不在上游 `.gitignore` 里，别带进 PR。
