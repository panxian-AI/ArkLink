# 无头 Linux 里跑 QQ：真正「不影响其他程序」的发送方案

> 结论：**可行，而且是目前唯一能真正做到「后台、不抢焦点、不碰剪贴板、不影响宿主机」的方案**。
> 这台机器上只有 QQ 和我们的程序，抢焦点这件事失去了意义。

## 一、为什么这条路能解决问题

Windows 上 UI 通道的死结在于：键鼠输入只会送给**前台窗口**。所以每次发送都要
把 QQ 弹到最前面、占用剪贴板——对「不影响其他程序」是硬冲突。

把 QQ 放进一个**没有别人的 X 会话**（Xvfb 虚拟屏幕）之后：

| 问题 | 在 Xvfb 里 |
|------|-----------|
| 抢焦点 | 只在这个虚拟屏幕内激活窗口，宿主机焦点不动 |
| 占用剪贴板 | `xdotool type` 走 X 键盘事件，不经过剪贴板 |
| 弹窗打扰 | 没有真实显示器，没人看得见 |
| 多账号互相干扰 | 一个容器/VM 一个号，进程、数据、显示都隔离 |

代价是要多跑一套环境（约 1–2GB 内存、2 vCPU），以及**首次登录必须扫码**。

## 二、发行版与「桌面」怎么选

先纠正一个常见误区：**这里不需要桌面环境（DE），只需要一个窗口管理器（WM）**。
桌面环境是给人用的（面板、文件管理器、设置中心），我们只需要它能「把窗口切到前台」——
而这正是 WM 的职责。多装一个 DE，等于多几百 MB 内存和一堆会抢焦点的东西。

| 选择 | 体积 | 结论 |
|------|------|------|
| **Debian 12 slim + openbox** | 基础 ~75MB，装完 ~1.2GB | ✅ 首选：glibc、apt 能直接装官方 deb、openbox 稳 |
| Ubuntu Server 24.04 minimal + openbox | ~1.5GB | ✅ 次选：依赖更全，出问题资料多 |
| Debian + **jwm** | 比 openbox 再小 ~1.5MB | ✅ 极简党：配置是 XML，够用就一行 `<Program>` |
| Debian + icewm/fluxbox | 小 | ✅ 都行，选你熟的 |
| **i3 / sway（平铺）** | 小 | ❌ 会改变窗口尺寸与位置，坐标全乱 |
| Lubuntu / Xubuntu / antiX | 1.5–3GB | ⚠️ 能跑，但为了「无头」装整套桌面不划算 |
| **Alpine** | 极小 | ❌ musl libc，QQ Linux 版是 glibc 编译的 |
| Tiny Core | 极小 | ❌ 依赖要自己凑，Qt/Chromium 依赖链很长 |

容器镜像 `debian:12-slim` 已经够用；想要"虚拟机级隔离"再用 KVM/Proxmox 跑同一个镜像。

官方下载（认准 im.qq.com，别用第三方转载）：

* 页面：<https://im.qq.com/linuxqq/index.shtml>
* 版本历史（含直链与校验和）：<https://github.com/Rodert/qq-versions/releases>
* 三种包：`.deb` / `.rpm` / `.AppImage`（AppImage 免安装，适合塞进容器）

### 就想用 Ubuntu？可以，但别装 Ubuntu Desktop

Ubuntu 和 Debian 同源，这套方案整条都能用，唯一要改的是镜像和依赖名：

| 镜像 | 结论 |
|------|------|
| **Ubuntu Server 24.04 LTS（minimal）** | ✅ 首选 Ubuntu 路线：无 GUI，装完 ~1.5GB，空闲内存 150–200MB |
| **Ubuntu Base 24.04**（rootfs tarball） | ✅ 塞容器最省，基础 ~30MB，自己加 sysv/systemd 都行 |
| Ubuntu Desktop 24.04 | ❌ 不要：GNOME + gdm3 + snap，开机就 1.2GB，自带通知与焦点行为 |
| Lubuntu / Xubuntu | ⚠️ 能跑，但为「无头」背一整套桌面不划算 |

