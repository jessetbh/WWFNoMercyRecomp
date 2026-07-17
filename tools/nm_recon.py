#!/usr/bin/env python3
"""Initial ROM recon for WWF No Mercy (U) (Rev 1) — pass 1. Adapted from
tools/recon.py (Wm2k's, kept verbatim as the method reference), with VPW64's
entry-stub decode added. WM2000 is the reference sibling: its ROM supplies the
similarity baseline, the audio-ucode signature (byte-identical to Revenge's),
and (in nm_recon2) the libultra fingerprint source.
"""
import struct

NM   = r"C:\Users\selki\depot\NoMercyRecomp\nomercy.z64"
WM2K = r"C:\Users\selki\depot\Wm2kRecomp\wm2k.z64"

nm = open(NM, "rb").read()
wm = open(WM2K, "rb").read()

# --- entry stub decode: rom 0x1000 = vram 0x80000400 (AKI standard). Expect the
#     family shape: clear bss [lui/addiu bounds, sw loop], set sp, j game_main.
print("=== entry stub (rom 0x1000, vram 0x80000400) ===")
REG = ("zero at v0 v1 a0 a1 a2 a3 t0 t1 t2 t3 t4 t5 t6 t7 "
       "s0 s1 s2 s3 s4 s5 s6 s7 t8 t9 k0 k1 gp sp fp ra").split()
def dis(w, pc):
    op = w >> 26
    rs, rt = (w >> 21) & 31, (w >> 16) & 31
    imm = w & 0xFFFF
    simm = imm - 0x10000 if imm & 0x8000 else imm
    if w == 0: return "nop"
    if op == 0x0F: return f"lui   ${REG[rt]}, 0x{imm:04X}"
    if op == 0x09: return f"addiu ${REG[rt]}, ${REG[rs]}, {'0x%X' % imm if simm >= 0 else '-0x%X' % -simm}"
    if op == 0x0D: return f"ori   ${REG[rt]}, ${REG[rs]}, 0x{imm:04X}"
    if op == 0x2B: return f"sw    ${REG[rt]}, {simm}(${REG[rs]})"
    if op == 0x2F: return f"cache 0x{rt:X}, {simm}(${REG[rs]})"
    if op == 0x05: return f"bne   ${REG[rs]}, ${REG[rt]}, 0x{pc + 4 + simm*4:08X}"
    if op == 0x04: return f"beq   ${REG[rs]}, ${REG[rt]}, 0x{pc + 4 + simm*4:08X}"
    if op == 0x02: return f"j     0x{(pc & 0xF0000000) | ((w & 0x3FFFFFF) << 2):08X}"
    if op == 0x03: return f"jal   0x{(pc & 0xF0000000) | ((w & 0x3FFFFFF) << 2):08X}"
    if op == 0 and (w & 0x3F) == 8: return f"jr    ${REG[rs]}"
    if op == 0x23: return f"lw    ${REG[rt]}, {simm}(${REG[rs]})"
    return f".word 0x{w:08X}"
for i in range(0x1000, 0x1050, 4):
    w = struct.unpack_from(">I", nm, i)[0]
    pc = 0x80000400 + (i - 0x1000)
    print(f"  {pc:08X}: {w:08X}  {dis(w, pc)}")

def jr_density(rom, window=0x4000):
    out = []
    for base in range(0, len(rom), window):
        n = rom[base:base+window].count(b"\x03\xe0\x00\x08")
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

print("=== No Mercy code regions (jr $ra density >= 8/16KB) ===")
for s, e in regions(nm):
    print(f"  0x{s:06X} - 0x{e:06X}  ({(e-s)//1024} KB)")

print("=== boot-code similarity vs WM2000: identical 16-byte blocks, first 64KB from 0x1000 ===")
same = total = 0
for off in range(0x1000, 0x11000, 16):
    total += 1
    if nm[off:off+16] == wm[off:off+16]:
        same += 1
print(f"  vs WM2000: {same}/{total} blocks identical ({100*same//total}%)")

print("=== overlay descriptor candidates (9-word: rom pair + 7 KSEG0 words) ===")
def is_vram(w): return 0x80000000 <= w < 0x80800000
for off in range(0x1000, min(len(nm), 0x400000), 4):
    w = struct.unpack_from(">9I", nm, off)
    if (0x1000 <= w[0] < w[1] <= len(nm) and
            all(is_vram(x) for x in w[2:9]) and
            w[3] <= w[4] <= w[5] <= w[6] <= w[7] <= w[8] and
            w[3] <= w[2] < w[8] and
            (w[1] - w[0]) == (w[6] - w[3])):
        print(f"  desc @rom 0x{off:06X}: rom 0x{w[0]:06X}-0x{w[1]:06X} entry 0x{w[2]:08X} "
              f"text 0x{w[3]:08X}-0x{w[4]:08X} data 0x{w[5]:08X}-0x{w[6]:08X} bss 0x{w[7]:08X}-0x{w[8]:08X}")

# --- ucode block: WM2000's rspboot text 0x39440..0x39510, audio text 0x39510+0xC54
#     (byte-identical to Revenge's), gfx text follows; audio dispatch table 0x4A8F0.
print("=== ucode byte-search (WM2000 signatures) ===")
for label, lo, ln in (("rspboot text (0x39440)", 0x39440, 0xD0),
                      ("audio ucode text 0xC54 (0x39510)", 0x39510, 0xC54),
                      ("audio ucode text first 0x100", 0x39510, 0x100),
                      ("gfx ucode first 0x100 (0x3A164+)", 0x3C628, 0x100),
                      ("audio dispatch table 0x20 (0x4A8F0)", 0x4A8F0, 0x20)):
    sig = wm[lo:lo+ln]
    hits, pos = [], 0
    while len(hits) < 4:
        i = nm.find(sig, pos)
        if i < 0: break
        hits.append(i); pos = i + 1
    print(f"  {label}: " + (" ".join(hex(h) for h in hits) if hits else "NOT FOUND"))

print("=== ucode name strings ===")
for pat in (b"F3DEX2", b"S2DEX", b"aspMain", b"RSP"):
    pos, hits = 0, []
    while True:
        i = nm.find(pat, pos)
        if i < 0 or len(hits) > 6: break
        hits.append(i); pos = i + 1
    if hits:
        print(f"  {pat}: " + " ".join(hex(h) for h in hits[:6]))
