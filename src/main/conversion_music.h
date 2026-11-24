#pragma once
// [wcw fullconv] aki-roadmap#322: plays the active conversion's music pack (<conversion>/music/0x<track>.mp3, the
// player's own files) in place of the hack community's external "music tool". See music_policy.h for the contract.
#include <cstdint>
#include <filesystem>

namespace conversion_music {
// Once, before the game runs: the pack folder (empty / missing = off; nothing else here does anything then).
void init(const std::filesystem::path& music_dir);
// Every audio batch, on the game's thread: follows the game's track (rdram = the game's RDRAM base) and keeps the
// music going while the game's audio is. Cheap when off.
void on_game_audio(const uint8_t* rdram);
bool active();
}
