// [wcw fullconv] aki-roadmap#322: the active conversion's music pack, played by the port (see conversion_music.h).
// Decoding: Windows Media Foundation (built into Windows: no decoder of our own, nothing shipped). Output: a second SDL
// audio device at the file's own rate, so the music keeps its quality instead of going through the game's ~22 kHz
// stream; the system mixes the two. It runs only while the game's audio does (a paused or stalled game pauses it).
#include "conversion_music.h"
#include "music_policy.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <map>
#include <mutex>
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
#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mfreadwrite.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "ole32.lib")
#endif

namespace {

std::map<int, std::filesystem::path> g_files;  // track -> the pack's file
music_policy::Follower g_follower;
std::atomic<int> g_want{-1};                     // the track to play (-1: none, the game's own music)
std::atomic<uint32_t> g_want_serial{0};          // bumps on every pick, so the same track again restarts it
std::atomic<uint64_t> g_last_game_audio_ms{0};
std::atomic<bool> g_quit{false};
std::thread g_thread;
bool g_on = false;

uint64_t now_ms() {
    return (uint64_t)std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
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
        MFCreateMediaType(&t);
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
    void rewind() {
        PROPVARIANT pos; PropVariantInit(&pos);
        pos.vt = VT_I8; pos.hVal.QuadPart = 0;
        r->SetCurrentPosition(GUID_NULL, pos);
        PropVariantClear(&pos);
    }
    // Appends decoded stereo frames to out; false at the end of the file (or an error).
    bool read(std::vector<int16_t>& out) {
        DWORD flags = 0; IMFSample* s = nullptr;
        if (FAILED(r->ReadSample((DWORD)MF_SOURCE_READER_FIRST_AUDIO_STREAM, 0, nullptr, &flags, nullptr, &s))) return false;
        if (flags & MF_SOURCE_READERF_ENDOFSTREAM) { if (s) s->Release(); return false; }
        if (!s) return true;  // a gap: no data this time
        IMFMediaBuffer* b = nullptr;
        if (SUCCEEDED(s->ConvertToContiguousBuffer(&b))) {
            BYTE* data = nullptr; DWORD len = 0;
            if (SUCCEEDED(b->Lock(&data, nullptr, &len))) {
                const int16_t* pcm = reinterpret_cast<const int16_t*>(data);
                const size_t n = len / sizeof(int16_t);
                if (channels == 2) out.insert(out.end(), pcm, pcm + n);
                else for (size_t i = 0; i < n; i++) { out.push_back(pcm[i]); out.push_back(pcm[i]); }
                b->Unlock();
            }
            b->Release();
        }
        s->Release();
        return true;
    }
};

void run() {
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    MFStartup(MF_VERSION, MFSTARTUP_LITE);
    SDL_AudioDeviceID dev = 0; int dev_rate = 0;
    Reader reader;
    int playing = -1; uint32_t serial = 0;
    bool paused = true;
    int fade_in = 0;                        // frames of fade-in still to go
    std::vector<int16_t> chunk;
    auto open_device = [&](int rate) {
        if (dev && dev_rate == rate) return;
        if (dev) SDL_CloseAudioDevice(dev);
        SDL_AudioSpec want{}, have{};
        want.freq = rate; want.format = AUDIO_S16SYS; want.channels = 2; want.samples = 1024;
        dev = SDL_OpenAudioDevice(nullptr, 0, &want, &have, 0);
        dev_rate = dev ? rate : 0;
        paused = true;
        if (!dev) std::fprintf(stderr, "[music] no audio device: %s\n", SDL_GetError());
    };
    while (!g_quit.load()) {
        const uint32_t s = g_want_serial.load();
        if (s != serial) {
            serial = s;
            const int want = g_want.load();
            if (dev) SDL_ClearQueuedAudio(dev);  // the old track stops at once (the game's own change is a cut too)
            reader.close(); playing = -1;
            auto it = want >= 0 ? g_files.find(want) : g_files.end();
            if (it != g_files.end()) {
                if (reader.open(it->second)) {
                    open_device(reader.rate);
                    playing = want; fade_in = reader.rate / 20;  // 50 ms
                    std::fprintf(stderr, "[music] track 0x%X: %s\n", want, it->second.filename().string().c_str());
                } else {
                    std::fprintf(stderr, "[music] track 0x%X: %s can't be decoded; the game's own music\n", want, it->second.filename().string().c_str());
                }
            } else if (want >= 0) {
                std::fprintf(stderr, "[music] track 0x%X: no file in the pack; the game's own music\n", want);
            }
        }
        // the game's audio stopped (paused, loading, closing): so does ours
        const bool game_quiet = now_ms() - g_last_game_audio_ms.load() > 250;
        if (dev && playing >= 0 && paused != game_quiet) { SDL_PauseAudioDevice(dev, game_quiet ? 1 : 0); paused = game_quiet; }
        if (playing < 0 || !dev || paused) { std::this_thread::sleep_for(std::chrono::milliseconds(10)); continue; }
        // keep about 150 ms queued, so a volume change or a new track is heard soon
        const uint32_t queued_frames = SDL_GetQueuedAudioSize(dev) / 4;
        if (queued_frames > (uint32_t)dev_rate * 15 / 100) { std::this_thread::sleep_for(std::chrono::milliseconds(10)); continue; }
        chunk.clear();
        if (!reader.read(chunk)) { reader.rewind(); continue; }  // the end: loop it, as the music tool does
        const float vol = std::clamp((float)recompui::config::sound::get_main_volume() / 100.0f, 0.0f, 1.0f);
        for (size_t i = 0; i + 1 < chunk.size(); i += 2) {
            float g = vol;
            if (fade_in > 0) { g *= 1.0f - (float)fade_in / (float)(dev_rate / 20); fade_in--; }
            chunk[i] = (int16_t)(chunk[i] * g); chunk[i + 1] = (int16_t)(chunk[i + 1] * g);
        }
        SDL_QueueAudio(dev, chunk.data(), (Uint32)(chunk.size() * sizeof(int16_t)));
    }
    reader.close();
    if (dev) SDL_CloseAudioDevice(dev);
    MFShutdown();
    CoUninitialize();
}
#endif

}  // namespace

namespace conversion_music {

void init(const std::filesystem::path& music_dir) {
#ifdef _WIN32
    std::error_code ec;
    if (music_dir.empty() || !std::filesystem::is_directory(music_dir, ec)) return;
    for (const auto& e : std::filesystem::directory_iterator(music_dir, ec)) {
        if (!e.is_regular_file(ec)) continue;
        const int t = music_policy::track_of_file(e.path().filename().string());
        if (t >= 0) g_files[t] = e.path();
    }
    std::fprintf(stderr, "[music] pack %s: %zu tracks\n", music_dir.string().c_str(), g_files.size());
    if (g_files.empty()) return;
    g_on = true;
    g_thread = std::thread(run);
    g_thread.detach();  // ends with the process (the game exits through quick-exit paths)
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
    g_want.store(music_policy::play_for(g_files.count(t) != 0) == music_policy::Play::File ? t : -1);
    g_want_serial.fetch_add(1);
    if (g_files.count(t) == 0) std::fprintf(stderr, "[music] track 0x%X: no file in the pack; the game's own music\n", t);
}

}  // namespace conversion_music
