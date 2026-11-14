"""一键部署：把「跑 QQ 的这台机器」准备好。

两个落点，同一套开关：

* **本机虚拟机**（不给地址）——`arklink env setup` 那条路：VirtualBox +
  无人值守装 Ubuntu + Xvfb + QQ，全程不动你的桌面；
* **远程 Linux 服务器**（给地址）——打包本地源码、scp 上去、跑
  `tools/setup_server.sh`：裸机上直接跑，不需要虚拟机。

之所以单独做一层，是为了让 `deploy.bat`（双击那个）只负责问一句地址，
剩下的事情都有可测试的 Python 实现兜着。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

#: 打包时永不带上这些东西：体积大、且目标机器会自己生成
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", "dist", "build", ".idea", ".vscode", ".workbuddy",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".tgz", ".vdi", ".iso"}
#: 部署到服务器的固定目录（和文档一致，服务单元里也写死这个路径）
REMOTE_DIR = "/opt/ArkLink"


@dataclass
class Target:
    """部署落点。`host` 为空 = 本机虚拟机。"""

    raw: str = ""

    @property
    def host(self) -> str:
        return self.raw.strip()

    @property
    def is_local_vm(self) -> bool:
        h = self.host.lower()
        return h in ("", "local", "vm", "本机", "虚拟机", "127.0.0.1", "localhost")

    @property
    def ssh_target(self) -> str:
        """把 `ip` / `ip:端口` / `user@ip:端口` 归一成 ssh 认识的写法。"""
        h = self.host
        if self.is_local_vm:
            return ""
        user = ""
        if "@" in h:
            user, h = h.split("@", 1)
            user += "@"
        port = ""
        if h.count(":") == 1:  # 只处理 ip:port，不碰 IPv6
            h, port = h.split(":", 1)
        if port:
            return f"-p {port} {user}{h}"
        return f"{user}{h}"


def parse_target(argv: list[str]) -> Target:
    """`arklink deploy user@1.2.3.4:2222` 也能写 `arklink deploy --host ...`。"""
    for i, a in enumerate(argv):
        if a in ("--host", "-H") and i + 1 < len(argv):
            return Target(argv[i + 1])
        if not a.startswith("-"):
            return Target(a)
    return Target("")


def pack_source(root: Path | str, dest: Path | str) -> Path:
    """把仓库打成 tar.gz（跳过 .git / 虚拟环境 / 缓存 / 数据）。"""
    root = Path(root).resolve()
    dest = Path(dest)
    with tarfile.open(dest, "w:gz") as tar:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if path.is_dir() or path.suffix in EXCLUDE_SUFFIXES:
                continue
            # 数据库/日志这类运行时产物不跟着走，避免把本机数据带到服务器
            if rel.parts and rel.parts[0] in ("data", "logs"):
                continue
            tar.add(path, arcname=str(Path(root.name) / rel))
    return dest


def _run(cmd: list[str], *, dry_run: bool = False) -> int:
    printable = " ".join(cmd)
    print(f"[deploy] $ {printable}", flush=True)
    if dry_run:
        return 0
    return subprocess.call(cmd)


def deploy_remote(
    target: Target,
    *,
    source: Path | str = ".",
    remote_dir: str = REMOTE_DIR,
    dry_run: bool = False,
) -> int:
    """远程服务器：打包 → scp → ssh 解包并跑 setup_server.sh。"""
    if not shutil.which("ssh") or not shutil.which("scp"):
        print("[deploy] 本机没有 ssh/scp：Windows 装 OpenSSH 客户端，"
              "或在「设置→应用→可选功能」里加装", file=sys.stderr)
        return 2
    host = target.ssh_target
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp = Path(os.environ.get("TEMP") or "/tmp") / f"arklink-{stamp}.tgz"
    print("[deploy] 打包源码…", flush=True)
    pack_source(source, tmp)
    size_mb = tmp.stat().st_size / 1024**2
    print(f"[deploy] 包大小 {size_mb:.1f}MB → 传到 {host}", flush=True)
    remote_pkg = f"/tmp/arklink-{stamp}.tgz"
    # ssh 的 host 部分带 -p 时不能直接拼，拆开用
    parts = host.split()
    port_args = parts[:-1] if len(parts) > 1 else []
    hostname = parts[-1]

    try:
        rc = _run(["scp", *port_args, str(tmp), f"{hostname}:{remote_pkg}"], dry_run=dry_run)
        if rc != 0:
            return rc
        # 一条命令搞定：解包 → 跑安装脚本（-t 让 sudo 能要密码）
        remote_cmd = (
            f"set -e; sudo mkdir -p {remote_dir}; "
            f"sudo tar xzf {remote_pkg} -C {remote_dir} --strip-components=1; "
            f"sudo bash {remote_dir}/tools/setup_server.sh --source {remote_dir}"
        )
        rc = _run(
            ["ssh", *port_args, "-t", hostname, remote_cmd],
            dry_run=dry_run,
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if rc == 0:
        print(f"\n[deploy] 完成：浏览器打开 http://{hostname.split('@')[-1]}:6800/")
        print("[deploy] 首次需要扫码登录（WebUI 首页），登录态会自动备份")
    return rc


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    target = parse_target(argv)
    if target.is_local_vm:
        from .__main__ import main as cli_main

        print("[deploy] 落点：本机虚拟机（VirtualBox + Ubuntu + QQ，全自动）", flush=True)
        rest = ["env", "setup", "--source", "."] + (["--dry-run"] if dry_run else [])
        return cli_main(rest)
    print(f"[deploy] 落点：远程服务器 {target.raw}", flush=True)
    return deploy_remote(target, dry_run=dry_run)
