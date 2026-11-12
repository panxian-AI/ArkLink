"""版本检测 / 更新 / 回退的测试。

全部离线：把 `fetch_versions` 换掉，把状态文件指向 tmp，绝不真的调 pip。
"""

from __future__ import annotations

import json
import os

import pytest

from arklink import update


@pytest.fixture()
def state_file(tmp_path):
    return str(tmp_path / "state.json")


def test_parse_and_sort_versions():
    assert update.parse_version("v0.5.1") == (0, 5, 1)
    assert update.sort_versions(["0.2.0", "0.10.0", "0.9.9", "nightly"]) == [
        "0.10.0",
        "0.9.9",
        "0.2.0",
        "nightly",
    ]
    with pytest.raises(ValueError):
        update.parse_version("abc")


def test_check_compatibility_blocks_old_target():
    compat = update.check_compatibility("0.2.0", {"hook-windows", "hook-linux", "onebot-serve"})
    assert not compat.ok
    since = {feature.key: feature.since for feature in compat.missing}
    assert since == {"hook-windows": "0.3.0", "hook-linux": "0.5.0", "onebot-serve": "0.4.0"}
    text = compat.explain()
    assert "不可用" in text and "--force" in text


def test_check_compatibility_allows_same_version():
    compat = update.check_compatibility("0.5.0", {"hook-linux", "onebot-serve"})
    assert compat.ok
    assert "功能完整" in compat.explain()


def test_detect_in_use_reads_toml(tmp_path):
    config = tmp_path / "bot.toml"
    config.write_text(
        '[[adapters]]\ntype = "ntqq-hook"\n\n[[adapters]]\ntype = "milky"\n', encoding="utf-8"
    )
    names = update.adapters_from_config(str(config))
    assert names == ["ntqq-hook", "milky"]
    used = update.detect_in_use(names, platform="linux")
    assert {"hook-windows", "hook-linux", "milky"} <= used


def test_detect_in_use_windows_skips_linux_feature():
    used = update.detect_in_use(["ntqq-hook"], platform="win32")
    assert "hook-windows" in used
    assert "hook-linux" not in used


def test_update_check_can_be_disabled(state_file, monkeypatch):
    update.set_update_check(False, state_file)

    def boom(*_a, **_k):
        raise AssertionError("关闭后不能联网")

    monkeypatch.setattr(update, "fetch_versions", boom)
    status = update.check_update(state_file=state_file)
    assert status.checked is False
    assert "关闭" in status.reason


def test_update_check_uses_cache(state_file, monkeypatch):
    update.save_state(
        {
            "update_check": True,
            "checked_at": 9_999_999_999,
            "latest": {"version": "9.9.9", "notes": "测试版本"},
        },
        state_file,
    )

    def boom(*_a, **_k):
        raise AssertionError("有缓存不该联网")

    monkeypatch.setattr(update, "fetch_versions", boom)
    status = update.check_update(state_file=state_file)
    assert status.checked and status.available
    assert status.latest == "9.9.9"
    assert status.notes == "测试版本"


def test_update_check_offline_is_not_fatal(state_file, monkeypatch):
    update.save_state({"update_check": True, "checked_at": 0}, state_file)
    monkeypatch.setattr(update, "fetch_versions", lambda *a, **k: [])
    status = update.check_update(state_file=state_file)
    assert status.checked is False
    assert "离线" in status.reason or "失败" in status.reason
    assert update.load_state(state_file).get("last_error")


def test_latest_release_picks_highest():
    releases = [
        update.ReleaseInfo("0.3.0"),
        update.ReleaseInfo("0.10.0"),
        update.ReleaseInfo("0.9.0"),
    ]
    assert update.latest_release(releases).version == "0.10.0"


def test_install_command_is_tag_pinned():
    command = update.install_command("0.5.0", python="/usr/bin/python3")
    assert command[:3] == ["/usr/bin/python3", "-m", "pip"]
    assert any("ArkLink@v0.5.0" in part for part in command)


def test_cli_update_blocks_incompatible_rollback(tmp_path, state_file, monkeypatch, capsys):
    from arklink.__main__ import main

    monkeypatch.setenv("ARKLINK_STATE", state_file)
    monkeypatch.setenv("ARKLINK_NO_UPDATE", "1")
    config = tmp_path / "bot.toml"
    config.write_text('[[adapters]]\ntype = "ntqq-hook"\n', encoding="utf-8")

    code = main(["update", "--to", "0.2.0", "--config", str(config)])
    captured = capsys.readouterr()
    assert code == 2, "回退到缺少 hook 的版本必须被拦住"
    assert "不可用" in captured.err


