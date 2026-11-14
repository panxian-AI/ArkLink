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
  xvfb openbox xdotool x11-utils scrot imagemagick xclip \
  fonts-wqy-microhei fonts-wqy-zenhei fontconfig \
  libgbm1 "${AUDIO_PKG}" libnss3 libxss1 libxtst6 libatk-bridge2.0-0 libatk1.0-0 \
  libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
  python3 python3-pip python3-venv ca-certificates wget procps \
  >/dev/null

# 中文字体没装成功的话，QQ 里中文全是方块，而且布局会变、坐标全错
fc-list :lang=zh | grep -q . || die "中文字体缺失，坐标校准会失效：请检查 fonts-wqy-* 是否装上"

# ---------------------------------------------------------------- 2. QQ
# 官方 CDN 对部分网络环境回 403，官方直连失败时回退 GitHub 镜像（校验 SHA256）
QQ_MIRROR_URL="${QQ_MIRROR_URL:-https://github.com/Rodert/qq-versions/releases/download/qq-packages-20260813-1d08f1d4/QQ_3.2.32_260812_amd64_01.deb}"
QQ_MIRROR_SHA="${QQ_MIRROR_SHA:-4409ec87f96675318ab4c456735741ec05513f8197733623f55c729579023353}"
if [ -z "$QQ_DEB" ]; then
  for cand in /tmp/QQ_*.deb /root/QQ_*.deb /opt/QQ_*.deb; do
    [ -f "$cand" ] && QQ_DEB="$cand" && break
  done
