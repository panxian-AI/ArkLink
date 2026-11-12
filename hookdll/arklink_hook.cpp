// ArkLink hook DLL —— QQ 进程内的「管道服务端」骨架。
//
// 为什么要有这个东西：现成的 hook 实现把 DLL 私有化了，但「注入 + 命名管道 + 帧
// 协议」这三层是可以自己写死的部分。本文件只负责这三层，业务 hook（收发消息）
// 留在 AttachMessageHooks() 里，等真机定位好 wrapper.node 的收发函数再填。
//
// 已实现：
//   * DllMain 起线程，创建 \\.\pipe\arklink.<pid>.control / .recv 两个管道
//   * 客户端接入后立刻发 HELLO（op=1，msg=管道名，value0=pid）
//   * 收到 REQUEST（op=2）：回 ACK（op=3），按需回 REPLY（op=4）
//   * 内置 arklink.ping / arklink.status 两个诊断命令
//   * 尽力而为的登录态探测（扫 QQ 数据目录里最近使用的 uin），发 LOGIN_STATE（op=7）
//   * ArkLinkPrepareUnload 导出：停监听、放线程，供远程 FreeLibrary 前调用
//
// 未实现（故意留白，见 docs/进程注入通道.md）：
//   * 真正抓 SSO 包 —— 需要定位 wrapper.node 内部的收发函数，版本相关
//   * 发送消息 —— 依赖上一条
#include <windows.h>
#include <shlobj.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

constexpr uint32_t kMagic = 0x31504851;  // "QHP1"
constexpr uint16_t kVersion = 1;
constexpr size_t kHeader = 40;

constexpr uint16_t OP_HELLO = 1;
constexpr uint16_t OP_REQUEST = 2;
constexpr uint16_t OP_ACK = 3;
constexpr uint16_t OP_REPLY = 4;
constexpr uint16_t OP_ERROR = 5;
constexpr uint16_t OP_PACKET = 6;
constexpr uint16_t OP_LOGIN_STATE = 7;
constexpr uint16_t OP_CAPABILITY = 22;

constexpr uint32_t FLAG_WANT_REPLY = 1;
constexpr uint32_t FLAG_LOGGED_IN = 4;

constexpr const char* kBuildVersion = "0.1.0";

volatile LONG g_stop = 0;
HANDLE g_control_pipe = INVALID_HANDLE_VALUE;
HANDLE g_recv_pipe = INVALID_HANDLE_VALUE;
HANDLE g_threads[2] = {nullptr, nullptr};
DWORD g_started_at = 0;

// ---------------------------------------------------------------- 字节序
inline void put16(char* p, uint16_t v) { memcpy(p, &v, 2); }
inline void put32(char* p, uint32_t v) { memcpy(p, &v, 4); }
inline uint16_t get16(const char* p) { uint16_t v; memcpy(&v, p, 2); return v; }
inline uint32_t get32(const char* p) { uint32_t v; memcpy(&v, p, 4); return v; }
inline uint64_t get64(const char* p) { uint64_t v; memcpy(&v, p, 8); return v; }

struct Frame {
    uint16_t op = 0;
    uint32_t request_id = 0;
    int32_t status = 0;
    uint32_t flags = 0;
    uint64_t value0 = 0;
    std::string cmd;
    std::string msg;
    std::string body;
};

std::string encode(const Frame& f) {
    std::string out(kHeader, '\0');
    char* p = &out[0];
    put32(p + 0, kMagic);
    put16(p + 4, kVersion);
    put16(p + 6, f.op);
    put32(p + 8, f.request_id);
    put32(p + 12, static_cast<uint32_t>(f.status));
    put32(p + 16, f.flags);
    put32(p + 20, static_cast<uint32_t>(f.cmd.size()));
    put32(p + 24, static_cast<uint32_t>(f.msg.size()));
    put32(p + 28, static_cast<uint32_t>(f.body.size()));
    memcpy(p + 32, &f.value0, 8);
    out += f.cmd;
    out += f.msg;
    out += f.body;
    return out;
}

