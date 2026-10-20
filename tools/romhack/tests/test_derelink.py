"""Tests for the de-relink tool on synthetic MIPS snippets (no ROMs needed).

    python -m unittest discover -s tools/romhack/tests -v
"""
import os, struct, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import bps
import derelink as d
import mips


# --- tiny assembler --------------------------------------------------------------------------
def lui(rt, imm): return (0x0F << 26) | (rt << 16) | (imm & 0xFFFF)
def addiu(rt, rs, imm): return (0x09 << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)
def ori(rt, rs, imm): return (0x0D << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)
def lw(rt, imm, rs): return (0x23 << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)
def sw(rt, imm, rs): return (0x2B << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)
def addu(rd, rs, rt): return (rs << 21) | (rt << 16) | (rd << 11) | 0x21
def jal(t): return (3 << 26) | ((t >> 2) & 0x3FFFFFF)
def j(t): return (2 << 26) | ((t >> 2) & 0x3FFFFFF)
def jr(rs): return (rs << 21) | 0x08
def beq(rs, rt, off): return (0x04 << 26) | (rs << 21) | (rt << 16) | (off & 0xFFFF)
NOP = 0
AT, V0, V1, A0, A1, T0, S0, RA = 1, 2, 3, 4, 5, 8, 16, 31


def hi_lo(addr):
    return ((addr + 0x8000) >> 16) & 0xFFFF, addr & 0xFFFF


class HiLoTests(unittest.TestCase):
    def test_simple_pair(self):
        h, l = hi_lo(0x80123456)
        refs = mips.hilo_refs([lui(A0, h), addiu(A0, A0, l)])
        self.assertEqual(refs, [(0, 1, 0x80123456)])

    def test_negative_lo(self):
        h, l = hi_lo(0x8012F000)
        self.assertEqual(mips.hilo_refs([lui(AT, h), lw(V0, l, AT)])[0][2], 0x8012F000)

    def test_one_lui_many_uses(self):
        h, _ = hi_lo(0x80050000)
        refs = mips.hilo_refs([lui(S0, h), lw(V0, 0x10, S0), sw(V1, 0x20, S0)])
        self.assertEqual([a for _, _, a in refs], [0x80050010, 0x80050020])

    def test_addu_index_keeps_hi(self):
        h, l = hi_lo(0x80140000)
        refs = mips.hilo_refs([lui(AT, h), addu(AT, AT, V0), lw(V0, l, AT)])
        self.assertEqual(refs, [(0, 2, 0x80140000)])

    def test_jal_delay_slot_still_sees_hi(self):
        # lui a0; jal f; addiu a0, a0, lo  (the argument is formed in the delay slot)
        h, l = hi_lo(0x80057188)
        refs = mips.hilo_refs([lui(A0, h), jal(0x80000DB0), addiu(A0, A0, l)])
        self.assertEqual(refs, [(0, 2, 0x80057188)])

    def test_call_clobbers_after_delay_slot(self):
        h, l = hi_lo(0x80057188)
        refs = mips.hilo_refs([lui(A0, h), jal(0x80000DB0), NOP, addiu(A0, A0, l)])
        self.assertEqual(refs, [])

    def test_overwrite_drops_hi(self):
        h, l = hi_lo(0x80057188)
        self.assertEqual(mips.hilo_refs([lui(A0, h), addiu(A0, 0, 5), addiu(A1, A0, l)]), [])

    def test_increment_after_base_use_is_not_a_lo(self):
        # lui v1, 0x8017; loop: sh a1, 0(v1); addiu v1, v1, 2 (a fill loop, not %hi/%lo)
        sh = (0x29 << 26) | (V1 << 21) | (A1 << 16)
        refs = mips.hilo_refs([lui(V1, 0x8017), sh, addiu(V1, V1, 2)])
        self.assertEqual([(lo, a) for hi, lo, a in refs], [(1, 0x80170000)])
        # ...while the classic in-place pair still is one
        self.assertEqual([a for hi, lo, a in mips.hilo_refs([lui(V1, 0x8017), addiu(V1, V1, 0x10)])], [0x80170010])

    def test_ori_is_unsigned(self):
        refs = mips.hilo_refs([lui(T0, 0x8040), ori(T0, T0, 0x9000)])
        self.assertEqual(refs[0][2], 0x80409000)


