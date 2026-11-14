"""内置虚拟环境的测试：算账、命令序列、状态、错误路径。

不需要真的装 VirtualBox —— 所有 VBoxManage 调用都由 FakeRunner 接住，
测试只看「生成了什么命令」和「失败了怎么报」。
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from arklink.env import (
    EnvManager,
    EnvState,
    IsoRef,
    download_iso,
    find_local_iso,
    resolve_iso,
    sha256_of,
)
from arklink.env.plan import (
    DISK_LIMIT_GB,
    EnvPlan,
    choose_drive,
    plan as build_plan,
)
from arklink.env.probe import Drive, HostReport, list_drives
from arklink.env.vbox import (
    VBM,
    VBox,
    VBoxError,
    copyto_commands,
    create_commands,
    eject_commands,
    guest_run_commands,
    unattended_commands,
)


# ---------------------------------------------------------------- 夹具
class FakeRunner:
    """记录调用；可按子串返回预设输出或失败。"""

    def __init__(self, responses: dict[str, object] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        joined = " ".join(argv[1:])
        for key, value in self.responses.items():
            if key in joined:
                if isinstance(value, tuple):  # (rc, stdout, stderr)
                    rc, out, err = value
                    return subprocess.CompletedProcess(argv, rc, out, err)
                return subprocess.CompletedProcess(argv, 0, str(value), "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def make_report(*, vbox="VBoxManage", ram=16.0, drives=None) -> HostReport:
    return HostReport(
        platform="win32",
        vboxmanage=vbox,
        ram_gb=ram,
        drives=drives or (Drive("C:\\", 120.0, 500.0), Drive("E:\\", 300.0, 465.0)),
    )


def make_plan(**kwargs) -> EnvPlan:
    return build_plan(make_report(**kwargs))


# ---------------------------------------------------------------- 探测与算账
def test_probe_lists_at_least_one_drive():
    drives = list_drives()
    assert drives, "至少应该能列出一块盘"
    assert all(d.total_gb >= d.free_gb > 0 for d in drives)


def test_choose_drive_prefers_non_system_with_space():
    drives = (Drive("C:\\", 500.0, 900.0), Drive("E:\\", 60.0, 465.0))
    chosen = choose_drive(drives)
    assert chosen is not None and chosen.path == "E:\\", "系统盘再大也不该选它"


def test_choose_drive_falls_back_to_system_when_only_option():
    chosen = choose_drive((Drive("C:\\", 100.0, 500.0),))
    assert chosen is not None and chosen.is_system


def test_choose_drive_returns_none_when_space_tight():
    assert choose_drive((Drive("C:\\", 9.0, 500.0),)) is None


def test_plan_without_vbox_is_not_ok_but_still_has_layout():
    p = make_plan(vbox="")
    assert not p.ok
    assert any("VBoxManage" in msg for msg in p.problems)
    assert p.vdi.endswith("arklink.vdi"), "算账失败也要能给出计划路径"


def test_plan_picks_biggest_free_drive_and_pins_disk_limit():
    p = make_plan(drives=(Drive("C:\\", 100.0, 500.0), Drive("D:\\", 50.0, 400.0),
                          Drive("E:\\", 300.0, 465.0)))
    assert p.ok, p.problems
    assert p.workdir.startswith("E:\\")
    assert p.vdi == os.path.join(p.workdir, "arklink.vdi") or p.vdi == p.workdir + "\\arklink.vdi"
    assert p.disk_limit_gb == DISK_LIMIT_GB


def test_plan_rejects_low_memory_host():
    p = make_plan(ram=3.0)
    assert not p.ok
    assert any("内存" in msg for msg in p.problems)


# ---------------------------------------------------------------- 命令序列
def test_create_commands_layout():
    p = make_plan()
    cmds = create_commands(p)
    flat = [" ".join(c) for c in cmds]
    assert any("createvm" in c and "--basefolder" in c for c in flat)
    assert any("createmedium" in c and "--size 8192" in c for c in flat), "磁盘上限必须钉在 8GB"
    assert any("storagectl" in c for c in flat)
    assert any("storageattach" in c and "--medium" in c for c in flat)
    modify = " ".join(cmds[1])
    assert "--memory 2048" in modify and "--cpus 2" in modify
    assert "--clipboard-mode disabled" in modify and "--draganddrop disabled" in modify
    assert "--audio-driver none" in modify
    nat = " ".join(cmds[-1])
    assert "webui,tcp,127.0.0.1,6800" in nat and "ssh,tcp,127.0.0.1,2222" in nat
    # bot 接口也要映射，否则宿主机上的上层程序连不上
    assert "api,tcp,127.0.0.1,6900" in nat


def test_unattended_commands_include_sudo_and_additions():
    p = make_plan()
    cmd = unattended_commands(
        p, "/tmp/u.iso", user="arklink", password="pw", additions_iso="/x/VBoxGuestAdditions.iso"
    )[0]
    joined = " ".join(cmd)
    assert "unattended install" in joined
    assert "--iso=/tmp/u.iso" in joined and "--user=arklink" in joined and "--password=pw" in joined
    assert "--install-additions" in joined
    sudo = cmd[cmd.index("--post-install-command") + 1]
    assert "NOPASSWD:ALL" in sudo and "/etc/sudoers.d/arklink" in sudo
    assert "Asia/Shanghai" in joined
    # VBox 7.2 要求 hostname 带域名（裸名会 E_INVALIDARG）
    assert "--hostname=arklink.local" in joined


def test_create_sequence_ends_with_headless_start():
    """unattended install 只挂应答盘不开机——最后一步必须无头启动。"""
    mgr = EnvManager.open(
        "arklink", report=make_report(), runner=FakeRunner(), dry_run=True
    )
    cmds = mgr.commands_for_create("/tmp/u.iso", user="arklink", password="pw")
    last = " ".join(cmds[-1])
    assert "startvm arklink" in last and "--type headless" in last


def test_eject_detaches_dvd_and_boots_disk():
    cmds = eject_commands("arklink")
    assert any("--type dvddrive --medium none" in " ".join(c) for c in cmds)
    assert any("--boot1 disk" in " ".join(c) for c in cmds)


def test_push_commands_use_guestcontrol():
    copy = copyto_commands("arklink", "/src/ArkLink", "/opt/ArkLink",
                           user="arklink", password="pw")[0]
    assert copy[1:4] == ["guestcontrol", "arklink", "copyto"]
    assert "--recursive" in copy and "--password" in copy and "pw" in copy
    run = guest_run_commands("arklink", "sudo bash x", user="arklink", password="pw")[0]
    assert run[1:4] == ["guestcontrol", "arklink", "run"]


def test_push_creates_opt_dir_before_copy():
    """/opt 归 root，必须先 sudo 建目录授权，copyto 才不会 VERR_ACCESS_DENIED。"""
    mgr = EnvManager.open(
        "arklink", report=make_report(), runner=FakeRunner({"list vms": ""})
    )
    mgr.state.user = "arklink"
    mgr.push("/src/ArkLink", password="pw", script="setup_guest.sh")
    joins = [" ".join(c[1:]) for c in mgr.require_box().runner.calls]
    mkdir_idx = next(i for i, c in enumerate(joins)
                     if "mkdir -p /opt/ArkLink" in c and "chown" in c)
    # 必须递归 chown：上一次 setup 以 sudo 跑过的话，目录里还有 root 的
    # egg-info 之类老文件，copyto 覆盖它们会 VERR_ACCESS_DENIED
    assert "chown -R" in joins[mkdir_idx]
    copy_idx = next(i for i, c in enumerate(joins) if "copyto" in c)
    setup_idx = next(i for i, c in enumerate(joins) if "setup_guest.sh" in c)
    assert mkdir_idx < copy_idx < setup_idx


# ---------------------------------------------------------------- 执行
def test_vbox_replaces_executable_and_raises_on_error():
    runner = FakeRunner({
        "list vms": '"arklink" {2b3c4d5e-1111-2222-3333-444455556666}\n',
        "unregistervm": (1, "", "VM is locked"),
    })
    box = VBox(r"C:\VBox\VBoxManage.exe", runner=runner)
    with pytest.raises(VBoxError) as err:
        box.destroy("arklink")
    assert "VM is locked" in str(err.value)
    assert runner.calls[0][0] == r"C:\VBox\VBoxManage.exe", "argv[0] 要换成真实路径"


def test_vbox_status_parsing():
    runner = FakeRunner({
        "list vms": '"arklink" {2b3c4d5e-1111-2222-3333-444455556666}\n',
        "showvminfo": 'name="arklink"\nVMState="running"\n',
    })
    box = VBox("VBoxManage", runner=runner)
    assert box.exists("arklink")
    assert box.status("arklink") == "running"
    assert box.describe("arklink")["running"] is True


def test_vbox_status_missing_vm():
    box = VBox("VBoxManage", runner=FakeRunner({"list vms": ""}))
    assert box.status("arklink") == "missing"


def test_manager_dry_run_executes_nothing(tmp_path):
    runner = FakeRunner({"list vms": ""})
    report = make_report()
    mgr = EnvManager.open("arklink", report=report, runner=runner, dry_run=True)
    iso = tmp_path / "u.iso"
    iso.write_bytes(b"x")
    mgr.create(str(iso), user="arklink", password="pw")
    assert runner.calls == [], "dry-run 不许碰 VirtualBox"
    assert not mgr.state.installed


def test_manager_create_requires_existing_iso():
    mgr = EnvManager.open("arklink", report=make_report(), runner=FakeRunner(), dry_run=True)
    with pytest.raises(FileNotFoundError):
        mgr.create("/nope/u.iso", user="arklink", password="pw")


def test_manager_refuses_create_without_plan_ok():
    mgr = EnvManager.open("arklink", report=make_report(vbox=""), runner=FakeRunner())
    with pytest.raises(RuntimeError) as err:
        mgr.create("/tmp/u.iso", user="arklink", password="pw")
    assert "VBoxManage" in str(err.value)


def test_manager_destroy_requires_confirmation():
    mgr = EnvManager.open("arklink", report=make_report(), runner=FakeRunner(), dry_run=True)
    with pytest.raises(RuntimeError) as err:
        mgr.destroy()
    assert "--yes" in str(err.value)


def test_manager_status_reports_paths_and_ports():
    runner = FakeRunner({"list vms": '"arklink" {2b3c4d5e-1111-2222-3333-444455556666}\n',
                         "showvminfo": 'VMState="poweroff"\n'})
    mgr = EnvManager.open("arklink", report=make_report(), runner=runner)
    data = mgr.status()
    assert data["state"] == "poweroff" and data["exists"] is True
    assert data["webui"].startswith("http://127.0.0.1:")
    assert data["api"].endswith(":6900")
    assert data["disk_limit_gb"] == DISK_LIMIT_GB
    assert data["ssh"].startswith("ssh -p ")


# ---------------------------------------------------------------- 状态落盘
def test_state_roundtrip_and_mode(tmp_path):
    path = tmp_path / "env.json"
    state = EnvState(name="arklink", workdir="/x", vdi="/x/a.vdi", extra={"password": "pw"})
    state.save(path)
    loaded = EnvState.load(path)
    assert loaded is not None and loaded.name == "arklink"
    assert loaded.extra["password"] == "pw"
    if os.name != "nt":
        assert oct(path.stat().st_mode)[-3:] == "600", "配置里有密码，必须 0600"


def test_state_load_missing_returns_none(tmp_path):
    assert EnvState.load(tmp_path / "nope.json") is None


# ---------------------------------------------------------------- ISO
def test_resolve_iso_picks_highest_version():
    body = (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  *ubuntu-24.04.1-live-server-amd64.iso\n"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  *ubuntu-24.04.3-live-server-amd64.iso\n"
        "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc  *ubuntu-24.04.3-desktop-amd64.iso\n"
    ).encode()

    class Resp:
        headers: dict[str, str] = {}

        def read(self, *a):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    ref = resolve_iso(opener=lambda url, timeout: Resp())
    assert ref.name == "ubuntu-24.04.3-live-server-amd64.iso"
    assert ref.url.endswith(ref.name)
    assert ref.sha256.startswith("bbbb")


def test_download_iso_verifies_checksum(tmp_path):
    payload = b"iso-bytes"

    class Resp:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self) -> None:
            self.sent = False

        def read(self, n=-1):
            if self.sent:
                return b""
            self.sent = True
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    good = IsoRef("u.iso", "http://x/u.iso", sha256_of_bytes(payload))
    dest = tmp_path / "u.iso"
    download_iso(good, str(dest), opener=lambda url, timeout: Resp(), log=lambda *_: None)
    assert dest.read_bytes() == payload

    bad = IsoRef("u.iso", "http://x/u.iso", "0" * 64)
    with pytest.raises(RuntimeError) as err:
        download_iso(bad, str(tmp_path / "v.iso"), opener=lambda url, timeout: Resp(),
                     log=lambda *_: None)
    assert "校验失败" in str(err.value)
    assert not (tmp_path / "v.iso").exists()


def test_download_iso_resumes_from_part_file(tmp_path):
    """真机第一次下载在 6% 断了：必须留着 .part 带 Range 续传，不能从 0 重来。"""
    payload = b"0123456789abcdef"
    dest = tmp_path / "u.iso"
    part = tmp_path / "u.iso.part"
    part.write_bytes(payload[:8])  # 上次下了前一半

    seen: dict = {}

    class Resp:
        status = 206
        headers = {"Content-Length": str(len(payload) - 8)}

        def read(self, n=-1):
            if seen.get("done"):
                return b""
            seen["done"] = True
            return payload[8:]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=0):
        seen["range"] = dict(getattr(req, "headers", {})).get("Range")
        return Resp()

    download_iso(IsoRef("u.iso", "http://x/u.iso", sha256_of_bytes(payload)), str(dest),
                 opener=opener, log=lambda *_: None)
    assert seen["range"] == "bytes=8-"
    assert dest.read_bytes() == payload


def test_download_iso_retries_after_network_error(tmp_path):
    """断线算正常事件：重试一次能接上就不该让用户重跑命令。"""
    payload = b"abc"
    dest = tmp_path / "u.iso"
    attempts = {"n": 0}

    class Resp:
        status = 200
        headers = {"Content-Length": str(len(payload))}

        def read(self, n=-1):
            if attempts.get("read"):
                return b""
            attempts["read"] = True
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=0):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("connection reset by peer")
        return Resp()

    download_iso(IsoRef("u.iso", "http://x/u.iso", sha256_of_bytes(payload)), str(dest),
                 opener=opener, log=lambda *_: None)
    assert attempts["n"] >= 2 and dest.read_bytes() == payload


def test_find_local_iso(tmp_path):
    assert find_local_iso([str(tmp_path)]) == ""
    name = "ubuntu-24.04.3-live-server-amd64.iso"
    (tmp_path / name).write_bytes(b"x")
    assert find_local_iso([str(tmp_path)]).endswith(name)


def sha256_of_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- CLI
def test_cli_plan_json_shape(capsys):
    from arklink.__main__ import main

    code = main(["env", "plan", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["name"] == "arklink"
    assert data["disk_limit_gb"] == DISK_LIMIT_GB
    assert code in (0, 1), "能不能装都给 JSON，退出码区分"


def test_cli_destroy_without_yes_refuses(capsys):
    from arklink.__main__ import main

    code = main(["env", "destroy", "--dry-run"])
    assert code == 1
    assert "--yes" in capsys.readouterr().err


def test_destroy_powers_off_running_vm_first(monkeypatch):
    """运行中的机器是锁定的：不先关机，unregistervm 必然报
    VBOX_E_INVALID_OBJECT_STATE（真机第一次写卸载就翻在这）。"""
    runner = FakeRunner({
        "list vms": '"arklink" {2b3c4d5e-1111-2222-3333-444455556666}\n',
    })
    box = VBox(r"C:/VBox/VBoxManage.exe", runner=runner)
    states = ["running", "running", "poweroff"]
    monkeypatch.setattr(box, "status", lambda name: states.pop(0) if states else "poweroff")
    box.destroy("arklink")
    joins = [" ".join(c[1:]) for c in runner.calls]
    assert any("controlvm arklink acpipowerbutton" in j for j in joins)
    assert joins[-1].startswith("unregistervm arklink --delete")


def test_destroy_powers_off_running_vm_first(monkeypatch):
    """运行中的机器是锁定的：不先关机，unregistervm 必然报
    VBOX_E_INVALID_OBJECT_STATE（真机第一次写卸载就翻在这）。"""
    runner = FakeRunner({
        "list vms": '"arklink" {2b3c4d5e-1111-2222-3333-444455556666}\n',
    })
    box = VBox(r"C:\VBox\VBoxManage.exe", runner=runner)
    states = ["running", "running", "poweroff"]
    monkeypatch.setattr(box, "status", lambda name: states.pop(0) if states else "poweroff")
    box.destroy("arklink")
    joins = [" ".join(c[1:]) for c in runner.calls]
    assert any("controlvm arklink acpipowerbutton" in j for j in joins)
    assert joins[-1].startswith("unregistervm arklink --delete")


def test_download_iso_retries_from_scratch_after_checksum_mismatch(tmp_path):
    """断点续传可能混入坏数据：校验不过要**整包重下**，不是直接报死。"""
    payload = b"correct-bytes"

    class Resp:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self, bad: bool) -> None:
            self.bad = bad
            self.data = b"corrupted!!" if bad else payload

        def read(self, n=-1):
            out, self.data = self.data[:n], self.data[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    ref = IsoRef("u.iso", "http://x/u.iso", sha256_of_bytes(payload))
    calls = {"n": 0}

    def opener(url, timeout):
        calls["n"] += 1
        return Resp(calls["n"] == 1)

    dest = tmp_path / "u.iso"
    download_iso(ref, str(dest), opener=opener, log=lambda *_: None)
    assert dest.read_bytes() == payload
    # 第 1 轮：短流（11/13）→ 按中断保留；第 2 轮续上长度但内容混了坏字节
    # → 校验失败整包重下；第 3 轮拿到干净内容。关键是最终必须成功，
    # 不能像以前那样「校验失败直接报死」。
    assert calls["n"] >= 2


def test_resolve_qq_package_parses_official_cdn():
    """官网直链在 rainbow 配置 JS 里：解析失败等于部署断粮，必须测。"""
    from arklink.env import LINUXQQ_CONFIG_URL, resolve_qq_package

    cfg = (';(function(){var params= {"version":"3.2.35",'
           '"x64DownloadUrl":{"deb":"https://qqdl.gtimg.cn/qqfile/QQNTV2/9.9.37/'
           'release/816b3c79/QQ_3.2.35_261103_amd64_01.deb",'
           '"appimage":"https://qqdl.gtimg.cn/x.AppImage",'
           '"rpm":"https://qqdl.gtimg.cn/x.rpm"}};'
           'typeof callback_1888_config1 === "function" && callback_1888_config1(params)})()')

    class FakeOpener:
        def open(self, url, timeout=None):
            assert url == LINUXQQ_CONFIG_URL

            class Resp:
                def read(self, n=-1):
                    return cfg.encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return Resp()

    ref = resolve_qq_package(opener=FakeOpener())
    assert ref.name == "QQ_3.2.35_261103_amd64_01.deb"
    assert ref.url.startswith("https://qqdl.gtimg.cn/")
    assert ref.sha256 == "", "官方 CDN 没公布 sha256，不该瞎填"


def test_download_iso_retries_from_scratch_after_checksum_mismatch(tmp_path):
    """断点续传可能混入坏数据：校验不过要**整包重下**，不是直接报死。"""
    payload = b"correct-bytes"

    class Resp:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self, bad: bool) -> None:
            self.bad = bad
            self.data = b"corrupted!!" if bad else payload

        def read(self, n=-1):
            out, self.data = self.data[:n], self.data[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    ref = IsoRef("u.iso", "http://x/u.iso", sha256_of_bytes(payload))
    calls = {"n": 0}

    def opener(url, timeout):
        calls["n"] += 1
        return Resp(calls["n"] == 1)

    dest = tmp_path / "u.iso"
    download_iso(ref, str(dest), opener=opener, log=lambda *_: None)
    assert dest.read_bytes() == payload
    # 第 1 轮：短流（11/13）→ 按中断保留；第 2 轮续上长度但内容混了坏字节
    # → 校验失败整包重下；第 3 轮拿到干净内容。关键是最终必须成功，
    # 不能像以前那样「校验失败直接报死」。
    assert calls["n"] >= 2


def test_resolve_qq_package_parses_official_cdn():
    """官网直链在 rainbow 配置 JS 里：解析失败等于部署断粮，必须测。"""
    from arklink.env import LINUXQQ_CONFIG_URL, resolve_qq_package

    cfg = (';(function(){var params= {"version":"3.2.35",'
           '"x64DownloadUrl":{"deb":"https://qqdl.gtimg.cn/qqfile/QQNTV2/9.9.37/'
           'release/816b3c79/QQ_3.2.35_261103_amd64_01.deb",'
           '"appimage":"https://qqdl.gtimg.cn/x.AppImage",'
           '"rpm":"https://qqdl.gtimg.cn/x.rpm"}};'
           'typeof callback_1888_config1 === "function" && callback_1888_config1(params)})()')

    class FakeOpener:
        def open(self, url, timeout=None):
            assert url == LINUXQQ_CONFIG_URL

            class Resp:
                def read(self, n=-1):
                    return cfg.encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return Resp()

    ref = resolve_qq_package(opener=FakeOpener())
    assert ref.name == "QQ_3.2.35_261103_amd64_01.deb"
    assert ref.url.startswith("https://qqdl.gtimg.cn/")
    assert ref.sha256 == "", "官方 CDN 没公布 sha256，不该瞎填"


def test_download_iso_keeps_progress_when_server_ignores_range(tmp_path):
    """加速镜像站（ghproxy 类）不支持 Range，回 200 全量流。

    之前直接从头写：每次中断都清零，177MB 永远下不完（真机踩过）。
    现在要把已下过的字节跳过，往 .part 后面续。
    """
    payload = b"0123456789abcdef" * 2  # 32 字节

    class Resp:
        def __init__(self, offset: int) -> None:
            self.data = payload[offset:]  # 模拟 200 全量流
            self.headers = {"Content-Length": str(len(self.data))}

        def read(self, n=-1):
            out, self.data = self.data[:n], self.data[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    ref = IsoRef("u.iso", "http://x/u.iso", sha256_of_bytes(payload))
    dest = tmp_path / "u.iso"
    part = dest.with_suffix(".iso.part")
    part.write_bytes(payload[:10])  # 假设已下 10 字节

    def opener(url, timeout):
        # 请求带了 Range 头，但服务器忽略它：返回 200 + 完整流
        return Resp(0)

    download_iso(ref, str(dest), opener=opener, log=lambda *_: None)
    assert dest.read_bytes() == payload


def test_download_iso_keeps_progress_when_server_ignores_range(tmp_path):
    """加速镜像站（ghproxy 类）不支持 Range，回 200 全量流。

    之前直接从头写：每次中断都清零，177MB 永远下不完（真机踩过）。
    现在要把已下过的字节跳过，往 .part 后面续。
    """
    payload = b"0123456789abcdef" * 2  # 32 字节

    class Resp:
        def __init__(self, offset: int) -> None:
            self.data = payload[offset:]  # 模拟 200 全量流
            self.headers = {"Content-Length": str(len(self.data))}

        def read(self, n=-1):
            out, self.data = self.data[:n], self.data[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    ref = IsoRef("u.iso", "http://x/u.iso", sha256_of_bytes(payload))
    dest = tmp_path / "u.iso"
    part = dest.with_suffix(".iso.part")
    part.write_bytes(payload[:10])  # 假设已下 10 字节

    def opener(url, timeout):
        # 请求带了 Range 头，但服务器忽略它：返回 200 + 完整流
        return Resp(0)

    download_iso(ref, str(dest), opener=opener, log=lambda *_: None)
    assert dest.read_bytes() == payload
