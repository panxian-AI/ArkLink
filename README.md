# ArkLink

ArkBot 的**协议外挂层**。四个通道、一套契约：

| 通道 | 用途 | 状态 |
|------|------|------|
| `milky` | Milky 1.3（NapCat / Lagrange / Acidify / LLOneBot） | ✅ 可用 |
| `satori` | Satori v1（Koishi / Chronocat，顺带接上 Telegram/Discord） | ✅ 可用 |
| `ntqq-hook` | 检测官方 QQ 进程 → 注入自研模块 → 管道收推送（Win + Linux） | 🟡 链路已通真机，收包侧待接内核 API |
| `ntqq-native` | QQ NT 原生 SSO：扫码登录 / 只读花名册 / 收推送 | 🟡 只读可用，不发消息 |
| `ntqq-client` | 操作**已登录的官方客户端**（Win32 键鼠，只发文本） | ⚠️ 可用，最后一条通知腿 |

它不是又一个 bot 框架。业务插件写在 [ArkBot](https://github.com/panxian-AI/ArkBot) 里，
本仓库只负责「协议 ↔ 统一事件」的翻译和逆向工具箱。
两者依赖单向、版本独立、都能单独安装 —— 详见
[docs/两个仓库的关系.md](docs/两个仓库的关系.md)：

```
   插件（一次编写，永不改动）
        │  统一事件 / 统一 API
   ┌────▼─────────────────────────────┐
   │  ArkBot 内核（路由 / 发件箱 / 调度）│
   └────┬──────────┬──────────┬───────┘
        │          │          │
   OneBot 11    Milky     Satori      官方 Bot API（ArkBot 内置）
   （主仓）     （本仓库）  （本仓库）   （合规兜底）
        │          │          │
        └──────────┴──────────┴─→ NapCat / Lagrange / Koishi …
                                      │
                                NTQQ 原生 SSO（本仓库研究态）
```

## 为什么是这三个

- **Milky**：OneBot 11 的重设计版，强类型、语义清晰，是当前 QQ 生态最像「下一代标准」的东西；
- **Satori**：Koishi 生态的通用协议，接上它等于一次拿到多平台；
- **NTQQ 原生**：不要中间层，直接说 SSO。抗封最强、工程最难，所以只做脚手架。

前两个和 OneBot 是**同生共死**的关系（协议端仍然是 NapCat/Lagrange 这类实现）。
真正「封不掉」的只有官方 API —— 这也是 ArkBot 双通道架构存在的原因，
见 [docs/协议逆向分析.md](docs/协议逆向分析.md)。

## 快速开始

```bash
git clone https://github.com/panxian-AI/ArkLink && cd ArkLink
pip install -e ".[test]"
python -m arklink selftest        # 13 项自检，不联网
pytest tests -q                   # 181 项测试（Linux 上另有 8 项平台测试）
python -m arklink version         # 当前版本 + 新版本检测
python -m arklink doctor          # 迁移体检：环境/模块/进程/管道/服务端/配置
```

### 接一个 Milky 协议端

```toml
# bot.toml（ArkBot 的配置）
[[adapters]]
type = "milky"                       # 装了本包后直接写名字
base_url = "http://127.0.0.1:3000"
access_token = "your-token"
```

```bash
pip install "arkbot @ git+https://github.com/panxian-AI/ArkBot"
arkbot -c bot.toml
```

### 接一个 Satori SDK

```toml
[[adapters]]
type = "satori"
api_base = "http://127.0.0.1:5140"
token = "your-token"
platform = "qq"
```

### 只看不接（协议探针）

```bash
python -m arklink probe-milky --url http://127.0.0.1:3000 --token t
python -m arklink probe-satori --api http://127.0.0.1:5140 --token t
```

打印**翻译后的 OneBot 事件**，不启动插件、不回消息 —— 接新协议时最快的确认方式。

### 原生协议：扫码 → 只读 → 收推送

```bash
python -m arklink login --dry-run          # 只打印 0x31 申请二维码的字节，对拍用
python -m arklink login --out qr.png       # 真机扫码，票据落到 data/ntqq.json
python -m arklink roster --json            # 好友 + 群，验证票据是否真的有效
```

这一步的意义：**能扫码说明 ECDH/TEA/封包全对，能拉花名册说明票据与 OIDB 全对。**
发消息故意不做（要过 A2 校验和频率风控，出问题无法定位），
详见 [docs/扫码登录.md](docs/扫码登录.md)。

### 从别的框架迁移

NapCat / SnowLuma / ArkBot / NoneBot2 / Koishi / 自研脚本，各自怎么换、要做几步、
API 有哪些差异、出事怎么回退，见 [docs/从其他框架迁移.md](docs/从其他框架迁移.md)。
一句话：**改地址，不改代码。**

### 一键替换 NapCat（上层零改动）

```bash
python -m arklink hook --install                                  # 找进程→注入→自检→打印接入地址
python -m arklink serve onebot --type ntqq-hook --port 6700 --token t
```

`serve onebot` 是一个标准库写的 **OneBot 11 服务端**（正向 WS + HTTP API + HTTP 上报同端口，
四个 WS 路径都认），所以 NoneBot2 / Koishi / AstrBot / 自研脚本只改地址、不改代码。
细节见 [docs/一键替换NapCat.md](docs/一键替换NapCat.md)。

### 注入通道：自己登 QQ → 检测进程 → 注入 → 收推送

```bash
python tools/build_hook.py                                        # 编译自研 hook DLL
python -m arklink hook --list                                     # 找 QQ 主进程 + 探管道
python -m arklink hook --inject --dll hookdll/build/arklink-hook-x64.dll
python -m arklink hook --probe --prefix arklink --seconds 30 --ping
```

思路来自对 SnowLuma 的逆向：DLL 注入 QQNT 主进程、起两条命名管道对外通信。
我们把这套结构自研复刻（自己的帧协议、自己的 DLL），并与 `ntqq-native` 的
`push.to_onebot` 打通。协议格式、真机步骤和踩坑记录见
[docs/进程注入通道.md](docs/进程注入通道.md)。

### 发消息 + WebUI 管理

```bash
python -m arklink send --to 示例好友 --text "hi"          # 默认演练，加 --yes 才真发
python -m arklink serve webui --port 6800 --password 你的口令   # 浏览器打开 http://127.0.0.1:6800
```

发送走**官方客户端本身**（认账号窗口 → 搜索会话 → 粘贴 → 回车），消息由客户端产生，
不拼包、不伪造协议；账号/收件人/群聊三条硬闸门写在代码里，突发连发与多对象由引擎
排队串行（保序）。WebUI 有口令登录、状态/配置/日志/发送/体检一页，支持多收件人多条。
**限制**：Windows 上这条路要抢前台焦点，做不到"最小化后台发送"。要真正后台、
不打扰宿主机的任何程序，把 QQ 放进无头 Linux 的虚拟屏幕里跑：

```bash
python -m arklink send --to 示例好友 --text "hi" --engine x11 --yes
```

配方（Debian 12 + Xvfb + QQ Linux + xdotool，含 Dockerfile）见
[docs/无头Linux运行.md](docs/无头Linux运行.md)。一键装完用
`tools/setup_guest.sh`（自动处理中文字体与 24.04 的 `libasound2t64` 改名）。

资源占用（估算）：VirtualBox + Ubuntu Server 最小安装约 **3.4GB 磁盘、2GB 内存**，
把镜像放在 D:/E: 就完全不占系统盘；WSL2 形态约 2.3GB；Windows 虚拟机 22GB 起步，不划算。

在 Windows 上判断"我到底有没有隔离"，跑 `python tools/check_session.py`：
它读每个 QQ 进程的 `SessionId`，和当前进程不同才算真隔离（同会话必然抢焦点、
共用剪贴板）。四种"另开桌面"的区别见
[docs/Windows会话隔离.md](docs/Windows会话隔离.md)。

### Linux

同一套代码在 Linux 上也能跑：进程发现走 `/proc`，管道换成 Unix domain socket
（帧协议不变），注入走 `ptrace + dlopen`。模块编译成 `.so`：

```bash
sudo apt install build-essential && python tools/build_hook.py   # libarklink-hook-x64.so
python -m arklink hook --install                                  # 需要 ptrace_scope=0
```

差异、权限、坑都在 [docs/Linux适配.md](docs/Linux适配.md)。

### 协议级封禁的应对思路

如果腾讯未来在**协议层**封禁（看流量指纹/客户端完整性/账号行为），再强的协议实现也会死。
我们的答案是**零协议改写**：流量永远由官方客户端自己产生，我们只在进程内借用它的函数；
注入方式、旁路读取、节律策略三层递进。见 [docs/协议级封禁.md](docs/协议级封禁.md)。

## 迁移体检与验收

```bash
python -m arklink doctor            # 自动 10 项：环境、hook 模块、QQ 进程、管道、服务端、配置
```

自动检查只证明「管道通」；收/发消息和异常场景按
[docs/迁移验收清单.md](docs/迁移验收清单.md) 手动过（A 基础设施 / B 收消息 /
C 发消息 / D 会话与异常 / E 上层框架，每项带排查方向）。

## 版本管理

```bash
python -m arklink version            # 当前版本，顺带检查有没有新版
python -m arklink versions           # 所有历史版本（日期 + 更新说明）
python -m arklink update             # 看最新版和升级命令（不执行）
python -m arklink update --to 0.4.0  # 回退：先打印将执行的命令
python -m arklink update --to 0.4.0 --yes   # 真的执行
```

**两个仓库分别管**（`--project arkbot` 切到框架本体）：

```bash
python -m arklink version --project arkbot            # ArkBot 自己的版本
python -m arklink versions --project arkbot
python -m arklink update --project arkbot --to 0.3.0 --config bot.toml
```

**回退会先算清楚**：目标版本缺什么功能，直接列出来并拦住（加 `--force` 才继续）。
检查表在 `arklink/update.py` 的 `FEATURES` / `BOT_FEATURES`，新增能力时补一行即可。

**检测可以关**：

```bash
python -m arklink update --disable-check   # 永久关闭，写入 ~/.arklink/state.json
export ARKLINK_NO_UPDATE=1                 # 临时关闭（CI 建议设上）
```

结果缓存 6 小时；离线、被关掉、CI 环境下都只是「不提示」，不影响任何命令。

## 逆向工具箱

```bash
python -m arklink selftest                                   # TEA / SSO / protobuf / 管线
python -m arklink decode --hex 0200000014000000071234... --key 0123456789abcdef --pb
python -m arklink pb --hex 089601120361726b                  # 把 protobuf 拆成树
```

| 模块 | 内容 |
|------|------|
| `arklink/ntqq/tea.py` | QQ TEA（16 轮）+ CBC/PCBC + 三种填充，含标准测试向量 |
| `arklink/ntqq/sso.py` | SSO 封包编解码（magic/length/seq/cmd/uin/body/tail），处理粘包 |
| `arklink/ntqq/pb.py` | protobuf 线格式编解码 + 人类可读 dump |
| `arklink/ntqq/qsign.py` | 本地签名服务客户端（GET/POST 双路） |
| `arklink/ntqq/ecdh.py` | secp192k1 密钥协商（纯 Python，含阶/曲线自洽测试） |
| `arklink/ntqq/code2d.py` | `wtlogin.trans_emp`：0x31 申请二维码 / 0x12 轮询 / 0x13 换票据 |
| `arklink/ntqq/session.py` | 扫码状态机 + keystore 落盘（0600），支持票据恢复 |
| `arklink/ntqq/ntframe.py` | NT 帧（protocol 12/13，服务名字符串）—— 真正发 OIDB 的那层 |
| `arklink/ntqq/roster.py` | 好友（0xfd4，cookie 翻页）/ 群（0xfe5）列表 |
| `arklink/ntqq/push.py` | 推送 `MsgPush` → OneBot 11 事件（段/撤回，未知段不丢事件） |
| `arklink/ntqq/native.py` | 把上面几件串起来：拼包→加密→发→收→解密→解包，登录/只读/收推送 |
| `arklink/ntqq/host.py` | 操作已登录的官方客户端：进程/窗口探测、剪贴板粘贴、按键注入 |
| `arklink/ntqq/hook*.py` | 注入通道：帧协议 / 进程发现 / 注入卸载 / 管道客户端 / 适配器 |
| `hookdll/` | 自研 hook DLL（C++，管道服务端 + ping/status + uin 探测），`tools/build_hook.py` 编译 |

`ntqq-native` 是**只读通道**：能扫码、能拉好友/群、能收推送；`send_*` 一律返回 `1404`，
框架自动换通道。设备指纹与发送侧风控仍未做，差距清单见
[docs/协议逆向分析.md](docs/协议逆向分析.md)，上手步骤见 [docs/扫码登录.md](docs/扫码登录.md)。

## 目录

```
arklink/
├── base.py        适配器契约（不 import arkbot，可独立使用）
├── transport.py   标准库 HTTP（urllib + to_thread）
├── milky.py       Milky 1.3：/api + /event，段与事件映射
├── satori.py      Satori v1：/v1/* + WS opcode，XML 内容编解码
├── registry.py    type 名 → 工厂
├── __main__.py    probe / decode / pb / selftest
└── ntqq/          TEA · SSO · pb · ecdh · code2d · session · roster · push · ntframe · host
docs/
├── 协议逆向分析.md  三层结构、官方开放协议、NTQQ 逆向、对拍清单
├── 挂载到ArkBot.md  三种挂载方式、契约、双通道配置、自己加协议
├── Milky接入.md     配置、映射表、三处协议差异、排障
└── Satori接入.md    XML 编码、id 映射、能力对照、排障
tests/
├── fakes/            假 Milky / 假 Satori（真 HTTP + 真 WS）
├── test_milky.py     段/事件/API 映射
├── test_satori.py    内容编解码/事件/API
├── test_ntqq_tea.py  TEA 向量与往返
├── test_ntqq_sso.py  SSO 布局/protobuf/管线
├── test_ntqq_host.py 客户端通道：结构体/热键/纯文本过滤
├── test_ntqq_code2d.py 二维码封包、状态机、票据落盘
├── test_ntqq_login.py  ECDH/NT 帧/推送入队
├── test_ntqq_roster.py 好友群列表、OIDB 信封、cookie 翻页
├── test_ntqq_push.py   推送 → OneBot 事件映射
└── test_mount.py     ★ 真实 ArkBot + 假协议端 的端到端挂载
```

## 文档

| 文档 | 内容 |
|------|------|
| [docs/扫码登录.md](docs/扫码登录.md) | 三步封包、状态码、命令用法、推送映射、未验证清单与校准方法 |
| [docs/协议逆向分析.md](docs/协议逆向分析.md) | 协议分层、为什么影分身容易被踢、官方开放协议能力、NTQQ 封包/TEA/qsign、对拍清单 |
| [docs/挂载到ArkBot.md](docs/挂载到ArkBot.md) | 三种挂载方式、适配器契约、双通道、加新协议 |
| [docs/Milky接入.md](docs/Milky接入.md) | Milky 配置、映射表、协议差异、排障 |
| [docs/Satori接入.md](docs/Satori接入.md) | Satori 配置、XML 编码、id 映射、能力对照 |
| [docs/发消息链路.md](docs/发消息链路.md) | 三条发送路线、UI 通道踩坑、进程内直调逆向结论、1.0 清单 |
| [docs/无头Linux运行.md](docs/无头Linux运行.md) | 无头 Linux 跑 QQ、Xvfb + xdotool 配方、Dockerfile、已知坑、**各形态磁盘/内存账单** |
| [docs/Windows会话隔离.md](docs/Windows会话隔离.md) | 「另开一个桌面」四种做法哪个能行：虚拟桌面/CreateDesktop/独立窗口站/独立会话，及 CDP 备选 |

## 合规与安全

- 本项目用于**协议研究与自有账号实验**（当前只做只读：登录、拉列表、收推送）。模拟登录通常违反 QQ 用户协议，
  由此产生的封号、冻结风险由使用者承担；
- 不提供任何绕过风控、批量注册/登录、营销群发的能力；
- Milky / Satori 协议端**不要监听公网**：它们没有 TLS，只适合 `127.0.0.1` 或内网；
- 抓包与回放只用于自己账号的流量。

## License

MIT
