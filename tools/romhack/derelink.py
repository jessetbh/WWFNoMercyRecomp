"""De-relink an AKI-engine ROM hack back onto the stock layout, as a ROM-patch mod.

Games are described by layout files (layouts/<game>.json: No Mercy, WrestleMania 2000); the
one whose base_sha1 matches --base is used. Hacks that kept the stock layout (WM2000's) need
little more than their changed functions ported and their extra code registered.

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
layouts/ next to this file, so it runs on a player's machine. A layout holds addresses only
(sections, function offsets/sizes, the overlay descriptor table, co-residency, stock code kept
inside data areas) plus the base ROM's SHA1; `gen-layout` writes one from a port's
RecompiledFuncs/recomp_overlays.inl and its ROM.
"""
import argparse, bisect, collections, io, json, os, struct, sys, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mips
from mips import word, put_word

HERE = os.path.dirname(os.path.abspath(__file__))
LAYOUTS_DIR = os.path.join(HERE, 'layouts')
LAYOUT_PATH = os.path.join(LAYOUTS_DIR, 'nomercy.json')


def find_layout(base):
    """The layout file whose base_sha1 matches the base ROM, or None."""
    import glob, hashlib
    sha = hashlib.sha1(base).hexdigest()
    for path in sorted(glob.glob(os.path.join(LAYOUTS_DIR, '*.json'))):
        if json.load(open(path)).get('base_sha1') == sha:
            return path
    return None
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


def find_descriptor_table(rom, count=5, lo=0x1000, hi=0x400000, hint=None):
    """Offset of the overlay descriptor table (9 words each: rom start/end, entry, text start/end,
    data start/end, bss start/end). `hint`: where the stock table is (hacks rarely move it)."""
    def ok(w):
        return (0x1000 <= w[0] < w[1] <= len(rom) and all(0x80000000 <= x < 0x80800000 for x in w[2:9])
                and w[3] <= w[4] <= w[5] <= w[6] <= w[7] <= w[8] and w[1] - w[0] == w[6] - w[3])
    def table_at(off):
        return off + 0x24 * count <= len(rom) and all(ok(struct.unpack_from('>9I', rom, off + 0x24 * i)) for i in range(count))
    if hint is not None and table_at(hint):
        return hint
    for off in range(lo, min(hi, len(rom) - 0x24 * count), 4):
        if table_at(off):
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


ORDER = ['ovl_a', 'ovl_b', 'ovl_c', 'ovl_e', 'ovl_d']   # No Mercy's descriptor order (old layouts)


def gen_layout(overlays_inl, base_rom, game, mod_game_id, manifest_base):
    """Stock layout from recomp_overlays.inl + the stock ROM's descriptor table and entry.
    Overlay sections are matched to descriptors by ROM start. Other recompiled sections (code a
    segment keeps after its data, e.g. WM2000's) are listed as extra stock code ranges."""
    import hashlib
    from bps import load_sections
    secs = load_sections(overlays_inl)
    ovls = [x for x in secs if '_ovl_' in x[0]]
    table = find_descriptor_table(base_rom, count=len(ovls))
    assert table is not None, 'no overlay descriptor table'
    descs = read_descriptors(base_rom, table, len(ovls))
    bss_start, bss_size = entry_bss(base_rom)
    out, extra = [], []
    for name, rom, ram, size, funcs in secs:
        f = [(off, sz) for _, off, sz in funcs]
        if name == 'section_0_entry_funcs':
            out.append(Section('entry', rom, ram, ram + size, ram + size, ram + size, f))
        elif name.startswith('section_1_main'):
            # main: text, then data up to the bss the entry clears.
            out.append(Section('main', rom, ram, ram + size, bss_start, bss_start + bss_size, f))
        elif '_ovl_' in name:
            ovl = name.split('_', 2)[2].rsplit('_funcs', 1)[0]           # ovl_e
            k = next(i for i, d in enumerate(descs) if d[0] == rom)
            d = descs[k]
            assert d[3] == ram, (name, hex(ram), hex(d[3]))
            sec = Section(ovl, d[0], d[3], d[5], d[7], d[8], f, slot=ram).to_json()
            sec['desc_index'] = k
            out.append(sec)
        else:
            owner = None            # the segment whose ROM range holds this code
            for d in descs:
                if d[0] <= rom < d[1]:
                    owner = next(o[0].split('_', 2)[2].rsplit('_funcs', 1)[0] for o in ovls if o[1] == d[0])
            extra.append(dict(section=owner or 'main', ram=ram, rom=rom, size=size, funcs=f))
    out = [x if isinstance(x, dict) else x.to_json() for x in out]
    # overlays that can be resident together: their RAM ranges don't overlap
    ranges = {x['name']: (x['text'], x['end']) for x in out if x.get('slot') is not None}
    partners = {o: [tuple(sorted(q for q in ranges if q != o and (ranges[q][1] <= lo or hi <= ranges[q][0])))]
                for o, (lo, hi) in ranges.items()}
    partners = {o: [g for g in v if g] for o, v in partners.items()}
    slots = collections.defaultdict(list)
    for x in out:
        if x.get('slot') is not None:
            slots[x['slot']].append(x['name'])
    groups = [tuple(sorted(v)) for k, v in sorted(slots.items(), reverse=True)] + [(o,) for o in sorted(ranges)]
    return dict(game=game, mod_game_id=mod_game_id, manifest_base=manifest_base,
                base_sha1=hashlib.sha1(base_rom).hexdigest(), descriptor_table=table,
                descriptor_count=len(ovls), partners=partners, default_groups=groups,
                sections=out, extra_code=extra)


def load_layout(path=LAYOUT_PATH):
    d = json.load(open(path))
    return d, [Section.from_json(s) for s in d['sections']]