相比 Debian 12 slim 的代价只是多 ~100MB 内存、systemd 服务多几个；换来的是**官方
deb 主要在 Ubuntu 上验证过、搜到的答案九成能直接套**。如果你本来就用 Ubuntu，选它没错。

四个 Ubuntu 专属的坑，按踩到的概率排序：

1. **中文字体缺失**（必踩）：`minimal` 镜像不带 CJK 字体，发给对方的中文在 QQ 里
   全是方块，而且字体一缺布局就变，坐标校准跟着一起错。装 `fonts-wqy-microhei`。
2. **`libasound2` 改名**：24.04 起是 `libasound2t64`，手抄依赖清单必报
   `Depends: libasound2 but it is not installable`。**别用 `dpkg -i`**，用
   `apt install ./QQ_*.deb`，让 apt 自己解依赖。
3. **非特权 user namespace 被 AppArmor 挡住**：24.04 默认
   `kernel.apparmor_restrict_unprivileged_userns=1`，Chromium 内核的 QQ 可能起不来
   沙箱。表现是启动即退或空白窗口。确认真是这条再关：
   ```bash
   sysctl -w kernel.apparmor_restrict_unprivileged_userns=0   # 临时
   echo 'kernel.apparmor_restrict_unprivileged_userns=0' > /etc/sysctl.d/99-qq.conf
   ```
   容器里更简单：直接 `--no-sandbox`（安全边界由容器负责）。
4. **不要 `apt install ubuntu-desktop` / `tasksel`**：装了就把 GNOME 的窗口管理器和
   守护进程一起带进 Xvfb，费内存还抢焦点。Ubuntu 路线只需要
   `xvfb openbox xdotool`，和 Debian 一字不差。

## 三、最小环境（Debian 12 / Ubuntu 24.04，容器或虚拟机都行）

```bash
# 1) 基础：虚拟屏幕 + 窗口管理器 + 按键工具 + 中文字体
apt update && apt install -y \
  xvfb openbox xdotool x11vnc \
  fonts-wqy-microhei fontconfig \
  libgbm1 libasound2 libnss3 libxss1 libxtst6 libatk-bridge2.0-0 \
  python3 python3-pip ca-certificates wget
  # Ubuntu 24.04 上把 libasound2 换成 libasound2t64（t64 改名），别两个都写

# 2) QQ Linux 版（NT 架构）。版本号会变，去 im.qq.com/linuxqq 或
#    github.com/Rodert/qq-versions/releases 取当前直链
wget -O /tmp/qq.deb https://qqdl.gtimg.cn/qqfile/QQNT/9.9.33/release/3f89efc5/QQ_3.2.32_260812_amd64_01.deb
apt install -y /tmp/qq.deb || (apt install -f -y && dpkg -i /tmp/qq.deb)

# 3) ArkLink
pip install -e /opt/arklink

# 4) 起虚拟屏幕（1280x800 足够），窗口管理器必备——没有 wm，窗口不会「激活」
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99
openbox &
```

首次登录有两种看法，都只用于扫码那一次：

```bash
# A. VNC（客户端装个 VNC Viewer）
x11vnc -display :99 -nopw -listen 0.0.0.0 -forever

# B. 浏览器里看（noVNC，不用装客户端，适合远程服务器）
apt install -y novnc websockify
websockify --web /usr/share/novnc 6080 localhost:5900 &
# 浏览器打开 http://<host>:6080/vnc.html
```

扫完把 `~/.config/QQ`（或容器里的 `data/`）备份下来，下次换机器直接拷，省一次扫码。

## 四、发送

```bash
export DISPLAY=:99
python -m arklink send --to 备注名 --text "hi" --engine x11 --yes
python -m arklink serve webui --port 6800 --engine x11
```

`X11Engine` 做的事（`arklink/ntqq/x11.py`）：

```
xdotool search --name QQ          → 找窗口
xdotool windowactivate --sync ID  → 在这个虚拟屏幕里激活（外部无感）
xdotool mousemove --window ID …   → 点会话搜索框
xdotool type --clearmodifiers 备注名 → 输入收件人
xdotool click 1                   → 点第一条结果
xdotool mousemove … / click 1     → 点输入框
xdotool type --clearmodifiers …   → 输入正文
xdotool key Return                → 发送
```

