// [wcw fullconv] aki-roadmap#322: the active conversion's music pack, played by the port (see conversion_music.h).
// Decoding: Windows Media Foundation (built into Windows: no decoder of our own, nothing shipped). It is delay-loaded
// and probed first, so a Windows without it (N editions with no Media Feature Pack) still runs the game, without the
// pack's music. Output: a second SDL audio device at 44.1 kHz, opened on the game's audio thread (where the game's own
// device is opened too), so the music keeps its quality instead of going through the game's ~22 kHz stream; files at
// another rate go through an SDL_AudioStream. It runs only while the game's audio does (a paused game pauses it).
#include "conversion_music.h"
#include "music_policy.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <map>
#include <string>
#include <thread>
#include <vector>

#include "SDL.h"
#include "recompui/config.h"

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mferror.h>
#endif

namespace {

constexpr int kRate = 44100;
std::map<int, std::filesystem::path> g_files;   // track -> the pack's file (built before the player starts; read-only)
music_policy::Follower g_follower;               // the game's audio thread only
uint32_t g_serial = 0;                           // the game's audio thread only
std::atomic<uint64_t> g_pick{music_policy::pack_pick(0, -1)};
std::atomic<uint64_t> g_last_game_audio_ms{0};
SDL_AudioDeviceID g_dev = 0;
bool g_on = false;

uint64_t now_ms() {
    return (uint64_t)std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
std::string path_u8(const std::filesystem::path& p) {
    const std::u8string s = p.u8string();
    return std::string(s.begin(), s.end());
}

#ifdef _WIN32
struct Reader {
    IMFSourceReader* r = nullptr;
    int rate = 0, channels = 0;
    ~Reader() { close(); }
    void close() { if (r) { r->Release(); r = nullptr; } }
    bool open(const std::filesystem::path& p) {
        close();
        if (FAILED(MFCreateSourceReaderFromURL(p.wstring().c_str(), nullptr, &r))) { r = nullptr; return false; }
        r->SetStreamSelection((DWORD)MF_SOURCE_READER_ALL_STREAMS, FALSE);
        r->SetStreamSelection((DWORD)MF_SOURCE_READER_FIRST_AUDIO_STREAM, TRUE);
        IMFMediaType* t = nullptr;
        if (FAILED(MFCreateMediaType(&t))) { close(); return false; }
        t->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Audio);
        t->SetGUID(MF_MT_SUBTYPE, MFAudioFormat_PCM);
        t->SetUINT32(MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
        HRESULT hr = r->SetCurrentMediaType((DWORD)MF_SOURCE_READER_FIRST_AUDIO_STREAM, nullptr, t);
        t->Release();
        if (FAILED(hr)) { close(); return false; }
        IMFMediaType* cur = nullptr;
        if (FAILED(r->GetCurrentMediaType((DWORD)MF_SOURCE_READER_FIRST_AUDIO_STREAM, &cur))) { close(); return false; }
        UINT32 sr = 0, ch = 0;
        cur->GetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND, &sr);
        cur->GetUINT32(MF_MT_AUDIO_NUM_CHANNELS, &ch);
        cur->Release();
        rate = (int)sr; channels = (int)ch;
        if (rate <= 0 || channels < 1 || channels > 2) { close(); return false; }
        return true;
    }
    bool rewind() {
        PROPVARIANT pos; PropVariantInit(&pos);
        pos.vt = VT_I8; pos.hVal.QuadPart = 0;
        const HRESULT hr = r->SetCurrentPosition(GUID_NULL, pos);
        PropVariantClear(&pos);
        return SUCCEEDED(hr);
    }
    // Appends decoded stereo frames to out.
    music_policy::Read read(std::vector<int16_t>& out) {
        DWORD flags = 0; IMFSample* s = nullptr;
        if (FAILED(r->ReadSample((DWORD)MF_SOURCE_READER_FIRST_AUDIO_STREAM, 0, nullptr, &flags, nullptr, &s))) return music_policy::Read::Error;
        if (flags & MF_SOURCE_READERF_ERROR) { if (s) s->Release(); return music_policy::Read::Error; }
        if (flags & MF_SOURCE_READERF_ENDOFSTREAM) { if (s) s->Release(); return music_policy::Read::End; }
        if (!s) return music_policy::Read::Gap;  // a gap: nothing this time, not the end
        bool got = false;
        IMFMediaBuffer* b = nullptr;
        if (SUCCEEDED(s->ConvertToContiguousBuffer(&b))) {
            BYTE* data = nullptr; DWORD len = 0;
            if (SUCCEEDED(b->Lock(&data, nullptr, &len))) {
                const int16_t* pcm = reinterpret_cast<const int16_t*>(data);
                const size_t n = len / sizeof(int16_t);
                if (channels == 2) out.insert(out.end(), pcm, pcm + n);
                else for (size_t i = 0; i < n; i++) { out.push_back(pcm[i]); out.push_back(pcm[i]); }
                got = n > 0;
                b->Unlock();
            }
            b->Release();
        }
        s->Release();
        return got ? music_policy::Read::Data : music_policy::Read::Gap;
    }
};

void run() {
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_LITE))) {
        std::fprintf(stderr, "[music] Media Foundation didn't start: no pack music this session\n");
        return;
    }
    Reader reader;
    music_policy::LoopGuard guard;
    SDL_AudioStream* conv = nullptr;          // the file's rate -> 44.1 kHz (null when it is 44.1 kHz)
    int playing = -1; uint32_t serial = 0;
    bool paused = true;
    int fade_in = 0;                          // frames of fade-in still to go
    std::vector<int16_t> chunk, out;
    auto stop = [&]() {
        reader.close(); playing = -1;
        if (conv) { SDL_FreeAudioStream(conv); conv = nullptr; }
    };
    for (;;) {
        const uint64_t pick = g_pick.load();
        if (music_policy::pick_serial(pick) != serial) {
            serial = music_policy::pick_serial(pick);
            const int want = music_policy::pick_track(pick);
            SDL_ClearQueuedAudio(g_dev);      // the old track stops at once (the game's own change is a cut too)
            stop();
            auto it = want >= 0 ? g_files.find(want) : g_files.end();
            if (it != g_files.end()) {
                if (reader.open(it->second)) {
                    if (reader.rate != kRate) conv = SDL_NewAudioStream(AUDIO_S16SYS, 2, reader.rate, AUDIO_S16SYS, 2, kRate);
                    if (reader.rate != kRate && !conv) {
                        std::fprintf(stderr, "[music] track 0x%X: can't convert %d Hz; the game's own music\n", want, reader.rate);
                        stop();
                    } else {
                        playing = want; guard.start(); fade_in = kRate / 20;  // 50 ms
                        std::fprintf(stderr, "[music] track 0x%X: %s\n", want, path_u8(it->second.filename()).c_str());
                    }
                } else {
                    std::fprintf(stderr, "[music] track 0x%X: %s can't be decoded; the game's own music\n", want, path_u8(it->second.filename()).c_str());
                }
            }
        }
        const bool quiet = music_policy::game_quiet(now_ms(), g_last_game_audio_ms.load());
        if (playing >= 0 && paused != quiet) { SDL_PauseAudioDevice(g_dev, quiet ? 1 : 0); paused = quiet; }
        // keep about 150 ms queued, so a volume change or a new track is heard soon
        if (playing < 0 || paused || SDL_GetQueuedAudioSize(g_dev) / 4 > (uint32_t)kRate * 15 / 100) {
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }
        chunk.clear();
        const music_policy::Read rd = reader.read(chunk);
        const music_policy::Next next = guard.on(rd);
        if (next == music_policy::Next::Stop) {
            std::fprintf(stderr, "[music] track 0x%X: %s; stopped\n", playing, rd == music_policy::Read::Error ? "read error" : "no sound in the file");
            stop();
            continue;
        }
        if (next == music_policy::Next::Rewind) {  // the end: loop it, as the music tool does
            if (!reader.rewind()) { std::fprintf(stderr, "[music] track 0x%X: can't loop; stopped\n", playing); stop(); }
            continue;
        }
        if (chunk.empty()) continue;
        const std::vector<int16_t>* pcm = &chunk;
        if (conv) {
            SDL_AudioStreamPut(conv, chunk.data(), (int)(chunk.size() * sizeof(int16_t)));
            out.resize((size_t)SDL_AudioStreamAvailable(conv) / sizeof(int16_t));
            out.resize((size_t)std::max(0, SDL_AudioStreamGet(conv, out.data(), (int)(out.size() * sizeof(int16_t)))) / sizeof(int16_t));
            pcm = &out;
        }
        std::vector<int16_t>& v = const_cast<std::vector<int16_t>&>(*pcm);
        const float vol = std::clamp((float)recompui::config::sound::get_main_volume() / 100.0f, 0.0f, 1.0f);
        for (size_t i = 0; i + 1 < v.size(); i += 2) {
            float g = vol;
            if (fade_in > 0) { g *= 1.0f - (float)fade_in / (float)(kRate / 20); fade_in--; }
            v[i] = (int16_t)(v[i] * g); v[i + 1] = (int16_t)(v[i + 1] * g);
        }
        if (!v.empty()) SDL_QueueAudio(g_dev, v.data(), (Uint32)(v.size() * sizeof(int16_t)));
    }
    // (no exit: the thread ends with the process, as the game's own audio does)
}

