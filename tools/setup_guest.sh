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
# 判据：二维码卡片区域出现「当前二维码已过期」的红字。
#   convert txt:- 每行形如 "122,234: (247,81,54)  #F75136  srgb(247,81,54)"，
#   按 [(,)] 切字段：第 1 列是 x、第 2 列是 y、之后依次 R/G/B（**第 2 列是 y
#   不是 R**，字段序写错就是一句不痛不痒的 0，检测形同虚设——真机踩过）。
#   还必须看红色的**横向跨度**：二维码中间的企鹅 logo 有红围巾
#   （实测 167px、宽 28px），只数总量会把有效码误当过期、扫码扫一半被刷掉；
#   过期红字是一条横带（实测 313px、宽 108px）——宽 >60px 才算过期。
# 旧码本来就已经失效，点刷新不会打断任何正在进行的扫码。
qr_refresh() {
  command -v convert >/dev/null 2>&1 || return 0
  local id X Y WIDTH HEIGHT shot red red_n red_w rx ry
  shot=/tmp/.ark_qr.png
  for id in \$(xdotool search --onlyvisible --name '^QQ\$' 2>/dev/null); do
    eval "\$(xdotool getwindowgeometry --shell "\$id" 2>/dev/null)" || continue
    is_login_window "\$WIDTH" "\$HEIGHT" || continue
    scrot -o "\$shot" >/dev/null 2>&1 || return 0
    # 卡片区在窗口内的相对位置：x 60..260，y 110..310
    rx=\$((X + 60)); ry=\$((Y + 110))
    red=\$(convert "\$shot" -crop 200x200+\${rx}+\${ry} txt:- 2>/dev/null | awk -F'[(,)]' 'NR>1 && \$3+0>180 && \$4+0<130 && \$5+0<130 {n++; x=\$1+0; if(min==""||x<min)min=x; if(max==""||x>max)max=x} END{if(n>0) print n, max-min; else print "0 0"}')
    red_n=\${red%% *}; red_w=\${red##* }
    # 心跳日志：每轮的检测值都落盘，出问题一眼看出是「没检测到」还是「没点上」
    echo "\$(date +%T) red=\$red" >> /tmp/wd.log
    rm -f "\$shot"
    # 防抖：连续两轮都检测到红字才动手，避免把「正在自动登录」的过渡画面误当过期
    local state=/tmp/.ark_qr_red n
    if [ "\${red_n:-0}" -gt 120 ] && [ "\${red_w:-0}" -gt 60 ]; then
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

# 快捷登录窗口（QQ 重启后记住账号，显示头像 + 蓝色「登录」按钮）：
# 检测到蓝按钮就点，免得人去点。有两种窗口形态，按钮位置不同：
#   窄版 320x460：按钮中心 (160,327)，核心区窗口相对 +105+313（110x28）；
#   宽版 680x440：QQ 崩溃/被反复杀后容易掉进这种，按钮中心 (340,354)，
#                 核心区窗口相对 +285+340（110x28）。
# 判据：核心区成片 QQ 蓝 #0099FF——窄版实测 2863/3080 像素是蓝；二维码窗口
# 这个位置没有这片蓝，不会误点；主窗口尺寸不符也不会进来。
quick_login() {
  command -v convert >/dev/null 2>&1 || return 0
  local id X Y WIDTH HEIGHT shot bx by cx cy blue state n
  shot=/tmp/.ark_quick.png
  for id in \$(xdotool search --onlyvisible --name '^QQ\$' 2>/dev/null); do
    eval "\$(xdotool getwindowgeometry --shell "\$id" 2>/dev/null)" || continue
    # 窄版 320x460 或宽版 680x440 都认（复用 is_login_window 的窄版判据）
    if [ "\$WIDTH" -ge 600 ] && [ "\$WIDTH" -le 760 ] && [ "\$HEIGHT" -ge 400 ] && [ "\$HEIGHT" -le 500 ]; then
      bx=\$((X + 285)); by=\$((Y + 340)); cx=340; cy=354
    elif is_login_window "\$WIDTH" "\$HEIGHT"; then
      bx=\$((X + 105)); by=\$((Y + 313)); cx=160; cy=327
    else
      continue
    fi
    scrot -o "\$shot" >/dev/null 2>&1 || return 0
    blue=\$(convert "\$shot" -crop 110x28+\${bx}+\${by} txt:- 2>/dev/null | awk -F'[(,)]' 'NR>1 && \$3+0<100 && \$4+0>120 && \$4+0<200 && \$5+0>200 {n++} END{print n+0}')
    echo "\$(date +%T) quick_blue=\$blue \${WIDTH}x\${HEIGHT}" >> /tmp/wd.log
    rm -f "\$shot"
    # 防抖：连续两轮都看到蓝按钮才点，避开登录过渡画面
    if [ "\${blue:-0}" -gt 1200 ]; then
      state=/tmp/.ark_quick_state
      n=\$(cat "\$state" 2>/dev/null || echo 0)
      if [ "\$n" -lt 1 ]; then echo 1 > "\$state"; return 0; fi
      echo 0 > "\$state"
      xdotool windowactivate "\$id" 2>/dev/null; sleep 0.4
      xdotool mousemove --window "\$id" \$cx \$cy click 1 2>/dev/null
      echo "[\$(date '+%F %T')] quick login -> clicked 登录 (\${WIDTH}x\${HEIGHT})"
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

# 「下线通知」弹窗（账号在别处终端登录，本机被踢下线）：盖在登录窗口上，
# 只有一颗「确定」按钮，Escape 对 frameless 弹窗不保证有效，必须精确点按钮。
# 弹窗不管是独立窗口还是画在登录窗口内部的覆盖层，像素都落在登录窗口附近，
# 所以扫「登录窗口外扩 60px 的下半部」（按钮在约 2/3 高度处；上半部的 QQ
# logo 也是蓝色，限定下半窗 + 行密度双保险排除）。按钮是实心蓝 #0099FF：
# 最密一行约几十个蓝像素；「账号登录」那种蓝色文字一行只有稀疏几个，
# 密度判据能干净分开。实测（真实弹窗截图）：按钮 52px 宽、峰值行 45px；
# 正常二维码页底部蓝链接峰值 68px 但行带高度只有 1px，被高度下限挡住。
# 双按钮弹窗守卫：蓝按钮右侧同一行如果有浅灰按钮边框（195~215 的中性灰），
# 说明这是个「蓝按钮 + 白按钮」的双按钮弹窗——典型就是「更新提醒」：
# 蓝色是「立即重启更新」（点了会重启 QQ 丢登录态！），白色才是「关闭」。
# 这种情况绝不能点蓝按钮，改按 Escape（实测能关掉更新提醒）。
#   实测（真机）：关闭按钮左右边框 x=721/798，蓝按钮右缘 712——同排灰边框
#   就是双按钮的铁证；下线通知是单蓝按钮，右侧同排一片纯白，不会误判。
dismiss_offline() {
  command -v convert >/dev/null 2>&1 || return 0
  local id X Y WIDTH HEIGHT shot hit bx cy ox wxoff
  shot=/tmp/.ark_off.png
  for id in \$(xdotool search --onlyvisible --name '^QQ\$' 2>/dev/null); do
    eval "\$(xdotool getwindowgeometry --shell "\$id" 2>/dev/null)" || continue
    is_login_window "\$WIDTH" "\$HEIGHT" || continue
    scrot -o "\$shot" >/dev/null 2>&1 || return 0
    ox=\$((X > 60 ? X - 60 : 0))
    wxoff=\$((X - ox))
    hit=\$(convert "\$shot" -crop "\$((WIDTH + 120))x\$((HEIGHT / 2 + 30))+\${ox}+\$((Y + HEIGHT / 2))" txt:- 2>/dev/null | awk -F'[(,)]' -v wxoff="\$wxoff" -v half="\$((HEIGHT / 2))" -v ww="\$WIDTH" '
      NR>1 {
        x=\$1+0; y=\$2+0
        if (\$3+0<120 && \$4+0>110 && \$4+0<210 && \$5+0>190) {
          cnt[y]++
          if (!(y in rmin) || x<rmin[y]) rmin[y]=x
          if (!(y in rmax) || x>rmax[y]) rmax[y]=x
        }
        if (n<30000 && \$3+0>=195 && \$3+0<=215 && \$4+0>=195 && \$4+0<=215 && \$5+0>=195 && \$5+0<=215 && (\$3-\$4<6) && (\$4-\$5<6) && (\$5-\$3<6)) {
          n++; gx[n]=x; gy[n]=y
        }
      }
      END {
        best=0; by=-1
        for (y in cnt) if (cnt[y]>best) { best=cnt[y]; by=y }
        if (best<30) { print ""; exit }
        lo=by; hi=by
        while ((lo-1) in cnt && cnt[lo-1]>=best/2) lo--
        while ((hi+1) in cnt && cnt[hi+1]>=best/2) hi++
        xmin=rmin[lo]; xmax=rmax[lo]
        for (y=lo+1; y<=hi; y++) {
          if (rmin[y]<xmin) xmin=rmin[y]
          if (rmax[y]>xmax) xmax=rmax[y]
        }
        w=xmax-xmin+1; h=hi-lo+1
        if (!(w>=ww*0.12 && w<=ww*0.6 && h>=18 && h<=55 && best>=w/2)) { print ""; exit }
        twin=0
        for (i=1; i<=n; i++)
          if (gy[i]>=lo && gy[i]<=hi && gx[i]>xmax+1 && gx[i]<=xmax+150) {
            gcnt[gx[i]]++
            if (gcnt[gx[i]]>twin) twin=gcnt[gx[i]]
          }
        # 边框是一条竖线（同一 x 上连续二三十行）；零散灰阶抗锯齿到不了 10 行
        if (twin>=10) { print "esc"; exit }
        print xmin+int(w/2)-wxoff, half+int((lo+hi)/2)
      }')
    [ -n "\$hit" ] || continue
    xdotool windowactivate "\$id" 2>/dev/null; sleep 0.4
    if [ "\$hit" = "esc" ]; then
      xdotool key --clearmodifiers Escape 2>/dev/null
      echo "[\$(date '+%F %T')] twin-button dialog over login window -> escaped (never click its blue button)"
    else
      bx=\${hit%% *}; cy=\${hit##* }
      xdotool mousemove --window "\$id" "\$bx" "\$cy" click 1 2>/dev/null
      echo "[\$(date '+%F %T')] offline notice -> clicked confirm at \$bx,\$cy"
    fi
    sleep 1
  done
}

# 「更新提醒」弹窗（修复了一些已知问题/新增了戳一戳……）：画在 QQ 主窗口
# 内部的覆盖层，xdotool 看不到独立窗口，dismiss_popups 的 Escape 够不着它
# （它只处理独立窗口）。蓝按钮是「立即重启更新」——点了 QQ 会重启升级、
# 丢登录态，**永远不能点**；正确做法是 Escape（实测能关掉它，等效点「关闭」）。
# 判据：大窗口（主窗口，非登录窗）内出现实心蓝按钮行带。实测真机：
#   有弹窗 峰值=108 宽=108 高=32 → 命中；正常主窗口 蓝峰值=0 → 不误触发。
dismiss_update() {
  command -v convert >/dev/null 2>&1 || return 0
  local id X Y WIDTH HEIGHT shot hit ox oy wxoff wyoff
  shot=/tmp/.ark_upd.png
  for id in \$(xdotool search --onlyvisible --name '^QQ\$' 2>/dev/null); do
    eval "\$(xdotool getwindowgeometry --shell "\$id" 2>/dev/null)" || continue
    # 窄版 320x460 和宽版 680x440 都是登录窗本体，不是弹窗，别碰
    is_login_window "\$WIDTH" "\$HEIGHT" && continue
    if [ "\$WIDTH" -ge 600 ] && [ "\$WIDTH" -le 760 ] && [ "\$HEIGHT" -ge 400 ] && [ "\$HEIGHT" -le 500 ]; then continue; fi
    [ "\$WIDTH" -gt 400 ] && [ "\$HEIGHT" -gt 400 ] || continue
    scrot -o "\$shot" >/dev/null 2>&1 || return 0
    ox=\$((X > 40 ? X - 40 : 0)); oy=\$((Y > 40 ? Y - 40 : 0))
    wxoff=\$((X - ox)); wyoff=\$((Y - oy))
    hit=\$(convert "\$shot" -crop "\$((WIDTH + 80))x\$((HEIGHT + 80))+\${ox}+\${oy}" txt:- 2>/dev/null | awk -F'[(,)]' '
      NR>1 && \$3+0<120 && \$4+0>110 && \$4+0<210 && \$5+0>190 {
        x=\$1+0; y=\$2+0
        cnt[y]++
        if (!(y in rmin) || x<rmin[y]) rmin[y]=x
        if (!(y in rmax) || x>rmax[y]) rmax[y]=x
      }
      END {
        best=0; by=-1
        for (y in cnt) if (cnt[y]>best) { best=cnt[y]; by=y }
        if (best<30) { print ""; exit }
        lo=by; hi=by
        while ((lo-1) in cnt && cnt[lo-1]>=best/2) lo--
        while ((hi+1) in cnt && cnt[hi+1]>=best/2) hi++
        xmin=rmin[lo]; xmax=rmax[lo]
        for (y=lo+1; y<=hi; y++) {
          if (rmin[y]<xmin) xmin=rmin[y]
          if (rmax[y]>xmax) xmax=rmax[y]
        }
        w=xmax-xmin+1; h=hi-lo+1
        if (w>=60 && w<=200 && h>=20 && h<=55 && best>=w/2) print "esc"
        else print ""
      }')
    [ "\$hit" = "esc" ] || continue
    xdotool windowactivate "\$id" 2>/dev/null; sleep 0.4
    xdotool key --clearmodifiers Escape 2>/dev/null
    echo "[\$(date '+%F %T')] update reminder -> escaped (never restart QQ)"
    sleep 1
  done
}

watchdog() {
  dismiss_popups
  dismiss_offline
  dismiss_update
  quick_login
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
# 先清掉占着 6800 的老进程：systemd 重启时 KillMode=process 只杀 start.sh，
# 上一轮的 webui python 会继续持有端口，新实例绑定时直接 EADDRINUSE 退出，
# 然后 Restart=always 无限重启——整栈看起来"活着"，实际 WebUI 早没了（真机踩过）。
command -v fuser >/dev/null 2>&1 && fuser -k 6800/tcp >/dev/null 2>&1
sleep 1
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
