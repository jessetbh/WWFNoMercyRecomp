# libultra identification evidence — WWF No Mercy

How every named function in `tools/gen_symbols.py`'s `RENAME` map was identified.
Convention inherited from the sister projects (see World Tour's
`disasm/libultra.md` for the original per-function evidence, and WM2000's for the
fingerprint-transfer method this project uses).

## 1. Fingerprint transfer from WM2000 (2026-07-14, tools/nm_recon2.py)

No Mercy (Nov 2000) is WM2000's direct sequel by the same AKI team, one year
apart. Masked full-body fingerprinting (opcode stream with relocatable fields
masked, per WM2000's recon2.py method) of WM2000's named fixed-segment set
against this ROM's fixed segment matched **36/42 uniquely** — a family record
(Revenge→WM2000 was 27/39). Those 36 names were transferred verbatim; see the
first RENAME block in tools/gen_symbols.py for the list (osDisableInt through
osDestroyThread).

## 2. Ambiguity resolutions (2026-07-14, tools/nm_recon3.py)

- **osAiGetLength / osAiGetStatus** — adjacent tiny leaf pair at
  0x80030710/0x80030720; Length reads AI_LEN (0xA4500004), Status reads
  AI_STATUS (0xA450000C). A third shape-identical candidate read SP_STATUS and
  was rejected.
- **osViGetCurrentFramebuffer / osViGetNextFramebuffer** — adjacent pair 0x40
  apart at 0x80037E30/0x80037E70, WT's source order (Current first); they load
  the vi-manager global at +0x4BF0/+0x4BF4 and bracket the load with jals to
  the transferred __osDisableInt/__osRestoreInt addresses (cross-check passed).
- **__osSiDeviceBusy** — of 2 shape matches, 0x8003FBE0 reads SI_STATUS
  (lui 0xA480 / lw 0x18 / andi 3); the other candidate read SP_STATUS.
- **osGetCount** — too small to fingerprint; unique `mfc0 $v0, $Count`
  (0x40024800) byte-signature hit in the whole fixed segment → 0x8003E270.
  NEVER stub (WM2000 frozen-clock lesson).

## 3. FLASH save driver (2026-07-14, first-boot crash analysis)

The first boot crashed with a host access violation reading
rdram+0x24600010 = guest **0xA4600010 = PI_STATUS_REG**: the game's flash save
driver ran its original raw-PI polling code at startup. symbolize.py mapped the
crash chain to game code → func_8002FED0 → func_8002FFE0 → func_8003D660 →
osEPiWriteIo → __osEPiRawWriteIo (PI_STATUS poll). Reading the driver
identified the complete libultra 2.0L `osflash.c` public API, split across two
text regions (0x8002FED0–0x8003070C and 0x8003D660–0x8003D75x):

| vram | name | evidence (flash command constants + structure) |
|---|---|---|
| 0x8002FED0 | osFlashInit | handler setup: baseAddress **0xA8000000** (flash bus), latency/pgs/rls bytes 5/0xC/0xF/2/1; osEPiLinkHandle; calls ReadId then compares vendor vs MX IDs **0xC2001E/0xC20001/0xC20000**, stores version in D_80090B90; returns &D_800A8448 (handler) |
| 0x8002FFE0 | osFlashReadId | ReadStatus; CMD **0xE1000000** (ID); osInvalDCache(0x10) + osEPiStartDma READ + osRecvMesg; returns type/vendor from DMA buffer |
| 0x800300E0 | osFlashAllErase | CMD **0x3C000000** (erase-all) + **0x78000000** (execute); osSetTimer(0xABA95) poll loop on status bit 2; final status vs 0x08/0x48 |
| 0x80030220 | osFlashSectorErase | CMD **0x4B000000\|page** + **0x78000000**; same wait, timeout 0x8F0D1 |
| 0x80030370 | osFlashWriteBuffer | CMD **0xB4000000** (page program); builds OSIoMesg, size **0x80**, osEPiStartDma WRITE |
| 0x80030410 | osFlashWriteArray | version==1 re-issues 0xB4000000; CMD **0xA5000000\|page** (program execute); timer 0x249F, poll status bit 1, final vs 0x04/0x44 |
| 0x80030560 | osFlashReadArray | CMD **0xF0000000** (read array); chunked DMA loop over 0x100-page pieces via __osFlashGetAddr + osEPiStartDma READ + osRecvMesg |
| 0x8003D660 | osFlashReadStatus | CMD **0xD2000000** twice with osEPiReadIo between, low byte to out-param; handler D_800A8448, base D_800A8454, cmd reg = base\|0x10000 |
| 0x8003D700 | osFlashClearStatus | two osEPiWriteIo, no wait |

Internals identified along the way (NOT renamed — only reachable from the nine
publics, verified by a full `jal` scan of the fixed segment):

- 0x8003D250 `__osEPiRawReadIo` / 0x8003D3C0 `__osEPiRawWriteIo` — PI_STATUS
  busy-poll, domain latency/PGS/RLS/PWD sync from handler vs __osCurrentHandle
  table D_80053440, then lw/sw at 0xA0000000|devAddr.
- 0x8003D530 `osEPiWriteIo` / 0x8003D590 `osEPiReadIo` — __osPiGetAccess
  (0x8002F3D4) → raw op → __osPiRelAccess (0x8002F440).
- 0x8003D5F0 `osEPiLinkHandle` — __osDisableInt, link into __osPiTable
  (D_8005343C), __osRestoreInt.
- 0x8003D640 `__osFlashGetAddr` — page → device address, shift 7 vs 6 by
  flash version (D_80090B90).

Renaming the nine publics routes all flash traffic through librecomp's
flash.cpp (its osFlashReadId_recomp reports vendor 0x00C2001E — exactly the MX
ID osFlashInit checks, so the version path behaves as on hardware) and removes
every raw PI register access. The **0x78xxxxxx execute-command writes are
WM2000's predicted flash evidence pattern**, confirming `SaveType::Flashram`
in src/main/main.cpp (1Mbit / 128 KB, matching librecomp's flash_size).
