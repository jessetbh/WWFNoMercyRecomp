#!/usr/bin/env python3
"""Recompile loop: run N64Recomp, and when it dies with
  "Unhandled branch in func_X at 0xY to 0xZ"
(a j/branch from one function into the middle of another — IDO shared-tail
cluster), inject 0xZ as an extra function entry (syms/extra_funcs.txt, consumed by
gen_symbols.py's EXTRA_FUNCS), regenerate syms/dump.toml + stubs, and retry.
Mirrors Revenge's recomp-loop scripts. Stops on success or an unrecognized error.
"""
import re, subprocess, sys, os

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRANCH_RE = re.compile(r"Unhandled branch in (\S+) at 0x[0-9A-Fa-f]+ to 0x([0-9A-Fa-f]+)")
INSN_RE = re.compile(r"Unhandled instruction|Unsupported instruction")
ERROR_FUNC_RE = re.compile(r"Error recompiling (\S+)")

def load_funcs():
    """[(section, name, vram, size)] from syms/dump.toml"""
    out, sec = [], None
    for line in open("syms/dump.toml"):
        m = re.match(r'name = "([^"]+)"', line)
        if m: sec = m.group(1)
        m = re.search(r'\{ name = "([^"]+)", vram = (0x[0-9A-Fa-f]+), size = (0x[0-9A-Fa-f]+)', line)
        if m: out.append((sec, m.group(1), int(m.group(2), 16), int(m.group(3), 16)))
    return out

for it in range(1, 200):
    r = subprocess.run([".\\N64Recomp.exe", "nomercy.toml"], capture_output=True, text=True)
    out = r.stdout + r.stderr
    if r.returncode == 0:
        print(f"iteration {it}: SUCCESS")
        sys.exit(0)
    m = BRANCH_RE.search(out)
    if m:
        target = int(m.group(2), 16)
        funcs = load_funcs()
        host = None
        for sec, name, vram, size in funcs:
            if vram < target < vram + size and sec != "entry":
                host = (sec, name, vram, size); break
        if host is None:
            # target sits in a GAP between labeled functions (unreferenced fragment
            # splat didn't label): size runs to the next function start in the
            # same section.
            cands = sorted((vram, sec, name) for sec, name, vram, size in funcs
                           if vram > target and sec != "entry")
            below = sorted((vram + size, vram, sec) for sec, name, vram, size in funcs
                           if vram + size <= target and sec != "entry")
            if not cands or not below:
                print(f"iteration {it}: target 0x{target:08X} outside all sections — manual fix needed")
                print(out[-2000:]); sys.exit(1)
            nxt, sec = cands[0][0], cands[0][1]
            line = f"{sec} func_{target:08X} 0x{target:08X} 0x{nxt - target:X}\n"
            with open("syms/extra_funcs.txt", "a", newline="\n") as f:
                f.write(line)
            print(f"iteration {it}: injected gap fragment {line.strip()}")
            subprocess.run([sys.executable, "tools/gen_symbols.py"], check=True)
            subprocess.run([sys.executable, "tools/gen_stubs.py"], check=True)
            continue
        sec, name, vram, size = host
        new_size = vram + size - target
        line = f"{sec} func_{target:08X} 0x{target:08X} 0x{new_size:X}\n"
        with open("syms/extra_funcs.txt", "a", newline="\n") as f:
            f.write(line)
        print(f"iteration {it}: injected {line.strip()} (inside {name})")
        subprocess.run([sys.executable, "tools/gen_symbols.py"], check=True)
        subprocess.run([sys.executable, "tools/gen_stubs.py"], check=True)
        continue
    m = ERROR_FUNC_RE.search(out)
    if m and INSN_RE.search(out):
        # unrecompilable instruction (cop2/etc.) — stub it, Revenge's treatment
        fn = m.group(1)
        with open("syms/manual_stubs.txt", "a", newline="\n") as f:
            f.write(fn + "\n")
        subprocess.run([sys.executable, "tools/gen_stubs.py"], check=True)
        print(f"iteration {it}: stubbed {fn} (unhandled instruction)")
        continue
    print(f"iteration {it}: unrecognized failure")
    print(out[-3000:])
    sys.exit(1)
