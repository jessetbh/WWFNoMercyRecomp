"""BPS patch encoder/decoder (copied from AkiMods tools/romhack/make_patch_mod.py, fix/74-bps-sourcecopy:
SourceCopy for moved data). Self-contained so the de-relink tool runs on its own."""
import re, zlib

SECTION_RE = re.compile(r'\{ \.rom_addr = 0x([0-9A-Fa-f]+), \.ram_addr = 0x([0-9A-Fa-f]+), \.size = 0x([0-9A-Fa-f]+), \.funcs = (\w+),')
FUNC_RE = re.compile(r'\{ \.func = (\w+), \.offset = 0x([0-9A-Fa-f]+), \.rom_size = 0x([0-9A-Fa-f]+) \}')

def bps_number(n):
    """BPS variable-length number."""
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n == 0:
            out.append(0x80 | x)
            break
        out.append(x)
        n -= 1
    return bytes(out)


BLOCK = 32        # source index granularity: moved runs of 2*BLOCK-1 bytes or more are always found
MIN_COPY = 12     # shortest copy worth an action at a tried offset (an action costs a few bytes)
ISSUE = 'https://github.com/jessetbh/aki-roadmap/issues/74'


class TooMuchTargetRead(ValueError):
    """The patch would carry more literal (TargetRead) bytes than allowed."""
    def __init__(self, limit):
        super().__init__('the patch would carry more than %d literal bytes (TargetRead)' % limit)
        self.limit = limit


def _match_len(a, i, b, j):
    """Length of the common run of a[i:] and b[j:]."""
    limit = min(len(a) - i, len(b) - j)
    n, chunk = 0, 64
    while n < limit:
        k = min(chunk, limit - n)
        if a[i + n:i + n + k] == b[j + n:j + n + k]:
            n += k
            chunk = min(chunk * 2, 1 << 16)
            continue
        lo, hi = 0, k  # a prefix of lo bytes matches, one of hi bytes doesn't
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if a[i + n:i + n + mid] == b[j + n:j + n + mid]:
                lo = mid
            else:
                hi = mid
        return n + lo
    return n


def _block_index(source):
    """{crc32 of an aligned BLOCK-byte block: its first offset}. Hash collisions only cost a
    missed match: every candidate is checked against the bytes."""
    index = {}
    for off in range(0, len(source) - BLOCK + 1, BLOCK):
        index.setdefault(zlib.crc32(source[off:off + BLOCK]), off)
    return index


def bps_encode(source, target, metadata=b'', max_target_read=None, stats=None):
    """BPS patch that finds moved data. SourceRead copies a run that matches the source in
    place; SourceCopy copies a run from anywhere in the source; TargetRead carries the bytes
    the source doesn't have. Moved runs are found by trying the last copy's offset first (a
    hack that slides data shifts long stretches by the same amount), then an index of the
    source's aligned BLOCK-byte blocks. Greedy, not optimal, but a hack that moves data costs
    a few bytes per moved run instead of a copy of everything after the move.

    max_target_read: raise TooMuchTargetRead when the patch would carry more TargetRead bytes
    than this (those bytes are copied into the patch, possibly from the stock ROM).
    stats: optional dict, filled with the byte count of each action kind."""
    SOURCE_READ, TARGET_READ, SOURCE_COPY = 0, 1, 2
    out = bytearray(b'BPS1')
    out += bps_number(len(source)) + bps_number(len(target)) + bps_number(len(metadata)) + metadata
    counts = {'source_read': 0, 'source_copy': 0, 'target_read': 0}

    def action(kind, length):
        out.extend(bps_number(((length - 1) << 2) | kind))

    n, src_len = len(target), len(source)
    index = None
    src_rel = 0       # BPS sourceRelativeOffset
    last_delta = 0    # source offset - target offset of the last copy
    lit_start = pos = 0

    def flush_literal(end):
        if end > lit_start:
            action(TARGET_READ, end - lit_start)
            out.extend(target[lit_start:end])
            counts['target_read'] += end - lit_start

    while pos < n:
        found = None
        b = target[pos]
        for d in ((last_delta, 0) if last_delta else (0,)):
            s = pos + d
            if 0 <= s < src_len and source[s] == b:
                length = _match_len(source, s, target, pos)
                if length >= MIN_COPY or pos + length == n:
                    found = (s, length)
                    break
        if found is None and pos + BLOCK <= n:
            if index is None:
                index = _block_index(source)
            s = index.get(zlib.crc32(target[pos:pos + BLOCK]))
            if s is not None:
                length = _match_len(source, s, target, pos)
                if length >= BLOCK:
                    found = (s, length)
        if found is None:
            pos += 1
            if max_target_read is not None and counts['target_read'] + pos - lit_start > max_target_read:
                raise TooMuchTargetRead(max_target_read)
            continue

        s, length = found
        # The match may start before the byte that found it: take those bytes off the literal.
        while pos > lit_start and s > 0 and source[s - 1] == target[pos - 1]:
            pos -= 1
            s -= 1
            length += 1
        flush_literal(pos)
        if s == pos:
            action(SOURCE_READ, length)
            counts['source_read'] += length
        else:
            action(SOURCE_COPY, length)
            off = s - src_rel
            out.extend(bps_number((abs(off) << 1) | (off < 0)))
            src_rel = s + length
            counts['source_copy'] += length
        last_delta = s - pos
        pos += length
        lit_start = pos
    flush_literal(n)

    out += zlib.crc32(source).to_bytes(4, 'little')
    out += zlib.crc32(target).to_bytes(4, 'little')
    out += zlib.crc32(bytes(out)).to_bytes(4, 'little')
    if stats is not None:
        stats.update(counts)
    return bytes(out)


