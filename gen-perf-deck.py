#!/usr/bin/env python3
"""Generate the cool-k6 performance-testing presentation (ODP / Impress).

Collabora Purple palette, Montserrat (display) + Carlito (body). Shared chrome
(accent spine, title rule, footer + page number) lives on master pages; every
visual property traces back to the palette/font constants and named styles.
"""
import os
from odf.opendocument import OpenDocumentPresentation
from odf.style import (
    Style, TextProperties, ParagraphProperties, GraphicProperties,
    DrawingPageProperties, MasterPage, PageLayout, PageLayoutProperties,
    FontFace,
)
from odf.draw import Page, Frame, TextBox, Image, Rect
from odf.text import P, PageNumber
from odf.dc import Creator, Title
from odf.meta import InitialCreator
from odf import teletype

BASE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = "/home/meven/code/auto/jail/repos/.claude/skills/create-demo-document"
LOGO = os.path.join(SKILL_DIR, "collabora-online.png")
LOGO_WHITE = os.path.join(SKILL_DIR, "collabora-online-white.png")
PIPELINE_SVG = os.path.join(BASE, "perf-pipeline.svg")

AUTHOR = "Méven Car"
ORG = "Collabora Productivity"
DATE = "July 2026"

# --- Collabora palette --------------------------------------------------
ACCENT, ACCENT_DARK, ACCENT_LIGHT = "#5c2983", "#2e1a47", "#efe7f3"
BODY_TEXT, MID_GREY, LIGHT_GREY = "#3c3c3e", "#8b8b89", "#ced0d2"
GREEN, WHITE = "#40ba2f", "#ffffff"
FONT_DISPLAY, FONT_BODY = "Montserrat", "Carlito"

doc = OpenDocumentPresentation()
_title = Title(); _title.addText("Performance Testing Collabora Online with k6")
_creator = Creator(); _creator.addText(AUTHOR)
_ic = InitialCreator(); _ic.addText(AUTHOR)
for e in (_title, _creator, _ic):
    doc.meta.addElement(e)

for fam in (FONT_DISPLAY, FONT_BODY):
    doc.fontfacedecls.addElement(FontFace(
        name=fam, fontfamily=fam, fontfamilygeneric="swiss", fontpitch="variable"))

# --- page layout --------------------------------------------------------
pl = PageLayout(name="SlideLayout")
pl.addElement(PageLayoutProperties(pagewidth="25.4cm", pageheight="14.29cm",
                                   printorientation="landscape"))
doc.automaticstyles.addElement(pl)

# --- drawing-page (background) styles -----------------------------------
def drawing_page(name, color):
    s = Style(name=name, family="drawing-page")
    s.addElement(DrawingPageProperties(fill="solid", fillcolor=color))
    doc.automaticstyles.addElement(s)
    return s

dp_white, dp_light, dp_accent = (drawing_page("dpWhite", WHITE),
                                 drawing_page("dpLight", ACCENT_LIGHT),
                                 drawing_page("dpAccent", ACCENT))

# --- graphic (shape fill) styles ----------------------------------------
def gstyle(name, color):
    s = Style(name=name, family="graphic")
    s.addElement(GraphicProperties(fill="solid", fillcolor=color, stroke="none"))
    doc.automaticstyles.addElement(s)
    return s

g_accent, g_light, g_green = gstyle("gAccent", ACCENT), gstyle("gLight", LIGHT_GREY), gstyle("gGreen", GREEN)
frame_style = Style(name="Frame", family="graphic")
frame_style.addElement(GraphicProperties(fill="none", stroke="none"))
doc.automaticstyles.addElement(frame_style)

