"""De-relink a relinked No Mercy ROM hack back onto the stock layout, as a ROM-patch mod.

Big No Mercy hacks (No Mercy Plus, ECW Born to be Wired) were relinked: every section moved in
RAM, so almost every function's bytes differ, but mostly only in address immediates. Playing
such a hack through the stock recompiled code needs the hack's code and data at stock
addresses. This tool builds that: a ROM with the stock code/data layout, where

  - functions the hack only moved are the stock bytes (the static recompiled code runs them,
    with every repair the port made);
  - functions the hack changed are its version, with addresses rewritten to stock ones, in
    place (the runtime live-recompiles functions whose bytes differ from stock);
  - code the hack added or reshaped goes to a new code area past the end of the ROM, described
    in relink.json so the runtime registers it; stock entry points that the hack reworked
    become `j <new code>` trampolines;
  - data the hack changed is copied to the stock locations, pointers translated;
  - variables the hack added to bss go to a free Expansion-RAM block;
  - assets keep the hack's ROM offsets (the hack's own file tables point there).

    python tools/romhack/derelink.py analyze --base nomercy.z64 --hack hack.z64
    python tools/romhack/derelink.py build --base nomercy.z64 --hack hack.z64 --id nmplus21 \\
        --name "No Mercy Plus v2.1" --out nmplus21.nrm

Self-contained (Python 3.8+, no third-party modules): it reads the stock layout from
nomercy_layout.json next to this file, so it runs on a player's machine.
"""
import argparse, bisect, collections, io, json, os, struct, sys, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mips
from mips import word, put_word

HERE = os.path.dirname(os.path.abspath(__file__))
LAYOUT_PATH = os.path.join(HERE, 'nomercy_layout.json')
DESC_WORDS = 9


# ------------------------------------------------------------------------------- layouts

class Section:
    """One code section: text, then data, then bss, loaded linearly from rom."""

    def __init__(self, name, rom, text, data, bss, end, funcs=(), slot=None):
        self.name, self.rom, self.text, self.data, self.bss, self.end = name, rom, text, data, bss, end
        self.funcs = list(funcs)    # [(offset, size)] relative to text
        self.slot = slot            # overlay slot (vram) or None for main

    def rom_of(self, vram):
        return self.rom + (vram - self.text)

    @property
    def rom_end(self):
        return self.rom + (self.bss - self.text)

    def to_json(self):
        return dict(name=self.name, rom=self.rom, text=self.text, data=self.data, bss=self.bss, end=self.end,
                    funcs=self.funcs, slot=self.slot)

    @staticmethod
    def from_json(d):
        return Section(d['name'], d['rom'], d['text'], d['data'], d['bss'], d['end'], [tuple(f) for f in d['funcs']], d['slot'])


def read_descriptors(rom, table_off, count):
    out = []
    for i in range(count):
        w = struct.unpack_from('>9I', rom, table_off + 4 * DESC_WORDS * i)
        out.append(w)
    return out


def find_descriptor_table(rom, count=5, lo=0x40000, hi=0x80000):
    """Offset of the overlay descriptor table (9 words each: rom start/end, entry, text start/end,
    data start/end, bss start/end)."""
    def ok(w):
        return (0x1000 <= w[0] < w[1] <= len(rom) and all(0x80000000 <= x < 0x80800000 for x in w[2:9])
                and w[3] <= w[4] <= w[5] <= w[6] <= w[7] <= w[8] and w[1] - w[0] == w[6] - w[3])
    for off in range(lo, hi, 4):
        if all(ok(struct.unpack_from('>9I', rom, off + 0x24 * i)) for i in range(count)):
            return off
    return None


def entry_bss(rom):
    """(bss start, bss size) from the entry point's clear loop (lui/addiu t0, lui/ori t1)."""
    words = [word(rom, 0x1000 + 4 * i) for i in range(20)]
    if words[0] >> 26 == 2:          # a hack's entry hook: follow the jump into its cave
        return None
    refs = mips.hilo_refs(words)
    vals = [a for _, _, a in refs]
    return vals[0], vals[1]


ORDER = ['ovl_a', 'ovl_b', 'ovl_c', 'ovl_e', 'ovl_d']   # descriptor order in the table


def gen_layout(overlays_inl, base_rom):
    """Stock layout from recomp_overlays.inl + the stock ROM's descriptor table and entry."""
    from bps import load_sections
    secs = load_sections(overlays_inl)
    table = find_descriptor_table(base_rom)
    descs = read_descriptors(base_rom, table, 5)
    bss_start, bss_size = entry_bss(base_rom)
    out = []
    for name, rom, ram, size, funcs in secs:
        f = [(off, sz) for _, off, sz in funcs]
        if name == 'section_0_entry_funcs':
            out.append(Section('entry', rom, ram, ram + size, ram + size, ram + size, f))
        elif name.startswith('section_1_main'):
            # main: text, then data up to the bss the entry clears.
            out.append(Section('main', rom, ram, ram + size, bss_start, bss_start + bss_size, f))
        else:
            ovl = name.split('_', 2)[2].rsplit('_funcs', 1)[0]           # ovl_e
            d = descs[ORDER.index(ovl)]
            assert d[0] == rom and d[3] == ram, (name, hex(rom), hex(d[0]))
            out.append(Section(ovl, d[0], d[3], d[5], d[7], d[8], f, slot=ram))
    return dict(game='nomercy', descriptor_table=table, sections=[s.to_json() for s in out])


def load_layout(path=LAYOUT_PATH):
    d = json.load(open(path))
    return d, [Section.from_json(s) for s in d['sections']]


def hack_sections(hack, stock_layout, stock_secs):
    """The hack's section placement: overlays from its descriptor table; main text start and
    rom are assumed unchanged (AKI's boot loads main from rom 0x1000 to 0x80000400)."""
    table = find_descriptor_table(hack)
    if table is None:
        raise SystemExit('hack: no overlay descriptor table found')
    descs = read_descriptors(hack, table, 5)
    out = {}
    for s in stock_secs:
        if s.slot is None:
            out[s.name] = None      # filled in by the matcher (main shifts piecewise)
            continue
        d = descs[ORDER.index(s.name)]
        out[s.name] = Section(s.name, d[0], d[3], d[5], d[7], d[8], slot=None)
    return table, out


# ------------------------------------------------------------------------------ matching

K = 6   # words in the index window


class SectionMatch:
    """Stock -> hack placement of one section's functions.
    matched: [(stock_vram, hack_vram, size)] in stock order (masked shape identical);
    chunks:  [(stock_lo, stock_hi, hack_lo, hack_hi)] code between matched neighbours whose
             shape the hack changed (functions rewritten, added or removed)."""

    def __init__(self, stock, hack_text, hack_text_end):
        self.stock, self.hack_text, self.hack_text_end = stock, hack_text, hack_text_end
        self.matched, self.chunks = [], []


def masked_words(data, off, n):
    return [mips.masked(word(data, off + 4 * i)) for i in range(n)]


