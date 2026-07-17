// Entry point for WWF No Mercy: Recompiled.
// Minimal launcher: sets up SDL + RT64 (via recompui) + the runtime callbacks, registers
// the game, auto-loads the ROM (no UI launcher yet), and calls recomp::start.
// Adapted from Bomberman Hero: Recompiled's src/main/main.cpp.
#include <cstdio>
#include <cstring>
#include <cassert>
#include <vector>
#include <array>
#include <filesystem>
#include <thread>
#include <set>
#include <chrono>

#define SDL_MAIN_HANDLED
#include "SDL.h"
#include "SDL_syswm.h"
#include "nfd.h"

#include "ultramodern/ultra64.h"
#include "ultramodern/ultramodern.hpp"
#include "recompui/recompui.h"
#include "recompui/renderer.h"
#include "recompui/config.h"
#include "recompui/program_config.h"
#include "util/file.h"
#include "recompinput/recompinput.h"
#include "recompinput/profiles.h"
#include "recompinput/input_events.h"
#include "recompinput/input_state.h"
#include "recompinput/players.h"
#include "librecomp/game.hpp"
#include "librecomp/rsp.hpp"
#include "ovl_patches.hpp"
#include "icon_bytes.h"
#include "logo_bytes.h"
#include "../../lib/RecompFrontend/recompui/src/elements/ui_image.h"
#ifdef _WIN32
#include "renderdoc_app.h" // [wcw] RenderDoc in-app capture API (diagnostic)
#endif

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <timeapi.h>
#pragma comment(lib, "winmm.lib") // timeBeginPeriod
#include <io.h>
#include <fcntl.h>
#include <dxgi1_4.h>
#include <d3d12.h>
#include <dbghelp.h>
#include <exception>
#include <cstdlib>
#pragma comment(lib, "dbghelp.lib")

// DIAGNOSTIC: print one resolved stack frame (module!symbol+off or module+off).
static void wcw_print_frame(HANDLE proc, int i, DWORD64 addr) {
    char symbuf[sizeof(SYMBOL_INFO) + 512];
    SYMBOL_INFO* sym = (SYMBOL_INFO*)symbuf;
    sym->SizeOfStruct = sizeof(SYMBOL_INFO);
    sym->MaxNameLen = 511;
    char modname[MAX_PATH] = "?";
    HMODULE mod = nullptr;
    if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           (LPCSTR)addr, &mod) && mod) {
        char full[MAX_PATH];
        if (GetModuleFileNameA(mod, full, MAX_PATH)) {
            const char* b = strrchr(full, '\\'); strncpy(modname, b ? b + 1 : full, MAX_PATH - 1);
        }
    }
    DWORD64 disp = 0;
    if (SymFromAddr(proc, addr, &disp, sym)) {
        fprintf(stderr, "  #%2d  %-20s  %s+0x%llX  (0x%llX)\n", i, modname, sym->Name, (unsigned long long)disp, (unsigned long long)addr);
    } else {
        DWORD64 modbase = mod ? (DWORD64)mod : 0;
        fprintf(stderr, "  #%2d  %-20s  +0x%llX  (0x%llX)\n", i, modname, (unsigned long long)(addr - modbase), (unsigned long long)addr);
    }
}

// DIAGNOSTIC: dump a backtrace + in-flight C++ exception when std::terminate fires.
static void wcw_terminate_handler() {
    fprintf(stderr, "\n==== [wcw] std::terminate called ====\n");
    if (auto ep = std::current_exception()) {
        try { std::rethrow_exception(ep); }
        catch (const std::exception& e) { fprintf(stderr, "[wcw] in-flight exception: %s: %s\n", typeid(e).name(), e.what()); }
        catch (...) { fprintf(stderr, "[wcw] in-flight exception: non-std type\n"); }
    } else {
        fprintf(stderr, "[wcw] no in-flight C++ exception (pure-virtual call / explicit abort?)\n");
    }
    void* frames[64];
    USHORT n = RtlCaptureStackBackTrace(0, 64, frames, nullptr);
    HANDLE proc = GetCurrentProcess();
    SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_LOAD_LINES);
    SymInitialize(proc, nullptr, TRUE);
    for (USHORT i = 0; i < n; i++) wcw_print_frame(proc, i, (DWORD64)frames[i]);
    fprintf(stderr, "==== [wcw] end backtrace ====\n");
    fflush(stderr);
    _exit(2);
}

// DIAGNOSTIC: walk the faulting thread's stack for hard faults (access violations etc.),
// which don't go through std::terminate.
static LONG WINAPI wcw_unhandled_filter(EXCEPTION_POINTERS* ep) {
    fprintf(stderr, "\n==== [wcw] unhandled exception 0x%08lX at %p ====\n",
        (unsigned long)ep->ExceptionRecord->ExceptionCode, (void*)ep->ExceptionRecord->ExceptionAddress);
    if (ep->ExceptionRecord->ExceptionCode == EXCEPTION_ACCESS_VIOLATION && ep->ExceptionRecord->NumberParameters >= 2) {
        fprintf(stderr, "[wcw]   access %s address 0x%llX\n",
            ep->ExceptionRecord->ExceptionInformation[0] ? "WRITE" : "READ",
            (unsigned long long)ep->ExceptionRecord->ExceptionInformation[1]);
    }
    HANDLE proc = GetCurrentProcess();
    HANDLE thread = GetCurrentThread();
    SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_LOAD_LINES);
    SymInitialize(proc, nullptr, TRUE);
    CONTEXT ctx = *ep->ContextRecord;
    STACKFRAME64 sf{};
    sf.AddrPC.Offset = ctx.Rip;    sf.AddrPC.Mode = AddrModeFlat;
    sf.AddrFrame.Offset = ctx.Rbp; sf.AddrFrame.Mode = AddrModeFlat;
    sf.AddrStack.Offset = ctx.Rsp; sf.AddrStack.Mode = AddrModeFlat;
    for (int i = 0; i < 64; i++) {
        if (!StackWalk64(IMAGE_FILE_MACHINE_AMD64, proc, thread, &sf, &ctx, nullptr,
                         SymFunctionTableAccess64, SymGetModuleBase64, nullptr)) break;
        if (sf.AddrPC.Offset == 0) break;
        wcw_print_frame(proc, i, sf.AddrPC.Offset);
    }
    fprintf(stderr, "==== [wcw] end backtrace ====\n");
    fflush(stderr);
    return EXCEPTION_EXECUTE_HANDLER;
}
#endif

