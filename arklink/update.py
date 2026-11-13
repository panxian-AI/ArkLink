"""版本检测、更新与回退。

三条硬约束：

1. **可以关**。`arklink update --disable-check`（或环境变量 `ARKLINK_NO_UPDATE=1`）
   关掉之后，任何命令都不再联网；
2. **回退前先算清楚**。低版本可能没有你现在用的功能（比如 0.3.0 才有 `ntqq-hook`，
   0.4.0 才有 `serve onebot`，0.5.0 才支持 Linux hook）。目标版本不够就明确列出
   「哪个功能会不可用」，除非 `--force`；
3. **不静默装东西**。只打印命令，真正执行要带 `--yes`；`pip` 是被调用的外部进程，
   失败原因原样透出，不吞。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

REPO = "panxian-AI/ArkLink"
GIT_URL = f"git+https://github.com/{REPO}"

#: 两个仓库各管一段：ArkBot 是框架本体（插件 / 内核 / 兼容层），
#: ArkLink 是协议外挂层（连 QQ 的通道）。升级回退要分别针对，不能混。
PROJECTS: dict[str, str] = {
    "arklink": "panxian-AI/ArkLink",
    "arkbot": "panxian-AI/ArkBot",
}
PROJECT_TITLES: dict[str, str] = {
    "arklink": "ArkLink（协议外挂层）",
    "arkbot": "ArkBot（框架内核）",
}


def repo_of(project: str = "arklink") -> str:
    if project not in PROJECTS:
        raise KeyError(f"未知仓库 {project!r}，可选：{', '.join(PROJECTS)}")
    return PROJECTS[project]


def tags_url(project: str = "arklink") -> str:
    return f"https://api.github.com/repos/{repo_of(project)}/tags?per_page=100"


def releases_url(project: str = "arklink") -> str:
    return f"https://api.github.com/repos/{repo_of(project)}/releases?per_page=100"

#: 缓存多久（秒）。离线环境下不会每次都卡在超时上。
CACHE_TTL = 6 * 3600
TIMEOUT = 6.0


# ---------------------------------------------------------------- 状态文件
def state_path() -> str:
    override = os.environ.get("ARKLINK_STATE", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".arklink", "state.json")


def load_state(path: str | None = None) -> dict[str, Any]:
    target = path or state_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any], path: str | None = None) -> None:
    target = path or state_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def update_check_enabled(state: dict[str, Any] | None = None) -> bool:
    if os.environ.get("ARKLINK_NO_UPDATE", "").strip() not in ("", "0", "false", "False"):
        return False
    data = state if state is not None else load_state()
    return bool(data.get("update_check", True))


def set_update_check(enabled: bool, path: str | None = None) -> None:
    data = load_state(path)
    data["update_check"] = bool(enabled)
    save_state(data, path)


# ---------------------------------------------------------------- 版本号
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.\-]+))?$")


def parse_version(text: str) -> tuple[int, int, int]:
    match = _SEMVER.match(text.strip())
    if not match:
        raise ValueError(f"不是合法版本号：{text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def current_version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "0.0.0"


def sort_versions(items: Iterable[str]) -> list[str]:
    """从新到旧；无法解析的版本（如 nightly）排在最后。"""
    good: list[tuple[tuple[int, int, int], str]] = []
    bad: list[str] = []
    for item in items:
        try:
            good.append((parse_version(item), item))
        except ValueError:
            bad.append(item)
    good.sort(key=lambda pair: pair[0], reverse=True)
    return [name for _key, name in good] + bad


# ---------------------------------------------------------------- 功能清单
@dataclass(frozen=True)
class Feature:
    key: str
    since: str
    description: str


#: 「某个能力从哪个版本开始有」。回退检查就靠这张表，新增能力时记得补一行。
FEATURES: tuple[Feature, ...] = (
    Feature("milky", "0.1.0", "Milky 1.3 通道"),
    Feature("satori", "0.1.0", "Satori v1 通道"),
    Feature("ntqq-native", "0.2.0", "扫码登录 / 只读花名册 / 收推送"),
    Feature("hook-windows", "0.3.0", "Windows 注入通道（ntqq-hook）"),
    Feature("onebot-serve", "0.4.0", "独立 OneBot 11 服务端（serve onebot）"),
    Feature("hook-linux", "0.5.0", "Linux 注入通道（ptrace + Unix socket）"),
    Feature("update", "0.6.0", "版本检测 / 更新 / 回退"),
    Feature("doctor", "0.7.0", "迁移体检（arklink doctor）"),
    Feature("send", "0.8.0", "发送链路（策略闸门 + UI 通道 / arklink send）"),
    Feature("webui", "0.8.0", "WebUI 控制台（serve webui）"),
    Feature("x11", "0.9.0", "无头 Linux 发送（Xvfb + xdotool，不抢焦点）"),
    Feature("env", "0.10.0", "内置虚拟环境（arklink env：建/启/停/推/删）"),
    Feature("standalone", "0.11.0", "自助使用：policy.json + arklink peers + arklink start + 双击启动"),
    Feature("bot", "0.12.0", "bot 运行时：MessageBus + arklink bot new/run/check + 能力矩阵"),
    Feature("api", "0.12.0", "给别人调用的 HTTP + SSE 接口（v1）与 Python SDK"),
    Feature("server", "0.13.0", "小服务器直装部署（无虚拟机：systemd + QQ 看护 + 登录态持久化）"),
)

_FEATURE_BY_KEY = {f.key: f for f in FEATURES}

#: ArkBot 的能力表。写得保守：只列「确实某版本才有」的东西，宁可漏不可编。
BOT_FEATURES: tuple[Feature, ...] = (
    Feature("official-qq", "0.1.0", "QQ 官方 Bot 通道"),
    Feature("outbox", "0.2.0", "离线发件箱（落盘补发）"),
    Feature("adapter-loader", "0.4.0", "适配器加载器（entry point / 点分路径）"),
    Feature("onebot-compat", "0.4.0", "OneBot 11 兼容层（对外扮演协议端）"),
    Feature("arkbot2", "0.4.0", "ArkBot NG 微内核"),
)

_BOT_FEATURE_BY_KEY = {f.key: f for f in BOT_FEATURES}


def features_of(project: str = "arklink") -> tuple[Feature, ...]:
    return FEATURES if project == "arklink" else BOT_FEATURES


@dataclass
class Compatibility:
    target: str
    missing: list[Feature] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing

    def explain(self) -> str:
        if self.ok:
            return f"回退到 {self.target} 功能完整。"
        lines = [f"回退到 {self.target} 会导致以下功能不可用："]
        for feature in self.missing:
            lines.append(f"  - {feature.description}（需要 {feature.since}+）")
        if self.notes:
            lines.append("")
            lines.extend(f"  {note}" for note in self.notes)
        lines.append("")
        lines.append("确认要继续请加 --force。")
        return "\n".join(lines)


def detect_in_use(
    config_adapters: Iterable[str] = (),
    platform: str | None = None,
    project: str = "arklink",
    config_text: str = "",
) -> set[str]:
    """推断「你现在实际用到了哪些能力」。

    只做保守推断：配置里出现的适配器类型 + 当前平台 + 当前安装里有的模块。
    """
    if project == "arkbot":
        return _detect_in_use_arkbot(config_adapters, config_text)
    used: set[str] = set()
    platform = platform or sys.platform
    for name in config_adapters:
        name = str(name).strip()
        if name in ("milky",):
            used.add("milky")
        elif name in ("satori",):
            used.add("satori")
        elif name in ("ntqq-native",):
            used.add("ntqq-native")
        elif name in ("ntqq-hook", "ntqq-client"):
            used.add("hook-windows")
            if platform.startswith("linux") or platform == "darwin":
                used.add("hook-linux")
    try:
        from . import onebot  # noqa: F401

        used.add("onebot-serve")
    except Exception:
        pass
    return used


def _detect_in_use_arkbot(adapters: Iterable[str], config_text: str) -> set[str]:
    used: set[str] = {"official-qq"}
    for name in adapters:
        name = str(name).strip()
        if name in ("official_qq", "official-qq"):
            used.add("official-qq")
        elif name in ("onebot11", "ntqq-hook", "milky", "satori", "ntqq-native"):
            # 这些都要靠 0.4.0 的加载器 + 兼容层
            used.add("adapter-loader")
    if "[compat]" in (config_text or ""):
        used.add("onebot-compat")
    try:
        import arkbot2  # noqa: F401

        used.add("arkbot2")
    except Exception:
        pass
    return used


def config_text_of(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def adapters_from_config(path: str) -> list[str]:
    """从 bot.toml / arklink.toml 里抠出 `type = "..."`（不引 tomllib 之外的依赖）。"""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as fh:
            data = __import__("tomllib").load(fh)
    except Exception:
        text = ""
        try:
            text = open(path, "r", encoding="utf-8").read()
        except OSError:
            return []
        return re.findall(r'type\s*=\s*["\']([\w\-]+)["\']', text)
    out: list[str] = []
    for item in data.get("adapters") or []:
        if isinstance(item, dict) and item.get("type"):
            out.append(str(item["type"]))
    return out


def check_compatibility(target: str, used: Iterable[str], project: str = "arklink") -> Compatibility:
    result = Compatibility(target=target)
    try:
        target_tuple = parse_version(target)
    except ValueError:
        result.notes.append(f"目标版本 {target!r} 无法解析，跳过功能检查")
        return result
    table = _FEATURE_BY_KEY if project == "arklink" else _BOT_FEATURE_BY_KEY
    for key in used:
        feature = table.get(key)
        if feature is None:
            continue
        if target_tuple < parse_version(feature.since):
            result.missing.append(feature)
    return result


# ---------------------------------------------------------------- 远端版本
def _github_json(url: str, timeout: float = TIMEOUT) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "arklink-update"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass
class ReleaseInfo:
    version: str
    date: str = ""
    notes: str = ""


def fetch_versions(timeout: float = TIMEOUT, project: str = "arklink") -> list[ReleaseInfo]:
    """拉远端所有版本（先 releases，拿不到再退回 tags）。失败返回空列表，不抛。"""
    try:
        releases = _github_json(releases_url(project), timeout)
    except Exception:
        releases = []
    out: list[ReleaseInfo] = []
    if isinstance(releases, list):
        for item in releases:
            tag = str(item.get("tag_name") or "")
            if not _SEMVER.match(tag):
                continue
            out.append(
                ReleaseInfo(
                    version=tag.lstrip("v"),
                    date=str(item.get("published_at") or "")[:10],
                    notes=str(item.get("body") or "").strip(),
                )
            )
    if out:
        return out
    try:
        tags = _github_json(tags_url(project), timeout)
    except Exception:
        return []
    for item in tags if isinstance(tags, list) else []:
        tag = str(item.get("name") or "")
        if _SEMVER.match(tag):
            out.append(ReleaseInfo(version=tag.lstrip("v")))
    return out


def latest_release(releases: list[ReleaseInfo]) -> ReleaseInfo | None:
    ordered = sort_versions([r.version for r in releases])
    if not ordered:
        return None
    top = ordered[0]
    for item in releases:
        if item.version == top:
            return item
    return ReleaseInfo(version=top)


@dataclass
class UpdateStatus:
    current: str
    latest: str = ""
    available: bool = False
    notes: str = ""
    checked: bool = False
    reason: str = ""
    versions: list[ReleaseInfo] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "available": self.available,
            "notes": self.notes,
            "checked": self.checked,
            "reason": self.reason,
        }


def check_update(
    force: bool = False,
    state_file: str | None = None,
    timeout: float = TIMEOUT,
    project: str = "arklink",
    current: str | None = None,
) -> UpdateStatus:
    """检查新版本。被关掉或离线都只是「不检查」，不报错、不阻塞。"""
    current = current or (current_version() if project == "arklink" else project_version(project))
    state = load_state(state_file)
    if not update_check_enabled(state):
        return UpdateStatus(current=current, reason="更新检查已关闭（arklink update --enable-check 可恢复）")

    cached = state.get(_key(project, "latest")) or {}
    fresh = time.time() - float(state.get(_key(project, "checked_at")) or 0) < CACHE_TTL
    if not force and fresh and isinstance(cached, dict) and cached.get("version"):
        latest = str(cached["version"])
        return UpdateStatus(
            current=current,
            latest=latest,
            available=_newer(latest, current),
            notes=str(cached.get("notes") or ""),
            checked=True,
            reason="缓存",
        )

    releases = fetch_versions(timeout, project)
    latest_info = latest_release(releases)
    if latest_info is None:
        state[_key(project, "checked_at")] = time.time()
        state[_key(project, "last_error")] = "拉取版本列表失败（离线或 GitHub 不可达）"
        save_state(state, state_file)
        return UpdateStatus(current=current, reason=state[_key(project, "last_error")], versions=releases)

    state[_key(project, "checked_at")] = time.time()
    state[_key(project, "latest")] = {"version": latest_info.version, "notes": latest_info.notes[:2000]}
    state.pop(_key(project, "last_error"), None)
    save_state(state, state_file)
    return UpdateStatus(
        current=current,
        latest=latest_info.version,
        available=_newer(latest_info.version, current),
        notes=latest_info.notes,
        checked=True,
        versions=releases,
    )


def _key(project: str, name: str) -> str:
    """arklink 用扁平键（兼容旧状态文件），其他仓库加前缀。"""
    return name if project == "arklink" else f"{project}:{name}"


def project_version(project: str = "arklink") -> str:
    """另一个仓库的已安装版本（没装就是空串）。"""
    if project == "arklink":
        return current_version()
    module = "arkbot" if project == "arkbot" else project
    try:
        mod = __import__(module)
        return str(getattr(mod, "__version__", "") or "")
    except Exception:
        return ""


def _newer(candidate: str, current: str) -> bool:
    try:
        return parse_version(candidate) > parse_version(current)
    except ValueError:
        return False


# ---------------------------------------------------------------- 安装
def install_command(version: str, python: str | None = None, project: str = "arklink") -> list[str]:
    exe = python or sys.executable
    url = f"git+https://github.com/{repo_of(project)}"
    return [exe, "-m", "pip", "install", "--upgrade", "--force-reinstall", f"{url}@v{version}"]


def install(
    version: str, dry_run: bool = True, python: str | None = None, project: str = "arklink"
) -> tuple[int, str]:
    """切换版本。默认只打印命令（dry_run），返回 (returncode, 输出)。"""
    command = install_command(version, python, project)
    if dry_run:
        return 0, " ".join(command)
    proc = subprocess.run(command, capture_output=True, text=True)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()