def match_section(base, hack, stock, hack_rom, hack_text, hack_text_end, slack=0x400):
    """Locate each stock function of `stock` in the hack's text [hack_text, hack_text_end),
    loaded from hack_rom. Masked full-function compare; among candidates prefer the one
    nearest the neighbours' shift; isolated shift outliers are dropped back to unmatched."""
    hrom = lambda v: hack_rom + (v - hack_text)
    n_hack = (hack_text_end - hack_text) // 4
    hw = [mips.masked(word(hack, hrom(hack_text) + 4 * i)) for i in range(n_hack + K)]
    index = collections.defaultdict(list)
    for i in range(n_hack):
        index[tuple(hw[i:i + K])].append(i)
    small = {n: collections.defaultdict(list) for n in range(2, K)}
    for i in range(n_hack):
        for n in range(2, K):
            small[n][tuple(hw[i:i + n])].append(i)
    funcs = sorted(stock.funcs)
    cands = []
    for off, size in funcs:
        n = size // 4
        sv = stock.text + off
        sw = masked_words(base, stock.rom_of(sv), n)
        if n < 2:
            cands.append([])
            continue
        if n >= K:
            pos = index.get(tuple(sw[:K]), [])
        else:
            pos = small[n].get(tuple(sw), [])
        good = [hack_text + 4 * i for i in pos if hw[i:i + n] == sw and i + n <= n_hack]
        cands.append(good)
    # pass 1: unique candidates are anchors
    choice = [None] * len(funcs)
    for k, good in enumerate(cands):
        if len(good) == 1:
            choice[k] = good[0]
    # drop anchor outliers whose shift differs from both anchor neighbours and that are short
    def anchors():
        return [(k, choice[k] - (stock.text + funcs[k][0])) for k in range(len(funcs)) if choice[k] is not None]
    for _ in range(2):
        a = anchors()
        for j, (k, d) in enumerate(a):
            prev_d = a[j - 1][1] if j > 0 else None
            next_d = a[j + 1][1] if j + 1 < len(a) else None
            if d != prev_d and d != next_d and (prev_d is not None or next_d is not None):
                if prev_d == next_d or funcs[k][1] < 0x40:
                    choice[k] = None
    # pass 2: non-unique candidates take the one nearest the previous anchor's shift,
    # if it keeps the hack order monotonic
    a = anchors()
    ak = [k for k, _ in a]
    for k, good in enumerate(cands):
        if choice[k] is not None or len(good) < 2:
            continue
        j = bisect.bisect_left(ak, k)
        prev = a[j - 1] if j > 0 else None
        nxt = a[j] if j < len(a) else None
        ref = [x[1] for x in (prev, nxt) if x is not None]
        sv = stock.text + funcs[k][0]
        best = min(good, key=lambda g: min(abs((g - sv) - d) for d in ref) if ref else 0)
        if ref and (best - sv) in ref:
            choice[k] = best
    # monotonic check over the final list
    m = SectionMatch(stock, hack_text, hack_text_end)
    last_hack_end = hack_text
    for k, (off, size) in enumerate(funcs):
        h = choice[k]
        if h is not None and h < last_hack_end:
            choice[k] = None
            continue
        if h is not None:
            last_hack_end = h + size
    # inside a stretch whose size changed, functions that still appear exactly once in the
    # hack's part of it are matched there too (only the reworked function stays unmatched)
    chosen = [k for k in range(len(funcs)) if choice[k] is not None]
    bounds = [(-1, hack_text)] + [(k, choice[k] + funcs[k][1]) for k in chosen] + [(len(funcs), hack_text_end)]
    for (k0, h0), (k1, _) in zip(bounds, bounds[1:]):
        h1 = choice[k1] if k1 < len(funcs) else hack_text_end
        s0 = stock.text + (funcs[k0][0] + funcs[k0][1] if k0 >= 0 else 0)
        s1 = stock.text + (funcs[k1][0] if k1 < len(funcs) else max(o + z for o, z in funcs))
        if (s1 - s0) == (h1 - h0) or k1 - k0 < 2:
            continue
        last = h0
        for k in range(k0 + 1, k1):
            inside = [g for g in cands[k] if last <= g and g + funcs[k][1] <= h1]
            if len(inside) == 1:
                choice[k] = inside[0]
                last = inside[0] + funcs[k][1]
    # build matched + chunks
    prev_s_end, prev_h_end = stock.text, hack_text
    for k, (off, size) in enumerate(funcs):
        h = choice[k]
        if h is None:
            continue
        s = stock.text + off
        if s > prev_s_end or h > prev_h_end:
            m.chunks.append((prev_s_end, s, prev_h_end, h))
        m.matched.append((s, h, size))
        prev_s_end, prev_h_end = s + size, h + size
    s_end = stock.text + max(o + z for o, z in funcs)
    if s_end > prev_s_end or hack_text_end > prev_h_end:
        m.chunks.append((prev_s_end, s_end, prev_h_end, hack_text_end))
    return m


def ref_pairs(base, hack, stock, sm, hack_rom, hack_text):
    """(stock_addr, hack_addr) for every jal target and %hi/%lo address in functions whose masked
    shape matched (same instruction index in both). The raw evidence for the address map."""
    out = []
    for s, h, size in sm.matched:
        n = size // 4
        sw = [word(base, stock.rom_of(s) + 4 * i) for i in range(n)]
        hw = [word(hack, hack_rom + (h - hack_text) + 4 * i) for i in range(n)]
        if sw == hw:
            continue
        for (i, kind, t), (j, kind2, t2) in zip(mips.jumps(sw, s), mips.jumps(hw, h)):
            if i == j and kind == kind2 and kind == 'jal':
                out.append((t, t2))
        a = {(hi, lo): v for hi, lo, v in mips.hilo_refs(sw)}
        b = {(hi, lo): v for hi, lo, v in mips.hilo_refs(hw)}
        for k, v in a.items():
            if k in b:
                out.append((v, b[k]))
    return out


# ---------------------------------------------------------------------------- address map