#ifdef _WIN32
#include <tlhelp32.h>
// DIAGNOSTIC: after a delay, suspend every other thread and print its stack, to find where
// the game threads are parked when the boot stalls (no crash, no progress).
static void wcw_sample_all_threads() {
    HANDLE proc = GetCurrentProcess();
    SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS);
    SymInitialize(proc, nullptr, TRUE);
    DWORD me = GetCurrentThreadId();
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    THREADENTRY32 te{}; te.dwSize = sizeof(te);
    DWORD pid = GetCurrentProcessId();
    if (Thread32First(snap, &te)) {
        do {
            if (te.th32OwnerProcessID != pid || te.th32ThreadID == me) continue;
            HANDLE th = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, te.th32ThreadID);
            if (!th) continue;
            SuspendThread(th);
            CONTEXT ctx{}; ctx.ContextFlags = CONTEXT_FULL;
            if (GetThreadContext(th, &ctx)) {
                STACKFRAME64 sf{};
                sf.AddrPC.Offset = ctx.Rip;    sf.AddrPC.Mode = AddrModeFlat;
                sf.AddrFrame.Offset = ctx.Rbp; sf.AddrFrame.Mode = AddrModeFlat;
                sf.AddrStack.Offset = ctx.Rsp; sf.AddrStack.Mode = AddrModeFlat;
                fprintf(stderr, "[wcw][sample] thread %lu:\n", (unsigned long)te.th32ThreadID);
                for (int i = 0; i < 16; i++) {
                    if (!StackWalk64(IMAGE_FILE_MACHINE_AMD64, proc, th, &sf, &ctx, nullptr,
                                     SymFunctionTableAccess64, SymGetModuleBase64, nullptr)) break;
                    if (sf.AddrPC.Offset == 0) break;
                    wcw_print_frame(proc, i, sf.AddrPC.Offset);
                }
            }
            ResumeThread(th);
            CloseHandle(th);
        } while (Thread32Next(snap, &te));
    }
    CloseHandle(snap);
    fprintf(stderr, "[wcw][sample] done\n");
    fflush(stderr);
}
#endif

extern "C" void recomp_entrypoint(uint8_t* rdram, recomp_context* ctx);

static const recomp::Version version { 0, 1, 0, "" };

template <typename... Ts>
[[noreturn]] static void exit_error(const char* str, Ts... args) {
    fprintf(stderr, str, args...);
    assert(false);
    ultramodern::error_handling::quick_exit(__FILE__, __LINE__, __FUNCTION__);
}

// ---------------- gfx / window ----------------
// These globals are referenced by recompui/recompinput, so they must be non-static.
SDL_Window* window = nullptr;
uint8_t* g_ww_rdram = nullptr;   // rdram base for the [nomercy][ww] write-watch diagnostic
std::vector<recomp::GameEntry> supported_games;

ultramodern::gfx_callbacks_t::gfx_data_t create_gfx() {
    SDL_SetHint(SDL_HINT_WINDOWS_DPI_AWARENESS, "permonitorv2");
    SDL_SetHint(SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS, "1");
    if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_GAMECONTROLLER | SDL_INIT_JOYSTICK) > 0) {
        exit_error("Failed to initialize SDL2: %s\n", SDL_GetError());
    }
    fprintf(stdout, "SDL Video Driver: %s\n", SDL_GetCurrentVideoDriver());
    return {};
}

ultramodern::renderer::WindowHandle create_window(ultramodern::gfx_callbacks_t::gfx_data_t) {
    fprintf(stderr, "[wcw] create_window\n");
    uint32_t flags = SDL_WINDOW_RESIZABLE;
#if defined(RT64_SDL_WINDOW_VULKAN)
    flags |= SDL_WINDOW_VULKAN;
#endif
    window = SDL_CreateWindow("WWF No Mercy: Recompiled",
        SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, 1280, 960, flags);
    if (window == nullptr) {
        exit_error("Failed to create window: %s\n", SDL_GetError());
    }
    SDL_SysWMinfo wmInfo;
    SDL_VERSION(&wmInfo.version);
    SDL_GetWindowWMInfo(window, &wmInfo);
#if defined(_WIN32)
    return ultramodern::renderer::WindowHandle{ wmInfo.info.win.window, GetCurrentThreadId() };
#else
    return ultramodern::renderer::WindowHandle{ window };
#endif
}

// Test aid: WCW_VPADS=<n> attaches n SDL virtual gamepads that each pulse one distinct
// button (pad 1: A/South, pad 2: B/East, pad 3: X/West, pad 4: Y/North) every 2 seconds.
// Combined with WCW_INPUT_LOG=1 (librecomp si.cpp) this verifies per-player input
// separation end to end without physical controllers.
static void update_virtual_pads() {
    static int vpad_count = -1;
    static SDL_Joystick* vpads[4] = {};
    if (vpad_count < 0) {
        const char* env = getenv("WCW_VPADS");
        vpad_count = env ? std::clamp(atoi(env), 0, 4) : 0;
        for (int i = 0; i < vpad_count; i++) {
            int dev = SDL_JoystickAttachVirtual(SDL_JOYSTICK_TYPE_GAMECONTROLLER,
                                                SDL_CONTROLLER_AXIS_MAX, SDL_CONTROLLER_BUTTON_MAX, 0);
            if (dev >= 0) vpads[i] = SDL_JoystickOpen(dev);
            fprintf(stderr, "[wcw] virtual pad %d attached (dev=%d joy=%p)\n", i + 1, dev, (void*)vpads[i]);
        }
    }
    if (vpad_count == 0) return;
    uint32_t t = SDL_GetTicks();
    for (int i = 0; i < vpad_count; i++) {
        if (vpads[i] == nullptr) continue;
        // 2 s period, 500 ms press, staggered 250 ms per pad.
        bool pressed = ((t + i * 250) % 2000) < 500;
        SDL_JoystickSetVirtualButton(vpads[i], SDL_CONTROLLER_BUTTON_A + i, pressed ? SDL_PRESSED : SDL_RELEASED);
    }
}

void update_gfx(void*) {
    update_virtual_pads();
    recompinput::handle_events();
}

ultramodern::input::connected_device_info_t get_connected_device_info(int controller_num) {
    // All four ports report a controller so the game considers every player slot joinable
    // (unassigned players read as neutral input, like an idle plugged-in pad). Only port 1
    // has a pak: the emulated hybrid Controller/Rumble Pak in librecomp si.cpp.
    return { .connected_device = ultramodern::input::Device::Controller,
             .connected_pak = (controller_num == 0) ? ultramodern::input::Pak::RumblePak
                                                    : ultramodern::input::Pak::None };
}

// ---------------- audio (minimal: queue as-is, 16-bit stereo) ----------------
static SDL_AudioDeviceID g_audio_device = 0;
static uint32_t g_sample_rate = 32000;
static uint32_t g_requested_rate = 0;
constexpr uint32_t input_channels = 2;
constexpr uint32_t bytes_per_frame = input_channels * sizeof(int16_t);

// With no audio endpoint (headless/unattended session) queued audio used to vanish:
// get_frames_remaining()==0 forever, so the game's AI-full throttle never engages and the
// audio task loop free-runs (~500/s, bringup-plan session 4). Model a device that drains
// the queue in real time so pacing works endpoint or not.
static uint64_t g_virtual_frames = 0;
static uint64_t g_virtual_last_ms = 0;
static void virtual_drain() {
    uint64_t now = SDL_GetTicks64();
    if (g_virtual_last_ms) {
        uint64_t drained = (now - g_virtual_last_ms) * g_sample_rate / 1000;
        g_virtual_frames = drained >= g_virtual_frames ? 0 : g_virtual_frames - drained;
    }
    g_virtual_last_ms = now;
}