# --- paragraph styles (automatic: Impress applies these to text boxes) --
def para(name, *, font, size, color, weight="normal", align="start",
         above=0.0, below=0.0, mleft=0.0, bg=None):
    s = Style(name=name, family="paragraph")
    pp = {"textalign": align}
    if above: pp["margintop"] = f"{above}cm"
    if below: pp["marginbottom"] = f"{below}cm"
    if mleft: pp["marginleft"] = f"{mleft}cm"
    if bg: pp.update(backgroundcolor=bg, padding="0.15cm")
    s.addElement(ParagraphProperties(**pp))
    s.addElement(TextProperties(fontsize=f"{size}pt", fontweight=weight,
                                color=color, fontname=font))
    doc.automaticstyles.addElement(s)
    return name

TITLE_XL  = para("TitleXL",  font=FONT_DISPLAY, size=38, color=ACCENT,     weight="bold", align="center")
SUBTITLE  = para("Subtitle", font=FONT_BODY,    size=17, color=BODY_TEXT,  align="center", above=0.4)
AUTHOR_ST = para("Author",   font=FONT_BODY,    size=14, color=MID_GREY,   align="center", above=0.5)
SLIDE_TTL = para("SlideTitle", font=FONT_DISPLAY, size=26, color=ACCENT,   weight="bold")
SEC_TTL   = para("SecTitle", font=FONT_DISPLAY, size=36, color=WHITE,      weight="bold", align="center")
SEC_SUB   = para("SecSub",   font=FONT_BODY,    size=17, color=ACCENT_LIGHT, align="center", above=0.3)
BULLET    = para("Bullet",   font=FONT_BODY,    size=17, color=BODY_TEXT,  above=0.17, below=0.17)
SUBBULLET = para("SubBullet", font=FONT_BODY,   size=14, color=MID_GREY,   above=0.06, below=0.06, mleft=1.1)
CMD       = para("Cmd",      font=FONT_BODY,    size=13, color=ACCENT_DARK, above=0.12, below=0.12, bg=ACCENT_LIGHT)
CAPTION   = para("Caption",  font=FONT_BODY,    size=12, color=MID_GREY,   align="center", above=0.3)
FOOT_L    = para("FootL",    font=FONT_BODY,    size=10, color=MID_GREY)
FOOT_R    = para("FootR",    font=FONT_BODY,    size=10, color=MID_GREY,   align="end")

# --- helpers to build shapes/frames -------------------------------------
def rect(parent, style, x, y, w, h):
    parent.addElement(Rect(stylename=style, x=f"{x}cm", y=f"{y}cm",
                           width=f"{w}cm", height=f"{h}cm"))

def frame(parent, x, y, w, h):
    f = Frame(stylename=frame_style, width=f"{w}cm", height=f"{h}cm",
              x=f"{x}cm", y=f"{y}cm")
    parent.addElement(f)
    return f

def textframe(parent, x, y, w, h, paras):
    f = frame(parent, x, y, w, h)
    tb = TextBox()
    for text, style in paras:
        p = P(stylename=style)
        teletype.addTextToElement(p, text)
        tb.addElement(p)
    f.addElement(tb)
    return f

def imageframe(parent, path, x, y, w, h):
    f = frame(parent, x, y, w, h)
    f.addElement(Image(href=doc.addPicture(path)))

# --- master pages (shared chrome) ---------------------------------------
def master_content():
    m = MasterPage(name="mContent", pagelayoutname=pl)
    rect(m, g_accent, 0, 0, 0.32, 14.29)          # left accent spine
    rect(m, g_accent, 1.4, 2.35, 2.6, 0.09)       # title accent rule
    rect(m, g_light, 1.4, 13.22, 22.6, 0.03)      # footer hairline
    textframe(m, 1.4, 13.32, 16, 0.7, [("cool-k6  ·  Performance testing", FOOT_L)])
    f = frame(m, 21.4, 13.32, 2.6, 0.7)
    tb = TextBox(); p = P(stylename=FOOT_R); p.addElement(PageNumber()); tb.addElement(p)
    f.addElement(tb)
    doc.masterstyles.addElement(m)
    return m

