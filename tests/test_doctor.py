"""迁移体检的测试。

不依赖真 QQ：`with_server=False` 时只检查纯离线项；服务端自检本身是
「起一个临时端口再打自己」，所以可以真跑。
"""

from __future__ import annotations

import pytest

from arklink import doctor


def test_run_checks_offline_has_core_items():
    report = doctor.run_checks(config="/nonexistent/bot.toml", offline=True, with_server=False)
    names = [c.name for c in report.checks]
    assert "Python" in names
    assert "版本" in names
    assert "hook 模块" in names
    assert "QQ 进程" in names
    # 配置文件不存在必须是 SKIP 而不是 FAIL
    config_check = next(c for c in report.checks if c.name == "配置文件")
    assert config_check.status == doctor.SKIP
    assert not report.failed, report.render()


def test_check_config_reports_unknown_adapter(tmp_path):
    config = tmp_path / "bot.toml"
    config.write_text('[[adapters]]\ntype = "not-a-real-adapter"\n', encoding="utf-8")
    report = doctor.Report()
    doctor.check_config(report, str(config))
    check = report.checks[-1]
    assert check.status == doctor.FAIL
    assert "未知类型" in check.detail
    assert "可选" in check.hint


def test_check_config_accepts_known_and_dotted(tmp_path):
    config = tmp_path / "bot.toml"
    config.write_text(
        '[[adapters]]\ntype = "milky"\n\n[[adapters]]\ntype = "pkg.mod:Class"\n', encoding="utf-8"
    )
    report = doctor.Report()
    adapters = doctor.check_config(report, str(config))
    assert adapters == ["milky", "pkg.mod:Class"]
    assert report.checks[-1].status == doctor.OK


def test_onebot_server_selfcheck_passes():
    report = doctor.Report()
    doctor.check_onebot_server(report, token="t")
    check = report.checks[-1]
    assert check.status == doctor.OK, check.detail
    assert "鉴权生效" in check.detail


def test_summary_counts():
    report = doctor.Report()
    report.add("a", doctor.OK, "")
    report.add("b", doctor.WARN, "")
    report.add("c", doctor.FAIL, "")
    report.add("d", doctor.SKIP, "")
    text = doctor.summary(report)
    assert "通过 1" in text and "注意 1" in text and "失败 1" in text and "跳过 1" in text


def test_cli_doctor_exit_code(tmp_path, monkeypatch, capsys):
    from arklink.__main__ import main

    monkeypatch.setenv("ARKLINK_NO_UPDATE", "1")
    code = main(["doctor", "--offline", "--config", str(tmp_path / "none.toml")])
    out = capsys.readouterr().out
    assert "检查项" in out
    assert "共" in out
    assert code in (0, 1)