static bool reset_audio(uint32_t freq) {
    // Diagnostic: every open/close drops the whole SDL queue, so frequent calls here
    // zero osAiGetLength and free-run the game's audio generator (bringup-plan session 4).
    { static int n = 0; fprintf(stderr, "[audio] reset_audio #%d freq=%u driver=%s (had_device=%d)\n",
        n++, freq, SDL_GetCurrentAudioDriver() ? SDL_GetCurrentAudioDriver() : "none", g_audio_device != 0); }
    SDL_AudioSpec want{}, have{};
    want.freq = (int)freq;
    want.format = AUDIO_S16SYS;
    want.channels = (Uint8)input_channels;
    // 512-frame chunks (23ms @ 22050) — with 1024 (46ms) the device drained more per pull
    // than the game's ~25ms audio bursts could cover, guaranteeing periodic underruns.
    want.samples = 512;
    want.callback = nullptr;
    if (g_audio_device) { SDL_CloseAudioDevice(g_audio_device); g_audio_device = 0; }
    g_audio_device = SDL_OpenAudioDevice(nullptr, 0, &want, &have, 0);
    if (g_audio_device == 0) {
        fprintf(stderr, "SDL_OpenAudioDevice failed: %s\n", SDL_GetError());
        g_sample_rate = freq; // keep the virtual-drain model at the game's real rate
        return false;
    }
    g_sample_rate = (uint32_t)have.freq;
    g_virtual_frames = 0;
    fprintf(stderr, "[audio] device opened id=%u have.freq=%d have.samples=%u\n",
        (unsigned)g_audio_device, have.freq, (unsigned)have.samples);
    SDL_PauseAudioDevice(g_audio_device, 0);
    return true;
}

void set_frequency(uint32_t freq) {
    // Reopening the device discards SDL's entire queued buffer, so skip redundant calls
    // (WM2000 re-sets 28800 on menu transitions — each one was an audible drop).
    if (g_audio_device && freq == g_requested_rate) return;
    g_requested_rate = freq;
    reset_audio(freq);
}

size_t get_frames_remaining();

void queue_samples(int16_t* audio_data, size_t sample_count) {
    // audio_data is interleaved stereo; swap L/R to correct for endianness handling.
    static std::vector<int16_t> buf;
    buf.resize(sample_count);
    for (size_t i = 0; i + 1 < sample_count; i += 2) {
        buf[i + 0] = audio_data[i + 1];
        buf[i + 1] = audio_data[i + 0];
    }
    uint32_t queued_frames_before = (uint32_t)get_frames_remaining();
    if (g_audio_device) {
        SDL_QueueAudio(g_audio_device, buf.data(), (Uint32)(sample_count * sizeof(int16_t)));
    } else {
        virtual_drain();
        g_virtual_frames += sample_count / input_channels;
    }
    // Diagnostic (1/s): audio health. queued_frames_before==0 means the device ran dry and
    // SDL fed silence (an audible pop); minlvl is the low-water mark in frames; maxgap the
    // longest interval between batches (starvation if it exceeds the buffered duration).
    static uint64_t last_ms = 0, report_ms = 0; static uint64_t max_gap = 0;
    static uint32_t underruns = 0, batches = 0, min_lvl = UINT32_MAX; static int peak = 0;
    uint64_t now = SDL_GetTicks64();
    if (last_ms && now - last_ms > max_gap) max_gap = now - last_ms;
    last_ms = now;
    if (queued_frames_before == 0) underruns++;
    if (queued_frames_before < min_lvl) min_lvl = queued_frames_before;
    batches++;
    for (size_t i = 0; i < sample_count; i++) { int v = audio_data[i] < 0 ? -audio_data[i] : audio_data[i]; if (v > peak) peak = v; }
    static const bool audio_log = getenv("WCW_AUDIO_LOG") != nullptr;
    if (now - report_ms >= 1000) {
        if (audio_log) {
            fprintf(stderr, "[audio] t=%llus batches=%u minlvl=%u underruns=%u maxgap=%llums peak=%d rate=%u\n",
                (unsigned long long)(now / 1000), batches, min_lvl, underruns, (unsigned long long)max_gap, peak, g_sample_rate);
        }
        report_ms = now; underruns = 0; min_lvl = UINT32_MAX; max_gap = 0; peak = 0; batches = 0;
    }
}

size_t get_frames_remaining() {
    if (!g_audio_device) {
        virtual_drain();
        return (size_t)g_virtual_frames;
    }
    return SDL_GetQueuedAudioSize(g_audio_device) / bytes_per_frame;
}

// ---------------- RSP ucode (audio) ----------------
// The audio ucode (located at runtime via the OSTask log: ucode=0x8002BD10 ->
// ROM 0x2C910, ucode_data=0x8003A5C0 -> ROM 0x3B1C0) is recompiled by RSPRecomp
// (rsp/revenge_audio.toml -> rsp/revenge_audio.cpp). It is NOT WT's aspMain (newer
// revision, text 0xC54); its jump table was extracted from the ucode's own data
// section. Never return nullptr here: that makes recomp::rsp::run_task return false,
// which task_thread_func (events.cpp) treats as FATAL (ULTRAMODERN_QUICK_EXIT). For
// unexpected task types, hand back a no-op ucode that reports a clean RSP break so
// the task "completes" and the game keeps running.
static RspExitReason wcw_null_ucode(uint8_t* rdram, uint32_t ucode_addr) {
    (void)rdram; (void)ucode_addr;
    return RspExitReason::Broke; // pretend the task finished so the runtime continues
}

extern RspUcodeFunc nomercy_audio_ucode;

