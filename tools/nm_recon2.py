#!/usr/bin/env python3
"""Recon pass 2 for No Mercy: exact code/data boundary, descriptor-table
neighborhood, audio dispatch table verification, gfx-ucode location, data
pockets in the fixed segment, and libultra fingerprint transfer from WM2000
(adapted from tools/recon2.py, Wm2k's Revenge->WM2000 pass)."""
import re, struct

NM   = r"C:\Users\selki\depot\NoMercyRecomp\nomercy.z64"
WM2K = r"C:\Users\selki\depot\Wm2kRecomp\wm2k.z64"
WM_DUMP = r"C:\Users\selki\depot\Wm2kRecomp\syms\dump.toml"

nm = open(NM, "rb").read()
wm = open(WM2K, "rb").read()

# --- exact end of fixed-segment CPU code: last jr $ra before rspboot (0x40F80)
last = nm.rfind(b"\x03\xe0\x00\x08", 0x1000, 0x40F80)
print(f"=== last jr $ra before rspboot: rom 0x{last:06X} (code ends 0x{last+8:06X}) ===")

# --- descriptor table neighborhood: any entries before 0x539A0 / after 0x53A30?
print("=== words around descriptor table 0x53950..0x53A80 ===")
for off in range(0x53954, 0x53A78, 0x24):
    w = struct.unpack_from(">9I", nm, off)
    print(f"  0x{off:06X}: " + " ".join(f"{x:08X}" for x in w))

# --- audio ucode data / dispatch table: WM2000's table at 0x4A8F0 matched at
#     0x55920. Dump both to confirm the 14 12-bit-PC entries are identical.
print("=== audio dispatch table (nm 0x55920 vs wm2k 0x4A8F0), 16 halfwords ===")
nm_t = struct.unpack_from(">16H", nm, 0x55920)
wm_t = struct.unpack_from(">16H", wm, 0x4A8F0)
print("  nm : " + " ".join(f"{x:04X}" for x in nm_t))
print("  wm : " + " ".join(f"{x:04X}" for x in wm_t))
print("  identical:", nm_t == wm_t)

# --- gfx ucode: Wm2k's gfx text starts right after audio text (0x39510+0xC54 =
#     0x3A164). No Mercy's audio text ends 0x41050+0xC54 = 0x41CA4.
print("=== gfx ucode ===")
gfx_sig = wm[0x3A164:0x3A164+0x100]
i = nm.find(gfx_sig)
print(f"  WM2000 gfx text first 0x100 at: {hex(i) if i >= 0 else 'NOT FOUND'}")
# compare the region right after No Mercy's audio text against wm2k's post-audio
same = 0
for off in range(0, 0x1000, 16):
    if nm[0x41CA4+off:0x41CA4+off+16] == wm[0x3A164+off:0x3A164+off+16]:
        same += 1
print(f"  post-audio-text region: {same}/256 16-byte blocks identical to WM2000's")
# how far does the byte-identical ucode block run from rspboot on?
n = 0
while nm[0x40F80+n] == wm[0x39440+n] and n < 0x20000:
    n += 1
print(f"  byte-identical run from rspboot: 0x{n:X} bytes (nm 0x40F80..0x{0x40F80+n:X})")

# --- fixed-segment data pockets: valid-instruction ratio per 1KB window
def looks_insn(w):
    op = w >> 26
    if w == 0: return True
    if op == 0: return True
    return op in (1,2,3,4,5,6,7,8,9,0xA,0xB,0xC,0xD,0xE,0xF,0x10,0x11,0x14,0x15,0x16,0x17,
                  0x20,0x21,0x22,0x23,0x24,0x25,0x26,0x27,0x28,0x29,0x2A,0x2B,0x2E,0x2F,
                  0x31,0x35,0x39,0x3D)
print("=== low-validity 1KB windows in fixed-segment code (likely data pockets) ===")
run_start = None
CODE_END = last + 8
for base in range(0x1000, CODE_END, 0x400):
    cnt = ok = 0
    for o in range(base, min(base+0x400, CODE_END), 4):
        w = struct.unpack_from(">I", nm, o)[0]
        cnt += 1
        if looks_insn(w): ok += 1
    bad = ok / cnt < 0.9
    if bad and run_start is None: run_start = base
    if not bad and run_start is not None:
        print(f"  0x{run_start:06X} - 0x{base:06X}")
        run_start = None
if run_start is not None:
    print(f"  0x{run_start:06X} - 0x{CODE_END:06X}")

# --- fingerprint transfer from WM2000 ---
def mask_word(w):
    op = w >> 26
    if op in (2, 3):
        return w & 0xFC000000
    if op in (8,9,0xA,0xB,0xC,0xD,0xE,0xF) or 0x20 <= op <= 0x3F:
        return w & 0xFFFF0000
    return w

def masked(buf):
    return [mask_word(struct.unpack_from(">I", buf, i)[0]) for i in range(0, len(buf)//4*4, 4)]

nm_code = masked(nm[0x1000:CODE_END])

# parse WM2000 dump.toml: named (non-func_) functions in its FIXED segment only
# (rom < 0x4C160 — overlay names like ovl_swap helpers transfer too if unique,
# but keep the scan to fixed-seg sources to avoid rom/vram mapping confusion).
sec_rom = sec_vram = 0
targets = []
for line in open(WM_DUMP):
    m = re.match(r'rom = (0x[0-9A-Fa-f]+)', line)
    if m: sec_rom = int(m.group(1), 16)
    m = re.match(r'vram = (0x[0-9A-Fa-f]+)', line)
    if m: sec_vram = int(m.group(1), 16)
    m = re.search(r'\{ name = "([^"]+)", vram = (0x[0-9A-Fa-f]+), size = (0x[0-9A-Fa-f]+)', line)
    if m and not m.group(1).startswith("func_") and m.group(1) not in ("entrypoint",):
        name, vram, size = m.group(1), int(m.group(2), 16), int(m.group(3), 16)
        rom = sec_rom + (vram - sec_vram)
        if rom < 0x4C160:
            targets.append((name, rom, size))

print(f"=== fingerprint transfer: {len(targets)} named WM2000 fixed-seg functions -> No Mercy ===")
hits = 0
for name, rom, size in sorted(targets, key=lambda t: t[0]):
    pat = masked(wm[rom:rom+size])
    if len(pat) < 4:
        print(f"  {name:28s} too small, skipped"); continue
    matches = []
    first = pat[0]
    for i in range(len(nm_code) - len(pat) + 1):
        if nm_code[i] != first: continue
        if nm_code[i:i+len(pat)] == pat:
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
