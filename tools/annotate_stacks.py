import re, sys, bisect
from pathlib import Path

MAP = Path(__file__).resolve().parent.parent / "build-msvc" / "NoMercyRecompiled.map"
BASE = 0x140000000
SYM_RE = re.compile(r"^\s*[0-9a-f]{4}:[0-9a-f]{8}\s+(\S+)\s+([0-9a-f]{16})\s", re.IGNORECASE)
FRAME_RE = re.compile(r"NoMercyRecompiled\+0x([0-9A-Fa-f]+)")

syms = []
for line in open(MAP, encoding="utf-8", errors="replace"):
    m = SYM_RE.match(line)
    if m:
        addr = int(m.group(2), 16)
        if addr:
            syms.append((addr, m.group(1)))
syms.sort()

def lookup(rva):
    addr = BASE + rva
    i = bisect.bisect_right(syms, (addr, "\xff")) - 1
    if i < 0:
        return f"+0x{rva:X}"
    return f"{syms[i][1]}+0x{addr - syms[i][0]:X}"

sys.stdout.reconfigure(errors="replace")
enc = "utf-16" if open(sys.argv[1], "rb").read(2) == b"\xff\xfe" else "utf-8"
for line in open(sys.argv[1], encoding=enc, errors="replace"):
    out = FRAME_RE.sub(lambda m: lookup(int(m.group(1), 16)), line.rstrip("\n"))
    print(out)
