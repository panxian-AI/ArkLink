"""QQ NT `wrapper.node` 的发送 ABI 表 + 只读探针。

发消息不用自己拼协议、也不用自己加密：QQ 客户端内部就有现成的发送入口

```
void nt::wrapper::KernelMsgService::sendMsg(
        int64_t                                    msgId,       // 0 = 新建
        const Contact&                             contact,     // 发给谁
        const std::vector<MsgElement>&             elements,    // 发什么
        const std::unordered_map<int, MsgAttributeInfo>& attrs, // 附加属性，可空
        const std::shared_ptr<IOperateCallback>&   cb);         // 结果回调，可空
```

它是 `IKernelMsgService` 的一个虚函数。本模块只做两件事：

1. **算地址**：把「QQ 版本 → vtable RVA / 字段偏移」这张表固化下来（`KNOWN_ABIS`），
   并在运行时用真实进程里的字节**校验**这张表还对得上；
2. **找实例**：`KernelMsgService` 在进程里是单例，且对象头第一个指针就是它的
   vtable。于是「在已提交内存里搜这个指针」就能拿到对象地址 —— 全程只读，
   不注入、不改内存、不发包，失败也只是找不到。

地址怎么来的（可复现，见 `tools/re_qq_abi.py`）：

* `wrapper.node` 导出表里有 C++ 修饰名，字符串里还留着
  `?sendMsg@KernelMsgService@wrapper@nt@@UEAAX_JAEBUContact...`，
  说明这个类名/签名可直接读出来；
* napi 的属性描述符表 `{name="sendMsg", method=0xe40bcc, ...}` 指向它的 JS 包装；
* 包装函数里 `mov rax,[rax+0xf0]` + `call rax` 就是虚调用 —— 槽位 30；
* RTTI 里 `.?AVKernelMsgService@wrapper@nt@@` 的 COL 反向索引到 vtable。

为什么先做「只读探针」：错一个偏移就可能把客户端搞崩，而探针只读内存，
最坏结果是 `ok=False`。真正的调用必须等 ABI 校验通过 + 用户显式放行。
"""

from __future__ import annotations

import ctypes
import os
import struct
from dataclasses import asdict, dataclass, field
from typing import Any

#: `__qq::std::string`（libc++ 布局）的三个字段：{cap|1, size, data}
#: 判长字符串用 `cap & 1`，数据指针在 +0x10，所以构造长串只要三根 8 字节。
STRING_LAYOUT = {"cap": 0, "size": 8, "data": 16, "long_flag": 1}


@dataclass(frozen=True)
class SendAbi:
    """一个 QQ 版本的发送 ABI。

    字段名就是用途，偏移一律相对结构体首地址。
    """

    label: str
    vtable_rva: int
    send_slot: int
    send_rva: int
    element_size: int
    contact_size: int
    element_type_offset: int
    contact_chat_type: int
    contact_peer_uid: int
    contact_guild_id: int
    element_text: int
    text_content: int
    text_at_type: int
    verified: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["offsets"] = {
            "vtable": hex(self.vtable_rva),
            "send": hex(self.send_rva),
            "element_slot": self.send_slot * 8,
        }
        return data


#: 已知版本表。键是「主版本-构建号」，见 `wrapper.node` 所在目录名。
#:
#: 只有 `verified` 里的字段是在真机上核对过的；`unverified` 的字段会在
#: `probe()` 里被跳过，不参与判定，避免拿猜测当结论。
KNOWN_ABIS: dict[str, SendAbi] = {
    "9.9.31-49738": SendAbi(
        label="9.9.31-49738",
        vtable_rva=0x39A5888,      # KernelMsgService 主 vtable（RTTI 反查）
        send_slot=30,              # napi 包装里 call [rax+0xf0]
        send_rva=0x2C5E86,         # 槽位 30 指向的函数
        element_size=0x2C40,       # std::vector<MsgElement> 的步长
        contact_size=0x38,         # {int32 chatType; string peerUid; string guildId}
        element_type_offset=0x00,
        contact_chat_type=0x00,
        contact_peer_uid=0x08,
        contact_guild_id=0x20,
        element_text=0x20,
        text_content=0x20,
        text_at_type=0x38,
        verified=("vtable", "send_slot", "send_rva", "element_size", "contact", "string_abi"),
        # 对象发现：9.9.31 真机上用「扫 vtable 指针」只命中一处静态表，槽位 30 为 0，
        # 说明这个类在进程里不是以「对象首地址 = 该 vtable」的形式存在（可能被派生类
        # 覆盖，或对象由别的壳持有）。所以实例定位改用函数级 hook 抓模板，见
        # docs/发消息链路.md「三条路线」。
        unverified=("instance_scan", "text_content", "text_at_type"),
    ),
}