class AddressMap:
    """hack RAM address -> stock RAM address. Ranges are (hack_lo, hack_hi, delta, kind);
    main's ranges are global, each overlay's apply when code of that overlay (or any overlay
    sharing the slot, if they agree) references them. Expansion RAM maps to itself."""

    def __init__(self):
        self.main = []          # [(lo, hi, delta, kind)]
        self.ovl = {}           # name -> [(lo, hi, delta, kind)]
        self.priority = []      # checked first (code moved out of a data area)
        self.evidence = {}      # overlay -> sorted hack addresses its own code references
        self.identity = [(0x80400000, 0x80800000)]
        self.rom = []           # [(hack_rom_lo, hack_rom_hi, delta)] for the code region (weak candidates)

    @staticmethod
    def _find(ranges, a):
        i = bisect.bisect_right(ranges, (a, 0xFFFFFFFF)) - 1
        if i >= 0:
            lo, hi, d, kind = ranges[i]
            if lo <= a < hi:
                return (a + d) & 0xFFFFFFFF, kind
        return None

    def lookup(self, a, ctx=None):
        """(stock address, kind) or None. ctx: overlay name of the referencing code."""
        r = self._find(self.priority, a)
        if r:
            return r
        r = self._find(self.main, a)
        if r:
            return r
        for lo, hi in self.identity:
            if lo <= a < hi:
                return a, 'exp'
        if ctx in self.ovl:
            r = self._find(self.ovl[ctx], a)
            if r:
                return r
        # another overlay that can be resident at the same time (No Mercy: ovl_a with one of
        # b/c/d; ovl_e alone): take the shift the candidates agree on
        for group in self.partners(ctx):
            hits = {self._find(self.ovl[o], a) for o in group if o in self.ovl} - {None}
            if len(hits) == 1:
                return hits.pop()
            shifts = {(h[0] - a) & 0xFFFFFFFF for h in hits}
            if len(shifts) == 1:
                return hits.pop()
        return None

    def candidates(self, a, ctx=None):
        """Every plausible stock address for hack address a, best first."""
        out = []
        def add(r):
            if r is not None and r[0] not in out:
                out.append(r[0])
        add(self._find(self.priority, a))
        add(self._find(self.main, a))
        for lo, hi in self.identity:
            if lo <= a < hi:
                add((a, 'exp'))
        if ctx in self.ovl:
            add(self._find(self.ovl[ctx], a))
        groups = self.partners(ctx) + [tuple(self.ovl)]
        if ctx is None and self.evidence:
            # code with no overlay of its own (payload, caves): prefer the overlay whose own code
            # references a variable right below this address
            near = []
            for o, hs in self.evidence.items():
                i = bisect.bisect_right(hs, a) - 1
                if i >= 0 and a - hs[i] < 0x100 and self._find(self.ovl.get(o, []), a):
                    near.append((a - hs[i], o))
            if near:
                groups = [(min(near)[1],)] + groups
        for group in groups:
            for o in group:
                if o in self.ovl:
                    add(self._find(self.ovl[o], a))
        if a < 0x00800000 or 0xA0000000 <= a < 0xA0800000:
            # a physical (or uncached KSEG1) RAM address: translate it as KSEG0
            seg = a & 0xE0000000
            for c in self.candidates((a & 0x1FFFFFFF) | 0x80000000, ctx):
                if c >= 0x80000000:
                    c = (c & 0x1FFFFFFF) | seg
                    if c not in out:
                        out.append(c)
        for rlo, rhi, d in self.rom:
            if rlo <= a < rhi:
                out.append((a + d) & 0xFFFFFFFF)
                break
        if a >= 0x80000004 and not getattr(self, '_in_end', False):
            # one past the end of a section (bss end, table end)
            self._in_end = True
            try:
                for c in self.candidates(a - 4, ctx):
                    if c >= 0x80000000 and c + 4 not in out:
                        out.append(c + 4)
            finally:
                self._in_end = False
        return out

    PARTNERS = {'ovl_a': [('ovl_b', 'ovl_c', 'ovl_d')], 'ovl_b': [('ovl_a',)], 'ovl_c': [('ovl_a',)],
                'ovl_d': [('ovl_a',)], 'ovl_e': []}

    def partners(self, ctx):
        if ctx in self.PARTNERS:
            return self.PARTNERS[ctx]
        return [('ovl_b', 'ovl_c', 'ovl_d'), ('ovl_a',), ('ovl_e',), ('ovl_a', 'ovl_e')]

    def translate(self, a, ctx=None):
        r = self.lookup(a, ctx)
        return None if r is None else r[0]

    def finish(self):
        self.main.sort()
        for k in self.ovl:
            self.ovl[k].sort()

    def to_json(self):
        enc = lambda rs: [[lo, hi, d - (1 << 32) if d & 0x80000000 else d, k] for lo, hi, d, k in rs]
        return dict(main=enc(self.main), overlays={k: enc(v) for k, v in self.ovl.items()},
                    identity=self.identity)


def runs_from_pairs(pairs, lo, hi):
    """Piecewise ranges over hack addresses [lo, hi) from (stock, hack) evidence: each hack
    address takes the shift of the nearest evidence at or below it (majority per address).
    A lone evidence point whose neighbours agree on another shift is dropped as a stray."""
    per = collections.defaultdict(collections.Counter)
    for s, h in pairs:
        if lo <= h < hi:
            per[h][(s - h) & 0xFFFFFFFF] += 1
    pts = sorted((h, c.most_common(1)[0][0]) for h, c in per.items())
    runs = []                     # [first_h, delta, count]
    for h, d in pts:
        if runs and runs[-1][1] == d:
            runs[-1][2] += 1
        else:
            runs.append([h, d, 1])
    changed = True
    while changed:
        changed = False
        for i in range(1, len(runs) - 1):
            if runs[i][2] == 1 and runs[i - 1][1] == runs[i + 1][1]:
                runs[i - 1][2] += runs[i + 1][2]
                del runs[i:i + 2]
                changed = True
                break
    out = []
    for i, (h, d, n) in enumerate(runs):
        start = lo if i == 0 else h
        end = runs[i + 1][0] if i + 1 < len(runs) else hi
        out.append((start, end, d))
    # the last evidence of each run bounds where the next shift can start
    runs_from_pairs.last_evidence = []
    for i in range(len(out) - 1):
        nxt = out[i + 1][0]
        prev = [h for h, d in pts if h < nxt and out[i][0] <= h]
        runs_from_pairs.last_evidence.append(max(prev) if prev else out[i][0])
    return out