// Media Foundation is delay-loaded (CMakeLists /DELAYLOAD): only call into it once both DLLs are known to load.
bool media_foundation_present() {
    HMODULE a = LoadLibraryW(L"mfplat.dll"), b = LoadLibraryW(L"mfreadwrite.dll");
    const bool ok = a && b;
    if (b) FreeLibrary(b);
    if (a) FreeLibrary(a);
    return ok;
}
#endif

}  // namespace

namespace conversion_music {

void init(const std::filesystem::path& music_dir) {
#ifdef _WIN32
    std::error_code ec;
    if (music_dir.empty() || !std::filesystem::is_directory(music_dir, ec)) return;
    for (auto it = std::filesystem::directory_iterator(music_dir, ec); !ec && it != std::filesystem::directory_iterator(); it.increment(ec)) {
        if (!it->is_regular_file(ec)) continue;
        const int t = music_policy::track_of_file(path_u8(it->path().filename()));
        if (t >= 0) g_files[t] = it->path();
    }
    std::fprintf(stderr, "[music] pack %s: %zu tracks\n", path_u8(music_dir).c_str(), g_files.size());
    if (g_files.empty()) return;
    if (!media_foundation_present()) {
        std::fprintf(stderr, "[music] this Windows has no Media Foundation (an N edition without the Media Feature Pack?): no pack music\n");
        return;
    }
    // Opened here, on the game's audio thread, like the game's own device (SDL's open isn't safe from two threads).
    SDL_AudioSpec want{}, have{};
    want.freq = kRate; want.format = AUDIO_S16SYS; want.channels = 2; want.samples = 1024;
    g_dev = SDL_OpenAudioDevice(nullptr, 0, &want, &have, 0);
    if (g_dev == 0) {
        std::fprintf(stderr, "[music] no audio device for the pack: %s\n", SDL_GetError());
        return;
    }
    g_on = true;
    std::thread(run).detach();  // ends with the process (the game exits through quick-exit paths)
#else
    (void)music_dir;
#endif
}

bool active() { return g_on; }

void on_game_audio(const uint8_t* rdram) {
    if (!g_on || rdram == nullptr) return;
    g_last_game_audio_ms.store(now_ms());
    // RDRAM keeps 32-bit words in host order: a halfword at N64 address a is at host offset (a ^ 2)
    const uint32_t off = (music_policy::kTrackAddr - 0x80000000u) ^ 2u;
    const uint16_t v = *reinterpret_cast<const uint16_t*>(rdram + off);
    const int t = g_follower.step(v);
    if (t < 0) return;
    const bool has = g_files.count(t) != 0;
    if (!has) std::fprintf(stderr, "[music] track 0x%X: no file in the pack; the game's own music\n", t);
    g_pick.store(music_policy::pack_pick(++g_serial, music_policy::play_for(has) == music_policy::Play::File ? t : -1));
}

}  // namespace conversion_music