def shift_map(ranges):
    """candidates() for a piecewise map: [(lo, hi, delta)]."""
    def cands(a):
        for lo, hi, dl in ranges:
            if lo <= a < hi:
                return [(a + dl) & 0xFFFFFFFF]
        return []
    return cands


class RewriteTests(unittest.TestCase):
    def test_moved_function_becomes_stock(self):
        # stock references 0x80050000 and calls 0x80001000; the hack moved data +0xA0, code +0x90
        hh, hl = hi_lo(0x800500A0)
        sh, sl = hi_lo(0x80050000)
        hack = [lui(AT, hh), lw(V0, hl, AT), jal(0x80001090), NOP, jr(RA), NOP]
        stock = [lui(AT, sh), lw(V0, sl, AT), jal(0x80001000), NOP, jr(RA), NOP]
        cands = shift_map([(0x80040000, 0x80060000, -0xA0), (0x80000400, 0x80040000, -0x90)])
        probs = []
        out = d.rewrite(hack, 0x80002090, 0x80002000, cands, stock, probs)
        self.assertEqual(out, stock)
        self.assertEqual(probs, [])

    def test_real_change_is_kept(self):
        hh, hl = hi_lo(0x800500A0)
        hack = [lui(AT, hh), lw(V0, hl, AT), addiu(V0, V0, 0x60), jr(RA), NOP]   # 0x39 -> 0x60 style edit
        stock = [lui(AT, hh), lw(V0, hl - 0xA0, AT), addiu(V0, V0, 0x39), jr(RA), NOP]
        out = d.rewrite(hack, 0x80002000, 0x80002000, shift_map([(0x80040000, 0x80060000, -0xA0)]), stock, [])
        self.assertEqual(out[2], addiu(V0, V0, 0x60))
        self.assertEqual(out[1], stock[1])

    def test_rewrite_crosses_64k_boundary(self):
        # hack 0x80060010 -> stock 0x8005FF70: the %hi half changes too
        hh, hl = hi_lo(0x80060010)
        out = d.rewrite([lui(AT, hh), lw(V0, hl, AT)], 0x80001000, 0x80001000,
                        shift_map([(0x80040000, 0x80070000, -0xA0)]), None, [])
        self.assertEqual(mips.hilo_refs(out)[0][2], 0x8005FF70)

    def test_jump_inside_function_follows_placement(self):
        hack = [beq(0, 0, 1), NOP, j(0x80003008), NOP]
        out = d.rewrite(hack, 0x80003000, 0x80007000, shift_map([]), None, [])
        self.assertEqual(mips.jump_target(out[2], 0x80007008), 0x80007008)

    def test_unresolved_is_reported(self):
        probs = []
        d.rewrite([jal(0x80300000), NOP], 0x80001000, 0x80001000, shift_map([]), None, probs)
        self.assertEqual(probs, [(0, 'jal', 0x80300000)])

    def test_candidate_matching_stock_wins(self):
        # two plausible overlays (-0x1500 / -0x1530): the one that reproduces the stock word wins
        def cands(a):
            return [(a - 0x1500) & 0xFFFFFFFF, (a - 0x1530) & 0xFFFFFFFF]
        hack = [jal(0x8012F308), NOP]
        stock = [jal(0x8012F308 - 0x1530), NOP]
        self.assertEqual(d.rewrite(hack, 0x80001000, 0x80001000, cands, stock, []), stock)

    def test_lo_after_return_point_pairs_with_the_hooks_lui(self):
        # the hook left at = 0x8015xxxx (+index); back in the function: lw v0, 0x34FC(at); jr v0
        words = [lw(V0, 0x34FC, AT), jr(V0), NOP]
        init = {AT: 0x80150000}
        cand = lambda a: [a - 0x1520] if 0x80150000 <= a < 0x80160000 else []
        out = d.rewrite(words, 0x800DC478, 0x800DAF48, cand, [lw(V0, 0x1FDC, AT), jr(V0), NOP], [], init=init)
        self.assertEqual(out[0], lw(V0, 0x1FDC, AT))

    def test_split_hi_takes_common_shift(self):
        # one lui feeds two uses whose first choices would need different %hi halves; a shift
        # valid for both (-0x1500) is used instead
        def cands(a):
            if a == 0x80148000:
                return [(a - 0x12000) & 0xFFFFFFFF, (a - 0x1500) & 0xFFFFFFFF]
            return [(a - 0x1500) & 0xFFFFFFFF]
        hack = [lui(S0, 0x8015), lw(V0, -0x8000, S0), lw(V1, -0x7FF0, S0)]   # 0x80148000, 0x80148010
        probs = []
        out = d.rewrite(hack, 0x80001000, 0x80001000, cands, None, probs)
        self.assertEqual(probs, [])
        self.assertEqual([a for _, _, a in mips.hilo_refs(out)], [0x80146B00, 0x80146B10])