class Analysis:
    """Everything derived from (stock layout, hack ROM): per-section matches, hack placement,
    the address map and the hack-ROM -> stock-ROM map for the code region."""

    def __init__(self, base, hack, layout_path=LAYOUT_PATH):
        self.base, self.hack = base, hack
        self.layout, self.secs = load_layout(layout_path)
        self.byname = {s.name: s for s in self.secs}
        self.table, self.hsecs = hack_sections(hack, self.layout, self.secs)
        self.matches = {}
        self.place = {}          # name -> (hack_rom, hack_text, hack_text_end, hack_data, hack_end)
        main = self.byname['main']
        # main: same rom and start; its text end is found from the last matched function
        m = match_section(base, hack, main, main.rom, main.text, main.data + 0x200)
        last_s, last_h, last_z = m.matched[-1]
        main_text_end = main.data + (last_h - last_s)
        m = match_section(base, hack, main, main.rom, main.text, main_text_end)
        self.matches['main'] = m
        slot_a = min(h.text for h in self.hsecs.values() if h is not None)
        self.place['main'] = (main.rom, main.text, main_text_end, main_text_end, slot_a)
        for s in self.secs:
            if s.slot is None:
                continue
            h = self.hsecs[s.name]
            self.matches[s.name] = match_section(base, hack, s, h.rom, h.text, h.data)
            self.place[s.name] = (h.rom, h.text, h.data, h.data, h.end)
        e = self.byname['entry']
        self.place['entry'] = (e.rom, e.text, e.data, e.data, e.data)
        # units: matched functions plus same-size chunks, (stock vram, hack vram, size)
        self.units = {}
        self.resized = []
        self.shrunk = {}         # (section, stock vram) -> hack size, for functions the hack shortened
        for name, m in self.matches.items():
            u = list(m.matched)
            for slo, shi, hlo, hhi in m.chunks:
                if shi - slo == hhi - hlo:
                    if shi > slo:
                        u.append((slo, hlo, shi - slo))
                elif shi > slo or hhi > hlo:
                    st = self.byname[name]
                    inside = [o for o, z in st.funcs if slo <= st.text + o < shi]
                    if name != 'main' and len(inside) == 1 and 0 < hhi - hlo <= shi - slo:
                        # one function the hack made shorter: its version fits in place
                        # (main's resized code is the Rev 0 flash-save code, which stays Rev 1)
                        u.append((slo, hlo, shi - slo))
                        self.shrunk[(name, slo)] = hhi - hlo
                    else:
                        self.resized.append((name, slo, shi, hlo, hhi))
            self.units[name] = sorted(u)
        self.build_map()

    def hack_rom_of(self, name, vram):
        hrom, htext = self.place[name][:2]
        return hrom + (vram - htext)

    def build_map(self):
        amap = AddressMap()
        amap.identity.append((0x80000000, 0x80000400))
        pairs = {}
        for name, m in self.matches.items():
            hrom, htext = self.place[name][:2]
            pairs[name] = ref_pairs(self.base, self.hack, self.byname[name], m, hrom, htext)
        for name, m in self.matches.items():
            rs = amap.main if name == 'main' else amap.ovl.setdefault(name, [])
            for s, h, z in m.matched:
                rs.append((h, h + z, (s - h) & 0xFFFFFFFF, 'text'))
            for slo, shi, hlo, hhi in m.chunks:
                if (shi - slo == hhi - hlo or (name, slo) in self.shrunk) and hhi > hlo:
                    rs.append((hlo, hhi, (slo - hlo) & 0xFFFFFFFF, 'text'))
            # entry points inside size-changed chunks, from calls in matched code
            for slo, shi, hlo, hhi in m.chunks:
                if shi - slo != hhi - hlo:
                    for ps, ph in set(p for ps in pairs.values() for p in ps):
                        if hlo <= ph < hhi and slo <= ps < shi:
                            rs.append((ph, ph + 4, (ps - ph) & 0xFFFFFFFF, 'entry'))
            _, _, _, dlo, dhi = self.place[name]
            if name == 'main':
                ev = [p for ps in pairs.values() for p in ps]
            else:
                ev = pairs[name]
                # the overlay's own data/bss: one shift from its descriptor unless evidence says otherwise
                s = self.byname[name]
                ev = ev + [(s.data, dlo)]
            runs = runs_from_pairs(ev, dlo, dhi)
            runs = self.refine_boundaries(name, runs, list(runs_from_pairs.last_evidence))
            for lo, hi, d in runs:
                rs.append((lo, hi, d, 'data'))
        # above the overlay slots: the heap arena (its header is referenced by address)
        top = max(p[4] for p in self.place.values())
        allp = [p for ps in pairs.values() for p in ps]
        for lo, hi, d in runs_from_pairs(allp, top, 0x80400000):
            if any(top <= h < 0x80400000 for _, h in allp):
                amap.main.append((lo, hi, d, 'heap'))
        for name, (hrom, htext, htext_end, hdata, hend) in self.place.items():
            st = self.byname[name]
            hdata_rom_end = hrom + (self.hsecs[name].bss - htext) if self.hsecs.get(name) else hrom + (st.bss - st.text) + 0x100
            # ROM offsets inside a section translate like its RAM addresses (weak candidates)
            rs = amap.main if name in ('main', 'entry') else amap.ovl[name]
            for lo, hi, d, kind in rs:
                if kind == 'exp':
                    continue
                rl, rh = hrom + (lo - htext), hrom + (hi - htext)
                if rl < hrom or rl >= hdata_rom_end:
                    continue
                rd = ((st.rom - st.text) - (hrom - htext) + d) & 0xFFFFFFFF
                amap.rom.append((rl, min(rh, hdata_rom_end), rd))
        amap.rom.sort()
        amap.evidence = {n: sorted({h for _, h in ps}) for n, ps in pairs.items() if n in amap.ovl}
        amap.finish()
        self.map = amap
        self.pairs = pairs

    def refine_boundaries(self, name, runs, last_ev):
        """Place each shift change inside its evidence gap where the hack's data lines up
        best with the stock data (a few words inserted or removed between two variables)."""
        st = self.byname[name]
        hrom, htext = self.place[name][:2]
        hack_bss = self.hsecs[name].bss if self.hsecs.get(name) else None
        def score_word(y, d):
            x = (y + d) & 0xFFFFFFFF
            if not (st.data <= x < st.bss) or (hack_bss is not None and y >= hack_bss):
                return 0
            return 1 if word(self.hack, hrom + (y - htext)) == word(self.base, st.rom_of(x)) else 0
        out = [list(r) for r in runs]
        for i in range(len(out) - 1):
            a, b = last_ev[i] + 4, out[i + 1][0]
            if b - a > 0x4000 or b <= a:
                continue
            d1, d2 = out[i][2], out[i + 1][2]
            best, best_score = b, -1
            # score(b') = matches with d1 below b' plus matches with d2 from b'
            s1 = [score_word(y, d1) for y in range(a, b, 4)]
            s2 = [score_word(y, d2) for y in range(a, b, 4)]
            n = len(s1)
            pre = [0]
            for v in s1:
                pre.append(pre[-1] + v)
            suf = [0] * (n + 1)
            for k in range(n - 1, -1, -1):
                suf[k] = suf[k + 1] + s2[k]
            for k in range(n + 1):
                sc = pre[k] + suf[k]
                if sc > best_score or (sc == best_score and k == n):
                    best, best_score = a + 4 * k, sc
            out[i][1] = best
            out[i + 1][0] = best
        return [tuple(r) for r in out]

    def ctx_of_hack(self, name):
        return None if name in ('main', 'entry') else name


# ------------------------------------------------------------------------------ porting

class Unresolved(Exception):
    pass