def master_plain(name, accent_top=None):
    m = MasterPage(name=name, pagelayoutname=pl)
    if accent_top:
        rect(m, accent_top, 0, 0, 25.4, 0.25)     # top accent band
    doc.masterstyles.addElement(m)
    return m

mContent = master_content()
mTitle = master_plain("mTitle", accent_top=g_accent)
mSection = master_plain("mSection", accent_top=g_green)

_n = 0
def page(master, dp):
    global _n
    _n += 1
    p = Page(name=f"page{_n}", masterpagename=master, stylename=dp)
    doc.presentation.addElement(p)
    return p

# ------------------------------------------------------------------ slides
def title_slide():
    pg = page(mTitle, dp_light)
    textframe(pg, 1.5, 2.7, 22.4, 3.1,
              [("Performance Testing Collabora Online with k6", TITLE_XL)])
    textframe(pg, 2.7, 6.1, 20.0, 1.6,
              [("cool-k6 - the embedded WOPI host, load testing for integrators, "
                "and two flamegraphs: the kit and the browser client",
                SUBTITLE)])
    textframe(pg, 2.7, 8.3, 20.0, 0.9,
              [(f"{AUTHOR}  ·  {ORG}  ·  {DATE}", AUTHOR_ST)])
    imageframe(pg, LOGO, 9.7, 10.6, 6.0, 2.25)

def section_slide(title, sub=None):
    pg = page(mSection, dp_accent)
    paras = [(title, SEC_TTL)] + ([(sub, SEC_SUB)] if sub else [])
    textframe(pg, 2.0, 5.2, 21.4, 4.0, paras)

def content_slide(title, items):
    pg = page(mContent, dp_white)
    textframe(pg, 1.4, 0.85, 22.4, 1.5, [(title, SLIDE_TTL)])
    kind = {"bullet": BULLET, "sub": SUBBULLET, "cmd": CMD}
    paras = [((("•  " if k == "bullet" else "–  " if k == "sub" else "") + t), kind[k])
             for t, k in items]
    textframe(pg, 1.4, 2.9, 22.4, 10.1, paras)

def pipeline_slide():
    pg = page(mContent, dp_white)
    textframe(pg, 1.4, 0.85, 22.4, 1.5, [("How it fits together", SLIDE_TTL)])
    imageframe(pg, PIPELINE_SVG, 1.6, 3.2, 22.0, 6.73)  # svg 980x300 -> 0.306
    textframe(pg, 1.4, 10.4, 22.4, 1.2, [
        ("k6 drives coolwsd through a real WOPI + WebSocket flow; perf records the kit; a flamegraph is rendered.",
         CAPTION)])

def closing_slide():
    pg = page(mSection, dp_accent)
    textframe(pg, 2.0, 4.7, 21.4, 5.0, [
        ("Launch · Measure · Profile · Track", SEC_TTL),
        ("k6 + WOPI host   │   performance testing   │   kit + browser flamegraphs   │   recurrent baselines", SEC_SUB),
        ("Thank you - questions?", SEC_SUB),
        (f"{AUTHOR}  ·  {ORG}", SEC_SUB),
    ])
    imageframe(pg, LOGO_WHITE, 10.2, 11.1, 5.0, 1.88)

# ------------------------------------------------------------------ deck
title_slide()

content_slide("What is cool-k6?", [
    ("A k6-based performance-testing suite for Collabora Online.", "bullet"),
    ("Drives a client against a running coolwsd through a real WOPI flow - end-to-end, as the client sees it.", "bullet"),
    ("Everything needed to run a test lives in the repo. Five parts in this talk:", "bullet"),
    ("k6, the embedded WOPI server, and k6-wrap - the pieces that make a test run", "sub"),
    ("Performance testing - what it is for", "sub"),
    ("k6-flamegraph - profiling where the kit spends CPU", "sub"),
    ("Puppeteer - a flamegraph of the browser client", "sub"),
    ("Recurrent runs on fixed hardware - a baseline that catches regressions", "sub"),
])