class MapTests(unittest.TestCase):
    def test_runs_from_pairs_and_stray(self):
        pairs = [(0x80040000 + 4 * i, 0x800400A0 + 4 * i) for i in range(10)]
        pairs.append((0x80040100, 0x800401A0 + 0x30))                  # one stray
        pairs += [(0x80040200 + 4 * i, 0x800402A0 + 4 * i) for i in range(10)]
        pairs += [(0x80050000 + 4 * i, 0x80051530 + 4 * i) for i in range(5)]
        runs = d.runs_from_pairs(pairs, 0x80040000, 0x80060000)
        self.assertEqual([r[2] for r in runs], [(-0xA0) & 0xFFFFFFFF, (-0x1530) & 0xFFFFFFFF])
        self.assertEqual(runs[0][0], 0x80040000)
        self.assertEqual(runs[-1][1], 0x80060000)

    def test_address_map_overlay_context(self):
        m = d.AddressMap()
        m.main = [(0x80000400, 0x80040000, (-0x90) & 0xFFFFFFFF, 'text')]
        m.ovl = {'ovl_b': [(0x80107C60, 0x80120000, (-0x1500) & 0xFFFFFFFF, 'text')],
                 'ovl_e': [(0x800DAE90, 0x8016CF10, (-0x1530) & 0xFFFFFFFF, 'text')]}
        m.finish()
        self.assertEqual(m.translate(0x80001090), 0x80001000)
        self.assertEqual(m.translate(0x80110000, 'ovl_b'), 0x8010EB00)
        self.assertEqual(m.translate(0x80110000, 'ovl_e'), 0x8010EAD0)
        self.assertEqual(m.translate(0x80420000), 0x80420000)        # Expansion RAM stays


def put(buf, off, words):
    for i, w in enumerate(words):
        struct.pack_into('>I', buf, off + 4 * i, w)


class MatchTests(unittest.TestCase):
    def test_shifted_functions_and_a_changed_one(self):
        # stock: three functions at 0x80001000; hack: the same shapes 0x40 later, the middle one reshaped
        f1 = [addiu(29, 29, -0x18), sw(RA, 0x10, 29), jal(0x80004000), NOP, lw(RA, 0x10, 29), jr(RA), addiu(29, 29, 0x18)]
        f2 = [lui(AT, 0x8005), lw(V0, 0x10, AT), addiu(V0, V0, 1), sw(V0, 0x10, AT), jr(RA), NOP, NOP]
        f3 = [lui(A0, 0x8006), addiu(A0, A0, 0x20), jal(0x80004100), NOP, jr(RA), NOP, NOP]
        f2h = [addiu(V0, 0, 5), addiu(V1, 0, 6), addu(V0, V0, V1), jr(RA), NOP, NOP, NOP]
        base, hack = bytearray(0x4000), bytearray(0x4000)
        put(base, 0x1000, f1 + f2 + f3)
        put(hack, 0x1040, f1 + f2h + f3)
        sec = d.Section('t', 0x1000, 0x80001000, 0x80001000 + 4 * 21, 0x80001000 + 4 * 21, 0x80001000 + 4 * 21,
                        [(0, 28), (28, 28), (56, 28)])
        m = d.match_section(bytes(base), bytes(hack), sec, 0x1000, 0x80001000, 0x80001000 + 0x40 + 4 * 21)
        self.assertEqual([(s, h) for s, h, z in m.matched], [(0x80001000, 0x80001040), (0x80001038, 0x80001078)])
        self.assertIn((0x8000101C, 0x80001038, 0x8000105C, 0x80001078), m.chunks)


