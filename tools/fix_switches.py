#!/usr/bin/env python3
"""Post-process RecompiledFuncs: complete N64Recomp-truncated jump tables.

N64Recomp infers a jump table's entry count by reading words until one falls
outside the current function. WM2000's ovl_c has IDO cold-path fragments in
its out-of-line island (rom 0xD0C2C+): a jump-table entry that targets an
island fragment TRUNCATES the inferred table, so indices the game's own
`sltiu` guard allows hit switch_error() -> hard process abort (stdout only!).
boot94: func_801255E4's table at 0x8016DAE8 emitted 29 of 34 cases; menu
states 29-33 killed the process ~21s into vpad play.

For every `default: switch_error(__func__, pc, table)` site:
  1. find the IDO guard: the nearest preceding `< 0xNN ? 1 : 0` (sltiu) in
     the same function body; skip the switch if none (rare, hand-check),
  2. if guard > emitted case count, read the missing entries from the ROM,
  3. for each missing entry:
     - target decoded in this body -> `case N: goto L_<T>;` (insert the label
       at the target's top-level decode line if N64Recomp didn't emit one),
     - target outside the body -> `case N: func_<T>(rdram, ctx); return;`
       (island fragments chain back into text via mid-entry tail calls, same
       as N64Recomp's own j-to-other-function emission). If no function
       starts at T, FAIL LOUDLY: add a mid-entry to syms/extra_funcs.txt
       (`<section> func_<T> 0x<T> <size>`) and regenerate first.

Run after tools/fix_stumps.py in the build loop. Idempotent: a completed
switch satisfies its guard and is never flagged again.
"""
import re, os, glob, struct, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

rom = open(os.path.join(ROOT, 'nomercy.z64'), 'rb').read()
dump = open(os.path.join(ROOT, 'syms', 'dump.toml'), encoding='utf-8').read()

sections = []  # (name, vram, rom, size)
for m in re.finditer(r'\[\[section\]\]\nname = "(\w+)"\nrom = (0x[0-9A-Fa-f]+)\n'
                     r'vram = (0x[0-9A-Fa-f]+)\nsize = (0x[0-9A-Fa-f]+)', dump):
    sections.append((m.group(1), int(m.group(3), 16), int(m.group(2), 16), int(m.group(4), 16)))

func_at = set()  # vrams with a function start (any section)
for m in re.finditer(r'\{ name = "\w+", vram = (0x[0-9A-Fa-f]+)', dump):
    func_at.add(int(m.group(1), 16))

def table_rom_offset(table_vram, func_name, body):
    """Map the table vram to a rom offset via the section the FUNCTION lives in
    (overlay slots overlap in vram; the function's own section is authoritative)."""
    # pick the section containing the function's first decoded address
    first = int(re.search(r'// (0x[0-9A-F]{8}):', body).group(1), 16)
    cands = [(n, r + (table_vram - vr)) for n, vr, r, sz in sections
             if vr <= table_vram < vr + sz and vr <= first < vr + sz]
    if len(cands) == 1:
        return cands[0]
    # fall back: any section covering both (e.g. island vs parent overlay ranges)
    cands2 = [(n, r + (table_vram - vr)) for n, vr, r, sz in sections
              if vr <= table_vram < vr + sz]
    return cands2[0] if len(cands2) == 1 else (cands[0] if cands else None)

sw_err_re = re.compile(r'( *)default: switch_error\(__func__, (0x[0-9A-Fa-f]+), (0x[0-9A-Fa-f]+)\);')
case_re = re.compile(r'case (\d+): ')
guard_re = re.compile(r'< (0X[0-9A-F]+) \? 1 : 0')
func_re = re.compile(r'RECOMP_FUNC void (\w+)\(uint8_t\* rdram, recomp_context\* ctx\) \{(.*?)^;\}',
                     re.S | re.M)

fixed, failed = 0, 0
for path in sorted(glob.glob(os.path.join(ROOT, 'RecompiledFuncs', 'funcs_*.c'))):
    src = open(path, 'rb').read().decode('utf-8', errors='replace')
    out = src
    for fm in func_re.finditer(src):
        name, body = fm.group(1), fm.group(2)
        newbody = body
        for m in sw_err_re.finditer(body):
            indent, pc, table = m.group(1), int(m.group(2), 16), int(m.group(3), 16)
            sw_start = body.rfind('switch (', 0, m.start())
            ncases = len(set(case_re.findall(body[sw_start:m.start()])))
            gm = None
            for g in guard_re.finditer(body[:sw_start]):
                gm = g
            if gm is None:
                continue
            guard = int(gm.group(1), 16)
            if guard <= ncases:
                continue
            loc = table_rom_offset(table, name, body)
            if loc is None:
                print('FAIL %s jr@%08X: cannot map table 0x%08X to rom' % (name, pc, table))
                failed += 1
                continue
            secname, romoff = loc
            adds, label_adds = [], []
            ok = True
            for i in range(ncases, guard):
                t = struct.unpack('>I', rom[romoff + i * 4: romoff + i * 4 + 4])[0]
                dec = '    // 0x%08X:' % t
                if dec in newbody:
                    lbl = 'L_%08X' % t
                    if (re.search(r'^%s:\r?$' % lbl, newbody, re.M) is None
                            and lbl not in [l for _, l in label_adds]):
                        n_top = newbody.count('\n' + dec)
                        if n_top != 1:
                            print('FAIL %s jr@%08X case %d: %d top-level decode lines for 0x%08X'
                                  % (name, pc, i, n_top, t))
                            ok = False
                            break
                        label_adds.append((dec, lbl))
                    adds.append('%scase %d: goto %s; break; /* [nomercy fix_switches] rom table entry */'
                                % (indent, i, lbl))
                elif t in func_at:
                    adds.append('%scase %d: func_%08X(rdram, ctx); return; /* [nomercy fix_switches] out-of-body (island) entry */'
                                % (indent, i, t))
                else:
                    print('FAIL %s jr@%08X case %d: target 0x%08X not decoded here and no '
                          'function starts there. Add "%s func_%08X 0x%08X <size>" to '
                          'syms/extra_funcs.txt and regenerate.'
                          % (name, pc, i, t, secname, t, t))
                    ok = False
                    failed += 1
                    break
            if not ok:
                continue
            for dec, lbl in label_adds:
                newbody = newbody.replace('\n' + dec, '\n%s:\n%s' % (lbl, dec), 1)
            patched = m.group(0).replace(
                '%sdefault:' % indent,
                '%s/* [nomercy fix_switches] table has %d entries per the sltiu guard; N64Recomp\n'
                '%s   stopped at the first out-of-function entry. Completed from rom 0x%X. */\n'
                '%s%s\n%sdefault:' % (indent, guard, indent, romoff, indent,
                                      ('\n' + indent).join(a.strip() for a in adds), indent), 1)
            newbody = newbody.replace(m.group(0), patched, 1)
            print('completed %s jr@%08X table@%08X: %d -> %d cases' % (name, pc, table, ncases, guard))
            fixed += 1
        if newbody != body:
            out = out.replace(fm.group(0), fm.group(0).replace(body, newbody, 1), 1)
    if out != src:
        open(path, 'wb').write(out.encode('utf-8'))
print('%d switches completed, %d failures' % (fixed, failed))
sys.exit(1 if failed else 0)
