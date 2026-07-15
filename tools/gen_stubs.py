#!/usr/bin/env python3
"""Regenerate syms/stub_candidates.txt (functions containing cop0/cache opcodes —
unrecompilable, runtime-provided) and rebuild nomercy.toml's stubs block.
recomp-loop2.py appends further stubs discovered at recompile time."""
import re, struct, os
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
rev = open("nomercy.z64", "rb").read()
funcs = []
sec_rom = sec_vram = 0
for line in open("syms/dump.toml"):
    ms = re.match(r'rom = (0x[0-9A-Fa-f]+)', line)
    if ms: sec_rom = int(ms.group(1), 16)
    ms = re.match(r'vram = (0x[0-9A-Fa-f]+)', line)
    if ms: sec_vram = int(ms.group(1), 16)
    m = re.search(r'\{ name = "([^"]+)", vram = (0x[0-9A-Fa-f]+), size = (0x[0-9A-Fa-f]+)', line)
    if m: funcs.append((m.group(1), int(m.group(2), 16), int(m.group(3), 16), sec_rom, sec_vram))
special = []
for name, vram, size, sec_rom, sec_vram in funcs:
    # RENAMEd (identified libultra) functions are auto-ignored by N64Recomp and
    # runtime-provided — never stub them (Revenge lesson: naming replaces stubbing).
    if not name.startswith("func_"):
        continue
    rom = sec_rom + (vram - sec_vram)
    for off in range(rom, rom + size - 3, 4):
        w = struct.unpack_from(">I", rev, off)[0]
        if (w >> 26) in (0x10, 0x2F):
            special.append(name); break
with open("syms/stub_candidates.txt", "w", newline="\n") as f:
    for name in special:
        f.write(f'    "{name}",\n')
stubs = open("syms/stub_candidates.txt").read()
# manual stubs (recomp_loop.py's unhandled-instruction discoveries, etc.) survive
# regeneration via syms/manual_stubs.txt — one function name per line.
if os.path.exists("syms/manual_stubs.txt"):
    for l in open("syms/manual_stubs.txt"):
        l = l.strip()
        if l and f'"{l}"' not in stubs:
            stubs += f'    "{l}",\n'
toml = open("nomercy.toml").read()
toml = re.sub(r"stubs = \[\n.*?\]", "stubs = [\n" + stubs + "]", toml, flags=re.S)
open("nomercy.toml", "w", newline="\n").write(toml)
print(f"{len(special)} stub candidates; nomercy.toml updated")