def hack_sections(hack, stock_layout, stock_secs):
    """The hack's section placement: overlays from its descriptor table; main text start and
    rom are assumed unchanged (AKI's boot loads main from rom 0x1000 to 0x80000400)."""
    count = stock_layout.get('descriptor_count', 5)
    table = find_descriptor_table(hack, count=count, hint=stock_layout.get('descriptor_table'))
    if table is None:
        raise SystemExit('hack: no overlay descriptor table found')
    descs = read_descriptors(hack, table, count)
    index = {x['name']: x['desc_index'] for x in stock_layout['sections'] if 'desc_index' in x}
    out = {}
    for s in stock_secs:
        if s.slot is None:
            out[s.name] = None      # filled in by the matcher (main shifts piecewise)
            continue
        d = descs[index[s.name] if s.name in index else ORDER.index(s.name)]
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
        moved = self._find(self.priority, a)
        if moved:
            return [moved[0]]       # code or buffers moved out of the stock layout: no alternative
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

    DEFAULT_GROUPS = [('ovl_b', 'ovl_c', 'ovl_d'), ('ovl_a',), ('ovl_e',), ('ovl_a', 'ovl_e')]

    def partners(self, ctx):
        table = getattr(self, 'partners_of', None) or self.PARTNERS
        if ctx in table:
            return table[ctx]
        return getattr(self, 'default_groups', None) or self.DEFAULT_GROUPS

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
        if lo <= h < hi and (s - h) & 3 == 0:     # sections move by whole words
            per[h][(s - h) & 0xFFFFFFFF] += 1
    pts = sorted((h, c.most_common(1)[0][0]) for h, c in per.items())
    runs = []                     # [first_h, delta, count]
    for h, d in pts:
        if runs and runs[-1][1] == d:
            runs[-1][2] += 1
        else:
            runs.append([h, d, 1])
    # weakly supported runs (a hack edit pointing an instruction elsewhere) between runs that
    # agree are dropped, smallest first, until the neighbours of every short run differ
    changed = True
    while changed:
        changed = False
        for need in (1, 2):
            for i in range(1, len(runs) - 1):
                if runs[i][2] <= need:
                    # look past a stretch of short runs for agreeing neighbours
                    j = i
                    while j + 1 < len(runs) - 1 and runs[j + 1][2] <= need:
                        j += 1
                    if runs[i - 1][1] == runs[j + 1][1]:
                        runs[i - 1][2] += runs[j + 1][2]
                        del runs[i:j + 2]
                        changed = True
                        break
            if changed:
                break
    out = []
    for i, (h, d, n) in enumerate(runs):
        start = lo if i == 0 else (h & ~3)
        end = (runs[i + 1][0] & ~3) if i + 1 < len(runs) else hi
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
        self.reshaped = []       # (section, stock lo, stock hi, hack lo, hack hi) rebuilt by the hack
        for name, m in self.matches.items():
            u = list(m.matched)
            st = self.byname[name]
            hrom_, htext_ = self.place[name][:2]
            for slo, shi, hlo, hhi in m.chunks:
                if shi - slo == hhi - hlo:
                    if shi > slo:
                        n = (shi - slo) // 4
                        same = sum(mips.masked(word(base, st.rom_of(slo) + 4 * i)) ==
                                   mips.masked(word(hack, hrom_ + (hlo - htext_) + 4 * i)) for i in range(n))
                        if name != 'main' and n >= 16 and same < n // 2:
                            # the hack rebuilt this stretch (new functions, tables inside code):
                            # it moves whole to the new-code area instead of being ported in place
                            self.reshaped.append((name, slo, shi, hlo, hhi))
                        else:
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
        self.move_main_rewrites()

    def move_main_rewrites(self):
        """Main's resized stretch (the Rev 0 flash-save code) moves whole to the new-code area
        instead of staying stock Rev 1: hacks edit the code around it (V3 enlarges the save
        records), so only the hack's own version is consistent. Its Rev 0-sized buffers (main bss
        pieces that don't line up with Rev 1) get their own block past the payload."""
        zones = keep_stock_zones(self, 'main')
        m = self.matches['main']
        for zlo, zhi in zones:
            if zhi - zlo < 0x100:
                continue            # a small libultra difference: stays stock
            before = [(s, h, z) for s, h, z in m.matched if s + z <= zlo]
            after = [(s, h, z) for s, h, z in m.matched if s >= zhi]
            if not before or not after:
                continue
            hlo = zlo + (before[-1][1] - before[-1][0])
            hhi = zhi + (after[0][1] - after[0][0])
            self.reshaped.append(('main', zlo, zhi, hlo, hhi))
            self.units['main'] = [u for u in self.units['main'] if not (zlo <= u[0] < zhi)]
            self.resized = [r for r in self.resized if not (r[0] == 'main' and zlo <= r[1] < zhi)]
        # the save buffers: main data runs between the first and last shift
        runs = [(lo, hi, d) for lo, hi, d, k in self.map.main if k == 'data']
        self.save_area = None
        if len(runs) > 2 and any(r[0] == 'main' for r in self.reshaped):
            lo, hi = runs[1][0], runs[-1][0]
            pay = find_payload(self.hack)
            base = ((pay['ram'] + pay['size']) + 0xFFFF) & ~0xFFFF if pay else 0x80600000
            self.save_area = (lo, hi, base)
            self.map.priority.append((lo, hi, (base - lo) & 0xFFFFFFFF, 'save'))
            self.map.priority.sort()

    def hack_rom_of(self, name, vram):
        hrom, htext = self.place[name][:2]
        return hrom + (vram - htext)

    def build_map(self):
        amap = AddressMap()
        if 'partners' in self.layout:
            amap.partners_of = {k: [tuple(g) for g in v] for k, v in self.layout['partners'].items()}
            amap.default_groups = [tuple(g) for g in self.layout.get('default_groups', [])]
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
        stock_top = max(s.end for s in self.secs)
        allp = [p for ps in pairs.values() for p in ps]
        for lo, hi, d in runs_from_pairs(allp, top, 0x80400000):
            if any(top <= h < 0x80400000 for _, h in allp):
                if (lo + d) & 0xFFFFFFFF < stock_top:
                    # a hack that moved its heap elsewhere reuses the old arena as plain memory and
                    # repoints variables there; no heap shift, the area is free in the stock layout too
                    amap.main.append((lo, hi, 0, 'heap'))
                else:
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
            a, b = (last_ev[i] + 4) & ~3, out[i + 1][0] & ~3
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


