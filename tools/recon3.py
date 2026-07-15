#!/usr/bin/env python3
"""Recon pass 3: precise fixed-segment layout for the splat yaml.

1. Collect all jal targets from fixed text + the four overlay texts (bounds from the
   rom-0x48A80 descriptors). Targets landing in the fixed segment prove CPU code
   there; histogram them per 1KB to find real code extents vs ucode/data.
2. Resolve the ambiguous fingerprints by raw MMIO immediates:
   AI_LEN 0xA4500004 / AI_STATUS 0xA450000C / SI_STATUS 0xA4800018,
   __osViCurr vs __osViNext by which global each loads.
3. Refine the 0x37800/0x37C00 pocket edges (last jr $ra + 8 / first addiu $sp).
"""
import struct

wm = open(r"C:\Users\selki\depot\Wm2kRecomp\wm2k.z64", "rb").read()

FIXED = (0x1000, 0x4C160, 0x80000400)
OVLS = [  # (romStart, textSizeBytes, vramText) from descriptors
    (0x04C160, 0x800FEF10-0x800E1B90, 0x800E1B90),
    (0x073390, 0x801226F0-0x8011C900, 0x8011C900),
    (0x0809D0, 0x80161460-0x8011C900, 0x8011C900),
    (0x0D2720, 0x8014C640-0x800E1B90, 0x800E1B90),
]

def word(off): return struct.unpack_from(">I", wm, off)[0]

targets = set()
def scan(rom_start, size):
    for off in range(rom_start, rom_start + size, 4):
        w = word(off)
        if (w >> 26) == 3:  # jal
            targets.add(((w & 0x03FFFFFF) << 2) | 0x80000000)

scan(0x1000, 0x39440 - 0x1000)   # fixed text through rspboot start
for rom, tsize, vram in OVLS:
    scan(rom, tsize)

fixed_targets = sorted(t for t in targets if 0x80000400 <= t < 0x80000400 + (0x4C160-0x1000))
print(f"jal targets into fixed segment: {len(fixed_targets)}")
print(f"  min 0x{fixed_targets[0]:08X}  max 0x{fixed_targets[-1]:08X}")
# any targets past the rspboot rom offset (vram 0x80000400 + 0x39440-0x1000 = 0x80038840)?
late = [t for t in fixed_targets if t >= 0x80038840]
print(f"  targets at/after vram 0x80038840 (rspboot rom 0x39440): {len(late)}")
for t in late[:20]:
    print(f"    0x{t:08X} (rom 0x{t - 0x80000400 + 0x1000:06X})")
# targets inside the coarse pocket 0x37800-0x37C00 (vram 0x80036C00-0x80037000)?
inpocket = [t for t in fixed_targets if 0x80036C00 <= t < 0x80037000]
print(f"  targets in coarse pocket rom 0x37800-0x37C00: {[hex(t) for t in inpocket]}")

print("\n=== ambiguity resolution ===")
def show(off, n=4):
    return " ".join(f"{word(off+i*4):08X}" for i in range(n))
for cand in (0x2C480, 0x2C490, 0x38310):
    print(f"  rom 0x{cand:06X}: {show(cand)}")
for cand in (0x33710, 0x33750):
    print(f"  rom 0x{cand:06X}: {show(cand)}")
for cand in (0x382F0, 0x38E60):
    print(f"  rom 0x{cand:06X}: {show(cand)}")

print("\n=== pocket edge refinement around 0x37800/0x37C00 ===")
# last jr $ra before 0x37C00 scanning from 0x37400:
last_jr = None
for off in range(0x37000, 0x37C00, 4):
    if word(off) == 0x03E00008: last_jr = off
print(f"  last jr $ra in 0x37000-0x37C00: {hex(last_jr) if last_jr else None} -> code end {hex(last_jr+8) if last_jr else '?'}")
# first prologue (addiu $sp,$sp,-N or common starts) at/after 0x37800:
for off in range(0x37800, 0x38400, 4):
    w = word(off)
    if (w >> 16) == 0x27BD and (w & 0x8000):  # addiu $sp, $sp, -N
        print(f"  first addiu $sp,-N at/after 0x37800: {hex(off)}")
        break
# also show jr $ra positions right before rspboot 0x39440
last_jr2 = None
for off in range(0x38E00, 0x39440, 4):
    if word(off) == 0x03E00008: last_jr2 = off
print(f"  last jr $ra before rspboot: {hex(last_jr2) if last_jr2 else None} (+8 = code end)")
