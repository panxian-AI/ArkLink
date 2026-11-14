"""`env doctor` 的规则层测试。

规则表里每一条都对应真机踩过的坑（VBox 7.2 hostname、unattended 不开机、
/opt 写不进去、Xvfb 僵死、QQ 搜索弹窗劫持……），所以这里的测试是「坑的回归」：
以后谁把某条规则删了，测试会立刻炸。
"""

from __future__ import annotations

import json

import arklink.env.doctor as D


def _healthy() -> dict:
    return {
        "vm": {"name": "arklink", "exists": True, "state": "running",
               "disk_used_gb": 5.8, "disk_limit_gb": 8},
        "guest": {"USER": "arklink", "DISK": "42", "SVC_BOT": "active",
                  "SVC_API": "active", "QQ_PROC": "6", "QQ_WIN": "1",
                  "CRASHES": "0", "LOGIN_BAK": "yes"},
        "ports": {"webui": "open", "api": "open"},
    }


def test_healthy_report_has_no_problems():
    assert [f for f in D.run_checks(_healthy()) if not f.ok] == []


def test_vm_not_running_points_to_env_up():
    report = _healthy()
    report["vm"]["state"] = "poweroff"
    problems = D.run_checks(report)
    assert any(f.check == "vm_state" for f in problems)
    assert any("env up" in " ".join(f.fix) for f in problems)


def test_vm_missing_points_to_create():
    report = {"vm": {"exists": False, "state": "poweroff"}, "guest": {}, "ports": {}}
    problems = D.run_checks(report)
    assert problems[0].check == "vm_exists"
    assert "create" in " ".join(problems[0].fix)


def test_qq_process_dead_has_restart_fix():
    report = _healthy()
    report["guest"]["QQ_PROC"] = "0"
    problems = D.run_checks(report)
    hit = [f for f in problems if f.check == "qq_proc"][0]
    assert "systemctl restart arklink.service" in " ".join(hit.fix)


def test_xvfb_dead_detected_by_window_not_process():
    """进程在但窗口不在 = Xvfb 僵死，必须杀干净重建，光 restart 不够。"""
    report = _healthy()
    report["guest"]["QQ_WIN"] = "0"
    problems = D.run_checks(report)
    hit = [f for f in problems if f.check == "qq_win"][0]
    joined = " ".join(hit.fix)
    assert "pkill" in joined and "Xvfb" in joined


def test_login_backup_missing_teaches_tar():
    report = _healthy()
    report["guest"]["LOGIN_BAK"] = "no"
    problems = D.run_checks(report)
    hit = [f for f in problems if f.check == "login_backup"][0]
    assert "qq-login.tgz" in " ".join(hit.fix)


def test_guest_unreachable_stops_guest_checks():
    report = _healthy()
    report["guest"] = {"error": "guestcontrol timeout"}
    problems = D.run_checks(report)
    assert [f.check for f in problems] == ["guest_reachable"]


def test_vbox_error_short_circuits():
    report = {"vm": {"error": "VBoxManage 不在 PATH"}, "guest": {}, "ports": {}}
    problems = D.run_checks(report)
    assert problems[0].check == "vbox"


# ---------------------------------------------------------------- AI key


def test_ai_key_roundtrip(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(D, "AI_KEY_FILE", tmp_path / "ai.json")
    assert D.load_ai_key() == ""
    D.set_ai_key("sk-test-123")
    assert D.load_ai_key() == "sk-test-123"
    assert json.loads((tmp_path / "ai.json").read_text())["key"] == "sk-test-123"


def test_env_key_wins_over_file(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "AI_KEY_FILE", tmp_path / "ai.json")
    D.set_ai_key("from-file")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    assert D.load_ai_key() == "from-env"


def test_findings_serializable():
    """doctor --json 要能直接吐出去。"""
    report = _healthy()
    report["guest"]["QQ_PROC"] = "0"
    data = [f.__dict__ for f in D.run_checks(report)]
    assert json.dumps(data, ensure_ascii=False)