class TraceTests(unittest.TestCase):
    def test_extent_follows_branches_and_stops_at_jr(self):
        words = {0x80400000: beq(V0, 0, 3), 0x80400004: NOP, 0x80400008: jr(RA), 0x8040000C: NOP,
                 0x80400010: j(0x80400100), 0x80400014: NOP, 0x80400018: 0xDEADBEEF}
        end, jt, jalt, back = d.trace_extent(lambda v: words.get(v, 0), 0x80400000, 0x80400000, 0x80400100)
        self.assertEqual(end, 0x80400018)
        self.assertEqual(jt, {0x80400100})

    def test_branch_into_a_jump_delay_slot_traces_on(self):
        # bne -> the j's delay slot, then fall through to a jump-table dispatch (hand-written hack code)
        words = {0x80400000: beq(AT, S0, 2) | (1 << 26), 0x80400004: NOP, 0x80400008: j(0x80100000),
                 0x8040000C: NOP, 0x80400010: lui(AT, 0x8015), 0x80400014: jr(V0), 0x80400018: NOP}
        end, jt, jalt, back = d.trace_extent(lambda v: words.get(v, 0), 0x80400000, 0x80400000, 0x80400100)
        self.assertEqual(end, 0x8040001C)


class PlausibleTests(unittest.TestCase):
    def test_code_is_plausible_and_text_is_not(self):
        self.assertTrue(mips.plausible_entry([addiu(AT, 0, 1), lui(V0, 0x8015), addu(V0, V0, A0), jr(RA), NOP]))
        # a byte table that happens to decode as a shift with rs set, and ASCII text
        self.assertFalse(mips.plausible_entry([0x01030202, 0x03020401]))
        self.assertFalse(mips.plausible(0x43414745))      # "CAGE": COP0 with an unused rs


class RefreshHookTests(unittest.TestCase):
    T1, T4 = 9, 12

    def test_pure_hook_body(self):
        # out = *(in) + 0x10000, then back into the hooked function
        words = [lui(T0, 0x8012), lw(T0, 0x3450, T0), lui(self.T1, 1), addu(T0, T0, self.T1),
                 lui(self.T4, 0x8041), sw(T0, 0x20, self.T4), j(0x80001234), NOP, 0xDEADBEEF]
        idx, ins, outs = d.pure_hook_body(words)
        self.assertEqual(idx, [0, 1, 2, 3, 4, 5])
        self.assertEqual(ins, {0x80123450})
        self.assertEqual(outs, {0x80410020})

    def test_hook_with_branch_or_saved_register_is_not_pure(self):
        self.assertIsNone(d.pure_hook_body([lui(T0, 0x8012), beq(T0, 0, 2), NOP, j(0x80001234), NOP]))
        self.assertIsNone(d.pure_hook_body([lui(S0, 0x8012), lw(T0, 0x3450, S0), lui(self.T4, 0x8041),
                                            sw(T0, 0x20, self.T4), j(0x80001234), NOP]))

    def test_delay_slot_of_the_jump_back_is_part_of_the_body(self):
        words = [lui(T0, 0x8012), lw(T0, 0x3450, T0), lui(self.T4, 0x8041), j(0x80001234), sw(T0, 0x20, self.T4)]
        idx, ins, outs = d.pure_hook_body(words)
        self.assertEqual(idx, [0, 1, 2, 4])
        self.assertEqual(outs, {0x80410020})


def fake_payload(words_by_vram, extents, shift=None):
    """A minimal analysis around synthetic payload code at 0x80400000 (ROM 0x1000), for the passes
    that rewrite payload code in place. extents: [(vram, size)] of payload functions."""
    import types
    rom, ram, size = 0x1000, 0x80400000, 0x100
    buf = bytearray(rom + size)
    for v, w in words_by_vram.items():
        put(buf, rom + (v - ram), [w])
    cand = shift or (lambda a, ctx=None: [])
    A = types.SimpleNamespace(payload=dict(rom=rom, ram=ram, size=size), region_ctx={},
                              map=types.SimpleNamespace(candidates=cand), hack=bytes(buf),
                              extras=[d.Extra('payload', v, v, z) for v, z in extents])
    return A, bytearray(buf)


