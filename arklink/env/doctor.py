"""`arklink env doctor` —— 虚拟环境体检 + 排障。

两层设计：
1. 规则层（零依赖、离线可用）：把真机部署踩过的每一个坑做成检查项，
   命中就给出「原因 + 可直接执行的修复命令」。
2. AI 层（可选）：规则没覆盖的疑难杂症，把体检报告发给 DeepSeek 分析，
   返回结构化的诊断和修复命令。API key 存 ~/.arklink/ai.json（0600）。

规则层的条目维护原则：每一条都必须是**真踩过的坑**，不许凭空猜测。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from . import EnvManager

AI_KEY_FILE = Path.home() / ".arklink" / "ai.json"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"


@dataclass
class Finding:
    """一条体检结论。ok=True 是健康项；否则给 cause 和 fix。"""

    check: str
    ok: bool
    detail: str = ""
    cause: str = ""
    #: 可直接执行的修复命令（guest 内 shell 或宿主 VBoxManage，按 ctx 区分）
    fix: list[str] = field(default_factory=list)
    #: fix 的执行位置：guest=虚拟机里跑，host=宿主机跑
    ctx: str = "guest"


def set_ai_key(key: str) -> Path:
    """把 AI key 落盘（0600），之后 doctor --ai 不用再传。"""
    AI_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    AI_KEY_FILE.write_text(json.dumps({"provider": "deepseek", "key": key}), encoding="utf-8")
    try:
        AI_KEY_FILE.chmod(0o600)
    except OSError:  # pragma: no cover - Windows 上 chmod 语义不同，不阻塞
        pass
    return AI_KEY_FILE


def load_ai_key() -> str:
    """优先级：环境变量 > 配置文件；没有返回空串。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key
    try:
        data = json.loads(AI_KEY_FILE.read_text(encoding="utf-8"))
        return str(data.get("key") or "")
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------- 体检
def collect_report(mgr: "EnvManager", *, password: str) -> dict[str, Any]:
    """宿主 + 虚拟机两侧的原始事实，AI 层也吃这个。"""
    from .vbox import VBoxError

    report: dict[str, Any] = {"vm": {}, "guest": {}, "ports": {}}
    try:
        report["vm"] = mgr.status()
    except VBoxError as exc:
        report["vm"] = {"error": str(exc)}
        return report

    name, user = mgr.plan.name, mgr.state.user
    probe = (
        "echo USER=$(id -un); "
        "echo DISK=$(df -h / | awk 'NR==2{print $5}' | tr -d '%'); "
        "echo SVC_BOT=$(systemctl is-active arklink.service 2>/dev/null); "
        "echo SVC_API=$(systemctl is-active arklink-api.service 2>/dev/null); "
        # 用 -x 精确匹配可执行名：-f 会匹配到命令行里带 /opt/QQ/qq 的 shell 自己
        "echo QQ_PROC=$( (pgrep -x qq; pgrep -f '^/opt/QQ/qq') 2>/dev/null | sort -u | wc -l); "
        "export DISPLAY=:99; "
        "echo QQ_WIN=$(xdotool search --name '^QQ$' 2>/dev/null | wc -l); "
        "echo CRASHES=$(ls $HOME/.config/QQ/crash_files 2>/dev/null | wc -l); "
        "echo LOGIN_BAK=$([ -f $HOME/qq-login.tgz ] && echo yes || echo no); "
        "echo QQLOG=$(tail -c 400 /tmp/qq.log 2>/dev/null | tr '\\n' ' ' | tail -c 200)"
    )
    try:
        cmds = guest_run_commands_safe(mgr, name, probe, user, password)
        out = "\n".join(mgr.require_box().apply(cmds, timeout=120))
        for line in out.splitlines():
            key, _, value = line.partition("=")
            if key in ("USER", "DISK", "SVC_BOT", "SVC_API", "QQ_PROC", "QQ_WIN",
                       "CRASHES", "LOGIN_BAK", "QQLOG"):
                report["guest"][key] = value.strip()
    except Exception as exc:  # SSH/guestcontrol 不通也要继续出报告
        report["guest"]["error"] = str(exc)

    for label, port in (("webui", mgr.plan.webui_port), ("api", mgr.plan.api_port)):
        report["ports"][label] = _probe_port(port)
    return report