// Diagnostic wrapper (env WCW_AUDIO_LOG=1): dump the first audio command lists so the
// acmd opcodes/arguments can be compared against the aspMain ABI (silent-audio debug).
static uint32_t dbg_acmd_ptr, dbg_acmd_size;
static RspExitReason nomercy_audio_traced(uint8_t* rdram, uint32_t ucode_addr) {
    static int n = 0;
    if (n++ < 2 && dbg_acmd_ptr >= 0x80000000) {
        uint32_t off = dbg_acmd_ptr - 0x80000000;
        fprintf(stderr, "[rsp][acmd] list @0x%08X size=0x%X:\n", dbg_acmd_ptr, dbg_acmd_size);
        for (uint32_t i = 0; i < dbg_acmd_size && i < 0x140; i += 8) {
            uint32_t w0 = *(uint32_t*)(rdram + off + i);
            uint32_t w1 = *(uint32_t*)(rdram + off + i + 4);
            fprintf(stderr, "  %02X %06X %08X\n", w0 >> 24, w0 & 0xFFFFFF, w1);
        }
    }
    // Opcode histogram across ALL audio tasks (printed every ~256 tasks ≈ 6s):
    // discriminates "voice opcodes never submitted" (game-side silence) from
    // "voice opcodes mis-executed" (ucode bug).
    // TODO(rom): every hardcoded vram probe below (0x80104DB0.., 0x800604xx,
    // 0x80036F54..) is WM2000's audio-pipeline layout — meaningless for No Mercy
    // until re-derived from this ROM's disasm. Env-gated (WCW_AUDIO_LOG), harmless.
    static uint32_t histo[16] = {};
    if (dbg_acmd_ptr >= 0x80000000) {
        uint32_t off = dbg_acmd_ptr - 0x80000000;
        for (uint32_t i = 0; i < dbg_acmd_size; i += 8) {
            histo[(*(uint32_t*)(rdram + off + i)) >> 28 == 0 ? ((*(uint32_t*)(rdram + off + i)) >> 24) & 0xF : 15]++;
        }
    }
    if ((n & 0xFF) == 0) {
        fprintf(stderr, "[rsp][acmd-histo] ");
        for (int i = 0; i < 16; i++) if (histo[i]) fprintf(stderr, "%02X:%u ", i, histo[i]);
        // Probe the mixer's LOADBUFF source DRAM buffers: nonzero = a CPU-side synth
        // (or earlier RSP pass) is producing input; all-zero = the voice path is dead.
        auto probe = [&](uint32_t addr) {
            uint32_t s = 0;
            for (int k = 0; k < 64; k += 4) s |= *(uint32_t*)(rdram + (addr - 0x80000000) + k);
            return s;
        };
        fprintf(stderr, "| in104DB0=%08X in105850=%08X in106610=%08X\n",
            probe(0x80104DB0), probe(0x80105850), probe(0x80106610));
        // Voice-pipeline stage probe (stage map from the 1050.s disasm; this is how
        // the 2026-07-07 silent-audio bug was localized — see rsp/README.md):
        // overlay -> func_8000E55C(track) -> func_8001381C (alloc voices, handle ctr
        // D_800604E8) -> event queue (w=D_80060508 r=D_80060504) -> handler
        // func_80014500 (client D_800604A0 on alGlobals D_80036F54) -> alSyn* ->
        // func_80018C24 event post -> RSP voice opcodes. Game voices at D_800604D0
        // (count D_800604C8, stride 0x158, active = voice+4 != 0). rdram u32 loads at
        // 4-aligned offsets return the BE word's value, so halfwords live in the
        // high/low 16 bits of their containing word.
        auto w32 = [&](uint32_t addr) { return *(uint32_t*)(rdram + (addr - 0x80000000)); };
        uint32_t vcnt = w32(0x800604C8), vbase = w32(0x800604D0);
        uint32_t active = 0, flagged = 0;
        if (vbase >= 0x80000000 && vcnt <= 64) {
            for (uint32_t i = 0; i < vcnt; i++) {
                if (w32(vbase + i * 0x158 + 0x4)) active++;
                if (w32(vbase + i * 0x158) & 1) flagged++;
            }
        }
        uint32_t alg = w32(0x80036F54);
        uint32_t head = alg >= 0x80000000 ? w32(alg) : 0;
        uint32_t handler = head >= 0x80000000 ? w32(head + 0x8) : 0;
        fprintf(stderr, "[voice] qw=%u qr=%u handles=%u track=%u hmus=%08X voices=%u act=%u flag=%u alhead=%08X handler=%08X\n",
            w32(0x80060508), w32(0x80060504), w32(0x800604E8),
            w32(0x80036748) >> 16, w32(0x8002EED0), vcnt, active, flagged, head, handler);
    }
    return nomercy_audio_ucode(rdram, ucode_addr);
}

RspUcodeFunc* get_rsp_microcode(const OSTask* task) {
    static int n = 0;
    if (n++ < 4) {
        fprintf(stderr, "[rsp] task type=%u ucode=0x%08X ucode_data=0x%08X ucode_size=0x%X\n",
            (unsigned)task->t.type, (unsigned)task->t.ucode, (unsigned)task->t.ucode_data,
            (unsigned)task->t.ucode_size);
    }
    // Only non-gfx tasks reach here — gfx tasks go through the renderer path.
    if (task->t.type == M_AUDTASK) {
        // Diagnostic (env WCW_AUDIO_LOG=1, first 8 tasks): acmd list location/size —
        // discriminates "game submits empty lists" from "ucode output wrong".
        static const bool audio_log = getenv("WCW_AUDIO_LOG") != nullptr;
        static int an = 0;
        if (audio_log && an++ < 8) {
            fprintf(stderr, "[rsp][aud] data_ptr=0x%08X data_size=0x%X yield_data_ptr=0x%08X output_buff=0x%08X\n",
                (unsigned)task->t.data_ptr, (unsigned)task->t.data_size,
                (unsigned)task->t.yield_data_ptr, (unsigned)task->t.output_buff);
        }
        if (audio_log) {
            dbg_acmd_ptr = (uint32_t)task->t.data_ptr;
            dbg_acmd_size = (uint32_t)task->t.data_size;
            return &nomercy_audio_traced;
        }
        return &nomercy_audio_ucode;
    }
    fprintf(stderr, "[rsp] unknown task type=%u — running no-op ucode\n", (unsigned)task->t.type);
    return &wcw_null_ucode;
}

#ifdef _WIN32
// [wcw] C4: release builds link as /SUBSYSTEM:WINDOWS (no console window). When launched
// without usable stdio (double-click), route stdout+stderr to a log file in the app folder
// so bug reports have something to attach (the terminate/SEH backtraces land there too).
// --show-console allocates a console instead. If the parent provided std handles (a dev
// console, or cycle.ps1's `> bt.txt` redirection), stdio is left untouched.
static void wcw_setup_logging(bool show_console) {
    if (show_console) {
        if (AllocConsole()) {
            FILE* f = nullptr;
            freopen_s(&f, "CONOUT$", "w", stdout);
            freopen_s(&f, "CONOUT$", "w", stderr);
        }
        return;
    }
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    HANDLE err = GetStdHandle(STD_ERROR_HANDLE);
    if ((out != nullptr && out != INVALID_HANDLE_VALUE) || (err != nullptr && err != INVALID_HANDLE_VALUE)) {
        return;
    }
    std::filesystem::path app_folder = recompui::file::get_app_folder_path();
    std::error_code ec;
    std::filesystem::create_directories(app_folder, ec);
    std::filesystem::path log_path = app_folder / "NoMercyRecompiled.log";
    // Open with read sharing (freopen would deny it) so the log can be copied/viewed while
    // the game is running; route both streams through dups of the same fd (shared offset).
    HANDLE log_handle = CreateFileW(log_path.c_str(), GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (log_handle == INVALID_HANDLE_VALUE) {
        return;
    }
    int log_fd = _open_osfhandle(reinterpret_cast<intptr_t>(log_handle), _O_WRONLY);
    if (log_fd == -1) {
        CloseHandle(log_handle);
        return;
    }
    // In a handle-less GUI launch the CRT streams have no fd (-2); freopen to NUL first to
    // materialize one, then dup the log fd over it.
    FILE* f = nullptr;
    freopen_s(&f, "NUL", "w", stderr);
    _dup2(log_fd, _fileno(stderr));
    freopen_s(&f, "NUL", "w", stdout);
    _dup2(log_fd, _fileno(stdout));
    _close(log_fd);
}
#endif

int main(int argc, char** argv) {
    bool show_console = false;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--show-console") == 0) {
            show_console = true;
        }
    }
    (void)show_console;

    // Program identity. The program id names the config/save folder
    // (%LOCALAPPDATA%\NoMercyRecompiled on Windows) — keep it matched to the exe name.
    // Set before anything that resolves the app folder (logging below, config path later).
    recompui::programconfig::set_program_name("WWF No Mercy: Recompiled");
    recompui::programconfig::set_program_id(u8"NoMercyRecompiled");

