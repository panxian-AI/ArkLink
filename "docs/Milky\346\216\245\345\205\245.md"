# Milky 接入

Milky 是「重设计版 OneBot」：强类型、显式 `data` 包装、事件用 `event_type` 打标签。
2025 年起 NapCat / Lagrange / Acidify / LLOneBot / LuckyLilliaBot 都支持，
NoneBot2 与 Koishi 也已有官方适配器。

- 协议文档：<https://milky.ntqqrev.org/>
- 本仓库实现：`arklink/milky.py`（Milky 1.3）

## 1. 协议端怎么开

以 NapCat 为例（不同实现入口名字不同，认准「Milky」字样）：

1. 打开 NapCat 的「网络配置」→ 新建 **Milky** 服务器；
2. 监听 `127.0.0.1:3000`（**不要监听 0.0.0.0**，Milky 没有 TLS）；
3. 记下 `access_token`；
4. 保存并启用。协议端会自己提供 `POST /api/{endpoint}` 与 `GET /event`。

Milky 是**一个端口两个端点**，不需要像 OneBot 那样分开配 HTTP 与 WS。

## 2. 挂到 ArkBot

```toml
[[adapters]]
type = "milky"                       # 或 "arklink.milky:MilkyAdapter"
base_url = "http://127.0.0.1:3000"
access_token = "your-token"
# event_url = "ws://127.0.0.1:3000/event"   # 默认按 base_url 推导，一般不用写
reconnect_interval = 3.0
```

前提：`pip install arklink`（或 `pip install -e .`）后 `entry_points` 生效，
ArkBot 的加载器才认 `type = "milky"`。没装包时用点分路径也行。

单独验证：

```bash
python -m arklink probe-milky --url http://127.0.0.1:3000 --token your-token
```

它会打印**翻译后的 OneBot 事件**，不启动插件、不回消息 —— 接新协议最快的确认方式。

## 3. 翻译规则

### 消息段

| Milky | 本适配器 | 备注 |
|-------|----------|------|
| `text` | `text` | |
| `mention` | `at` | `qq = user_id` |
| `mention_all` | `at` (`qq=all`) | |
| `face` | `face` | |
| `reply` | `reply` | `id = message_seq` |
| `image` / `record` / `video` | 同名段 | `file` 取 `temp_url`，取不到退回 `resource_id` |
| `file` | `file` | |
| `forward` | `forward` | 只有 `id`，要内容得再调 `get_forwarded_messages` |
| `market_face` | `mface` | |
| `light_app` / `xml` | `json` / `xml` | |
| `markdown` | `text` | |

发送方向做逆映射；**不认识的段会被丢弃并打 debug 日志**，因为 Milky 是强类型协议，
塞未知字段会让整条消息被协议端以 `-400` 拒绝，丢一段好过整条失败。

### 事件

| Milky `event_type` | ArkBot |
|--------------------|--------|
| `message_receive`（`group` / `friend` / `temp`） | `message` / `group` 或 `private` |
| `message_recall` | `notice.group_recall` / `friend_recall` |
| `group_member_increase` / `decrease` | `notice.group_increase` / `group_decrease` |
| `group_mute` / `group_whole_mute` | `notice.group_ban` |
| `group_admin_change` | `notice.group_admin` |
| `friend_nudge` / `group_nudge` | `notice.notify` + `poke` |
| `*_file_upload` | `notice.group_upload` / `offline_file` |
| `friend_request` / `group_join_request` / `group_invitation` | `request` |
| 其它（群名变更、精华、表态…） | `notice` + `notice_type = 原事件名`，**不丢信息** |

原始报文保留在 `event.raw["native"]`，插件需要细节时直接读。

## 4. 三处「协议差异」要注意

1. **`message_id` 就是 `message_seq`**。Milky 的撤回/查消息需要
   `(message_scene, peer_id, message_seq)` 三元组，OneBot 只给一个 id。
   适配器在收到消息/发出消息时维护索引，所以：
   - 回复自己刚发的消息 → 正常；
   - 撤回**重启前**的消息 → 索引没了，返回 `1404`，框架会降级到其它通道。
2. **没有「连接成功」信令**。收到第一个事件才算可用，所以刚启动时
   `is_connected` 是 `False`；主动发送会先失败一次再进发件箱，几秒后补发。
3. **未映射的动作返回 `1404`**（不是抛异常）。这是 ArkBot 的约定：
   `1404` = 本通道不支持，换通道或入发件箱，不要打断业务。

## 5. 排障

| 现象 | 原因 | 处理 |
|------|------|------|
| 启动即 `401` | token 不匹配 | 核对 `access_token`；Milky 用 `Authorization: Bearer` |
| 能收事件、发不出 | 协议端未登录 / 掉线 | 看日志里的 `bot_offline`，重新登录协议端 |
| 发图失败 `-400` | `uri` 不是协议端能取的地址 | 用绝对路径、http(s) URL 或 base64 |
| 撤回总是 1404 | 消息不在索引里（重启/别处发的） | 属预期，交给其它通道 |
| 中文乱码 | 协议端用了非 UTF-8 | Milky 规范要求 UTF-8，换实现 |
