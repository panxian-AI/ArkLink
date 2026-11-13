#!/usr/bin/env bash
# ArkLink 服务器直装脚本 —— 部署在小服务器上，不需要 VirtualBox / 虚拟机。
#
# 与 tools/setup_guest.sh 的区别：
#   * 那个脚本面向「专门跑 QQ 的虚拟机/容器」（guest），宿主机要做端口映射；
#   * 这个脚本面向**裸服务器**：ArkLink 直接跑在服务器本机上，装完即是
#     「长期前台运行」——三重保障：
#       1) systemd：三个服务开机自启、崩溃自动重启；
#       2) QQ 看护（watchdog）：QQ 进程/窗口没了自动拉起，最小间隔 60 秒防抖；
#       3) 登录态持久化：每天自动备份 ~/.config/QQ，重启后自动还原，免重新扫码。
#
# 说明：QQ Linux 是图形程序，所以仍然需要一个**无头虚拟屏**（Xvfb，几 MB 内存），
#       这不是装桌面环境，更不是虚拟机——服务器上不需要 VirtualBox。
#
# 用法（在服务器上，root）：
#   curl -fsSL https://raw.githubusercontent.com/panxian-AI/ArkLink/feat/server-deploy/tools/setup_server.sh | bash
# 或：
#   bash tools/setup_server.sh --qq-deb /tmp/QQ_3.2.32_260812_amd64_01.deb
#   bash tools/setup_server.sh --bind-local        # WebUI/API 只绑 127.0.0.1（用 SSH 隧道访问）
#
# 支持：Ubuntu 22.04 / 24.04、Debian 12。其它发行版会提示后退出。
set -euo pipefail

QQ_DEB=""
DISPLAY_NUM=99
GEOMETRY="1280x800x24"
ARKLINK_SRC=""
BIND_HOST="0.0.0.0"     # 默认对外提供 WebUI（有密码/token 保护）；--bind-local 改回只听本机

while [ $# -gt 0 ]; do
  case "$1" in
    --qq-deb)    QQ_DEB="$2"; shift 2 ;;
    --display)   DISPLAY_NUM="$2"; shift 2 ;;
    --geometry)  GEOMETRY="$2"; shift 2 ;;
    --source)    ARKLINK_SRC="$2"; shift 2 ;;
    --bind-local) BIND_HOST="127.0.0.1"; shift ;;
    -h|--help)   sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[36m[arklink]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[arklink]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[arklink]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo bash $0）"
command -v systemctl >/dev/null 2>&1 || die "没找到 systemctl：本脚本面向 systemd 服务器"

# ---------------------------------------------------------------- 识别发行版
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:22.04) AUDIO_PKG=libasound2 ;;
  ubuntu:24.04) AUDIO_PKG=libasound2t64 ;;   # 24.04 起 t64 改名，写错必报依赖错
  debian:12)    AUDIO_PKG=libasound2 ;;
  *) die "未适配的发行版 ${ID:-?} ${VERSION_ID:-?}：请用 Ubuntu 22.04/24.04 或 Debian 12" ;;
esac
log "发行版：${PRETTY_NAME}（音频包 ${AUDIO_PKG}）"

# ---------------------------------------------------------------- 1. 基础依赖
log "安装基础依赖（无头虚拟屏 / 窗口管理器 / 按键注入 / 中文字体）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  xvfb openbox xdotool x11-utils \
  fonts-wqy-microhei fonts-wqy-zenhei fontconfig \
  libgbm1 "${AUDIO_PKG}" libnss3 libxss1 libxtst6 libatk-bridge2.0-0 libatk1.0-0 \
  libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
  python3 python3-pip python3-venv ca-certificates wget procps \
  >/dev/null

fc-list :lang=zh | grep -q . || die "中文字体缺失，QQ 里中文全是方块、坐标会错：检查 fonts-wqy-*"

# ---------------------------------------------------------------- 2. QQ
if [ -z "$QQ_DEB" ]; then
  for cand in /tmp/QQ_*.deb /root/QQ_*.deb /opt/QQ_*.deb; do
    [ -f "$cand" ] && QQ_DEB="$cand" && break
  done
fi
if [ -n "$QQ_DEB" ] && [ -f "$QQ_DEB" ]; then
  log "安装 QQ：$QQ_DEB"
  apt-get install -y "$QQ_DEB" >/dev/null || die "QQ 安装失败，检查包与依赖"
elif command -v qq >/dev/null 2>&1 || [ -x /opt/QQ/qq ]; then
  log "已安装 QQ，跳过"
