#!/usr/bin/env python3
"""Audit + fix N64Recomp's silent mis-decode of out-of-span branch targets.

THE BUG CLASS (session 8 part 4, the rope-corruption root cause): an overlapping
mid-entry function (extra_funcs) whose body contains MIPS BRANCHES (b/bne/beq/
blez/...) targeting addresses OUTSIDE its own decoded span — typically backward
into the parent function's loop (IDO cold-path re-entry chains). N64Recomp
emits a bogus LOCAL label for the unreachable target, silently rerouting
control flow. func_8001E344's loop back-edges collapsed into an unconditional
tail call, killing the rope-template init after station 0 -> in-match rope
strips kept raw DMA'd bytes as X coordinates ("ropes bunched on one side,
stretched to the ramp").

Fix: rewrite `goto L_<target>` for out-of-span targets into a tail call to the
function at that address (`func_<target>(rdram, ctx); return;`) — semantically
identical to how N64Recomp emits out-of-function `j` targets. Requires a
function to START at the target: missing ones are reported loudly (add to
syms/extra_funcs.txt + regen; their truncated decodes are then completed by
tools/fix_stumps.py chaining and, recursively, by this script).

Run AFTER fix_stumps.py + fix_switches.py, every regen. Idempotent.

Portable to the sister recomps (same generated-code shape):
  python tools/fix_backbranches.py --audit --root ..\\WcwRevengeRecomp
  python tools/fix_backbranches.py --audit --root ..\\WcwNwoWorldTour --dump ..\\WcwNwoWorldTour\\WCWSyms\\dump.toml
--audit reports without modifying any file (use for repos you don't own the
build loop of); default (no flags) = fix in place for THIS repo.
"""
import re, os, glob, sys, argparse

ap = argparse.ArgumentParser()
ap.add_argument('--root', default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ap.add_argument('--dump', default=None, help='dump.toml path (default <root>/syms/dump.toml)')
ap.add_argument('--audit', action='store_true', help='report only, do not modify files')
args = ap.parse_args()
ROOT = args.root
dump = open(args.dump or os.path.join(ROOT, 'syms', 'dump.toml'), encoding='utf-8').read()

sec_positions = [(m.start(), m.group(1)) for m in re.finditer(r'^name = "(\w+)"', dump, re.M)]
name2sec, funcs_by_sec = {}, {}
for m in re.finditer(r'\{ name = "(\w+)", vram = (0x[0-9A-Fa-f]+)', dump):
    sec = None
    for pos, s in sec_positions:
        if pos < m.start(): sec = s
        else: break
    name2sec[m.group(1)] = sec
    funcs_by_sec.setdefault(sec, set()).add(int(m.group(2), 16))

def func_sec(name):
    s = name2sec.get(name)
    if s: return s
    m = re.match(r'static_(\d+)_', name)
    if m and int(m.group(1)) < len(sec_positions):
        return sec_positions[int(m.group(1))][1]
    m = re.match(r'func_[0-9A-Fa-f]{8}_(\w+)$', name)
    if m: return m.group(1)
    return None

func_re = re.compile(r'RECOMP_FUNC void (\w+)\(uint8_t\* rdram, recomp_context\* ctx\) \{(.*?)^;\}',
                     re.S | re.M)
# branch decode comments: "// 0xADDR: bne   $v0, $zero, L_TARGET" (branches only,
# j/jal are already emitted as calls by N64Recomp)
br_re = re.compile(r'// 0x([0-9A-F]{8}): b\w*\s+.*?L_([0-9A-F]{8})')
addr_re = re.compile(r'// (0x[0-9A-F]{8}):')

fixed = missing = 0
for path in sorted(glob.glob(os.path.join(ROOT, 'RecompiledFuncs', 'funcs_*.c'))):
    src = open(path, 'rb').read().decode('utf-8', errors='replace')
    out = src
    for fm in func_re.finditer(src):
        name, body = fm.group(1), fm.group(2)
        addrs = [int(a, 16) for a in addr_re.findall(body)]
        if not addrs:
            continue
        lo, hi = min(addrs), max(addrs) + 8
        bad_targets = {}
        for bm in br_re.finditer(body):
            tgt = int(bm.group(2), 16)
            if not (lo <= tgt < hi):
                bad_targets[tgt] = bm.group(0)
        if not bad_targets:
            continue
        sec = func_sec(name)
        newbody = body
        for tgt, where in sorted(bad_targets.items()):
            have = sec in funcs_by_sec and tgt in funcs_by_sec[sec]
            if not have:
                print('MISSING %s (%s): branch to 0x%08X outside [0x%08X,0x%08X) has no '
                      'function at the target. Add "%s func_%08X 0x%08X <size>" to '
                      'syms/extra_funcs.txt and regenerate.'
                      % (name, sec, tgt, lo, hi, sec, tgt, tgt))
                missing += 1
                continue
            lbl = 'L_%08X' % tgt
            call = 'func_%08X' % tgt
            # rewrite every goto to the bogus label into a tail call
            n = newbody.count('goto %s;' % lbl)
            newbody = newbody.replace(
                'goto %s;' % lbl,
                '{ /* [nomercy fix_backbranches] out-of-span branch target */ %s(rdram, ctx); return; }'
                % call)
            # neutralize the bogus label (keep a breadcrumb)
            newbody = re.sub(r'^%s:\r?$' % lbl,
                             '/* [nomercy fix_backbranches] bogus label %s removed */' % lbl,
                             newbody, flags=re.M)
            print('fixed %s (%s): %d goto(s) -> tail call %s' % (name, sec, n, call))
            fixed += n
        if newbody != body:
            out = out.replace(fm.group(0), fm.group(0).replace(body, newbody, 1), 1)
    if out != src and not args.audit:
        open(path, 'wb').write(out.encode('utf-8'))
print('%d gotos fixed, %d missing mid-entries' % (fixed, missing))
sys.exit(1 if missing else 0)