def wd(buf, A, v):
    return d.word(buf, A.payload['rom'] + (v - A.payload['ram']))


class PayloadStubTests(unittest.TestCase):
    S0, A0, A1, T1, T2, T3, T5, T9 = 16, 4, 5, 9, 10, 11, 13, 25

    def test_lo_that_no_hi_reaches_goes_through_a_stub(self):
        # one lui feeds 0x800993F0 and 0x800A261C; after a -0x1530 shift they need different %hi
        hack = {0x80400000: lui(self.S0, 0x800A), 0x80400004: lw(self.A0, -0x6C10, self.S0),
                0x80400008: lw(self.A1, 0x261C, self.S0), 0x8040000C: addiu(self.T1, 0, 1),
                0x80400010: jr(RA), 0x80400014: NOP}
        shift = lambda a, ctx=None: [a - 0x1530] if 0x80090000 <= a < 0x800B0000 else []
        A, D = fake_payload(hack, [(0x80400000, 0x18)], shift)
        put(D, 0x1000 + 8, [lw(self.A1, 0x10EC, self.S0)])      # what rewrite() could do: the majority
        area, report = bytearray(), {}
        d.fix_unreachable_lo(A, D, area, report)
        self.assertEqual(len(report['unreachable_lo_fixed']), 1)
        stub = d.NEW_CODE_VRAM
        self.assertEqual(wd(D, A, 0x80400004), j(stub))
        self.assertEqual(wd(D, A, 0x80400008), NOP)
        self.assertEqual([d.word(area, 4 * i) for i in range(5)],
                         [lui(self.A0, 0x8009), lw(self.A0, 0x7EC0, self.A0), lw(self.A1, 0x10EC, self.S0),
                          j(0x8040000C), NOP])
        self.assertIn(0x8040000C, [x.vram for x in A.extras if x.kind == 'payload'])

    def test_self_modified_constant_moves_to_a_ram_variable(self):
        # ori t3, zero, 3 holds a variable; jal getpc / addiu t3, ra, -0xE / sh t2, 0(t3) patches it
        sh = lambda rt, off, base: (0x29 << 26) | (base << 21) | (rt << 16) | (off & 0xFFFF)
        hack = {0x80400000: addiu(self.T2, 0, 0x40), 0x80400004: addiu(self.T9, 0, 0),
                0x80400008: ori(self.T3, 0, 3), 0x8040000C: addiu(self.T5, 0, 1),
                0x80400010: jal(0x80400040), 0x80400014: NOP,
                0x80400018: addiu(self.T3, RA, -0xE), 0x8040001C: sh(self.T2, 0, self.T3),
                0x80400020: jr(RA), 0x80400024: NOP,
                0x80400040: jr(RA), 0x80400044: NOP}
        A, D = fake_payload(hack, [(0x80400000, 0x28), (0x80400040, 8)])
        area, report = bytearray(), {}
        d.fix_get_pc(A, D, area, report, new_rom=0x400000)
        var = A.ram_vars[1][0]
        self.assertEqual(var['ram'], 0x80402000)
        self.assertEqual(d.word(area, var['rom'] - 0x400000), ori(self.T3, 0, 3))    # preloaded original word
        vt = var['ram'] + 2
        self.assertEqual(wd(D, A, 0x80400014), lui(self.T3, (vt + 0x8000) >> 16))      # absolute address,
        self.assertEqual(wd(D, A, 0x80400018), addiu(self.T3, self.T3, vt & 0xFFFF))   # no ra needed
        stub = wd(D, A, 0x80400008)
        self.assertEqual(stub >> 26, 2)
        s = mips.jump_target(stub, 0x80400008) - d.NEW_CODE_VRAM
        lhu = (0x25 << 26) | (self.T3 << 21) | (self.T3 << 16) | (vt & 0xFFFF)
        self.assertEqual([d.word(area, s + 4 * i) for i in range(5)],
                         [lui(self.T3, (vt + 0x8000) >> 16), lhu, addiu(self.T5, 0, 1), j(0x80400010), NOP])


