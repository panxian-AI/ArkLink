"""服务器直装部署（feat: server，0.13.0）。

守住的契约：
1. `tools/setup_server.sh` 是**无虚拟机**路径——不出现 VBoxManage，脚本语法必须可解析；
2. 「长期前台运行」三件套都在：systemd 三服务、QQ 看护（60 秒防抖）、登录态备份/还原；
3. 密钥不裸奔：WebUI 口令与 API token 都生成、落盘 /etc/arklink/、chmod 600；
4. 版本登记：FEATURES 里有 server@0.13.0，pyproject 与 __version__ 一致。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from arklink import __version__
from arklink.update import _FEATURE_BY_KEY

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "setup_server.sh"
DOC = ROOT / "docs" / "部署到服务器.md"


# ---------------------------------------------------------------- 脚本本体
def test_script_exists() -> None:
    assert SCRIPT.is_file(), "tools/setup_server.sh 必须存在（服务器直装入口）"


def test_bash_syntax() -> None:
    """bash -n 能解析才允许上线（Windows 上没 bash 就跳过）。"""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("本机没有 bash")
    proc = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, f"语法错误：{proc.stderr}"


def test_no_virtualbox() -> None:
    """这条路径的核心卖点：不依赖 VirtualBox。"""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "VBoxManage" not in body, "服务器直装脚本不应调用 VBoxManage"


def test_long_running_guarantees() -> None:
    """长期前台运行三件套：看护循环 / 防抖 / 登录态备份还原。"""
    body = SCRIPT.read_text(encoding="utf-8")
    # QQ 看护循环：每 15 秒巡检
    assert "qq-watchdog.sh" in body and "sleep 15" in body
    # QQ 崩溃拉起有 60 秒防抖（避免闪退刷日志）
    assert "-ge 60" in body
    # 登录态：备份 + 重启后自动还原
    assert "qq-login.tgz" in body
    assert "已还原登录态" in body
    assert ".config/QQ" in body


def test_systemd_trio() -> None:
    """三个服务：看护 / WebUI / API，全部开机自启 + Restart=always。"""
    body = SCRIPT.read_text(encoding="utf-8")
    for unit in ("arklink-qq.service", "arklink.service", "arklink-api.service"):
        assert unit in body, f"缺 {unit}"
    assert body.count("Restart=always") >= 3
    assert body.count("WantedBy=multi-user.target") >= 3


def test_secrets_generated_and_private() -> None:
    """WebUI 口令与 API token：生成、落 /etc/arklink/、权限 600，并真的传给服务。"""
    body = SCRIPT.read_text(encoding="utf-8")
    for name in ("/etc/arklink/webui.token", "/etc/arklink/api.token"):
        assert name in body
    assert "chmod 600 /etc/arklink/webui.token" in body
    # 密钥真的用上了，不是生成完就丢
    assert '--password "$(cat /etc/arklink/webui.token)"' in body
    assert '--token "$(cat /etc/arklink/api.token)"' in body


def test_bind_local_option() -> None:
    """保守模式：--bind-local 让两个服务只听 127.0.0.1（配合 SSH 隧道）。"""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "--bind-local" in body
    assert 'BIND_HOST="127.0.0.1"' in body


def test_chinese_fonts_hard_fail() -> None:
    """中文字体缺失必须报错退出——否则 QQ 里中文全是方块、坐标全错。"""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "中文字体缺失" in body


# ---------------------------------------------------------------- 文档
def test_doc_exists_and_covers() -> None:
    assert DOC.is_file(), "docs/部署到服务器.md 必须存在"
    text = DOC.read_text(encoding="utf-8")
    for key in (
        "不需要 VirtualBox",       # 与虚拟环境路径的区分
        "setup_server.sh",          # 一条命令安装
        "长期前台运行",             # 三层保障
        "qq-login.tgz",             # 登录态持久化
        "6800",                     # WebUI 前台
        "6900",                     # API
        "安全组",                   # 安全清单
    ):
        assert key in text, f"文档缺关键内容：{key}"


# ---------------------------------------------------------------- 版本登记
def test_feature_registered() -> None:
    feat = _FEATURE_BY_KEY.get("server")
    assert feat is not None, "FEATURES 里要登记 server 能力"
    assert feat.since == "0.13.0"


def test_version_bumped() -> None:
    assert __version__ == "0.13.0"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.13.0"' in pyproject
