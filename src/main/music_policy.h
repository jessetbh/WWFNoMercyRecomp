#pragma once
// [wcw fullconv] aki-roadmap#322: a conversion's own music pack (the player's files, never shipped). Pure decisions,
// tested in tests/music_policy_tests.cpp; conversion_music.cpp plays what they pick.
//
// The contract is the hack community's "music tool" (stubbs' No Mercy Thing, Walter's Music Mod 1.2, as WWF Legends:
// Challenge 64 ships it): the game's current music track, the halfword music_update (Rev 1 0x80017C64) keeps at
// Rev 1 0x80048504 (Rev 0 0x800485A4), names a file `0x<track in hex>.mp3` in the pack (any case: the packs use
// `0xE.mp3`, the tool asks for `0xe.mp3` on a case-blind file system). The file loops until the track changes.
#include <cstdint>
#include <string>

namespace music_policy {

constexpr uint32_t kTrackAddr = 0x80048504;  // No Mercy Rev 1: music_update's current track (decomp bulk.toml)
constexpr uint16_t kNoTrack = 0xFFFF;         // between tracks (the game's stop / fade): nothing changes
constexpr int kTrackCount = 80;               // music_update's tracks are 0-79

// The track id a pack file stands for: "0x1C.mp3" / "0x1c.MP3" -> 28; -1 for anything else (the packs' "0x1C - Razor
// Ramon.txt" notes, other files, ids past the game's 80 tracks).
inline int track_of_file(const std::string& name) {
    if (name.size() < 7 || name[0] != '0' || (name[1] != 'x' && name[1] != 'X')) return -1;
    const size_t dot = name.size() - 4;
    auto lower = [](char c) { return (c >= 'A' && c <= 'Z') ? char(c - 'A' + 'a') : c; };
    if (name[dot] != '.' || lower(name[dot + 1]) != 'm' || lower(name[dot + 2]) != 'p' || lower(name[dot + 3]) != '3') return -1;
    if (dot - 2 < 1 || dot - 2 > 2) return -1;  // one or two hex digits
    int v = 0;
    for (size_t i = 2; i < dot; i++) {
        const char c = lower(name[i]);
        int d = (c >= '0' && c <= '9') ? c - '0' : (c >= 'a' && c <= 'f') ? c - 'a' + 10 : -1;
        if (d < 0) return -1;
        v = v * 16 + d;
    }
    return v < kTrackCount ? v : -1;
}

// Follows the game's track, one read per audio batch. step() returns the track to switch to, or -1 for no change.
// A new track (any value 0-79 other than the last one seen) is a change; kNoTrack and out-of-range values aren't, so
// the stop between two tracks doesn't cut the music (the music tool doesn't either). The same track after another one
// (0x0, the packs' blank, between two entrances to one theme) starts it again from the top.
struct Follower {
    int last = -1;
    int step(uint16_t value) {
        if (value == kNoTrack || value >= kTrackCount) return -1;
        if (int(value) == last) return -1;
        last = value;
        return value;
    }
};

// What to do with a picked track: play the pack's file, or (no file for it) stop ours so the game's own music for
// that track is what's heard (the fallback; in a pack-made hack such as Legends the ROM's track is silent).
enum class Play { File, GameOwn };
inline Play play_for(bool pack_has_file) { return pack_has_file ? Play::File : Play::GameOwn; }

// The player's reads of one file: data, its end, or an error. A file that ends with no data since it last started (a
// tag-only or empty mp3) or fails to read is stopped, not looped (review: rewinding it again would spin a core).
enum class Read { Data, Gap, End, Error };  // Gap: a read with no samples (neither data nor the end)
enum class Next { Keep, Rewind, Stop };
struct LoopGuard {
    bool data = false;  // data since the file (re)started
    void start() { data = false; }
    Next on(Read r) {
        if (r == Read::Data) { data = true; return Next::Keep; }
        if (r == Read::Gap) return Next::Keep;  // doesn't count as data: a file of only gaps then its end stops
        if (r == Read::End && data) { data = false; return Next::Rewind; }
        return Next::Stop;
    }
};

// The game's audio has stopped (paused, loading, closing) when no batch came for quiet_ms: the music pauses too.
constexpr uint64_t kQuietMs = 250;
inline bool game_quiet(uint64_t now_ms, uint64_t last_batch_ms) { return last_batch_ms == 0 || now_ms - last_batch_ms > kQuietMs; }

// The pick handed from the game's audio thread to the player: track (-1 = none) and a serial in one word, so the player
// never pairs one pick's track with another's serial (review: that restarted a track twice).
inline uint64_t pack_pick(uint32_t serial, int track) { return (uint64_t(serial) << 32) | uint32_t(track + 1); }
inline uint32_t pick_serial(uint64_t p) { return uint32_t(p >> 32); }
inline int pick_track(uint64_t p) { return int(uint32_t(p)) - 1; }

}  // namespace music_policy
