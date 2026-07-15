#!/usr/bin/env python3
"""Cross-overlay static-bind audit (the session-6 sweep, now a committed tool).

RULE (bringup-plan session 6): for every direct-bound cross-section call in the
recompiled output, every CO-LOADABLE overlay covering the target address must
have a function start at that exact address -- otherwise N64Recomp silently
binds the call to the wrong overlay's copy of that vram (boot81: ovl_a's music
service ran OVL_B code on ovl_c data).

Run after any re-split or symbol change, BEFORE regenerating; a finding means
"add a mid-entry to syms/extra_funcs.txt for the listed overlay(s)".

Co-residency model (descriptor table rom 0x48A80, session 6):
  - main segments co-load with everything
  - ovl_a <-> ovl_b and ovl_a <-> ovl_c are legal pairs
  - ovl_d spans both slots: never co-resident with a/b/c
  - ovl_b never with ovl_c (same slot)
  - main_D0C2C is ovl_c's out-of-line island (co-resident iff ovl_c)
  - main_70EF8 is ovl_a's hidden pocket (co-resident iff ovl_a)
"""
import re, os, sys, glob
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
dump = open(os.path.join(ROOT, 'syms', 'dump.toml'), encoding='utf-8').read()

sections = []  # (name, vram, size)
for m in re.finditer(r'\[\[section\]\]\nname = "(\w+)"\nrom = 0x[0-9A-Fa-f]+\n'
                     r'vram = (0x[0-9A-Fa-f]+)\nsize = (0x[0-9A-Fa-f]+)', dump):
    sections.append((m.group(1), int(m.group(2), 16), int(m.group(3), 16)))

# effective overlay identity for co-residency purposes
ALIAS = {'main_D0C2C': 'ovl_c', 'main_70EF8': 'ovl_a'}
OVERLAYS = {'ovl_a', 'ovl_b', 'ovl_c', 'ovl_d'}

def ident(sec):
    return ALIAS.get(sec, sec)

def coloadable(a, b):
    """Can overlays (identities) a and b be resident simultaneously?"""
    a, b = ident(a), ident(b)
    if a == b:
        return True
    if a not in OVERLAYS or b not in OVERLAYS:
        return True  # main segments co-load with everything
    pair = frozenset((a, b))
    return pair in (frozenset(('ovl_a', 'ovl_b')), frozenset(('ovl_a', 'ovl_c')))

# function name -> section; (section, vram) -> True
sec_positions = [(m.start(), m.group(1)) for m in re.finditer(r'^name = "(\w+)"', dump, re.M)]
name2sec, starts = {}, defaultdict(set)
for m in re.finditer(r'\{ name = "(\w+)", vram = (0x[0-9A-Fa-f]+)', dump):
    sec = None
    for pos, s in sec_positions:
        if pos < m.start(): sec = s
        else: break
    name2sec[m.group(1)] = sec
    starts[int(m.group(2), 16)].add(sec)

def covering(v):
    return [n for n, vr, sz in sections if vr <= v < vr + sz]

call_re = re.compile(r'^\s{4,8}(func_([0-9A-Fa-f]{8})\w*)\(rdram, ctx\);', re.M)
func_re = re.compile(r'RECOMP_FUNC void (\w+)\(uint8_t\* rdram, recomp_context\* ctx\) \{(.*?)^;\}',
                     re.S | re.M)

violations = 0
for path in sorted(glob.glob(os.path.join(ROOT, 'RecompiledFuncs', 'funcs_*.c'))):
    src = open(path, 'rb').read().decode('utf-8', errors='replace')
    for fm in func_re.finditer(src):
        caller, body = fm.group(1), fm.group(2)
        csec = name2sec.get(caller)
        if csec is None:
            sm = re.match(r'static_(\d+)_[0-9A-Fa-f]+$', caller)
            if sm and int(sm.group(1)) < len(sec_positions):
                csec = sec_positions[int(sm.group(1))][1]
            else:
                continue
        for cm in call_re.finditer(body):
            target = int(cm.group(2), 16)
            for osec in covering(target):
                if ident(osec) == ident(csec):
                    continue
                if ident(osec) not in OVERLAYS:
                    # main segments are always resident; overlapping main
                    # sections (main_3A1D8 tail == main_3C0D0) map identical
                    # rom bytes, so either binding behaves the same
                    continue
                if not coloadable(csec, osec):
                    continue
                if osec not in starts.get(target, set()) and ident(osec) not in {
                        ident(s) for s in starts.get(target, set())}:
                    print('VIOLATION %s (%s) calls 0x%08X: co-loadable %s has no '
                          'function start there (add a mid-entry to extra_funcs.txt)'
                          % (caller, csec, target, osec))
                    violations += 1
print('%d violations' % violations)
sys.exit(1 if violations else 0)
