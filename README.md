# ArkLink

ArkBot 的**协议外挂层**。三个通道、一套契约：

| 通道 | 用途 | 状态 |
|------|------|------|
| `milky` | Milky 1.3（NapCat / Lagrange / Acidify / LLOneBot） | ✅ 可用 |
| `satori` | Satori v1（Koishi / Chronocat，顺带接上 Telegram/Discord） | ✅ 可用 |
| `ntqq-native` | QQ NT 原生 SSO 协议（TEA / 封包 / qsign） | 🧪 研究态，不登录 |

它不是又一个 bot 框架。业务插件写在 [ArkBot](https://github.com/panxian-AI/ArkBot) 里，
本仓库只负责「协议 ↔ 统一事件」的翻译和逆向工具箱：

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
python -m arklink selftest        # 8 项自检，不联网
pytest tests -q                   # 83 项测试
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
| `arklink/ntqq/native.py` | 拼包→加密→发→收→解密→解包 的完整管线（`selftest()` 有单测） |

`NativeQQAdapter` 挂载后**永远不健康**：`call_api` 返回 `1404`，框架自动降级，
不会抢占总通道。登录握手（`wtlogin`）、设备指纹、ECDH 密钥协商尚未实现，
差距清单与优先级见 [docs/协议逆向分析.md](docs/协议逆向分析.md)。

## 目录

```
arklink/
├── base.py        适配器契约（不 import arkbot，可独立使用）
├── transport.py   标准库 HTTP（urllib + to_thread）
├── milky.py       Milky 1.3：/api + /event，段与事件映射
├── satori.py      Satori v1：/v1/* + WS opcode，XML 内容编解码
├── registry.py    type 名 → 工厂
├── __main__.py    probe / decode / pb / selftest
└── ntqq/          TEA · SSO · protobuf · qsign · native
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
└── test_mount.py     ★ 真实 ArkBot + 假协议端 的端到端挂载
```

## 文档

| 文档 | 内容 |
|------|------|
| [docs/协议逆向分析.md](docs/协议逆向分析.md) | 协议分层、官方开放协议能力与限制、NTQQ 封包/TEA/qsign、对拍清单 |
| [docs/挂载到ArkBot.md](docs/挂载到ArkBot.md) | 三种挂载方式、适配器契约、双通道、加新协议 |
| [docs/Milky接入.md](docs/Milky接入.md) | Milky 配置、映射表、协议差异、排障 |
| [docs/Satori接入.md](docs/Satori接入.md) | Satori 配置、XML 编码、id 映射、能力对照 |

## 合规与安全

- 本项目用于**协议研究与自有账号实验**。模拟登录通常违反 QQ 用户协议，
  由此产生的封号、冻结风险由使用者承担；
- 不提供任何绕过风控、批量注册/登录、营销群发的能力；
- Milky / Satori 协议端**不要监听公网**：它们没有 TLS，只适合 `127.0.0.1` 或内网；
- 抓包与回放只用于自己账号的流量。

## License

MIT
