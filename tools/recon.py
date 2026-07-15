#!/usr/bin/env python3
"""Initial ROM recon for WWF WrestleMania 2000 (U) (V1.2), using the methods proven
on World Tour and Revenge (see ..\\WcwRevengeRecomp\\tools\\recon.py and WT's
docs/devlog.md): jr-$ra density to find code regions, similarity vs the sister
ROMs, an overlay-descriptor scan (WT/Revenge use 9-word descriptors:
romStart, romEnd, entry, text/data/bss bounds), and an audio-ucode byte search
(does Revenge's newer AKI audio microcode transfer?).
"""
import struct

WM2K = r"C:\Users\selki\depot\Wm2kRecomp\wm2k.z64"
REV  = r"C:\Users\selki\depot\WcwRevengeRecomp\revenge.z64"
WT   = r"C:\Users\selki\depot\WcwNwoWorldTour\wcw.z64"

wm = open(WM2K, "rb").read()
rev = open(REV, "rb").read()
wt = open(WT, "rb").read()

def jr_density(rom, window=0x4000):
    out = []
    for base in range(0, len(rom), window):
        chunk = rom[base:base+window]
        n = chunk.count(b"\x03\xe0\x00\x08")
        out.append((base, n))
    return out

def regions(rom, thresh=8):
    dens = jr_density(rom)
    regs, start = [], None
    for base, n in dens:
        if n >= thresh and start is None:
            start = base
        elif n < thresh and start is not None:
            regs.append((start, base)); start = None
    if start is not None:
        regs.append((start, len(rom)))
    return regs

print("=== WM2000 code regions (jr $ra density >= 8/16KB) ===")
for s, e in regions(wm):
    print(f"  0x{s:06X} - 0x{e:06X}  ({(e-s)//1024} KB)")

print("=== boot-code similarity: identical 16-byte blocks at same offset, first 64KB from 0x1000 ===")
for name, other in (("Revenge", rev), ("WorldTour", wt)):
    same = total = 0
    for off in range(0x1000, 0x11000, 16):
        total += 1
        if wm[off:off+16] == other[off:off+16]:
            same += 1
    print(f"  vs {name}: {same}/{total} blocks identical ({100*same//total}%)")

# Overlay descriptor scan: WT/Revenge descriptors are 9 words:
#   romStart, romEnd, entry(vram), textStart, textEnd, dataStart, dataEnd, bssStart, bssEnd
# with romStart < romEnd <= len(rom), all vram words in KSEG0 and ascending-ish.
print("=== overlay descriptor candidates (9-word: rom pair + 7 KSEG0 words) ===")
def is_vram(w): return 0x80000000 <= w < 0x80800000
found = []
for off in range(0x1000, min(len(wm), 0x200000), 4):
    w = struct.unpack_from(">9I", wm, off)
    if (0x1000 <= w[0] < w[1] <= len(wm) and
            all(is_vram(x) for x in w[2:9]) and
            w[3] <= w[4] <= w[5] <= w[6] <= w[7] <= w[8] and
            w[3] <= w[2] < w[8] and
            (w[1] - w[0]) == (w[6] - w[3])):  # rom size == text+data size
        found.append((off, w))
for off, w in found:
    print(f"  desc @rom 0x{off:06X}: rom 0x{w[0]:06X}-0x{w[1]:06X} entry 0x{w[2]:08X} "
          f"text 0x{w[3]:08X}-0x{w[4]:08X} data 0x{w[5]:08X}-0x{w[6]:08X} bss 0x{w[7]:08X}-0x{w[8]:08X}")

# Audio ucode: does Revenge's audio microcode text (ROM 0x2C910, 0xC54 bytes) appear?
print("=== Revenge audio-ucode text search in WM2000 ===")
sig = rev[0x2C910:0x2C910+0xC54]
idx = wm.find(sig)
print(f"  full 0xC54-byte match at: {hex(idx) if idx >= 0 else 'NOT FOUND'}")
if idx < 0:
    head = rev[0x2C910:0x2C910+0x100]
    idx = wm.find(head)
    print(f"  first 0x100 bytes match at: {hex(idx) if idx >= 0 else 'NOT FOUND'}")

# rspboot / F3DEX2 markers: find gfx ucode text too (helps locate the ucode block).
print("=== F3DEX2 name string search ===")
for pat in (b"F3DEX2", b"S2DEX", b"aspMain", b"RSP"):
    pos, hits = 0, []
    while True:
        i = wm.find(pat, pos)
        if i < 0 or len(hits) > 6: break
        hits.append(i); pos = i + 1
    if hits:
        print(f"  {pat}: " + " ".join(hex(h) for h in hits[:6]))