#ifdef _WIN32
    wcw_setup_logging(show_console);
#endif
    setvbuf(stdout, nullptr, _IONBF, 0);
    setvbuf(stderr, nullptr, _IONBF, 0);
    fprintf(stderr, "[wcw] main start\n");
#ifdef _WIN32
    std::set_terminate(wcw_terminate_handler);
    SetUnhandledExceptionFilter(wcw_unhandled_filter);

    // [wcw] ultramodern paces VI/timer events with raw Sleep() (timer.cpp), whose
    // granularity is the PER-PROCESS timer resolution on Win10 2004+/Win11. Nothing
    // in-process holds 1ms otherwise, so retrace delivery can quantize to 15.6ms and
    // burst — the >=pri-80 event relay then never drains and starves the game loop
    // (frozen video, live audio, from boot). Hold 1ms for the process lifetime.
    timeBeginPeriod(1);

    // [wcw] DIAGNOSTIC: when running under RenderDoc (renderdoc.dll injected), trigger a
    // one-frame capture ~8s in (title screen rendering steadily by then). The capture is the
    // ground truth for why game draws rasterize nothing (see disasm/libultra.md deep dive).
    {
        HMODULE rdoc = GetModuleHandleA("renderdoc.dll");
        if (rdoc != nullptr) {
            pRENDERDOC_GetAPI getApi = (pRENDERDOC_GetAPI)GetProcAddress(rdoc, "RENDERDOC_GetAPI");
            static RENDERDOC_API_1_1_2* rdocApi = nullptr;
            if (getApi != nullptr && getApi(eRENDERDOC_API_Version_1_1_2, (void**)&rdocApi) == 1) {
                int rdocDelay = 8;
                if (const char* d = getenv("WCW_RDC_T")) { int v = atoi(d); if (v > 0) rdocDelay = v; }
                fprintf(stderr, "[wcw][renderdoc] in-app API active; capture will trigger at t+%ds\n", rdocDelay);
                std::thread([rdocDelay]() {
                    std::this_thread::sleep_for(std::chrono::seconds(rdocDelay));
                    fprintf(stderr, "[wcw][renderdoc] triggering capture NOW\n");
                    // Multi-frame: game workloads land in ~27 of 60 present intervals, so a
                    // single-frame capture can contain only the VI blit. 12 frames guarantees
                    // several captures with real game draws.
                    rdocApi->TriggerMultiFrameCapture(12);
                    std::this_thread::sleep_for(std::chrono::seconds(2));
                    fprintf(stderr, "[wcw][renderdoc] captures so far: %u\n", rdocApi->GetNumCaptures());
                }).detach();
            }
        }
    }
#endif
#ifdef _WIN32
    // WORKAROUND: the first DXGI factory + D3D12 device creation must happen on the main
    // thread, or they fast-fail (0xC0000409) when RT64 first touches them on the game
    // thread. (BMHero avoids this because its launcher UI creates a render context on the
    // main thread first.) Prime DXGI + D3D12 here so RT64's later calls on the game thread
    // succeed.
    {
        IDXGIFactory4* tf = nullptr;
        fprintf(stderr, "[wcw] priming DXGI + D3D12 on main thread...\n");
        if (SUCCEEDED(CreateDXGIFactory2(0, IID_PPV_ARGS(&tf))) && tf) {
            IDXGIAdapter1* ad = nullptr;
            for (UINT i = 0; tf->EnumAdapters1(i, &ad) != DXGI_ERROR_NOT_FOUND; i++) {
                ID3D12Device8* dev = nullptr;  // match RT64/plume: it requests ID3D12Device8
                bool ok = SUCCEEDED(D3D12CreateDevice(ad, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&dev)));
                fprintf(stderr, "[wcw]   prime adapter %u D3D12CreateDevice(Device8) ok=%d\n", i, (int)ok);
                if (dev) dev->Release();
                ad->Release();
                if (ok) break;
            }
            tf->Release();
        }
        fprintf(stderr, "[wcw] priming done\n");
    }
#endif
#ifdef _WIN32
    timeBeginPeriod(1);
    SDL_setenv("SDL_AUDIODRIVER", "wasapi", true);
