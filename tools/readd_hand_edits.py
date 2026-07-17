#!/usr/bin/env python3
"""Re-apply RecompiledFuncs hand-edit diagnostics after a regen — No Mercy.

Every N64Recomp regen wipes hand-edits made directly in RecompiledFuncs/*.c.
This script is the ONLY sanctioned way to carry such edits: register each edit
here (idempotent, safe to re-run) instead of re-adding it by hand, and document
it in docs/bringup-plan.md. Run it AFTER tools/fix_stumps.py +
tools/fix_switches.py + tools/fix_backbranches.py.

NO HAND-EDITS ARE REGISTERED YET for No Mercy. The framework below is kept from
WM2000's version (see the sister repo for worked examples: trap canaries, music
traces, s-reg canary+repair, sp probes — and the gotcha that ctx GPRs are 64-bit
SIGN-EXTENDED, so compare with 0xFFFFFFFF8xxxxxxxull constants).

Anchors are exact generated lines (ending-agnostic: generated files are CRLF,
insertions are LF, the compiler doesn't care). If an anchor is missing the
script FAILS LOUDLY -- regen output changed shape; re-derive the edit and update
this script.
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUNCS = os.path.join(ROOT, 'RecompiledFuncs')

failures = []

def load_lines(fn):
    with open(os.path.join(FUNCS, fn), 'rb') as f:
        return f.read().decode('utf-8', errors='replace').splitlines(keepends=True)

def save_lines(fn, lines):
    with open(os.path.join(FUNCS, fn), 'wb') as f:
        f.write(''.join(lines).encode('utf-8'))

def body_range(lines, func_name):
    start = end = None
    sig = 'RECOMP_FUNC void %s(' % func_name
    for i, l in enumerate(lines):
        if start is None and l.startswith(sig):
            start = i
        elif start is not None and l.rstrip('\r\n') == ';}':
            end = i
            break
    if start is None or end is None:
        failures.append('%s: cannot locate %s' % (func_name, sig))
    return start, end

def insert_after(lines, idx_range, anchor, text, tag):
    """Insert text (list of LF-terminated lines) after the unique line whose
    stripped content == anchor, within idx_range."""
    lo, hi = idx_range
    if lo is None:
        return False
    hits = [i for i in range(lo, hi) if lines[i].rstrip('\r\n') == anchor]
    if len(hits) != 1:
        failures.append('%s: anchor %r matched %d times' % (tag, anchor, len(hits)))
        return False
    lines[hits[0] + 1:hits[0] + 1] = text
    return True

def ensure_include(lines, why):
    if not any(l.startswith('#include <stdio.h>') for l in lines):
        lines.insert(0, '#include <stdio.h>  /* [nomercy HAND-EDIT] %s */\n' % why)

DCC4_PROBE = [
    '    { /* [nomercy][dcc4] portrait-record probe + airbag (bringup session 2): the\n',
    '         char-select crash reads s2=[[elem+0x68]+4] with s2 = 0xA2/0xB2/0xC2/0xD2\n',
    '         (stride-0x10 spaced) => rec+4 holds an UNRELOCATED file offset. Log the\n',
    '         values; on a bad table pointer, skip the draw instead of faulting. */\n',
    '        static int nm_dcc4_ok = 0, nm_dcc4_bad = 0;\n',
    '        uint32_t nm_elem = (uint32_t)ctx->r5;\n',
    '        uint32_t nm_rec = 0, nm_tbl = 0; int nm_rec_ok = 0, nm_tbl_ok = 0;\n',
    '        if (nm_elem >= 0x80000000u && nm_elem < 0x80800000u) {\n',
    '            nm_rec = (uint32_t)MEM_W(ctx->r5, 0X68);\n',
    '            nm_rec_ok = (nm_rec >= 0x80000000u && nm_rec < 0x80800000u);\n',
    '            if (nm_rec_ok) {\n',
    '                nm_tbl = (uint32_t)MEM_W((gpr)(int32_t)nm_rec, 0X4);\n',
    '                nm_tbl_ok = (nm_tbl >= 0x80000000u && nm_tbl < 0x80800000u);\n',
    '            }\n',
    '        }\n',
    '        if (((!nm_rec_ok || !nm_tbl_ok) && nm_dcc4_bad < 200) || nm_dcc4_ok < 12) {\n',
    '            fprintf(stderr, "[nm][dcc4]%s elem=%08X t60=%04X id62=%04X f57=%02X rec=%08X tbl=%08X\\n",\n',
    '                (!nm_rec_ok || !nm_tbl_ok) ? " BAD" : "", nm_elem,\n',
    '                (nm_elem >= 0x80000000u) ? (uint32_t)MEM_HU(ctx->r5, 0X60) : 0u,\n',
    '                (nm_elem >= 0x80000000u) ? (uint32_t)MEM_HU(ctx->r5, 0X62) : 0u,\n',
    '                (nm_elem >= 0x80000000u) ? (uint32_t)MEM_BU(ctx->r5, 0X57) : 0u,\n',
    '                nm_rec, nm_tbl);\n',
    '            if (!nm_rec_ok || !nm_tbl_ok) nm_dcc4_bad++; else nm_dcc4_ok++;\n',
    '        }\n',
    '        if (!nm_rec_ok || !nm_tbl_ok) {\n',
    '            return; /* airbag: nothing executed yet (pre-prologue), out-param DL\n',
    '                       cursor *a0 left untouched = element draws nothing */\n',
    '        }\n',
    '    }\n',
]

def main():
    # Register hand-edits here as bring-up demands them. Pattern per edit:
    #   lines = load_lines('funcs_NN.c')
    #   if not any('[nomercy][mytag]' in l for l in lines):
    #       ensure_include(lines, 'mytag diagnostics')
    #       rng = body_range(lines, 'func_XXXXXXXX')
    #       insert_after(lines, rng, '<exact generated anchor line>',
    #                    ['<LF-terminated diagnostic lines>\n'], 'mytag')
    #       save_lines('funcs_NN.c', lines)
    applied = []

    # [memsz4] REMOVED (session 5): the 4MB-report experiment was moot (nothing in
    # the game reads osMemSize) AND the injected `MEM_W(0X80000318, 0)` was WRONG —
    # the MEM_W macro expects a sign-extended base gpr, so it wrote to rdram+4GB
    # (random host memory; crashed boot25 when ASLR made it unmapped). The removal
    # below strips it from a previously-edited funcs_0.c; harmless if absent.
    lines = load_lines('funcs_0.c')
    if any('[nomercy][memsz4]' in l for l in lines):
        lines = [l for l in lines if '[nomercy][memsz4]' not in l and 'MEM_W(0X80000318, 0) = 0X400000;' not in l.rstrip('\r\n')]
        save_lines('funcs_0.c', lines)
        applied.append('memsz4 REMOVED from funcs_0.c')

    # [emitprobe]/[mstprobe] RETIRED (session 5): the render-pipeline probes that
    # cornered the exploded-wrestler bug. Root cause fixed by tools/fix_selfentry.py
    # (misplaced self-entry branch labels); see docs/bringup-plan.md session 5.

    # [dcc4] char-select portrait crash probe (docs/bringup-plan.md session 2)
    lines = load_lines('funcs_55.c')
    if not any('[nomercy][dcc4]' in l for l in lines):
        ensure_include(lines, 'dcc4 portrait probe')
        rng = body_range(lines, 'func_8011DCC4')
        if insert_after(lines, rng, '    int c1cs = 0;', DCC4_PROBE, 'dcc4'):
            save_lines('funcs_55.c', lines)
            applied.append('dcc4 -> funcs_55.c func_8011DCC4')
    else:
        applied.append('dcc4 (already present)')

    print('readd_hand_edits: ' + ('; '.join(applied) if applied else 'nothing applied'))
    if failures:
        for f_ in failures:
            print('FAIL: ' + f_, file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