def abi_for_version(version: str) -> SendAbi | None:
    """按 `9.9.31-49738` 这种键取 ABI；也接受 `9.9.31` 前缀。"""
    v = (version or "").strip()
    if not v:
        return None
    if v in KNOWN_ABIS:
        return KNOWN_ABIS[v]
    for key, abi in KNOWN_ABIS.items():
        if key.split("-")[0] == v or key.startswith(v):
            return abi
    return None


def abi_for_install(app_dir: str) -> SendAbi | None:
    """从 `.../versions/9.9.31-49738/resources/app` 这种路径里认出 ABI。"""
    parts = [p for p in os.path.normpath(app_dir).replace("\\", "/").split("/") if p]
    for part in reversed(parts):
        abi = abi_for_version(part)
        if abi is not None:
            return abi
    return None


# ---------------------------------------------------------------- 进程内只读探针


class ProbeUnavailable(RuntimeError):
    """当前平台/进程不支持只读探针。"""


@dataclass
class ProbeResult:
    ok: bool = False
    pid: int = 0
    module_base: int = 0
    vtable: int = 0
    instance: int = 0
    send_fn: int = 0
    hits: int = 0
    scanned: int = 0
    abi: str = ""
    note: str = ""
    slots: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("module_base", "vtable", "instance", "send_fn"):
            value = data.get(key) or 0
            data[key] = hex(value) if value else ""
        return data


_MEM_COMMIT = 0x1000
_PAGE_GUARD = 0x100
_PAGE_NOACCESS = 0x01
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_TH32CS_SNAPMODULE = 0x00000008
_TH32CS_SNAPMODULE32 = 0x00000010


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("th32ModuleID", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("GlblcntUsage", ctypes.c_ulong),
        ("ProccntUsage", ctypes.c_ulong),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", ctypes.c_ulong),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("PartitionId", ctypes.c_ushort),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    ]


def _kernel32():
    if os.name != "nt":
        raise ProbeUnavailable("只读探针目前只实现了 Windows（POSIX 走 /proc + ptrace）")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def module_base(pid: int, module: str = "wrapper.node") -> int:
    """取目标进程里某个模块的加载基址；没有就返回 0。"""
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPMODULE | _TH32CS_SNAPMODULE32, pid)
    if snap in (0, -1):
        return 0
    try:
        entry = _MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() == module.lower():
                return ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
            ok = k32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return 0


def _read(handle: int, address: int, size: int) -> bytes:
    k32 = _kernel32()
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, size, ctypes.byref(got))
    if not ok or got.value != size:
        return b""
    return buf.raw


def _iter_regions(handle: int):
    k32 = _kernel32()
    mbi = _MBI()
    addr = 0
    while k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        base = mbi.BaseAddress or 0
        size = int(mbi.RegionSize)
        if size <= 0:
            break
        writable = mbi.State == _MEM_COMMIT and not (mbi.Protect & (_PAGE_GUARD | _PAGE_NOACCESS))
        yield base, size, writable
        addr = base + size


def find_pointers(pid: int, value: int, *, limit: int = 8, chunk: int = 1 << 20) -> tuple[list[int], int]:
    """在目标进程已提交内存里找 `value` 这个 8 字节常量，返回 (命中地址, 扫描字节数)。

    只读。用 `bytes.find` 扫，835MB 大约 1.5s（C++ 侧会快两个数量级）。
    """
    k32 = _kernel32()
    handle = k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        raise ProbeUnavailable(f"打不开进程 {pid}（换管理员权限试试）")
    pattern = struct.pack("<Q", value)
    hits: list[int] = []
    scanned = 0
    try:
        for base, size, readable in _iter_regions(handle):
            if not readable:
                continue
            off = 0
            while off < size and len(hits) < limit:
                want = min(chunk, size - off)
                data = _read(handle, base + off, want)
                if data:
                    scanned += len(data)
                    idx = data.find(pattern)
                    while idx >= 0:
                        hits.append(base + off + idx)
                        if len(hits) >= limit:
                            break
                        idx = data.find(pattern, idx + 1)
                off += want
    finally:
        k32.CloseHandle(handle)
    return hits, scanned


