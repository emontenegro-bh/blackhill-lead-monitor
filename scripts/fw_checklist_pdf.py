"""Build the City's completion-report PDF straight from the CompanyCam API.

CompanyCam has no PDF export endpoint, so the export used to be clicked
through in a browser on a laptop every day. Everything the export shows is in
the checklist API: each field's answer and its photos.
This rebuilds the report (photos included, no technician name or times, sections marked N/A and empty
fields left out, three photos per row) as a PDF in memory, so the tracker can
attach it to the completion draft itself.

Graph's simple attachment POST caps a file at 3 MB, so photos are shrunk and
recompressed here. (CompanyCam's photo URLs are signed: editing their
`rs:fit:W:H` size segment returns 403, so the original is downloaded once.)
build_pdf() raises if the result still will not fit, and the caller falls back to the manual PDF queue for that job.
"""
import io
import urllib.request
from datetime import datetime, timezone

from PIL import Image
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (Image as RLImage, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

MAX_BYTES = 2_900_000            # under Graph's 3 MB simple-attachment limit
PHOTO_PX = 800                   # longest side requested from CompanyCam
PER_ROW = 3

_TITLE = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=16, leading=20)
_SUB = ParagraphStyle("s", fontName="Helvetica", fontSize=10, leading=13, textColor=HexColor("#555555"))
_FIELD = ParagraphStyle("f", fontName="Helvetica-Bold", fontSize=12, leading=15, spaceBefore=10)
_ANSWER = ParagraphStyle("a", fontName="Helvetica", fontSize=11, leading=14)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _answer(task):
    parts = []
    for st in task.get("sub_tasks") or []:
        if st.get("answer_text"):
            parts.append(st["answer_text"].strip())
        opts = st.get("answer_options") or []
        for i in st.get("answer_choices") or []:
            if isinstance(i, int) and 0 <= i < len(opts):
                parts.append(str(opts[i]))
    return "\n".join(p for p in parts if p)


_RAW = {}


def _fetch_photo(url, px, quality):
    if url not in _RAW:            # one download per photo across size retries
        with urllib.request.urlopen(url, timeout=60) as resp:
            _RAW[url] = resp.read()
    img = Image.open(io.BytesIO(_RAW[url]))
    from PIL import ImageOps
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((px, px))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True)
    out.seek(0)
    return out


def _render(checklist, title, px, quality):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title=title)
    width = doc.width
    story = [Paragraph(_esc(title), _TITLE),
             Paragraph("Fort Worth Irrigation Repairs - Black Hill Landscaping", _SUB)]
    story.append(Spacer(1, 8))

    tasks = list(checklist.get("sectionless_tasks") or [])
    for sec in checklist.get("sections") or []:
        tasks += sec.get("tasks") or []
    tasks.sort(key=lambda t: t.get("position") or 0)

    cell_w = width / PER_ROW
    img_w = cell_w - 8
    for t in tasks:
        if t.get("not_applicable") or t.get("marked_not_applicable"):
            continue
        # Evelin, 2026-11-24: no technician name and no times on the City's copy.
        if "technician" in (t.get("title") or "").lower():
            continue
        ans, photos = _answer(t), t.get("photos") or []
        if not ans and not photos:
            continue                       # empty field: the export leaves it out too
        story.append(Paragraph(_esc(t.get("title")), _FIELD))
        if ans:
            story.append(Paragraph(_esc(ans).replace("\n", "<br/>"), _ANSWER))
        cells = []
        for p in photos:
            data = _fetch_photo(p["url"], px, quality)
            iw, ih = ImageReader(data).getSize()
            data.seek(0)
            h = img_w * ih / iw
            if h > 2.6 * inch:
                img_w2, h = 2.6 * inch * iw / ih, 2.6 * inch
            else:
                img_w2 = img_w
            cells.append(RLImage(data, width=img_w2, height=h))
        rows = [cells[i:i + PER_ROW] for i in range(0, len(cells), PER_ROW)]
        for row in rows:
            row += [""] * (PER_ROW - len(row))
            tbl = Table([row], colWidths=[cell_w] * PER_ROW)
            tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 4),
                                     ("RIGHTPADDING", (0, 0), (-1, -1), 4)]))
            story += [Spacer(1, 4), tbl]
    doc.build(story)
    return buf.getvalue()


def build_pdf(checklist, title):
    """PDF bytes for one checklist. Steps the photo size down until the file
    fits under MAX_BYTES; raises ValueError if it never does."""
    for px, quality in ((PHOTO_PX, 72), (650, 62), (500, 55)):
        pdf = _render(checklist, title, px, quality)
        if len(pdf) <= MAX_BYTES:
            return pdf
    raise ValueError(f"PDF is {len(pdf):,} bytes even at 500px photos; over the 3 MB attachment limit")
