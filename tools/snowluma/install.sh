#!/usr/bin/env bash
# 在 VM 里把 SnowLuma 装成服务 —— 收信的那一半。
#
# 为什么要在 VM 里装第二套：ArkLink 负责「发」（x11 点界面），但收信要靠 hook 注入
# QQ 进程，那条路在 Electron 40 上撞墙。SnowLuma 走的是同一门技术但注入在运行期，
# 装在同一个 VM 里就能把 100010001 的收包解出来，顺便把语音/引用也补齐。
#
# 用法（在 VM 里，arklink 用户，sudo 免密）：
#   bash install.sh                    # 默认 v2.4.2 + Node 24.22.1
#   SNOWLUMA_VERSION=2.4.2 bash install.sh
#
# 跑完它会打印「宿主侧要填的三行」（端口 + 两个 token）。宿主机那边还要把
# guest 3000/3001 转发到 host 3102/3101（VBox NAT），见 tools/snowluma/README.md。
set -euo pipefail

VERSION="${SNOWLUMA_VERSION:-2.4.2}"
NODE_VERSION="${NODE_VERSION:-24.22.1}"
DEST="${SNOWLUMA_DIR:-/opt/snowluma}"
NODE_DIR="${NODE_DIR:-/opt/node24}"
PKG="SnowLuma-v${VERSION}-linux-x64-lite.tar.gz"
HERE="$(cd "$(dirname "$0")" && pwd)"

say() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

[[ "$(uname -m)" == "x86_64" ]] || { echo "这个脚本只写了 x64 的包；arm64 请改 PKG 后缀为 linux-arm64-lite"; exit 1; }

# ptrace_scope=1 只允许父进程 attach 子进程，注入器要跨进程，必须放开
say "放开 ptrace_scope"
sudo sysctl -q -w kernel.yama.ptrace_scope=0
echo 'kernel.yama.ptrace_scope=0' | sudo tee /etc/sysctl.d/99-snowluma.conf >/dev/null

if [[ ! -x "$NODE_DIR/bin/node" ]]; then
  say "装 Node ${NODE_VERSION}（SnowLuma 要 ^24.22.1，Ubuntu 自带的 18 太老）"
  curl -fsSL -o /tmp/node24.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"
  sudo mkdir -p "$NODE_DIR"
  sudo tar xJf /tmp/node24.tar.xz -C "$NODE_DIR" --strip-components=1
fi
"$NODE_DIR/bin/node" -v

say "取 SnowLuma ${VERSION}（lite：不带 ffmpeg addon，收信/发文字/引用都够用）"
curl -fsSL -o "/tmp/${PKG}" "https://github.com/TomyJan/SnowLuma/releases/download/v${VERSION}/${PKG}"
sudo mkdir -p "$DEST"
sudo tar xzf "/tmp/${PKG}" -C "$DEST"
sudo chown -R arklink:arklink "$DEST" "$NODE_DIR"
ln -sf "$NODE_DIR/bin/node" "$DEST/node"

say "写启动包装与 systemd 单元"
sudo install -m 755 "${HERE}/run.sh" "$DEST/run.sh"
sudo install -m 644 "${HERE}/snowluma.service" /etc/systemd/system/snowluma.service

# hookAutoLoad：每次发现 QQ 进程就注入。默认是 false —— 少了这一行它只起个网页面板。
"$NODE_DIR/bin/node" -e '
const fs = require("fs");
const file = process.argv[1];
const base = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
base.hookAutoLoad = true;
fs.writeFileSync(file, JSON.stringify(base, null, 2));
console.log("hookAutoLoad = true");
' "$DEST/config/runtime.json"

sudo systemctl daemon-reload
sudo systemctl enable snowluma >/dev/null
sudo systemctl restart snowluma

say "等它发现账号并生成 onebot 配置（第一次会解密 login.db，通常 3~10 秒）"
CONF=""
for _ in $(seq 1 30); do
  CONF="$(ls "$DEST"/config/onebot_*.json 2>/dev/null | head -1 || true)"
  [[ -n "$CONF" ]] && break
  sleep 2
done
[[ -n "$CONF" ]] || { echo "没等到 onebot_*.json，看日志：sudo journalctl -u snowluma -n 50"; exit 1; }

say "把 OneBot 监听改成 0.0.0.0（VBox NAT 的转发到不了 127.0.0.1）"
"$NODE_DIR/bin/node" -e '
const fs = require("fs");
const file = process.argv[1];
const cfg = JSON.parse(fs.readFileSync(file, "utf8"));
for (const one of cfg.networks?.httpServers ?? []) one.host = "0.0.0.0";
for (const one of cfg.networks?.wsServers ?? []) one.host = "0.0.0.0";
fs.writeFileSync(file, JSON.stringify(cfg, null, 2));
const http = cfg.networks?.httpServers?.[0] ?? {};
const ws = cfg.networks?.wsServers?.[0] ?? {};
console.log(JSON.stringify({
  httpPort: http.port, httpToken: http.accessToken,
  wsPort: ws.port, wsToken: ws.accessToken,
}, null, 2));
' "$CONF"
sudo systemctl restart snowluma
sleep 8

say "结果"
systemctl is-active snowluma
ss -ltnp | grep -E ':3000|:3001' || true
"$NODE_DIR/bin/node" -e '
const fs = require("fs");
const file = process.argv[1];
const cfg = JSON.parse(fs.readFileSync(file, "utf8"));
const http = cfg.networks?.httpServers?.[0] ?? {};
const ws = cfg.networks?.wsServers?.[0] ?? {};
console.log(`
宿主侧要填的三行（cordis.patch.yml 的 snowluma 段）：
  httpUrl: http://127.0.0.1:3102
  wsUrl: ws://127.0.0.1:3101
  httpToken: ${http.accessToken}
  wsToken: ${ws.accessToken}

宿主机（Windows）上补 NAT 转发：
  VBoxManage controlvm arklink natpf1 "snowluma-http,tcp,127.0.0.1,3102,,${http.port ?? 3000}"
  VBoxManage controlvm arklink natpf1 "snowluma-ws,tcp,127.0.0.1,3101,,${ws.port ?? 3001}"
  VBoxManage controlvm arklink natpf1 "vnc,tcp,127.0.0.1,5903,,5900"

人工操作口（VNC 手动接管，watchdog 没收录的弹窗都靠它）：
  VM 里跑 tools/vm-manual/install.sh，装 x11vnc（密码在 /etc/x11vnc-pass.env）；
  宿主用任意 VNC 客户端连 127.0.0.1:5903，密码问装环境的人或在 ~/.arklink/env.json 里。
`);
' "$CONF"