def guest_run_commands_safe(mgr: "EnvManager", name: str, shell: str,
                            user: str, password: str) -> list[list[str]]:
    from .vbox import guest_run_commands

    return guest_run_commands(name, shell, user=user, password=password)


def _probe_port(port: int) -> str:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return "open"
    except OSError:
        return "closed"


def run_checks(report: dict[str, Any]) -> list[Finding]:
    """规则表：每条都是真踩过的坑，修法必须可以直接执行。"""
    findings: list[Finding] = []
    vm, guest = report.get("vm", {}), report.get("guest", {})
    ports = report.get("ports", {})

    if vm.get("error"):
        findings.append(Finding("vbox", False, vm["error"],
                                "VBoxManage 不可用或虚拟机不存在", ["arklink env plan"], ctx="host"))
        return findings
    if not vm.get("exists"):
        findings.append(Finding("vm_exists", False, "虚拟机还没创建",
                                "跑 arklink env create（需要 Ubuntu ISO）",
                                ["arklink env create --download --yes"], ctx="host"))
        return findings
    state = str(vm.get("state", ""))
    if state != "running":
        findings.append(Finding("vm_state", False, f"虚拟机状态 {state}",
                                "unattended install 只挂应答盘不开机 / 被手动关机了",
                                ["arklink env up"], ctx="host"))
        return findings

    disk_used = vm.get("disk_used_gb") or 0
    disk_limit = vm.get("disk_limit_gb") or 8
    if disk_limit and disk_used / disk_limit > 0.9:
        findings.append(Finding("disk_host", False,
                                f"虚拟磁盘已用 {disk_used}GB / 上限 {disk_limit}GB",
                                "磁盘快满了：删 ISO / 旧快照，或重建加大 disk_limit_gb",
                                [], ctx="host"))

    if "error" in guest:
        findings.append(Finding("guest_reachable", False, guest["error"],
                                "guestcontrol 连不上：系统还没装完，或密码不对",
                                ["等安装完成（env status 磁盘 >2GB 且重启后）再试；"
                                 "密码忘了只能 destroy 重建"], ctx="host"))
        return findings

    disk_pct = int(guest.get("DISK") or 0)
    if disk_pct >= 90:
        findings.append(Finding("disk_guest", False, f"guest 根分区使用 {disk_pct}%",
                                "清理 apt 缓存与 QQ 缓存",
                                ["sudo apt-get clean", "rm -rf /home/arklink/.config/QQ/crash_files/*"]))

    if guest.get("SVC_BOT") != "active":
        findings.append(Finding("svc_bot", False, f"arklink.service={guest.get('SVC_BOT')}",
                                "主服务挂了（Restart=always 会自动拉起，连续失败才需要人工）",
                                ["sudo systemctl restart arklink.service"]))
    if guest.get("SVC_API") != "active":
        findings.append(Finding("svc_api", False, f"arklink-api.service={guest.get('SVC_API')}",
                                "API 服务挂了",
                                ["sudo systemctl restart arklink-api.service"]))

    if int(guest.get("QQ_PROC") or 0) == 0:
        findings.append(Finding("qq_proc", False, "QQ 进程数 0",
                                "QQ 崩溃后未被拉起（服务监督循环会每 20s 自愈，也可手动）",
                                ["sudo systemctl restart arklink.service"]))
    if int(guest.get("QQ_WIN") or 0) == 0 and int(guest.get("QQ_PROC") or 0) > 0:
        findings.append(Finding("qq_win", False, "QQ 进程在但窗口不在",
                                "常见于 Xvfb 僵死（进程在屏幕黑）——start.sh 会检测 xdpyinfo 并重建；"
                                "手动修法是杀干净 Xvfb/QQ/openbox 再重启服务",
                                ["sudo pkill -f 'Xvfb :99'; sudo pkill -f '/opt/QQ/qq'; "
                                 "sudo pkill openbox; sleep 2; sudo systemctl restart arklink.service"]))

    crashes = int(guest.get("CRASHES") or 0)
    if crashes > 0:
        findings.append(Finding("qq_crash_files", False, f"crash_files 里有 {crashes} 个文件",
                                "QQ 之前崩溃过；强杀进程会这样。崩溃不丢登录态的前提是已备份登录态",
                                ["tar czf ~/qq-login.tgz -C ~ .config/QQ"]))

    qqlog = str(guest.get("QQLOG") or "")
    if "Permission denied" in qqlog:
        findings.append(Finding("qq_log_perm", False, "QQ 启动日志写不进去（Permission denied）",
                                "start.sh 以 root 跑 QQ，而 /tmp/qq.log 属主是别的用户——"
                                "QQ 会被重定向拦住直接不起来；新版 start.sh 用登录用户跑 QQ 并自动换日志名",
                                ["sudo systemctl restart arklink.service"]))

    if guest.get("LOGIN_BAK") != "yes":
        findings.append(Finding("login_backup", False, "还没备份 QQ 登录态",
                                "登录成功后立刻备份：之后 QQ 崩溃重启可以免扫码恢复",
                                ["tar czf ~/qq-login.tgz -C ~ .config/QQ"]))

    if ports.get("webui") != "open":
        findings.append(Finding("port_webui", False, f"WebUI 端口 {ports.get('webui')}",
                                "端口映射没生效或服务没跑（装 Guest Additions 后才有 push/映射全链路）",
                                ["arklink env status"], ctx="host"))
    if ports.get("api") != "open":
        findings.append(Finding("port_api", False, f"API 端口 {ports.get('api')}",
                                "API 服务没跑或映射缺失",
                                ["sudo systemctl restart arklink-api.service"]))
    return findings


