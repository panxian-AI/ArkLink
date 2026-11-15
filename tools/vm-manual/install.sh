#!/usr/bin/env bash
# 在 ArkLink 虚拟机里装「人工操作口」：x11vnc 绑在 QQ 所在的 Xvfb 显示器（:99）上。
# 有了它，宿主用 VNC 客户端就能亲手点 QQ —— 扫码、验证、换号、关弹窗，
# watchdog 没收录的任何界面都能人工兜底。
#
# 用法（VM 里）：
#   bash install.sh                # 自动生成随机密码
#   bash install.sh <密码>         # 指定密码（4~8 位，VNC 协议上限 8 位）
#
# 装完打印：宿主侧 NAT 命令 + 连接方式。
#
# 坑（2026-11-15 实测）：`x11vnc -storepasswd` 写出的密码文件，配 -rfbauth 在这台
# 环境上就是认不了（密码对也报 rejected）。所以这里不用 -rfbauth，改把密码放进
# root-only 的 /etc/x11vnc-pass.env，systemd 用 -passwd "$X11VNC_PASS" 注入。
set -euo pipefail

PASS="${1:-}"
if [ -z "$PASS" ]; then
  PASS="$(openssl rand -hex 4)"
fi
# VNC 标准认证只认前 8 个字符
PASS="${PASS:0:8}"

echo "[1/4] 安装 x11vnc…"
if ! command -v x11vnc >/dev/null 2>&1; then
  sudo apt-get install -y x11vnc
fi

echo "[2/4] 写密码环境文件 /etc/x11vnc-pass.env（仅 root 可读）…"
printf 'X11VNC_PASS=%s\n' "$PASS" | sudo tee /etc/x11vnc-pass.env >/dev/null
sudo chmod 600 /etc/x11vnc-pass.env

echo "[3/4] 装 systemd 服务（开机自启，掉了自动拉起）…"
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/x11vnc.service" ]; then
  sudo install -m 644 "$HERE/x11vnc.service" /etc/systemd/system/x11vnc.service
else
  cat <<'UNIT' | sudo tee /etc/systemd/system/x11vnc.service >/dev/null
[Unit]
Description=x11vnc manual takeover for QQ (display :99)
After=multi-user.target

[Service]
EnvironmentFile=/etc/x11vnc-pass.env
ExecStart=/bin/sh -c 'exec /usr/bin/x11vnc -display :99 -passwd $X11VNC_PASS -rfbport 5900 -forever -shared -noxdamage -repeat -quiet'
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT
fi
sudo systemctl daemon-reload
sudo systemctl enable --now x11vnc

echo "[4/4] 等它起来…"
sleep 2
systemctl is-active x11vnc

cat <<TIP

装好了。宿主机（Windows）上补 NAT 转发（一条）：
  VBoxManage controlvm arklink natpf1 "vnc,tcp,127.0.0.1,5903,,5900"

然后任意 VNC 客户端（TightVNC Viewer / RealVNC / Windows 自带 mstsc 不行）连：
  地址：127.0.0.1:5903
  密码：$PASS
  （密码也存在 VM 的 /etc/x11vnc-pass.env 里，root 可读；要换密码就改这个文件后
   sudo systemctl restart x11vnc）

注意：
  - 只看到桌面没有 QQ？QQ 由 watchdog/arklink-qq.service 管理，正常情况就在 :99 上；
    真不在就 sudo systemctl restart arklink-qq（名字以 systemctl list-units | grep qq 为准）。
  - VNC 里别乱点「退出登录」——那会让收发全断，得重新扫码。
TIP