bool read_exact(HANDLE pipe, char* buf, DWORD need) {
    DWORD got = 0;
    DWORD done = 0;
    while (done < need) {
        if (!ReadFile(pipe, buf + done, need - done, &got, nullptr) || got == 0) return false;
        done += got;
    }
    return true;
}

bool write_all(HANDLE pipe, const std::string& data) {
    DWORD wrote = 0;
    size_t done = 0;
    while (done < data.size()) {
        if (!WriteFile(pipe, data.data() + done, static_cast<DWORD>(data.size() - done), &wrote, nullptr) ||
            wrote == 0) {
            return false;
        }
        done += wrote;
    }
    return true;
}

bool read_frame(HANDLE pipe, Frame& out) {
    char head[kHeader];
    if (!read_exact(pipe, head, kHeader)) return false;
    if (get32(head) != kMagic || get16(head + 4) != kVersion) return false;
    out.op = get16(head + 6);
    out.request_id = get32(head + 8);
    out.status = static_cast<int32_t>(get32(head + 12));
    out.flags = get32(head + 16);
    const uint32_t cmd_len = get32(head + 20);
    const uint32_t msg_len = get32(head + 24);
    const uint32_t body_len = get32(head + 28);
    out.value0 = get64(head + 32);
    if (cmd_len > 4096 || msg_len > 65536 || body_len > 16u * 1024 * 1024) return false;
    std::string payload(cmd_len + msg_len + body_len, '\0');
    if (!payload.empty() && !read_exact(pipe, &payload[0], static_cast<DWORD>(payload.size()))) return false;
    out.cmd = payload.substr(0, cmd_len);
    out.msg = payload.substr(cmd_len, msg_len);
    out.body = payload.substr(cmd_len + msg_len);
    return true;
}

// ---------------------------------------------------------------- 诊断
std::string json_escape(const std::string& s) {
    std::string out;
    for (char c : s) {
        if (c == '"' || c == '\\') {
            out += '\\';
            out += c;
        } else if (c == '\n') {
            out += "\\n";
        } else if (static_cast<unsigned char>(c) >= 0x20) {
            out += c;
        }
    }
    return out;
}

// 尽力而为：QQ 把账号数据放在
//   %USERPROFILE%\Documents\Tencent Files\<uin>\nt_qq\
// 老版本还有 <uin>\ 直接带数据的。取「含 nt_qq 子目录 + 修改时间最新」的纯数字目录
// 当当前登录账号。拿不到就报未登录，绝不猜一个数字出来。
bool has_nt_qq(const std::string& account_dir) {
    const std::string probe = account_dir + "\\nt_qq";
    const DWORD attr = GetFileAttributesA(probe.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY);
}