def rewrite(words, hack_pc, new_pc, candidates, stock_words=None, problems=None):
    """Rewrite one function's absolute addresses from hack to stock space.
    words: the hack's words (at hack_pc); new_pc: where the result will live; candidates(a) ->
    plausible stock addresses for hack address a, best first (the last ones may be ROM-offset
    readings, used only when they agree with the stock code). stock_words: the stock counterpart
    when the shapes match: a reference whose words equal the stock ones is kept, and among the
    candidates the one that reproduces the stock words wins.
    Returns the new words. Unresolvable references go to `problems` (index, kind, hack address)
    and are left as they were."""
    out = list(words)
    n = len(words)
    lo_pc, hi_pc = hack_pc, hack_pc + 4 * n
    has_stock = stock_words is not None
    same = lambda idx: has_stock and all(words[i] == stock_words[i] for i in idx)

    def cands(a, ram_only=True):
        if lo_pc <= a < hi_pc:                      # inside the function itself
            return [new_pc + (a - hack_pc)]
        c = candidates(a)
        if ram_only:
            c = [x for x in c if x >= 0x80000000] if a >= 0x80000000 else []
        return c

    for i, kind, t in mips.jumps(words, hack_pc):
        if same([i]):
            continue
        cs = cands(t)
        pick = None
        for c in cs:
            w = mips.with_jump_target(words[i], c)
            if has_stock and w == stock_words[i]:
                pick = c
                break
        if pick is None and cs:
            pick = cs[0]
        if pick is None:
            if problems is not None:
                problems.append((i, kind, t))
            continue
        out[i] = mips.with_jump_target(words[i], pick)
    for i, w in enumerate(words):                    # branches that leave the function
        t = mips.branch_target(w, hack_pc + 4 * i)
        if t is None or lo_pc <= t < hi_pc or same([i]):
            continue
        cs = cands(t)
        nt = cs[0] if cs else None
        off = None if nt is None else (nt - (new_pc + 4 * i + 4)) >> 2
        if off is None or not -0x8000 <= off < 0x8000:
            if problems is not None:
                problems.append((i, 'branch', t))
            continue
        out[i] = (w & 0xFFFF0000) | (off & 0xFFFF)
    groups = collections.defaultdict(list)
    for hi, lo, a in mips.hilo_refs(words):
        groups[hi].append((lo, a))

    def halves(lo_word, na):
        if lo_word >> 26 == 0x0D:                    # ori: unsigned low half
            return na >> 16, na & 0xFFFF
        return ((na + 0x8000) >> 16) & 0xFFFF, na & 0xFFFF

    for hi, uses in groups.items():
        idx = [hi] + [lo for lo, _ in uses]
        if same(idx):
            continue
        new = []
        for lo, a in uses:
            cs = cands(a, ram_only=False)
            pick = None
            if has_stock:
                for c in cs:
                    h16, l16 = halves(words[lo], c)
                    if (stock_words[lo] & 0xFFFF) == l16 and (stock_words[hi] & 0xFFFF) == h16:
                        pick = c
                        break
            if pick is None:
                ram = [c for c in cs if c >= 0x80000000] if a >= 0x80000000 else []
                pick = ram[0] if ram else None
            if pick is None:
                if 0x80000000 <= a < 0x80800000 and problems is not None:
                    problems.append((lo, 'addr', a))
                pick = a
            new.append((lo, pick))
        his = {halves(words[lo], na)[0] for lo, na in new}
        if len(his) != 1:
            if problems is not None:
                problems.append((hi, 'split-hi', [hex(na) for _, na in new]))
            continue
        out[hi] = (words[hi] & 0xFFFF0000) | his.pop()
        for lo, na in new:
            out[lo] = (words[lo] & 0xFFFF0000) | (na & 0xFFFF)
    return out


class Port:
    """One function's port: stock placement, the rewritten words, and why."""
    def __init__(self, section, stock_vram, size, words, status, problems):
        self.section, self.stock_vram, self.size, self.words = section, stock_vram, size, words
        self.status, self.problems = status, problems


def keep_stock_zones(A, name, gap=0x400):
    """Stretches of stock code around the chunks the hack resized, merged when closer than
    `gap`. Matched functions in between belong to the same rewrite (in No Mercy's relinked hacks:
    the Rev 0 flash-save code that Rev 1 replaced) and keep the stock code with them."""
    spans = sorted((slo, shi) for n, slo, shi, hlo, hhi in A.resized if n == name and shi > slo)
    out = []
    for lo, hi in spans:
        if out and lo - out[-1][1] < gap:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [tuple(z) for z in out]


def port_code(A):
    """Classify every stock function and port the changed ones in place.
    status: 'moved' (translated hack == stock: keep the static code), 'changed' (ported in place).
    Stretches of code the hack resized are listed in A.resized and keep the stock code."""
    ports = []
    for name, units in A.units.items():
        stock = A.byname[name]
        ctx = A.ctx_of_hack(name)
        tr = lambda a, ctx=ctx: A.map.candidates(a, ctx)
        zones = keep_stock_zones(A, name)
        for s, h, z in units:
            if any(lo <= s and s + z <= hi for lo, hi in zones):
                ports.append(Port(name, s, z, None, 'kept', []))
                continue
            n = z // 4
            sw = [word(A.base, stock.rom_of(s) + 4 * i) for i in range(n)]
            probs = []
            if (name, s) in A.shrunk:
                hn = A.shrunk[(name, s)] // 4
                hw = [word(A.hack, A.hack_rom_of(name, h) + 4 * i) for i in range(hn)]
                nw = rewrite(hw, h, s, tr, None, probs) + [0] * (n - hn)
            else:
                hw = [word(A.hack, A.hack_rom_of(name, h) + 4 * i) for i in range(n)]
                nw = rewrite(hw, h, s, tr, sw, probs)
            status = 'moved' if nw == sw else 'changed'
            ports.append(Port(name, s, z, nw, status, probs))
    return ports, A.resized


# ------------------------------------------------------------------ code outside the text

PAYLOAD_RAM_LIMIT = 0x80800000


def find_payload(hack):
    """(rom, ram, size, zero_lo, zero_hi) of the Expansion-RAM payload the hack's entry hook
    copies with direct cart reads, or None. The hook: `j cave` at the entry point; the cave
    zeroes [zero_lo, zero_hi) and copies ROM (KSEG1 0xB0000000+rom) to RAM until an end address."""
    w0 = word(hack, 0x1000)
    if w0 >> 26 != 2:
        return None
    cave = mips.jump_target(w0, 0x80000400)
    rom = 0x1000 + (cave - 0x80000400)
    words = [word(hack, rom + 4 * i) for i in range(64)]
    vals = [a for _, _, a in mips.hilo_refs(words)]
    cart = [v for v in vals if 0xB0000000 <= v < 0xB4000000]
    ram = [v for v in vals if 0x80400000 <= v <= PAYLOAD_RAM_LIMIT]
    if not cart or len(ram) < 4:
        return None
    zero_lo, zero_hi, dst, end = ram[0], ram[1], ram[2], ram[3]
    return dict(rom=cart[0] - 0xB0000000, ram=dst, size=end - dst, zero_lo=zero_lo, zero_hi=zero_hi + 4,
                cave=cave, resume=0x80000404)