def fake_main(funcs, stock_words, hack_words):
    """A minimal analysis with one 'main' section at 0x80000400 (ROM 0x1000) for the entry passes;
    stock_words / hack_words: {vram: word}."""
    import types
    size = 0x100
    base, D = bytearray(0x1000 + size), bytearray(0x1000 + size)
    for v, w in stock_words.items():
        put(base, 0x1000 + (v - 0x80000400), [w])
    for v, w in hack_words.items():
        put(D, 0x1000 + (v - 0x80000400), [w])
    sec = d.Section('main', 0x1000, 0x80000400, 0x80000400 + size, 0x80000400 + size, 0x80000400 + size,
                    [(v - 0x80000400, z) for v, z in funcs])
    A = types.SimpleNamespace(byname={'main': sec}, layout={'extra_code': []}, base=bytes(base), extras=[])
    return A, D


HULK = 0x48756C6B   # "Hulk": COP2, not an instruction a game has


class DataInCodeTests(unittest.TestCase):
    def test_dead_code_filled_with_names_is_data(self):
        whole, _ = d.data_in_code([HULK, 0x20486F67, 0x616E0000, NOP], 0x80001000)
        self.assertTrue(whole)

    def test_stray_word_after_the_return_is_unreached(self):
        whole, unreached = d.data_in_code([addiu(V0, 0, 1), jr(RA), NOP, HULK], 0x80001000)
        self.assertFalse(whole)
        self.assertEqual(unreached, {3})

    def test_branch_target_is_reached(self):
        # beq over a word to code after the return: both are code
        words = [beq(A0, 0, 3), NOP, jr(RA), NOP, addiu(V0, 0, 2), jr(RA), NOP]
        self.assertEqual(d.data_in_code(words, 0x80001000), (False, set()))

    def test_chunk_is_judged_per_function(self):
        # a changed chunk of two stock functions: the first now holds names, the second is hack code
        stock = {0x80000400: addiu(V0, 0, 1), 0x80000404: jr(RA), 0x80000408: NOP, 0x8000040C: NOP,
                 0x80000410: addiu(V0, 0, 2), 0x80000414: jr(RA), 0x80000418: NOP, 0x8000041C: NOP}
        A, _ = fake_main([(0x80000400, 0x10), (0x80000410, 0x10)], stock, {})
        sw = [stock[0x80000400 + 4 * i] for i in range(8)]
        hw = [HULK, 0x20486F67, 0x616E0000, 0, addiu(V0, 0, 3), jr(RA), NOP, NOP]
        ports = d.port_data_chunk(A, 'main', 0x80000400, 0x80000400, 0x20, hw, sw, shift_map([]))
        self.assertEqual([(p.stock_vram, p.status) for p in ports], [(0x80000400, 'data'), (0x80000410, 'changed')])
        self.assertEqual(ports[0].words, hw[:4])
        # inside such a chunk, a function whose entry decodes but which holds a non-instruction is data too
        hw[7] = HULK
        ports = d.port_data_chunk(A, 'main', 0x80000400, 0x80000400, 0x20, hw, sw, shift_map([]))
        self.assertEqual([(p.stock_vram, p.status) for p in ports], [(0x80000400, 'data'), (0x80000410, 'data')])

    def test_unreached_word_pointing_nowhere_is_kept_as_it_is(self):
        # a stray word after the return decodes as a jal to nowhere: data, not an unresolved reference
        stock = {0x80000400: addiu(V0, 0, 1), 0x80000404: jr(RA), 0x80000408: NOP, 0x8000040C: NOP}
        A, _ = fake_main([(0x80000400, 0x10)], stock, {})
        sw = [stock[0x80000400 + 4 * i] for i in range(4)]
        hw = [HULK, 0, 0, 0]                                   # the function's entry is data...
        p = d.port_data_chunk(A, 'main', 0x80000400, 0x80000400, 0x10, hw, sw, shift_map([]))
        self.assertEqual([(x.status, x.problems) for x in p], [('data', [])])
        hw = [addiu(V0, 0, 5), jr(RA), NOP, jal(0x80300000)]   # ...or only a stray word is
        p = d.port_data_chunk(A, 'main', 0x80000400, 0x80000400, 0x10, hw, sw, shift_map([]))
        self.assertEqual([(x.status, x.problems, x.words[3]) for x in p], [('changed', [], jal(0x80300000))])