# ---- Part 1 -----------------------------------------------------------
section_slide("Part 1 - k6, the server, k6-wrap", "The pieces that make a test run")

content_slide("k6 and the cool-k6 suite", [
    ("k6 (Grafana) - an open-source load-testing tool; scenarios are ES-module JavaScript run by the k6 engine.", "bullet"),
    ("Run straight from the official container - no local k6 install needed:", "bullet"),
    ("grafana/k6:1.7.1-with-browser", "cmd"),
    ("cool-k6 wraps k6 for Collabora Online: launchers, an embedded WOPI host, example scenarios, and a profiler.", "bullet"),
    ("Scenarios live in src/*-test.js, bundled to dist/ with webpack:", "bullet"),
    ("npm run build", "cmd"),
])

content_slide("The embedded WOPI server", [
    ("A test needs an integrator (a WOPI host) to hand COOL a document - cool-k6 ships one, so nothing external is required.", "bullet"),
    ("A small Node / Express host under server/; start it with:", "bullet"),
    ("npm run server      # http on :3000 (https when SSL_KEY_FILE / SSL_CRT_FILE are set)", "cmd"),
    ("Speaks the WOPI endpoints COOL calls:", "bullet"),
    ("CheckFileInfo (GET /wopi/files/:id), GetFile (.../contents), PutFile (POST .../contents)", "sub"),
    ("/collaboraUrl proxies COOL's /hosting/discovery to find the client URL.", "sub"),
    ("Documents are listed in server/routes/files.json (fileId -> path) - add your own there.", "bullet"),
])

content_slide("k6-wrap & k6-run - launching a test", [
    ("k6-wrap runs the k6 container: mounts the repo at /app and forwards WOPI_URL / WOPI_HOST plus test env.", "bullet"),
    ("Auto-detects a broken docker bridge and falls back to --network host (override with COOL_K6_DOCKER_NETWORK).", "bullet"),
    ("k6-run is a thin launcher on top - defaults to --vus 1, adds --insecure-skip-tls-verify for self-signed certs.", "bullet"),
    ("./k6-run dist/cool-test.js", "cmd"),
    ("Extra k6 flags follow the script (no -- separator):   ./k6-run dist/cool-test.js --vus 10 --duration 5m", "bullet"),
])

# ---- Part 2 -----------------------------------------------------------
section_slide("Part 2 - Performance testing", "What it is for")

content_slide("What performance testing is for", [
    ("Measure Collabora Online end-to-end, as the client sees it, under controlled load.", "bullet"),
    ("Smoke check - did a change break or slow the open / edit path?", "bullet"),
    ("Capacity - raise the VUs and watch where latency starts to degrade.", "bullet"),
    ("Regression tracking - keep an eye on the same key timings over time.", "bullet"),
    ("Integrator sizing - how much load can this deployment take?", "bullet"),
    ("It reports how long, not why - pair a moved number with server logs and metrics.", "bullet"),
])

content_slide("The metrics that matter", [
    ("frame_loading_time - time until the editor is usable (the headline number).", "bullet"),
    ("http_req_duration p(95) - the user-visible latency target to track.", "bullet"),
    ("checks - any failure means discard that iteration's timings.", "bullet"),
    ("page_loading_time, browser_web_vital_* - full editor load (browser scenarios).", "bullet"),
    ("data_sent / data_received, iteration_duration - volume and wall-clock per run.", "bullet"),
])

content_slide("Two ways to drive the load", [
    ("Network scenario - talks the WebSocket protocol directly via CoolClientWs.", "bullet"),
    ("Lightest, most VUs per host; measures throughput and shell timing.", "sub"),
    ("cool-test.js - open the document, type, close.", "sub"),
    ("Browser scenario - a real Chromium via k6-browser.", "bullet"),
    ("Heavier, fewer VUs; measures time-to-interactive and web vitals.", "sub"),
    ("cool-browser-test.js - waits on the App_LoadingStatus postMessage stream.", "sub"),
    ("Bring your own WOPI host + document; the bundled ones are templates.", "bullet"),
])

