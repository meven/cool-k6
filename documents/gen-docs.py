#!/usr/bin/env python3
"""Generate the test documents for the per-application browser tests.

For each Collabora Online application it writes two files under documents/:
  - a blank document (blank.odt / blank.ods / blank.odp / blank.odg), and
  - a big document named after its size (writer-5mb.odt, writer-10mb.odt),
    filled with the content a user of that application would typically have,
    so load and render times are realistic:
      Writer   titles, paragraphs, tables and images
      Calc     several sheets of data tables plus rendered charts
      Impress  slides with titles, text, images and drawn shapes
      Draw     pages of drawn shapes (rectangles, ellipses, lines) and images

Size is driven by embedded raster images (ODF compresses text and cells well,
so images are what make a file big), padded until the embedded-image bytes
reach the target.

Dependencies (see documents/requirements.txt):
    python3 -m pip install odfpy Pillow matplotlib numpy
Run:
    python3 documents/gen-docs.py            # all apps, ~5 MB each
    python3 documents/gen-docs.py --mb 10    # the 10 MB documents as well
    python3 documents/gen-docs.py --mb 2     # smaller, for a quick check
"""
import argparse
import io
import shutil
import subprocess
import sys
import tempfile
import random
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image as PILImage, ImageDraw

from odf.opendocument import (OpenDocumentText, OpenDocumentSpreadsheet,
                              OpenDocumentPresentation, OpenDocumentDrawing)
from odf.style import (Style, TextProperties, ParagraphProperties,
                       GraphicProperties, MasterPage, PageLayout,
                       PageLayoutProperties)
from odf.text import P, H
from odf.table import Table, TableColumn, TableRow, TableCell
from odf.draw import Page, Frame, Image, Rect, Ellipse, Line, Polygon, TextBox

OUT = Path(__file__).resolve().parent
RNG = random.Random(20260813)

WORDS = ("document collaboration render layout paragraph table chart image "
         "spreadsheet presentation drawing heading section column formula "
         "review comment revision export import filter style template").split()


def size_label(mb: float) -> str:
    """The part of a big document's name that its size gives it: 5 -> "5mb"."""
    return f"{mb:g}mb"


def prose(n_words: int) -> str:
    return " ".join(RNG.choice(WORDS) for _ in range(n_words)).capitalize() + "."


def photo_png(w: int = 900, h: int = 640) -> bytes:
    """A colourful raster image, incompressible enough to carry real bytes."""
    arr = (np.random.default_rng(RNG.randrange(1 << 30))
           .integers(0, 256, size=(h, w, 3), dtype=np.uint8))
    img = PILImage.fromarray(arr, "RGB")
    draw = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = RNG.randrange(w), RNG.randrange(h)
        x1, y1 = RNG.randrange(w), RNG.randrange(h)
        col = tuple(RNG.randrange(256) for _ in range(3))
        draw.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
                       outline=col, width=4)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def chart_png(kind: str = "line") -> bytes:
    fig, ax = plt.subplots(figsize=(5.2, 3.2), dpi=130)
    x = np.arange(24)
    if kind == "bar":
        ax.bar(x, np.random.default_rng(RNG.randrange(1 << 30)).integers(1, 100, 24))
    else:
        for _ in range(3):
            ax.plot(x, np.random.default_rng(RNG.randrange(1 << 30)).integers(0, 100, 24),
                    marker="o", markersize=3)
    ax.set_title(prose(3))
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def frame(doc, png: bytes, w_cm: float, h_cm: float, **kw) -> Frame:
    href = doc.addPictureFromString(png, "image/png")
    f = Frame(width="%.1fcm" % w_cm, height="%.1fcm" % h_cm, **kw)
    f.addElement(Image(href=href))
    return f


# --------------------------------------------------------------------------

