#!/usr/bin/env python3
"""Re-apply RecompiledFuncs hand-edit diagnostics after a regen — No Mercy.

Every N64Recomp regen wipes hand-edits made directly in RecompiledFuncs/*.c.
This script is the ONLY sanctioned way to carry such edits: register each edit
here (idempotent, safe to re-run) instead of re-adding it by hand, and document
it in docs/bringup-plan.md. Run it AFTER tools/fix_stumps.py +
tools/fix_switches.py + tools/fix_backbranches.py.

Registered edits: the dcc4 bring-up airbag, and the [wcw fix] edits used by the
60Hz interpolation and netplay (see main()). The framework below is kept from
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

def replace_line(lines, idx_range, anchor, text, tag):
    """Replace the unique line whose stripped content == anchor, within idx_range."""
    lo, hi = idx_range
    if lo is None:
        return False
    hits = [i for i in range(lo, hi) if lines[i].rstrip('\r\n') == anchor]
    if len(hits) != 1:
        failures.append('%s: anchor %r matched %d times' % (tag, anchor, len(hits)))
        return False
    lines[hits[0]:hits[0] + 1] = text
    return True

def insert_after_includes(lines, text):
    """Insert file-scope declarations right after the last #include line."""
    last = max((i for i, l in enumerate(lines) if l.startswith('#include')), default=-1)
    lines[last + 1:last + 1] = text

# [wcw fix] 60Hz: RT64 interpolates this game's models from their camera-independent matrices,
# which the game only exposes at the moment it multiplies them with the camera. These hooks hand
# that pair to RT64 (lib/rt64/src/hle/rt64_rsp.cpp, wcw_interp_* functions).
WCW_MATRIX_DECLS = [
    '#include <string.h>  /* [wcw fix] memcpy for the matrix hook */\n',
    '/* [wcw fix] 60Hz matrix hooks, implemented in RT64 (rt64_rsp.cpp). */\n',
    'extern void wcw_interp_process_multiply(uint32_t destAddr, uint32_t opBAddr, uint32_t srcAddr, const float* aMatrix16, const float* bMatrix16);\n',
    'extern void wcw_interp_invalidate_local_matrix(uint32_t destAddr);\n',
]

WCW_MATRIX_HOOK = [
    '    { /* [wcw fix] 60Hz: func_80001E08 is a 4x4 multiply (out = A * B). When B is the camera\n',
    "         matrix (0x80062BA8), pass A (the object's camera-independent matrix) and B to RT64,\n",
    '         keyed by the output address; otherwise drop any stale entry for that address. */\n',
    '        uint32_t wcw_dst = (uint32_t)ctx->r6;\n',
    '        if ((uint32_t)ctx->r5 == 0x80062BA8u) {\n',
    '            float wcw_a[16], wcw_b[16];\n',
    '            for (int wcw_i = 0; wcw_i < 16; wcw_i++) {\n',
    '                uint32_t wcw_abits = (uint32_t)MEM_W(ctx->r4, wcw_i * 4);\n',
    '                uint32_t wcw_bbits = (uint32_t)MEM_W(ctx->r5, wcw_i * 4);\n',
    '                memcpy(&wcw_a[wcw_i], &wcw_abits, sizeof(float));\n',
    '                memcpy(&wcw_b[wcw_i], &wcw_bbits, sizeof(float));\n',
    '            }\n',
    '            wcw_interp_process_multiply(wcw_dst, (uint32_t)ctx->r5, (uint32_t)ctx->r4, wcw_a, wcw_b);\n',
    '        }\n',
    '        else {\n',
    '            wcw_interp_invalidate_local_matrix(wcw_dst);\n',
    '        }\n',
    '    }\n',
]

WCW_F2L_DECLS = [
    '/* [wcw fix] 60Hz matrix hook, implemented in RT64 (rt64_rsp.cpp). */\n',
    'extern void wcw_interp_forward_local_matrix(uint32_t srcAddr, uint32_t dstAddr);\n',
]

WCW_F2L_HOOK = [
    "    /* [wcw fix] 60Hz: func_80038880 is guMtxF2L. Move the matrix hook's entry from the float\n",
    '       source (a0) to the fixed-point destination (a1) that gSPForceMatrix uploads. */\n',
    '    wcw_interp_forward_local_matrix((uint32_t)ctx->r4, (uint32_t)ctx->r5);\n',
]

