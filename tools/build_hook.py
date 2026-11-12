"""编译 ArkLink 的 hook 模块（Windows 是 DLL，Linux/macOS 是 .so）。

    python tools/build_hook.py                     # 自动找编译器
    python tools/build_hook.py --cc g++-13         # 指定编译器

产物：
    Windows  hookdll/build/arklink-hook-x64.dll   （mingw-w64）
    POSIX    hookdll/build/libarklink-hook-x64.so （g++/clang++）

编译完会顺手把导出表打出来，确认 ArkLinkPrepareUnload 在（卸载依赖它，
缺了它 dlclose/FreeLibrary 时工作线程会跳进已释放内存，直接把 QQ 带崩）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"
SRC = ROOT / "hookdll" / ("arklink_hook.cpp" if IS_WINDOWS else "arklink_hook_posix.cpp")
OUT_DIR = ROOT / "hookdll" / "build"
OUT = OUT_DIR / ("arklink-hook-x64.dll" if IS_WINDOWS else "libarklink-hook-x64.so")

CANDIDATES = (
    ("x86_64-w64-mingw32-g++", "g++", r"D:/mingw64/bin/x86_64-w64-mingw32-g++.exe",
     r"C:/msys64/mingw64/bin/x86_64-w64-mingw32-g++.exe",
     r"C:/ProgramData/mingw64/mingw64/bin/x86_64-w64-mingw32-g++.exe")
    if IS_WINDOWS
    else ("g++", "c++", "clang++")
)


def find_compiler(explicit: str = "") -> str:
    if explicit:
        if not Path(explicit).is_file():
            raise SystemExit(f"指定的编译器不存在：{explicit}")
        return explicit
    for candidate in CANDIDATES:
        found = shutil.which(candidate) if os.sep not in candidate else (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    hint = "装一个 mingw（scoop install mingw）" if IS_WINDOWS else "装一个 g++（apt install build-essential）"
    raise SystemExit(f"找不到 C++ 编译器。{hint}，或用 --cc 指定路径。")


def exports_of(path: Path) -> list[str]:
    """极简 PE 导出表解析：只为了在 CI 里断言导出名。"""
    data = path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        raise SystemExit("不是有效的 PE 文件")
    nsections = struct.unpack_from("<H", data, pe + 6)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    exp_rva = struct.unpack_from("<I", data, dd)[0]
    if not exp_rva:
        return []
    sections = []
    for i in range(nsections):
        off = opt + struct.unpack_from("<H", data, pe + 20)[0] + i * 40
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((vaddr, max(vsize, rawsize), rawptr))

    def rva_to_off(rva: int) -> int | None:
        for vaddr, size, rawptr in sections:
            if vaddr <= rva < vaddr + size:
                return rawptr + (rva - vaddr)
        return None

    exp = rva_to_off(exp_rva)
    if exp is None:
        return []
    count = struct.unpack_from("<I", data, exp + 24)[0]
    names_rva = struct.unpack_from("<I", data, exp + 32)[0]
    names = rva_to_off(names_rva)
    result: list[str] = []
    for i in range(count):
        entry = rva_to_off(struct.unpack_from("<I", data, names + i * 4)[0])
        if entry is None:
            continue
        end = data.index(b"\0", entry)
        result.append(data[entry:end].decode("ascii", "replace"))
    return result


def posix_exports(path: Path) -> list[str]:
    """用 nm 读动态导出（等价于 Windows 侧解析 PE 导出表）。"""
    try:
        text = subprocess.run(
            ["nm", "-D", "--defined-only", str(path)], capture_output=True, text=True
        ).stdout
    except OSError:
        return []
    names: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] in ("T", "t", "W"):
            names.append(parts[2])
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="编译 ArkLink hook DLL")
    parser.add_argument("--cc", default="", help="g++ 路径")
    parser.add_argument("--debug", action="store_true", help="带调试符号，不优化")
    args = parser.parse_args()

    if not SRC.is_file():
        raise SystemExit(f"源码不存在：{SRC}")
    compiler = find_compiler(args.cc)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if IS_WINDOWS:
        cmd = [
            compiler, "-shared", "-std=c++17", "-DUNICODE", "-D_UNICODE",
            "-Wall", "-Wextra", "-o", str(OUT), str(SRC),
            "-lshell32", "-lole32", "-static-libgcc", "-static-libstdc++",
        ]
        cmd += ["-g", "-O0"] if args.debug else ["-O2", "-s"]
        cmd.append("-static")
    else:
        cmd = [
            compiler, "-shared", "-fPIC", "-std=c++17", "-Wall", "-Wextra",
            # 静态链 libstdc++：QQ 是 Electron，不保证系统里那份 libstdc++ 版本够新
            "-static-libstdc++", "-static-libgcc", "-pthread", "-o", str(OUT), str(SRC),
        ]
        cmd += ["-g", "-O0"] if args.debug else ["-O2"]

    print(" ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return proc.returncode

    size = OUT.stat().st_size
    names = exports_of(OUT) if IS_WINDOWS else posix_exports(OUT)
    print(f"已生成 {OUT}（{size} 字节）")
    print(f"导出：{', '.join(sorted(names)) or '(无)'}")
    required = {"ArkLinkPrepareUnload", "ArkLinkHookVersion"}
    missing = required - set(names)
    if missing:
        print(f"缺少导出：{', '.join(sorted(missing))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