def build_writer(target: int, blank: bool, label: str) -> None:
    doc = OpenDocumentText()
    h1 = Style(name="Heading 1", family="paragraph")
    h1.addElement(TextProperties(fontsize="20pt", fontweight="bold"))
    doc.styles.addElement(h1)
    if blank:
        doc.text.addElement(P(text=""))
        doc.save(str(OUT / "blank.odt"))
        return
    embedded, n = 0, 0
    while embedded < target:
        doc.text.addElement(H(outlinelevel=1, stylename=h1, text=f"Section {n + 1}"))
        for _ in range(3):
            doc.text.addElement(P(text=prose(60)))
        tbl = Table(name=f"table{n}")
        tbl.addElement(TableColumn(numbercolumnsrepeated=5))
        for r in range(6):
            row = TableRow()
            for c in range(5):
                cell = TableCell(valuetype="string")
                cell.addElement(P(text=f"r{r}c{c}" if r else f"Col {c + 1}"))
                row.addElement(cell)
            tbl.addElement(row)
        doc.text.addElement(tbl)
        png = photo_png()
        p = P()
        p.addElement(frame(doc, png, 14, 9, anchortype="paragraph"))
        doc.text.addElement(p)
        embedded += len(png)
        n += 1
    doc.save(str(OUT / f"writer-{label}.odt"))


def build_calc(target: int, blank: bool, label: str) -> None:
    doc = OpenDocumentSpreadsheet()
    if blank:
        doc.spreadsheet.addElement(Table(name="Sheet1"))
        doc.save(str(OUT / "blank.ods"))
        return
    embedded, sheet = 0, 0
    while embedded < target:
        tbl = Table(name=f"Data {sheet + 1}")
        for r in range(400):
            row = TableRow()
            for c in range(12):
                if r == 0:
                    cell = TableCell(valuetype="string")
                    cell.addElement(P(text=f"Col {c + 1}"))
                else:
                    cell = TableCell(valuetype="float", value=RNG.uniform(0, 10000))
                row.addElement(cell)
            tbl.addElement(row)
        # a couple of charts, embedded as images inside cells (a draw:frame is
        # not allowed directly under table:table, only inside a table:table-cell)
        for kind in ("line", "bar"):
            png = chart_png(kind)
            row = TableRow()
            cell = TableCell()
            cell.addElement(frame(doc, png, 12, 7.5, anchortype="cell"))
            row.addElement(cell)
            tbl.addElement(row)
            embedded += len(png)
        doc.spreadsheet.addElement(tbl)
        sheet += 1
    doc.save(str(OUT / f"calc-{label}.ods"))


def _draw_master(doc):
    """A single landscape master page, needed by Impress/Draw pages."""
    layout = PageLayout(name="pl1")
    layout.addElement(PageLayoutProperties(
        pagewidth="28cm", pageheight="21cm", printorientation="landscape"))
    doc.automaticstyles.addElement(layout)
    master = MasterPage(name="Standard", pagelayoutname=layout)
    doc.masterstyles.addElement(master)
    return master


def build_impress(target: int, blank: bool, label: str) -> None:
    doc = OpenDocumentPresentation()
    _draw_master(doc)
    if blank:
        doc.presentation.addElement(Page(masterpagename="Standard"))
        doc.save(str(OUT / "blank.odp"))
        return
    embedded, n = 0, 0
    while embedded < target:
        page = Page(masterpagename="Standard")
        title = Frame(width="24cm", height="3cm", x="2cm", y="1cm")
        tb = TextBox()
        tb.addElement(P(text=f"Slide {n + 1}: {prose(4)}"))
        title.addElement(tb)
        page.addElement(title)
        body = Frame(width="12cm", height="12cm", x="2cm", y="5cm")
        btb = TextBox()
        for _ in range(5):
            btb.addElement(P(text=prose(12)))
        body.addElement(btb)
        page.addElement(body)
        png = photo_png()
        img = frame(doc, png, 11, 8, x="15cm", y="6cm")
        page.addElement(img)
        page.addElement(Ellipse(width="4cm", height="2cm", x="16cm", y="16cm"))
        doc.presentation.addElement(page)
        embedded += len(png)
        n += 1
    doc.save(str(OUT / f"impress-{label}.odp"))


