"""VBoxManage 编排：把「建一个专门跑 QQ 的虚拟机」拆成可测试的命令序列。

设计要点：
* 所有命令由**纯函数**生成（`create_commands` / `unattended_commands` / …），
  测试只看 argv，不需要真装 VirtualBox；
* 生成时 argv[0] 统一写 `VBoxManage`，执行时由 `VBox.run` 替换成真实路径；
* `runner` 可注入，和 `x11.py` 同一套路；
* 一台机器一台 VM，名字固定，避免用户面对一堆 UUID。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from .plan import EnvPlan

# VBox 的 ostype 标识各版本有增补，Ubuntu_64 是长期稳定的那个
DEFAULT_OSTYPE = "Ubuntu_64"
VBM = "VBoxManage"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class VBoxError(RuntimeError):
    """VBoxManage 返回非零。`argv` 留着，方便用户自己复现。"""

    def __init__(self, argv: Sequence[str], stderr: str, code: int) -> None:
        self.argv = list(argv)
        self.stderr = (stderr or "").strip()
        self.code = code
        super().__init__(f"VBoxManage 失败({code}): {' '.join(self.argv[1:])}\n{self.stderr}")


# ---------------------------------------------------------------- 命令构造
def create_commands(plan: EnvPlan, *, ostype: str = DEFAULT_OSTYPE) -> list[list[str]]:
    """建 VM + 建盘 + 挂盘 + 端口映射。不含 ISO 与无人值守安装（那步要密码）。"""
    name, workdir, vdi = plan.name, plan.workdir, plan.vdi
    return [
        [VBM, "createvm", "--name", name, "--basefolder", workdir,
         "--ostype", ostype, "--register"],
        [VBM, "modifyvm", name,
         "--memory", str(plan.ram_mb),
         "--cpus", str(plan.cpus),
         "--vram", "16",
         "--graphicscontroller", "vmsvga",
         # 没声卡需求，省一点；剪贴板和拖放封死，避免和宿主互通（隔离的一部分）
         "--audio-driver", "none",
         "--clipboard-mode", "disabled",
         "--draganddrop", "disabled",
         "--nic1", "nat",
         "--boot1", "dvd", "--boot2", "disk",
         "--rtcuseutc", "on"],
        [VBM, "createmedium", "disk", "--filename", vdi,
         "--size", str(int(round(plan.disk_limit_gb * 1024))),
         "--format", "VDI", "--variant", "Standard"],
        [VBM, "storagectl", name, "--name", "SATA", "--add", "sata",
         "--controller", "IntelAhci", "--portcount", "2", "--bootable", "on"],
        [VBM, "storageattach", name, "--storagectl", "SATA",
         "--port", "0", "--device", "0", "--type", "hdd", "--medium", vdi],
        [VBM, "modifyvm", name,
         "--natpf1", f"webui,tcp,127.0.0.1,{plan.webui_port},,6800",
         "--natpf1", f"ssh,tcp,127.0.0.1,{plan.ssh_port},,22"],
    ]


def unattended_commands(
    plan: EnvPlan,
    iso: str,
    *,
    user: str,
    password: str,
    additions_iso: str = "",
    post_install: str = "",
    locale: str = "en_US",
    country: str = "CN",
    timezone: str = "Asia/Shanghai",
) -> list[list[str]]:
    """无人值守安装：VBox 自己生成 autoinstall 应答盘，装完自动跑 post-install。

    默认 post-install 给该用户开 NOPASSWD sudo，后面 `env push` 推代码就不用再喂密码。
    """
    cmd = [VBM, "unattended", "install", plan.name,
           f"--iso={iso}",
           f"--user={user}",
           f"--password={password}",
           f"--full-user-name={user}",
           f"--hostname={plan.name}",
           f"--locale={locale}",
           f"--country={country}",
           f"--time-zone={timezone}"]
    if additions_iso:
        cmd += ["--install-additions", f"--additions-iso={additions_iso}"]
    sudoers = (
        f"echo '{user} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/{user} "
        f"&& chmod 440 /etc/sudoers.d/{user}"
    )
    cmd += ["--post-install-command", post_install or sudoers]
    return [cmd]


def eject_commands(name: str) -> list[list[str]]:
    """装完摘掉 ISO 并改回硬盘启动——否则每次重启又进安装程序。"""
    return [
        [VBM, "storageattach", name, "--storagectl", "SATA",
         "--port", "1", "--device", "0", "--type", "dvddrive", "--medium", "none"],
        [VBM, "modifyvm", name, "--boot1", "disk", "--boot2", "none"],
    ]


def copyto_commands(
    name: str, source: str, dest: str, *, user: str, password: str
) -> list[list[str]]:
    """把宿主目录推进 guest（要 Guest Additions，unattended 已装）。"""
    return [
        [VBM, "guestcontrol", name, "copyto", source, dest,
         "--username", user, "--password", password, "--recursive"]
    ]


def guest_run_commands(
    name: str, script: str, *, user: str, password: str
) -> list[list[str]]:
    """在 guest 里跑一段 shell（sudo 由 post-install 的 NOPASSWD 兜住）。"""
    return [
        [VBM, "guestcontrol", name, "run", "--exe", "/bin/bash",
         "--username", user, "--password", password,
         "--wait-stdout", "--wait-stderr", "--", "-lc", script]
    ]


# ---------------------------------------------------------------- 执行与解析
@dataclass
class VBox:
    """只做执行与解析。`exe` 为空表示没装 VirtualBox。"""

    exe: str
    runner: Runner | None = None
    verbose: bool = False

    def __post_init__(self) -> None:
        if not self.exe:
            raise VBoxError([VBM], "未安装 VirtualBox（找不到 VBoxManage）", 127)
        if self.runner is None:
            self.runner = subprocess.run

    def _argv(self, argv: Sequence[str]) -> list[str]:
        return [self.exe, *argv[1:]]

    def run(self, argv: Sequence[str], *, timeout: int = 900) -> str:
        real = self._argv(argv)
        if self.verbose:
            print("+", " ".join(real))
        proc = self.runner(real, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise VBoxError(real, getattr(proc, "stderr", ""), proc.returncode)
        return getattr(proc, "stdout", "") or ""

    def apply(self, commands: Sequence[Sequence[str]], *, timeout: int = 900) -> list[str]:
        """顺序执行，任一失败即停。返回每条命令的 stdout。"""
        return [self.run(cmd, timeout=timeout) for cmd in commands]

    # ------------------------------------------------------------ 查询
    def version(self) -> str:
        return self.run([VBM, "--version"], timeout=30).strip()

    def guest_additions_iso(self) -> str:
        if os.name == "nt":
            cands = [
                os.path.join(os.path.dirname(self.exe), "VBoxGuestAdditions.iso"),
                r"C:\Program Files\Oracle\VirtualBox\VBoxGuestAdditions.iso",
            ]
        else:
            cands = [
                "/usr/share/virtualbox/VBoxGuestAdditions.iso",
                "/opt/VirtualBox/VBoxGuestAdditions.iso",
            ]
        for cand in cands:
            if os.path.isfile(cand):
                return cand
        return ""

    def list_vms(self) -> dict[str, str]:
        """名字 -> UUID。VBox 输出形如 `"arklink" {2b3c...}`。"""
        out: dict[str, str] = {}
        text = self.run([VBM, "list", "vms"], timeout=60)
        for line in text.splitlines():
            m = re.match(r'^"(.+?)"\s+\{([0-9a-fA-F-]{36})\}\s*$', line.strip())
            if m:
                out[m.group(1)] = m.group(2)
        return out

    def exists(self, name: str) -> bool:
        return name in self.list_vms()

    def status(self, name: str) -> str:
        """running / poweroff / saved / aborted …；VM 不存在返回 missing。"""
        if not self.exists(name):
            return "missing"
        text = self.run([VBM, "showvminfo", name, "--machinereadable"], timeout=60)
        for line in text.splitlines():
            if line.startswith("VMState="):
                return line.split("=", 1)[1].strip().strip('"')
        return "unknown"

    def disk_usage_gb(self, vdi: str) -> float:
        """虚拟盘真实占用（不是 8GB 上限）。文件不在就返回 0。"""
        try:
            return round(os.path.getsize(vdi) / 1024**3, 2)
        except OSError:
            return 0.0

    # ------------------------------------------------------------ 动作
    def start(self, name: str) -> str:
        """无头启动：没有窗口，宿主机桌面不会多出任何东西。"""
        return self.run([VBM, "startvm", name, "--type", "headless"], timeout=180)

    def stop(self, name: str, *, force: bool = False) -> str:
        action = "poweroff" if force else "acpipowerbutton"
        return self.run([VBM, "controlvm", name, action], timeout=120)

    def destroy(self, name: str, *, delete_disks: bool = True) -> str:
        if not self.exists(name):
            return ""
        cmd = [VBM, "unregistervm", name] + (["--delete"] if delete_disks else [])
        return "\n".join(self.apply([cmd], timeout=300))

    def describe(self, name: str) -> dict[str, object]:
        state = self.status(name)
        return {"name": name, "state": state, "running": state == "running"}
