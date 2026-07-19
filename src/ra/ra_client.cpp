// RetroAchievements prototype for No Mercy (ported from VPW2, softcore only).
// See ra_client.h for the
// env-var surface. Design notes:
//  - The recomp rdram buffer stores each 32-bit word in host (little-endian)
//    order holding the big-endian word's VALUE - the same layout mupen-family
//    cores expose to RetroAchievements on LE hosts, so the default peek is a
//    RAW byte copy; WCW_RA_SWAP=1 switches to XOR^3 byte addressing if a real
//    set ever proves the default wrong.
//  - rc_client is not thread-safe: every rc_client call happens on the single
//    RA thread. HTTP requests run on a WinHTTP worker; completed responses are
//    queued back and drained by the RA thread.
//  - Hardcore is explicitly disabled (prototype is softcore by design).
#include "ra_client.h"

#ifdef _WIN32

#include <windows.h>
#include <winhttp.h>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "rc_client.h"
#include "rc_runtime.h"
#include "rc_consoles.h"

namespace {

std::atomic<uint8_t*> g_rdram{nullptr};
std::atomic<bool> g_thread_started{false};
rc_client_t* g_client = nullptr;

constexpr uint32_t RDRAM_SIZE = 8 * 1024 * 1024;
bool g_swap_mode = false;  // WCW_RA_SWAP=1: XOR^3 byte addressing

// ---------------------------------------------------------------------------
// Memory peek
// ---------------------------------------------------------------------------

uint32_t peek_bytes(uint32_t address, uint8_t* buffer, uint32_t num_bytes) {
    uint8_t* rdram = g_rdram.load();
    if (rdram == nullptr) return 0;
    uint32_t copied = 0;
    for (uint32_t i = 0; i < num_bytes; i++) {
        uint32_t a = address + i;
        if (a >= RDRAM_SIZE) break;
        buffer[i] = g_swap_mode ? rdram[a ^ 3] : rdram[a];
        copied++;
    }
    return copied;
}

uint32_t client_read_memory(uint32_t address, uint8_t* buffer, uint32_t num_bytes,
                            rc_client_t*) {
    return peek_bytes(address, buffer, num_bytes);
}

unsigned runtime_peek(unsigned address, unsigned num_bytes, void*) {
    uint8_t buf[4] = {0, 0, 0, 0};
    if (num_bytes > 4) num_bytes = 4;
    peek_bytes(address, buf, num_bytes);
    // rcheevos assembles little-endian
    return (unsigned)buf[0] | ((unsigned)buf[1] << 8) | ((unsigned)buf[2] << 16) |
           ((unsigned)buf[3] << 24);
}

// ---------------------------------------------------------------------------
// HTTP (WinHTTP worker thread; responses queued back to the RA thread)
// ---------------------------------------------------------------------------

struct HttpJob {
    std::wstring host;
    std::wstring path;
    bool https = true;
    INTERNET_PORT port = 443;
    std::string post_data;      // empty = GET
    std::string content_type;
    rc_client_server_callback_t callback = nullptr;
    void* callback_data = nullptr;
    // filled by the worker:
    std::string body;
    int status = 0;
};

std::mutex g_http_mx;
std::deque<HttpJob*> g_http_pending;    // to worker
std::deque<HttpJob*> g_http_done;       // to RA thread
std::atomic<bool> g_http_worker_started{false};

void http_execute(HttpJob* job) {
    HINTERNET ses = WinHttpOpen(L"NoMercyRecompiled/0.1.0 (Windows) rcheevos",
                                WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                                WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!ses) { job->status = 0; return; }
    HINTERNET con = WinHttpConnect(ses, job->host.c_str(), job->port, 0);
    if (!con) { WinHttpCloseHandle(ses); job->status = 0; return; }
    const wchar_t* verb = job->post_data.empty() ? L"GET" : L"POST";
    HINTERNET req = WinHttpOpenRequest(con, verb, job->path.c_str(), nullptr,
                                       WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                       job->https ? WINHTTP_FLAG_SECURE : 0);
    if (!req) { WinHttpCloseHandle(con); WinHttpCloseHandle(ses); job->status = 0; return; }

    std::wstring headers;
    if (!job->post_data.empty()) {
        std::wstring ct(job->content_type.begin(), job->content_type.end());
        if (ct.empty()) ct = L"application/x-www-form-urlencoded";
        headers = L"Content-Type: " + ct;
    }
    BOOL ok = WinHttpSendRequest(
        req, headers.empty() ? WINHTTP_NO_ADDITIONAL_HEADERS : headers.c_str(),
        headers.empty() ? 0 : (DWORD)-1,
        job->post_data.empty() ? WINHTTP_NO_REQUEST_DATA : (LPVOID)job->post_data.data(),
        (DWORD)job->post_data.size(), (DWORD)job->post_data.size(), 0);
    if (ok) ok = WinHttpReceiveResponse(req, nullptr);
    if (ok) {
        DWORD status = 0, len = sizeof(status);
        WinHttpQueryHeaders(req, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                            WINHTTP_HEADER_NAME_BY_INDEX, &status, &len,
                            WINHTTP_NO_HEADER_INDEX);
        job->status = (int)status;
        for (;;) {
            DWORD avail = 0;
            if (!WinHttpQueryDataAvailable(req, &avail) || avail == 0) break;
            size_t off = job->body.size();
            job->body.resize(off + avail);
            DWORD got = 0;
            if (!WinHttpReadData(req, job->body.data() + off, avail, &got)) break;
            job->body.resize(off + got);
            if (got == 0) break;
        }
    } else {
        job->status = 0;
    }
    WinHttpCloseHandle(req);
    WinHttpCloseHandle(con);
    WinHttpCloseHandle(ses);
}

void http_worker() {
    for (;;) {
        HttpJob* job = nullptr;
        {
            std::lock_guard lk(g_http_mx);
            if (!g_http_pending.empty()) {
                job = g_http_pending.front();
                g_http_pending.pop_front();
            }
        }
        if (!job) { Sleep(20); continue; }
        http_execute(job);
        std::lock_guard lk(g_http_mx);
        g_http_done.push_back(job);
    }
}

void client_server_call(const rc_api_request_t* request,
                        rc_client_server_callback_t callback, void* callback_data,
                        rc_client_t*) {
    auto* job = new HttpJob();
    // crack the URL
    std::string url = request->url ? request->url : "";
    std::wstring wurl(url.begin(), url.end());
    URL_COMPONENTS uc{};
    uc.dwStructSize = sizeof(uc);
    wchar_t host[256]{}, path[1024]{};
    uc.lpszHostName = host; uc.dwHostNameLength = 255;
    uc.lpszUrlPath = path; uc.dwUrlPathLength = 1023;
    if (!WinHttpCrackUrl(wurl.c_str(), 0, 0, &uc)) {
        rc_api_server_response_t resp{};
        resp.http_status_code = 0;
        callback(&resp, callback_data);
        delete job;
        return;
    }
    job->host = host;
    job->path = path;
    job->https = uc.nScheme == INTERNET_SCHEME_HTTPS;
    job->port = uc.nPort;
    if (request->post_data) job->post_data = request->post_data;
    if (request->content_type) job->content_type = request->content_type;
    job->callback = callback;
    job->callback_data = callback_data;

    bool expected = false;
    if (g_http_worker_started.compare_exchange_strong(expected, true))
        std::thread(http_worker).detach();
    std::lock_guard lk(g_http_mx);
    g_http_pending.push_back(job);
}

// Drain completed HTTP jobs ON the RA thread (all rc_client callbacks stay
// single-threaded).
void drain_http_done() {
    for (;;) {
        HttpJob* job = nullptr;
        {
            std::lock_guard lk(g_http_mx);
            if (!g_http_done.empty()) {
                job = g_http_done.front();
                g_http_done.pop_front();
            }
        }
        if (!job) return;
        rc_api_server_response_t resp{};
        resp.body = job->body.c_str();
        resp.body_length = job->body.size();
        resp.http_status_code = job->status;
        if (job->callback) job->callback(&resp, job->callback_data);
        delete job;
    }
}

// ---------------------------------------------------------------------------
// rc_client wiring
// ---------------------------------------------------------------------------

void client_log(const char* message, const rc_client_t*) {
    fprintf(stderr, "[ra][rc] %s\n", message);
}

void client_event(const rc_client_event_t* event, rc_client_t*) {
    switch (event->type) {
    case RC_CLIENT_EVENT_ACHIEVEMENT_TRIGGERED:
        fprintf(stderr, "[ra] ACHIEVEMENT UNLOCKED: %s (%u pts) - %s\n",
                event->achievement->title, event->achievement->points,
                event->achievement->description);
        break;
    case RC_CLIENT_EVENT_GAME_COMPLETED:
        fprintf(stderr, "[ra] GAME MASTERED!\n");
        break;
    case RC_CLIENT_EVENT_SERVER_ERROR:
        fprintf(stderr, "[ra] server error: %s (%s)\n",
                event->server_error ? event->server_error->error_message : "?",
                event->server_error ? event->server_error->api : "?");
        break;
    default:
        break;
    }
}

const char* ach_state_name(uint8_t state) {
    switch (state) {
    case RC_CLIENT_ACHIEVEMENT_STATE_INACTIVE: return "inactive";
    case RC_CLIENT_ACHIEVEMENT_STATE_ACTIVE: return "active";
    case RC_CLIENT_ACHIEVEMENT_STATE_UNLOCKED: return "unlocked";
    case RC_CLIENT_ACHIEVEMENT_STATE_DISABLED: return "DISABLED";
    default: return "?";
    }
}

void dump_achievement_states(rc_client_t* client) {
    rc_client_achievement_list_t* list = rc_client_create_achievement_list(
        client, RC_CLIENT_ACHIEVEMENT_CATEGORY_CORE,
        RC_CLIENT_ACHIEVEMENT_LIST_GROUPING_PROGRESS);
    if (!list) return;
    for (uint32_t b = 0; b < list->num_buckets; b++) {
        for (uint32_t i = 0; i < list->buckets[b].num_achievements; i++) {
            const rc_client_achievement_t* a = list->buckets[b].achievements[i];
            fprintf(stderr, "[ra]   ach %u '%s' state=%s%s - %s\n", a->id, a->title,
                    ach_state_name(a->state),
                    a->unlocked ? " (unlocked-on-server)" : "",
                    a->description ? a->description : "");
        }
    }
    rc_client_destroy_achievement_list(list);
}

void load_game_callback(int result, const char* error_message, rc_client_t* client,
                        void*) {
    if (result != RC_OK) {
        fprintf(stderr, "[ra] game load failed: %s\n",
                error_message ? error_message : "?");
        return;
    }
    const rc_client_game_t* game = rc_client_get_game_info(client);
    fprintf(stderr, "[ra] game identified: %s (id %u)\n",
            game ? game->title : "?", game ? game->id : 0);
    rc_client_user_game_summary_t summary;
    rc_client_get_user_game_summary(client, &summary);
    fprintf(stderr, "[ra] achievements: %u core, %u unlocked\n",
            summary.num_core_achievements, summary.num_unlocked_achievements);
    dump_achievement_states(client);
}

void login_callback(int result, const char* error_message, rc_client_t* client, void*) {
    if (result != RC_OK) {
        fprintf(stderr, "[ra] login failed: %s\n", error_message ? error_message : "?");
        return;
    }
    const rc_client_user_t* user = rc_client_get_user_info(client);
    fprintf(stderr, "[ra] logged in as %s (%u points). Session token available -\n"
                    "[ra] set WCW_RA_TOKEN to it to avoid storing the password.\n",
            user->display_name, user->score);
    fprintf(stderr, "[ra] token: %s\n", user->token ? user->token : "?");
    const char* rom = getenv("WCW_RA_ROM");
    if (!rom) rom = getenv("WCW_AUTOBOOT");
    if (rom == nullptr || rom[0] == '\0' || strcmp(rom, "1") == 0) rom = "nomercy.z64";
    fprintf(stderr, "[ra] identifying ROM %s\n", rom);
    rc_client_begin_identify_and_load_game(client, RC_CONSOLE_NINTENDO_64, rom,
                                           nullptr, 0, load_game_callback, nullptr);
}

// ---------------------------------------------------------------------------
// Offline test trigger (WCW_RA_TEST=size:addr:value) - proves peek + per-frame
// evaluation with zero server involvement.
// ---------------------------------------------------------------------------

rc_runtime_t g_test_runtime;
bool g_test_active = false;
unsigned g_test_addr = 0, g_test_size = 8;

// WCW_RA_WATCH=addr,addr,... : log the 8-bit value at each address (through
// the active peek mode) whenever any of them changes.
std::vector<unsigned> g_watch;
std::vector<unsigned> g_watch_last;

void setup_watch() {
    const char* spec = getenv("WCW_RA_WATCH");
    if (!spec) return;
    char buf[512];
    snprintf(buf, sizeof(buf), "%s", spec);
    for (char* tok = strtok(buf, ","); tok; tok = strtok(nullptr, ",")) {
        unsigned a = (unsigned)strtoul(tok, nullptr, 0);
        g_watch.push_back(a);
        g_watch_last.push_back(0xFFFFFFFF);
    }
    fprintf(stderr, "[ra] watching %zu addresses\n", g_watch.size());
}

void poll_watch() {
    uint8_t* rdram = g_rdram.load();
    if (!rdram) return;
    bool changed = false;
    for (size_t i = 0; i < g_watch.size(); i++) {
        unsigned a = g_watch[i];
        // both interpretations packed: high byte = raw, low byte = xor3
        unsigned v = ((unsigned)rdram[a] << 8) | rdram[a ^ 3];
        if (v != g_watch_last[i]) { g_watch_last[i] = v; changed = true; }
    }
    if (!changed) return;
    std::string line = "[ra] watch(raw/x3):";
    char part[48];
    for (size_t i = 0; i < g_watch.size(); i++) {
        snprintf(part, sizeof(part), " %06X=%02X/%02X", g_watch[i],
                 g_watch_last[i] >> 8, g_watch_last[i] & 0xFF);
        line += part;
    }
    fprintf(stderr, "%s\n", line.c_str());
}

void test_event_handler(const rc_runtime_event_t* e) {
    if (e->type == RC_RUNTIME_EVENT_ACHIEVEMENT_TRIGGERED)
        fprintf(stderr, "[ra] TEST ACHIEVEMENT TRIGGERED (id %u)\n", e->id);
}

void setup_test_trigger() {
    const char* spec = getenv("WCW_RA_TEST");
    if (!spec) return;
    unsigned size = 8, addr = 0, value = 0;
    if (sscanf(spec, "%u:%i:%i", &size, (int*)&addr, (int*)&value) != 3) {
        fprintf(stderr, "[ra] bad WCW_RA_TEST (want size:addr:value): %s\n", spec);
        return;
    }
    const char* prefix = size == 32 ? "0xX" : size == 16 ? "0x" : "0xH";
    char memaddr[64];
    snprintf(memaddr, sizeof(memaddr), "%s%06x=%u", prefix, addr, value);
    rc_runtime_init(&g_test_runtime);
    int rc = rc_runtime_activate_achievement(&g_test_runtime, 1, memaddr, nullptr, 0);
    if (rc != RC_OK) {
        fprintf(stderr, "[ra] test trigger activate failed (%d) for '%s'\n", rc, memaddr);
        return;
    }
    fprintf(stderr, "[ra] test trigger armed: %s\n", memaddr);
    g_test_addr = addr;
    g_test_size = size;
    g_test_active = true;
}

// ---------------------------------------------------------------------------
// RA thread
// ---------------------------------------------------------------------------

void ra_thread() {
    fprintf(stderr, "[ra] thread up (peek mode: %s)\n", g_swap_mode ? "xor3" : "raw");
    setup_test_trigger();
    setup_watch();

    const char* user = getenv("WCW_RA_USER");
    const char* pass = getenv("WCW_RA_PASS");
    const char* token = getenv("WCW_RA_TOKEN");
    bool online = user && (pass || token);
    if (online) {
        g_client = rc_client_create(client_read_memory, client_server_call);
        rc_client_enable_logging(g_client, RC_CLIENT_LOG_LEVEL_WARN, client_log);
        rc_client_set_event_handler(g_client, client_event);
        rc_client_set_hardcore_enabled(g_client, 0);  // softcore by design
        if (token)
            rc_client_begin_login_with_token(g_client, user, token, login_callback, nullptr);
        else
            rc_client_begin_login_with_password(g_client, user, pass, login_callback, nullptr);
    } else {
        fprintf(stderr, "[ra] offline (set WCW_RA_USER + WCW_RA_PASS or WCW_RA_TOKEN "
                        "for live achievements)\n");
    }

    char last_rp[256] = "";
    int rp_tick = 0;
    for (;;) {
        drain_http_done();
        if (!g_watch.empty()) poll_watch();
        if (g_client) {
            rc_client_do_frame(g_client);
            // Rich presence renders game memory through the set's own script -
            // sensible text here proves the peeks read what the authors expect.
            if (++rp_tick % 300 == 0 && rc_client_get_game_info(g_client)) {
                char rp[256] = "";
                rc_client_get_rich_presence_message(g_client, rp, sizeof(rp));
                if (rp[0] && strcmp(rp, last_rp) != 0) {
                    fprintf(stderr, "[ra] rich presence: %s\n", rp);
                    snprintf(last_rp, sizeof(last_rp), "%s", rp);
                }
            }
        }
        if (g_test_active) {
            rc_runtime_do_frame(&g_test_runtime, test_event_handler, runtime_peek,
                                nullptr, nullptr);
            static int tick = 0;
            if (++tick % 120 == 0)  // every ~2s: live value at the test address
                fprintf(stderr, "[ra] test addr 0x%06X value(%u-bit) = 0x%08X\n",
                        g_test_addr, g_test_size,
                        runtime_peek(g_test_addr, g_test_size / 8, nullptr));
        }
        Sleep(16);  // ~60Hz; trigger evaluation is microseconds
    }
}

}  // namespace

namespace wcw::ra {

void notify_rdram(uint8_t* rdram) {
    g_rdram.store(rdram);
    const char* en = getenv("WCW_RA");
    if (en == nullptr || en[0] != '1') return;
    bool expected = false;
    if (!g_thread_started.compare_exchange_strong(expected, true)) return;
    const char* swap = getenv("WCW_RA_SWAP");
    g_swap_mode = swap && swap[0] == '1';
    std::thread(ra_thread).detach();
}

}  // namespace wcw::ra

#else
namespace wcw::ra {
void notify_rdram(uint8_t*) {}
}
#endif
