"""Differential flamegraphs of two capacity runs, for the comparison on the Capacity tab.

The folded stacks of a run are not kept, so they are read back from its flamegraph SVG: the title
of each frame gives its value, "(12,345 bytes, 0.01%)" or "(12 samples, ...)". Frames narrower than
the minimum width of the SVG were not drawn, so their value counts in their parent, which blurs
small changes and leaves large ones exact.

difffolded.pl and flamegraph.pl of FlameGraph then draw the stacks of the newer run, red where
they grew and blue where they shrank, or with ``removed`` those of the older run, where a stack
that is gone in the newer run still has its width.
"""
import html
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

FRAME = re.compile(r'<title>(.*?)</title>\s*<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)"')
VALUE = re.compile(r"^(.*) \(([\d,]+) ([a-z]+), [\d.]+%\)$")
# The flamegraphs that a run directory holds, which two runs can be compared on.
NAMES = re.compile(r"flamegraph\.svg|heap-(all|kit|coolwsd|forkit|other)-"
                   r"(peak|allocations|temporary|leaked)\.svg")


def folded(svg: Path) -> tuple[Counter, str]:
    """The folded stacks of a flamegraph.pl SVG, and the unit of its values."""
    frames = []
    unit = "samples"
    for m in FRAME.finditer(svg.read_text(errors="replace")):
        v = VALUE.match(html.unescape(m.group(1)))
        if v:
            unit = v.group(3)
            frames.append((v.group(1), float(m.group(2)), float(m.group(3)),
                           float(m.group(4)), int(v.group(2).replace(",", ""))))
    # The root ("all") is the lowest row; the children of a frame are in the row above it.
    levels = sorted({f[2] for f in frames}, reverse=True)
    rows = defaultdict(list)
    for f in frames:
        rows[f[2]].append(f)
    out: Counter = Counter()
    todo = [(f, (), 0) for f in rows[levels[0]]] if levels else []
    while todo:
        (name, x, _, w, value), path, depth = todo.pop()
        kids = ([k for k in rows[levels[depth + 1]]
                 if k[1] >= x - 1e-3 and k[1] + k[3] <= x + w + 1e-3]
                if depth + 1 < len(levels) else [])
        here = path + (name,) if depth else path
        rest = value - sum(k[4] for k in kids)
        if rest > 0 and here:
            out[";".join(here)] += rest
        todo.extend((k, here, depth + 1) for k in kids)
    return out, unit


def _tool(name: str) -> str:
    return shutil.which(name) or f"/opt/FlameGraph/{name}"


def diff(old: Path, new: Path, title: str, removed: bool = False) -> bytes:
    """The differential flamegraph of @p old against @p new, as SVG."""
    a, unit = folded(old)
    b, _ = folded(new)
    with tempfile.TemporaryDirectory() as tmp:
        pa, pb = Path(tmp, "old"), Path(tmp, "new")
        pa.write_text("".join(f"{s} {v}\n" for s, v in a.items()))
        pb.write_text("".join(f"{s} {v}\n" for s, v in b.items()))
        # A perf profile counts samples, which grow with the length of the run, so the two are
        # scaled to the same total; a heap profile counts bytes, which compare as they are.
        norm = ["-n"] if unit == "samples" else []
        first, second = (pb, pa) if removed else (pa, pb)
        stacks = subprocess.run(["perl", _tool("difffolded.pl"), *norm, str(first), str(second)],
                                check=True, capture_output=True).stdout
        draw = ["perl", _tool("flamegraph.pl"), "--title", title, "--countname", unit]
        if removed:
            draw.append("--negate")
        return subprocess.run(draw, input=stacks, check=True, capture_output=True).stdout
