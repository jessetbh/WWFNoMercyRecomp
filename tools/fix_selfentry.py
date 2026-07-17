#!/usr/bin/env python3
"""Post-process RecompiledFuncs: fix misplaced self-entry branch labels.

When a function contains a branch to ITS OWN first instruction (a loop back to
the entry, common in IDO shared-tail interiors carved out as functions),
N64Recomp emits the local label L_<entryvram> AFTER the entry instruction's
code instead of before it. Every looped iteration then SKIPS the entry
instruction. Session-5 poster child: func_800032A4's entry `sra $a1, $s5, 16`
derives the render parity; the emit loop's `bnez ..., func_800032A4` jumped
past it, so gSPForceMatrix targets were computed from a stale pointer half —
wrestler bone matrices were never the ones emitted, models rendered as
exploded wedges ("pixels everywhere") in char-select and matches.

Fix: move the label line to just before the first `// 0x<entryvram>:`
instruction comment. Run after fix_stumps/fix_switches/fix_backbranches and
BEFORE readd_hand_edits in the standard regen loop.
"""
import re
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fixed = 0
for path in sorted(glob.glob(os.path.join(ROOT, 'RecompiledFuncs', 'funcs_*.c'))):
    src = open(path, 'rb').read().decode('utf-8', errors='replace')
    out_parts = []
    pos = 0
    changed = False
    for m in re.finditer(r'RECOMP_FUNC void (?:func_|static_\d+_)([0-9A-F]{8})\(uint8_t\* rdram, recomp_context\* ctx\) \{.*?^;\}',
                         src, re.S | re.M):
        vram = m.group(1)
        body = m.group(0)
        label_line_re = re.compile(r'^L_%s:\r?\n' % vram, re.M)
        lm = label_line_re.search(body)
        if lm is None or ('goto L_%s;' % vram) not in body:
            continue
        insn_idx = body.find('    // 0x%s:' % vram)
        if insn_idx == -1 or lm.start() < insn_idx:
            continue  # label already before the entry instruction (or no insn found)
        # remove the label line, re-insert before the entry instruction comment
        new_body = body[:lm.start()] + body[lm.end():]
        insn_idx = new_body.find('    // 0x%s:' % vram)
        new_body = new_body[:insn_idx] + ('L_%s:\n' % vram) + new_body[insn_idx:]
        out_parts.append(src[pos:m.start()])
        out_parts.append(new_body)
        pos = m.end()
        changed = True
        fixed += 1
        print('fixed self-entry label in %s at func/static %s' % (os.path.basename(path), vram))
    if changed:
        out_parts.append(src[pos:])
        open(path, 'wb').write(''.join(out_parts).encode('utf-8'))
print(fixed, 'self-entry labels relocated')
