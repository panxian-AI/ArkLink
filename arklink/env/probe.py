"""宿主能力探测：有没有 VBoxManage、哪块盘放得下、内存多少。

纯只读——**不执行任何虚拟化命令**，也不下载任何东西。装/删在 `vbox.py` 里，
必须显式调用。这样 `arklink env plan` 可以放心在别人机器上跑。
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class Drive:
    """一块本地盘。`path` 是可直接拼路径的根（Windows 带反斜杠，POSIX 是 "/"）。"""

    path: str
    free_gb: float
    total_gb: float

    @property
    def is_system(self) -> bool:
        if os.name == "nt":
            return self.path.upper().startswith(os.environ.get("SystemDrive", "C:").upper())
        return self.path == "/"


@dataclass(frozen=True)
class HostReport:
    platform: str
    vboxmanage: str
    ram_gb: float
    drives: tuple[Drive, ...]

    @property
    def has_vbox(self) -> bool:
        return bool(self.vboxmanage)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["drives"] = [asdict(d) | {"is_system": d.is_system} for d in self.drives]
        data["has_vbox"] = self.has_vbox
        return data


# VirtualBox 的默认安装位置就那么几个，别去扫全盘
_WIN_CANDIDATES = (
    r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    r"C:\Program Files (x86)\Oracle\VirtualBox\VBoxManage.exe",
)
_POSIX_CANDIDATES = (
    "/usr/bin/VBoxManage",
    "/usr/local/bin/VBoxManage",
    "/opt/VirtualBox/VBoxManage",
)


def find_vboxmanage() -> str:
    """返回 VBoxManage 的可执行路径，找不到返回空串。"""
    found = shutil.which("VBoxManage") or shutil.which("vboxmanage")
    if found:
        return found
    for cand in _WIN_CANDIDATES if os.name == "nt" else _POSIX_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return ""


def _ram_gb() -> float:
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes as wt

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wt.DWORD),
                ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return round(st.ullTotalPhys / 1024**3, 1)
        return 0.0
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / 1024**3, 1)
    except (ValueError, OSError, AttributeError):
        return 0.0


def list_drives() -> tuple[Drive, ...]:
    """列出可用的本地盘。Windows 逐个盘符试，POSIX 只看 /。"""
    roots: list[str] = []
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if os.path.exists(root):
                roots.append(root)
    else:
        roots.append("/")

    drives: list[Drive] = []
    for root in roots:
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        drives.append(
            Drive(root, round(usage.free / 1024**3, 1), round(usage.total / 1024**3, 1))
        )
    return tuple(drives)


def introspect() -> HostReport:
    return HostReport(
        platform=f"{sys.platform}",
        vboxmanage=find_vboxmanage(),
        ram_gb=_ram_gb(),
        drives=list_drives(),
    )


if __name__ == "__main__":  # 手工看一眼
    import json

    print(json.dumps(introspect().as_dict(), ensure_ascii=False, indent=2))
