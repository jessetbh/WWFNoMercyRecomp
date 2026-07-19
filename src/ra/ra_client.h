#pragma once
#include <cstdint>

// RetroAchievements prototype (softcore only), env-gated:
//   WCW_RA=1                 enable; with WCW_RA_USER + WCW_RA_PASS (or
//                            WCW_RA_TOKEN) it logs in and loads the real
//                            achievement set for the ROM
//   WCW_RA_TEST=8:0xADDR:0xVAL  offline plumbing proof: a local achievement
//                            "8/16/32-bit value at RDRAM offset ADDR == VAL"
//                            evaluated per frame, no server involved
//   WCW_RA_SWAP=1            byte-order fallback (XOR^3 peeks) if the default
//                            raw layout proves wrong against real sets
// All events are logged to stderr with an [ra] prefix.
namespace wcw::ra {
// Called once from the osDriveRomInit bridge (first boot-time hook that holds
// the rdram base). Starts the RA worker thread on first call when WCW_RA=1.
void notify_rdram(uint8_t* rdram);
}
