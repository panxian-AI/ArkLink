"""会话自检：QQ 和我，是不是在同一个输入队列里。

Windows 的键盘鼠标按**登录会话**分发。同一个会话里所有进程共用一条输入队列，
所以只要 QQ 和你的工作窗口同在 Session 1，「后台发送」就不可能——每次注入都得
先把 QQ 顶成前台窗口，也就是抢焦点、占剪贴板。

这个脚本只读不写，用来在部署完「另一个会话 / 另一个虚拟机」之后确认隔离真的生效。

用法：
    python tools/check_session.py
    python tools/check_session.py --json

退出码：0 = 隔离正常（或本机没有 QQ）；1 = QQ 与本进程同会话，会互相干扰。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys

if sys.platform != "win32":
    print("这个脚本只对 Windows 有意义：Linux 下每个 X 会话天然隔离，无需检查。")
    sys.exit(0)

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
UOI_NAME = 2


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]
    k.ProcessIdToSessionId.restype = wt.BOOL
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = wt.BOOL
    k.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.restype = wt.BOOL
    k.CloseHandle.argtypes = [wt.HANDLE]
    return k


def _user32():
    """窗口站 / 桌面 / UOI 都在 user32，不在 kernel32（踩过一次）。"""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetProcessWindowStation.restype = wt.HANDLE
    u.GetProcessWindowStation.argtypes = []
    u.GetThreadDesktop.restype = wt.HANDLE
    u.GetThreadDesktop.argtypes = [wt.DWORD]
    u.GetUserObjectInformationW.argtypes = [
        wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)
    ]
    u.GetUserObjectInformationW.restype = wt.BOOL
    return u


def session_of(k, pid: int) -> int | None:
    sid = wt.DWORD(0)
    if not k.ProcessIdToSessionId(pid, ctypes.byref(sid)):
        return None
    return int(sid.value)


def _object_name(u, handle) -> str:
    if not handle:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    need = wt.DWORD(0)
    if not u.GetUserObjectInformationW(
        handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(need)
    ):
        return ""
    return buf.value


def iter_processes(k):
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wt.HANDLE(-1).value:
        return
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            yield int(entry.th32ProcessID), entry.szExeFile
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检查 QQ 是否与当前进程在同一会话")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--names", default="QQ.exe,QQEX.exe", help="要检查的进程名，逗号分隔")
    args = ap.parse_args(argv)

    k = _kernel32()
    u = _user32()
    me = os.getpid()
    my_session = session_of(k, me)
    station = _object_name(u, u.GetProcessWindowStation())
    desktop = _object_name(u, u.GetThreadDesktop(k.GetCurrentThreadId()))
    wanted = {n.strip().lower() for n in args.names.split(",") if n.strip()}

    found: list[dict] = []
    for pid, name in iter_processes(k):
        if name.lower() in wanted:
            found.append({"pid": pid, "name": name, "session": session_of(k, pid)})
    found.sort(key=lambda r: (r["name"], r["pid"]))

    same = [r for r in found if r["session"] == my_session]
    other = [r for r in found if r["session"] is not None and r["session"] != my_session]
    isolated = bool(found) and not same

    report = {
        "me": {"pid": me, "session": my_session, "window_station": station, "desktop": desktop},
        "qq": found,
        "same_session": [r["pid"] for r in same],
        "isolated": isolated,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if (isolated or not found) else 1

    print(f"当前进程  pid={me}  session={my_session}  "
          f"窗口站={station or '?'}  桌面={desktop or '?'}")
    if not found:
        print("没有发现 QQ 进程（QQ.exe / QQEX.exe）。")
        print("结论：无需判断；启动 QQ 后再跑一次。")
        return 0

    # 按会话聚合：QQ 一次会拉起二十多个子进程，逐个列没有信息量
    groups: dict[int, dict[str, list[int]]] = {}
    for r in found:
        groups.setdefault(r["session"], {}).setdefault(r["name"], []).append(r["pid"])
    print(f"发现 {len(found)} 个 QQ 相关进程，分布在 {len(groups)} 个会话：")
    for sid, by_name in sorted(groups.items()):
        count = sum(len(v) for v in by_name.values())
        detail = " / ".join(f"{n}×{len(v)}" for n, v in sorted(by_name.items()))
        mark = "  ←与你同会话" if sid == my_session else ""
        print(f"  session {sid}: {count} 个（{detail}）{mark}")

    print()
    if isolated:
        print("✅ 隔离已生效：QQ 在别的会话里，注入不会碰你当前的键盘鼠标与剪贴板。")
        if other:
            print(f"   （QQ 会话：{sorted({r['session'] for r in other})}，"
                  f"本进程会话：{my_session}）")
        return 0

    print("❌ QQ 和你在同一个会话（session "
          f"{my_session}），共享同一条输入队列：")
    print("   · 每次发送都必须把 QQ 顶成前台窗口 → 抢焦点")
    print("   · 剪贴板是同会话共享的 → 会占用你的剪贴板")
    print("   解决：把 QQ 放进另一个会话 / 另一个虚拟机（见 docs/Windows会话隔离.md）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