WCW_RESYNC_NULL_GUARD = [
    '    /* [wcw fix] netplay: after a resync this lighting-buffer pointer can be null; skip the\n',
    '       call instead of writing through it. */\n',
    '    if (ctx->r4 != 0) {\n',
    '        func_80021B10(rdram, ctx);\n',
    '    }\n',
]

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

# [wcw fix] Widescreen cull (aki-roadmap#47 / #5). func_800024F8 is the per-object view cull of the
# 8x layer passes (func_800025B0): an object is dropped when |x| / (depth + 150) > 1.5 or
# |y| / (depth + 150) > 1.5 (view space; the threshold is the double at 0x80055828). That is about
# 3.8x the 4:3 screen edge, but in Expand the screen is wider, and large set pieces whose center is
# past the line while their geometry reaches into the extra width (barricade segments) vanished at
# the screen edges. Scale only the horizontal limit by the screen's widening
# (wcw_widescreen_cull_scale, src/game/recomp_api.cpp: 1 at Original). The projection is untouched
# (RT64's Expand widens it). Never hook func_800024F8 from a mod: a hooked function is recompiled from
# the ROM without this edit.
WCW_CULL_DECLS = [
    '/* [wcw fix] widescreen cull scale, implemented in src/game/recomp_api.cpp. */\n',
    'extern double wcw_widescreen_cull_scale(void);\n',
]

WCW_CULL_COMPARE = '    c1cs = ctx->f22.d < ctx->f0.d;'
WCW_CULL_X_LOAD = '    ctx->f12.u32l = MEM_W(ctx->r16, 0X18);'
WCW_CULL_EDIT = [
    '    /* [wcw fix] widescreen cull: the horizontal limit follows the screen width (aki-roadmap#47). */\n',
    '    c1cs = (ctx->f22.d * wcw_widescreen_cull_scale()) < ctx->f0.d;\n',
]

def widescreen_cull_edit(lines):
    """Replace func_800024F8's horizontal compare (the first after the x load) in place."""
    lo, hi = body_range(lines, 'func_800024F8')
    if lo is None:
        return False
    loads = [i for i in range(lo, hi) if lines[i].rstrip('\r\n') == WCW_CULL_X_LOAD]
    if len(loads) != 1:
        failures.append('wcw-cull: x load %r matched %d times' % (WCW_CULL_X_LOAD, len(loads)))
        return False
    compares = [i for i in range(loads[0], hi) if lines[i].rstrip('\r\n') == WCW_CULL_COMPARE]
    if len(compares) != 2:
        failures.append('wcw-cull: expected the x and y compares after the x load, found %d' % len(compares))
        return False
    lines[compares[0]:compares[0] + 1] = WCW_CULL_EDIT
    return True

