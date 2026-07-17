#!/usr/bin/env python3
"""Generate N64Recomp symbol TOML (syms/dump.toml) from splat output — No Mercy.

Same approach as WM2000/Revenge/VPW64's generator (symbol-TOML mode; spimdisasm
emits `nonmatching <name>, <size>` + per-instruction `/* ROM VRAM WORD */`
comments). One [[section]] per asm file; overlays are ordinary sections
(librecomp tracks their loads by rom address).

RENAME transfers libultra knowledge. No Mercy (Nov 2000) is WM2000's DIRECT
SEQUEL — same AKI team, one year apart, likely the same (or a newer) libultra
generation, so fingerprint the fixed segment against WM2000 FIRST (adapt
WM2000's tools/recon2.py, which matched Revenge->WM2000 27/39; the recon*.py
copies in this repo still point at Wm2kRecomp paths — for this game that repo
IS the fingerprint source). Evidence method per function: WT's
disasm/libultra.md; record this game's evidence in ours. The map below is EMPTY
until recon fills it — see WM2000's tools/gen_symbols.py for the worked example
(including the boot-crash-loop identifications: osCreatePiManager/
osCartRomInit/osDriveRomInit/osAiSetNextBuffer and the osViGet*Framebuffer
adjacency pair).
Input invariant from ALL sisters: rename ONLY osContInit + __osSiRawStartDma +
__osSiDeviceBusy; do NOT rename osContStartReadData (kills the raw-SI path).
NEVER stub/mis-rename osGetCount (frozen game clock, WM2000 lesson).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASM = ROOT / "disasm" / "asm"
OUT = ROOT / "syms" / "dump.toml"

RENAME = {
    # host-collision rename, same as every sister (splat leaves the entry target
    # unnamed; the entry stub at rom 0x1000 jr's to it at 0x80000460 — SAME vram
    # as WM2000's; confirmed by entry-stub decode, nm_recon.py 2026-07-14):
    "main": "game_main",
    "func_80000460": "game_main",

    # --- WM2000 fingerprint transfer, 2026-07-14 (nm_recon2.py): masked
    #     full-body match of WM2000's named fixed-segment set, 36/42 unique —
    #     a family record, as the direct-sequel prediction said. Evidence per
    #     function: WT's disasm/libultra.md method; this game's notes in ours.
    #     Naming a function makes N64Recomp auto-ignore it (built-in set in
    #     N64Recomp symbol_lists) so the runtime provides it.
    "func_8002E8F0": "__osDisableInt",
    "func_8002E960": "__osRestoreInt",
    "func_8002E980": "osSetIntMask",
    "func_8002EA20": "osCreatePiManager",
    "func_8002EDA0": "osEPiStartDma",
    "func_8002EE40": "osCartRomInit",
    "func_80026BE0": "osDriveRomInit",
    "func_80030730": "osAiSetFrequency",
    "func_80030850": "osAiSetNextBuffer",
    "func_80034770": "osContInit",
    "func_80034AE0": "osVirtualToPhysical",
    "func_80036B60": "osCreateMesgQueue",
    "func_80036B90": "osJamMesg",
    "func_80036CD0": "osRecvMesg",
    "func_80036E00": "osSendMesg",
    "func_80036F30": "osSetEventMesg",
    "func_80036FE0": "osSpTaskLoad",
    "func_800371EC": "osSpTaskStartGo",
    "func_80037220": "osSpTaskYield",
    "func_80037240": "osSpTaskYielded",
    "func_80037290": "__osSiRawStartDma",
    "func_80037520": "osCreateThread",
    "func_800375F0": "osGetThreadPri",
    "func_80037610": "osSetThreadPri",
    "func_800376E0": "osStartThread",
    "func_80037890": "osGetTime",
    "func_80037EB0": "osCreateViManager",
    "func_800381F0": "osViSetEvent",
    "func_80038250": "osViSetMode",
    "func_800382A0": "osViSetSpecialFeatures",
    "func_80038410": "osViSetYScale",
    "func_80038460": "osViSwapBuffer",
    "func_800387C0": "osViBlack",
    "func_8003C478": "osInitialize",
    "func_8003E470": "osDestroyThread",

    # --- ambiguity resolutions, nm_recon3.py (sisters' evidence patterns):
    # AI getter pair: adjacent tiny leaves; Length reads AI_LEN 0xA4500004,
    # Status reads AI_STATUS 0xA450000C (third candidate read SP_STATUS):
    "func_80030710": "osAiGetLength",
    "func_80030720": "osAiGetStatus",
    # Vi getter pair: adjacent 0x40-apart, WT's source order (Current first) —
    # 0x80037E30 loads global +0x4BF0, 0x80037E70 loads +0x4BF4; both bracket the
    # load with jal __osDisableInt/__osRestoreInt at the transferred addresses
    # (cross-consistency check passed):
    "func_80037E30": "osViGetCurrentFramebuffer",
    "func_80037E70": "osViGetNextFramebuffer",
    # __osSiDeviceBusy: of 2 shape-matches, the lui 0xA480 / lw 0x18 / andi 3
    # one = SI_STATUS (other candidate read SP_STATUS 0xA4040010 andi 0x1C):
    "func_8003FBE0": "__osSiDeviceBusy",
    # osGetCount: too small to fingerprint; UNIQUE mfc0 $v0,$Count (0x40024800)
    # byte-signature hit in the whole fixed segment. NEVER stub (WM2000 lesson).
    "func_8003E270": "osGetCount",

    # --- FLASH save driver (libultra 2.0L osflash.c), identified 2026-07-14
    #     from the first boot crash: game boots -> osFlashInit -> osFlashReadId
    #     -> raw PI_STATUS (0xA4600010) poll in __osEPiRawWriteIo = host AV at
    #     rdram+0x24600010. Renaming the NINE public entry points routes flash
    #     through librecomp (flash.cpp implements all; N64Recomp symbol_lists
    #     knows the names) and removes every raw PI access — the internals
    #     (osEPiWriteIo/ReadIo 0x8003D530/590, __osEPiRawWriteIo/ReadIo
    #     0x8003D3C0/250, osEPiLinkHandle 0x8003D5F0, __osFlashGetAddr
    #     0x8003D640) are ONLY reachable from these nine, verified by jal scan.
    #     Evidence per function (flash command constants) in disasm/libultra.md.
    #     This also CONFIRMS SaveType::Flashram in src/main/main.cpp (the
    #     0x78xxxxxx execute-command writes, WM2000's predicted pattern).
    "func_8002FED0": "osFlashInit",          # base 0xA8000000, MX vendor checks
    "func_8002FFE0": "osFlashReadId",        # CMD 0xE1000000, DMA id read
    "func_800300E0": "osFlashAllErase",      # CMD 0x3C000000 + 0x78000000
    "func_80030220": "osFlashSectorErase",   # CMD 0x4B000000|page + 0x78000000
    "func_80030370": "osFlashWriteBuffer",   # CMD 0xB4000000, EPiDma WRITE 0x80
    "func_80030410": "osFlashWriteArray",    # CMD 0xA5000000|page, status 0x04/0x44
    "func_80030560": "osFlashReadArray",     # CMD 0xF0000000, chunked EPiDma READ
    "func_8003D660": "osFlashReadStatus",    # CMD 0xD2000000 x2 + ReadIo
    "func_8003D700": "osFlashClearStatus",   # two osEPiWriteIo, no status wait
}

# Extra function entry points injected into dump.toml that splat cannot express:
# j-referenced entries living INSIDE another sized function (IDO multi-entry
# shared-tail clusters; Revenge's EXTRA_FUNCS mechanism — PERVASIVE in WM2000 and
# expected here). N64Recomp recompiles each independently from ROM bytes, so the
# overlap just duplicates a little code. Populated automatically by
# tools/recomp_loop.py from N64Recomp "Unhandled branch" errors and by
# tools/fix_backbranches.py audits, one "section name vram size" line per entry
# in syms/extra_funcs.txt. AUDIT after any re-split: a stale entry with a wrapped
# offset silently shadows the real function in the overlay lookup table (WM2000
# session 5 part 5).
EXTRA_FUNCS = {}
_extra_file = Path(__file__).resolve().parent.parent / "syms" / "extra_funcs.txt"
if _extra_file.exists():
    for l in open(_extra_file):
        parts = l.split()
        if len(parts) == 4:
            EXTRA_FUNCS.setdefault(parts[0], []).append(
                (parts[1], int(parts[2], 16), int(parts[3], 16)))

# Functions suppressed as symbols (continuation fragments merged into an earlier
# function).
SKIP = set()
_skip_file = ROOT / "syms" / "skip_functions.txt"
if _skip_file.exists():
    SKIP = {l.strip() for l in open(_skip_file) if l.strip()}

FUNC_RE = re.compile(r"^nonmatching (\S+), (0x[0-9A-Fa-f]+)")
GLABEL_RE = re.compile(r"^glabel (\S+)")
INSN_RE = re.compile(r"^\s*/\* ([0-9A-Fa-f]+) ([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{8}) \*/")

# Overlays per the descriptor table at rom 0x539A0 (disasm/nomercy.yaml): five
# overlays across two vram slots (0x800D9960: a+e; 0x80106760: b/c/d).
# N64Recomp statics are `static_<section index>_<vram>` with the index = the
# section ORDER in dump.toml — keep fix_stumps' index-based resolution in mind
# (WM2000 session 7) when reading its "chained X -> Y" output.
SECTION_NAMES = {
    "1000.s": "entry",
    "57310.s": "ovl_a",
    "81210.s": "ovl_b",
    "AE390.s": "ovl_c",
    "FD250.s": "ovl_d",
    "144BE0.s": "ovl_e",
}

def parse_file(path):
    """Return (rom_start, vram_start, rom_end, [(name, vram, size)])."""
    funcs = []
    pending_size = None
    pending_name = None
    first = None
    last = None
    for line in open(path, encoding="utf-8"):
        m = FUNC_RE.match(line)
        if m:
            pending_name, pending_size = m.group(1), int(m.group(2), 16)
            continue
        m = GLABEL_RE.match(line)
        if m and pending_name == m.group(1):
            funcs.append([pending_name, None, pending_size])
            continue
        m = INSN_RE.match(line)
        if m:
            rom, vram = int(m.group(1), 16), int(m.group(2), 16)
            if first is None:
                first = (rom, vram)
            last = (rom, vram)
            if funcs and funcs[-1][1] is None:
                funcs[-1][1] = vram
    if first is None:
        return None
    return first[0], first[1], last[0] + 4, [(n, v, s) for n, v, s in funcs if v is not None]

def main():
    sections = []
    for path in sorted(ASM.glob("*.s")):
        parsed = parse_file(path)
        if not parsed:
            continue
        rom, vram, rom_end, funcs = parsed
        if path.name == "1000.s":
            name = "entry"
            funcs = [("entrypoint", vram, 0x38)] if not funcs else funcs
        elif path.name in SECTION_NAMES:
            name = SECTION_NAMES[path.name]
        else:
            name = f"main_{rom:X}"
        sections.append((name, rom, vram, rom_end - rom, funcs))

    OUT.parent.mkdir(exist_ok=True)
    unused = set(RENAME) - {"main"}
    with open(OUT, "w", newline="\n") as f:
        f.write("# Autogenerated from splat disassembly by tools/gen_symbols.py\n")
        total = 0
        seen = {}
        for name, rom, vram, size, funcs in sections:
            f.write(f"\n[[section]]\nname = \"{name}\"\n")
            f.write(f"rom = 0x{rom:08X}\nvram = 0x{vram:08X}\nsize = 0x{size:X}\n\n")
            f.write("functions = [\n")
            funcs = sorted(list(funcs) + EXTRA_FUNCS.get(name, []), key=lambda t: t[1])
            for fn, fv, fs in funcs:
                if fn in SKIP:
                    continue
                unused.discard(fn)
                fn = RENAME.get(fn, fn)
                # disambiguate names colliding across same-vram overlays (WT scheme)
                if fn in seen and seen[fn] != (name, fv):
                    fn = f"{fn}_{name}"
                seen[fn] = (name, fv)
                f.write(f"    {{ name = \"{fn}\", vram = 0x{fv:08X}, size = 0x{fs:X} }},\n")
                total += 1
            f.write("]\n")
    print(f"wrote {OUT}: {len(sections)} sections, {total} functions")
    if unused:
        print("WARNING: RENAME keys not found in splat output (splat merged/missed them):")
        for k in sorted(unused):
            print(f"  {k} -> {RENAME[k]}")

if __name__ == "__main__":
    main()
