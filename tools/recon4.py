#!/usr/bin/env python3
"""Recon pass 4: hunt the six libultra functions whose full-body fingerprints didn't
transfer from Revenge (revised between libultra releases), using looser evidence:
- osGetCount: exact 3-word body (mfc0 v0,C0_COUNT; jr ra; nop).
- __osDisableInt at 0x8002A250? verify mfc0/mtc0 SR shape.
- prefix fingerprints (first N masked words) for osInitialize, osCreatePiManager,
  osCartRomInit, osSetEventMesg, osAiSetNextBuffer, __osDisableInt.
"""
import re, struct

wm = open(r"C:\Users\selki\depot\Wm2kRecomp\wm2k.z64", "rb").read()
rev = open(r"C:\Users\selki\depot\WcwRevengeRecomp\revenge.z64", "rb").read()
REV_DUMP = r"C:\Users\selki\depot\WcwRevengeRecomp\syms\dump.toml"

def word(buf, off): return struct.unpack_from(">I", buf, off)[0]

print("=== osGetCount (40024800 03E00008 00000000) ===")
pat = bytes.fromhex("40024800") + bytes.fromhex("03E00008") + bytes(4)
pos = 0
while True:
    i = wm.find(pat, pos)
    if i < 0: break
    print(f"  rom 0x{i:06X} vram 0x{0x80000400 + i - 0x1000:08X}")
    pos = i + 4

print("=== bytes at rom 0x2AE50 (suspected __osDisableInt) ===")
for off in range(0x2AE50, 0x2AEC8, 4):
    print(f"  0x{off:06X}: {word(wm, off):08X}")

# --- prefix fingerprints ---
def mask_word(w):
    op = w >> 26
    if op in (2, 3): return w & 0xFC000000
    if op in (8,9,0xA,0xB,0xC,0xD,0xE,0xF) or 0x20 <= op <= 0x3F: return w & 0xFFFF0000
    return w

def masked(buf, start, n):
    return [mask_word(word(buf, start + i*4)) for i in range(n)]

sec_rom = sec_vram = 0
targets = {}
for line in open(REV_DUMP):
    m = re.match(r'rom = (0x[0-9A-Fa-f]+)', line)
    if m: sec_rom = int(m.group(1), 16)
    m = re.match(r'vram = (0x[0-9A-Fa-f]+)', line)
    if m: sec_vram = int(m.group(1), 16)
    m = re.search(r'\{ name = "([^"]+)", vram = (0x[0-9A-Fa-f]+), size = (0x[0-9A-Fa-f]+)', line)
    if m and m.group(1) in ("osInitialize","osCreatePiManager","osCartRomInit",
                            "osSetEventMesg","osAiSetNextBuffer","__osDisableInt"):
        targets[m.group(1)] = (sec_rom + (int(m.group(2),16) - sec_vram), int(m.group(3),16))

wm_code = masked(wm, 0x1000, (0x39440 - 0x1000)//4)

for name, (rom, size) in sorted(targets.items()):
    total = size // 4
    for n in (24, 16, 10, 6):
        if n > total: continue
        pat = masked(rev, rom, n)
        matches = []
        first = pat[0]
        for i in range(len(wm_code) - n):
            if wm_code[i] != first: continue
            if wm_code[i:i+n] == pat:
                matches.append(0x1000 + i*4)
                if len(matches) > 4: break
        if len(matches) == 1:
            v = 0x80000400 + matches[0] - 0x1000
            print(f"  {name:20s} prefix {n:2d} words -> rom 0x{matches[0]:06X} vram 0x{v:08X}")
            break
        if len(matches) > 1 and n <= 6:
            print(f"  {name:20s} prefix {n:2d} words AMBIGUOUS: {[hex(m) for m in matches]}")
            break
    else:
        print(f"  {name:20s} no prefix match")

# entry stub decode: where does the boot stub jump (game_main)?
print("=== entry stub at 0x1000 ===")
for off in range(0x1000, 0x1050, 4):
    print(f"  0x{off:06X}: {word(wm, off):08X}")
