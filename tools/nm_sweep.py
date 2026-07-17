#!/usr/bin/env python3
"""One-shot co-residency remediation loop (bootstrap helper): run
audit_coresidency, convert its unique (section, target) findings into
extra_funcs mid-entries, regenerate, recompile, re-run the fixup pipeline,
and repeat until the audit is clean. Mirrors Wm2k's session-6 sweep."""
import re, subprocess, sys, os

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VIOL_RE = re.compile(r'calls 0x([0-9A-Fa-f]{8}): co-loadable (\w+) has no')

def load_funcs():
    out, sec = [], None
    for line in open("syms/dump.toml"):
        m = re.match(r'name = "([^"]+)"', line)
        if m: sec = m.group(1)
        m = re.search(r'\{ name = "([^"]+)", vram = (0x[0-9A-Fa-f]+), size = (0x[0-9A-Fa-f]+)', line)
        if m: out.append((sec, m.group(1), int(m.group(2), 16), int(m.group(3), 16)))
    return out

def run(cmd, ok_rc=(0,)):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in ok_rc:
        print(f"FAILED: {cmd}\n" + (r.stdout + r.stderr)[-2000:])
        sys.exit(1)
    return r.stdout + r.stderr

MISSING_RE = re.compile(r'MISSING \S+ \((\w+)\): branch to 0x([0-9A-Fa-f]{8})')

def add_entries(pairs):
    funcs = load_funcs()
    existing = {l.strip() for l in open("syms/extra_funcs.txt")}
    added = 0
    with open("syms/extra_funcs.txt", "a", newline="\n") as f:
        for sec, t in sorted(pairs):
            inside = [(v, z) for s, n, v, z in funcs if s == sec and v <= t < v + z]
            if inside:
                end = max(v + z for v, z in inside)
            else:
                nxts = [v for s, n, v, z in funcs if s == sec and v > t]
                if not nxts:
                    print(f"  SKIP {sec} 0x{t:08X}: no next function"); continue
                end = min(nxts)
            line = f"{sec} func_{t:08X} 0x{t:08X} 0x{end - t:X}"
            if line in existing:
                continue
            print("  +", line)
            f.write(line + "\n")
            added += 1
    return added

for rnd in range(1, 12):
    out = subprocess.run([sys.executable, "tools/audit_coresidency.py"],
                         capture_output=True, text=True).stdout
    pairs = {(m.group(2), int(m.group(1), 16)) for m in VIOL_RE.finditer(out)}
    if not pairs:
        print(f"round {rnd}: audit clean")
        sys.exit(0)
    print(f"round {rnd}: {len(pairs)} unique (section, target) mid-entries needed")
    if add_entries(pairs) == 0:
        print("no new entries could be added but violations remain — manual fix needed")
        print(out[-1500:]); sys.exit(1)
    run([sys.executable, "tools/gen_symbols.py"])
    run([sys.executable, "tools/gen_stubs.py"])
    run([sys.executable, "tools/recomp_loop.py"])
    # inner loop: fix_backbranches may demand entries of its own after a regen
    for _ in range(5):
        run([sys.executable, "tools/fix_stumps.py"])
        run([sys.executable, "tools/fix_switches.py"])
        bb = run([sys.executable, "tools/fix_backbranches.py"], ok_rc=(0, 1))
        miss = {(m.group(1), int(m.group(2), 16)) for m in MISSING_RE.finditer(bb)}
        if not miss:
            break
        print(f"  backbranches: {len(miss)} missing mid-entries")
        if add_entries(miss) == 0:
            print("backbranch entries not addable — manual fix needed"); sys.exit(1)
        run([sys.executable, "tools/gen_symbols.py"])
        run([sys.executable, "tools/gen_stubs.py"])
        run([sys.executable, "tools/recomp_loop.py"])
    run([sys.executable, "tools/readd_hand_edits.py"])
print("did not converge in 11 rounds")
sys.exit(1)