std::string detect_uin() {
    char docs[MAX_PATH] = {0};
    if (FAILED(SHGetFolderPathA(nullptr, CSIDL_PERSONAL, nullptr, 0, docs))) return "";
    const std::string base = std::string(docs) + "\\Tencent Files";
    const std::string pattern = base + "\\*";
    WIN32_FIND_DATAA data;
    HANDLE find = FindFirstFileA(pattern.c_str(), &data);
    if (find == INVALID_HANDLE_VALUE) return "";
    std::string best;
    FILETIME best_time{};
    bool best_has_db = false;
    do {
        if (!(data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        const std::string name = data.cFileName;
        if (name.empty() || name == "." || name == "..") continue;
        bool digits = true;
        for (char c : name) {
            if (c < '0' || c > '9') {
                digits = false;
                break;
            }
        }
        if (!digits) continue;
        const std::string full = base + "\\" + name;
        const bool with_db = has_nt_qq(full);
        // 有 nt_qq 子目录的优先；同为「有库」时比修改时间
        if (!best.empty() && best_has_db && !with_db) continue;
        if (!best.empty() && best_has_db == with_db &&
            CompareFileTime(&data.ftLastWriteTime, &best_time) <= 0) {
            continue;
        }
        best = name;
        best_time = data.ftLastWriteTime;
        best_has_db = with_db;
    } while (FindNextFileA(find, &data));
    FindClose(find);
    return best;
}

// ---------------------------------------------------------------- 业务占位
// 真正的 hook 点：wrapper.node 里的收发函数。返回 false 表示「没接管消息流」，
// 上层据此就知道这个 DLL 只能做管道握手，不会误以为能收消息。
bool AttachMessageHooks() { return false; }

// ---------------------------------------------------------------- 管道服务
void send_login_state(HANDLE pipe) {
    const std::string uin = detect_uin();
    Frame f;
    f.op = OP_LOGIN_STATE;
    f.status = uin.empty() ? 0 : 1;
    f.flags = uin.empty() ? 0 : FLAG_LOGGED_IN;
    f.value0 = uin.empty() ? 0 : strtoull(uin.c_str(), nullptr, 10);
    f.msg = uin;
    write_all(pipe, encode(f));
}

std::string handle_request(const Frame& req, DWORD pid) {
    if (req.cmd == "arklink.ping") {
        char buf[128];
        snprintf(buf, sizeof(buf), "{\"pong\":true,\"uptime\":%lu}",
                 static_cast<unsigned long>((GetTickCount() - g_started_at) / 1000));
        return buf;
    }
    if (req.cmd == "arklink.status") {
        const std::string uin = detect_uin();
        char buf[512];
        snprintf(buf, sizeof(buf),
                 "{\"name\":\"arklink-hook\",\"version\":\"%s\",\"pid\":%lu,\"uin\":\"%s\","
                 "\"hooks\":%s,\"caps\":[\"pipe\"]}",
                 kBuildVersion, static_cast<unsigned long>(pid), json_escape(uin).c_str(),
                 AttachMessageHooks() ? "true" : "false");
        return buf;
    }
    return "";
}

// 服务端只有一个客户端：连上 → 握手 → 对话 → 断开 → 重建，保证单实例语义。
// 与现成 hook 的行为一致：管道被占时第二个客户端会拿到 ERROR_PIPE_BUSY。
void serve_pipe(const std::wstring& name, bool recv_pipe) {
    const DWORD pid = GetCurrentProcessId();
    while (!g_stop) {
        HANDLE pipe = CreateNamedPipeW(
            name.c_str(), PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1, 64 * 1024, 64 * 1024, 0, nullptr);
        if (pipe == INVALID_HANDLE_VALUE) {
            Sleep(200);
            continue;
        }
        if (recv_pipe) {
            g_recv_pipe = pipe;
        } else {
            g_control_pipe = pipe;
        }
        const BOOL connected = ConnectNamedPipe(pipe, nullptr) ? TRUE : (GetLastError() == ERROR_PIPE_CONNECTED);
        if (!connected || g_stop) {
            CloseHandle(pipe);
            continue;
        }

        Frame hello;
        hello.op = OP_HELLO;
        hello.msg = std::string(name.begin(), name.end());  // 仅诊断用；下面用窄串重写
        {
            // 管道名是宽字符，这里按 ASCII 转一次（路径都是 ASCII）
            std::string narrow;
            for (wchar_t ch : name) narrow += static_cast<char>(ch & 0xFF);
            hello.msg = narrow;
        }
        hello.value0 = pid;
        write_all(pipe, encode(hello));
        send_login_state(pipe);
        Frame cap;
        cap.op = OP_CAPABILITY;
        cap.status = 0;  // 暂不支持「客户端私聊上报」
        write_all(pipe, encode(cap));

        Frame req;
        while (!g_stop && read_frame(pipe, req)) {
            if (req.op != OP_REQUEST) continue;
            Frame ack;
            ack.op = OP_ACK;
            ack.request_id = req.request_id;
            write_all(pipe, encode(ack));
            if (!(req.flags & FLAG_WANT_REPLY)) continue;
            const std::string body = handle_request(req, pid);
            Frame reply;
            reply.op = OP_REPLY;
            reply.request_id = req.request_id;
            if (body.empty() && req.cmd.rfind("arklink.", 0) == 0) {
                reply.status = -1;
                reply.msg = "unknown arklink command: " + req.cmd;
            }
            reply.body = body;
            write_all(pipe, encode(reply));
        }
        FlushFileBuffers(pipe);
        DisconnectNamedPipe(pipe);
        CloseHandle(pipe);
        if (recv_pipe) {
            g_recv_pipe = INVALID_HANDLE_VALUE;
        } else {
            g_control_pipe = INVALID_HANDLE_VALUE;
        }
    }
}

DWORD WINAPI control_thread(LPVOID) {
    wchar_t name[128];
    swprintf(name, 128, L"\\\\.\\pipe\\arklink.%lu.control", static_cast<unsigned long>(GetCurrentProcessId()));
    serve_pipe(name, false);
    return 0;
}

DWORD WINAPI recv_thread(LPVOID) {
    wchar_t name[128];
    swprintf(name, 128, L"\\\\.\\pipe\\arklink.%lu.recv", static_cast<unsigned long>(GetCurrentProcessId()));
    serve_pipe(name, true);
    return 0;
}

void start_once() {
    static LONG started = 0;
    if (InterlockedCompareExchange(&started, 1, 0) != 0) return;
    g_started_at = GetTickCount();
    AttachMessageHooks();
    g_threads[0] = CreateThread(nullptr, 0, control_thread, nullptr, 0, nullptr);
    g_threads[1] = CreateThread(nullptr, 0, recv_thread, nullptr, 0, nullptr);
}

}  // namespace

// 卸载前调用：停监听 + 踢掉阻塞的 ConnectNamedPipe/ReadFile。
// 远程 FreeLibrary 之前先 CreateRemoteThread 跑这个导出，否则加载器锁里等线程会卡。
// 关键：必须等两个工作线程真的退出，调用方才能 FreeLibrary。
// 少了这一步，模块代码段先被卸载、线程还在跑，下一次 ReadFile 就会跳进已释放内存，
// 表现是目标进程直接消失（我们踩过）。
extern "C" __declspec(dllexport) void ArkLinkPrepareUnload() {
    InterlockedExchange(&g_stop, 1);
    if (g_control_pipe != INVALID_HANDLE_VALUE) {
        CancelIoEx(g_control_pipe, nullptr);
        CloseHandle(g_control_pipe);
        g_control_pipe = INVALID_HANDLE_VALUE;
    }
    if (g_recv_pipe != INVALID_HANDLE_VALUE) {
        CancelIoEx(g_recv_pipe, nullptr);
        CloseHandle(g_recv_pipe);
        g_recv_pipe = INVALID_HANDLE_VALUE;
    }
    HANDLE waiting[2] = {nullptr, nullptr};
    DWORD count = 0;
    for (HANDLE thread : g_threads) {
        if (thread) waiting[count++] = thread;
    }
    if (count) WaitForMultipleObjects(count, waiting, TRUE, 3000);
}

extern "C" __declspec(dllexport) const char* ArkLinkHookVersion() { return kBuildVersion; }

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
        start_once();
    } else if (reason == DLL_PROCESS_DETACH) {
        // 这里不能等线程（加载器锁），只把阻塞中的 ConnectNamedPipe/ReadFile 弄醒，
        // 剩下交给线程自己看到 g_stop 后退出。
        InterlockedExchange(&g_stop, 1);
        if (g_control_pipe != INVALID_HANDLE_VALUE) {
            CloseHandle(g_control_pipe);
            g_control_pipe = INVALID_HANDLE_VALUE;
        }
        if (g_recv_pipe != INVALID_HANDLE_VALUE) {
            CloseHandle(g_recv_pipe);
            g_recv_pipe = INVALID_HANDLE_VALUE;
        }
    }
    return TRUE;
}