def bps_apply(source, patch):
    """Reference decoder (SourceRead/TargetRead/SourceCopy/TargetCopy), used to verify output."""
    def number(p):
        data, shift = 0, 1
        while True:
            x = patch[p]
            p += 1
            data += (x & 0x7F) * shift
            if x & 0x80:
                return data, p
            shift <<= 7
            data += shift

    assert patch[:4] == b'BPS1'
    p = 4
    src_size, p = number(p)
    tgt_size, p = number(p)
    meta_size, p = number(p)
    p += meta_size
    out = bytearray()
    src_rel = tgt_rel = 0
    end = len(patch) - 12
    while p < end:
        cmd, p = number(p)
        kind, length = cmd & 3, (cmd >> 2) + 1
        if kind == 0:
            out += source[len(out):len(out) + length]
        elif kind == 1:
            out += patch[p:p + length]
            p += length
        else:
            off, p = number(p)
            off = (-1 if off & 1 else 1) * (off >> 1)
            if kind == 2:
                src_rel += off
                out += source[src_rel:src_rel + length]
                src_rel += length
            else:
                tgt_rel += off
                for _ in range(length):
                    out.append(out[tgt_rel])
                    tgt_rel += 1
    assert len(out) == tgt_size and src_size == len(source)
    return bytes(out)


# ---------------------------------------------------------------- changed-function report

SECTION_RE = re.compile(r'\{ \.rom_addr = 0x([0-9A-Fa-f]+), \.ram_addr = 0x([0-9A-Fa-f]+), \.size = 0x([0-9A-Fa-f]+), \.funcs = (\w+),')
FUNC_RE = re.compile(r'\{ \.func = (\w+), \.offset = 0x([0-9A-Fa-f]+), \.rom_size = 0x([0-9A-Fa-f]+) \}')


def load_sections(overlays_inl):
    """[(name, rom, ram, size, [(func, offset, rom_size)])] from recomp_overlays.inl."""
    text = open(overlays_inl, encoding='utf-8', errors='replace').read()
    tables = {}
    for m in re.finditer(r'static FuncEntry (\w+)\[\] = \{(.*?)\n\};', text, re.S):
        tables[m.group(1)] = [(f.group(1), int(f.group(2), 16), int(f.group(3), 16)) for f in FUNC_RE.finditer(m.group(2))]
    sections = []
    for m in SECTION_RE.finditer(text):
        sections.append((m.group(4), int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16), tables.get(m.group(4), [])))
    return sections