坐标默认按 1280x800 的默认布局算（搜索框 `(150,65)`，输入框 65% × 87%）。
窗口大小/缩放变了就用参数校准：

```python
X11Engine(search_xy=(150, 65), input_xy=(0.65, 0.87))
```

## 五、Docker（更省事）

```dockerfile
FROM debian:12-slim
RUN apt update && apt install -y --no-install-recommends \
      xvfb openbox xdotool x11vnc fonts-wqy-microhei \
      libgbm1 libasound2 libnss3 libxss1 libxtst6 libatk-bridge2.0-0 \
      python3 python3-pip ca-certificates wget \
 && wget -O /tmp/qq.deb https://dldir1.qq.com/qqfile/qq/QQNT/Linux/QQ_3.2.7_240808_amd64_01.deb \
 && apt install -y /tmp/qq.deb && rm -f /tmp/qq.deb
COPY . /opt/arklink
RUN pip install --break-system-packages -e /opt/arklink
ENV DISPLAY=:99
CMD ["bash","-lc","Xvfb :99 -screen 0 1280x800x24 & openbox & sleep 1; \
     /opt/qq/qq --no-sandbox & sleep 8; \
     python -m arklink serve webui --port 6800 --engine x11 --host 0.0.0.0"]
```

要点：

* `--no-sandbox`：容器里 QQ 的 Chromium 沙箱起不来，必须关（安全边界由容器负责）；
* `openbox` 不能省：没有窗口管理器，`windowactivate` 不会生效；
* 端口 6800 是 WebUI，映射出来加 `--password`；VNC 只用来首次扫码，扫完就别暴露。

## 六、和 Windows 注入通道的关系

两条腿，各管一段：

| 场景 | 通道 | 后台发送 |
|------|------|---------|
| Windows 日常（人在用这台机器） | `host`（UI 通道） | ❌ 会抢焦点 |
| 无头 Linux / 容器 / 家用服务器 | `x11`（Xvfb + xdotool） | ✅ 完全互不影响 |
| 两条腿都想要"进程内直调" | `pipe`（hook，待完成） | ✅ 不依赖任何窗口 |

Linux 端的注入（`ptrace + dlopen`）代码已经在仓库里，等 `pipe` 引擎补上 send，
无头环境就能彻底摆脱「模拟点击」。

## 七、已知坑

1. **首次扫码**：QQ Linux 版没有无交互登录；扫完记得备份登录态目录。
2. **字体**：不装 `fonts-wqy-*` 会看到方块，坐标校准也会失败（布局会变）。
3. **GPU**：Xvfb 无 GPU，QQ 会走软件渲染，启动慢 5–10 秒；日志里报 `GPU process` 属正常。
4. **DPI/缩放**：容器里默认 96 DPI，和宿主机不一致时以容器内截图为准校准。
5. **`xdotool type` 打不出中文**：需要 `--clearmodifiers` + 正确的 XKB 布局；
   实在不行用 `xdotool key` 逐字发 Unicode（`xdotool type --delay 30`）。
6. **切换会话的偶发延迟**：`open_delay` 从 0.7 起调，网络慢的机器加到 1.2。
7. **AppImage**：容器里没有 FUSE 时用 `./QQ.AppImage --appimage-extract` 解包再跑，
   或加 `--no-sandbox`；解包后入口在 `squashfs-root/qq`。
8. **不要装桌面环境**：GNOME/KDE 会带来自己的窗口管理器和一堆守护进程，
   在 Xvfb 里既费内存又可能抢走焦点，反而更容易出错。

## 八、资源账单：每种「虚拟环境」到底吃多少盘

先看本机余量（实测）：**C: 剩 20.3GB、D: 剩 87.3GB、E: 剩 335.7GB、物理内存 15GB**。
原则很简单：**虚拟机镜像一律放 E: 或 D:，C: 一个字节都不占**（只多 VirtualBox 本体 ~300MB）。

现有参照物（实测）：Windows 版 QQ 装完 **1.3GB**，用户数据（聊天记录/图片缓存）
**2.5GB**——后者会持续增长，是真正需要防的那个数字。