#endif
    SDL_SetMainReady();
    NFD_Init();

    SDL_InitSubSystem(SDL_INIT_AUDIO);
    reset_audio(32000);

    // Source controller mappings file (SDL_GameControllerDB), same as BMHero: lets SDL
    // recognize third-party pads that lack built-in mappings. Missing file is non-fatal.
    std::u8string controller_db_path = (recompui::file::get_program_path() / "recompcontrollerdb.txt").u8string();
    if (SDL_GameControllerAddMappingsFromFile(reinterpret_cast<const char *>(controller_db_path.c_str())) < 0) {
        fprintf(stderr, "Failed to load controller mappings: %s\n", SDL_GetError());
    }

    // Register the game. The entry is stored in the global `supported_games` vector because
    // recompui's launcher reads supported_games[0] (e.g. default_launcher_init_callback) to
    // build the game-options menu; leaving it empty access-violates during boot.
    // No Mercy is documented as a FLASH-cart game (everything saves on-cart; no
    // Controller Pak save support), so the pak emulation should never touch the cart
    // save — keep the separate pak backing regardless (saves/<game id>.pak.bin,
    // [wcw2k] in librecomp si.cpp): if a pak probe does happen it can't corrupt the
    // flash image. VERIFY during bring-up (WM2000 lesson: the shared buffer would
    // have let pak writes corrupt the cart save).
    extern bool wcw_pak_separate_backing;
    wcw_pak_separate_backing = true;

    // Hardware-accurate osAiSetFrequency return (28800 -> 28805 via DAC quantization).
    // WM2000 sizes its audio-heap allocations from the return value; the unquantized
    // return shifted the audio heap onto thread 6's stack ([wcw2k] in librecomp ai.cpp).
    extern bool wcw_ai_accurate_freq;
    wcw_ai_accurate_freq = true;

    recomp::GameEntry game{};
    game.rom_hash = 0x0455F72A0F9FF778ULL;          // XXH3_64bits of the NTSC-U Rev 1 ROM
    game.internal_name = "WWF No Mercy";            // 20-byte header name, trailing spaces trimmed
    game.display_name = "WWF No Mercy";
    game.game_id = u8"wwf.nomercy.us";
    game.mod_game_id = "wwfnomercy";
    game.thumbnail_bytes = std::span<const char>(icon_bytes);
    // No Mercy (NW4E) is documented as FLASH RAM 1Mbit (128 KB) — one of the few
    // flash-cart N64 games, and the reason Rev 0 had its famous save-erasure bug.
    // librecomp supports Flashram (routes the flash command protocol; see
    // librecomp pi.cpp/recomp.cpp). VERIFY during bring-up from the game's save
    // driver: find its PI handle init (WM2000 built an SRAM OSPiHandle in
    // func_80000A88, baseAddress 0xA8000000, device type 3; a flash driver instead
    // issues the 0x78xxxxxx command writes librecomp's flashram path expects).
    game.save_type = recomp::SaveType::Flashram;
    game.is_enabled = true;
    // Must be SIGN-EXTENDED: entrypoint_address is a gpr (int64) and librecomp's MEM_*
    // macros compute `rdram + (addr - 0xFFFFFFFF80000000)`. The bare literal 0x80000400 is
    // unsigned and zero-extends to 0x0000000080000400, which yields a bogus +4GB rdram offset
    // in the initial boot DMA. Cast through int32_t so it sign-extends to 0xFFFFFFFF80000400.
    game.entrypoint_address = (gpr)(int32_t)0x80000400;
    game.entrypoint = recomp_entrypoint;
    supported_games.push_back(game);
    recomp::register_game(supported_games[0]);

    // Config/saves/stored-ROM location: %LOCALAPPDATA%\WCWRecompiled (or next to the exe
    // if a portable.txt file exists in the working dir — handled by get_app_folder_path).
    // librecomp doesn't create the folder itself, and select_rom's stored-ROM write plus
    // the json config writes all assume it exists.
    std::filesystem::path app_folder = recompui::file::get_app_folder_path();
    std::error_code app_folder_ec;
    std::filesystem::create_directories(app_folder, app_folder_ec);
    recomp::register_config_path(app_folder);
    wcw::register_wcw_overlays();
    // wcw::register_wcw_patches();  // bootstrap: no patches pipeline yet (see CMakeLists)

    // Register UI fonts (loaded from ./assets during render-context init). Without a primary
    // font, recompui throws during boot. Matches BMHero's font registration.
    recompui::register_primary_font("InterVariable.ttf", "Inter Variable");

    // Local multiplayer (WCW is a 4-player game). recompinput's multiplayer mode normally
    // requires every pad to be claimed through the player-assignment modal before it produces
    // any input; our recompinput fork adds auto-assignment ([wcw fix]) so pads claim player
    // slots in plug-in order (pad 1 -> P1, pad 2 -> P2, ...). The keyboard always drives P1
    // via the default player-0 profiles, so solo keyboard/pad play needs no ceremony. The
    // "Assign players" flow in the Controls tab remains for custom arrangements (e.g. making
    // the keyboard P2). Must run before create_controls_tab (the tab snapshots the mode).
    recompinput::players::set_single_player_mode(false);
    recompinput::players::set_player_count_range(1, 4);

    // WCW-specific default bindings (must run before the config tabs load / any profile is
    // created — recompinput forbids changing defaults after first use). WCW's in-ring scheme
    // (manual): D-PAD moves, ANALOG STICK taunts, L = duck/dodge, R = block/guard, C-Down =
    // run, C-Up = climb/flip, C-Left/Right = switch focus, Z = unused. The generic recomp
    // defaults fit badly (movement not on left stick; duck on LB while block is on RT).
    // Layout here: move = left stick + d-pad; taunt = right stick; duck = LEFT trigger and
    // block = RIGHT trigger (mirrored defensive pair); grapple = A, attack = X, run = B,
    // climb/flip = Y, focus = LB/RB, Z parked on left-stick click.
    {
        using recompinput::GameInput;
        using recompinput::InputField;
        auto pad_digital = [](SDL_GameControllerButton b) { return InputField::controller_digital(b); };
        auto pad_analog = [](SDL_GameControllerAxis a, bool positive) { return InputField::controller_analog(a, positive); };

        recompinput::set_default_mapping_for_controller(GameInput::DPAD_LEFT,  { pad_digital(SDL_CONTROLLER_BUTTON_DPAD_LEFT),  pad_analog(SDL_CONTROLLER_AXIS_LEFTX, false) });
        recompinput::set_default_mapping_for_controller(GameInput::DPAD_RIGHT, { pad_digital(SDL_CONTROLLER_BUTTON_DPAD_RIGHT), pad_analog(SDL_CONTROLLER_AXIS_LEFTX, true) });
        recompinput::set_default_mapping_for_controller(GameInput::DPAD_UP,    { pad_digital(SDL_CONTROLLER_BUTTON_DPAD_UP),    pad_analog(SDL_CONTROLLER_AXIS_LEFTY, false) });
        recompinput::set_default_mapping_for_controller(GameInput::DPAD_DOWN,  { pad_digital(SDL_CONTROLLER_BUTTON_DPAD_DOWN),  pad_analog(SDL_CONTROLLER_AXIS_LEFTY, true) });
        recompinput::set_default_mapping_for_controller(GameInput::X_AXIS_NEG, { pad_analog(SDL_CONTROLLER_AXIS_RIGHTX, false) });
        recompinput::set_default_mapping_for_controller(GameInput::X_AXIS_POS, { pad_analog(SDL_CONTROLLER_AXIS_RIGHTX, true) });
        recompinput::set_default_mapping_for_controller(GameInput::Y_AXIS_POS, { pad_analog(SDL_CONTROLLER_AXIS_RIGHTY, false) });
        recompinput::set_default_mapping_for_controller(GameInput::Y_AXIS_NEG, { pad_analog(SDL_CONTROLLER_AXIS_RIGHTY, true) });
        recompinput::set_default_mapping_for_controller(GameInput::L,          { pad_analog(SDL_CONTROLLER_AXIS_TRIGGERLEFT, true) });
        recompinput::set_default_mapping_for_controller(GameInput::R,          { pad_analog(SDL_CONTROLLER_AXIS_TRIGGERRIGHT, true) });
        recompinput::set_default_mapping_for_controller(GameInput::Z,          { pad_digital(SDL_CONTROLLER_BUTTON_LEFTSTICK) });
        recompinput::set_default_mapping_for_controller(GameInput::C_UP,       { pad_digital(recompinput::SDL_CONTROLLER_BUTTON_NORTH) });
        recompinput::set_default_mapping_for_controller(GameInput::C_DOWN,     { pad_digital(recompinput::SDL_CONTROLLER_BUTTON_EAST) });
        recompinput::set_default_mapping_for_controller(GameInput::C_LEFT,     { pad_digital(SDL_CONTROLLER_BUTTON_LEFTSHOULDER) });
        recompinput::set_default_mapping_for_controller(GameInput::C_RIGHT,    { pad_digital(SDL_CONTROLLER_BUTTON_RIGHTSHOULDER) });

        // Keyboard: same semantics — WASD = move (d-pad), IJKL = taunt (analog stick).
        // The generic default had these reversed for WCW (WASD was the stick = taunt).
        recompinput::set_default_mapping_for_keyboard(GameInput::DPAD_LEFT,  { InputField::keyboard(SDL_SCANCODE_A) });
        recompinput::set_default_mapping_for_keyboard(GameInput::DPAD_RIGHT, { InputField::keyboard(SDL_SCANCODE_D) });
        recompinput::set_default_mapping_for_keyboard(GameInput::DPAD_UP,    { InputField::keyboard(SDL_SCANCODE_W) });
        recompinput::set_default_mapping_for_keyboard(GameInput::DPAD_DOWN,  { InputField::keyboard(SDL_SCANCODE_S) });
        recompinput::set_default_mapping_for_keyboard(GameInput::X_AXIS_NEG, { InputField::keyboard(SDL_SCANCODE_J) });
        recompinput::set_default_mapping_for_keyboard(GameInput::X_AXIS_POS, { InputField::keyboard(SDL_SCANCODE_L) });
        recompinput::set_default_mapping_for_keyboard(GameInput::Y_AXIS_POS, { InputField::keyboard(SDL_SCANCODE_I) });
        recompinput::set_default_mapping_for_keyboard(GameInput::Y_AXIS_NEG, { InputField::keyboard(SDL_SCANCODE_K) });
    }

    // Create the standard config tabs (general/graphics/controls/sound) and load them from
    // disk. The input/render/audio paths read these configs during boot; without the tabs,
    // the gfx thread throws "General config has not been created yet" -> std::terminate.
    // (We skip the launcher UI but still need the config state. Mirrors BMHero init_config.)
    recompui::config::GeneralTabOptions general_options{};
    // Rumble works: the game's raw-SI Rumble Pak probe/motor commands are handled by the
    // hybrid pak emulation in librecomp si.cpp (answer the title-screen "Insert a Rumble Pak
    // now" prompt to enable it in-game). The slider must be on — recompinput::update_rumble
    // is a no-op when has_rumble_strength is false.
    general_options.has_rumble_strength = true;
    recompui::config::create_general_tab(general_options);
    recompui::config::create_graphics_tab();
    recompui::config::create_controls_tab();
    recompui::config::create_sound_tab();
    recompui::config::finalize();

    // ROM intake. Default: the recompui launcher menu — first run shows "Load ROM" (nfd
    // file dialog, validated against the registered hash, then stored/remembered in the
    // config path); later runs show "Start Game" (librecomp's check_all_stored_roms, run
    // inside recomp::start, revalidates the stored copy). WCW_AUTOBOOT=<path|1> bypasses
    // the launcher for dev iteration (tools/cycle.ps1, capture scripts): loads the given
    // ROM path ("1" = wcw.z64 in the working dir) and boots immediately.
    std::u8string game_id = u8"wwf.nomercy.us";
    const char* autoboot = getenv("WCW_AUTOBOOT");
    if (autoboot != nullptr) {
        std::filesystem::path autoboot_rom = (autoboot[0] != '\0' && strcmp(autoboot, "1") != 0)
            ? std::filesystem::path(autoboot) : std::filesystem::path("nomercy.z64");
        auto rom_err = recomp::select_rom(autoboot_rom, game_id);
        if (rom_err != recomp::RomValidationError::Good) {
            exit_error("WCW_AUTOBOOT ROM validation failed (err=%d) for %ls.\n", (int)rom_err, autoboot_rom.c_str());
        }
    }
    else {
        // Launcher menu options: no Mods entry (add_default_options has one) until mod
        // support is actually wired up.
        recompui::register_launcher_init_callback([](recompui::LauncherMenu* menu) {
            // Game logo in place of the default program-name text title (mirrors World
            // Tour). The image must be queued before the Image element referencing its
            // src is created.
            menu->remove_default_title();
            static const std::string logo_src = "?/game/wwf.nomercy.us/logo";
            recompui::queue_image_from_bytes_file(logo_src,
                std::vector<char>(logo_bytes, logo_bytes + logo_bytes_size));
            auto context = recompui::get_current_context();
            recompui::Image* logo = context.create_element<recompui::Image>(menu, logo_src);
            logo->set_position(recompui::Position::Absolute);
            logo->set_top(25.0f, recompui::Unit::Percent);
            logo->set_left(50.0f, recompui::Unit::Percent);
            logo->set_translate_2D(-50.0f, -50.0f, recompui::Unit::Percent);
            logo->set_width(520.0f);
            logo->set_height(520.0f * 497.0f / 1200.0f);  // icons/logo.png aspect

            auto* game_options_menu = menu->init_game_options_menu(
                supported_games[0].game_id,
                supported_games[0].mod_game_id,
                supported_games[0].display_name,
                supported_games[0].thumbnail_bytes,
                recompui::GameOptionsMenuLayout::Center);
            game_options_menu->add_start_game_or_load_rom_option();
            game_options_menu->add_setup_controls_option();
            game_options_menu->add_settings_option();
            game_options_menu->add_exit_option();
        });
    }

    // Callbacks.
    recomp::rsp::callbacks_t rsp_callbacks{ .get_rsp_microcode = get_rsp_microcode };
    ultramodern::renderer::callbacks_t renderer_callbacks{
        .create_render_context = [](uint8_t* rdram, ultramodern::renderer::WindowHandle window_handle, bool developer_mode)
            -> std::unique_ptr<ultramodern::renderer::RendererContext> {
            fprintf(stderr, "[wcw] create_render_context (RT64)...\n");
            extern uint8_t* g_ww_rdram;          // write-watch diag ([nomercy][ww] block below)
            g_ww_rdram = rdram;
            try {
                // Console presentation mode, NOT PresentEarly (which BMHero uses). PresentEarly
                // fires a present the moment a workload writes any previously-displayed
                // framebuffer — correct for one-gfx-task-per-frame games, but WCW builds each
                // frame from MULTIPLE tasks and the last task of a frame PRE-CLEARS the next
                // buffer (fbPair1). PresentEarly presented that freshly-cleared buffer -> one
                // pure black frame per game frame (verified by swapchain readback: a steady
                // [content, content, BLACK] present pattern = constant visible flicker).
                auto ctx = recompui::renderer::create_render_context(rdram, window_handle,
                    ultramodern::renderer::PresentationMode::Console, developer_mode);
                fprintf(stderr, "[wcw] create_render_context done (ctx=%p)\n", (void*)ctx.get());
                return ctx;
            } catch (const std::exception& e) {
                fprintf(stderr, "[wcw] render context EXCEPTION: %s\n", e.what());
                throw;
            } catch (...) {
                fprintf(stderr, "[wcw] render context UNKNOWN exception\n");
                throw;
            }
        },
    };
    ultramodern::gfx_callbacks_t gfx_callbacks{ .create_gfx = create_gfx, .create_window = create_window, .update_gfx = update_gfx };
    ultramodern::audio_callbacks_t audio_callbacks{ .queue_samples = queue_samples, .get_frames_remaining = get_frames_remaining, .set_frequency = set_frequency };
    ultramodern::input::callbacks_t input_callbacks{
        .poll_input = recompinput::poll_inputs,
        .get_input = recompinput::profiles::get_n64_input,
        .set_rumble = recompinput::set_rumble,
        .get_connected_device_info = get_connected_device_info,
    };
    // update_rumble runs every VI to ramp/decay the host controller's rumble motor toward the
    // state last set via set_rumble (BMHero registers it the same way).
    ultramodern::events::callbacks_t events_callbacks{ .vi_callback = recompinput::update_rumble, .gfx_init_callback = nullptr };
    ultramodern::error_handling::callbacks_t error_callbacks{ .message_box = recompui::message_box };
    ultramodern::threads::callbacks_t threads_callbacks{ .get_game_thread_name = [](const OSThread*) -> std::string { return "game"; } };

    // Dev auto-boot only: kick off the game once the runtime is up. In the normal flow the
    // launcher's Start Game option calls recomp::start_game from its click handler.
    if (autoboot != nullptr) {
        std::thread starter([game_id]() {
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
            std::u8string gid = game_id;
            recomp::start_game(gid, "");
        });
        starter.detach();
    }