# ---------------------------------------------------------------- AI 层
SYSTEM_PROMPT = """你是 ArkLink 的部署排障助手。ArkLink 是一个 QQ bot 运行时，
用 VirtualBox 虚拟机（Ubuntu 24.04 + Xvfb 虚拟屏幕 + openbox + xdotool）无头跑
Linux QQ，宿主机通过 VBoxManage guestcontrol 和端口映射与之交互。
用户会给你一份体检报告（JSON）。已知背景：
- `env create` 用 VBoxManage unattended install 挂应答盘后必须 startvm 才真正装机；
- QQ 官方 CDN 对部分网络回 403，GitHub 镜像可用且 SHA256 校验；
- guestcontrol 直写 /opt 会被拒（要先 sudo 建目录 chown）；
- Xvfb 会僵死（进程在但屏幕黑），要用 xdpyinfo 健康检查而不是 pgrep；
- QQ 搜索结果是独立 X 窗口「综合搜索」，点它的坐标要相对弹窗而非主窗口；
- QQ 强杀会崩溃，登录态在 ~/.config/QQ，崩溃后可能要重新扫码。
请输出 JSON：{"diagnosis": "一句话结论", "steps": [{"desc": "做什么",
"cmd": "在虚拟机里或宿主机执行的命令", "ctx": "guest 或 host"}]}。
只给确定能执行的命令，不要给解释性长文。"""


def deepseek_diagnose(report: dict[str, Any], findings: list[Finding], key: str) -> dict[str, Any]:
    """把报告+规则结论发给 DeepSeek，返回解析后的 JSON 建议。"""
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                {"report": report,
                 "rule_findings": [f.__dict__ for f in findings]},
                ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"DeepSeek API 返回 {exc.code}：{detail}") from exc
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"DeepSeek 响应解析失败：{exc}") from exc


def run_fixes(mgr: "EnvManager", steps: list[dict[str, Any]], *, password: str) -> list[str]:
    """按 AI/规则给出的修复步骤逐条执行。ctx=host 的走宿主 shell。"""
    import subprocess

    from .vbox import guest_run_commands

    done: list[str] = []
    for step in steps:
        cmd = str(step.get("cmd") or "").strip()
        ctx = str(step.get("ctx") or "guest")
        if not cmd:
            continue
        if ctx == "host":
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
            out = (proc.stdout + proc.stderr).strip()
        else:
            cmds = guest_run_commands(mgr.plan.name, cmd,
                                      user=mgr.state.user, password=password)
            out = "\n".join(mgr.require_box().apply(cmds, timeout=600)).strip()
        done.append(f"$ {cmd}\n{out or '（无输出）'}")
    return done
