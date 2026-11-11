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
  - for Impress also impress-duplicate.odp, the deck the slide-duplication
    requirement is measured on: every slide carries formatted text, a
    photograph and five drawing objects (arrows, a circle, rectangles), and
    impress-slides-5mb.odp, 40 slides of the same size in all, one for each
    editor of the shared-document capacity scenario.

Size is driven by embedded raster images (ODF compresses text and cells well,
so images are what make a file big), padded until the embedded-image bytes
reach the target. The padding is noise, which no encoder can shrink, so a
target is reached in few images and few pages. --report pads the Writer
document with photographs instead and gives it a table of contents, so 10 MB
becomes dozens of sections over as many pages, which is what a document of
that size holds.

Dependencies (see documents/requirements.txt):
    python3 -m pip install odfpy Pillow matplotlib numpy
It also writes one standalone image of the same size (image-5mb.png), which the
insert-image test inserts into an open document from the local file system.

Run:
    python3 documents/gen-docs.py            # all apps, ~5 MB each, plus the image
    python3 documents/gen-docs.py --apps writer --mb 10 --report --no-ooxml
    python3 documents/gen-docs.py --mb 2     # smaller, for a quick check
"""
import argparse
import datetime
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
from PIL import Image as PILImage, ImageDraw, ImageFilter

from odf.opendocument import (OpenDocumentText, OpenDocumentSpreadsheet,
                              OpenDocumentPresentation, OpenDocumentDrawing)
from odf.style import (Style, TextProperties, ParagraphProperties,
                       GraphicProperties, MasterPage, PageLayout,
                       PageLayoutProperties, TabStops, TabStop)
from odf.text import (P, H, Span, Tab, TableOfContent, TableOfContentSource,
                      IndexTitleTemplate, TableOfContentEntryTemplate, IndexBody,
                      IndexTitle, IndexEntryLinkStart, IndexEntryChapter,
                      IndexEntryText, IndexEntryTabStop, IndexEntryPageNumber,
                      IndexEntryLinkEnd)
from odf.table import (Table, TableColumn, TableRow, TableCell, Shapes, DatabaseRanges,
                       DatabaseRange, DataPilotTables, DataPilotTable, SourceCellRange,
                       DataPilotField, DataPilotLevel, DataPilotSubtotals, DataPilotSubtotal)
from odf.style import (TableColumnProperties, TableCellProperties, ChartProperties,
                       GraphicProperties)
from odf.number import (NumberStyle, Number, PercentageStyle, DateStyle, Year, Month, Day,
                        Text as NumberText)
from odf.opendocument import OpenDocumentChart
from odf import chart
from odf.element import Element
from odf.draw import (Page, Frame, Image, Object as DrawObject, Rect, Ellipse, Line, Polygon,
                      TextBox, CustomShape, EnhancedGeometry)

OUT = Path(__file__).resolve().parent
RNG = random.Random(20260813)

# What one section of the report takes, for the page numbers the saved index carries.
# A render of the 10 MB document puts section n on page n, its index sharing page 1.
PAGES_PER_SECTION = 1

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


def photo_jpeg(w: int = 1600, h: int = 1100, quality: int = 85) -> bytes:
    """A photograph-like image: smooth colour fields, a few shapes and mild grain.

    photo_png is noise, which no encoder can shrink, so a target size is reached in a
    handful of huge images. A photograph compresses, so the same target takes dozens of
    them, which is what a document of that size really holds."""
    rng = np.random.default_rng(RNG.randrange(1 << 30))
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    field = np.zeros((h, w, 3), dtype=np.float32)
    for c in range(3):
        field[:, :, c] = 128 + 40 * (
            np.sin(x / rng.uniform(90, 260) + rng.uniform(0, 6))
            + np.cos(y / rng.uniform(90, 260) + rng.uniform(0, 6))
            + np.sin((x + y) / rng.uniform(140, 420)))
    field += rng.normal(0, 6, size=(h, w, 3))
    img = PILImage.fromarray(np.clip(field, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(img)
    for _ in range(14):
        x0, y0 = RNG.randrange(w), RNG.randrange(h)
        x1, y1 = RNG.randrange(w), RNG.randrange(h)
        col = tuple(RNG.randrange(256) for _ in range(3))
        draw.ellipse([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
                     outline=col, width=6)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
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


def frame(doc, picture: bytes, w_cm: float, h_cm: float, mime: str = "image/png",
          **kw) -> Frame:
    href = doc.addPictureFromString(picture, mime)
    f = Frame(width="%.1fcm" % w_cm, height="%.1fcm" % h_cm, **kw)
    f.addElement(Image(href=href))
    return f


def table_of_contents(doc, headings: list[str]) -> TableOfContent:
    """A table of contents over @p headings, entries and all.

    The entries are written out, not left to the reader's application: an index in a saved
    document holds the last rendering of itself, and one that opens empty until the reader
    updates it costs no layout, which is the work worth measuring here. The page numbers are
    an estimate for the same reason.
    """
    head = Style(name="Contents Heading", family="paragraph")
    head.addElement(TextProperties(fontsize="16pt", fontweight="bold"))
    doc.styles.addElement(head)

    entry = Style(name="Contents 1", family="paragraph")
    stops = TabStops()
    stops.addElement(TabStop(position="17cm", type="right", leaderstyle="dotted",
                             leadertext="."))
    properties = ParagraphProperties()
    properties.addElement(stops)
    entry.addElement(properties)
    doc.styles.addElement(entry)

    toc = TableOfContent(name="Table of Contents1")
    source = TableOfContentSource(outlinelevel=10)
    title = IndexTitleTemplate(stylename=head)
    title.addText("Table of Contents")
    source.addElement(title)
    template = TableOfContentEntryTemplate(outlinelevel=1, stylename=entry)
    for part in (IndexEntryLinkStart(), IndexEntryChapter(), IndexEntryText(),
                 IndexEntryTabStop(type="right", leaderchar="."),
                 IndexEntryPageNumber(), IndexEntryLinkEnd()):
        template.addElement(part)
    source.addElement(template)
    toc.addElement(source)

    body = IndexBody()
    index_title = IndexTitle(name="Table of Contents1_Head")
    index_title.addElement(P(stylename=head, text="Table of Contents"))
    body.addElement(index_title)
    for n, heading in enumerate(headings):
        line = P(stylename=entry)
        line.addText(heading)
        line.addElement(Tab())
        line.addText(str(1 + n * PAGES_PER_SECTION))
        body.addElement(line)
    toc.addElement(body)
    return toc


# --------------------------------------------------------------------------

def build_writer(target: int, blank: bool, label: str, report: bool = False) -> None:
    doc = OpenDocumentText()
    h1 = Style(name="Heading 1", family="paragraph")
    h1.addElement(TextProperties(fontsize="20pt", fontweight="bold"))
    doc.styles.addElement(h1)
    if blank:
        doc.text.addElement(P(text=""))
        doc.save(str(OUT / "blank.odt"))
        return
    embedded, n = 0, 0
    headings = []
    while embedded < target:
        heading = f"Section {n + 1}"
        headings.append(heading)
        doc.text.addElement(H(outlinelevel=1, stylename=h1, text=heading))
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
        picture = photo_jpeg() if report else photo_png()
        p = P()
        p.addElement(frame(doc, picture, 14, 9,
                           mime="image/jpeg" if report else "image/png",
                           anchortype="paragraph"))
        doc.text.addElement(p)
        embedded += len(picture)
        n += 1
    if report:
        doc.text.insertBefore(table_of_contents(doc, headings), doc.text.childNodes[0])
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
        # ODF declares the columns of a sheet before its rows, as office suites write them.
        tbl.addElement(TableColumn(numbercolumnsrepeated=12))
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


# calc-finance.ods: the sales ledger of a company over three years, the way a finance team keeps
# one. The Sales sheet has one row per order line, with formulas for the year, the quarter, the
# revenue, the cost and the margin. The Pivot sheet holds a pivot table of the revenue by region,
# product line and year. The Charts sheet has four charts over SUMIFS tables of the ledger, so an
# edit of the ledger recalculates the tables and redraws the charts. Every value is derived from a
# fixed seed, so the document is the same on every machine.
FINANCE_ROWS = 12000
FINANCE_YEARS = (2023, 2024, 2025)
FINANCE_REGIONS = {
    "Europe": (0.38, ["France", "Germany", "Italy", "Spain", "United Kingdom", "Netherlands"]),
    "North America": (0.34, ["United States", "Canada"]),
    "Asia Pacific": (0.18, ["Japan", "Australia", "Singapore"]),
    "Latin America": (0.10, ["Brazil", "Mexico"]),
}
# product line, product, list price and unit cost in the first year, units of one order line
FINANCE_PRODUCTS = [
    ("Hardware", "Laptop 14", 1150.0, 790.0, (1, 40)),
    ("Hardware", "Laptop 16", 1590.0, 1080.0, (1, 25)),
    ("Hardware", "Monitor 27", 380.0, 245.0, (2, 60)),
    ("Hardware", "Docking station", 240.0, 130.0, (2, 60)),
    ("Software", "Office suite licence", 180.0, 25.0, (5, 250)),
    ("Software", "Security suite", 95.0, 12.0, (5, 300)),
    ("Software", "Backup service", 60.0, 14.0, (5, 200)),
    ("Services", "Installation", 450.0, 290.0, (1, 12)),
    ("Services", "Training day", 900.0, 520.0, (1, 8)),
    ("Services", "Support contract", 1200.0, 610.0, (1, 15)),
    ("Accessories", "Headset", 85.0, 38.0, (5, 120)),
    ("Accessories", "Keyboard", 55.0, 22.0, (5, 150)),
    ("Accessories", "Mouse", 30.0, 11.0, (5, 200)),
]
# channel, share of the order lines, and the range of its discount
FINANCE_CHANNELS = [("Direct", 0.45, (0.0, 0.10)), ("Partner", 0.35, (0.15, 0.25)),
                    ("Online", 0.20, (0.0, 0.05))]
# the orders of each month against an average month: a quiet August, a strong fourth quarter
FINANCE_SEASON = [0.85, 0.9, 1.0, 0.95, 1.0, 1.0, 0.9, 0.7, 1.05, 1.05, 1.15, 1.4]
FINANCE_COLOURS = ["#2a6fdb", "#e8833a", "#3aa76d", "#c43c5c"]
FINANCE_HEADERS = ["Order date", "Order ID", "Year", "Quarter", "Region", "Country", "Channel",
                   "Product line", "Product", "Units", "Unit price", "Discount", "Revenue",
                   "Unit cost", "Cost", "Gross margin", "Margin %"]


def finance_ledger() -> list[dict]:
    """The order lines of the ledger, sorted by date, with the values that the formulas of the
    Sales sheet compute."""
    rng = random.Random(20261111)
    months = [(y, m) for y in FINANCE_YEARS for m in range(1, 13)]
    # 9% more orders each year
    weights = [FINANCE_SEASON[m - 1] * 1.09 ** (y - FINANCE_YEARS[0]) for y, m in months]
    total = sum(weights)
    counts = [round(FINANCE_ROWS * w / total) for w in weights]
    counts[-1] += FINANCE_ROWS - sum(counts)
    regions = list(FINANCE_REGIONS)
    region_weights = [FINANCE_REGIONS[r][0] for r in regions]
    rows, serial = [], {}
    for (y, m), count in zip(months, counts):
        last = (datetime.date(y + (m == 12), m % 12 + 1, 1) - datetime.timedelta(days=1)).day
        for day in sorted(rng.randint(1, last) for _ in range(count)):
            region = rng.choices(regions, region_weights)[0]
            line, product, price, cost, (low, high) = rng.choice(FINANCE_PRODUCTS)
            channel, _, (dlow, dhigh) = rng.choices(
                FINANCE_CHANNELS, [c[1] for c in FINANCE_CHANNELS])[0]
            serial[y] = serial.get(y, 0) + 1
            grow = y - FINANCE_YEARS[0]
            units = rng.randint(low, high)
            unit_price = round(price * 1.03 ** grow, 2)
            discount = round(rng.uniform(dlow, dhigh) * 200) / 200
            unit_cost = round(cost * 1.02 ** grow, 2)
            revenue = units * unit_price * (1 - discount)
            rows.append({
                "date": datetime.date(y, m, day), "id": f"SO-{y}-{serial[y]:05d}", "year": y,
                "quarter": f"Q{(m + 2) // 3}", "region": region,
                "country": rng.choice(FINANCE_REGIONS[region][1]), "channel": channel,
                "line": line, "product": product, "units": units, "price": unit_price,
                "discount": discount, "revenue": revenue, "unit_cost": unit_cost,
                "cost": units * unit_cost,
            })
    return rows


def _finance_styles(doc) -> dict[str, Style]:
    """The cell and column styles of the finance workbook, by a short name."""
    def data_style(style, *parts):
        for part in parts:
            style.addElement(part)
        doc.automaticstyles.addElement(style)
        return style

    data_style(NumberStyle(name="NAmount"),
               Number(decimalplaces=2, minintegerdigits=1, grouping="true"))
    data_style(NumberStyle(name="NCount"), Number(decimalplaces=0, minintegerdigits=1))
    data_style(PercentageStyle(name="NPercent"),
               Number(decimalplaces=1, minintegerdigits=1), NumberText(text="%"))
    data_style(DateStyle(name="NDate"), Year(style="long"), NumberText(text="-"),
               Month(style="long"), NumberText(text="-"), Day(style="long"))
    data_style(DateStyle(name="NMonth"), Month(textual="true"), NumberText(text=" "),
               Year(style="long"))
    styles = {}
    for name, data in (("amount", "NAmount"), ("count", "NCount"), ("percent", "NPercent"),
                       ("date", "NDate"), ("month", "NMonth")):
        style = Style(name=f"c{name}", family="table-cell", datastylename=data)
        doc.automaticstyles.addElement(style)
        styles[name] = style
    header = Style(name="cheader", family="table-cell")
    header.addElement(TextProperties(fontweight="bold"))
    header.addElement(TableCellProperties(backgroundcolor="#dde4ee"))
    doc.automaticstyles.addElement(header)
    styles["header"] = header
    title = Style(name="ctitle", family="table-cell")
    title.addElement(TextProperties(fontweight="bold", fontsize="14pt"))
    doc.automaticstyles.addElement(title)
    styles["title"] = title
    for width in ("2.2cm", "2.6cm", "3.4cm"):
        style = Style(name=f"col{width.replace('.', '')}", family="table-column")
        style.addElement(TableColumnProperties(columnwidth=width))
        doc.automaticstyles.addElement(style)
        styles[width] = style
    return styles


CALCEXT = "urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0"


def _calcext(name: str, **attributes) -> Element:
    """An element of the namespace that LibreOffice writes conditional formats in, which odfpy
    does not know. The keyword names take "_" for "-"."""
    return Element(qname=(CALCEXT, name), check_grammar=False,
                   qattributes={(CALCEXT, k.replace("_", "-")): str(v)
                                for k, v in attributes.items()})


def _conditional_formats(doc, sheet: str, last: int) -> Element:
    """The conditional formats of the @p sheet of the finance workbook, whose data ends in row
    @p last: what a finance team marks in a ledger and in its summary."""
    formats = _calcext("conditional-formats")

    def add(target: str, rule: Element) -> None:
        fmt = _calcext("conditional-format", target_range_address=target)
        fmt.addElement(rule, check_grammar=False)
        formats.addElement(fmt, check_grammar=False)

    def entries(rule: Element, *pairs) -> Element:
        for kind, value, *colour in pairs:
            if colour:
                rule.addElement(_calcext("color-scale-entry", value=value, type=kind,
                                         color=colour[0]), check_grammar=False)
            else:
                rule.addElement(_calcext("formatting-entry", value=value, type=kind),
                                check_grammar=False)
        return rule

    if sheet == "Sales":
        # margin %: red for the lowest, green for the highest
        add(f"Sales.Q2:Sales.Q{last}", entries(
            _calcext("color-scale"), ("minimum", 0, "#f8696b"), ("percentile", 50, "#ffeb84"),
            ("maximum", 0, "#63be7b")))
        # revenue: a bar in proportion to the amount
        add(f"Sales.M2:Sales.M{last}", entries(
            _calcext("data-bar", positive_color="#638ec6", negative_color="#ff0000",
                     axis_color="#000000", axis_position="automatic", max_length=100),
            ("auto-minimum", 0), ("auto-maximum", 0)))
        # discounts of 20% and more, and the large orders
        add(f"Sales.L2:Sales.L{last}", _calcext(
            "condition", apply_style_name="High discount", value=">=0.2",
            base_cell_address="Sales.L2"))
        add(f"Sales.J2:Sales.J{last}", _calcext(
            "condition", apply_style_name="Large order", value=">=100",
            base_cell_address="Sales.J2"))
    else:
        # the margin % of each month against the others
        add(f"Charts.R4:Charts.R{last}", entries(
            _calcext("icon-set", icon_set_type="3Arrows"),
            ("percent", 0), ("percent", 33), ("percent", 67)))
    return formats


def _named_cell_styles(doc) -> None:
    """The cell styles that the conditions of the finance workbook apply, which a condition names
    and so must be common styles."""
    for name, colour, background in (("High discount", "#9c0006", "#ffc7ce"),
                                     ("Large order", "#006100", "#c6efce")):
        style = Style(name=name, family="table-cell")
        style.addElement(TextProperties(color=colour, fontweight="bold"))
        style.addElement(TableCellProperties(backgroundcolor=background))
        doc.styles.addElement(style)


def _cell(value=None, style=None, formula=None):
    """A cell of @p value: a str, a float or int, or a datetime.date."""
    kw = {"stylename": style} if style is not None else {}
    if formula:
        kw["formula"] = formula
    if value is None:
        return TableCell(**kw)
    if isinstance(value, datetime.date):
        cell = TableCell(valuetype="date", datevalue=value.isoformat(), **kw)
        text = value.isoformat()
    elif isinstance(value, str):
        cell = TableCell(valuetype="string", **({"stringvalue": value} if formula else {}), **kw)
        text = value
    elif (isinstance(value, float) and style is not None
          and style.getAttribute("name") == "cpercent"):
        cell = TableCell(valuetype="percentage", value=value, **kw)
        text = f"{100 * value:.1f}%"
    else:
        cell = TableCell(valuetype="float", value=value, **kw)
        text = f"{value:.2f}" if isinstance(value, float) else str(value)
    cell.addElement(P(text=text))
    return cell


def _columns(table, styles, widths):
    for width in widths:
        table.addElement(TableColumn(stylename=styles[width]))


def _finance_chart(doc, shapes, title, kind, x, y, categories, series, labels_row):
    """A chart over cells of the Charts sheet. @p categories is (first row, last row, column)
    of the category names, @p series the columns of the values in those rows, and row
    @p labels_row holds the name of each series."""
    first, last, cat_col = categories
    obj = OpenDocumentChart()
    axis_style = Style(name="axis", family="chart")
    axis_style.addElement(ChartProperties(displaylabel="true"))
    axis_style.addElement(TextProperties(fontsize="8pt"))
    obj.automaticstyles.addElement(axis_style)
    series_styles = []
    for n, colour in enumerate(FINANCE_COLOURS[:len(series)]):
        style = Style(name=f"series{n}", family="chart")
        style.addElement(GraphicProperties(stroke="solid", strokecolor=colour,
                                           strokewidth="0.05cm", fillcolor=colour))
        if kind == "line":
            style.addElement(ChartProperties(symboltype="none"))
        obj.automaticstyles.addElement(style)
        series_styles.append(style)
    root = chart.Chart(attributes={"class": f"chart:{kind}", "width": "15cm", "height": "9cm"})
    t = chart.Title()
    t.addElement(P(text=title))
    root.addElement(t)
    root.addElement(chart.Legend(attributes={"legendposition": "bottom"}))
    ranges = [f"Charts.{cat_col}{labels_row}:Charts.{cat_col}{last}"]
    ranges += [f"Charts.{c}{labels_row}:Charts.{c}{last}" for c in series]
    area = chart.PlotArea(attributes={"cellrangeaddress": " ".join(ranges),
                                      "datasourcehaslabels": "both"})
    xaxis = chart.Axis(dimension="x", name="primary-x", stylename=axis_style)
    xaxis.addElement(chart.Categories(
        cellrangeaddress=f"Charts.{cat_col}{first}:Charts.{cat_col}{last}"))
    area.addElement(xaxis)
    yaxis = chart.Axis(dimension="y", name="primary-y", stylename=axis_style)
    yaxis.addElement(chart.Grid(attributes={"class": "major"}))
    area.addElement(yaxis)
    for c, style in zip(series, series_styles):
        attributes = {"valuescellrangeaddress": f"Charts.{c}{first}:Charts.{c}{last}",
                      "labelcelladdress": f"Charts.{c}{labels_row}", "class": f"chart:{kind}"}
        # A pie colours each slice on its own.
        if kind != "circle":
            attributes["stylename"] = style
        area.addElement(chart.Series(attributes=attributes))
    root.addElement(area)
    obj.chart.addElement(root)
    href = doc.addObject(obj)
    f = Frame(width="15cm", height="9cm", x=x, y=y)
    f.addElement(DrawObject(href=href, notifyonupdateofranges=" ".join(ranges)))
    shapes.addElement(f)


def build_calc_finance() -> None:
    """documents/calc-finance.ods, see FINANCE_ROWS above."""
    rows = finance_ledger()
    last = len(rows) + 1
    doc = OpenDocumentSpreadsheet()
    st = _finance_styles(doc)
    _named_cell_styles(doc)

    # The Sales sheet: the ledger.
    sales = Table(name="Sales")
    _columns(sales, st, ["2.6cm", "3.4cm", "2.2cm", "2.2cm", "3.4cm", "3.4cm", "2.2cm", "3.4cm",
                         "3.4cm", "2.2cm", "2.6cm", "2.2cm", "2.6cm", "2.6cm", "2.6cm", "2.6cm",
                         "2.2cm"])
    head = TableRow()
    for name in FINANCE_HEADERS:
        head.addElement(_cell(name, st["header"]))
    sales.addElement(head)
    for i, r in enumerate(rows, start=2):
        row = TableRow()
        margin = r["revenue"] - r["cost"]
        for value, style, formula in (
                (r["date"], st["date"], None),
                (r["id"], None, None),
                (r["year"], None, f"of:=YEAR([.A{i}])"),
                (r["quarter"], None, f'of:="Q"&ROUNDUP(MONTH([.A{i}])/3;0)'),
                (r["region"], None, None), (r["country"], None, None),
                (r["channel"], None, None), (r["line"], None, None),
                (r["product"], None, None),
                (r["units"], st["count"], None),
                (r["price"], st["amount"], None),
                (r["discount"], st["percent"], None),
                (r["revenue"], st["amount"], f"of:=[.J{i}]*[.K{i}]*(1-[.L{i}])"),
                (r["unit_cost"], st["amount"], None),
                (r["cost"], st["amount"], f"of:=[.J{i}]*[.N{i}]"),
                (margin, st["amount"], f"of:=[.M{i}]-[.O{i}]"),
                (margin / r["revenue"] if r["revenue"] else 0.0, st["percent"],
                 f"of:=IF([.M{i}]=0;0;[.P{i}]/[.M{i}])")):
            row.addElement(_cell(value, style, formula))
        sales.addElement(row)
    sales.addElement(_conditional_formats(doc, "Sales", last), check_grammar=False)
    doc.spreadsheet.addElement(sales)

    # The Pivot sheet: the revenue by region and product line, one column per year. The cells
    # hold the output that the pivot table computes, as an office suite saves it.
    regions = sorted(FINANCE_REGIONS)
    lines = sorted({p[0] for p in FINANCE_PRODUCTS})
    sums: dict[tuple, float] = {}
    for r in rows:
        sums[(r["region"], r["line"], r["year"])] = (
            sums.get((r["region"], r["line"], r["year"]), 0.0) + r["revenue"])
    pivot = Table(name="Pivot")
    _columns(pivot, st, ["3.4cm", "3.4cm", "2.6cm", "2.6cm", "2.6cm", "2.6cm"])

    def pivot_row(*cells):
        row = TableRow()
        for c in cells:
            row.addElement(c)
        pivot.addElement(row)

    pivot_row(_cell("Sum - Revenue"), _cell(), _cell("Year"))
    pivot_row(_cell("Region"), _cell("Product line"),
              *[_cell(y) for y in FINANCE_YEARS], _cell("Total Result"))
    for region in regions:
        for n, line in enumerate(lines):
            values = [sums.get((region, line, y), 0.0) for y in FINANCE_YEARS]
            pivot_row(_cell(region) if n == 0 else _cell(), _cell(line),
                      *[_cell(v, st["amount"]) for v in values + [sum(values)]])
        values = [sum(sums.get((region, line, y), 0.0) for line in lines) for y in FINANCE_YEARS]
        pivot_row(_cell(f"{region} Result"), _cell(),
                  *[_cell(v, st["amount"]) for v in values + [sum(values)]])
    values = [sum(v for (_, _, y), v in sums.items() if y == year) for year in FINANCE_YEARS]
    pivot_row(_cell("Total Result"), _cell(),
              *[_cell(v, st["amount"]) for v in values + [sum(values)]])
    pivot_last = 2 + len(regions) * (len(lines) + 1) + 1
    doc.spreadsheet.addElement(pivot)

    # The Charts sheet: four charts on the left, over SUMIFS tables of the ledger on the right.
    charts = Table(name="Charts")
    shapes = Shapes()
    charts.addElement(shapes)
    _columns(charts, st, ["2.6cm"] * 13 + ["2.6cm", "3.4cm", "3.4cm", "3.4cm", "2.2cm", "2.2cm",
                                            "3.4cm", "3.4cm", "3.4cm", "3.4cm"])
    def rng(col: str) -> str:
        """The cells of column @p col of the ledger, below its header."""
        return f"[$Sales.${col}$2:.${col}${last}]"

    months = [datetime.date(y, m, 1) for y in FINANCE_YEARS for m in range(1, 13)]
    monthly = {}
    for r in rows:
        key = r["date"].replace(day=1)
        rev, cost = monthly.get(key, (0.0, 0.0))
        monthly[key] = (rev + r["revenue"], cost + r["cost"])
    by_region = {(r_, y): 0.0 for r_ in regions for y in FINANCE_YEARS}
    by_channel = {c[0]: 0.0 for c in FINANCE_CHANNELS}
    for r in rows:
        by_region[(r["region"], r["year"])] += r["revenue"]
        by_channel[r["channel"]] += r["revenue"]

    # row 1 the title, row 3 the headers, rows 4 to 39 the months
    title_row = [_cell("Sales summary", st["title"])]
    header = [_cell(h, st["header"]) for h in ("Month", "Revenue", "Cost", "Gross margin",
                                               "Margin %")]
    header += [_cell(), _cell("Region", st["header"])]
    header += [_cell(y, st["header"]) for y in FINANCE_YEARS]
    body = []
    for n, month in enumerate(months):
        i = 4 + n
        rev, cost = monthly.get(month, (0.0, 0.0))
        when = (f'{rng("A")};">="&[.N{i}];{rng("A")};"<"&EDATE([.N{i}];1)')
        cells = [_cell(month, st["month"]),
                 _cell(rev, st["amount"], f"of:=SUMIFS({rng('M')};{when})"),
                 _cell(cost, st["amount"], f"of:=SUMIFS({rng('O')};{when})"),
                 _cell(rev - cost, st["amount"], f"of:=[.O{i}]-[.P{i}]"),
                 _cell((rev - cost) / rev if rev else 0.0, st["percent"],
                       f"of:=IF([.O{i}]=0;0;[.Q{i}]/[.O{i}])"),
                 _cell()]
        if n < len(regions):
            region = regions[n]
            cells.append(_cell(region))
            for col, y in zip("UVW", FINANCE_YEARS):
                cells.append(_cell(by_region[(region, y)], st["amount"],
                                   f"of:=SUMIFS({rng('M')};{rng('E')};[.$T{i}];"
                                   f"{rng('C')};[.{col}$3])"))
        elif n == 6:
            cells += [_cell("Channel", st["header"]), _cell("Revenue", st["header"])]
        elif 7 <= n < 7 + len(FINANCE_CHANNELS):
            channel = FINANCE_CHANNELS[n - 7][0]
            cells += [_cell(channel), _cell(by_channel[channel], st["amount"],
                                            f"of:=SUMIFS({rng('M')};{rng('G')};[.$T{i}])")]
        body.append(cells)
    # The tables start in column N, right of the charts.
    for n, cells in enumerate([title_row, [], header] + body):
        row = TableRow()
        for c in cells if n == 0 else [_cell() for _ in range(13)] + cells:
            row.addElement(c)
        charts.addElement(row)
    end = 3 + len(months)
    _finance_chart(doc, shapes, "Monthly revenue and gross margin", "line", "0.3cm", "1.2cm",
                   (4, end, "N"), ["O", "Q"], 3)
    _finance_chart(doc, shapes, "Revenue by region", "bar", "15.8cm", "1.2cm",
                   (4, 3 + len(regions), "T"), ["U", "V", "W"], 3)
    _finance_chart(doc, shapes, "Revenue by channel", "circle", "0.3cm", "10.7cm",
                   (11, 10 + len(FINANCE_CHANNELS), "T"), ["U"], 10)
    _finance_chart(doc, shapes, "Gross margin %", "line", "15.8cm", "10.7cm",
                   (4, end, "N"), ["R"], 3)
    charts.addElement(_conditional_formats(doc, "Charts", end), check_grammar=False)
    doc.spreadsheet.addElement(charts)

    ranges = DatabaseRanges()
    ranges.addElement(DatabaseRange(name="__Anonymous_Sheet_DB__0",
                                    targetrangeaddress=f"Sales.A1:Sales.Q{last}",
                                    displayfilterbuttons="true"))
    doc.spreadsheet.addElement(ranges)
    pilots = DataPilotTables()
    table = DataPilotTable(name="Revenue by region",
                           targetrangeaddress=f"Pivot.A1:Pivot.F{pivot_last}",
                           buttons="Pivot.A2 Pivot.B2 Pivot.C1", showfilterbutton="false")
    table.addElement(SourceCellRange(cellrangeaddress=f"Sales.A1:Sales.Q{last}"))
    for name, orientation in (("Region", "row"), ("Product line", "row"), ("Year", "column")):
        field = DataPilotField(sourcefieldname=name, orientation=orientation, usedhierarchy="-1")
        level = DataPilotLevel(showempty="false")
        subtotals = DataPilotSubtotals()
        subtotals.addElement(DataPilotSubtotal(function="auto"))
        level.addElement(subtotals)
        field.addElement(level)
        table.addElement(field)
    table.addElement(DataPilotField(sourcefieldname="Revenue", orientation="data", function="sum",
                                    usedhierarchy="-1"))
    table.addElement(DataPilotField(sourcefieldname="", isdatalayoutfield="true",
                                    orientation="column", usedhierarchy="-1"))
    pilots.addElement(table)
    doc.spreadsheet.addElement(pilots)
    doc.save(str(OUT / "calc-finance.ods"))


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


# The deck of the shared-document capacity scenario: one editor types on each slide, and Impress
# locks a text to the view that edits it, so the deck needs a slide for each editor. 160 users
# at one editor in 10 take 16 slides; 40 leave room.
SHARED_SLIDES = 40


def build_slide_deck(target: int, label: str) -> None:
    """impress-slides-<label>.odp: SHARED_SLIDES slides of a title, a body text and an image,
    the images sized so that the deck weighs about as much as impress-<label>.odp."""
    doc = OpenDocumentPresentation()
    _draw_master(doc)
    # the size of the images, from what one trial image takes a pixel
    per = target // SHARED_SLIDES
    trial = len(photo_png(300, 214)) / (300 * 214)
    w = int((per / trial * 1.4) ** 0.5)
    h = int(w / 1.4)
    for n in range(SHARED_SLIDES):
        page = Page(masterpagename="Standard")
        # the title comes first, so that Tab selects it
        title = Frame(width="24cm", height="3cm", x="2cm", y="1cm")
        tb = TextBox()
        tb.addElement(P(text=f"Slide {n + 1}: {prose(4)}"))
        title.addElement(tb)
        page.addElement(title)
        body = Frame(width="12cm", height="12cm", x="2cm", y="5cm")
        btb = TextBox()
        for _ in range(4):
            btb.addElement(P(text=prose(12)))
        body.addElement(btb)
        page.addElement(body)
        page.addElement(frame(doc, photo_png(w, h), 11, 8, x="15cm", y="6cm"))
        doc.presentation.addElement(page)
    doc.save(str(OUT / f"impress-slides-{label}.odp"))


# The deck the slide-duplication requirement is measured on. It is not sized: what matters is
# what one slide holds, which the requirement spells out as formatted text, a photo and five
# drawing objects such as arrows, circles and rectangles. Every slide is built the same way,
# so whichever slide is current when the test duplicates it, it duplicates that.
DUPLICATE_SLIDES = 12


def _text_style(doc, name: str, **props) -> Style:
    st = Style(name=name, family="text")
    st.addElement(TextProperties(**props))
    doc.automaticstyles.addElement(st)
    return st


def _graphic_style(doc, name: str, **props) -> Style:
    st = Style(name=name, family="graphic")
    st.addElement(GraphicProperties(**props))
    doc.automaticstyles.addElement(st)
    return st


def build_impress_duplicate() -> None:
    doc = OpenDocumentPresentation()
    _draw_master(doc)

    bold = _text_style(doc, "TBold", fontweight="bold")
    italic = _text_style(doc, "TItalic", fontstyle="italic")
    red = _text_style(doc, "TRed", color="#c9211e", fontweight="bold")
    blue = _text_style(doc, "TBlue", color="#2a6099", textunderlinestyle="solid",
                       textunderlinewidth="auto", textunderlinecolor="font-color")
    big = Style(name="PTitle", family="paragraph")
    big.addElement(TextProperties(fontsize="32pt", fontweight="bold"))
    doc.automaticstyles.addElement(big)
    body = Style(name="PBody", family="paragraph")
    body.addElement(TextProperties(fontsize="18pt"))
    doc.automaticstyles.addElement(body)

    # One graphic style per drawing object, so each is its own colour, the way a hand-drawn
    # slide is.
    # Text frames are plain, so the fill a default graphic style would give them does not
    # read as a sixth drawing object.
    plain = _graphic_style(doc, "GText", fill="none", stroke="none")
    fills = ["#3465a4", "#e8a202", "#77bc65", "#ff6d6d", "#8e86ae"]
    shapes = [_graphic_style(doc, f"GShape{i}", fill="solid", fillcolor=c, stroke="solid",
                             strokecolor="#1c1c1c", strokewidth="0.05cm")
              for i, c in enumerate(fills)]

    def arrow(kind: str, style: Style, **kw) -> CustomShape:
        shape = CustomShape(stylename=style, **kw)
        shape.addElement(EnhancedGeometry(type=kind))
        return shape

    for n in range(DUPLICATE_SLIDES):
        page = Page(masterpagename="Standard")

        title = Frame(stylename=plain, width="24cm", height="2.5cm", x="2cm", y="0.8cm")
        tb = TextBox()
        head = P(stylename=big)
        head.addText(f"Slide {n + 1}: ")
        head.addElement(Span(stylename=red, text=prose(2).rstrip(".")))
        tb.addElement(head)
        title.addElement(tb)
        page.addElement(title)

        text = Frame(stylename=plain, width="12cm", height="9cm", x="2cm", y="4cm")
        tb = TextBox()
        for _ in range(3):
            para = P(stylename=body)
            para.addElement(Span(stylename=bold, text=prose(3) + " "))
            para.addText(prose(6) + " ")
            para.addElement(Span(stylename=italic, text=prose(4) + " "))
            para.addElement(Span(stylename=blue, text=prose(3)))
            tb.addElement(para)
        text.addElement(tb)
        page.addElement(text)

        page.addElement(frame(doc, photo_jpeg(1200, 800), 11, 7.3, mime="image/jpeg",
                              x="15cm", y="4cm"))

        # The five drawing objects: two arrows, a circle, two rectangles.
        page.addElement(arrow("right-arrow", shapes[0], width="4cm", height="1.6cm",
                              x="2cm", y="16.6cm"))
        page.addElement(arrow("left-arrow", shapes[1], width="4cm", height="1.6cm",
                              x="7cm", y="16.6cm"))
        page.addElement(Ellipse(stylename=shapes[2], width="3cm", height="3cm",
                                x="12cm", y="15.9cm"))
        page.addElement(Rect(stylename=shapes[3], width="4.5cm", height="2.5cm",
                             x="16cm", y="16.1cm"))
        page.addElement(Rect(stylename=shapes[4], width="4.5cm", height="2.5cm",
                             x="21.5cm", y="16.1cm"))
        doc.presentation.addElement(page)

    doc.save(str(OUT / "impress-duplicate.odp"))


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


def build_image(target: int, label: str) -> None:
    """A single photograph of about the target size, for the insert-image test.

    That test inserts a file from the local file system, so what has to be right is
    the size of the file on disk. Noise is what no encoder can shrink, so the
    encoded PNG lands close to the target, and each pass corrects the pixel count
    from what the encoder actually produced.
    """
    data = b""
    pixels = target / 3.05  # random RGB costs about that many bytes per pixel as PNG
    for _ in range(4):
        width = max(64, round((pixels * 1.4) ** 0.5))  # 1.4:1, a landscape photograph
        height = max(64, round(width / 1.4))
        data = photo_png(width, height)
        if abs(len(data) - target) <= target * 0.02:
            break
        pixels *= target / len(data)
    path = OUT / f"image-{label}.png"
    path.write_bytes(data)
    print(f"image: {path.name} written to {OUT} "
          f"({len(data) / (1024 * 1024):.1f} MB, {width}x{height})")


def convert_to_ooxml(app: str, label: str) -> bool:
    """Write the big document of @p app again in the Microsoft format, by converting the one
    just written. Converting rather than building it again is what makes the two comparable:
    same content, two formats, so a difference in load time is the format's.
    """
    if app not in OOXML:
        return False
    odf, fmt = OOXML[app]
    return convert_document(app, f"{app}-{label}.{odf}", fmt)


def convert_document(app: str, source: str, fmt: str) -> bool:
    """Write @p source of documents/ again in the format @p fmt, beside it.

    Needs a desktop LibreOffice on PATH. The engine of an Online build will not do: its
    soffice.bin refuses --convert-to.
    """
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
    ap.add_argument("--report", action="store_true",
                    help="give the big Writer document the shape of a long report: a table "
                         "of contents, and photographs rather than the noise the other "
                         "documents pad with, so the size comes from dozens of images and "
                         "the document runs to the page count that size implies")
    ap.add_argument("--image", action=argparse.BooleanOptionalAction, default=True,
                    help="also write a standalone image of that size (image-5mb.png), "
                         "which the insert-image test inserts from the local file system")
    ap.add_argument("--ooxml", action=argparse.BooleanOptionalAction, default=True,
                    help="also write the big documents in the Microsoft formats, by "
                         "converting them with a desktop LibreOffice")
    ap.add_argument("--slide-deck-only", action="store_true",
                    help=f"write only the {SHARED_SLIDES}-slide deck of that size "
                         "(impress-slides-5mb.odp), and leave the other documents as they are")
    args = ap.parse_args()
    target = int(args.mb * 1024 * 1024 * 0.96)  # rest is xml/zip overhead
    label = size_label(args.mb)
    if args.slide_deck_only:
        build_slide_deck(target, label)
        print(f"impress: {SHARED_SLIDES}-slide impress-slides-{label}.odp written to {OUT}")
        return
    builders = {"writer": build_writer, "calc": build_calc,
                "impress": build_impress, "draw": build_draw}
    # Only Writer has a report shape, since only Writer has headings to index.
    extras = {"writer": {"report": args.report}}
    for app in [a.strip() for a in args.apps.split(",") if a.strip()]:
        build = builders[app]
        build(target, blank=True, label=label)
        build(target, blank=False, label=label, **extras.get(app, {}))
        print(f"{app}: blank + ~{args.mb:g} MB document written to {OUT}")
        if args.ooxml:
            convert_to_ooxml(app, label)
        if app == "impress":
            build_impress_duplicate()
            print(f"impress: {DUPLICATE_SLIDES}-slide impress-duplicate.odp written to {OUT}")
            build_slide_deck(target, label)
            print(f"impress: {SHARED_SLIDES}-slide impress-slides-{label}.odp written to {OUT}")
        if app == "calc":
            build_calc_finance()
            print(f"calc: calc-finance.ods written to {OUT}")
            if args.ooxml:
                convert_document(app, "calc-finance.ods", "xlsx")
    if args.image:
        build_image(target, label)


if __name__ == "__main__":
    main()
