"""装之前先把账算清楚：放哪块盘、要多少、够不够。

这里的数字全部来自实测（见 docs/无头Linux运行.md 第八节）：
Ubuntu Server 最小安装 ~2.2GB + QQ ~0.6GB + 依赖 ~0.45GB + 用户数据初值。

`plan()` 只做决策，不碰磁盘、不跑命令——`arklink env plan` 靠它回答
「我这台机器能不能装、装在哪个盘、会占多少」。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .probe import Drive, HostReport

# 虚拟磁盘的**上限**（动态分配，实际只占 EXPECT_GB）
DISK_LIMIT_GB = 8.0
# 装完的预期真实占用，用来告诉用户"别慌，不是 8GB"
EXPECT_GB = 3.4
# 目标盘装完之后至少要剩这么多，否则换盘（留出日常使用余量）
RESERVE_GB = 5.0
# 内存与核数：QQ + Xvfb + WebUI 的实测够用值，再多就是浪费宿主机
RAM_MB = 2048
CPUS = 2
# 宿主机至少要剩这么多内存给虚拟机
HOST_RAM_HEADROOM_GB = 2.0
# 端口：WebUI 与 SSH 都只映射到 127.0.0.1，宿主外部访问不到
WEBUI_PORT = 6800
SSH_PORT = 2222


@dataclass
class EnvPlan:
    name: str
    workdir: str = ""
    vdi: str = ""
    ram_mb: int = RAM_MB
    cpus: int = CPUS
    disk_limit_gb: float = DISK_LIMIT_GB
    webui_port: int = WEBUI_PORT
    ssh_port: int = SSH_PORT
    user: str = "arklink"
    ok: bool = False
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    drive: Drive | None = None

    @property
    def expect_gb(self) -> float:
        """装完的预期真实占用（不是 8GB 上限）。"""
        return EXPECT_GB

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["drive"] = asdict(self.drive) if self.drive else None
        data["expect_gb"] = EXPECT_GB
        return data


def choose_drive(
    drives: tuple[Drive, ...],
    *,
    need_gb: float = DISK_LIMIT_GB,
    reserve_gb: float = RESERVE_GB,
) -> Drive | None:
    """挑一块装得下、且装完还剩得下余量的盘。

    优先非系统盘：虚拟机镜像放 C: 迟早出事（系统盘一满，Windows 会很难看）。
    非系统盘里挑余量最大的那块，别再纠结。
    """
    def fits(d: Drive) -> bool:
        return d.free_gb >= need_gb + reserve_gb

    others = [d for d in drives if not d.is_system and fits(d)]
    if others:
        return max(others, key=lambda d: d.free_gb)
    system = [d for d in drives if d.is_system and fits(d)]
    if system:
        return max(system, key=lambda d: d.free_gb)
    return None


def plan(report: HostReport, *, name: str = "arklink") -> EnvPlan:
    result = EnvPlan(name=name, ram_mb=RAM_MB, cpus=CPUS)

    if not report.has_vbox:
        result.problems.append(
            "没找到 VBoxManage。装一个 VirtualBox 7.x 再回来"
            "（Windows: https://www.virtualbox.org/wiki/Downloads ，"
            "Linux: apt install virtualbox）。注意：Docker/WSL2 那条路要开 Hyper-V 并重启，不走这里。"
        )

    drive = choose_drive(report.drives)
    if drive is None:
        detail = "、".join(f"{d.path} 剩 {d.free_gb:.1f}GB" for d in report.drives) or "无可用盘"
        result.problems.append(
            f"没有盘能同时满足「建 {DISK_LIMIT_GB:.0f}GB 虚拟盘 + 留 {RESERVE_GB:.0f}GB 余量」：{detail}"
        )
    else:
        result.drive = drive
        sep = "\\" if drive.path.endswith("\\") else "/"
        base = drive.path.rstrip("\\/")
        result.workdir = f"{base}{sep}{name}"
        result.vdi = f"{result.workdir}{sep}{name}.vdi"
        if drive.is_system:
            result.notes.append(
                f"只有系统盘 {drive.path} 放得下，镜像会落在 C: —— 建议先清点空间再决定"
            )

    if report.ram_gb and report.ram_gb - RAM_MB / 1024 < HOST_RAM_HEADROOM_GB:
        result.problems.append(
            f"物理内存只有 {report.ram_gb:.1f}GB，分出 {RAM_MB}MB 给虚拟机会太紧"
            f"（至少留 {HOST_RAM_HEADROOM_GB:.0f}GB 给宿主）"
        )
    elif report.ram_gb:
        result.notes.append(
            f"物理内存 {report.ram_gb:.1f}GB，虚拟机 {RAM_MB}MB，装完宿主还剩 "
            f"{report.ram_gb - RAM_MB / 1024:.1f}GB"
        )

    result.ok = not result.problems
    if result.ok:
        result.notes.append(
            f"磁盘上限 {DISK_LIMIT_GB:.0f}GB（动态分配），实际预期 {EXPECT_GB:.1f}GB；"
            f"装完目标盘还剩 {drive.free_gb - EXPECT_GB:.1f}GB"
        )
    return result