def rewrite(words, hack_pc, new_pc, candidates, stock_words=None, problems=None, init=None):
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
    external = []                               # %lo uses whose lui is in the code that jumped here
    for hi, lo, a in mips.hilo_refs(words, init=init):
        if hi is None:
            external.append((lo, a))
        else:
            groups[hi].append((lo, a))
    for lo, a in external:
        cs = [c for c in cands(a, ram_only=False) if c >= 0x80000000] if a >= 0x80000000 else []
        if not cs:
            continue
        hi_val = (a - mips.sx16(words[lo] & 0xFFFF)) & 0xFFFF0000     # the caller's lui stays as it is
        new_lo = (cs[0] - hi_val) & 0xFFFFFFFF
        if words[lo] >> 26 == 0x0D:
            ok = new_lo < 0x10000
        else:
            ok = new_lo < 0x8000 or new_lo >= 0xFFFF8000
        if ok:
            out[lo] = (words[lo] & 0xFFFF0000) | (new_lo & 0xFFFF)
        elif problems is not None:
            problems.append((lo, 'external-hi', a))

    def halves(lo_word, na):
        if lo_word >> 26 == 0x0D:                    # ori: unsigned low half
            return na >> 16, na & 0xFFFF
        return ((na + 0x8000) >> 16) & 0xFFFF, na & 0xFFFF

    for hi, uses in groups.items():
        idx = [hi] + [lo for lo, _ in uses]
        if same(idx):
            continue
        new = []
        all_cands = {}
        for lo, a in uses:
            cs = cands(a, ram_only=False)
            all_cands[lo] = cs
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
            # one lui feeds uses that picked different overlays: use one shift that suits them all
            uses_a = dict(uses)
            shift_sets = [{(c - uses_a[lo]) & 0xFFFFFFFF for c in all_cands[lo] if c >= 0x80000000}
                          for lo, _ in new if uses_a[lo] >= 0x80000000]
            common = set.intersection(*shift_sets) if shift_sets else set()
            if common:
                firsts = collections.Counter(((all_cands[lo][0] - uses_a[lo]) & 0xFFFFFFFF) for lo, _ in new
                                             if all_cands[lo])
                shift = max(common, key=lambda d: firsts.get(d, 0))
                new = [(lo, (uses_a[lo] + shift) & 0xFFFFFFFF) if uses_a[lo] >= 0x80000000 else (lo, na)
                       for lo, na in new]
                his = {halves(words[lo], na)[0] for lo, na in new}
        if len(his) != 1:
            # keep the majority; leave the odd uses as they were and report them
            count = collections.Counter(halves(words[lo], na)[0] for lo, na in new)
            major = count.most_common(1)[0][0]
            odd = [(lo, na) for lo, na in new if halves(words[lo], na)[0] != major]
            if problems is not None:
                problems.append((hi, 'split-hi', [hex(na) for _, na in odd]))
            new = [(lo, na) for lo, na in new if halves(words[lo], na)[0] == major]
            his = {major}
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
                # %lo uses after a hook's return point that pair with the hook's lui
                for t, init in getattr(A, 'return_init', {}).items():
                    if h < t < h + z:
                        k = (t - h) // 4
                        ext = {lo for hi, lo, a in mips.hilo_refs(hw[k:], init=init) if hi is None}
                        if ext:
                            nw2 = rewrite(hw[k:], t, s + 4 * k, tr, sw[k:], probs, init=init)
                            for i in ext:
                                nw[k + i] = nw2[i]
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
    and fallthrough, stops after jr / j (j targets are separate entries). A branch into the delay
    slot of a j (hand-written hack code does that) traces on from the slot. Returns
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
                    break
            if op == 2:
                jt.add(mips.jump_target(w, pc))
                break
            if op == 3:
                jalt.add(mips.jump_target(w, pc))
            if op == 0 and (w & 0x3F) == 0x08:          # jr
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
    """Find the hack's code outside the stock text: payload functions, main-data caves, chunks
    the hack reshaped (moved whole to the new-code area) and mid-function return points.
    Fills A.payload, A.extras, A.region_entries, A.new_blocks and the map's priority ranges."""
    A.payload = find_payload(A.hack)
    main = A.byname['main']
    hrom, htext, htext_end = A.place['main'][:3]
    data_shift = A.map.translate(htext_end) - htext_end           # main data: one shift
    hack_main_data = (htext_end, main.bss - data_shift)
    # regions: (name, lo, hi, get_word, section or None); a reshaped chunk only receives jumps
    # from its own section's code (overlays share addresses)
    regions = []
    if A.payload:
        p = A.payload
        regions.append(('payload', p['ram'], p['ram'] + p['size'], lambda v, p=p: word(A.hack, p['rom'] + (v - p['ram'])), None))
    regions.append(('cave', hack_main_data[0], hack_main_data[1], lambda v: word(A.hack, hrom + (v - htext)), None))
    for name, slo, shi, hlo, hhi in A.reshaped:
        regions.append(('reshaped', hlo, hhi, lambda v, name=name: word(A.hack, A.hack_rom_of(name, v)), name))
    skip = {A.payload['cave']} if A.payload else set()

    stock_code = [(x['ram'], x['ram'] + x['size']) for x in A.layout.get('extra_code', [])]

    def region_of(a, src=None):
        for r in regions:
            if r[1] <= a < r[2] and (r[4] in (None, 'main') or r[4] == src):
                if r[0] == 'cave' and any(lo <= a < hi for lo, hi in stock_code):
                    return None     # a stock function the recompiler already has
                return r
        return None

    work = []                                   # (address, source section or None)
    for name, (srom, stext, stext_end, _, _) in A.place.items():
        words = [word(A.hack, srom + 4 * i) for i in range((stext_end - stext) // 4)]
        for i, kind, t in mips.jumps(words, stext):
            if region_of(t, name) and t not in skip:
                work.append((t, name))
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
                if hw & 3 == 0 and region_of(hw, name) and hw not in skip:
                    sw = word(A.base, st.rom_of(x))
                    r = region_of(hw, name)
                    if any(a <= sw < b for a, b in stock_text) and                             mips.plausible_entry([r[3](hw + 4 * k) for k in range(6)]):
                        work.append((hw, name))
                        data_seed_owner.setdefault(hw, set()).add((name, sw))
    entries = {}                                # address -> (region, end, back)
    text_jumps = set()

    def run(work):
        while work:
            e, src = work.pop()
            if e in entries or e in skip:
                continue
            r = region_of(e, src)
            if r is None:
                continue
            end, jt, jalt, back = trace_extent(r[3], e, r[1], r[2])
            entries[e] = (r, end, back)
            inner = r[4]                        # code in a reshaped chunk keeps its section
            for t in jt | jalt:
                if region_of(t, inner):
                    work.append((t, inner))
                elif t not in skip:
                    text_jumps.add((t, inner))
    run(work)

    # code reached from region code by j (not jal) chains, for hook-return ownership
    reach_from = {}
    def reach(e0, src):
        key = (e0, src)
        if key in reach_from:
            return reach_from[key]
        seen, out, todo = set(), set(), [(e0, src)]
        while todo:
            e, s = todo.pop()
            if e in seen:
                continue
            seen.add(e)
            r = region_of(e, s)
            if r is None:
                out.add(e)
                continue
            end, jt, jalt, back = trace_extent(r[3], e, r[1], r[2])
            for t in jt:
                todo.append((t, r[4]))
        reach_from[key] = out
        return out

    # hooks entered from a reshaped chunk return into it: those points are entries of the chunk
    for _ in range(3):
        more = []
        for r in regions:
            if r[0] != 'reshaped':
                continue
            words = [r[3](v) for v in range(r[1], r[2], 4)]
            for i, kind, t in mips.jumps(words, r[1]):
                if kind == 'j' and region_of(t) and not (r[1] <= t < r[2]):
                    for b in reach(t, None):
                        if r[1] <= b < r[2] and b not in entries:
                            more.append((b, r[4]))
        if not more:
            break
        run(more)
    A.region_entries = entries
    A.text_jumps = {t for t, _ in text_jumps}

    # the new-code area: caves (one span) and each reshaped chunk, whole, in hack layout
    A.new_blocks = []                           # (kind, section, hack lo, hack hi, new vram)
    cur = NEW_CODE_VRAM
    caves = sorted((e, end) for e, (r, end, _) in entries.items() if r[0] == 'cave')
    if caves:
        lo, hi = caves[0][0], max(end for _, end in caves)
        A.new_blocks.append(('cave', None, lo, hi, cur))
        cur = (cur + (hi - lo) + 15) & ~15
    for name, slo, shi, hlo, hhi in A.reshaped:
        A.new_blocks.append(('reshaped', name, hlo, hhi, cur))
        cur = (cur + (hhi - hlo) + 15) & ~15
    A.map.priority = sorted([r for r in A.map.priority if r[3] == 'save'] +
                            [(lo, hi, (nv - lo) & 0xFFFFFFFF, k) for k, sec, lo, hi, nv in A.new_blocks
                             if k == 'cave' or sec == 'main'])
    for k, sec, lo, hi, nv in A.new_blocks:
        if k == 'reshaped' and sec != 'main':
            A.map.ovl[sec].append((lo, hi, (nv - lo) & 0xFFFFFFFF, 'reshaped'))
    A.map.finish()
    for name in A.map.ovl:      # a reshaped range replaces the text ranges it overlaps
        rs = A.map.ovl[name]
        resh = [(lo, hi) for lo, hi, d, k in rs if k == 'reshaped']
        A.map.ovl[name] = [r for r in rs if r[3] == 'reshaped' or not any(lo <= r[0] < hi for lo, hi in resh)]

    def new_vram(e, r):
        for k, sec, lo, hi, nv in A.new_blocks:
            if lo <= e < hi and (sec is None or sec == r[4]):
                return nv + (e - lo)
        return None

    extras = []
    for e, (r, end, back) in sorted(entries.items(), key=lambda kv: kv[0]):
        if r[0] == 'payload':
            v = e
        else:
            v = new_vram(e, r)
        x = Extra(r[0], e, v, end - e, section=r[4])
        extras.append(x)

    # which overlay's code enters each payload/cave entry (directly or through other entries):
    # that overlay is the context for translating its addresses
    region_ctx = collections.defaultdict(set)
    def enter(e0, sec):
        todo, seen = [e0], set()
        while todo:
            e = todo.pop()
            if e in seen or e not in entries:
                continue
            seen.add(e)
            region_ctx[e].add(sec)
            r = entries[e][0]
            end, jt, jalt, back = trace_extent(r[3], e, r[1], r[2])
            todo += [t for t in jt | jalt if t in entries]
    for name, units in A.units.items():
        srom, stext = A.place[name][:2]
        for s, h, z in units:
            words = [word(A.hack, srom + (h - stext) + 4 * i) for i in range(z // 4)]
            for i, kind, t in mips.jumps(words, h):
                if t in entries and entries[t][0][0] in ('payload', 'cave'):
                    enter(t, name)
    for hw, owners_of in data_seed_owner.items():
        for name, sw in owners_of:
            if hw in entries:
                enter(hw, name)
    A.region_ctx = {e: next(iter(v)) for e, v in region_ctx.items() if len(v) == 1 and next(iter(v)) != 'main'}
    # registers holding a lui value when a function jumps into a hook: the hack moved the
    # displaced instructions there, so a %lo in the hook can pair with a lui left behind
    inits = {}
    for name, units in A.units.items():
        srom, stext = A.place[name][:2]
        for s, h, z in units:
            words = [word(A.hack, srom + (h - stext) + 4 * i) for i in range(z // 4)]
            for i, kind, t in mips.jumps(words, h):
                if kind == 'j' and t in entries and entries[t][0][0] in ('payload', 'cave'):
                    st = mips.hilo_state_at(words, i)
                    inits[t] = st if t not in inits else {r: v for r, v in inits[t].items() if st.get(r) == v}
    A.entry_init = {e: st for e, st in inits.items() if st}
    # ...and the other way round: registers a hook leaves holding a lui value when it jumps back
    # into the function (the hack moved a lui there; the %lo stayed behind)
    rinits = {}
    for e, (r, end, back) in entries.items():
        words = [r[3](v) for v in range(e, end, 4)]
        for i, kind, t in mips.jumps(words, e):
            if kind == 'j' and not region_of(t, r[4]):
                st = mips.hilo_state_at(words, i)
                rinits[t] = st if t not in rinits else {k: v for k, v in rinits[t].items() if st.get(k) == v}
    A.return_init = {t: st for t, st in rinits.items() if st}

    # mid-function return points: a hook entered by a `j` from function U returns into U
    owners = collections.defaultdict(set)          # hack text address -> {(section, s, h, z)}
    for name, units in A.units.items():
        srom, stext = A.place[name][:2]
        for s, h, z in units:
            words = [word(A.hack, srom + (h - stext) + 4 * i) for i in range(z // 4)]
            for i, kind, t in mips.jumps(words, h):
                if kind == 'j' and region_of(t, name) and not region_of(t, name)[0] == 'reshaped':
                    for b in reach(t, name):
                        if h < b < h + z:
                            owners[b].add((name, s, h, z))
    # hooks entered from a redirected switch case return into the function that owned the case
    for hw, owners_of in data_seed_owner.items():
        for name, sw in owners_of:
            for s, h, z in A.units.get(name, []):
                if s <= sw < s + z:
                    for b in reach(hw, name):
                        if h < b < h + z:
                            owners[b].add((name, s, h, z))
    starts = {h for units in A.units.values() for s, h, z in units}
    stock_starts = {st.text + o for st in A.secs for o, z in st.funcs}
    resh_ranges = [(lo, hi) for k, sec, lo, hi, nv in A.new_blocks if k == 'reshaped']
    for t in sorted(A.text_jumps):
        if t in entries:
            continue
        if t in starts and t not in owners:
            continue
        if t not in owners and A.map.translate(t) in stock_starts:
            continue        # a tail call to a function the map knows (e.g. inside resized code)
        own = owners.get(t)
        if not own:
            if any(lo <= t < hi for lo, hi in resh_ranges):
                continue    # into a reshaped chunk from code that can't say which overlay: no entry
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
    skip = [(lo, hi) for k, sec, lo, hi, nv in A.new_blocks if k == 'cave']
    if A.payload:
        cave = A.payload['cave']
        skip.append((cave, cave + 0x100))
    changed = 0
    for name, (hrom, htext, htext_end, hdata, hend) in A.place.items():
        if name == 'entry' or name in getattr(A, 'experiment_no_data', ()):
            continue
        skip_ranges = [tuple(int(v, 16) for v in e.split(':')[1].split('-'))
                       for e in getattr(A, 'experiment_no_data', ()) if e.startswith(name + ':')]
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
                if hw == sw or any(a <= x < b for a, b in skip_ranges):
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
    probs = []
    ctx_of = getattr(A, 'region_ctx', {})
    for lo, hi in spans:
        # the overlay whose code enters this stretch, when only one does
        secs = {ctx_of[e] for e in ctx_of if lo <= e < hi}
        ctx = secs.pop() if len(secs) == 1 else None
        cand = lambda a, ctx=ctx: A.map.candidates(a, ctx)
        words = [get(v) for v in range(lo, hi, 4)]
        nw = rewrite(words, lo, lo, cand, None, probs)
        for i, w in enumerate(nw):
            put_word(D, p['rom'] + (lo - p['ram']) + 4 * i, w)
    # hook entries: %lo uses paired with the jumping function's lui
    for x in A.extras:
        init = getattr(A, 'entry_init', {}).get(x.hack_vram) if x.kind == 'payload' else None
        if not init:
            continue
        ctx = getattr(A, 'region_ctx', {}).get(x.hack_vram)
        words = [get(v) for v in range(x.hack_vram, x.hack_vram + x.size, 4)]
        ext = {lo for hi, lo, a in mips.hilo_refs(words, init=init) if hi is None}
        nw = rewrite(words, x.hack_vram, x.hack_vram, lambda a, ctx=ctx: A.map.candidates(a, ctx), None, probs, init=init)
        for i in ext:
            put_word(D, p['rom'] + (x.hack_vram - p['ram']) + 4 * i, nw[i])
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


def patch_unsupported_instructions(D, functions, report):
    """The live recompiler only reads COP0 Status. Hacks read COP0 Random (\$1) as a cheap random
    number: replace `mfc0 rt, $Random` with `mflo rt` (LO, the last multiply's leftover, varies
    with game state too). [(vram, rom, size)]"""
    n = 0
    for vram, rom, size in functions:
        for i in range(size // 4):
            w = word(D, rom + 4 * i)
            if w >> 21 == (0x10 << 5) | 0x00 and ((w >> 11) & 31) == 1 and (w & 0x7FF) == 0:
                put_word(D, rom + 4 * i, (((w >> 16) & 31) << 11) | 0x12)
                n += 1
    report['cop0_random_replaced'] = n


def overlay_switch_tables(A, D, x):
    """[(section, table vram)] for jump tables in an overlay's data that payload function x
    dispatches through (`lw rX, %lo(table)(...)` ... `jr rX`, the hack taking over a switch)."""
    sec = getattr(A, 'region_ctx', {}).get(x.hack_vram)
    if x.kind != 'payload' or sec is None or sec == 'main':
        return []
    st = A.byname[sec]
    words = [word(D, x.rom + 4 * i) for i in range(x.size // 4)]
    jr_regs = {(w >> 21) & 31 for w in words if w >> 26 == 0 and (w & 0x3F) == 0x08 and (w >> 21) & 31 != 31}
    out = []
    for hi, lo, a in mips.hilo_refs(words):
        w = words[lo]
        if w >> 26 == 0x23 and (w >> 16) & 31 in jr_regs and st.data <= a < st.bss:
            out.append((sec, a))
    return out


def add_switch_case_entries(A, D):
    """Cases of overlay switch tables that payload code dispatches through are mid-function
    points of stock overlay code: register them so the lookup tail calls find them."""
    have = {(x.section, x.vram) for x in A.extras if x.kind == 'mid'}
    for x in list(A.extras):
        if x.kind != 'payload' or x.rom is None:
            continue
        for sec, tv in overlay_switch_tables(A, D, x):
            st = A.byname[sec]
            x.data_section = sec
            v = tv
            while v < st.bss:
                t = word(D, st.rom_of(v))
                if not (st.text <= t < st.data) or t & 3:
                    break
                inside = [(st.text + o, z) for o, z in st.funcs if st.text + o < t < st.text + o + z]
                if inside and (sec, t) not in have:
                    fs, fz = min(inside, key=lambda f: f[1])
                    A.extras.append(Extra('mid', None, t, fs + fz - t, section=sec))
                    have.add((sec, t))
                v += 4


def section_funcs(A, sec):
    """(offset, size) of every stock function of a section, including code it keeps inside its
    data area (layout extra_code)."""
    st = A.byname[sec]
    out = list(st.funcs)
    for x in A.layout.get('extra_code', []):
        if x['section'] == sec:
            out += [(x['ram'] - st.text + o, z) for o, z in x['funcs']]
    return out


def _ends_flow(w):
    """j, jr or an unconditional b: control never reaches the next instruction (after the slot)."""
    op = w >> 26
    return op == 2 or (op == 0 and (w & 0x3F) == 0x08) or (op == 0x04 and (w >> 16) & 0x3FF == 0)


def add_fall_through_entries(A, D, ports):
    """A changed function whose code now runs off its end (the hack wrote over the stock exit and
    continues into padding or the next function) is followed there by the runtime's lookup tail
    call: register an entry at that address when it isn't a function start, reaching through the
    next function."""
    have = {(x.section, x.vram) for x in A.extras if x.kind == 'mid'}
    # the runtime regenerates stock functions one by one: check each changed one
    bodies = []
    for sec in A.byname:
        if sec == 'entry':
            continue
        st = A.byname[sec]
        for o, z in section_funcs(A, sec):
            r = st.rom_of(st.text + o)
            if D[r:r + z] != A.base[r:r + z]:
                bodies.append((sec, st.text + o, z))
    for sec, vram, size in bodies:
        st = A.byname[sec]
        if size < 8 or not all(mips.plausible(word(D, st.rom_of(vram + 4 * i))) for i in range(size // 4)):
            continue        # data the recompiler keeps as a function (WM2000's ovl_a tail chunk)
        prev, last = word(D, st.rom_of(vram + size - 8)), word(D, st.rom_of(vram + size - 4))
        if _ends_flow(prev) or _ends_flow(last) or last == 0x42000018:
            continue
        t = vram + size
        funcs = sorted((st.text + o, z) for o, z in section_funcs(A, sec))
        if any(a == t for a, z in funcs) or (sec, t) in have:
            continue
        nxt = [(a, z) for a, z in funcs if a > t]
        inside = [(a, z) for a, z in funcs if a < t < a + z]
        end = max([a + z for a, z in inside] + ([nxt[0][0] + nxt[0][1]] if nxt else []), default=None)
        if end is None or end <= t:
            continue
        A.extras.append(Extra('mid', None, t, end - t, section=sec))
        have.add((sec, t))


def add_branch_out_entries(A, D, ports, rounds=4):
    """Branches and `j` tail calls that leave a function (IDO shares tails between functions)
    become lookup tail calls in the live recompiler, so their targets must be registered: add each
    target as an entry point of the same section's function containing it (a function start there
    may belong to a co-resident overlay only)."""
    have = {(x.section, x.vram) for x in A.extras if x.kind == 'mid'}
    bodies = [(p.section, p.stock_vram, p.size) for p in ports if p.status == 'changed']
    bodies += [(x.section, x.vram, x.size) for x in A.extras if x.kind == 'mid' and x.vram]
    # changed functions in code kept inside data areas: the runtime regenerates those itself
    for x in A.layout.get('extra_code', []):
        st = A.byname[x['section']]
        for o, z in x['funcs']:
            r = st.rom_of(x['ram'] + o)
            if D[r:r + z] != A.base[r:r + z]:
                bodies.append((x['section'], x['ram'] + o, z))
    funcs = {sec: section_funcs(A, sec) for sec in A.byname}
    for _ in range(rounds):
        new = []
        for sec, vram, size in bodies:
            st = A.byname[sec]
            if not all(mips.plausible(word(D, st.rom_of(vram + 4 * i))) for i in range(size // 4)):
                continue    # not code: its "branches" are data
            for i in range(size // 4):
                v = vram + 4 * i
                w = word(D, st.rom_of(v))
                t = mips.branch_target(w, v)
                if t is None and w >> 26 == 2:
                    t = mips.jump_target(w, v)
                if t is None or vram <= t < vram + size or (sec, t) in have:
                    continue
                inside = [(st.text + o, z) for o, z in funcs[sec] if st.text + o < t < st.text + o + z]
                if not inside or any(st.text + o == t for o, z in funcs[sec]):
                    continue        # a real function start is registered already
                fs, fz = min(inside, key=lambda f: f[1])
                x = Extra('mid', None, t, fs + fz - t, section=sec)
                A.extras.append(x)
                have.add((sec, t))
                new.append((sec, t, fs + fz - t))
        if not new:
            break
        bodies = new


REFRESH_OPS = {0x0F, 0x09, 0x0C, 0x0D, 0x0A, 0x0B} | mips.LOAD_GPR | {0x28, 0x29, 0x2B}
REFRESH_SPECIAL = {0x00, 0x02, 0x03, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x2A, 0x2B}
REFRESH_TEMPS = {1, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25}


def pure_hook_body(words):
    """The body of a hook that only derives values from memory: straight-line loads, arithmetic
    and stores into caller-saved temporaries, ending in the `j` back. Returns (indices of the body
    words, incl. the j's delay slot, input addresses, output addresses) or None."""
    for i, w in enumerate(words):
        op = w >> 26
        if op == 2:
            idx = list(range(i)) + ([i + 1] if i + 1 < len(words) and words[i + 1] else [])
            break
        if op == 0:
            if (w & 0x3F) not in REFRESH_SPECIAL:
                return None
        elif op not in REFRESH_OPS:
            return None
        r = mips.gpr_written(w)
        if r is not None and r not in REFRESH_TEMPS:
            return None
    else:
        return None
    body = [words[i] for i in idx]
    refs = mips.hilo_refs(body)
    ins = {a for hi, lo, a in refs if body[lo] >> 26 in mips.LOAD_GPR}
    outs = {a for hi, lo, a in refs if body[lo] >> 26 in mips.STORE_OPS}
    if not ins or not outs:
        return None
    return idx, ins, outs


def add_refresh_stubs(A, D, area, report, lookahead=12):
    """A hook the game runs every retrace can keep values derived from other memory (V3/BTBW: a
    pointer = *(save struct pointer) + 0x10000). On hardware the retrace interrupt preempts the game
    thread, so the value is fresh soon after its input changes; natively nothing preempts, and code
    that sets the input and reads the derived value in one go reads a stale one. For each pure
    hook, the call that follows each store to one of its inputs goes through a stub in the new-code
    area that runs the hook's body first and then tail-calls the original target (at a call, the
    temporaries the body uses are free)."""
    hooks = []
    for e, (r, end, back) in A.region_entries.items():
        if r[0] not in ('cave', 'payload'):
            continue
        hw = [r[3](v) for v in range(e, end, 4)]
        got = pure_hook_body(hw)
        if not got:
            continue
        idx, ins, outs = got
        # the already-translated body: caves sit in the new-code area, payload code in D
        x = next((x for x in A.extras if x.hack_vram == e and x.vram), None)
        if x is None:
            continue
        if r[0] == 'cave':
            tw = [word(area, x.vram - NEW_CODE_VRAM + 4 * i) for i in range(len(hw))]
        else:
            tw = [word(D, A.payload['rom'] + (e - A.payload['ram']) + 4 * i) for i in range(len(hw))]
        hooks.append((e, [tw[i] for i in idx], ins, outs))
    sites = []
    for name, units in A.units.items():
        st = A.byname[name]
        for s, h, z in units:
            hw = [word(A.hack, A.hack_rom_of(name, h) + 4 * i) for i in range(z // 4)]
            for hi, lo, a in mips.hilo_refs(hw):
                if hw[lo] >> 26 not in mips.STORE_OPS:
                    continue
                hook = next((k for k in hooks if a in k[2]), None)
                if hook is None:
                    continue
                if lo and (mips.branch_target(hw[lo - 1], 0) is not None or hw[lo - 1] >> 26 in (2, 3)):
                    continue                    # a store in a delay slot: leave it
                for k in range(lo + 1, min(lo + 1 + lookahead, len(hw))):
                    w = hw[k]
                    if w >> 26 == 3:
                        sites.append((name, s + 4 * k, hook, a))
                        break
                    if mips.branch_target(w, 0) is not None or w >> 26 == 2 or (w >> 26 == 0 and (w & 0x3F) in (0x08, 0x09)):
                        report.setdefault('refresh_unhandled', []).append((name, h + 4 * lo, a))
                        break
    stubs = {}
    for name, v, (e, tbody, ins, outs), a in sites:
        st = A.byname[name]
        r = st.rom_of(v)
        jal = word(D, r)
        target = mips.jump_target(jal, v)
        key = (e, target)
        if key not in stubs:
            off = (len(area) + 15) & ~15
            area.extend(bytes(off - len(area)))
            sv = NEW_CODE_VRAM + off
            for w in tbody + [mips.with_jump_target(0x08000000, target), 0]:
                area.extend(w.to_bytes(4, 'big'))
            x = Extra('refresh', None, sv, 4 * (len(tbody) + 2))
            A.extras.append(x)
            stubs[key] = sv
        put_word(D, r, mips.with_jump_target(jal, stubs[key]))
        report.setdefault('refresh_sites', []).append((name, v, a, stubs[key]))
    report['refresh_hooks'] = [(e, sorted(ins), sorted(outs)) for e, _, ins, outs in hooks]


def _jumpish(w):
    return mips.branch_target(w, 0) is not None or w >> 26 in (2, 3) or \
        (w >> 26 == 0 and (w & 0x3F) in (0x08, 0x09))


def stub_over(A, D, area, P, first, words_at, targets):
    """Replace payload instruction P (and the one after it, which would sit in the j's delay slot)
    by a jump to a new-code-area stub: `first` (instructions standing in for P), the instruction
    after P, then back to P + 8 (registered as a payload entry). None if P can't be moved."""
    p = A.payload
    w_prev, w_next = words_at(P - 4), words_at(P + 4)
    if _jumpish(w_prev) or _jumpish(w_next) or (P + 4) in targets:
        return None
    owner = next((x for x in A.extras if x.kind == 'payload' and x.vram and x.vram <= P < x.vram + x.size), None)
    if owner is None or P + 8 >= owner.vram + owner.size:
        return None
    off = (len(area) + 15) & ~15
    area.extend(bytes(off - len(area)))
    sv = NEW_CODE_VRAM + off
    code = list(first) + [w_next, mips.with_jump_target(0x08000000, P + 8), 0]
    for c in code:
        area.extend(c.to_bytes(4, 'big'))
    rom = p['rom'] + (P - p['ram'])
    put_word(D, rom, mips.with_jump_target(0x08000000, sv))
    put_word(D, rom + 4, 0)
    A.extras.append(Extra('refresh', None, sv, 4 * len(code)))
    A.extras.append(Extra('payload', P + 8, P + 8, owner.vram + owner.size - (P + 8)))
    return sv


def ram_variable(A, area, new_rom, init_word):
    """A word of free Expansion RAM (after the payload and the moved save buffers) that the runtime
    preloads with init_word (stored in the new-code area). Returns its address."""
    if not hasattr(A, 'ram_vars'):
        end = A.payload['ram'] + A.payload['size'] if A.payload else 0x80400000
        if getattr(A, 'save_area', None):
            lo, hi, base = A.save_area
            end = max(end, base + (hi - lo))
        A.ram_vars = [((end + 0xFFF) & ~0xFFF) + 0x1000, []]
    off = (len(area) + 3) & ~3
    area.extend(bytes(off - len(area)))
    area.extend(init_word.to_bytes(4, 'big'))
    v = A.ram_vars[0] + 4 * len(A.ram_vars[1])
    A.ram_vars[1].append(dict(rom=new_rom + off, ram=v, size=4))
    return v


def fix_get_pc(A, D, area, report, new_rom):
    """Hack code that takes its own address (`jal` to a bare `jr ra`, then `addiu rX, ra, imm`) gets
    nothing natively (the recompiler doesn't keep ra): compute the address absolutely. When the
    address is then used to patch the immediate of an `ori/addiu rY, zero, imm` in payload code
    (a variable kept in an instruction), that instruction reads the halfword from RAM instead."""
    p = A.payload
    if not p:
        return
    pw = lambda v: word(D, p['rom'] + (v - p['ram']))
    helpers = set()
    for x in A.extras:
        if x.kind == 'payload' and x.vram:
            ws = [pw(x.vram + 4 * i) for i in range(min(x.size // 4, 4))]
            k = next((i for i, w in enumerate(ws) if w), None)
            if k is not None and ws[k] == 0x03E00008:
                helpers.add(x.vram)
    if not helpers:
        return
    done = []
    for lo, hi in code_spans(A.extras, 'payload'):
        nw = [pw(v) for v in range(lo, hi, 4)]
        targets = {mips.branch_target(w, lo + 4 * i) for i, w in enumerate(nw)} - {None}
        for i, w in enumerate(nw):
            J = lo + 4 * i
            if w >> 26 != 3 or mips.jump_target(w, J) not in helpers or i + 1 >= len(nw) or nw[i + 1]:
                continue
            for k in range(i + 2, min(i + 9, len(nw))):
                x = nw[k]
                if x >> 26 == 0x09 and (x >> 21) & 31 == 31 and (x >> 16) & 31 not in (0, 31):
                    rt = (x >> 16) & 31
                    addr = (J + 8 + mips.sx16(x & 0xFFFF)) & 0xFFFFFFFF
                    put_word(D, p['rom'] + (J + 4 - p['ram']), (0x0F << 26) | (rt << 16) | (((addr + 0x8000) >> 16) & 0xFFFF))
                    put_word(D, p['rom'] + (lo + 4 * k - p['ram']), (0x09 << 26) | (rt << 21) | (rt << 16) | (addr & 0xFFFF))
                    done.append((J, addr))
                    # a halfword store through it into an instruction's immediate
                    for m in range(k + 1, min(k + 5, len(nw))):
                        y = nw[m]
                        if y >> 26 == 0x29 and (y >> 21) & 31 == rt:
                            tgt = (addr + mips.sx16(y & 0xFFFF)) & 0xFFFFFFFF
                            I = tgt & ~3
                            iw = pw(I) if p['ram'] <= I < p['ram'] + p['size'] else None
                            if iw is not None and tgt == I + 2 and iw >> 26 in (0x0D, 0x09) and (iw >> 21) & 31 == 0:
                                # the instruction's word lives in a RAM variable instead (its RAM copy
                                # becomes the jump to the stub); the store and the read go there
                                var = ram_variable(A, area, new_rom, iw)
                                vt = var + 2
                                ry = (iw >> 16) & 31
                                load = 0x25 if iw >> 26 == 0x0D else 0x21      # ori: lhu, addiu: lh
                                first = [(0x0F << 26) | (ry << 16) | (((vt + 0x8000) >> 16) & 0xFFFF),
                                         (load << 26) | (ry << 21) | (ry << 16) | (vt & 0xFFFF)]
                                if stub_over(A, D, area, I, first, pw, targets) is not None:
                                    base_addr = (vt - mips.sx16(y & 0xFFFF)) & 0xFFFFFFFF
                                    put_word(D, p['rom'] + (J + 4 - p['ram']), (0x0F << 26) | (rt << 16) | (((base_addr + 0x8000) >> 16) & 0xFFFF))
                                    put_word(D, p['rom'] + (lo + 4 * k - p['ram']), (0x09 << 26) | (rt << 21) | (rt << 16) | (base_addr & 0xFFFF))
                                    done.append((I, var))
                                else:
                                    report.setdefault('get_pc_left', []).append(I)
                    break
    report['get_pc_fixed'] = done


def fix_unreachable_lo(A, D, area, report):
    """One lui can feed %lo uses whose translations need different %hi values (the hack's
    variables straddle a 64 KB boundary after the shift). In payload code, a load into a GPR or an
    addiu the chosen %hi can't reach goes through a stub: `lui rt, %hi(A)` + the instruction based
    on rt, then the instruction after it (moved out of the j's delay slot), then back."""
    p = A.payload
    if not p:
        return
    fixed = []
    ctx_of = getattr(A, 'region_ctx', {})
    for lo, hi in code_spans(A.extras, 'payload'):
        secs = {ctx_of[e] for e in ctx_of if lo <= e < hi}
        ctx = secs.pop() if len(secs) == 1 else None
        rom = p['rom'] + (lo - p['ram'])
        hw = [word(A.hack, rom + i) for i in range(0, hi - lo, 4)]
        nw = [word(D, rom + i) for i in range(0, hi - lo, 4)]
        got = {l: a for h, l, a in mips.hilo_refs(nw)}
        targets = {mips.branch_target(w, lo + 4 * i) for i, w in enumerate(nw)} - {None}
        for h, l, a in mips.hilo_refs(hw):
            cs = A.map.candidates(a, ctx)
            if len(cs) != 1 or got.get(l) == cs[0] or cs[0] == a and got.get(l) == a:
                continue
            want, w = cs[0], nw[l]
            op, rt = w >> 26, (w >> 16) & 31
            if not ((op in mips.LOAD_GPR or op == 0x09) and rt) or l == 0 or l + 2 > len(nw):
                continue
            P = lo + 4 * l
            first = [(0x0F << 26) | (rt << 16) | (((want + 0x8000) >> 16) & 0xFFFF),
                     (w & 0xFC000000) | (rt << 21) | (rt << 16) | (want & 0xFFFF)]
            pw = lambda v: word(D, p['rom'] + (v - p['ram']))
            if stub_over(A, D, area, P, first, pw, targets) is None:
                report.setdefault('unreachable_lo_left', []).append(P)
                continue
            fixed.append((P, a, want))
    report['unreachable_lo_fixed'] = fixed


def add_entry_logs(A, D, area, report):
    """(debugging) For each 'section:vram' in A.experiment_log_entries: the function's first word
    becomes a jump to a stub that records (a0, a1) of calls with a0 >= 0x10000 in a 256-entry ring at 0x807FF108 (write offset
    at 0x807FF100, above No Mercy Plus's Expansion-RAM heap), runs the second word and jumps back."""
    for spec in getattr(A, 'experiment_log_entries', ()):
        if spec.startswith('jsite:'):
            # the j/jal at sec:vram goes through a stub that records (site vram, s0, s1) in the 0x804B0000
            # ring (12-byte entries) and then jumps to the original target
            _, sec, v = spec.split(':')
            v = int(v, 16)
            r = A.byname[sec].rom_of(v)
            w = word(D, r)
            target = mips.jump_target(w, v)
            off = (len(area) + 15) & ~15
            area.extend(bytes(off - len(area)))
            sv = NEW_CODE_VRAM + off
            code = [0x3C18804B, 0x8F190000, 0x2739000C, 0x3339FFFF, 0xAF190000, 0x0338C821,
                    0x3C010000 | (v >> 16), 0x34210000 | (v & 0xFFFF), 0xAF210008,   # site vram (not v0)
                    0xAF30000C, 0xAF310010, mips.with_jump_target(0x08000000, target), 0]
            for c in code:
                area.extend(c.to_bytes(4, 'big'))
            put_word(D, r, mips.with_jump_target(w, sv))
            A.extras.append(Extra('refresh', None, sv, 4 * len(code)))
            continue
        sec, v, *flt = spec.split(':')
        v = int(v, 16)
        reg = 5 if flt == ['a1'] else 4     # the argument compared with the minimum
        st = A.byname[sec]
        r = st.rom_of(v)
        fsz = next(z for o, z in st.funcs if st.text + o == v)
        w0, w1 = word(D, r), word(D, r + 4)
        off = (len(area) + 15) & ~15
        area.extend(bytes(off - len(area)))
        sv = NEW_CODE_VRAM + off
        if flt == ['heap']:
            # every call: (a0, size of the heap head block at 0x80500000, where No Mercy Plus V3 and
            # BTBW put it) into an 8192-entry ring at
            # 0x804B0008 (write offset at 0x804B0000; free Expansion RAM below the hack's heap)
            code = [0x3C18804B, 0x8F190000, 0x27390008, 0x3339FFF8, 0xAF190000, 0x0338C821,
                    0xAF240008, 0x3C018050, 0x8C210004, 0xAF21000C,
                    w1, mips.with_jump_target(0x08000000, v + 8), 0]
        else:
            # calls with a0 >= the minimum only: (a0, a1) into the ring
            code = [0x3C190000 | (getattr(A, 'experiment_log_min', 0) >> 16), (reg << 21) | 0x0019C82B, 0x17200008, 0x3C188080,
                    0x8F19F100, 0x27390008, 0x333907F8, 0xAF19F100, 0x0338C821,
                    0xAF24F108, 0xAF25F10C, w1, mips.with_jump_target(0x08000000, v + 8), 0]
        for w in code:
            area.extend(w.to_bytes(4, 'big'))
        put_word(D, r, mips.with_jump_target(0x08000000, sv))
        put_word(D, r + 4, w0)
        A.extras.append(Extra('refresh', None, sv, 4 * len(code)))
        A.extras.append(Extra('mid', None, v + 8, fsz - 8, section=sec))
        report.setdefault('entry_logs', []).append((sec, v, sv))


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
    # new-code area: caves and reshaped chunks, copied whole, their code rewritten in place
    new_rom = (len(D) + 0xFFFF) & ~0xFFFF
    area = bytearray()
    cave_probs = []
    for kind, sec, lo, hi, nv in A.new_blocks:
        get = (lambda v, sec=sec: word(A.hack, A.hack_rom_of(sec, v))) if sec else               (lambda v: word(A.hack, A.place['main'][0] + (v - A.place['main'][1])))
        off = nv - NEW_CODE_VRAM
        if len(area) < off + (hi - lo):
            area.extend(bytes(off + (hi - lo) - len(area)))
        raw = [get(v) for v in range(lo, hi, 4)]
        for i, w in enumerate(raw):
            put_word(area, off + 4 * i, w)
        spans = [(a_, b_) for a_, b_ in code_spans([x for x in A.extras if x.section == sec], kind)
                 if lo <= a_ < hi]
        cand = lambda a, sec=sec: A.map.candidates(a, sec)
        for a_, b_ in spans:
            words = raw[(a_ - lo) // 4:(b_ - lo) // 4]
            nw = rewrite(words, a_, nv + (a_ - lo), cand, None, cave_probs)
            for i, w in enumerate(nw):
                put_word(area, off + (a_ - lo) + 4 * i, w)
        if kind == 'reshaped':
            # words outside the code are tables: translate pointers into code the hack moved
            for i, w in enumerate(raw):
                v = lo + 4 * i
                if lo <= w < hi and w & 3 == 0:
                    # a case address inside the chunk (tables sit between code the trace covers)
                    put_word(area, off + 4 * i, nv + (w - lo))
                    continue
                if any(a_ <= v < b_ for a_, b_ in spans):
                    continue
                if 0x80000400 <= w < 0x80400000 and w & 3 == 0:
                    c = A.map.translate(w, sec)
                    if c is not None:
                        put_word(area, off + 4 * i, c)
    report['cave_problems'] = cave_probs
    add_refresh_stubs(A, D, area, report)
    fix_unreachable_lo(A, D, area, report)
    fix_get_pc(A, D, area, report, new_rom)
    add_entry_logs(A, D, area, report)
    for x in A.extras:
        if x.kind == 'payload' and x.vram:
            x.rom = A.payload['rom'] + (x.vram - A.payload['ram'])
    add_switch_case_entries(A, D)
    add_fall_through_entries(A, D, ports)
    add_branch_out_entries(A, D, ports)
    functions = []
    copies = 0
    for x in A.extras:
        if x.vram is None or not x.size:
            continue
        if x.kind == 'payload':
            x.rom = A.payload['rom'] + (x.vram - A.payload['ram'])
            floor = A.payload['ram']
        elif x.kind in ('cave', 'reshaped', 'refresh'):
            x.rom = new_rom + (x.vram - NEW_CODE_VRAM)
            floor = max(nv for k, sec, lo, hi, nv in A.new_blocks if nv <= x.vram)
        else:
            st = A.byname[x.section]
            x.rom = st.rom_of(x.vram)
            floor = max(lo for lo, hi in [(st.text + o, st.text + o + z) for o, z in section_funcs(A, x.section)] if lo <= x.vram)
        f = dict(vram=x.vram, rom=x.rom, size=x.size, kind=x.kind)
        if x.kind == 'mid' and A.byname[x.section].slot is not None:
            f['section_rom'] = A.byname[x.section].rom
        src = D if x.kind not in ('cave', 'reshaped', 'refresh') else None
        get = (lambda v, x=x: word(D, x.rom + (v - x.vram))) if src is not None else               (lambda v: word(area, v - NEW_CODE_VRAM))
        back = []
        for v in range(x.vram, x.vram + x.size, 4):
            w = get(v)
            t = mips.branch_target(w, v)     # loops branch; a backward j is a tail call
            if t is not None and floor <= t < x.vram:
                back.append(t)
        if back:
            # loops reach back above the entry: compile a copy of [lowest target, end) at its own
            # addresses, preceded by a branch to the real entry, and register it under the entry
            lo = min(back)
            if x.kind in ('cave', 'reshaped'):
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
        if x.kind == 'payload' and getattr(x, 'data_section', None):
            # dispatches through an overlay's switch table: that overlay's data for jump tables
            # (the payload's own tables still resolve through the manifest's data_ranges)
            st = A.byname[x.data_section]
            f['data'] = [st.text, st.bss, st.rom]
        elif x.kind == 'payload':
            f['data'] = [A.payload['ram'], A.payload['ram'] + A.payload['size'], A.payload['rom']]
        elif x.kind == 'mid':
            st = A.byname[x.section]
            f['data'] = [st.text, st.bss, st.rom]
        if f['size'] <= 0 or f['size'] & 3 or f['vram'] & 3 or f['rom'] + f['size'] > len(D) + len(area) + 0x10000:
            report.setdefault('bad_functions', []).append(f)
            continue
        functions.append(f)
    report['back_branch_copies'] = copies
    # (debugging) raw word patches at stock addresses: section:vram=word
    for spec in getattr(A, 'experiment_word_patches', ()):
        sec, rest = spec.split(':')
        v, w = (int(x, 16) for x in rest.split('='))
        put_word(D, A.byname[sec].rom_of(v), w)
    if area:
        D.extend(bytes(new_rom - len(D)))
        D.extend(area)
        D.extend(bytes((-len(D)) % 16))
    # COP0 Random reads are supported by the runtime (feat/fullconv); patch_unsupported_instructions()
    # stays for runtimes without it (its mflo stand-in can spin in retry loops).
    report['cop0_random_replaced'] = 0
    code_ranges = []
    if A.payload:
        code_ranges.append([A.payload['ram'], A.payload['ram'] + A.payload['size']])
    if area:
        code_ranges.append([NEW_CODE_VRAM, NEW_CODE_VRAM + len(area)])
    data_ranges = []
    if A.payload:
        data_ranges.append([A.payload['ram'], A.payload['ram'] + A.payload['size'], A.payload['rom']])
    manifest = dict(format=1, base=A.layout.get('manifest_base', 'nomercy-rev1'), functions=functions, code_ranges=code_ranges,
                    data_ranges=data_ranges)
    if A.payload:
        p = A.payload
        manifest['preload'] = [dict(rom=p['rom'], ram=p['ram'], size=p['size'])]
        manifest['preload'] += getattr(A, 'ram_vars', [0, []])[1]
        manifest['zero'] = [dict(ram=p['zero_lo'], size=p['zero_hi'] - p['zero_lo'])]
    if getattr(A, 'save_area', None):
        lo, hi, base = A.save_area
        manifest.setdefault('zero', []).append(dict(ram=base, size=(hi - lo + 0xF) & ~0xF))
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
    lines.append('extra functions: %d payload, %d cave, %d reshaped, %d mid-function return points' % (k['payload'], k['cave'], k['reshaped'], k['mid']))
    lines.append('data words changed: %d; payload pointers translated: %d' % (report['data_words_changed'], report.get('payload_pointers_translated', 0)))
    if report.get('unreachable_lo_fixed') or report.get('unreachable_lo_left'):
        lines.append('%%lo uses no %%hi reaches: %d moved to stubs, %d left' % (
            len(report.get('unreachable_lo_fixed', [])), len(report.get('unreachable_lo_left', []))))
    if report.get('get_pc_fixed') or report.get('get_pc_left'):
        lines.append('own-address (get-PC) uses fixed: %d, left: %d' % (
            len(report.get('get_pc_fixed', [])), len(report.get('get_pc_left', []))))
    if report.get('refresh_hooks'):
        lines.append('hook-derived values refreshed: %d hook(s), %d call site(s), %d store(s) with no call after' % (
            len(report['refresh_hooks']), len(report.get('refresh_sites', [])), len(report.get('refresh_unhandled', []))))
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
        p.add_argument('--base', required=True, help='the stock ROM the game port uses, .z64')
        p.add_argument('--hack', required=True, help='the hack ROM, .z64')
        p.add_argument('--layout', help='layout file (default: the one in layouts/ for the base ROM)')
        if name == 'build':
            p.add_argument('--id', required=True, help='mod id (also the saves folder)')
            p.add_argument('--name', required=True)
            p.add_argument('--version', default='1.0.0')
            p.add_argument('--author', action='append', default=[])
            p.add_argument('--game-id', help="the port's mod game id (default: from the layout)")
            p.add_argument('--out', required=True, help='output .nrm')
            p.add_argument('--rom-out', help='also write the stock-layout ROM (local testing only; never share it)')
            p.add_argument('--x-stock-ports', help='(debugging) comma-separated stock vrams to leave unported')
            p.add_argument('--x-no-data', help='(debugging) comma-separated sections whose data changes are skipped')
            p.add_argument('--x-word-patch', help='(debugging) section:vram=word,... raw words written at stock addresses')
            p.add_argument('--x-log-entry', help='(debugging) section:vram[:a1],... log (a0, a1) of calls with a0 (or a1) >= 0x10000 in a RAM ring')
        if name == 'map':
            p.add_argument('--out', required=True, help='output JSON (hack RAM ranges -> stock shifts)')
    gen = sub.add_parser('gen-layout', help='(developers) write a game layout from its RecompiledFuncs')
    gen.add_argument('--base', required=True)
    gen.add_argument('--overlays', default='RecompiledFuncs/recomp_overlays.inl')
    gen.add_argument('--game', required=True, help='layout name, e.g. nomercy, wm2k')
    gen.add_argument('--mod-game-id', required=True, help="the port's mod game id, e.g. wwfnomercy")
    gen.add_argument('--manifest-base', required=True, help='base name in relink.json, e.g. nomercy-rev1')
    gen.add_argument('--out')
    args = ap.parse_args(argv)

    if args.cmd == 'gen-layout':
        out = args.out or os.path.join(LAYOUTS_DIR, args.game + '.json')
        json.dump(gen_layout(args.overlays, open(args.base, 'rb').read(), args.game, args.mod_game_id,
                             args.manifest_base), open(out, 'w'))
        print('wrote', out)
        return
    base = open(args.base, 'rb').read()
    hack = open(args.hack, 'rb').read()
    for label, rom in (('base', base), ('hack', hack)):
        if rom[:4] != b'\x80\x37\x12\x40':
            sys.exit('%s ROM is not big-endian .z64 (starts %s)' % (label, rom[:4].hex()))
    layout_path = args.layout or find_layout(base)
    if layout_path is None:
        sys.exit('base ROM matches no layout in %s (it must be the exact ROM the game port uses)' % LAYOUTS_DIR)
    layout = json.load(open(layout_path))
    import hashlib
    if layout.get('base_sha1') and hashlib.sha1(base).hexdigest() != layout['base_sha1']:
        sys.exit('base ROM is not the one %s expects (SHA1 %s)' % (os.path.basename(layout_path), layout['base_sha1']))
    if getattr(args, 'game_id', None) is None and args.cmd == 'build':
        args.game_id = layout.get('mod_game_id', 'wwfnomercy')
    A = Analysis(base, hack, layout_path)
    if getattr(args, 'x_stock_ports', None):
        A.experiment_stock_ports = [int(v, 16) for v in args.x_stock_ports.split(',')]
    if getattr(args, 'x_word_patch', None):
        A.experiment_word_patches = args.x_word_patch.split(',')
    if getattr(args, 'x_log_entry', None):
        A.experiment_log_entries = args.x_log_entry.split(',')
        A.experiment_log_min = 0x10000
    if getattr(args, 'x_no_data', None):
        A.experiment_no_data = args.x_no_data.split(',')
    if args.cmd == 'map':
        json.dump(A.map.to_json(), open(args.out, 'w'), indent=1)
        print('wrote', args.out)
        return
    report = {}
    D, manifest = assemble(A, report)
    print(summarize(A, report))
    funcs = [(f['vram'], f['rom'], f['size'], f.get('data')) for f in manifest['functions']]
    funcs += [(p.stock_vram, A.byname[p.section].rom_of(p.stock_vram), p.size) for p in report['ports'] if p.status == 'changed']
    bad = check_jump_tables(D, funcs, manifest['code_ranges'], manifest.get('data_ranges', []))
    for vram, pc, table, why in bad:
        print('  JUMP TABLE: function 0x%08X, jr at 0x%08X, table 0x%08X: %s' % (vram, pc, table, why))
    if bad:
        print('WARNING: %d jump table(s) the runtime would reject (the mod would fail to load)' % len(bad))
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



# ------------------------------------------------------------------------- offline checks

def check_jump_tables(D, functions, code_ranges, data_ranges):
    """Find jump tables the runtime's live recompiler would reject: (vram, jr pc, table, reason).
    functions: [(vram, rom, size)]. Mirrors N64Recomp's analysis: a `lw reg, %lo(T)` feeding
    `jr reg`; entries are read until one falls outside the function (or the hack code ranges)."""
    bad = []
    for f in functions:
        vram, rom, size = f[:3]
        own = [tuple(f[3])] if len(f) > 3 and f[3] else []
        words = [word(D, rom + 4 * i) for i in range(size // 4)]
        loads = {}
        for hi, lo, a in mips.hilo_refs(words):
            if words[lo] >> 26 == 0x23:
                loads[lo] = a
        for i, w in enumerate(words):
            if w >> 26 != 0 or (w & 0x3F) != 0x08 or ((w >> 21) & 31) == 31:
                continue
            reg = (w >> 21) & 31
            table = None
            for j in range(i - 1, max(-1, i - 12), -1):
                if j in loads and ((words[j] >> 16) & 31) == reg:
                    table = loads[j]
                    break
            if table is None:
                continue                # an indirect tail call, not a table
            trom = None
            for lo, hi, r in own + list(data_ranges):
                if lo <= table < hi:
                    trom = r + (table - lo)
            if trom is None:
                trom = rom + (table - vram)
            if not (0 <= trom < len(D) - 4):
                bad.append((vram, vram + 4 * i, table, 'table outside the ROM'))
                continue
            first = word(D, trom)
            inside = vram <= first < vram + size or any(lo <= first < hi for lo, hi in code_ranges)
            if not inside:
                bad.append((vram, vram + 4 * i, table, 'first entry 0x%08X outside the function' % first))
    return bad


if __name__ == '__main__':
    main()