class PaddingDataTests(unittest.TestCase):
    STOCK = {0x80000400: addiu(V0, 0, 1), 0x80000404: jr(RA), 0x80000408: NOP}   # then nop padding to 0x20

    def kept(self, hack):
        A, D = fake_main([(0x80000400, 0x20)], self.STOCK, {**self.STOCK, **hack})
        return d.unchanged_code_functions(A, D, [], {})

    def test_data_in_padding_keeps_the_static_function(self):
        # Revenge Starrcade: a table after the function's return, in its nop padding
        self.assertEqual(self.kept({0x80000410: 0x80000410, 0x80000414: 1, 0x80000418: HULK}), [0x1000])

    def test_a_lone_instruction_in_padding_is_a_code_change(self):
        self.assertEqual(self.kept({0x80000414: addu(V1, V0, 0)}), [])

    def test_a_changed_instruction_is_a_code_change(self):
        self.assertEqual(self.kept({0x80000400: addiu(V0, 0, 2), 0x80000418: HULK}), [])

    def test_a_one_word_padding_function_holding_data_is_kept(self):
        # Revenge Starrcade 0x8002E900: a 4-byte all-nop "function"; a table word there decodes as a jal
        A, D = fake_main([(0x80000400, 0x4)], {}, {0x80000400: jal(0x80300000)})
        self.assertEqual(d.unchanged_code_functions(A, D, [], {}), [0x1000])

    def test_a_padding_function_the_code_before_runs_into_is_code(self):
        # VPW Gamma 0x80158984: the hack's code in the function before continues into the one-word stub
        stock = {0x80000400: jr(RA), 0x80000404: NOP}
        A, D = fake_main([(0x80000400, 0x8), (0x80000408, 0x4)], stock,
                         {0x80000400: addiu(V0, 0, 1), 0x80000404: addiu(V1, 0, 2), 0x80000408: lw(A0, 0, V0)})
        self.assertEqual(d.unchanged_code_functions(A, D, [], {}), [])
        # the function before still returns: nothing reaches the stub, so its word is data
        A, D = fake_main([(0x80000400, 0x8), (0x80000408, 0x4)], stock, {**stock, 0x80000408: jal(0x80300000)})
        self.assertEqual(d.unchanged_code_functions(A, D, [], {}), [0x1008])


class EntryPassTests(unittest.TestCase):
    def test_tail_call_into_the_middle_of_a_function_gets_an_entry(self):
        hack = {0x80000400: j(0x80000418), 0x80000404: NOP, 0x80000410: addiu(V0, 0, 1),
                0x80000418: addiu(V0, 0, 2), 0x80000428: jr(RA), 0x8000042C: NOP}
        A, D = fake_main([(0x80000400, 0x10), (0x80000410, 0x20)], {0x80000400: jr(RA)}, hack)
        d.add_branch_out_entries(A, D, [d.Port('main', 0x80000400, 0x10, None, 'changed', [])])
        self.assertEqual([(x.vram, x.size) for x in A.extras], [(0x80000418, 0x18)])

    def test_function_running_off_its_end_into_padding_gets_an_entry(self):
        # the hack replaced the exit of the first function; it now continues through padding
        stock = {0x80000400: addiu(V0, 0, 1), 0x80000408: jr(RA), 0x8000040C: NOP,
                 0x80000418: addiu(V0, 0, 2), 0x80000420: jr(RA), 0x80000424: NOP}
        hack = dict(stock)
        hack.update({0x80000408: addiu(A0, 0, 4), 0x8000040C: addiu(A1, 0, 2), 0x80000410: addiu(A0, 0, 1)})
        A, D = fake_main([(0x80000400, 0x10), (0x80000418, 0x10)], stock, hack)
        d.add_fall_through_entries(A, D, [])
        self.assertEqual([(x.vram, x.size) for x in A.extras], [(0x80000410, 0x18)])

    def test_blanked_function_gets_no_fall_through_entry(self):
        # the hack nopped a stub out: dead code, nothing runs into what follows
        stock = {0x80000400: addiu(V0, 0, 1), 0x80000408: jr(RA), 0x8000040C: NOP,
                 0x80000418: addiu(V0, 0, 2), 0x80000420: jr(RA), 0x80000424: NOP}
        hack = dict(stock)
        hack.update({0x80000400: NOP, 0x80000408: NOP})
        A, D = fake_main([(0x80000400, 0x10), (0x80000418, 0x10)], stock, hack)
        d.add_fall_through_entries(A, D, [])
        self.assertEqual(A.extras, [])

    def test_function_never_ends_on_a_jump_without_its_slot(self):
        self.assertEqual(d.with_delay_slot(0x4CC, jal(0x801409D4)), 0x4D0)
        self.assertEqual(d.with_delay_slot(0x20, beq(V0, 0, 3)), 0x24)
        self.assertEqual(d.with_delay_slot(0x20, addiu(V0, 0, 1)), 0x20)

    def test_data_kept_as_a_function_is_ignored(self):
        data = {0x80000400 + 4 * i: 0x43414745 for i in range(4)}       # "CAGE" words, not code
        A, D = fake_main([(0x80000400, 0x10), (0x80000418, 0x10)], {}, data)
        d.add_fall_through_entries(A, D, [])
        d.add_branch_out_entries(A, D, [d.Port('main', 0x80000400, 0x10, None, 'changed', [])])
        self.assertEqual(A.extras, [])