def trace_extent(get_word, entry, lo, hi):
    """Linear extent [entry, end) of code entered at `entry` within [lo, hi): follows branches
    and fallthrough, stops after jr / j (j targets are separate entries). Returns
    (end, j_targets, jal_targets, back_branches)."""
    seen = set()
    work = [entry]
    end = entry
    jt, jalt, back = set(), set(), set()
    while work:
        pc = work.pop()
        while lo <= pc < hi and pc not in seen:
            seen.add(pc)
            w = get_word(pc)
            end = max(end, pc + 8)
            op = w >> 26
            t = mips.branch_target(w, pc)
            if t is not None:
                if t < entry:
                    back.add(t)
                elif t not in seen:
                    work.append(t)
                # beq zero,zero (b) is unconditional
                if op == 0x04 and (w >> 16) & 0x3FF == 0:
                    seen.add(pc + 4)
                    break
            if op == 2:
                jt.add(mips.jump_target(w, pc))
                seen.add(pc + 4)
                break
            if op == 3:
                jalt.add(mips.jump_target(w, pc))
            if op == 0 and (w & 0x3F) == 0x08:          # jr
                seen.add(pc + 4)
                break
            pc += 4
    return min(end, hi), jt, jalt, back


NEW_CODE_VRAM = 0x8F000000      # relocated hack code: never a data address (mods use 0x81000000+), only ever called


class Extra:
    """Code the runtime must live-recompile and register besides the in-place ports.
    kind: 'payload' (Expansion-RAM code, vram = its RAM address), 'cave' (code the hack put in
    main's data area, moved to NEW_CODE_VRAM), 'mid' (a hook returns into the middle of a
    function: registered as its own function from there to the function's end)."""
    def __init__(self, kind, hack_vram, vram, size, section=None):
        self.kind, self.hack_vram, self.vram, self.size, self.section = kind, hack_vram, vram, size, section
        self.rom = None             # set when the ROM is assembled
        self.problems = []


