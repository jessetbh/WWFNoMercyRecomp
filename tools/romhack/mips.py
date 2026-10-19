"""Small MIPS (R4300) helpers for the ROM-hack tools: field decoding, address-field masking and
%hi/%lo pairing. Pure Python, no dependencies, so the tools run on a player's machine."""

LUI = 0x0F
# I-type opcodes whose 16-bit immediate can be the %lo half of an address.
LO_OPS = {0x09,                                            # addiu
          0x20, 0x21, 0x23, 0x24, 0x25, 0x27, 0x37,        # lb lh lw lbu lhu lwu ld
          0x28, 0x29, 0x2B, 0x3F,                          # sb sh sw sd
          0x31, 0x35, 0x39, 0x3D}                          # lwc1 ldc1 swc1 sdc1
LOAD_GPR = {0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x37, 0x1A, 0x1B}
STORE_OPS = {0x28, 0x29, 0x2A, 0x2B, 0x2E, 0x3F, 0x2C, 0x2D, 0x31, 0x35, 0x39, 0x3D}
# Immediates masked out for shape signatures (address halves and offsets).
MASKED_IMM_OPS = {0x0F, 0x09, 0x08, 0x0D, 0x0C, 0x0A, 0x0B, 0x0E,
                  0x20, 0x21, 0x23, 0x24, 0x25, 0x27, 0x28, 0x29, 0x2B, 0x31, 0x35, 0x39, 0x3D, 0x37, 0x3F}
# Registers a call clobbers (o32): at v0 v1 a0-a3 t0-t9 ra.
CALL_CLOBBERS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 31}


def word(data, off):
    return int.from_bytes(data[off:off + 4], 'big')


def put_word(buf, off, value):
    buf[off:off + 4] = (value & 0xFFFFFFFF).to_bytes(4, 'big')


def sx16(v):
    return v - 0x10000 if v & 0x8000 else v


def op_of(w):
    return w >> 26


def masked(w):
    """The word with address-like fields cleared, for matching moved code by shape."""
    op = w >> 26
    if op in (2, 3):
        return w & 0xFC000000
    if op in MASKED_IMM_OPS:
        return w & 0xFFFF0000
    return w


def jump_target(w, pc):
    return ((pc + 4) & 0xF0000000) | ((w & 0x3FFFFFF) << 2)


def with_jump_target(w, target):
    return (w & 0xFC000000) | ((target >> 2) & 0x3FFFFFF)


def branch_target(w, pc):
    """Target of a PC-relative branch, or None if w is not one."""
    op, rs, rt = w >> 26, (w >> 21) & 31, (w >> 16) & 31
    if op in (0x04, 0x05, 0x06, 0x07, 0x14, 0x15, 0x16, 0x17) or \
            (op == 0x01 and rt in (0x00, 0x01, 0x02, 0x03, 0x10, 0x11, 0x12, 0x13)) or \
            (op == 0x11 and rs == 0x08):
        return (pc + 4 + (sx16(w & 0xFFFF) << 2)) & 0xFFFFFFFF
    return None


def gpr_written(w):
    """The GPR an instruction writes (None if none or r0)."""
    op = w >> 26
    rs, rt, rd = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
    r = None
    if op == 0:
        funct = w & 0x3F
        if funct in (0x08, 0x0C, 0x0D, 0x0F, 0x11, 0x13) or 0x18 <= funct <= 0x1F or 0x30 <= funct <= 0x36:
            r = None       # jr syscall break sync mthi mtlo mult/div traps
        else:
            r = rd         # includes jalr (rd) and mfhi/mflo
    elif op == 3:
        r = 31
    elif 0x08 <= op <= 0x0F or op in (0x18, 0x19) or op in LOAD_GPR:
        r = rt
    elif op in (0x10, 0x11, 0x12) and rs in (0x00, 0x01, 0x02):   # mfc/dmfc/cfc
        r = rt
    return r or None


def hilo_state_at(words, index):
    """Registers holding a lui value when execution reaches words[index] (after its delay slot
    for a jump): {reg: hi_value}. Used to pair a hook's %lo uses with its caller's lui."""
    state = {}
    hilo_refs(words[:index + 2], final_state=state)
    return {r: v for r, (i, v) in state.items()}


