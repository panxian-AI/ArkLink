"""内置虚拟环境：`arklink env` 的全部实现。

四条命令覆盖生命周期，用户不需要打开 VirtualBox 界面：

    arklink env plan      # 算账：能不能装、装哪块盘、占多少
    arklink env create    # 建 VM + 无人值守装 Ubuntu + 无头启动
    arklink env push      # 把本仓库推进 guest 并跑 tools/setup_guest.sh
    arklink env up/down/status/destroy

设计原则：
* **不偷偷下载 2.6GB**：默认只打印下载地址与校验和，加 `--download` 才下；
* **不偷偷删东西**：`destroy` 必须 `--yes`；
* 端口只绑 127.0.0.1，宿主外部访问不到；
* 状态落 `~/.arklink/env.json`（POSIX 下 0600），下次直接复用。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .plan import EnvPlan, plan as build_plan
from .probe import HostReport, introspect
from .vbox import (
    VBM,
    VBox,
    VBoxError,
    copyto_commands,
    create_commands,
    eject_commands,
    guest_run_commands,
    unattended_commands,
)

STATE_NAME = "env.json"
# 用 SHA256SUMS 解析当前最新小版本，避免硬编码 24.04.1/24.04.3 这种会过期的名字
RELEASES_BASE = "https://releases.ubuntu.com/24.04"
SETUP_SCRIPT = "tools/setup_guest.sh"


def state_path() -> Path:
    home = os.environ.get("ARKLINK_HOME") or os.path.join(os.path.expanduser("~"), ".arklink")
    return Path(home) / STATE_NAME


# ---------------------------------------------------------------- ISO
@dataclass
class IsoRef:
    name: str
    url: str
    sha256: str = ""

    @property
    def version(self) -> tuple[int, ...]:
        m = re.search(r"ubuntu-(\d+\.\d+(?:\.\d+)?)-live-server", self.name)
        return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def resolve_iso(*, timeout: float = 20.0, opener=None) -> IsoRef:
    """从官方 SHA256SUMS 里找当前最新的 24.04 server ISO（同时拿到校验和）。"""
    open_fn = opener or urllib.request.urlopen
    with open_fn(f"{RELEASES_BASE}/SHA256SUMS", timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")
    found: list[IsoRef] = []
    for line in text.splitlines():
        m = re.match(r"^([0-9a-f]{64})\s+\*?(ubuntu-[\d.]+-live-server-amd64\.iso)$", line.strip())
        if m:
            found.append(IsoRef(m.group(2), f"{RELEASES_BASE}/{m.group(2)}", m.group(1)))
    if not found:
        raise RuntimeError("没能从 releases.ubuntu.com 解析出 server ISO，请用 --iso 手动指定")
    return max(found, key=lambda r: r.version)


def sha256_of(path: str, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            block = fp.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def download_iso(iso: IsoRef, dest: str, *, opener=None, log=print) -> str:
    """下载并校验。校验和来自官方 SHA256SUMS，不是我们自己算的。"""
    open_fn = opener or urllib.request.urlopen
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    tmp = dest + ".part"
    with open_fn(iso.url, timeout=60) as resp, open(tmp, "wb") as fp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            fp.write(block)
            done += len(block)
            now = time.monotonic()
            if total and now - last > 1.0:
                last = now
                log(f"  下载 {done / 1024**2:.0f}/{total / 1024**2:.0f} MB "
                    f"({done * 100 // total}%)")
    if iso.sha256:
        got = sha256_of(tmp)
        if got != iso.sha256:
            os.unlink(tmp)
            raise RuntimeError(f"ISO 校验失败：期望 {iso.sha256[:16]}… 实际 {got[:16]}…")
    os.replace(tmp, dest)
    return dest


def find_local_iso(dirs: Sequence[str] | None = None) -> str:
    """在常见位置找已有的 24.04 server ISO，省一次 2.6GB 下载。"""
    dirs = dirs or [os.getcwd(), os.path.expanduser("~/Downloads"),
                    os.path.expanduser("~/下载"), "/tmp"]
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if re.match(r"^ubuntu-[\d.]+-live-server-amd64\.iso$", name):
                return os.path.join(d, name)
    return ""


# ---------------------------------------------------------------- 状态
@dataclass
class EnvState:
    name: str = "arklink"
    workdir: str = ""
    vdi: str = ""
    user: str = "arklink"
    webui_port: int = 6800
    ssh_port: int = 2222
    iso: str = ""
    installed: bool = False
    created_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "EnvState | None":
        p = path or state_path()
        try:
            with open(p, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            return None
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path | None = None) -> Path:
        p = path or state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(asdict(self), fp, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        if os.name != "nt":
            os.chmod(p, 0o600)  # 里面有 guest 用户名，别给全机读
        return p

    @classmethod
    def from_plan(cls, plan: EnvPlan, *, iso: str = "") -> "EnvState":
        return cls(
            name=plan.name, workdir=plan.workdir, vdi=plan.vdi, user=plan.user,
            webui_port=plan.webui_port, ssh_port=plan.ssh_port, iso=iso,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )


# ---------------------------------------------------------------- 编排
@dataclass
class EnvManager:
    """一次操作要用到的全部东西：宿主报告、计划、VBox 句柄、状态。"""

    report: HostReport
    plan: EnvPlan
    box: VBox | None
    state: EnvState
    dry_run: bool = False

    @classmethod
    def open(
        cls,
        name: str = "arklink",
        *,
        report: HostReport | None = None,
        runner=None,
        dry_run: bool = False,
    ) -> "EnvManager":
        report = report or introspect()
        plan = build_plan(report, name=name)
        # 没装 VirtualBox 也要能跑 `env plan`——那正是用来告诉他去装的命令
        try:
            box: VBox | None = VBox(report.vboxmanage, runner=runner)
        except VBoxError:
            box = None
        state = EnvState.load() or EnvState.from_plan(plan)
        if state.name != name:
            state = EnvState.from_plan(plan)
        return cls(report=report, plan=plan, box=box, state=state, dry_run=dry_run)

    def require_box(self) -> VBox:
        if self.box is None:
            raise VBoxError(
                [VBM], "未安装 VirtualBox（找不到 VBoxManage）；先装它，再跑 arklink env plan", 127
            )
        return self.box

    # ------------------------------------------------------------ 创建
    def commands_for_create(
        self, iso: str, *, user: str, password: str, post_install: str = ""
    ) -> list[list[str]]:
        plan = self.plan
        plan.user = user
        cmds = create_commands(plan)
        cmds += unattended_commands(
            plan, iso,
            user=user, password=password,
            additions_iso=self.box.guest_additions_iso() if self.box else "",
            post_install=post_install,
        )
        return cmds

    def create(
        self, iso: str, *, user: str, password: str, post_install: str = "",
        force: bool = False, on_step=None,
    ) -> list[list[str]]:
        if not self.plan.ok and not self.dry_run:
            raise RuntimeError("；".join(self.plan.problems))
        if not os.path.isfile(iso):
            raise FileNotFoundError(f"ISO 不存在：{iso}")
        if not self.dry_run and self.require_box().exists(self.plan.name):
            if not force:
                raise RuntimeError(
                    f"虚拟机 {self.plan.name} 已存在，直接用 `arklink env up`；要重建加 --force"
                )
            self.require_box().destroy(self.plan.name)
        cmds = self.commands_for_create(
            iso, user=user, password=password, post_install=post_install
        )
        labels = ["建虚拟机", "配置硬件/网络", "建虚拟磁盘", "挂 SATA 控制器", "挂硬盘",
                  "端口映射（只绑 127.0.0.1）", "无人值守安装（约 10–20 分钟）"]
        if not self.dry_run:
            os.makedirs(self.plan.workdir, exist_ok=True)
        for index, cmd in enumerate(cmds):
            label = labels[index] if index < len(labels) else f"步骤{index + 1}"
            if on_step:
                on_step(index, len(cmds), label, cmd)
            if not self.dry_run:
                self.require_box().run(cmd, timeout=3600 if "unattended" in cmd else 900)
        if not self.dry_run:
            self.state = EnvState.from_plan(self.plan, iso=iso)
            self.state.user = user
            self.state.extra["password"] = password
            self.state.save()
        return cmds

    # ------------------------------------------------------------ 生命周期
    def up(self) -> str:
        if self.dry_run:
            return "dry-run"
        return self.require_box().start(self.plan.name)

    def down(self, *, force: bool = False) -> str:
        if self.dry_run:
            return "dry-run"
        return self.require_box().stop(self.plan.name, force=force)

    def eject(self) -> str:
        if self.dry_run:
            return "dry-run"
        out = "\n".join(self.require_box().apply(eject_commands(self.plan.name)))
        self.state.installed = True
        self.state.save()
        return out

    def destroy(self, *, confirm: bool = False) -> str:
        if not confirm:
            raise RuntimeError("删除虚拟机会连同虚拟磁盘一起删掉，确认请加 --yes")
        if self.dry_run:
            return "dry-run"
        out = self.require_box().destroy(self.plan.name)
        try:
            state_path().unlink()
        except OSError:
            pass
        return out

    def status(self) -> dict[str, Any]:
        name = self.plan.name
        box = self.require_box()
        data: dict[str, Any] = {
            "name": name,
            "exists": box.exists(name),
            "state": box.status(name),
            "workdir": self.plan.workdir,
            "vdi": self.plan.vdi,
            "disk_used_gb": box.disk_usage_gb(self.plan.vdi),
            "disk_limit_gb": self.plan.disk_limit_gb,
            "ram_mb": self.plan.ram_mb,
            "webui": f"http://127.0.0.1:{self.plan.webui_port}",
            "ssh": f"ssh -p {self.plan.ssh_port} {self.state.user}@127.0.0.1",
        }
        return data

    # ------------------------------------------------------------ 推代码
    def push(self, source: str, *, password: str, script: str = SETUP_SCRIPT) -> str:
        """把宿主仓库推进 guest 并执行自举脚本（guest 内会装 QQ/Xvfb/Arklink）。"""
        if self.dry_run:
            return "dry-run"
        name = self.plan.name
        target = "/opt/ArkLink"
        cmds = copyto_commands(name, os.path.abspath(source), target,
                              user=self.state.user, password=password)
        cmds += guest_run_commands(
            name,
            f"sudo bash {target}/{script} --source {target}",
            user=self.state.user, password=password,
        )
        return "\n".join(self.require_box().apply(cmds, timeout=3600))