def discover_extra_code(A):
    """Find the hack's code outside the stock text: payload functions, main-data caves and
    mid-function return points. Fills A.payload, A.extras, A.cave_map."""
    A.payload = find_payload(A.hack)
    main = A.byname['main']
    hrom, htext, htext_end = A.place['main'][:3]
    data_shift = A.map.translate(htext_end) - htext_end           # main data: one shift
    hack_main_data = (htext_end, main.bss - data_shift)
    regions = []
    if A.payload:
        p = A.payload
        regions.append(('payload', p['ram'], p['ram'] + p['size'], lambda v, p=p: word(A.hack, p['rom'] + (v - p['ram']))))
    regions.append(('cave', hack_main_data[0], hack_main_data[1], lambda v: word(A.hack, hrom + (v - htext))))
    if A.payload:     # the entry hook's own cave is replaced by a native preload
        skip = {A.payload['cave']}
    else:
        skip = set()

    def region_of(a):
        for r in regions:
            if r[1] <= a < r[2]:
                return r
        return None

    # seeds: j/jal from every hack text section into a region
    work = []
    for name, (srom, stext, stext_end, _, _) in A.place.items():
        words = [word(A.hack, srom + 4 * i) for i in range((stext_end - stext) // 4)]
        for i, kind, t in mips.jumps(words, stext):
            if region_of(t) and t not in skip:
                work.append(t)
    # seeds from data: jump-table cases and function pointers the hack pointed at its own code
    # (the stock word at the same place is a code address)
    stock_text = [(st.text, st.data) for st in A.secs]
    data_seed_owner = {}
    for name, (srom, stext, stext_end, hdata, hend) in A.place.items():
        if name == 'entry':
            continue
        st = A.byname[name]
        rs = A.map.main if name == 'main' else A.map.ovl[name]
        for lo, hi, d, kind in rs:
            if kind != 'data':
                continue
            for y in range(lo, hi, 4):
                x = (y + d) & 0xFFFFFFFF
                if not (st.data <= x < st.bss):
                    continue
                hw = word(A.hack, srom + (y - stext))
                if region_of(hw) and hw not in skip:
                    sw = word(A.base, st.rom_of(x))
                    if any(a <= sw < b for a, b in stock_text):
                        work.append(hw)
                        data_seed_owner.setdefault(hw, set()).add((name, sw))
    entries = {}
    text_jumps = set()
    while work:
        e = work.pop()
        if e in entries or e in skip:
            continue
        r = region_of(e)
        end, jt, jalt, back = trace_extent(r[3], e, r[1], r[2])
        entries[e] = (r[0], end, back)
        for t in jt | jalt:
            if region_of(t):
                work.append(t)
            elif t not in skip:
                text_jumps.add(t)
    A.region_entries = entries
    A.text_jumps = text_jumps

    # caves move to the new-code area, keeping their relative layout
    caves = sorted((e, end) for e, (k, end, _) in entries.items() if k == 'cave')
    A.cave_map = []
    if caves:
        lo, hi = caves[0][0], max(end for _, end in caves)
        A.cave_map = [(lo, hi, (NEW_CODE_VRAM - lo) & 0xFFFFFFFF)]
        A.map.priority = [(lo, hi, (NEW_CODE_VRAM - lo) & 0xFFFFFFFF, 'cave')]
    extras = []
    for e, (k, end, back) in sorted(entries.items()):
        v = e if k == 'payload' else A.map.translate(e)
        x = Extra(k, e, v, end - e)
        extras.append(x)

    # mid-function return points: a hook entered by a `j` from function U returns into U
    reach_from = {}
    def reach(e0):
        if e0 in reach_from:
            return reach_from[e0]
        seen, out, work = set(), set(), [e0]
        while work:
            e = work.pop()
            if e in seen or e not in entries:
                continue
            seen.add(e)
            r = region_of(e)
            end, jt, jalt, back = trace_extent(r[3], e, r[1], r[2])
            for t in jt:
                if region_of(t):
                    work.append(t)
                else:
                    out.add(t)
        reach_from[e0] = out
        return out
    owners = collections.defaultdict(set)          # hack text address -> {(section, s, h, z)}
    for name, units in A.units.items():
        srom, stext = A.place[name][:2]
        for s, h, z in units:
            words = [word(A.hack, srom + (h - stext) + 4 * i) for i in range(z // 4)]
            for i, kind, t in mips.jumps(words, h):
                if kind == 'j' and region_of(t):
                    for b in reach(t):
                        if h < b < h + z:
                            owners[b].add((name, s, h, z))
    # hooks entered from a redirected switch case return into the function that owned the case
    for hw, owners_of in data_seed_owner.items():
        for name, sw in owners_of:
            for s, h, z in A.units.get(name, []):
                if s <= sw < s + z:
                    for b in reach(hw):
                        if h < b < h + z:
                            owners[b].add((name, s, h, z))
    starts = {h for units in A.units.values() for s, h, z in units}
    stock_starts = {st.text + o for st in A.secs for o, z in st.funcs}
    for t in sorted(text_jumps):
        if t in starts and t not in owners:
            continue
        if t not in owners and A.map.translate(t) in stock_starts:
            continue        # a tail call to a function the map knows (e.g. inside resized code)
        own = owners.get(t)
        if not own:
            x = Extra('mid', t, None, 0)
            x.problems.append(('mid-unowned', t))
            extras.append(x)
            continue
        for name, s, h, z in sorted(own):
            extras.append(Extra('mid', t, s + (t - h), z - (t - h), section=name))
    A.extras = extras
    return extras


# ------------------------------------------------------------------------------ assembly

def code_spans(extras, kind):
    """Merged [lo, hi) hack spans of the extras of one kind."""
    spans = sorted((x.hack_vram, x.hack_vram + x.size) for x in extras if x.kind == kind and x.size)
    out = []
    for lo, hi in spans:
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [tuple(s) for s in out]


def translate_data(A, D, report):
    """Copy the hack's data changes into the stock data images in D (pointers translated).
    Words equal to stock, or whose translation equals stock, keep the stock value."""
    skip = code_spans(A.extras, 'cave')
    if A.payload:
        cave = A.payload['cave']
        skip.append((cave, cave + 0x100))
    changed = 0
    for name, (hrom, htext, htext_end, hdata, hend) in A.place.items():
        if name == 'entry' or name in getattr(A, 'experiment_no_data', ()):
            continue
        st = A.byname[name]
        ctx = A.ctx_of_hack(name)
        rs = A.map.main if name == 'main' else A.map.ovl[name]
        hack_bss = None
        if name in A.hsecs and A.hsecs[name] is not None:
            hack_bss = A.hsecs[name].bss
        for lo, hi, d, kind in rs:
            if kind != 'data':
                continue
            for y in range(lo, hi, 4):
                x = (y + d) & 0xFFFFFFFF
                if not (st.data <= x < st.bss):
                    continue
                if hack_bss is not None and y >= hack_bss:
                    continue
                if any(a <= y < b for a, b in skip):
                    continue
                hw = word(A.hack, hrom + (y - htext))
                xr = st.rom_of(x)
                sw = word(A.base, xr)
                if hw == sw:
                    continue
                cs = A.map.candidates(hw, ctx) if 0x80000000 <= hw < 0x80800000 or hw < 0x00800000 else []
                if sw in cs:
                    continue
                ram = [c for c in cs if c >= 0x80000000] if hw >= 0x80000000 else []
                put_word(D, xr, ram[0] if ram else hw)
                changed += 1
                report.setdefault('data_changes', []).append((name, x, sw, ram[0] if ram else hw))
    report['data_words_changed'] = changed


def payload_fixups(A, D, report):
    """Rewrite the payload's code (hack -> stock addresses) and translate pointers in its data."""
    p = A.payload
    if not p:
        return
    get = lambda v: word(A.hack, p['rom'] + (v - p['ram']))
    spans = code_spans(A.extras, 'payload')
    cand = lambda a: A.map.candidates(a, None)
    probs = []
    for lo, hi in spans:
        words = [get(v) for v in range(lo, hi, 4)]
        nw = rewrite(words, lo, lo, cand, None, probs)
        for i, w in enumerate(nw):
            put_word(D, p['rom'] + (lo - p['ram']) + 4 * i, w)
    ptrs = 0
    for v in range(p['ram'], p['ram'] + p['size'], 4):
        if any(a <= v < b for a, b in spans):
            continue
        w = get(v)
        if 0x80000400 <= w < 0x80400000 and w & 3 == 0:
            c = A.map.translate(w)
            if c is not None and c != w:
                put_word(D, p['rom'] + (v - p['ram']), c)
                ptrs += 1
    report['payload_code_spans'] = len(spans)
    report['payload_code_problems'] = probs
    report['payload_pointers_translated'] = ptrs


def assemble(A, report):
    """The stock-layout ROM: stock code/data with the hack's changes, the hack's assets and
    payload, and the new-code area appended. Returns (rom bytes, relink manifest)."""
    discover_extra_code(A)
    ports, resized = port_code(A)
    D = bytearray(A.hack)
    code_end = max(s.rom + (s.bss - s.text) for s in A.secs)
    D[0:code_end] = A.base[0:code_end]
    keep = set(getattr(A, 'experiment_stock_ports', ()))
    for p in ports:
        if p.status == 'changed' and p.stock_vram in keep:
            p.status = 'kept'
        if p.status == 'changed':
            r = A.byname[p.section].rom_of(p.stock_vram)
            for i, w in enumerate(p.words):
                put_word(D, r + 4 * i, w)
    report['ports'] = ports
    report['resized'] = resized
    translate_data(A, D, report)
    payload_fixups(A, D, report)
    # new-code area: caves, rewritten at their new addresses
    new_rom = (len(D) + 0xFFFF) & ~0xFFFF
    hrom, htext = A.place['main'][:2]
    area = bytearray()
    cave_probs = []
    for lo, hi in code_spans(A.extras, 'cave'):
        words = [word(A.hack, hrom + (v - htext)) for v in range(lo, hi, 4)]
        nv = A.map.translate(lo)
        nw = rewrite(words, lo, nv, lambda a: A.map.candidates(a, None), None, cave_probs)
        off = nv - NEW_CODE_VRAM
        if len(area) < off + 4 * len(nw):
            area.extend(bytes(off + 4 * len(nw) - len(area)))
        for i, w in enumerate(nw):
            put_word(area, off + 4 * i, w)
    report['cave_problems'] = cave_probs
    functions = []
    copies = 0
    for x in A.extras:
        if x.vram is None or not x.size:
            continue
        if x.kind == 'payload':
            x.rom = A.payload['rom'] + (x.vram - A.payload['ram'])
            floor = A.payload['ram']
        elif x.kind == 'cave':
            x.rom = new_rom + (x.vram - NEW_CODE_VRAM)
            floor = NEW_CODE_VRAM
        else:
            st = A.byname[x.section]
            x.rom = st.rom_of(x.vram)
            floor = max(lo for lo, hi in [(st.text + o, st.text + o + z) for o, z in st.funcs] if lo <= x.vram)
        f = dict(vram=x.vram, rom=x.rom, size=x.size, kind=x.kind)
        if x.kind == 'mid' and A.byname[x.section].slot is not None:
            f['section_rom'] = A.byname[x.section].rom
        src = D if x.kind != 'cave' else None
        get = (lambda v, x=x: word(D, x.rom + (v - x.vram))) if src is not None else               (lambda v: word(area, v - NEW_CODE_VRAM))
        back = []
        for v in range(x.vram, x.vram + x.size, 4):
            w = get(v)
            t = mips.branch_target(w, v)
            if t is None and w >> 26 == 2:
                t = mips.jump_target(w, v)
            if t is not None and floor <= t < x.vram:
                back.append(t)
        if back:
            # loops reach back above the entry: compile a copy of [lowest target, end) at its own
            # addresses, preceded by a branch to the real entry, and register it under the entry
            lo = min(back)
            if x.kind == 'cave':
                get_src = lambda v: word(area, v - NEW_CODE_VRAM)
            elif x.kind == 'payload':
                get_src = lambda v: word(D, A.payload['rom'] + (v - A.payload['ram']))
            else:
                st = A.byname[x.section]
                get_src = lambda v, st=st: word(D, st.rom_of(v))
            end = x.vram + x.size
            off = (len(area) + 15) & ~15
            area.extend(bytes(off - len(area)))
            words = [0x10000000 | (((x.vram - (lo - 4)) >> 2) & 0xFFFF), 0]
            words += [get_src(v) for v in range(lo, end, 4)]
            for w in words:
                area.extend(w.to_bytes(4, 'big'))
            f = dict(f, vram=lo - 8, entry=x.vram, rom=new_rom + off, size=4 * len(words), kind=x.kind + '+copy')
            copies += 1
        if x.kind == 'payload':
            f['data'] = [A.payload['ram'], A.payload['ram'] + A.payload['size'], A.payload['rom']]
        elif x.kind == 'mid':
            st = A.byname[x.section]
            f['data'] = [st.text, st.bss, st.rom]
        functions.append(f)
    report['back_branch_copies'] = copies
    if area:
        D.extend(bytes(new_rom - len(D)))
        D.extend(area)
        D.extend(bytes((-len(D)) % 16))
    code_ranges = []
    if A.payload:
        code_ranges.append([A.payload['ram'], A.payload['ram'] + A.payload['size']])
    if area:
        code_ranges.append([NEW_CODE_VRAM, NEW_CODE_VRAM + len(area)])
    manifest = dict(format=1, base='nomercy-rev1', functions=functions, code_ranges=code_ranges)
    if A.payload:
        p = A.payload
        manifest['preload'] = [dict(rom=p['rom'], ram=p['ram'], size=p['size'])]
        manifest['zero'] = [dict(ram=p['zero_lo'], size=p['zero_hi'] - p['zero_lo'])]
    return bytes(D), manifest


# ----------------------------------------------------------------------------------- CLI

def summarize(A, report):
    ports = report['ports']
    c = collections.Counter((p.section, p.status) for p in ports)
    lines = ['sections: ' + ', '.join('%s %d/%d changed' % (n, c[(n, 'changed')], c[(n, 'changed')] + c[(n, 'moved')])
                                      for n in A.units)]
    lines.append('in-place ports: %d function(s); resized code kept stock: %s' % (
        sum(1 for p in ports if p.status == 'changed'),
        ', '.join('%s %08X-%08X' % (n, a, b) for n, a, b, _, _ in report['resized'] if b > a) or 'none'))
    k = collections.Counter(x.kind for x in A.extras)
    lines.append('extra functions: %d payload, %d cave, %d mid-function return points' % (k['payload'], k['cave'], k['mid']))
    lines.append('data words changed: %d; payload pointers translated: %d' % (report['data_words_changed'], report.get('payload_pointers_translated', 0)))
    probs = [(p.section, p.stock_vram, x) for p in ports if p.status == 'changed' for x in p.problems]
    probs += [('payload', 0, x) for x in report.get('payload_code_problems', [])]
    probs += [('cave', 0, x) for x in report.get('cave_problems', [])]
    probs += [('extra', x.hack_vram, pr) for x in A.extras for pr in x.problems]
    lines.append('unresolved references: %d' % len(probs))
    for sec, v, x in probs[:30]:
        lines.append('  %s %08X %s' % (sec, v, x))
    if A.payload:
        p = A.payload
        lines.append('payload: rom 0x%X -> 0x%08X, 0x%X bytes (preloaded natively)' % (p['rom'], p['ram'], p['size']))
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('analyze', 'build', 'map'):
        p = sub.add_parser(name)
        p.add_argument('--base', required=True, help='stock No Mercy (USA) Rev 1, .z64')
        p.add_argument('--hack', required=True, help='the hack ROM, .z64')
        p.add_argument('--layout', default=LAYOUT_PATH)
        if name == 'build':
            p.add_argument('--id', required=True, help='mod id (also the saves folder)')
            p.add_argument('--name', required=True)
            p.add_argument('--version', default='1.0.0')
            p.add_argument('--author', action='append', default=[])
            p.add_argument('--game-id', default='wwfnomercy')
            p.add_argument('--out', required=True, help='output .nrm')
            p.add_argument('--rom-out', help='also write the stock-layout ROM (local testing only; never share it)')
            p.add_argument('--x-stock-ports', help='(debugging) comma-separated stock vrams to leave unported')
            p.add_argument('--x-no-data', help='(debugging) comma-separated sections whose data changes are skipped')
        if name == 'map':
            p.add_argument('--out', required=True, help='output JSON (hack RAM ranges -> stock shifts)')
    gen = sub.add_parser('gen-layout', help='(developers) regenerate nomercy_layout.json from RecompiledFuncs')
    gen.add_argument('--base', required=True)
    gen.add_argument('--overlays', default='RecompiledFuncs/recomp_overlays.inl')
    gen.add_argument('--out', default=LAYOUT_PATH)
    args = ap.parse_args(argv)

    if args.cmd == 'gen-layout':
        json.dump(gen_layout(args.overlays, open(args.base, 'rb').read()), open(args.out, 'w'))
        print('wrote', args.out)
        return
    base = open(args.base, 'rb').read()
    hack = open(args.hack, 'rb').read()
    for label, rom in (('base', base), ('hack', hack)):
        if rom[:4] != b'\x80\x37\x12\x40':
            sys.exit('%s ROM is not big-endian .z64 (starts %s)' % (label, rom[:4].hex()))
    layout = json.load(open(args.layout))
    import hashlib
    if layout.get('base_sha1') and hashlib.sha1(base).hexdigest() != layout['base_sha1']:
        sys.exit('base ROM is not No Mercy (USA) Rev 1 (SHA1 %s)' % layout['base_sha1'])
    A = Analysis(base, hack, args.layout)
    if getattr(args, 'x_stock_ports', None):
        A.experiment_stock_ports = [int(v, 16) for v in args.x_stock_ports.split(',')]
    if getattr(args, 'x_no_data', None):
        A.experiment_no_data = args.x_no_data.split(',')
    if args.cmd == 'map':
        json.dump(A.map.to_json(), open(args.out, 'w'), indent=1)
        print('wrote', args.out)
        return
    report = {}
    D, manifest = assemble(A, report)
    print(summarize(A, report))
    if args.cmd == 'analyze':
        return
    import bps
    patch = bps.bps_encode(base, D)
    assert bps.bps_apply(base, patch) == D, 'BPS round trip failed'
    mod = {
        'game_id': args.game_id,
        'id': args.id,
        'display_name': args.name,
        'description': '%s (relinked hack, de-relinked onto the stock layout; patches your own ROM when the game starts).' % args.name,
        'short_description': 'ROM hack (patches your own ROM when the game starts).',
        'version': args.version,
        'authors': args.author or ['unknown'],
        'minimum_recomp_version': '0.1.0',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mod.json', json.dumps(mod, indent=4))
        z.writestr('patch.bps', patch)
        z.writestr('relink.json', json.dumps(manifest, indent=1))
    open(args.out, 'wb').write(buf.getvalue())
    if args.rom_out:
        open(args.rom_out, 'wb').write(D)
    print('wrote %s (patch %d bytes, mod %d bytes, %d extra functions)' % (args.out, len(patch), len(buf.getvalue()), len(manifest['functions'])))


if __name__ == '__main__':
    main()
