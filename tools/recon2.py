#!/usr/bin/env python3
"""Recon pass 2 for WM2000: descriptor-table bounds, ucode block layout, fixed-segment
code/data boundary, and libultra fingerprint transfer from Revenge.

Fingerprint method: mask address-bearing fields (16-bit immediates of I-type ALU ops
and loads/stores, 26-bit j/jal targets), keep everything else (SPECIAL ops, branch
offsets, cop0/cop1 words). Match each identified Revenge function's full masked body
against the WM2000 fixed-segment code. Unlike WT->Revenge (different libultra
generations, 3/46 transferred), Revenge->WM2000 is one year apart and the audio
ucode already matched byte-exact.
"""
import re, struct

WM2K = r"C:\Users\selki\depot\Wm2kRecomp\wm2k.z64"
REV  = r"C:\Users\selki\depot\WcwRevengeRecomp\revenge.z64"
REV_DUMP = r"C:\Users\selki\depot\WcwRevengeRecomp\syms\dump.toml"

wm = open(WM2K, "rb").read()
rev = open(REV, "rb").read()

# --- descriptor table bounds: entries are 9 words at 0x48A80 + n*0x24; print raw
#     words just before/after the 4 known entries.
print("=== words around descriptor table 0x48A60..0x48B40 ===")
for off in range(0x48A60, 0x48B40, 0x24):
    w = struct.unpack_from(">9I", wm, off)
    print(f"  0x{off:06X}: " + " ".join(f"{x:08X}" for x in w))

# --- rspboot + gfx ucode: Revenge rspboot bytes 0x2C840..0x2C910; gfx text follows
#     audio text (Revenge 0x2D570).
print("=== ucode block ===")
boot = rev[0x2C840:0x2C910]
i = wm.find(boot)
print(f"  Revenge rspboot bytes at: {hex(i) if i >= 0 else 'NOT FOUND'}")
gfx = rev[0x2D570:0x2D570+0x100]
i = wm.find(gfx)
print(f"  Revenge gfx-ucode first 0x100 at: {hex(i) if i >= 0 else 'NOT FOUND'}")
adata = rev[0x3B1C0:0x3B1C0+0x20]
i = wm.find(adata)
print(f"  Revenge audio ucode-data dispatch table at: {hex(i) if i >= 0 else 'NOT FOUND'}")

# --- fixed-segment fine structure: valid-instruction ratio per 1KB window, 0x1000..0x4C160
def looks_insn(w):
    op = w >> 26
    if w == 0: return True  # nop / padding
    if op == 0: return (w & 0x3F) not in ()  # SPECIAL, accept
    return op in (1,2,3,4,5,6,7,8,9,0xA,0xB,0xC,0xD,0xE,0xF,0x10,0x11,0x14,0x15,0x16,0x17,
                  0x20,0x21,0x22,0x23,0x24,0x25,0x26,0x27,0x28,0x29,0x2A,0x2B,0x2E,0x2F,
                  0x31,0x35,0x39,0x3D)
print("=== low-validity 1KB windows in fixed segment (likely data pockets) ===")
run_start = None
for base in range(0x1000, 0x4C160, 0x400):
    n = ok = 0
    for o in range(base, min(base+0x400, 0x4C160), 4):
        w = struct.unpack_from(">I", wm, o)[0]
        n += 1
        if looks_insn(w): ok += 1
    bad = ok / n < 0.9
    if bad and run_start is None: run_start = base
    if not bad and run_start is not None:
        print(f"  0x{run_start:06X} - 0x{base:06X}")
        run_start = None
if run_start is not None:
    print(f"  0x{run_start:06X} - 0x4C160")

# --- fingerprint transfer ---
def mask_word(w):
    op = w >> 26
    if op in (2, 3):            # j/jal: keep opcode only
        return w & 0xFC000000
    if op in (8,9,0xA,0xB,0xC,0xD,0xE,0xF) or 0x20 <= op <= 0x3F:  # imm ALU + loads/stores
        return w & 0xFFFF0000
    return w

def masked(buf):
    return [mask_word(struct.unpack_from(">I", buf, i)[0]) for i in range(0, len(buf)//4*4, 4)]

wm_code = masked(wm[0x1000:0x4C160])

# parse Revenge dump.toml: sections + named (non-func_) functions
sec_rom = sec_vram = 0
targets = []
for line in open(REV_DUMP):
    m = re.match(r'rom = (0x[0-9A-Fa-f]+)', line)
    if m: sec_rom = int(m.group(1), 16)
    m = re.match(r'vram = (0x[0-9A-Fa-f]+)', line)
    if m: sec_vram = int(m.group(1), 16)
    m = re.search(r'\{ name = "([^"]+)", vram = (0x[0-9A-Fa-f]+), size = (0x[0-9A-Fa-f]+)', line)
    if m and not m.group(1).startswith("func_") and m.group(1) not in ("entrypoint",):
        name, vram, size = m.group(1), int(m.group(2), 16), int(m.group(3), 16)
        if sec_vram <= vram < sec_vram + 0x100000:
            targets.append((name, sec_rom + (vram - sec_vram), size))

print(f"=== fingerprint transfer: {len(targets)} named Revenge functions -> WM2000 ===")
hits = 0
for name, rom, size in sorted(targets, key=lambda t: t[0]):
    pat = masked(rev[rom:rom+size])
    if len(pat) < 4:
        print(f"  {name:28s} too small, skipped"); continue
    matches = []
    first = pat[0]
    for i in range(len(wm_code) - len(pat) + 1):
        if wm_code[i] != first: continue
        if wm_code[i:i+len(pat)] == pat:
            matches.append(0x1000 + i*4)
            if len(matches) > 3: break
    if len(matches) == 1:
        vram = 0x80000400 + (matches[0] - 0x1000)
        print(f"  {name:28s} -> rom 0x{matches[0]:06X} vram 0x{vram:08X}")
        hits += 1
    elif matches:
        print(f"  {name:28s} AMBIGUOUS x{len(matches)}: " + " ".join(hex(m) for m in matches))
    else:
        print(f"  {name:28s} no match")
print(f"=== {hits}/{len(targets)} unique transfers ===")
