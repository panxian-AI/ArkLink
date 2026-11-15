# SnowLuma in VM（收信那一半）

ArkLink 在无头 Linux 上的分工是「发」：x11 点界面，稳，但只能打字。
收信要注入 QQ 进程，那条路 ArkLink 撞墙（Electron 40 上 pre-main 有 socket/线程就自毁）。
SnowLuma 用同一门技术、把注入放在运行期，所以能进得去 —— 顺手把语音、引用、图片也补齐。

装完是两条腿并行：

| | 收信 | 发文字 | 语音 / 引用 / 图片 |
|---|---|---|---|
| ArkLink（x11） | ✗ | ✓ | ✗ |
| SnowLuma（协议） | ✓ | ✓ | ✓ |

门面（dsh-arkhub）先走 SnowLuma，失败退回 ArkLink；两个都连不上才算断。

## 装（VM 里，一条命令）

```bash
bash install.sh
```

它做这几件事：放开 `ptrace_scope`（注入器要跨进程）、装 Node 24.22.1（Ubuntu 自带 18 太旧）、
取 `SnowLuma-v2.4.2-linux-x64-lite.tar.gz`（8 MB，带 ffmpeg addon，够发语音）、解到
`/opt/snowluma`、写 `hookAutoLoad: true`（不写它只起个网页面板，不注入）、装
`snowluma.service`、把 OneBot 监听改成 `0.0.0.0`（VBox NAT 的转发到不了 127.0.0.1）、
最后打印宿主机要填的两个端口和两个 token。

## 宿主侧（Windows）

```bash
VBoxManage controlvm arklink natpf1 "snowluma-http,tcp,127.0.0.1,3102,,3000"
VBoxManage controlvm arklink natpf1 "snowluma-ws,tcp,127.0.0.1,3101,,3001"
```

**别用 3100**：那是 qq-bridge 的本地控制台，抢了不会报错，只会让转发静默失效
（表现为「连上了但收不到」，很难查）。装完自检：

```bash
curl -s -H "Authorization: Bearer <httpToken>" http://127.0.0.1:3102/get_login_info
# {"status":"ok","retcode":0,"data":{"user_id":100010001,"nickname":"阿潘"}}
```

`cordis.patch.yml` 的 `snowluma` 段填进去（门面读这三个键）：

```yaml
snowluma:
  enabled: true
  httpUrl: http://127.0.0.1:3102
  wsUrl: ws://127.0.0.1:3101
  httpToken: <httpToken>
  wsToken: <wsToken>
```

## 排障

- **日志**：`sudo journalctl -u snowluma -f`（`[Hook] login detected` 才是注入成功）。
- **`SQLITE_BUSY: database is locked` 刷屏**：第一次运行时 data 目录半建成，迁移脚本
  不幂等（`duplicate column name: media_data`）。清掉重来即可：
  `sudo systemctl stop snowluma && sudo rm -rf /opt/snowluma/data && sudo systemctl start snowluma`。
- **QQ 重启后**：不用管，`hookAutoLoad` 会重新注入（日志里 `PID=` 变了，`login detected` 再来一次）。
- **想换版本**：`SNOWLUMA_VERSION=2.4.2 bash install.sh`；lite 与 full 的区别只在媒体重量
  （full 带完整 ffmpeg），收信完全一样。
- **许可**：SnowLuma 源码可见、非商业；原生组件专有。别反汇编，按 EULA 用。
