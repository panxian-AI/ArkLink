// ArkLink hook 模块（Linux / macOS 版）。
//
// 与 Windows 版（arklink_hook.cpp）的区别只在「进程间通信」这一层：
//   Windows: 命名管道 \\.\pipe\arklink.<pid>.control
//   Linux:   Unix domain socket  <runtime_dir>/arklink.<pid>.control
// 帧协议、op 语义、握手顺序完全一致，所以外部 arkLink 客户端不用区分平台。
//
// 编译：
//   g++ -shared -fPIC -std=c++17 -O2 -pthread -o libarklink-hook-x64.so arklink_hook_posix.cpp
//
// 注意：这里**没有**任何对 QQ 进程的 hook 逻辑，只负责「管道 + 自检」。
// 真正的消息分发 hook 要在拿到客户端符号后接在 recv 管道的推送里，
// 见 docs/Linux适配.md 的三条路线。

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

#include <dirent.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace {

constexpr uint32_t kMagic = 0x31504851;  // "QHP1"
constexpr uint16_t kVersion = 1;

constexpr uint16_t kOpHello = 1;
constexpr uint16_t kOpRequest = 2;
constexpr uint16_t kOpAck = 3;
constexpr uint16_t kOpReply = 4;
constexpr uint16_t kOpError = 5;
constexpr uint16_t kOpLoginState = 7;

#pragma pack(push, 1)
struct FrameHeader {
    uint32_t magic;
    uint16_t version;
    uint16_t op;
    uint32_t request_id;
    int32_t status;
    uint32_t flags;
    uint32_t cmd_len;
    uint32_t msg_len;
    uint32_t body_len;
    uint64_t value0;
};
#pragma pack(pop)

static_assert(sizeof(FrameHeader) == 40, "帧头必须是 40 字节，与 hookproto.py 对齐");

std::atomic<bool> g_stop{false};
std::atomic<int> g_control_fd{-1};
std::atomic<int> g_recv_fd{-1};
pthread_t g_threads[2] = {0, 0};
int g_thread_count = 0;
std::string g_runtime_dir;

std::string runtime_dir() {
    const char* xdg = getenv("XDG_RUNTIME_DIR");
    if (xdg && *xdg) return xdg;
    return "/tmp";
}

bool write_all(int fd, const void* data, size_t size) {
    const char* p = static_cast<const char*>(data);
    while (size > 0) {
        ssize_t n = ::write(fd, p, size);
        if (n <= 0) return false;
        p += n;
        size -= static_cast<size_t>(n);
    }
    return true;
}

bool read_all(int fd, void* data, size_t size) {
    char* p = static_cast<char*>(data);
    while (size > 0) {
        ssize_t n = ::read(fd, p, size);
        if (n <= 0) return false;
        p += n;
        size -= static_cast<size_t>(n);
    }
    return true;
}

bool send_frame(int fd, uint16_t op, uint32_t request_id, int32_t status, uint32_t flags,
                const std::string& cmd, const std::string& msg, const std::string& body,
                uint64_t value0) {
    FrameHeader header{};
    header.magic = kMagic;
    header.version = kVersion;
    header.op = op;
    header.request_id = request_id;
    header.status = status;
    header.flags = flags;
    header.cmd_len = static_cast<uint32_t>(cmd.size());
    header.msg_len = static_cast<uint32_t>(msg.size());
    header.body_len = static_cast<uint32_t>(body.size());
    header.value0 = value0;
    if (!write_all(fd, &header, sizeof(header))) return false;
    if (!cmd.empty() && !write_all(fd, cmd.data(), cmd.size())) return false;
    if (!msg.empty() && !write_all(fd, msg.data(), msg.size())) return false;
    if (!body.empty() && !write_all(fd, body.data(), body.size())) return false;
    return true;
}

bool recv_frame(int fd, FrameHeader& header, std::string& cmd, std::string& msg, std::string& body) {
    if (!read_all(fd, &header, sizeof(header))) return false;
    if (header.magic != kMagic || header.version != kVersion) return false;
    // 防御：任何一段超过 8MB 都当协议错乱，直接断开，避免被恶意长度拖死
    if (header.cmd_len > 8u * 1024 * 1024 || header.msg_len > 8u * 1024 * 1024 ||
        header.body_len > 64u * 1024 * 1024) {
        return false;
    }
    cmd.assign(header.cmd_len, '\0');
    msg.assign(header.msg_len, '\0');
    body.assign(header.body_len, '\0');
    if (header.cmd_len && !read_all(fd, &cmd[0], header.cmd_len)) return false;
    if (header.msg_len && !read_all(fd, &msg[0], header.msg_len)) return false;
    if (header.body_len && !read_all(fd, &body[0], header.body_len)) return false;
    return true;
}

// QQ Linux 版把账号目录放在 ~/.local/share/tencent-qq/<uin>（新版）或 ~/.config/QQ/<uin>。
// 取修改时间最新的数字目录当「当前登录」，与 Windows 版的判断一致：拿不到就报未登录。
std::string detect_uin() {
    const char* home = getenv("HOME");
    if (!home || !*home) return "";
    const char* bases[] = {"/.local/share/tencent-qq", "/.config/QQ", "/.local/share/QQ"};
    std::string best;
    time_t best_time = 0;
    for (const char* suffix : bases) {
        std::string base = std::string(home) + suffix;
        DIR* dir = opendir(base.c_str());
        if (!dir) continue;
        while (dirent* entry = readdir(dir)) {
            const char* name = entry->d_name;
            if (name[0] == '.') continue;
            bool digits = true;
            for (const char* p = name; *p; ++p) {
                if (*p < '0' || *p > '9') {
                    digits = false;
                    break;
                }
            }
            if (!digits) continue;
            std::string full = base + "/" + name;
            struct stat st{};
            if (stat(full.c_str(), &st) != 0 || !S_ISDIR(st.st_mode)) continue;
            if (best.empty() || st.st_mtime > best_time) {
                best_time = st.st_mtime;
                best = name;
            }
        }
        closedir(dir);
    }
    return best;
}

