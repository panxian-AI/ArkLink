# 挂载到 ArkBot

ArkLink 不是又一个 bot 框架，它是**协议外挂**：把新协议翻译成 ArkBot 的统一事件，
插件代码一行不改就能跑在新通道上。

## 1. 三种挂载方式

### 方式 A：装包 + 用名字（推荐）

```bash
pip install arklink
# 想让 mount 测试也跑起来：pip install "arkbot @ git+https://github.com/panxian-AI/ArkBot"
```

```toml
[[adapters]]
type = "milky"
base_url = "http://127.0.0.1:3000"
access_token = "xxx"
```

原理：`pyproject.toml` 里声明了

```toml
[project.entry-points."arkbot.adapters"]
milky = "arklink.milky:create"
```

ArkBot 的加载器会用 `importlib.metadata` 找到它。

### 方式 B：点分路径（不装包、改路径即可）

```toml
[[adapters]]
type = "arklink.milky:MilkyAdapter"
base_url = "http://127.0.0.1:3000"
```

适合本地开发：`PYTHONPATH` 指到仓库根目录就行。

### 方式 C：写代码注册

```python
from arkbot import ArkBot
from arklink.milky import MilkyAdapter

app = ArkBot(adapter=MilkyAdapter(base_url="http://127.0.0.1:3000", access_token="x"))
app.run()
```

## 2. 契约（为什么可以「零改内核」）

ArkBot 通过鸭子类型调用适配器，只需要这些成员：

| 成员 | 作用 |
|------|------|
| `name` | 通道名，日志与路由标识 |
| `is_connected` | 是否可发送（决定是否参与路由） |
| `wait_connected(timeout)` | 调度器/启动阶段等就绪 |
| `on_event(handler)` | 框架注入事件回调 |
| `run_forever()` / `stop()` / `connect()` / `disconnect()` | 生命周期 |
| `call_api(action, params)` | 返回 `data`；不支持的动作抛 **`retcode=1404`** |
| `send_reply(event, chain, quote)` | 按来源通道回复；`False` = 不负责 |

事件侧更简单：适配器把协议报文翻译成 **OneBot 形状的 dict**，
交给 `Event.from_raw()`，框架自动还原成 `MessageEvent` / `NoticeEvent` / `RequestEvent`。

```python
await self._emit_dict({
    "post_type": "message",
    "message_type": "group",
    "group_id": 123,
    "user_id": 456,
    "message_id": 789,
    "message": [{"type": "text", "data": {"text": "hi"}}],
    "raw_message": "hi",
    "sender": {"nickname": "阿鱼", "role": "admin"},
})
```

`arklink/base.py` 的 `LinkAdapter` 已经把这套封装好了，新协议只要实现
`connect()` / `call_api()` + 一个 `event_to_raw()`。

## 3. 和官方通道组双通道

```toml
# 逆向侧：能力强，但可能被封
[[adapters]]
type = "milky"
base_url = "http://127.0.0.1:3000"

# 官方侧：能力受限，但不会因为逆向被修而消失
[[adapters]]
type = "official_qq"
app_id = "..."
app_secret = "..."
```

行为（由 ArkBot 内核保证）：

- 事件从哪条通道来，回复就走哪条（保持协议语义，比如官方被动回复的 `msg_id`）；
- 某条通道 `is_connected = False` 或返回 `1404`，自动换下一条；
- 全挂时 `send_*` 进离线发件箱，通道恢复后补发（TTL 内）。

## 4. 验收

```bash
pytest tests -q                  # 83 项：协议映射 + 封包密码学 + 跨仓库挂载
python -m arklink selftest      # 8 项：TEA/SSOPacket/protobuf/管线
```

`tests/test_mount.py` 是真正的端到端：起假 Milky / 假 Satori 协议端，
把 `MilkyAdapter` / `SatoriAdapter` 挂进**真实 ArkBot**，推一条 `/echo 你好`，
断言插件通过新通道把消息发了出去。这条链路过了，才算「挂上了」。

## 5. 自己加第四种协议

1. 复制 `arklink/milky.py` 改协议细节；
2. 实现 `event_to_raw(payload) -> dict`（照 OneBot 字段填）；
3. `call_api` 里把 OneBot 动作名映射到你的协议；不支持的抛 `api_error(action, 1404)`；
4. 在 `arklink/registry.py` 与 `pyproject.toml` 的 entry point 里登记；
5. 写测试：假协议端 + `test_mount.py` 那种端到端断言。

不用改 ArkBot 的任何一行。
