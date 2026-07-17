#!/usr/bin/env python3
"""Code-coverage audit: how much of each text section is claimed by known
functions, and does every jal target in the ROM resolve to a known entry?

Two checks (the second is the strong one — it's recon3's jal-histogram idea
turned into a completeness proof):

1. GAPS: per text section in syms/dump.toml, subtract the union of function
   spans from the section span. Report every gap >= 8 bytes and classify its
   bytes (zeros / nops / plausible MIPS code by opcode-validity ratio).
   Gaps full of zeros or data are fine; a gap that decodes cleanly and
   contains jr-$ra is a cold-path pocket candidate (Wm2k session-5 class).

2. JAL TARGETS: decode every jal in every text section (fixed + all five
   overlays, correct per-section vram) and check the target is a known
   function ENTRY (dump.toml functions + syms/extra_funcs.txt). Targets that
   land mid-function are IDO shared-tail entries (fine if fix_backbranches /
   the sweep added them; listed for review); targets landing in a GAP or
   outside all sections are MISSED CODE.

Overlap note: extra_funcs mid-entries and fix_stumps chains make function
spans overlap on purpose; the union handles that.
"""
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROM = (ROOT / "nomercy.z64").read_bytes()

# ---- parse dump.toml ----
sections = []  # dicts: name, rom, vram, size, funcs=[(name, vram, size)]
cur = None
sec_re = re.compile(r'^(name|rom|vram|size) = (.+)$')
fn_re = re.compile(r'\{ name = "([^"]+)", vram = (0x[0-9A-Fa-f]+), size = (0x[0-9A-Fa-f]+) \}')
for line in (ROOT / "syms" / "dump.toml").read_text().splitlines():
    line = line.strip()
    if line == "[[section]]":
        cur = {"funcs": []}
        sections.append(cur)
        continue
    if cur is None:
        continue
    m = sec_re.match(line)
    if m and "name" not in cur or (m and m.group(1) != "name" and m.group(1) not in cur):
        k, v = m.group(1), m.group(2)
        cur[k] = v.strip('"') if k == "name" else int(v, 0)
        continue
    m = fn_re.search(line)
    if m:
        cur["funcs"].append((m.group(1), int(m.group(2), 0), int(m.group(3), 0)))

# extra_funcs entries are already merged into dump.toml by gen_symbols, but
# read the file too so entry-point checking survives a stale regen.
extra_entries = set()
ef = ROOT / "syms" / "extra_funcs.txt"
if ef.exists():
    for l in ef.read_text().splitlines():
        p = l.split()
        if len(p) == 4:
            extra_entries.add(int(p[2], 16))

total_text = 0
total_claimed = 0
all_entries = set(extra_entries)
spans_by_sec = {}
print("== per-section coverage ==")
for s in sections:
    if not s["funcs"]:
        continue
    size = s["size"]
    spans = sorted((v - s["vram"], v - s["vram"] + sz) for _, v, sz in s["funcs"])
    for _, v, _ in s["funcs"]:
        all_entries.add(v)
    # union of spans
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    claimed = sum(b - a for a, b in merged)
    total_text += size
    total_claimed += min(claimed, size)
    spans_by_sec[s["name"]] = (s, merged)
    print(f"{s['name']:>10}: rom 0x{s['rom']:06X} vram 0x{s['vram']:08X} size 0x{size:06X} "
          f"claimed 0x{claimed:06X} ({100.0*claimed/size:5.1f}%) funcs {len(s['funcs'])}")

print(f"\nTOTAL text: 0x{total_text:X} bytes, claimed 0x{total_claimed:X} "
      f"({100.0*total_claimed/total_text:.2f}%)")

# ---- gap analysis ----
print("\n== gaps >= 8 bytes (classified) ==")
gap_ranges = []  # (vram_lo, vram_hi) for jal-target check
ngaps = 0
for name, (s, merged) in spans_by_sec.items():
    pos = 0
    edges = merged + [[s["size"], s["size"]]]
    for a, b in edges:
        if a - pos >= 8:
            lo, hi = pos, a
            rom_lo = s["rom"] + lo
            data = ROM[rom_lo:s["rom"] + hi]
            words = list(struct.unpack(f">{len(data)//4}I", data[:len(data)//4*4]))
            zeros = sum(1 for w in words if w == 0)
            jrra = sum(1 for w in words if w == 0x03E00008)
            # crude validity: top-6-bit opcode in the common MIPS set
            okops = {0,1,2,3,4,5,6,7,9,10,11,12,13,14,15,16,17,20,21,32,33,34,35,36,37,38,39,40,41,42,43,46,47,49,53,57,61,63}
            valid = sum(1 for w in words if (w >> 26) in okops)
            cls = "ZEROS" if zeros == len(words) else (
                  "code-like!" if words and valid / len(words) > 0.9 and jrra else "data/junk")
            gap_ranges.append((s["vram"] + lo, s["vram"] + hi, name))
            ngaps += 1
            print(f"  {name}: vram 0x{s['vram']+lo:08X}-0x{s['vram']+hi:08X} rom 0x{rom_lo:06X} "
                  f"len 0x{hi-lo:X}  [{cls}] zeros={zeros}/{len(words)} jr$ra={jrra}")
        pos = max(pos, b)
if ngaps == 0:
    print("  (none)")

# ---- jal target audit ----
print("\n== jal-target audit ==")
missed_gap, missed_out, mid = {}, {}, {}
for name, (s, merged) in spans_by_sec.items():
    data = ROM[s["rom"]:s["rom"] + s["size"]]
    vram0 = s["vram"]
    # which vram windows are legal targets? fixed seg always; overlay targets
    # resolved per-section (a jal inside ovl_b to slot vram could be b or its
    # co-residents — count target as known if ANY section's entry matches).
    for i in range(0, len(data) - 3, 4):
        w = struct.unpack(">I", data[i:i+4])[0]
        if (w >> 26) != 3:  # jal
            continue
        tgt = ((vram0 + i) & 0xF0000000) | ((w & 0x03FFFFFF) << 2)
        if tgt in all_entries:
            continue
        # inside any known function span (any section)?
        located = None
        for n2, (s2, m2) in spans_by_sec.items():
            off = tgt - s2["vram"]
            if 0 <= off < s2["size"]:
                for a, b in m2:
                    if a <= off < b:
                        located = ("mid", n2)
                        break
                if located is None:
                    located = ("gap", n2)
                break
        src = f"{name}+0x{i:X}"
        if located is None:
            missed_out.setdefault(tgt, []).append(src)
        elif located[0] == "gap":
            missed_gap.setdefault((tgt, located[1]), []).append(src)
        else:
            mid.setdefault((tgt, located[1]), []).append(src)

def show(d, label, limit=40):
    print(f"{label}: {len(d)}")
    for k, srcs in sorted(d.items())[:limit]:
        tgt = k[0] if isinstance(k, tuple) else k
        sec = k[1] if isinstance(k, tuple) else "?"
        print(f"  0x{tgt:08X} in {sec}  ({len(srcs)} call sites, e.g. {srcs[0]})")

show(missed_gap, "targets in UNCLAIMED GAPS (missed code!)")
show(missed_out, "targets OUTSIDE all text sections (bogus/data-decode or missing section)")
show(mid, "targets MID-FUNCTION without an entry (shared-tail candidates; verify handled)", 20)
print("\nverdict: ", "CLEAN" if not missed_gap and not missed_out and not mid else "REVIEW ABOVE")