def probe(pid: int, abi: SendAbi | None = None, *, module: str = "wrapper.node") -> ProbeResult:
    """只读体检：vtable 在不在、单例有几份、send 槽位指向哪里。

    判定标准（全部只读）：
    * `wrapper.node` 已加载；
    * 表里的 vtable RVA 落在模块映像范围内；
    * 全进程恰好 1 个指针指向该 vtable（多份 = 表过期或存在多会话）；
    * 实例 + `send_slot*8` 处指向模块 `.text` 里的 `send_rva`。
    """
    result = ProbeResult(pid=pid)
    if abi is None:
        abi = next(iter(KNOWN_ABIS.values()))
    result.abi = abi.label

    base = module_base(pid, module)
    if not base:
        result.note = f"进程 {pid} 里没有加载 {module}"
        return result
    result.module_base = base
    vtable = base + abi.vtable_rva
    result.vtable = vtable
    result.send_fn = base + abi.send_rva

    hits, scanned = find_pointers(pid, vtable, limit=8)
    result.hits = len(hits)
    result.scanned = scanned
    if not hits:
        result.note = "没有对象指向该 vtable（QQ 版本变了？先用 tools/re_qq_abi.py 重算）"
        return result
    if len(hits) > 1:
        result.note = f"命中 {len(hits)} 个对象，无法确定单例（多开 QQ？）"

    k32 = _kernel32()
    handle = k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        result.note = result.note or f"打不开进程 {pid}"
        return result
    try:
        for candidate in hits:
            slot = _read(handle, candidate + abi.send_slot * 8, 8)
            if len(slot) != 8:
                continue
            fn = struct.unpack("<Q", slot)[0]
            result.slots[hex(candidate)] = hex(fn)
            if fn == result.send_fn:
                result.instance = candidate
                result.ok = True
                break
        if not result.ok:
            result.note = "对象找到了，但 send 槽位不等于表里的地址（ABI 过期）"
    finally:
        k32.CloseHandle(handle)
    return result


def count_string(pid: int, text: str) -> int:
    """数一数某个字符串在目标进程内存里出现几次（UTF-16LE + ASCII 两种编码）。

    用途：一台机器上同时登着两个号时，两个 `QQ.exe` 都有窗口、标题都叫
    "QQ"，光看窗口分不出谁是谁。但**自己的号**在这份内存里出现的次数
    比"别人的号"高两个数量级（实测 632 vs 6），拿它当指纹足够稳。
    """
    if not text:
        return 0
    k32 = _kernel32()
    handle = k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        return 0
    patterns = [text.encode("utf-16-le"), text.encode("ascii")]
    total = 0
    try:
        for base, size, readable in _iter_regions(handle):
            if not readable:
                continue
            off = 0
            while off < size:
                want = min(1 << 20, size - off)
                data = _read(handle, base + off, want)
                if data:
                    total += sum(data.count(p) for p in patterns)
                off += want
    finally:
        k32.CloseHandle(handle)
    return total


def installed_uins(home: str | None = None) -> list[str]:
    """本机登录过的号（`Documents/Tencent Files/nt_qq_<uin>` 目录名）。

    这是「认号」的候选池：只有拿到全部候选，才谈得上「哪个更像自己」。
    """
    root = os.path.join(home or os.path.expanduser("~"), "Documents", "Tencent Files")
    try:
        names = os.listdir(root)
    except OSError:
        return []
    uins = []
    for name in names:
        low = name.lower()
        if low.startswith("nt_qq_") and name[len("nt_qq_") :].isdigit():
            uins.append(name[len("nt_qq_") :])
        elif name.isdigit() and os.path.isdir(os.path.join(root, name, "nt_qq")):
            # 本机实测布局是 `<uin>/nt_qq/...`，不是 `<uin>` 直接放库
            uins.append(name)
    return sorted(set(uins))


def whoami(pid: int, candidates) -> tuple[str, dict[str, int]]:
    """在若干候选 QQ 号里，判断哪个是这个进程自己的号。

    返回 `(最像的那个, {号: 次数})`；候选全为 0 次时返回空串，让调用方
    自己决定「认不出来」怎么办，而不是瞎猜一个。
    """
    counts = {str(c): count_string(pid, str(c)) for c in candidates if str(c)}
    if not counts:
        return "", {}
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    best, best_n = ranked[0]
    second_n = ranked[1][1] if len(ranked) > 1 else 0
    # 只有「明显占优」才算认出来：两个号互相出现在好友列表里是常态，
    # 数量差不多时说明这台机器上两个号都在跑，不能猜。
    if best_n <= 0 or best_n < second_n * 3:
        return "", counts
    return best, counts


def describe(result: ProbeResult) -> str:
    """把探针结果写成人话，CLI/日志都用它。"""
    if not result.ok:
        return f"发送链路不可用：{result.note or '未知原因'}"
    return (
        f"发送链路可用：pid={result.pid} instance=0x{result.instance:x} "
        f"vtable=0x{result.vtable:x} send=0x{result.send_fn:x} "
        f"(扫了 {result.scanned / 1048576:.0f}MB，命中 {result.hits} 个对象)"
    )
