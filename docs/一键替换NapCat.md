# 一键替换 NapCat（上层零改动）

目标：用户的 NoneBot2 / Koishi / AstrBot / 自研脚本**一行代码不改**，
把地址从 NapCat 换到 ArkLink 就能继续跑。

## 1. 三步

```bash
# ① 自己打开 QQ 客户端并登录（登录态由官方客户端维持，我们不碰密码）

# ② 一键接入：找主进程 → 编译/注入 → 握手自检 → 打印接入命令
python -m arklink hook --install

# ③ 起一个 OneBot 11 服务端（NapCat 的端口号，习惯上 6700/3000 都行）
python -m arklink serve onebot --type ntqq-hook --port 6700 --token your-token
```

`--install` 输出会直接给出上层要填的地址：

```
ws://127.0.0.1:6700/onebot/v11/ws
http://127.0.0.1:6700/
```

## 2. 上层怎么改

| 上层 | 原来 | 现在 |
|------|------|------|
| NoneBot2（反向 WS） | `ws://127.0.0.1:3001/onebot/v11/ws` | 把主机端口改成 ArkLink 的 |
| NoneBot2（正向 WS） | 连 NapCat 的 `ws://127.0.0.1:3001` | 连 `ws://127.0.0.1:6700/onebot/v11/ws` |
| Koishi / AstrBot | OneBot 适配器 + 地址 | 同上，只改地址 |
| 自研脚本 | HTTP `POST /send_group_msg` | 同路径同参数，只改 base_url |

路径 `/`、`/onebot/v11/ws`、`/api/ws`、`/ws` **四个都认**，因为不同框架默认值不一样。

## 3. 支持的协议行为

- 正向 WS + HTTP API + HTTP 上报，**同一端口**
- 鉴权：`Authorization: Bearer <token>` 或 `?access_token=<token>`
- 连接即发 `meta_event/lifecycle/connect`，之后按 `--heartbeat` 周期发心跳
- 事件：`message` / `notice` / `meta_event`，段数组格式与 NapCat 一致
- 未实现的动作返回 `retcode=1404`（不是 500），上层据此自动降级到别的通道

```bash
# 自检（不需要上层框架）
curl -H "Authorization: Bearer your-token" http://127.0.0.1:6700/
curl -H "Authorization: Bearer your-token" -X POST http://127.0.0.1:6700/get_status
```

## 4. 挂到 ArkBot（可选）

不想单独跑服务端，也可以让 ArkBot 直接加载通道：

```toml
[[adapters]]
type = "ntqq-hook"
dll_path = ""            # 空 = 自动找 hookdll/build/ 或 ARKLINK_HOOK_DLL
auto_inject = true
read_only = true         # 目前只收不发；发送走别的通道
```

## 5. 现在能跑到哪一步（诚实版）

| 能力 | 状态 |
|------|------|
| 进程发现 / 注入 / 管道 / 卸载 | ✅ 真机验证 |
| OneBot 11 服务端（WS/HTTP/上报/鉴权/心跳） | ✅ 162 项测试覆盖 |
| 事件推送链路（适配器 → OneBot 事件） | ✅ 通，但依赖 DLL 送包 |
| DLL 真正收到 QQ 的推送 | ❌ 待接 `wrapper.node` 消息分发 |
| 发送消息 | ❌ 只读，`send_*` 返回 1404 |

所以现在是「管道全通、最后一公里没接」：换到你的环境里，服务端、鉴权、事件格式都已经是
NapCat 的形状，只差 DLL 把包送出来。这一步的路线见 [协议级封禁.md](协议级封禁.md)。