#ifdef _WIN32
    // DIAGNOSTIC watchdog (opt-in): set WCW_SAMPLE=<seconds> to dump all thread stacks that
    // many seconds in (WCW_SAMPLE=1 keeps the old ~4s default), to locate a stall.
    if (const char *wcwSampleEnv = getenv("WCW_SAMPLE")) {
        int wcwSampleDelay = atoi(wcwSampleEnv);
        if (wcwSampleDelay <= 1) wcwSampleDelay = 4;
        std::thread([wcwSampleDelay]() {
            std::this_thread::sleep_for(std::chrono::seconds(wcwSampleDelay));
            wcw_sample_all_threads();
        }).detach();
    }
#endif

#ifdef _WIN32
    // [nomercy diag] WCW2K_WRITEWATCH=<vram hex>: hardware watchpoint (DR0, 4-byte write) on an
    // rdram word, applied to every thread in the process by a poller. Catches the exact host
    // RIP of whoever writes the word — built for the deterministic post-attract crash where
    // func_800E2704's saved-s0 stack slot (0x80083900) is stomped with 1 by an actor every
    // indirect probe (mesg sends, RSP tasks, scheduler clients, fb clears) has acquitted.
    // Symbolize logged RIPs offline: rip - base == the NoMercyRecompiled+0x... offset used by
    // tools/annotate_stacks.py.
    if (const char* ww = getenv("WCW2K_WRITEWATCH")) {
        fprintf(stderr, "[nomercy][ww] armed, target vram %s\n", ww);
        static uint32_t ww_vram = (uint32_t)strtoul(ww, nullptr, 16);
        static volatile uint32_t* ww_word = nullptr;
        AddVectoredExceptionHandler(1, [](EXCEPTION_POINTERS* ep) -> LONG {
            if (ep->ExceptionRecord->ExceptionCode == EXCEPTION_SINGLE_STEP) {
                uint64_t base = (uint64_t)GetModuleHandleA(nullptr);
                uint64_t rip = ep->ContextRecord->Rip;
                fprintf(stderr, "[nomercy][ww] watch hit: rip=NoMercyRecompiled+0x%llX tid=%lu value_now=%08X\n",
                    (unsigned long long)(rip - base), GetCurrentThreadId(), ww_word ? *ww_word : 0);
                // [nomercy diag] host backtrace for the first hits: recomp functions are real
                // host functions, so the return-address chain IS the game call chain
                // (symbolize each NoMercyRecompiled+0x offset against the .map). Needed for
                // the rope-matrix hunt: the RIP alone landed in guMtxF2L (a leaf converter);
                // the culprit is the caller that hands it an identity float matrix.
                static volatile long ww_bt_n = 0;
                // [nomercy diag] WCW2K_WW_BT=<n> raises the backtrace cap (default 12) —
                // needed when the interesting hits come late (registry-freeze hunt).
                static long ww_bt_max = [] { const char* v = getenv("WCW2K_WW_BT"); return v ? atol(v) : 12L; }();
                if (InterlockedIncrement(&ww_bt_n) <= ww_bt_max) {
                    CONTEXT c = *ep->ContextRecord;
                    for (int i = 0; i < 14 && c.Rip; i++) {
                        if (c.Rip >= base && c.Rip < base + 0x10000000ull)
                            fprintf(stderr, "[nomercy][ww]   bt[%d]=NoMercyRecompiled+0x%llX\n", i, (unsigned long long)(c.Rip - base));
                        DWORD64 imgBase = 0;
                        PRUNTIME_FUNCTION fe = RtlLookupFunctionEntry(c.Rip, &imgBase, nullptr);
                        if (fe == nullptr) { // leaf frame: return address sits at Rsp
                            c.Rip = *(DWORD64*)c.Rsp;
                            c.Rsp += 8;
                            continue;
                        }
                        PVOID handlerData = nullptr;
                        DWORD64 establisher = 0;
                        RtlVirtualUnwind(UNW_FLAG_NHANDLER, imgBase, c.Rip, fe, &c, &handlerData, &establisher, nullptr);
                    }
                }
                return EXCEPTION_CONTINUE_EXECUTION;
            }
            return EXCEPTION_CONTINUE_SEARCH;
        });
        std::thread([]() {
            // rdram pointer is stashed by the create_render_context callback.
            extern uint8_t* g_ww_rdram;
            while (g_ww_rdram == nullptr) { Sleep(100); }
            uint8_t* rdram_base = g_ww_rdram;
            uint64_t watch_addr = (uint64_t)(rdram_base + (ww_vram - 0x80000000u));
            ww_word = (volatile uint32_t*)watch_addr;
            fprintf(stderr, "[nomercy][ww] watching vram 0x%08X at host %p\n", ww_vram, (void*)watch_addr);
            std::set<DWORD> armed;
            const DWORD self = GetCurrentThreadId();
            while (true) {
                HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
                if (snap != INVALID_HANDLE_VALUE) {
                    THREADENTRY32 te{ sizeof(te) };
                    for (BOOL ok = Thread32First(snap, &te); ok; ok = Thread32Next(snap, &te)) {
                        if (te.th32OwnerProcessID != GetCurrentProcessId() || te.th32ThreadID == self) continue;
                        if (armed.count(te.th32ThreadID)) continue;
                        HANDLE th = OpenThread(THREAD_ALL_ACCESS, FALSE, te.th32ThreadID);
                        if (!th) continue;
                        if (SuspendThread(th) != (DWORD)-1) {
                            CONTEXT c{}; c.ContextFlags = CONTEXT_DEBUG_REGISTERS;
                            if (GetThreadContext(th, &c)) {
                                c.Dr0 = watch_addr;
                                c.Dr7 = (c.Dr7 & ~0xF0003ull) | 0x1 | (0b01ull << 16) | (0b11ull << 18); // local DR0, write, 4 bytes
                                if (SetThreadContext(th, &c)) armed.insert(te.th32ThreadID);
                            }
                            ResumeThread(th);
                        }
                        CloseHandle(th);
                    }
                    CloseHandle(snap);
                }
                Sleep(300);
            }
        }).detach();
    }
#endif

    // NOTE: graphics-API init (both D3D12 and Vulkan) aborts in a headless/virtual-display
    // session even with a GPU present; it should initialize on a normal local desktop.
    // Leave api_option at its default (Auto -> D3D12 on Windows). To override for testing:
    //   auto gcfg = ultramodern::renderer::get_graphics_config();
    //   gcfg.api_option = ultramodern::renderer::GraphicsApi::Vulkan;  // or D3D12
    //   ultramodern::renderer::set_graphics_config(gcfg);

    fprintf(stderr, "[wcw] calling recomp::start...\n");
    recomp::start(version, {}, rsp_callbacks, renderer_callbacks, audio_callbacks,
        input_callbacks, gfx_callbacks, events_callbacks, error_callbacks, threads_callbacks);
    fprintf(stderr, "[wcw] recomp::start returned\n");

    NFD_Quit();
    return 0;
}
