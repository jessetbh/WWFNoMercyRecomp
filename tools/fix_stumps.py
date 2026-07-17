#!/usr/bin/env python3
"""Post-process RecompiledFuncs: chain truncated overlapping functions.

N64Recomp truncates a function's decode at the next function symbol in the same
section, WITHOUT emitting the fall-through. Overlapping IDO mid-entries (our
extra_funcs additions for cross-overlay shared tails) therefore become "stumps":
a few instructions, then the C body just ends -- returning early without the
original tail (skipping logic AND the stack restore; this class caused the
boot84 saved-s0 corruption).

For every RECOMP_FUNC body whose END is REACHABLE (control flow can fall off
the closing brace), look up the same-section function starting at (last decoded
address + 4) and append a tail-call to it. Run after every tools/recomp_loop.py
invocation (see CLAUDE.md build loop).

Reachability, not "does the tail contain a return": the match-exit -0x18 stack
leak (boot91, session 7) was func_80009B7C, whose truncated fall-off sits right
AFTER a label (L_80009BEC) that follows another exit path's `return;` -- the old
last-8-lines heuristic saw that return and skipped the stump. Rule: walk the
body at brace depth 0; `return;`/`goto` kill the flow, any label revives it
(labels are goto targets); if the flow is alive at the closing brace, the
function falls off the end and must be chained.
"""
import re, glob, sys, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# name -> (section, vram); (section, vram) -> name
dump = open(os.path.join(ROOT, 'syms', 'dump.toml'), encoding='utf-8').read()
sec_positions = [(m.start(), m.group(1)) for m in re.finditer(r'^name = "(\w+)"', dump, re.M)]
name2sec = {}
by_sec_vram = {}
for m in re.finditer(r'\{ name = "(\w+)", vram = (0x[0-9A-Fa-f]+)', dump):
    sec = None
    for pos, s in sec_positions:
        if pos < m.start(): sec = s
        else: break
    name2sec[m.group(1)] = sec
    by_sec_vram[(sec, int(m.group(2), 16))] = m.group(1)

label_re = re.compile(r'(?:after_\d+|L_[0-9A-Fa-f]{8}):$')

def end_reachable(body):
    """True if control flow can fall off the end of a generated C body."""
    reachable = True
    depth = 0
    for raw in body.split('\n'):
        s = raw.strip()
        if not s or s.startswith('//') or s.startswith('/*') or s.startswith('*'):
            continue
        if depth == 0:
            if label_re.match(s):
                reachable = True   # a goto target: flow can resume here
            elif reachable and (s == 'return;' or s.startswith('goto ')):
                reachable = False  # unconditional exit at function level
        depth += s.count('{') - s.count('}')
    return reachable

# Section name -> its index in dump.toml order (static_<idx>_* naming).
sec_name2idx = {s: i for i, (_, s) in enumerate(sec_positions)}

# Pre-pass: index every generated RECOMP_FUNC name so stumps can chain into
# recompiler-synthesized statics too. boot9 (session 3) crash class: ovl_c's
# static_5_8011DA38 (IDO shared-tail mid-entry) truncated at 0x8011DC94 where
# the successor is ANOTHER static (static_5_8011DC94) -- by_sec_vram only
# indexes dump.toml, so the old code silently skipped it and every path-B menu
# widget draw returned with s-regs unrestored (s2=0xA0 => the 0xD2 fault).
all_gen_names = set()
for path in sorted(glob.glob(os.path.join(ROOT, 'RecompiledFuncs', 'funcs_*.c'))):
    src = open(path, 'rb').read().decode('utf-8', errors='replace')
    all_gen_names.update(re.findall(r'RECOMP_FUNC void (\w+)\(uint8_t\* rdram', src))

fixed = 0
for path in sorted(glob.glob(os.path.join(ROOT, 'RecompiledFuncs', 'funcs_*.c'))):
    src = open(path, 'rb').read().decode('utf-8', errors='replace')
    out = src
    for m in re.finditer(r'RECOMP_FUNC void (\w+)\(uint8_t\* rdram, recomp_context\* ctx\) \{(.*?)(^;\})', src, re.S | re.M):
        name, body, close = m.group(1), m.group(2), m.group(3)
        if not end_reachable(body):
            continue
        addrs = re.findall(r'// (0x[0-9A-F]{8}):', body)
        if not addrs:
            continue
        sec = name2sec.get(name)
        if sec is None:
            # N64Recomp emits injected entries as static_<section_index>_<vram>; the
            # index is the section's position in dump.toml order. Resolving by
            # "unique address in dump.toml" instead is WRONG for overlays: boot85's
            # post-match crash was static_9_8012DBB0 (ovl_d, index 9) resolved to
            # ovl_c (the only section with a named symbol at that address) and
            # chained into OVL_C's func_8012DC3C -- menu code run on match data.
            sm = re.match(r'static_(\d+)_[0-9A-Fa-f]+$', name)
            if sm and int(sm.group(1)) < len(sec_positions):
                sec = sec_positions[int(sm.group(1))][1]
            else:
                print('SKIP %s: unknown section' % name)
                continue
        nxt = int(addrs[-1], 16) + 4
        succ = by_sec_vram.get((sec, nxt))
        if succ is None:
            # Successor may be a recompiler-synthesized static in the same
            # section (IDO shared-tail epilogues reached only via j).
            idx = sec_name2idx.get(sec)
            cand = 'static_%d_%08X' % (idx, nxt) if idx is not None else None
            if cand and cand in all_gen_names:
                succ = cand
        if succ is None:
            print('WARN %s (%s): reachable fall-off at 0x%08X with NO same-section '
                  'successor -- unchained stump, will corrupt s-regs if executed' % (name, sec, nxt))
            continue
        patch = ('\n    /* [nomercy fix_stumps] N64Recomp truncated this overlapping function at the next\n'
                 '       same-section symbol without the fall-through; chain it. */\n'
                 '    %s(rdram, ctx);\n    return;\n' % succ)
        out = out.replace(m.group(0), 'RECOMP_FUNC void %s(uint8_t* rdram, recomp_context* ctx) {%s%s%s' %
                          (name, body, patch, close), 1)
        print('chained %s (%s) -> %s at 0x%08X' % (name, sec, succ, nxt))
        fixed += 1
    if out != src:
        open(path, 'wb').write(out.encode('utf-8'))
print(fixed, 'stumps chained')