# [nomercy][rt] Ray tracing experiment (exp/raytracing; src/game/raytracing.cpp): hand RT64
# the real camera. RT64 only uses it while ray tracing is active (menu option or WCW_RT).
def rt_hand_edits():
    import os as _os
    funcs = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'RecompiledFuncs')
    def _load(fn):
        with open(_os.path.join(funcs, fn), 'rb') as f:
            return f.read().decode('utf-8', errors='replace').splitlines(keepends=True)
    def _save(fn, lines):
        with open(_os.path.join(funcs, fn), 'wb') as f:
            f.write(''.join(lines).encode('utf-8'))
    def _body(lines, name):
        sig = 'RECOMP_FUNC void %s(' % name
        start = next((i for i, l in enumerate(lines) if l.startswith(sig)), None)
        if start is None:
            return None, None
        end = next((i for i in range(start, len(lines)) if lines[i].rstrip('\r\n') == ';}'), None)
        return start, end
    def _insert_after_all(fn, func, anchor, text, expect):
        lines = _load(fn)
        if any('[nomercy][rt]' in l for l in lines[slice(*_body(lines, func))] if l):
            return '%s already present' % func
        lo, hi = _body(lines, func)
        if lo is None or hi is None:
            raise SystemExit('rt: cannot locate %s in %s' % (func, fn))
        hits = [i for i in range(lo, hi) if lines[i].rstrip('\r\n') == anchor]
        if len(hits) != expect:
            raise SystemExit('rt: anchor %r matched %d times in %s (expected %d)' % (anchor, len(hits), func, expect))
        for i in reversed(hits):
            lines[i + 1:i + 1] = text
        _save(fn, lines)
        return '%s (%d site(s))' % (func, len(hits))
    done = []
    # The latch sits in the matrix multiply func_80001580 calls, not in func_80001580 itself:
    # mods hook func_80001580 (AkiMods' camera) and a hooked function is recompiled from the
    # ROM without hand-edits. Older generated files had it in func_80001580: strip that site.
    lines = _load('funcs_0.c')
    old = next((i for i, l in enumerate(lines) if '[nomercy][rt] camera latch: a1 = P, a2 = V' in l), None)
    if old is not None:
        del lines[old:old + 3]
        _save('funcs_0.c', lines)
        done.append('removed old latch in func_80001580')
    done.append(_insert_after_all('funcs_0.c', 'func_80001E08', '    int c1cs = 0;', [
        '    { /* [nomercy][rt] camera latch: a0 = V, a1 = P, a2 = product (the camera matrix when called from the camera builder) */\n',
        '      extern void wcw_rt_latch_camera_multiply(uint8_t* rdram, uint32_t view_vaddr, uint32_t proj_vaddr, uint32_t dest_vaddr);\n',
        '      wcw_rt_latch_camera_multiply(rdram, (uint32_t)ctx->r4, (uint32_t)ctx->r5, (uint32_t)ctx->r6); }\n'], 1))
    done.append(_insert_after_all('funcs_1.c', 'func_80002FA4', '    func_80002BB4(rdram, ctx);', [
        '    { /* [nomercy][rt] after the frame DL head is returned in v0 */\n',
        '      extern uint32_t wcw_rt_emit_camera(uint8_t* rdram, uint32_t gfx);\n',
        '      ctx->r2 = (int64_t)(int32_t)wcw_rt_emit_camera(rdram, (uint32_t)ctx->r2); }\n'], 1))
    return 'rt camera hand-off: ' + '; '.join(done)


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

    applied.append(rt_hand_edits())

    # [wcw fix] 60Hz matrix hooks (see WCW_MATRIX_HOOK / WCW_F2L_HOOK above)
    lines = load_lines('funcs_0.c')
    if not any('wcw_interp_process_multiply' in l for l in lines):
        rng = body_range(lines, 'func_80001E08')
        if insert_after(lines, rng, '    int c1cs = 0;', WCW_MATRIX_HOOK, 'wcw-matrix'):
            insert_after_includes(lines, WCW_MATRIX_DECLS)
            save_lines('funcs_0.c', lines)
            applied.append('wcw-matrix -> funcs_0.c func_80001E08')
    else:
        applied.append('wcw-matrix (already present)')

    lines = load_lines('funcs_16.c')
    if not any('wcw_interp_forward_local_matrix' in l for l in lines):
        rng = body_range(lines, 'func_80038880')
        if insert_after(lines, rng, '    int c1cs = 0;', WCW_F2L_HOOK, 'wcw-f2l'):
            insert_after_includes(lines, WCW_F2L_DECLS)
            save_lines('funcs_16.c', lines)
            applied.append('wcw-f2l -> funcs_16.c func_80038880')
    else:
        applied.append('wcw-f2l (already present)')

    # [wcw fix] netplay resync null guard (see WCW_RESYNC_NULL_GUARD above)
    lines = load_lines('funcs_10.c')
    if not any('[wcw fix] netplay: after a resync' in l for l in lines):
        rng = body_range(lines, 'func_80022CEC')
        if replace_line(lines, rng, '    func_80021B10(rdram, ctx);', WCW_RESYNC_NULL_GUARD, 'wcw-resync-null'):
            save_lines('funcs_10.c', lines)
            applied.append('wcw-resync-null -> funcs_10.c func_80022CEC')
    else:
        applied.append('wcw-resync-null (already present)')

    # [wcw fix] widescreen cull (see WCW_CULL_EDIT above)
    lines = load_lines('funcs_1.c')
    if not any('wcw_widescreen_cull_scale' in l for l in lines):
        if widescreen_cull_edit(lines):
            insert_after_includes(lines, WCW_CULL_DECLS)
            save_lines('funcs_1.c', lines)
            applied.append('wcw-cull -> funcs_1.c func_800024F8')
    else:
        applied.append('wcw-cull (already present)')

    print('readd_hand_edits: ' + ('; '.join(applied) if applied else 'nothing applied'))
    if failures:
        for f_ in failures:
            print('FAIL: ' + f_, file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