else
  warn "未找到 QQ 安装包。请先从 https://im.qq.com/linuxqq/index.shtml 下载 deb，"
  warn "然后重跑：bash $0 --qq-deb /tmp/QQ_xxx_amd64.deb"
fi

# ---------------------------------------------------------------- 3. Arklink
if [ -n "$ARKLINK_SRC" ]; then
  log "安装 Arklink（源码 $ARKLINK_SRC）"
  python3 -m venv /opt/arklink-venv
  /opt/arklink-venv/bin/pip install -q --upgrade pip
  /opt/arklink-venv/bin/pip install -q -e "$ARKLINK_SRC"
elif /opt/arklink-venv/bin/python -c "import arklink" 2>/dev/null; then
  log "Arklink 已安装，跳过"
else
  warn "未指定 Arklink 源码：加 --source /path/to/ArkLink 再跑一次"
fi

# ---------------------------------------------------------------- 4. 密钥
log "生成 WebUI 口令与 API token（落盘 /etc/arklink/，权限 600）"
mkdir -p /etc/arklink /var/lib/arklink
if [ ! -s /etc/arklink/webui.token ]; then
  head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-16 > /etc/arklink/webui.token
fi
if [ ! -s /etc/arklink/api.token ]; then
  head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-32 > /etc/arklink/api.token
fi
chmod 600 /etc/arklink/webui.token /etc/arklink/api.token

# ---------------------------------------------------------------- 5. QQ 看护脚本
log "写 QQ 看护脚本 /opt/arklink/qq-watchdog.sh"
cat > /opt/arklink/qq-watchdog.sh <<EOF
#!/usr/bin/env bash
# 长期前台运行的核心：保证 Xvfb / openbox / QQ 一直在。
# QQ 崩了自动拉起（最小间隔 60 秒防抖）；登录态每天自动备份一次。
set -u
DISPLAY_NUM="\${ARKLINK_DISPLAY:-${DISPLAY_NUM}}"
GEOMETRY="\${ARKLINK_GEOMETRY:-${GEOMETRY}}"
HOME_ROOT=/root
LOGIN_BK=/var/lib/arklink/qq-login.tgz
LAST_QQ_START=0
LAST_BK=0

log() { printf '\033[36m[watchdog]\033[0m %s\n' "\$*"; }

while true; do
  export DISPLAY=":\${DISPLAY_NUM}"
  export LANG=C.UTF-8

  # 无头虚拟屏（没有它 QQ 没地方显示）
  pgrep -f "Xvfb :\${DISPLAY_NUM}" >/dev/null || {
    Xvfb ":\${DISPLAY_NUM}" -screen 0 "\${GEOMETRY}" -nolisten tcp &
    sleep 1
  }
  # 窗口管理器（没有它 windowactivate 不生效）
  pgrep -x openbox >/dev/null || { openbox >/dev/null 2>&1 & sleep 1; }

  # QQ 本体
  if ! pgrep -f "/opt/QQ/qq|/opt/qq/qq" >/dev/null; then
    NOW=\$(date +%s)
    if [ \$((NOW - LAST_QQ_START)) -ge 60 ]; then
      LAST_QQ_START=\$NOW
      # 重启后自动还原登录态，免重新扫码
      if [ -s "\$LOGIN_BK" ] && [ ! -d "\$HOME_ROOT/.config/QQ" ]; then
        tar xzf "\$LOGIN_BK" -C "\$HOME_ROOT" && log "已还原登录态"
      fi
      QQ_BIN=\$(command -v qq || echo /opt/QQ/qq)
      if [ -x "\$QQ_BIN" ]; then
        "\$QQ_BIN" --no-sandbox >>/tmp/qq.log 2>&1 &
        log "已拉起 QQ（pid \$!）"
        sleep 8
      fi
    fi
  fi

  # 登录态每日备份：QQ 窗口在 + 距上次备份超过 24h
  if xdotool search --name QQ >/dev/null 2>&1; then
    NOW=\$(date +%s)
    BK_AGE=\$NOW
    [ -f "\$LOGIN_BK" ] && BK_AGE=\$(stat -c %Y "\$LOGIN_BK" 2>/dev/null || echo 0)
    if [ \$((NOW - BK_AGE)) -gt 86400 ]; then
      tar czf "\$LOGIN_BK.tmp" -C "\$HOME_ROOT" .config/QQ 2>/dev/null \\
        && mv "\$LOGIN_BK.tmp" "\$LOGIN_BK" && log "登录态已备份"
    fi
  fi

  sleep 15
done
EOF
chmod +x /opt/arklink/qq-watchdog.sh

# ---------------------------------------------------------------- 6. systemd 服务
log "注册 systemd 服务：arklink-qq（看护）/ arklink（WebUI）/ arklink-api（接口）"

