#!/usr/bin/env python3
"""Generate nomercy.toml [[patches.instruction]] entries that neuter raw PI/SI/AI/SP
MMIO accesses inside a recompiled function (librecomp's rdram guard pages make any
0xA4xxxxxx dereference fault). Loads become `addu rt, $zero, $zero` (reads as 0 =
device never busy); stores become nops. The function's memory-side work (handle
struct init etc.) is preserved.

Usage: python tools/gen_mmio_patches.py func_80022540 [more funcs...]
Prints a TOML block to append to nomercy.toml (and appends it if --apply).
"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSN_RE = re.compile(r"^\s*/\* ([0-9A-Fa-f]+) ([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{8}) \*/")

def body(func):
    lines, on = [], False
    for f in sorted((ROOT / "disasm" / "asm").glob("*.s")):
        for line in open(f, encoding="utf-8"):
            if line.startswith(f"glabel {func}"):
                on = True
            elif on and (line.startswith("endlabel") or line.startswith("glabel ")):
                return lines
            elif on:
                m = INSN_RE.match(line)
                if m:
                    lines.append((int(m.group(2), 16), int(m.group(3), 16)))
    return lines

def gen(func):
    insns = body(func)
    if not insns:
        print(f"# {func}: NOT FOUND"); return []
    mmio_regs = {}   # reg -> True while holding a device-page address
    patches = []
    for vram, w in insns:
        op = w >> 26
        rs = (w >> 21) & 31; rt = (w >> 16) & 31
        if op == 0x0F:  # lui
            mmio_regs[rt] = 0xA400 <= (w & 0xFFFF) <= 0xA4FF
        elif op == 0x0D and rs in mmio_regs:  # ori rt, rs, imm
            mmio_regs[rt] = mmio_regs.get(rs, False)
        elif op in (0x23, 0x24, 0x25, 0x27):  # lw/lbu/lhu/lwu
            if mmio_regs.get(rs):
                patches.append((vram, 0x00000021 | (rt << 11), f"lw r{rt},({w & 0xFFFF:#x}) MMIO -> move r{rt}, zero"))
                mmio_regs[rt] = False
        elif op in (0x2B, 0x28, 0x29):  # sw/sb/sh
            if mmio_regs.get(rs):
                patches.append((vram, 0x00000000, "sw MMIO -> nop"))
        elif op in (0x08, 0x09) and rs in mmio_regs:  # addiu keeps page
            mmio_regs[rt] = mmio_regs.get(rs, False)
        elif op == 0 and (w & 0x3F) in (0x21, 0x20):  # addu/add moves
            mmio_regs[(w >> 11) & 31] = mmio_regs.get(rs, False) or mmio_regs.get(rt, False)
    out = []
    for vram, val, why in patches:
        out.append(f"# {func}: {why}\n[[patches.instruction]]\nfunc = \"{func}\"\nvram = 0x{vram:08X}\nvalue = 0x{val:08X}\n")
    return out

blocks = []
apply = "--apply" in sys.argv
for fn in [a for a in sys.argv[1:] if not a.startswith("-")]:
    blocks += gen(fn)
text = "\n" + "\n".join(blocks)
print(text)
if apply and blocks:
    with open(ROOT / "nomercy.toml", "a", newline="\n") as f:
        f.write(text)
    print(f"# appended {len(blocks)} patches to nomercy.toml")