| 形态 | 宿主磁盘增量（稳态） | 峰值（含安装包） | 内存常驻 | 需重启 | 要重新登录 QQ |
|------|-------------------|----------------|---------|-------|--------------|
| **VirtualBox + Ubuntu Server 24.04** | **3.4GB**（VDI 3.1 + 本体 0.3） | 6.0GB（+ISO 2.6） | 2.0GB（VM 分配值） | 否 | 是（一次扫码） |
| **WSL2 + Ubuntu 24.04** | **2.3GB**（VHDX 稀疏） | 3.0GB | 2.0GB（`.wslconfig` 限） | **是**（开 Hyper-V 平台） | 是（一次扫码） |
| **Windows 独立会话** | **1.5GB**（新 profile 的 QQ 数据） | 1.5GB | **0**（QQ 本来就在跑） | 否 | 是（换用户=换数据目录） |
| Windows 虚拟机（Win11 guest） | 22GB+ | 26GB+ | 4.0GB | 否 | 是 |
| 同会话 UI 通道（现状） | 0 | 0 | 0 | 否 | 不需要 |

### 3.4GB 是怎么算出来的（guest 内明细）

| 项 | 大小 | 能否省 |
|---|---|---|
| Ubuntu Server 24.04 最小安装 | 2.0–2.4GB | 换 Ubuntu Base rootfs 可到 ~0.4GB，但内核要自己配 |
| QQ Linux 版（deb 装完） | ~600MB | 不能 |
| 依赖（Qt/Chromium 运行库、字体、Xvfb、openbox、xdotool） | ~450MB | 去掉 `--no-install-recommends` 之外的可选项，已基本到底 |
| Python venv + Arklink | ~120MB | 可省（不用 venv 直接装系统 python，-100MB） |
| 用户数据初值 | ~50MB | 会随聊天增长，见下 |
| **合计** | **≈3.4GB** | |

### 五条把账单压住的做法

```bash
# 1) 镜像放 E:，创建时就把上限钉死 8GB（动态分配，实际只占 3.x GB）
VBoxManage createmedium disk --filename "E:/vm/arklink/arklink.vdi" --size 8192 --variant Standard
# 2) 装完立刻卸掉 ISO（省 2.6GB）
VBoxManage storageattach arklink --storagectl SATA --port 1 --device 0 --medium none
# 3) 关 swap（宿主机 15GB 内存，guest 2GB 不需要交换，省 1GB）
swapoff -a && sed -i '/swap/s/^/#/' /etc/fstab && rm -f /swap.img
# 4) 清 apt 缓存与索引（省 ~250MB）
apt-get clean && rm -rf /var/lib/apt/lists/*
# 5) 删 snapd（Ubuntu Server 默认带，占 ~300MB，QQ 用不到）
apt-get purge -y snapd && rm -rf /var/cache/snapd
```

QQ 自己的缓存才是长期风险：设置里把「图片/视频自动下载」关掉、缓存上限设 500MB，
并定期清 `~/.config/QQ/nt_data/{Pic,File,Video}`。容器/VM 里没有回收站，删了就是删了。

### 复核命令（别信估算，装完量一次）

```bash
df -h /                                   # 全盘
du -sh /opt/QQ ~/.config/QQ /opt/arklink*  # 三个大头
du -sh /var/cache/apt /var/lib/apt/lists
```

宿主侧看 VDI 真实占用：VirtualBox 管理器里该虚拟机的「虚拟介质管理器」，
或 `VBoxManage showmediuminfo disk "E:/vm/arklink/arklink.vdi"`（看 `Actual size`）。
装完若远大于 3.4GB，八成是没删 ISO、没清 apt、或者装了桌面环境。

### 一句话选型

* **只要后台发送、能接受重启后重新登录** → Windows 独立会话，**1.5GB、0 内存**，最快；
* **要一个真正隔离、可整体备份/迁移的环境** → VirtualBox + Ubuntu Server，**3.4GB / 2GB 内存**；
* **C: 只剩 20GB 且不想装 VirtualBox** → WSL2，**2.3GB**，代价是开 Hyper-V 并重启一次；
* Windows 虚拟机可以直接排除：**22GB 起步**，是 Linux 方案的 6 倍。
