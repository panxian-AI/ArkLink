"""编译 ArkLink 的 hook DLL（x64，用 mingw，不需要装 Visual Studio）。

    python tools/build_hook.py            # 自动找编译器
    python tools/build_hook.py --cc "D:/mingw64/bin/x86_64-w64-mingw32-g++.exe"

产物：hookdll/build/arklink-hook-x64.dll
编译完会顺手把导出表打出来，确认 ArkLinkPrepareUnload 在（卸载依赖它）。
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
SRC = ROOT / "hookdll" / "arklink_hook.cpp"
OUT_DIR = ROOT / "hookdll" / "build"
OUT = OUT_DIR / "arklink-hook-x64.dll"

CANDIDATES = (
    "x86_64-w64-mingw32-g++",
    "g++",
    r"D:/mingw64/bin/x86_64-w64-mingw32-g++.exe",
    r"C:/msys64/mingw64/bin/x86_64-w64-mingw32-g++.exe",
    r"C:/ProgramData/mingw64/mingw64/bin/x86_64-w64-mingw32-g++.exe",
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
    raise SystemExit("找不到 mingw 的 g++。装一个（scoop install mingw）或用 --cc 指定路径。")


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


def main() -> int:
    parser = argparse.ArgumentParser(description="编译 ArkLink hook DLL")
    parser.add_argument("--cc", default="", help="g++ 路径")
    parser.add_argument("--debug", action="store_true", help="带调试符号，不优化")
    args = parser.parse_args()

    if not SRC.is_file():
        raise SystemExit(f"源码不存在：{SRC}")
    compiler = find_compiler(args.cc)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        compiler,
        "-shared",
        "-std=c++17",
        "-DUNICODE",
        "-D_UNICODE",
        "-Wall",
        "-Wextra",
        "-o",
        str(OUT),
        str(SRC),
        "-lshell32",
        "-lole32",
        "-static-libgcc",
        "-static-libstdc++",
    ]
    if args.debug:
        cmd += ["-g", "-O0"]
    else:
        cmd += ["-O2", "-s"]
    # -static-libgcc/-static-libstdc++ 已经足够：只剩 kernel32/shell32 这类系统 DLL
    cmd.append("-static")

    print(" ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return proc.returncode

    size = OUT.stat().st_size
    names = exports_of(OUT)
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