def build_draw(target: int, blank: bool, label: str) -> None:
    doc = OpenDocumentDrawing()
    _draw_master(doc)
    if blank:
        doc.drawing.addElement(Page(masterpagename="Standard"))
        doc.save(str(OUT / "blank.odg"))
        return
    embedded, n = 0, 0
    while embedded < target:
        page = Page(masterpagename="Standard")
        for _ in range(40):
            x, y = RNG.uniform(0, 24), RNG.uniform(0, 18)
            w, h = RNG.uniform(1, 5), RNG.uniform(1, 4)
            shape = RNG.choice(("rect", "ellipse", "line", "poly"))
            if shape == "rect":
                page.addElement(Rect(width="%.1fcm" % w, height="%.1fcm" % h,
                                     x="%.1fcm" % x, y="%.1fcm" % y))
            elif shape == "ellipse":
                page.addElement(Ellipse(width="%.1fcm" % w, height="%.1fcm" % h,
                                        x="%.1fcm" % x, y="%.1fcm" % y))
            elif shape == "line":
                page.addElement(Line(x1="%.1fcm" % x, y1="%.1fcm" % y,
                                     x2="%.1fcm" % (x + w), y2="%.1fcm" % (y + h)))
            else:
                pts = " ".join("%d,%d" % (RNG.randrange(2000), RNG.randrange(2000))
                               for _ in range(5))
                page.addElement(Polygon(points=pts, viewbox="0 0 2000 2000",
                                        width="%.1fcm" % w, height="%.1fcm" % h,
                                        x="%.1fcm" % x, y="%.1fcm" % y))
        png = photo_png()
        page.addElement(frame(doc, png, 10, 7, x="14cm", y="10cm"))
        doc.drawing.addElement(page)
        embedded += len(png)
        n += 1
    doc.save(str(OUT / f"draw-{label}.odg"))


# The Microsoft format each application's big document is also written in. Draw has none, so
# it has no OOXML document either.
OOXML = {"writer": ("odt", "docx"),
         "calc": ("ods", "xlsx"),
         "impress": ("odp", "pptx")}


def convert_to_ooxml(app: str, label: str) -> bool:
    """Write the big document of @p app again in the Microsoft format, by converting the one
    just written. Converting rather than building it again is what makes the two comparable:
    same content, two formats, so a difference in load time is the format's.

    Needs a desktop LibreOffice on PATH. The engine of an Online build will not do: its
    soffice.bin refuses --convert-to.
    """
    if app not in OOXML:
        return False

    odf, fmt = OOXML[app]
    source = f"{app}-{label}.{odf}"
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print(f"{app}: no soffice on PATH, so no {fmt} document", file=sys.stderr)
        return False

    with tempfile.TemporaryDirectory() as profile:
        done = subprocess.run([soffice, "--headless", "--norestore",
                               f"-env:UserInstallation=file://{profile}",
                               "--convert-to", fmt, source, "--outdir", "."],
                              cwd=str(OUT), capture_output=True, text=True)

    written = OUT / (Path(source).stem + "." + fmt)
    if done.returncode != 0 or not written.is_file():
        print(f"{app}: converting {source} to {fmt} failed: "
              f"{done.stderr.strip().splitlines()[-1:] or done.returncode}", file=sys.stderr)
        return False

    print(f"{app}: {written.name} written to {OUT}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate browser-test documents.")
    ap.add_argument("--mb", type=float, default=5.0,
                    help="approximate size of the big documents, which also names them "
                         "(default 5, so writer-5mb.odt)")
    ap.add_argument("--apps", default="writer,calc,impress,draw",
                    help="comma-separated subset to generate")
    ap.add_argument("--ooxml", action=argparse.BooleanOptionalAction, default=True,
                    help="also write the big documents in the Microsoft formats, by "
                         "converting them with a desktop LibreOffice")
    args = ap.parse_args()
    target = int(args.mb * 1024 * 1024 * 0.96)  # rest is xml/zip overhead
    label = size_label(args.mb)
    builders = {"writer": build_writer, "calc": build_calc,
                "impress": build_impress, "draw": build_draw}
    for app in [a.strip() for a in args.apps.split(",") if a.strip()]:
        build = builders[app]
        build(target, blank=True, label=label)
        build(target, blank=False, label=label)
        print(f"{app}: blank + ~{args.mb:g} MB document written to {OUT}")
        if args.ooxml:
            convert_to_ooxml(app, label)


if __name__ == "__main__":
    main()