def hilo_refs(words, base_pc=0, init=None, final_state=None):
    """Pair each %lo use with its lui. words: list of ints. Returns a list of refs
    (hi_index, lo_index, address); one lui can feed several lo uses. Linear scan over the
    function; a register keeps its lui value through `addu rd, rs, rt` (IDO indexes arrays
    that way) and loses it when anything else writes it or a call clobbers it."""
    state = {r: (None, v) for r, v in (init or {}).items()}   # reg -> (hi_index, hi_value); None = caller's
    out = []
    clobber_after = None  # a call's clobbers take effect after its delay slot
    for i, w in enumerate(words):
        if clobber_after is not None and i > clobber_after:
            for r in CALL_CLOBBERS:
                state.pop(r, None)
            clobber_after = None
        op = w >> 26
        rs, rt, rd = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
        if op == LUI:
            if rt:
                state[rt] = (i, (w & 0xFFFF) << 16)
            continue
        if (op in LO_OPS or op == 0x0D) and rs in state:      # 0x0D ori: unsigned %lo
            hi_i, hi_v = state[rs]
            lo = (w & 0xFFFF) if op == 0x0D else sx16(w & 0xFFFF)
            out.append((hi_i, i, (hi_v + lo) & 0xFFFFFFFF))
        if op == 0 and (w & 0x3F) in (0x21, 0x2D) and rd:     # addu / daddu
            a, b = state.get(rs), state.get(rt)
            if (a is None) != (b is None):
                state[rd] = a or b
                continue
        if op == 3 or (op == 0 and (w & 0x3F) == 0x09):
            clobber_after = i + 1
        r = gpr_written(w)
        if r is not None and op != LUI and not (op == 3 or (op == 0 and (w & 0x3F) == 0x09)):
            state.pop(r, None)
        if op == 0 and (w & 0x3F) == 0x08 and rs == 31:       # jr ra: end of a path
            pass
    if final_state is not None:
        final_state.update(state)
    return out


SPECIAL_OK = {0x00, 0x02, 0x03, 0x04, 0x06, 0x07, 0x08, 0x09, 0x0C, 0x0D, 0x0F} | set(range(0x10, 0x28)) - {0x15} |              {0x2A, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33, 0x34, 0x36,
              0x38, 0x3A, 0x3B, 0x3C, 0x3E, 0x3F}
SHIFT_IMM = {0x00, 0x02, 0x03, 0x38, 0x3A, 0x3B, 0x3C, 0x3E, 0x3F}
REGIMM_OK = {0x00, 0x01, 0x02, 0x03, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0E, 0x10, 0x11, 0x12, 0x13}
COP0_RS_OK = {0x00, 0x04, 0x10}
COP1_RS_OK = {0x00, 0x01, 0x02, 0x04, 0x05, 0x06, 0x08, 0x10, 0x11, 0x14, 0x15}
BAD_OPS = {0x12, 0x13, 0x1C, 0x1D, 0x1E, 0x1F, 0x3B}


def plausible(w):
    """False for words that are not R4300 instructions a game would contain (a cheap check for
    code addresses that really point at data)."""
    op = w >> 26
    if op in BAD_OPS:
        return False
    if op == 0:
        f = w & 0x3F
        if f not in SPECIAL_OK:
            return False
        if f in SHIFT_IMM and (w >> 21) & 31:
            return False
        return True
    if op == 1:
        return (w >> 16) & 31 in REGIMM_OK
    if op == 0x10:
        return (w >> 21) & 31 in COP0_RS_OK
    if op == 0x11:
        return (w >> 21) & 31 in COP1_RS_OK
    return True


def plausible_entry(words, n=6):
    """The first n words (or up to a jr/j and its delay slot) all plausible instructions."""
    for i, w in enumerate(words[:n]):
        if not plausible(w):
            return False
        if w >> 26 == 2 or (w >> 26 == 0 and (w & 0x3F) == 0x08):
            return i + 1 >= len(words) or plausible(words[i + 1])
    return True


def jumps(words, base_pc):
    """[(index, kind, target)] for j/jal."""
    out = []
    for i, w in enumerate(words):
        op = w >> 26
        if op in (2, 3):
            out.append((i, 'jal' if op == 3 else 'j', jump_target(w, base_pc + 4 * i)))
    return out
