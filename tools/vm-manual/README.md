# 人工操作口：VNC 手动接管 VM 里的 QQ

watchdog 自动登录认的是「已收录」的窗口形状 —— 快捷登录、二维码、更新弹窗。
凡是它没收录的界面（新版本的验证页、设备锁、异常提示），就需要一个人能**亲手用
鼠标键盘**操作 VM 里的 QQ。这个口就是干这个的：`x11vnc` 绑在 QQ 所在的 Xvfb
显示器（:99）上，宿主用任意 VNC 客户端连上去，看到的画面和 QQ 实际的那块屏一模一样。

## 装（VM 里）

```bash
# 密码自动生成；也可以 bash install.sh <密码> 自己定（VNC 协议只认前 8 位）
bash tools/vm-manual/install.sh
```

装完打印宿主侧的 NAT 命令。VM 是重建过的（`arklink env create`），记得补：

```powershell
# 宿主机（Windows）
VBoxManage controlvm arklink natpf1 "vnc,tcp,127.0.0.1,5903,,5900"
```

## 连（宿主机）

任意 VNC 客户端（TightVNC Viewer、RealVNC Viewer 都行；Windows 自带的远程桌面不行）：

- 地址：`127.0.0.1:5903`（只在本机回环上，不出网）
- 密码：装的时候打印过；忘了就在 VM 里 `sudo x11vnc -storepasswd 新密码 /home/arklink/.vncpass` 重设

连上之后看到的就是 :99 的桌面。QQ 掉了/卡在哪个界面，直接点。

## 注意

- **别在 VNC 里点「退出登录」**——那会让收发全断，得重新扫码。
- x11vnc 是 systemd 服务（`x11vnc.service`），开机自启、掉了自动拉起，
  `systemctl status x11vnc` 看状态。
- 它只多一个监听口，不影响 ArkLink / SnowLuma / dsh-arkhub 的任何行为；
  平时不连它几乎零开销。
