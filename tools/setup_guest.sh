#!/usr/bin/env bash
# ArkLink guest 自举脚本 —— 在**专门跑 QQ 的 Linux 虚拟机/容器**里执行。
#
# 目标：把「虚拟屏幕 + 窗口管理器 + 按键工具 + 中文字体 + QQ + Arklink」
#       一次装完，并注册成开机自启。宿主机对此一无所知。
#
# 用法（在 guest 内，root）：
#   curl -fsSL https://raw.githubusercontent.com/panxian-AI/ArkLink/main/tools/setup_guest.sh | bash
# 或：
#   bash tools/setup_guest.sh --qq-deb /tmp/QQ_3.2.32_260812_amd64_01.deb
#
# 支持：Ubuntu 22.04 / 24.04、Debian 12。其它发行版会提示手动步骤后退出。
set -euo pipefail

QQ_DEB=""
DISPLAY_NUM=99
GEOMETRY="1280x800x24"
ARKLINK_SRC=""

while [ $# -gt 0 ]; do
  case "$1" in
    --qq-deb)   QQ_DEB="$2"; shift 2 ;;
    --display)  DISPLAY_NUM="$2"; shift 2 ;;
    --geometry) GEOMETRY="$2"; shift 2 ;;
    --source)   ARKLINK_SRC="$2"; shift 2 ;;
    -h|--help)  sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[36m[arklink]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[arklink]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[arklink]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo bash $0）"

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
log "安装基础依赖（虚拟屏幕 / 窗口管理器 / 按键注入 / 中文字体）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  xvfb openbox xdotool x11-utils \
  fonts-wqy-microhei fonts-wqy-zenhei fontconfig \
  libgbm1 "${AUDIO_PKG}" libnss3 libxss1 libxtst6 libatk-bridge2.0-0 libatk1.0-0 \
  libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
  python3 python3-pip python3-venv ca-certificates wget procps \
  >/dev/null

# 中文字体没装成功的话，QQ 里中文全是方块，而且布局会变、坐标全错
fc-list :lang=zh | grep -q . || die "中文字体缺失，坐标校准会失效：请检查 fonts-wqy-* 是否装上"

# ---------------------------------------------------------------- 2. QQ
if [ -z "$QQ_DEB" ]; then
  for cand in /tmp/QQ_*.deb /root/QQ_*.deb /opt/QQ_*.deb; do
    [ -f "$cand" ] && QQ_DEB="$cand" && break
  done
fi
if [ -n "$QQ_DEB" ] && [ -f "$QQ_DEB" ]; then
  log "安装 QQ：$QQ_DEB"
  # 关键：用 apt 解依赖，不要 dpkg -i（t64 改名时 dpkg 会直接失败）
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

# ---------------------------------------------------------------- 4. 启动编排
log "写启动脚本 /opt/arklink/start.sh"
mkdir -p /opt/arklink
cat > /opt/arklink/start.sh <<EOF
#!/usr/bin/env bash
# 虚拟屏幕 + 窗口管理器 + QQ + Arklink WebUI
set -u
export DISPLAY=:${DISPLAY_NUM}
export LANG=C.UTF-8

# 已经跑着就别起第二份
pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null || {
  Xvfb :${DISPLAY_NUM} -screen 0 ${GEOMETRY} -nolisten tcp &
  sleep 1
}
# openbox 不能省：没有 WM，windowactivate 不会生效，窗口也没有正常尺寸
pgrep -x openbox >/dev/null || { openbox >/dev/null 2>&1 & sleep 1; }

# QQ（容器里 Chromium 沙箱起不来，必须 --no-sandbox）
if ! pgrep -f "/opt/QQ/qq|/opt/qq/qq" >/dev/null; then
  QQ_BIN=\$(command -v qq || echo /opt/QQ/qq)
  [ -x "\$QQ_BIN" ] && { "\$QQ_BIN" --no-sandbox >/tmp/qq.log 2>&1 & sleep 8; }
fi

# Arklink 控制面：只监听本机，外面通过端口映射/隧道访问
exec /opt/arklink-venv/bin/python -m arklink serve webui \\
  --host 0.0.0.0 --port 6800 --engine x11
EOF
chmod +x /opt/arklink/start.sh

log "注册 systemd 服务 arklink.service"
cat > /etc/systemd/system/arklink.service <<'EOF'
[Unit]
Description=ArkLink (Xvfb + QQ + WebUI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/arklink/start.sh
Restart=always
RestartSec=5
# QQ 需要的临时目录与共享内存
PrivateTmp=no
Environment=LANG=C.UTF-8

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable --now arklink.service >/dev/null 2>&1 || warn "systemd 不可用，请手动执行 /opt/arklink/start.sh"

# ---------------------------------------------------------------- 5. 自检
sleep 2
echo
log "自检"
DISPLAY=":${DISPLAY_NUM}" xdotool search --name QQ >/dev/null 2>&1 \
  && log "  ✅ 找到 QQ 窗口" \
  || warn "  ⚠️ 还没找到 QQ 窗口——首次需要在 VNC 里扫码登录"
pgrep -f "arklink serve webui" >/dev/null && log "  ✅ WebUI 进程在跑" || warn "  ⚠️ WebUI 未启动"
echo
cat <<MSG
下一步：
  1) 首次登录（只此一次）：
       apt-get install -y x11vnc && x11vnc -display :${DISPLAY_NUM} -nopw -forever &
       用 VNC 客户端连 <guest-ip>:5900 扫码
  2) 发送自检（在 guest 内）：
       DISPLAY=:${DISPLAY_NUM} /opt/arklink-venv/bin/python -m arklink send \\
         --to 示例好友 --text "自检" --engine x11 --yes
  3) 宿主机浏览器打开 http://<guest-ip>:6800 管理
  4) 扫完码备份登录态：tar czf /root/qq-login.tgz ~/.config/QQ
MSG