def test_cli_update_force_prints_command(tmp_path, state_file, monkeypatch, capsys):
    from arklink.__main__ import main

    monkeypatch.setenv("ARKLINK_STATE", state_file)
    monkeypatch.setenv("ARKLINK_NO_UPDATE", "1")
    config = tmp_path / "bot.toml"
    config.write_text('[[adapters]]\ntype = "ntqq-hook"\n', encoding="utf-8")

    code = main(["update", "--to", "0.2.0", "--config", str(config), "--force"])
    captured = capsys.readouterr()
    assert code == 0
    assert "pip install" in captured.out
    assert "--yes" in captured.out, "默认不能真装，要提示加 --yes"


def test_cli_disable_and_enable_check(state_file, monkeypatch, capsys):
    from arklink.__main__ import main

    monkeypatch.setenv("ARKLINK_STATE", state_file)
    assert main(["update", "--disable-check"]) == 0
    assert json.loads(open(state_file, encoding="utf-8").read())["update_check"] is False
    assert main(["update", "--enable-check"]) == 0
    assert json.loads(open(state_file, encoding="utf-8").read())["update_check"] is True
    capsys.readouterr()


def test_cli_version_reports_disabled(state_file, monkeypatch, capsys):
    from arklink.__main__ import main

    monkeypatch.setenv("ARKLINK_STATE", state_file)
    update.set_update_check(False, state_file)
    assert main(["version"]) == 0
    out = capsys.readouterr().out
    assert "ArkLink" in out
    assert "0.6.0" in out or update.current_version() in out
    assert "已关闭" in out


def test_notice_skipped_in_ci(monkeypatch, capsys):
    from arklink.__main__ import _maybe_notice

    monkeypatch.setenv("CI", "true")

    def boom(*_a, **_k):
        raise AssertionError("CI 里不该联网")

    monkeypatch.setattr(update, "check_update", boom)
    _maybe_notice()
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------- 双仓库
def test_repo_of_supports_both_projects():
    assert update.repo_of("arklink") == "panxian-AI/ArkLink"
    assert update.repo_of("arkbot") == "panxian-AI/ArkBot"
    with pytest.raises(KeyError):
        update.repo_of("nope")


def test_install_command_targets_arkbot_repo():
    command = update.install_command("0.3.0", project="arkbot")
    assert any("ArkBot@v0.3.0" in part for part in command)


def test_arkbot_compat_blocks_rollback_to_0_3():
    used = {"onebot-compat", "adapter-loader"}
    compat = update.check_compatibility("0.3.0", used, project="arkbot")
    assert not compat.ok
    assert {f.since for f in compat.missing} == {"0.4.0"}
    assert "OneBot 11 兼容层" in compat.explain()


def test_arkbot_compat_allows_same_version():
    compat = update.check_compatibility("0.4.0", {"onebot-compat"}, project="arkbot")
    assert compat.ok


def test_detect_in_use_arkbot_reads_compat_section():
    used = update.detect_in_use(["ntqq-hook"], project="arkbot", config_text="[compat]\nenabled = true\n")
    assert "onebot-compat" in used
    assert "adapter-loader" in used


def test_state_keys_are_namespaced_per_project(state_file, monkeypatch):
    update.save_state(
        {"update_check": True, "checked_at": 9_999_999_999, "latest": {"version": "9.9.9"}},
        state_file,
    )
    monkeypatch.setattr(update, "fetch_versions", lambda *a, **k: [])
    # arklink 走缓存；arkbot 没有缓存，会去拉（这里被替换成空列表）→ 不命中缓存
    arklink_status = update.check_update(state_file=state_file)
    assert arklink_status.available and arklink_status.reason == "缓存"
    arkbot_status = update.check_update(state_file=state_file, project="arkbot", current="0.4.0")
    assert arkbot_status.reason != "缓存"


def test_cli_update_blocks_arkbot_rollback(tmp_path, state_file, monkeypatch, capsys):
    from arklink.__main__ import main

    monkeypatch.setenv("ARKLINK_STATE", state_file)
    monkeypatch.setenv("ARKLINK_NO_UPDATE", "1")
    config = tmp_path / "bot.toml"
    config.write_text('[compat]\nenabled = true\n\n[[adapters]]\ntype = "ntqq-hook"\n', encoding="utf-8")

    code = main(["update", "--project", "arkbot", "--to", "0.3.0", "--config", str(config)])
    captured = capsys.readouterr()
    assert code == 2
    assert "兼容层" in captured.err
    assert "ArkBot" in captured.out
