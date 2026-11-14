"""deploy 的测试：地址解析、打包排除项、远程命令拼装。

不碰真机：ssh/scp 都通过 dry-run 或纯函数验证。
"""

from __future__ import annotations

import tarfile

from arklink.deploy import Target, pack_source, parse_target


def test_target_empty_means_local_vm():
    for raw in ("", "   ", "local", "vm", "本机", "虚拟机", "127.0.0.1", "localhost"):
        assert Target(raw).is_local_vm, raw


def test_target_ssh_string_with_user_and_port():
    assert Target("root@1.2.3.4").ssh_target == "root@1.2.3.4"
    assert Target("root@1.2.3.4:2222").ssh_target == "-p 2222 root@1.2.3.4"
    assert Target("1.2.3.4:22").ssh_target == "-p 22 1.2.3.4"
    assert Target("root@1.2.3.4").is_local_vm is False


def test_parse_target_accepts_flag_and_positional():
    assert parse_target(["root@1.2.3.4"]).raw == "root@1.2.3.4"
    assert parse_target(["--host", "root@1.2.3.4"]).raw == "root@1.2.3.4"
    assert parse_target(["-H", "1.2.3.4:2200"]).raw == "1.2.3.4:2200"
    assert parse_target(["--dry-run"]).is_local_vm


def test_pack_source_skips_heavy_dirs(tmp_path):
    repo = tmp_path / "ArkLink"
    (repo / "arklink").mkdir(parents=True)
    (repo / "arklink" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("secret\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.pyc").write_text("junk", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "bot.db").write_text("db", encoding="utf-8")
    (repo / "tools").mkdir()
    (repo / "tools" / "setup_server.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    out = pack_source(repo, tmp_path / "pkg.tgz")
    with tarfile.open(out) as tar:
        names = tar.getnames()

    joined = "\n".join(names)
    assert "arklink/core.py" in joined
    assert "tools/setup_server.sh" in joined
    assert ".git" not in joined
    assert "a.pyc" not in joined
    assert "bot.db" not in joined