class JumpTableCheckTests(unittest.TestCase):
    def test_table_outside_function_is_flagged(self):
        rom = bytearray(0x200)
        h, l = hi_lo(0x80000100)
        code = [lui(AT, h), addu(AT, AT, V0), lw(V0, l, AT), jr(V0), NOP, jr(RA), NOP]
        put(rom, 0x0, code)
        put(rom, 0x100, [0x80000014, 0x80000014])                         # good: inside the function
        self.assertEqual(d.check_jump_tables(bytes(rom), [(0x80000000, 0x0, 4 * len(code))], [], []), [])
        put(rom, 0x100, [0x80400000, 0x80000014])                         # first case in hack code...
        self.assertEqual(len(d.check_jump_tables(bytes(rom), [(0x80000000, 0x0, 4 * len(code))], [], [])), 1)
        # ...which is fine once the hack's code range is declared
        self.assertEqual(d.check_jump_tables(bytes(rom), [(0x80000000, 0x0, 4 * len(code))],
                                             [(0x80400000, 0x80500000)], []), [])

    def test_jump_through_a_pointer_variable_is_not_a_table(self):
        # lui + lw %lo + jr with no index added: a function pointer, which the runtime calls through the lookup
        rom = bytearray(0x200)
        h, l = hi_lo(0x80000100)
        code = [lui(AT, h), lw(V0, l, AT), jr(V0), NOP, jr(RA), NOP]
        put(rom, 0x0, code)
        put(rom, 0x100, [0x80400000])
        self.assertEqual(d.check_jump_tables(bytes(rom), [(0x80000000, 0x0, 4 * len(code))], [], []), [])

    def test_load_overwritten_before_the_jr_is_not_its_table(self):
        # an indexed array load into v0, then v0 is recomputed from another base before the jr
        rom = bytearray(0x200)
        h, l = hi_lo(0x80000100)
        A2, T3 = 6, 11
        code = [lui(V0, h), addu(V0, V0, A2), lw(V0, l, V0), addu(V0, V1, T3), lw(V0, 0, V0), jr(V0), NOP]
        put(rom, 0x0, code)
        put(rom, 0x100, [0x80400000])
        self.assertEqual(d.check_jump_tables(bytes(rom), [(0x80000000, 0x0, 4 * len(code))], [], []), [])


class BpsTests(unittest.TestCase):
    def test_round_trip_with_moved_data(self):
        src = bytes(range(256)) * 64
        tgt = src[:1000] + b'new data' + src[3000:9000] + src[100:2000] + bytes(500)
        patch = bps.bps_encode(src, tgt)
        self.assertEqual(bps.bps_apply(src, patch), tgt)


if __name__ == '__main__':
    unittest.main()
