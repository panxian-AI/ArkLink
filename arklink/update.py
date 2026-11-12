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
TAGS_URL = f"https://api.github.com/repos/{REPO}/tags?per_page=100"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
GIT_URL = f"git+https://github.com/{REPO}"

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
)

_FEATURE_BY_KEY = {f.key: f for f in FEATURES}


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


def detect_in_use(config_adapters: Iterable[str] = (), platform: str | None = None) -> set[str]:
    """推断「你现在实际用到了哪些能力」。

    只做保守推断：配置里出现的适配器类型 + 当前平台 + 是否装了 OneBot 服务端模块。
    """
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


def check_compatibility(target: str, used: Iterable[str]) -> Compatibility:
    result = Compatibility(target=target)
    try:
        target_tuple = parse_version(target)
    except ValueError:
        result.notes.append(f"目标版本 {target!r} 无法解析，跳过功能检查")
        return result
    for key in used:
        feature = _FEATURE_BY_KEY.get(key)
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


def fetch_versions(timeout: float = TIMEOUT) -> list[ReleaseInfo]:
    """拉远端所有版本（先 releases，拿不到再退回 tags）。失败返回空列表，不抛。"""
    try:
        releases = _github_json(RELEASES_URL, timeout)
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
        tags = _github_json(TAGS_URL, timeout)
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
) -> UpdateStatus:
    """检查新版本。被关掉或离线都只是「不检查」，不报错、不阻塞。"""
    current = current_version()
    state = load_state(state_file)
    if not update_check_enabled(state):
        return UpdateStatus(current=current, reason="更新检查已关闭（arklink update --enable-check 可恢复）")

    cached = state.get("latest") or {}
    fresh = time.time() - float(state.get("checked_at") or 0) < CACHE_TTL
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

    releases = fetch_versions(timeout)
    latest_info = latest_release(releases)
    if latest_info is None:
        state["checked_at"] = time.time()
        state["last_error"] = "拉取版本列表失败（离线或 GitHub 不可达）"
        save_state(state, state_file)
        return UpdateStatus(current=current, reason=state["last_error"], versions=releases)

    state["checked_at"] = time.time()
    state["latest"] = {"version": latest_info.version, "notes": latest_info.notes[:2000]}
    state.pop("last_error", None)
    save_state(state, state_file)
    return UpdateStatus(
        current=current,
        latest=latest_info.version,
        available=_newer(latest_info.version, current),
        notes=latest_info.notes,
        checked=True,
        versions=releases,
    )


def _newer(candidate: str, current: str) -> bool:
    try:
        return parse_version(candidate) > parse_version(current)
    except ValueError:
        return False


# ---------------------------------------------------------------- 安装
def install_command(version: str, python: str | None = None) -> list[str]:
    exe = python or sys.executable
    return [exe, "-m", "pip", "install", "--upgrade", "--force-reinstall", f"{GIT_URL}@v{version}"]


def install(version: str, dry_run: bool = True, python: str | None = None) -> tuple[int, str]:
    """切换版本。默认只打印命令（dry_run），返回 (returncode, 输出)。"""
    command = install_command(version, python)
    if dry_run:
        return 0, " ".join(command)
    proc = subprocess.run(command, capture_output=True, text=True)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()
