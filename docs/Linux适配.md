# Linux 适配

目标：同一套 ArkLink、同一套 OneBot 接口，在 Linux 上也能接管官方 QQ 客户端。
上层（NoneBot2 / Koishi / 自研脚本）完全不需要知道对面是 Windows 还是 Linux。

## 1. 支持矩阵

| 模块 | Windows | Linux | 说明 |
|------|---------|-------|------|
| `milky` / `satori` | ✅ | ✅ | 纯网络协议，本来就跨平台 |
| OneBot 11 服务端（`serve onebot`） | ✅ | ✅ | 标准库 asyncio，无平台分支 |
| `ntqq-native`（自研 SSO） | ✅ | ✅ | 纯 Python（TEA/ECDH/protobuf） |
| `ntqq-hook` 进程发现 | ✅ | ✅ | Windows: Toolhelp 快照；Linux: `/proc` |
| `ntqq-hook` 通信 | 命名管道 | Unix socket | 帧协议完全相同 |
| `ntqq-hook` 注入 | `CreateRemoteThread+LoadLibraryW` | `ptrace + dlopen` | Linux 需要权限 |
| `ntqq-client`（键鼠） | ✅ `SendInput` | 🟡 需 `xdotool`/`ydotool` | Wayland 下只能用 ydotool |

## 2. 三处差异

### 2.1 进程发现

Windows 要区分 Electron 主进程和十几个渲染进程，靠命令行里的 `--type=`。
Linux 一样：`/proc/<pid>/cmdline` 也是 NUL 分隔，转成空格后规则完全复用。

```python
# arklink/ntqq/hookproc.py
DEFAULT_EXE_NAMES = ("QQ.exe", "QQ", "qq")   # Linux 官方包的二进制叫 qq
```

Linux 版 QQNT 通常装在 `/opt/QQ/qq`，进程名就是 `qq`。

### 2.2 通信：命名管道 → Unix domain socket

| | Windows | Linux |
|---|---|---|
| 地址 | `\\.\pipe\arklink.<pid>.control` | `$XDG_RUNTIME_DIR/arklink.<pid>.control`（没有则 `/tmp`） |
| 独占 | 可以独占（`ERROR_PIPE_BUSY`） | 不能独占，多个客户端都能连 |
| 异步 | 必须 overlapped I/O | asyncio 流，天然异步 |
| 清理 | 随进程消失 | 文件会残留，`bind` 前先 `unlink` |

帧格式（40 字节头 + cmd/msg/body）**一个字都没改**，所以 `hookproto.py` 两边共用。

> 为什么不用 Linux 的「抽象命名空间」（`@arklink...`）？因为它不出现在文件系统里，
> 外部就没法用「列目录」判断管道是否存在，`probe_pipe` 会永远返回 missing。
> 用文件形式只多一个残留文件，换来的可观测性更值。

### 2.3 注入：远程线程 → ptrace

Windows 那套 `CreateRemoteThread(LoadLibraryW)` 在 Linux 没有对应物。这里的做法是
**借用目标进程已有的线程**：

1. `PTRACE_ATTACH` 停下目标，`PTRACE_GETREGS` 保存全部寄存器
2. 把 `.so` 路径写到它的栈上（`PTRACE_POKEDATA`，尾部不足 8 字节要读-改-写）
3. 改寄存器：`rdi=路径`、`rsi=RTLD_NOW`、`rip=目标 libc 的 dlopen`、`orig_rax=-1`
4. 返回地址故意写 `0`，`PTRACE_CONT` 之后 dlopen 返回时会触发 `SIGSEGV`
5. 这个信号被我们（tracer）截获，目标进程自己看不到；`PTRACE_SETREGS` 还原、`PTRACE_DETACH`

好处：没有幽灵线程残留，模块是正经 `dlopen` 进来的，可以 `dlclose` 干净卸载。
坏处：`ptrace` 需要权限（见下），且只实现了 **x86_64 / glibc**。

## 3. 权限

```bash
# 看当前策略
cat /proc/sys/kernel/yama/ptrace_scope
# 0 = 允许 attach 任意同用户进程
# 1 = 只允许 attach 子进程（Ubuntu 默认，会失败）
# 3 = 完全禁止

# 临时放开（重启失效）
sudo sysctl -w kernel.yama.ptrace_scope=0
# 永久
echo 'kernel.yama.ptrace_scope = 0' | sudo tee /etc/sysctl.d/10-ptrace.conf
```

不想改系统策略的话，还有一条路：**用 `LD_PRELOAD` 启动客户端**（不需要 ptrace）：

```bash
LD_PRELOAD=/path/to/libarklink-hook-x64.so /opt/QQ/qq &
```

代价是必须在启动时就注入，不能「先登录再挂上去」。适合做成桌面启动器。

## 4. 编译

```bash
# 依赖只要 g++ 和 pthread
sudo apt install build-essential
python tools/build_hook.py
# 产物：hookdll/build/libarklink-hook-x64.so
```

脚本会自动选源码（`arklink_hook.cpp` / `arklink_hook_posix.cpp`）、
自动切编译参数（`-shared -fPIC -pthread`），并用 `nm -D` 检查
`ArkLinkPrepareUnload` 是否导出 —— 缺了它 `dlclose` 时工作线程会跳进已释放内存。

## 5. 跑起来

```bash
# ① 启动并登录官方 QQ（Linux 版）
/opt/QQ/qq &

# ② 一键接入
python -m arklink hook --install

# ③ 起 OneBot 服务端，上层改地址即可
python -m arklink serve onebot --type ntqq-hook --port 6700 --token t
```

`--install` 在 Linux 上走的是 `ptrace` 分支，失败时的报错会直接告诉你是
`ptrace_scope` 还是文件不存在。

## 6. 已知限制

| 限制 | 影响 | 计划 |
|------|------|------|
| 只支持 x86_64 | ARM 服务器（如 Oracle 免费机）跑不了注入 | 需要补 aarch64 的寄存器结构 |
| 只支持 glibc | Alpine（musl）不行 | 需要另一套符号解析 |
| 不能 attach 不同用户的进程 | 客户端用 root 起、脚本用普通用户会失败 | 统一用户启动 |
| Wayland 下 xdotool 无效 | `ntqq-client` 通道不可用 | 改用 ydotool（需要 uinput 权限） |
| 消息分发 hook 未接 | 与 Windows 同一个缺口：DLL/.so 还没 hook 内核 API | 见 [协议级封禁.md](协议级封禁.md) |

## 7. 为什么 Linux 反而更好做

- QQNT 的 Linux 版是标准 Electron，符号表比 Windows 版好拿（`nm -D` / `readelf`）；
- 没有 PEB、没有模块链表隐藏这类对抗，`/proc/<pid>/maps` 一目了然；
- 服务器上跑无人值守比 Windows 稳，配合 systemd 可以做到开机自启 + 崩溃重拉。

所以 Linux 不是「兼容一下」，而是长期部署的主场：Windows 留给个人桌面，
Linux 留给 7×24 的服务器。