cat > /etc/systemd/system/arklink-qq.service <<EOF
[Unit]
Description=ArkLink QQ watchdog (Xvfb + openbox + QQ, auto-restart)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/arklink/qq-watchdog.sh
Restart=always
RestartSec=5
Environment=LANG=C.UTF-8
Environment=ARKLINK_DISPLAY=${DISPLAY_NUM}
Environment=ARKLINK_GEOMETRY=${GEOMETRY}

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/arklink.service <<EOF
[Unit]
Description=ArkLink WebUI (browser console)
After=network-online.target arklink-qq.service
Wants=network-online.target arklink-qq.service

[Service]
Type=simple
Environment=DISPLAY=:${DISPLAY_NUM}
ExecStart=/bin/sh -c 'exec /opt/arklink-venv/bin/python -m arklink serve webui \\
  --host ${BIND_HOST} --port 6800 --engine x11 \\
  --password "$(cat /etc/arklink/webui.token)"'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/arklink-api.service <<EOF
[Unit]
Description=ArkLink HTTP API (bot interface: send + receive)
After=network-online.target arklink-qq.service
Wants=network-online.target arklink-qq.service

[Service]
Type=simple
Environment=ARKLINK_API_STATE=/etc/arklink/api.json
Environment=DISPLAY=:${DISPLAY_NUM}
ExecStart=/bin/sh -c 'exec /opt/arklink-venv/bin/python -m arklink serve api \\
  --host ${BIND_HOST} --port 6900 --engine x11 --token "$(cat /etc/arklink/api.token)"'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable --now arklink-qq.service arklink.service arklink-api.service >/dev/null 2>&1 \
  || warn "systemd 启动失败，手动执行：systemctl enable --now arklink-qq arklink arklink-api"

# ---------------------------------------------------------------- 7. 自检与收尾
sleep 3
echo
log "自检"
DISPLAY=":${DISPLAY_NUM}" xdotool search --name QQ >/dev/null 2>&1 \
  && log "  ✅ QQ 窗口在（无头虚拟屏上长期前台运行）" \
  || warn "  ⚠️ 还没找到 QQ 窗口——首次需要在 VNC 里扫码登录"
pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null && log "  ✅ 无头虚拟屏在跑（不是虚拟机，无需 VirtualBox）" || warn "  ⚠️ Xvfb 未启动"
systemctl is-active --quiet arklink.service && log "  ✅ WebUI 服务在跑" || warn "  ⚠️ WebUI 未启动"
systemctl is-active --quiet arklink-api.service && log "  ✅ API 服务在跑（6900）" || warn "  ⚠️ API 未启动"
echo
WEBUI_TOKEN=$(cat /etc/arklink/webui.token)
API_TOKEN=$(cat /etc/arklink/api.token)
cat <<MSG
============================================================  部署完成
下一步：

  1) 首次登录（只此一次，扫完码以后重启都不用再扫）：
       apt-get install -y x11vnc
       x11vnc -display :${DISPLAY_NUM} -rfbport 5901 -passwd "${WEBUI_TOKEN}" -forever &
       # 只在本机听 5901，用 SSH 隧道连，别直接暴露 5901：
       #   在你自己的电脑上执行：ssh -L 5901:127.0.0.1:5901 root@<服务器IP>
       #   然后 VNC 客户端连 127.0.0.1:5901 扫码
  2) 浏览器前台操作：http://<服务器IP>:6800
       登录口令（/etc/arklink/webui.token）：${WEBUI_TOKEN}
  3) 上层程序接 API：http://<服务器IP>:6900
       token（/etc/arklink/api.token）：${API_TOKEN}
  4) 安全清单（对外服务器必做）：
       * 云控制台安全组只放行 6800 / 6900（VNC 只走 SSH 隧道）；
       * 或干脆不开端口，全部走隧道：ssh -L 6800:127.0.0.1:6800 -L 6900:127.0.0.1:6900 root@<服务器IP>
         （配合 --bind-local 重装，服务只听 127.0.0.1）
       * 两个 token 不要外发；泄露就重跑本脚本（会重新生成）或手动改 /etc/arklink/*.token 后
         systemctl restart arklink arklink-api

常用命令：
  systemctl status arklink-qq arklink arklink-api    # 三个服务的状态
  journalctl -u arklink-qq -f                        # 看护日志（拉起/备份记录）
  tail -f /tmp/qq.log                                # QQ 自己的日志
  tar czf qq-login-手动备份.tgz -C /root .config/QQ   # 迁移服务器：把备份拷到新机器重装即可
============================================================
MSG