# ---- Part 3 -----------------------------------------------------------
section_slide("Part 3 - k6-flamegraph", "Its purpose")

content_slide("What k6-flamegraph is for", [
    ("Performance testing says how long; the flamegraph says where the CPU went.", "bullet"),
    ("One command drives a k6 workload against a locally launched coolwsd + WOPI host and profiles the kit.", "bullet"),
    ("./k6-flamegraph.py --browser-runner k6 --no-browser-profile dist/cool-load-document-test.js", "cmd"),
    ("Outputs: flamegraph.svg, perf.data, k6 / coolwsd / wopi logs, perf-stat.csv.", "bullet"),
    ("Turns \"it feels slow\" into a named function you can go and fix.", "bullet"),
])

content_slide("Why the kit is hard to profile", [
    ("The document's kit is forked only when a client connects - after perf would normally have started.", "bullet"),
    ("It jails itself into new user / mount namespaces, so perf --pid inheritance does not follow it.", "bullet"),
    ("So: launch the workload first, wait for the live document kit (skip the idle kit_spare_* spares), then attach perf by name.", "bullet"),
    ("Whole-system -a would catch the late kit but can't symbolise the jail - target the live pid instead.", "bullet"),
])

pipeline_slide()

content_slide("A workload the kit can't sleep through", [
    ("A CPU flamegraph needs the kit busy - a human 20 ms/char cadence, or a stalled socket, profiles as idle.", "bullet"),
    ("Reliable workload: open a large document and scroll it, pulling every tile - import, layout and paint.", "bullet"),
    ("cool-load-document-test.js - a ~10 MB mixed doc (text, images, tables, graphs), driven from event-loop timers so the frames actually flush.", "bullet"),
    ("Example finding: rendering is dominated by image scaling / compositing (pixman + Cairo), ZSTD tile compression and WSS crypto.", "bullet"),
])

# ---- Part 4 -----------------------------------------------------------
section_slide("Part 4 - Puppeteer", "A flamegraph of the browser client")

content_slide("Profiling the other half - the browser", [
    ("The kit flamegraph is the C++ server; this is the JavaScript client running in the browser.", "bullet"),
    ("k6/browser v2 hides the CDP layer, so the V8 Profiler API is out of reach from a k6 test.", "bullet"),
    ("cool-puppeteer-profile.js drives a real Chromium through Puppeteer's own CDP session, enables the Profiler and records the client JS.", "bullet"),
    ("./k6-flamegraph.py --browser-runner puppeteer      # captures kit perf AND browser CPU in one run", "cmd"),
    ("Output: browser.cpuprofile - open it in speedscope.app or Chrome DevTools (Performance -> Load profile).", "bullet"),
])

# ---- Part 5 -----------------------------------------------------------
section_slide("Part 5 - Recurrent testing", "The objective of regular runs on fixed hardware")

content_slide("Why run it regularly, on fixed hardware", [
    ("A single run is a snapshot; the value is in the trend across builds.", "bullet"),
    ("Fixed reference hardware removes the biggest source of noise - so two runs are actually comparable.", "bullet"),
    ("Same scenario + same documents + same hardware = the number means the same thing every time.", "bullet"),
    ("Objective: a stable baseline that turns a regression into a visible step in a chart, not a hunch.", "bullet"),
    ("Catch slowdowns release-over-release, close to the commit that caused them.", "bullet"),
    ("Feed the results into a dashboard or a gate, so a regression is seen before it ships.", "bullet"),
])

closing_slide()

out = os.path.join(BASE, "cool-k6-performance-testing.odp")
doc.save(out)
print(f"wrote {out} ({_n} slides)")