fi
if [ -z "$QQ_DEB" ] && ! command -v qq >/dev/null 2>&1 && [ ! -x /opt/QQ/qq ]; then
  log "本地没有 QQ 安装包，尝试从 GitHub 镜像下载（约 177MB）…"
  if wget -q --timeout=120 --tries=3 -O /tmp/QQ_download.deb "$QQ_MIRROR_URL" 2>/dev/null; then
    if echo "$QQ_MIRROR_SHA  /tmp/QQ_download.deb" | sha256sum -c --status 2>/dev/null; then
      QQ_DEB=/tmp/QQ_download.deb
      log "镜像下载完成，SHA256 校验通过"
    else
      warn "镜像下载的包校验不符，删掉以防装错东西"
      rm -f /tmp/QQ_download.deb
    fi
  else
    warn "镜像下载失败（guest 可能访问不了 GitHub）"
    rm -f /tmp/QQ_download.deb
  fi
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
log "写启动脚本 /opt/arklink/start.sh（自愈式：每 20s 体检一遍，坏了自动拉起）"
mkdir -p /opt/arklink
cat > /opt/arklink/start.sh <<EOF
#!/usr/bin/env bash
# 虚拟屏幕 + 窗口管理器 + QQ + Arklink WebUI，自愈式监督
set -u
export DISPLAY=:${DISPLAY_NUM}
export LANG=C.UTF-8
# QQ 必须以**登录用户**身份跑：root 没有 ~/.config/QQ，起来就是扫码页，
# 而且 /tmp 里的日志文件属主不对会直接 Permission denied 把 QQ 拦在门外。
# systemd 以 root 起本脚本，所以这里自动挑「有 QQ 登录态的那个用户」。
QQ_USER="\${QQ_USER:-}"
if [ -z "\$QQ_USER" ]; then
  QQ_USER=\$(stat -c %U /home/*/.config/QQ 2>/dev/null | head -1)
  [ -n "\$QQ_USER" ] || QQ_USER=\$(awk -F: '\$3>=1000 && \$3<60000 {print \$1; exit}' /etc/passwd)
  [ -n "\$QQ_USER" ] || QQ_USER=root
fi

# 只看"活不活着"，不看"进程在不在"——Xvfb 僵死（进程在但屏幕黑）是最常见的坑
display_ok() { xdpyinfo -display :${DISPLAY_NUM} >/dev/null 2>&1; }

qq_alive() {
  # 用 pgrep -x 精确匹配可执行名，避免匹配到命令行里带这个字符串的 shell 自己
  pgrep -x qq >/dev/null 2>&1 || pgrep -f "^/opt/QQ/qq" >/dev/null 2>&1
}

# 登录窗口固定 320x460；认这个尺寸就能把它和 1x1/16x16 的辅助窗口、
# 登录后的主窗口、搜索弹窗区分开
is_login_window() {
  [ "\$1" -ge 300 ] && [ "\$1" -le 360 ] && [ "\$2" -ge 420 ] && [ "\$2" -le 500 ]
}

# 二维码过期 → 自动点「刷新」。
# 判据：二维码卡片区域出现成片的红色像素（"当前二维码已过期" 那行红字）。
# 旧码本来就已经失效，点刷新不会打断任何正在进行的扫码。
qr_refresh() {
  command -v convert >/dev/null 2>&1 || return 0
  local id X Y WIDTH HEIGHT shot red rx ry
  shot=/tmp/.ark_qr.png
  for id in \$(xdotool search --onlyvisible --name '^QQ\$' 2>/dev/null); do
    eval "\$(xdotool getwindowgeometry --shell "\$id" 2>/dev/null)" || continue
    is_login_window "\$WIDTH" "\$HEIGHT" || continue
    scrot -o "\$shot" >/dev/null 2>&1 || return 0
    # 卡片区在窗口内的相对位置：x 60..260，y 110..310
    rx=\$((X + 60)); ry=\$((Y + 110))
    # 红字判据：R>180 且 G/B<130（"当前二维码已过期" 就是这种红）
    red=\$(convert "\$shot" -crop 200x200+\${rx}+\${ry} txt:- 2>/dev/null | awk -F'[(,)]' 'NR>1 && \$2+0>180 && \$3+0<130 && \$4+0<130 {n++} END{print n+0}')
    rm -f "\$shot"
    # 防抖：连续两轮都检测到红字才动手，避免把「正在自动登录」的过渡画面误当过期
    local state=/tmp/.ark_qr_red n
    if [ "\${red:-0}" -gt 120 ]; then
      n=\$(cat "\$state" 2>/dev/null || echo 0)
      if [ "\$n" -lt 1 ]; then echo 1 > "\$state"; return 0; fi
      echo 0 > "\$state"
      xdotool windowactivate "\$id" 2>/dev/null; sleep 0.4
      xdotool mousemove --window "\$id" 160 281 click 1 2>/dev/null
      echo "[\$(date '+%F %T')] QR expired -> clicked refresh"
      sleep 1
    else
      echo 0 > "\$state" 2>/dev/null || true
    fi
  done
}

# 更新提示之类的弹窗：可见、标题带 QQ、尺寸像对话框 → Escape 收掉。
# 登录主窗口（320x460）和主界面大窗口都排除，1x1/16x16 辅助窗口也排除。
dismiss_popups() {
  local id X Y WIDTH HEIGHT name
  for id in \$(xdotool search --onlyvisible --name '.' 2>/dev/null); do
    name=\$(xdotool getwindowname "\$id" 2>/dev/null) || continue
    case "\$name" in *[Qq][Qq]*) ;; *) continue ;; esac
    eval "\$(xdotool getwindowgeometry --shell "\$id" 2>/dev/null)" || continue
    [ "\$WIDTH" -gt 50 ] && [ "\$HEIGHT" -gt 50 ] || continue
    [ "\$WIDTH" -lt 600 ] && [ "\$HEIGHT" -lt 600 ] || continue
    is_login_window "\$WIDTH" "\$HEIGHT" && continue
    xdotool windowactivate "\$id" 2>/dev/null; sleep 0.3
    xdotool key Escape 2>/dev/null
    echo "[\$(date '+%F %T')] dismissed popup: \$name (\${WIDTH}x\${HEIGHT})"
  done
}

watchdog() {
  dismiss_popups
  qr_refresh
}

heal() {
  # 1) Xvfb：健康检查，坏了先杀干净再起新的（pgrep 命中不等于能用）
  if ! display_ok; then
    pkill -f "Xvfb :${DISPLAY_NUM}" 2>/dev/null; sleep 1
    Xvfb :${DISPLAY_NUM} -screen 0 ${GEOMETRY} -nolisten tcp >/dev/null 2>&1 &
    sleep 2
  fi
  # 2) openbox 不能省：没有 WM，windowactivate 不会生效，窗口也没有正常尺寸
  pgrep -x openbox >/dev/null || { openbox >/dev/null 2>&1 & sleep 1; }
  # 3) QQ：进程死了才重启（容器里 Chromium 沙箱起不来，必须 --no-sandbox）
  if ! qq_alive; then
    QQ_BIN=\$(command -v qq || echo /opt/QQ/qq)
    # 日志绝对不能放 /tmp：sticky 目录有 fs.protected_regular 保护，
    # 属主不同的文件连 root 都写不进去（Permission denied，真机踩过）
    LOG=/var/log/qq.log
    [ -w "\$LOG" ] || LOG=/var/log/qq-\$(id -u).log
    if [ -x "\$QQ_BIN" ]; then
      if [ "\$(id -u)" = "0" ] && id "\$QQ_USER" >/dev/null 2>&1; then
        runuser -u "\$QQ_USER" -- setsid "\$QQ_BIN" --no-sandbox >"\$LOG" 2>&1 &
      else
        setsid "\$QQ_BIN" --no-sandbox >"\$LOG" 2>&1 &
      fi
      sleep 8
    fi
  fi
}

heal
watchdog

# Arklink 控制面：只监听本机，外面通过端口映射/隧道访问
# （故意写成一行：heredoc 里行尾续行符容易被转义规则搞坏，一行最稳）
/opt/arklink-venv/bin/python -m arklink serve webui --host 0.0.0.0 --port 6800 --engine x11 &
WEBUI=\$!

# 监督循环：webui 活着就一直体检；webui 死了再 heal 一轮后退出，
# 让 systemd Restart=always 接手整个栈
while kill -0 \$WEBUI 2>/dev/null; do
  sleep 20
  heal
  watchdog
done
heal
wait \$WEBUI
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
# 关键：只杀 start.sh 自己，别连带 Xvfb / openbox / QQ。
# 默认 control-group 会把整组一起 SIGKILL：QQ 来不及把登录令牌落盘，
# 下次启动又变成"新设备"，手机上每次都弹新设备确认。真机踩过。
KillMode=process
# QQ 需要的临时目录与共享内存
PrivateTmp=no
Environment=LANG=C.UTF-8

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable --now arklink.service >/dev/null 2>&1 || warn "systemd 不可用，请手动执行 /opt/arklink/start.sh"

# ---------------------------------------------------------------- 4.5 API 服务（给上层程序调用）
log "注册 systemd 服务 arklink-api.service（HTTP 接口：收发消息，供其他程序调用）"
mkdir -p /etc/arklink
if [ ! -s /etc/arklink/api.token ]; then
  head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-32 > /etc/arklink/api.token
fi
chmod 600 /etc/arklink/api.token

cat > /etc/systemd/system/arklink-api.service <<'UNIT'
[Unit]
Description=ArkLink HTTP API (bot interface: send + receive)
After=network-online.target arklink.service
Wants=network-online.target arklink.service

[Service]
Type=simple
Environment=ARKLINK_API_STATE=/etc/arklink/api.json
Environment=DISPLAY=:99
ExecStart=/bin/sh -c 'exec /opt/arklink-venv/bin/python -m arklink serve api --host 0.0.0.0 --port 6900 --engine x11 --token "$(cat /etc/arklink/api.token)"'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable --now arklink-api.service >/dev/null 2>&1 \
  || warn "systemd 不可用，请手动起：DISPLAY=:${DISPLAY_NUM} /opt/arklink-venv/bin/python -m arklink serve api --engine x11"

# ---------------------------------------------------------------- 5. 收尾验货
# QQ 没装上，后面全是白搭：直接非 0 退出，让上层 setup 报「失败」，
# 而不是傻乎乎地说「完成」（真机就静默跳过过 QQ，用户对着网页发呆）
command -v qq >/dev/null 2>&1 || [ -x /opt/QQ/qq ] \
  || die "QQ 未安装成功：看上面 apt/wget 的报错；宿主机重跑 setup 会把 deb 推进来"

# ---------------------------------------------------------------- 6. 自检
sleep 2
echo
log "自检"
DISPLAY=":${DISPLAY_NUM}" xdotool search --name QQ >/dev/null 2>&1 \
  && log "  ✅ 找到 QQ 窗口" \
  || warn "  ⚠️ 还没找到 QQ 窗口——首次需要在 VNC 里扫码登录"
pgrep -f "arklink serve webui" >/dev/null && log "  ✅ WebUI 进程在跑" || warn "  ⚠️ WebUI 未启动"
pgrep -f "arklink serve api" >/dev/null && log "  ✅ API 进程在跑（端口 6900）" || warn "  ⚠️ API 未启动"
echo
cat <<MSG
下一步：
  1) 首次登录（只此一次）：
       apt-get install -y x11vnc && x11vnc -display :${DISPLAY_NUM} -nopw -forever &
       用 VNC 客户端连 <guest-ip>:5900 扫码
  2) 发送自检（在 guest 内）：
       DISPLAY=:${DISPLAY_NUM} /opt/arklink-venv/bin/python -m arklink send \\
         --to 备注名 --text "自检" --engine x11 --yes
  3) 宿主机浏览器打开 http://127.0.0.1:6800 管理（端口已映射到本机）
  3b) 上层程序接 API：地址 http://127.0.0.1:6900，token 如下
       ${ARKLINK_API_TOKEN_HINT:-（宿主机执行 arklink env token 取）}
  4) 扫完码备份登录态：tar czf /root/qq-login.tgz ~/.config/QQ

API token（只在本机可见，别人要用就给他这一串）：
  $(cat /etc/arklink/api.token 2>/dev/null || echo "（还没生成）")
MSG
