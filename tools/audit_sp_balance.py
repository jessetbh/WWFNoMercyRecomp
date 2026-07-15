#!/usr/bin/env python3
"""Static sp-balance audit over RecompiledFuncs (found boot91's 0x18 leak).

Flags functions that decrement ctx->r29 ($sp) with no matching increment
anywhere in the body and no tail-call at the end -- the signature of a
truncated epilogue (the sp restore lives past the cut and never runs). This
is how func_80009B7C (the match-exit -0x18 drift, session 8) was found.

KNOWN-BENIGN findings on a healthy tree (all never-return functions):
  thread entries func_80000EDC / func_80001024 / func_80001180 (event threads),
  func_800033D4 (render), func_80026F18 (audio mgr), func_800222D8 (game loop),
  and func_80006668 (exits via LOOKUP_FUNC shared tail, which this scan does
  not recognize as a tail call). Anything NEW in the output = investigate.
"""
import re, os, glob
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

func_re = re.compile(r'^(?:RECOMP_FUNC )?void (\w+)\(uint8_t\* rdram, recomp_context\* ctx\)')
sp_re = re.compile(r'ctx->r29 = ADD32\(ctx->r29, (-?0X[0-9A-F]+)\)')
call_re = re.compile(r'^\s*(\w+)\(rdram, ctx\);')

BENIGN = {'func_80000EDC', 'func_80001024', 'func_80001180', 'func_800033D4',
          'func_80026F18', 'func_800222D8', 'func_80006668'}

leaks = []
nfuncs = 0
for path in sorted(glob.glob(os.path.join(ROOT, 'RecompiledFuncs', 'funcs_*.c'))):
    text = open(path, encoding='utf-8', errors='replace').read()
    parts = re.split(r'^(?=(?:RECOMP_FUNC )?void \w+\(uint8_t\* rdram, recomp_context\* ctx\))',
                     text, flags=re.M)
    for part in parts:
        m = func_re.match(part)
        if not m:
            continue
        name = m.group(1)
        nfuncs += 1
        decs, incs = Counter(), Counter()
        for sm in sp_re.finditer(part):
            v = int(sm.group(1), 16)
            (decs if v < 0 else incs)[abs(v)] += 1
        if not decs or all(v in incs for v in decs):
            continue
        lines = [l for l in part.strip().splitlines() if l.strip() and not l.strip().startswith('//')]
        tail_call = None
        for l in reversed(lines[-6:]):
            cm = call_re.match(l)
            if cm:
                tail_call = cm.group(1)
                break
        if tail_call:
            continue  # chained stump / tail-call carries the epilogue
        leaks.append((os.path.basename(path), name, dict(decs), dict(incs)))

print('scanned %d functions' % nfuncs)
new = 0
for fn, name, d, i in leaks:
    known = ' (known-benign never-return)' if name in BENIGN else ''
    if not known:
        new += 1
    print('%-14s %-28s dec=%s inc=%s%s' % (fn, name, {hex(k): v for k, v in d.items()},
                                           {hex(k): v for k, v in i.items()}, known))
print('%d findings, %d new' % (len(leaks), new))
raise SystemExit(1 if new else 0)