int make_listener(const std::string& name) {
    int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::string path = g_runtime_dir + "/" + name;
    if (path.size() >= sizeof(addr.sun_path)) {
        ::close(fd);
        return -1;
    }
    std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
    ::unlink(path.c_str());  // 上次异常退出的残留
    if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        ::close(fd);
        return -1;
    }
    if (::listen(fd, 4) != 0) {
        ::close(fd);
        return -1;
    }
    return fd;
}

void handle_control_client(int client) {
    std::string hello_msg = "arklink." + std::to_string(getpid()) + ".control";
    send_frame(client, kOpHello, 0, 0, 0, "", hello_msg, "", static_cast<uint64_t>(getpid()));

    std::string uin = detect_uin();
    if (!uin.empty()) {
        send_frame(client, kOpLoginState, 0, 0, 4, "", uin, "", 0);
    }

    FrameHeader header{};
    std::string cmd, msg, body;
    while (!g_stop.load() && recv_frame(client, header, cmd, msg, body)) {
        if (header.op == kOpRequest) {
            send_frame(client, kOpAck, header.request_id, 0, 0, "", "", "", 0);
            if (cmd == "arklink.ping") {
                send_frame(client, kOpReply, header.request_id, 0, 0, "pong", "", "", 0);
            } else if (cmd == "arklink.status") {
                std::string state = "pid=" + std::to_string(getpid()) +
                                    " uin=" + (uin.empty() ? "-" : uin) +
                                    " platform=posix";
                send_frame(client, kOpReply, header.request_id, 0, 0, "", state, "", 0);
            } else {
                send_frame(client, kOpError, header.request_id, 404, 0, cmd,
                           "这个平台的 hook 模块还没实现该命令", "", 0);
            }
        }
    }
    ::close(client);
}

void* control_thread(void*) {
    int listener = g_control_fd.load();
    while (!g_stop.load() && listener >= 0) {
        int client = ::accept(listener, nullptr, nullptr);
        if (client < 0) {
            if (g_stop.load()) break;
            continue;
        }
        handle_control_client(client);
        listener = g_control_fd.load();
    }
    return nullptr;
}

void* recv_thread(void*) {
    int listener = g_recv_fd.load();
    while (!g_stop.load() && listener >= 0) {
        int client = ::accept(listener, nullptr, nullptr);
        if (client < 0) {
            if (g_stop.load()) break;
            continue;
        }
        std::string hello_msg = "arklink." + std::to_string(getpid()) + ".recv";
        send_frame(client, kOpHello, 0, 0, 0, "", hello_msg, "", static_cast<uint64_t>(getpid()));
        // 真正的消息推送在这里：拿到客户端消息分发 hook 后，把 protobuf 原样
        // 用 kOpPacket 帧写进 client。当前只维持连接，让外部能验证链路。
        char sink[4096];
        while (!g_stop.load() && ::read(client, sink, sizeof(sink)) > 0) {
        }
        ::close(client);
        listener = g_recv_fd.load();
    }
    return nullptr;
}

void start_once() {
    g_runtime_dir = runtime_dir();
    std::string pid = std::to_string(getpid());
    g_control_fd.store(make_listener("arklink." + pid + ".control"));
    g_recv_fd.store(make_listener("arklink." + pid + ".recv"));
    if (g_control_fd.load() >= 0) {
        pthread_create(&g_threads[g_thread_count++], nullptr, control_thread, nullptr);
    }
    if (g_recv_fd.load() >= 0) {
        pthread_create(&g_threads[g_thread_count++], nullptr, recv_thread, nullptr);
    }
}

}  // namespace

extern "C" __attribute__((visibility("default"))) const char* ArkLinkHookVersion() {
    return "arklink-posix-1";
}

// 与 Windows 版同名：先停线程、关 socket，再让调用方 dlclose。
// 少了这一步，dlclose 撤掉代码段时线程还在 read，下一次返回就跳进已释放内存。
extern "C" __attribute__((visibility("default"))) void ArkLinkPrepareUnload() {
    g_stop.store(true);
    int control = g_control_fd.exchange(-1);
    if (control >= 0) {
        ::shutdown(control, SHUT_RDWR);
        ::close(control);
    }
    int recv_fd = g_recv_fd.exchange(-1);
    if (recv_fd >= 0) {
        ::shutdown(recv_fd, SHUT_RDWR);
        ::close(recv_fd);
    }
    for (int i = 0; i < g_thread_count; ++i) {
        if (g_threads[i]) pthread_join(g_threads[i], nullptr);
    }
    g_thread_count = 0;
    std::string pid = std::to_string(getpid());
    ::unlink((g_runtime_dir + "/arklink." + pid + ".control").c_str());
    ::unlink((g_runtime_dir + "/arklink." + pid + ".recv").c_str());
}

__attribute__((constructor)) static void on_load() {
    start_once();
}
