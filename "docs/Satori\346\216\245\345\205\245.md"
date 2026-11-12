# Satori 接入

Satori 是 Koishi 生态的跨平台聊天协议：SDK 把 QQ / Telegram / Discord / Lark
统一成同一套 HTTP API + WebSocket 事件。对 ArkBot 的意义是**顺手接上 Koishi 那一整片生态**。

- 协议文档：<https://satori.chat/zh-CN/protocol/>
- 本仓库实现：`arklink/satori.py`（v1）

## 1. 起一个 Satori SDK

任选其一：

- **Koishi**：装 `@koishijs/plugin-server` + `adapter-qq`，开「Satori」服务，默认 `5140`；
- **Chronocat**：QQ 侧的 Satori 实现，配置 `port` 与 `token`；
- **Lagrange** 的 Satori 桥。

拿到两样东西即可：`http://127.0.0.1:5140` 和 `token`。

## 2. 挂到 ArkBot

```toml
[[adapters]]
type = "satori"                      # 或 "arklink.satori:SatoriAdapter"
api_base = "http://127.0.0.1:5140"
token = "your-token"
platform = "qq"                      # Satori-Platform 头
# user_id = "10001"                  # Satori-User-ID 头，可就绪后自动填
reconnect_interval = 3.0
```

验证：

```bash
python -m arklink probe-satori --api http://127.0.0.1:5140 --token your-token
```

## 3. 协议要点（实现里踩过的坑）

### 3.1 消息是 XML 片段，不是数组

```
<quote id="90001"/><at id="123456"/> 你好 <img src="https://x/a.png"/>
```

- 文本要 HTML 转义（`& < >`）；
- 元素一律自闭合标签 + 双引号属性；
- 部分 QQ 桥会把 `[` `]` `,` 转义成 `&#91;` `&#93;` `&#44;`（OneBot 习惯），
  解析时会还原，避免插件看到一堆实体。

对应关系：`at → at`、`quote → reply`、`img → image`、`audio → record`、
`video → video`、`file → file`、`face → face`，其余元素按标签名透出。

### 3.2 id 是字符串，ArkBot 要 int

Satori 的 `message.id` / `channel.id` 都是字符串（可能是 UUID 或 openid 形式），
而 ArkBot 的 `message_id` / `group_id` 是 `int`。处理方式：

- 纯数字 → 直接转；
- 其它 → `CRC32 & 0x7FFFFFFF` 生成稳定伪 ID（同值永远同 ID，跨重启一致）；
- 反向映射（伪 ID → 原生字符串）在适配器内存里维护，用于发送与撤回。

这跟 ArkBot 官方通道把 openid 映射成伪 QQ 号是同一套思路，插件因此完全不用改。

### 3.3 私聊 vs 群聊看 `channel.type`

| type | 含义 | 映射 |
|------|------|------|
| 0 | TEXT（群频道） | `message_type = group`，`group_id = channel.id` |
| 1 | DIRECT（私聊） | `message_type = private`，`user_id = user.id` |
| 2 / 3 | CATEGORY / VOICE | 按群处理 |

QQ 上 `guild.plain` 特性成立时，一个群只有一个频道，`channel.id` 就是群标识。

### 3.4 WebSocket 是有状态的

```
客户端 → {"op": 3, "body": {"token": "...", "sn": 0}}     IDENTIFY
服务端 → {"op": 4, "body": {"logins": [...]}}              READY
服务端 → {"op": 0, "body": {...event...}}                 EVENT
客户端 → {"op": 1}  每 10s 心跳
```

`sn` 用于断线续传：重连时带上本地最大 `sn`，SDK 会补发缺失事件。
本实现目前从 0 开始（全量重收），够用但会有重复事件 —— 插件侧请按
`message_id` 去重。

## 4. 能力对照

| 动作 | 支持 | 说明 |
|------|------|------|
| 收发文本/图片/语音/视频/文件 | ✅ | 走 `message.create` |
| 引用回复 | ✅ | `<quote>` |
| 撤回 / 取消息 | ✅ | 需要本通道见过的 `message_id` |
| 群列表 / 群成员 | ✅ | `guild.list` / `guild.member.list` |
| 禁言 / 踢人 / 改名片 | ❌ | Satori 标准 API 无对应资源，返回 `1404` |
| 戳一戳 | ⚠️ | 用「@对方 + 空消息」近似，不保证各平台一致 |

缺失的动作会返回 `1404`，ArkBot 会自动换成别的通道去做（比如官方 API 或 OneBot），
所以**不需要为了一个禁言功能放弃 Satori**。

## 5. 排障

| 现象 | 原因 | 处理 |
|------|------|------|
| 一直没 READY | token 不对 / SDK 没开 WebSocket | 看 SDK 日志，确认 `/v1/events` 可连 |
| 事件重复 | 重连从 `sn=0` 全量重收 | 按 `message_id` 去重（或自己维护 `sn`） |
| 发出去是纯文本 | 内容没转义或标签写错 | 用 `encode_content()`，别手拼字符串 |
| 群号对不上 | 用的是伪 ID | 插件里不要拿 `group_id` 和真实群号比 |
