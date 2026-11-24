// aki-roadmap#322: src/main/music_policy.h. Plain C++, no game, no files; built and run with every game build.
#include "../src/main/music_policy.h"
#include <cstdio>

static int failed = 0, run = 0;
#define CHECK(c) do { run++; if (!(c)) { failed++; std::fprintf(stderr, "FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

int main() {
    using namespace music_policy;
    // pack file names (the Legends pack's own spellings), the tool's spelling, and what isn't a track
    CHECK(track_of_file("0xE.mp3") == 0xE);
    CHECK(track_of_file("0xe.mp3") == 0xE);
    CHECK(track_of_file("0x1C.mp3") == 0x1C);
    CHECK(track_of_file("0x4f.MP3") == 0x4F);
    CHECK(track_of_file("0x0.mp3") == 0);
    CHECK(track_of_file("0x1C - Razor Ramon.txt") == -1);
    CHECK(track_of_file("0x1C - Razor Ramon.mp3") == -1);
    CHECK(track_of_file("0x50.mp3") == -1);   // past the game's 80 tracks
    CHECK(track_of_file("0x100.mp3") == -1);
    CHECK(track_of_file("0x.mp3") == -1);
    CHECK(track_of_file("1C.mp3") == -1);
    CHECK(track_of_file("0x1G.mp3") == -1);
    CHECK(track_of_file("0x1C.mp4") == -1);
    CHECK(track_of_file("0x23_2.mp3") == -1);  // the tool's random alternates need its script's list: not read

    // the order a Legends walk logged (title, Exhibition menu, two Hogan entrances, commentary)
    Follower f;
    CHECK(f.step(0x0) == 0x0);
    CHECK(f.step(0xE) == 0xE);
    CHECK(f.step(0xE) == -1);         // held: no restart
    CHECK(f.step(kNoTrack) == -1);    // the stop between tracks doesn't cut it
    CHECK(f.step(0x0) == 0x0);        // the blank
    CHECK(f.step(0x10) == 0x10);
    CHECK(f.step(kNoTrack) == -1);
    CHECK(f.step(0x0) == 0x0);
    CHECK(f.step(0x38) == 0x38);
    CHECK(f.step(kNoTrack) == -1);
    CHECK(f.step(0x0) == 0x0);
    CHECK(f.step(0x38) == 0x38);      // the same theme again after the blank: from the top
    CHECK(f.step(0x3) == 0x3);
    CHECK(f.step(0x50) == -1);        // out of range: ignored, not a stop
    CHECK(f.step(0x3) == -1);

    CHECK(play_for(true) == Play::File);
    CHECK(play_for(false) == Play::GameOwn);

    // a file loops at its end, but one that ends with no data, or fails, stops (no spinning)
    LoopGuard g;
    g.start();
    CHECK(g.on(Read::Data) == Next::Keep);
    CHECK(g.on(Read::End) == Next::Rewind);
    CHECK(g.on(Read::Data) == Next::Keep);
    CHECK(g.on(Read::End) == Next::Rewind);
    CHECK(g.on(Read::End) == Next::Stop);     // rewound, then ended again with nothing
    g.start();
    CHECK(g.on(Read::End) == Next::Stop);     // a tag-only file
    g.start();
    CHECK(g.on(Read::Data) == Next::Keep);
    CHECK(g.on(Read::Error) == Next::Stop);   // a read error mid-file
    g.start();
    CHECK(g.on(Read::Gap) == Next::Keep);
    CHECK(g.on(Read::End) == Next::Stop);     // only gaps, then the end: not looped forever

    CHECK(game_quiet(1000, 0));               // no batch yet
    CHECK(!game_quiet(1000, 900));
    CHECK(!game_quiet(1000, 1000 - kQuietMs));
    CHECK(game_quiet(1000, 1000 - kQuietMs - 1));

    CHECK(pick_track(pack_pick(7, -1)) == -1 && pick_serial(pack_pick(7, -1)) == 7);
    CHECK(pick_track(pack_pick(0xFFFFFFFFu, 79)) == 79 && pick_serial(pack_pick(0xFFFFFFFFu, 79)) == 0xFFFFFFFFu);
    CHECK(pick_track(pack_pick(3, 0)) == 0);

    std::printf("[music policy tests] %d tests, %d failed\n", run, failed);
    return failed ? 1 : 0;
}
