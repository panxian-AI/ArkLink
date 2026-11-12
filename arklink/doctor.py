"""迁移后的体检：把「用户操作层面」的检查尽量自动化。

设计原则：

- **不猜**。能测的就测（进程在不在、管道通不通、端口能不能起、鉴权对不对），
  测不了的标 SKIP 并说明原因（QQ 没登录、平台不支持注入）；
- **不改状态**。只读探测，不注入、不发消息、不改配置；
- **给下一步**。每个 PROBLEM 都带一条可执行的建议，而不是只报错。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, hint: str = "") -> None:
        self.checks.append(Check(name, status, detail, hint))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def render(self) -> str:
        width = max((len(c.name) for c in self.checks), default=8)
        lines = [f"{'检查项'.ljust(width)}  状态   说明"]
        marks = {OK: "✓", WARN: "!", FAIL: "×", SKIP: "-"}
        for check in self.checks:
            lines.append(
                f"{check.name.ljust(width)}  {marks.get(check.status, '?')} {check.status:<5} {check.detail}"
            )
            if check.hint and check.status in (FAIL, WARN):
                lines.append(f"{' ' * width}         → {check.hint}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 单项检查
def check_python(report: Report) -> None:
    version = sys.version_info
    ok = version >= (3, 11)
    report.add(
        "Python",
        OK if ok else FAIL,
        f"{version.major}.{version.minor}.{version.micro}",
        "" if ok else "ArkBot / ArkLink 都要求 3.11+",
    )


def check_platform(report: Report) -> None:
    system = platform.system()
    detail = f"{system} {platform.machine()}"
    if system == "Windows":
        report.add("平台", OK, detail + "（hook 需要管理员权限才能注入提升进程）")
    elif system == "Linux":
        detail += "（hook 需要 ptrace 权限）"
        report.add("平台", OK, detail)
    else:
        report.add("平台", WARN, detail, "macOS / 其他平台的注入未实现，只能用 milky / satori")
    if sys.version_info >= (3, 11):
        try:
            import tomllib  # noqa: F401
        except Exception:  # pragma: no cover
            report.add("tomllib", WARN, "不可用", "配置文件解析会退化成正则，建议升级 Python")


def check_versions(report: Report, offline: bool = False) -> None:
    from . import update

    current = update.current_version()
    arkbot_version = update.project_version("arkbot")
    detail = f"ArkLink {current}" + (f" / ArkBot {arkbot_version}" if arkbot_version else " / 未装 ArkBot")
    report.add("版本", OK, detail)

    if offline or not update.update_check_enabled():
        report.add("新版本", SKIP, "已关闭或 --offline")
        return
    status = update.check_update()
    if not status.checked:
        report.add("新版本", SKIP, status.reason)
    elif status.available:
        report.add("新版本", WARN, f"有 {status.latest}", f"arklink update --to {status.latest}")
    else:
        report.add("新版本", OK, f"已是最新（{status.latest}）")


def check_config(report: Report, path: str = "bot.toml") -> list[str]:
    if not os.path.isfile(path):
        report.add("配置文件", SKIP, f"{path} 不存在", "用 arklink serve onebot 时可以不写配置")
        return []
    from . import registry

    adapters: list[str] = []
    try:
        import tomllib

        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        for item in data.get("adapters") or []:
            if isinstance(item, dict) and item.get("type"):
                adapters.append(str(item["type"]))
    except Exception as exc:
        report.add("配置文件", WARN, f"{path} 解析失败：{exc}", "检查 TOML 语法")
        return adapters

    known = set(registry.available())
    unknown = [name for name in adapters if name not in known and ":" not in name]
    if unknown:
        report.add(
            "适配器声明",
            FAIL,
            f"未知类型：{', '.join(unknown)}",
            f"可选：{', '.join(sorted(known))}，或写成 'pkg.mod:Class'",
        )
    else:
        report.add("适配器声明", OK, f"{len(adapters)} 个" + (f"：{', '.join(adapters)}" if adapters else ""))
    return adapters


def check_hook_module(report: Report) -> str:
    from .ntqq import hookproc

    path = hookproc.default_hook_dll()
    if not path:
        expected = "hookdll/build/arklink-hook-x64.dll" if os.name == "nt" else "hookdll/build/libarklink-hook-x64.so"
        report.add(
            "hook 模块",
            WARN,
            "没找到",
            f"python tools/build_hook.py 编译出 {expected}，或用 ARKLINK_HOOK_DLL 指定",
        )
        return ""
    size = os.path.getsize(path)
    report.add("hook 模块", OK, f"{os.path.basename(path)}（{size // 1024} KB）")
    return path


def check_processes(report: Report) -> tuple[int, str]:
    from .ntqq import hookproc

    try:
        processes = hookproc.find_processes()
    except Exception as exc:
        report.add("QQ 进程", FAIL, f"枚举失败：{exc}")
        return 0, ""
    if not processes:
        report.add("QQ 进程", SKIP, "没有 QQ 在跑", "先手动打开并登录官方 QQ 客户端")
        return 0, ""
    main = hookproc.pick_main(processes)
    if main is None:  # pragma: no cover - pick_main 兜底返回第一个
        report.add("QQ 进程", WARN, f"{len(processes)} 个进程，但没挑出主进程")
        return 0, ""
    report.add(
        "QQ 进程",
        OK,
        f"{len(processes)} 个 QQ 进程，主进程 pid={main.pid}",
    )
    return main.pid, main.pipe


def check_pipe(report: Report, pid: int) -> str:
    if not pid:
        report.add("hook 管道", SKIP, "没有目标进程")
        return ""
    from .ntqq import hookproc

    scanner = hookproc.ProcessScanner()
    state = scanner.probe_pipe(pid)
    name = scanner.pipe_name(pid)
    if state == "free":
        report.add("hook 管道", OK, f"{name} 可连接")
        return name
    if state == "busy":
        report.add("hook 管道", WARN, f"{name} 被占用", "别的客户端（如 SnowLuma）还连着，先退掉它")
        return name
    report.add("hook 管道", WARN, "不存在", "arklink hook --install 注入；不注入就只能用别的通道")
    return ""


def check_onebot_server(report: Report, token: str = "doctor") -> None:
    """真起一个服务端再打自己一下：验证端口、鉴权、JSON 形状。"""
    from .onebot import OneBot11Server

    class _Null:
        name = "doctor-probe"
        max_message_length = 1000
        is_connected = True

        def on_raw_event(self, handler: Any) -> None:
            pass

        async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
            if action in ("get_status",):
                return {"online": True, "good": True}
            raise RuntimeError(f"doctor 不支持 {action}")

    async def probe() -> tuple[bool, str]:
        server = OneBot11Server([_Null()], host="127.0.0.1", port=0, access_token=token, heartbeat=0)  # type: ignore[list-item]
        await server.start()
        port = server.port
        try:
            def fetch(path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
                request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, json.loads(response.read().decode())
                except urllib.error.HTTPError as exc:
                    return exc.code, {"retcode": exc.code}

            code, body = await asyncio.to_thread(fetch, "/", {"Authorization": f"Bearer {token}"})
            if code != 200 or body.get("status") != "ok":
                return False, f"带 token 请求失败：HTTP {code}"
            code_bad, _ = await asyncio.to_thread(fetch, "/")
            if code_bad != 401:
                return False, f"没带 token 竟然通了（HTTP {code_bad}），鉴权失效"
            return True, f"端口 {port} 可监听，鉴权生效，返回 OneBot 形状"
        finally:
            await server.stop()

    try:
        ok, detail = asyncio.run(probe())
    except Exception as exc:
        report.add("OneBot 服务端", FAIL, f"起不来：{exc}", "看是不是端口被占用或防火墙拦截")
        return
    report.add("OneBot 服务端", OK if ok else FAIL, detail)


def check_port(report: Report, host: str = "127.0.0.1", port: int = 6700) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    busy = sock.connect_ex((host, port)) == 0
    sock.close()
    if busy:
        report.add("端口 6700", WARN, "已被占用", "换端口 --port 6701，或先停掉占用的进程")
    else:
        report.add("端口 6700", OK, "可绑定")


# ---------------------------------------------------------------- 汇总
def run_checks(
    config: str = "bot.toml",
    offline: bool = False,
    with_server: bool = True,
) -> Report:
    report = Report()
    check_python(report)
    check_platform(report)
    check_versions(report, offline=offline)
    check_config(report, config)
    check_hook_module(report)
    pid, _pipe = check_processes(report)
    check_pipe(report, pid)
    if with_server:
        check_onebot_server(report)
        check_port(report)
    return report


def summary(report: Report) -> str:
    total = len(report.checks)
    skipped = sum(1 for c in report.checks if c.status == SKIP)
    return (
        f"共 {total} 项：通过 {sum(1 for c in report.checks if c.status == OK)}，"
        f"注意 {len(report.warned)}，失败 {len(report.failed)}，跳过 {skipped}"
    )
