r"""Extract structured refusal reasons + ABP reference from an Order PDF.

Order PDFs (downloaded from `/cases/orders/...`) contain the Board's decision
verb and a numbered list of "Reasons and Considerations" — the canonical
refusal-reason text that the analytics layer classifies.

Two layouts coexist in the wild (see docs/discovery-2026-05-02.md):

* Short orders (~5 pages): clean numbered list directly after the
  "Reasons and Considerations" header.
* Long orders (10+ pages): the same header appears earlier as a section
  introducing legislative/policy context (bullet points), then again later
  preceding the actual numbered reasons.

Strategy:

1. Concatenate page text with newlines.
2. Strip page-footer noise (`Page X of Y`, `ABP-XXXXXX-YY An Coimisiún Pleanála`).
3. Find every "Reasons and Considerations" anchor; pick the one whose
   immediate follow-up text contains a `1.` numbered item.
4. From that anchor, split into reasons by line-anchored numbered markers
   (`^N\.?\s+`), keeping only sequences that increase monotonically from 1.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from pypdf import PdfReader

from acp_decisions.models import RefusalReason


_ABP_REF_RE = re.compile(r"\bABP-\d+-\d+\b")
_REASONS_HEADER_RE = re.compile(r"(?im)^\s*Reasons and Considerations\s*$")
_DECISION_VERB_RE = re.compile(r"\b(REFUSE|GRANT|GRANTS)\b")
_NUMBERED_ITEM_RE = re.compile(r"(?m)^\s*(\d{1,2})\.?\s+(?=[A-Z])")
# Page footers come in several mangled forms in pypdf-extracted text:
#   "ABP-123-22 An Coimisián Pleanála Page 3 of 5"        (clean Unicode)
#   "ABP-123-22 An CoimisiCln Pleangla Page 3 of 5"       (broken accents)
#   "ABP-123-22 Board Order Page 1 of 13"                 (different layout)
#   "Page 5 of 5"                                          (bare)
# We strip an optional ABP prefix + up to ~5 short words + "Page N of M".
_PAGE_FOOTER_RES = [
    # With explicit "Page N of M" suffix.
    re.compile(r"(?:ABP-\d+-\d+\s+)?(?:\S+\s+){0,5}Page\s+\d+\s+of\s+\d+", re.IGNORECASE),
    # Trailing footer on the last page, no page-number suffix:
    # "ABP-315183-22 An Coimisian Pleanala". The "An Coimisi*n Plean*la"
    # signature is distinctive enough not to false-match in body text.
    re.compile(r"ABP-\d+-\d+\s+An\s+Coimisi\S*\s+Plean\S*", re.IGNORECASE),
]


@dataclass
class OrderParseResult:
    """Output of parse_order_pdf."""
    abp_reference: str | None
    decision_verb: str | None  # 'REFUSE' | 'GRANT' | 'GRANTS' | None
    reasons: list[RefusalReason]


def parse_order_pdf(pdf_bytes: bytes) -> OrderParseResult:
    """Parse an Order PDF's bytes into ABP reference + decision verb + reasons."""
    text = _extract_text(pdf_bytes)
    text = _strip_footers(text)
    abp = _extract_abp_reference(text)
    verb = _extract_decision_verb(text)
    reasons = _extract_reasons(text)
    return OrderParseResult(abp_reference=abp, decision_verb=verb, reasons=reasons)


_OCR_FALLBACK_THRESHOLD_CHARS = 500

# UB-Mannheim's Windows installer puts tesseract in one of these paths by default.
# pytesseract relies on tesseract being on PATH; we auto-detect to spare the user
# the env-var dance.
_WINDOWS_TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

# DjVuLibre ships ddjvu.exe for rasterising DjVu pages so Tesseract can OCR
# them. Older council scans (Meath, Wicklow) are DjVu rather than PDF.
_WINDOWS_DDJVU_PATHS = (
    r"C:\Program Files\DjVuLibre\ddjvu.exe",
    r"C:\Program Files (x86)\DjVuLibre\ddjvu.exe",
)


def _find_ddjvu() -> str | None:
    import os
    import shutil

    found = shutil.which("ddjvu")
    if found:
        return found
    env_path = os.environ.get("DDJVU_CMD")
    if env_path and os.path.exists(env_path):
        return env_path
    for path in _WINDOWS_DDJVU_PATHS:
        if os.path.exists(path):
            return path
    return None


def _ocr_djvu(djvu_bytes: bytes) -> str:
    """Rasterise a DjVu document with ddjvu, then OCR each page with Tesseract.

    Returns "" if DjVuLibre / Tesseract / their bindings aren't available.
    """
    import os
    import subprocess
    import tempfile

    ddjvu = _find_ddjvu()
    if not ddjvu:
        return ""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    _configure_tesseract_path(pytesseract)

    with tempfile.TemporaryDirectory() as td:
        djvu_path = os.path.join(td, "doc.djvu")
        with open(djvu_path, "wb") as f:
            f.write(djvu_bytes)
        # -eachpage with a %d template emits one TIFF per page.
        page_tmpl = os.path.join(td, "page-%d.tiff")
        try:
            subprocess.run(
                [ddjvu, "-format=tiff", "-quality=150", "-eachpage", djvu_path, page_tmpl],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            return ""
        pages = sorted(p for p in os.listdir(td) if p.startswith("page-"))
        out: list[str] = []
        for p in pages:
            try:
                img = Image.open(os.path.join(td, p))
                out.append(pytesseract.image_to_string(img))
            except Exception:  # noqa: BLE001
                continue
        return "\n".join(out)


def _configure_tesseract_path(pytesseract_module: object) -> None:
    """Point pytesseract at the Tesseract binary if PATH isn't set up.

    Honours the TESSERACT_CMD env var if set; otherwise tries the standard
    Windows install paths. On Linux / macOS, leaves the default alone (PATH
    typically works on those).
    """
    import os
    import shutil

    # If PATH already finds it, leave well alone.
    if shutil.which("tesseract"):
        return

    env_path = os.environ.get("TESSERACT_CMD")
    if env_path and os.path.exists(env_path):
        pytesseract_module.pytesseract.tesseract_cmd = env_path  # type: ignore[attr-defined]
        return

    for path in _WINDOWS_TESSERACT_PATHS:
        if os.path.exists(path):
            pytesseract_module.pytesseract.tesseract_cmd = path  # type: ignore[attr-defined]
            return


def _extract_text(pdf_bytes: bytes) -> str:
    """Extract text from a document (PDF or DjVu).

    Strategy:
      1. DjVu files (magic bytes 'AT&T') go straight to ddjvu -> Tesseract OCR.
      2. PDFs: try pypdf first (fast, handles ~80% cleanly).
      3. If pypdf yields essentially nothing, fall back to OCR via Tesseract
         (scanned-image PDFs + PDFs with a corrupted text layer).
      4. If the required tool isn't installed, return whatever we got; the
         caller logs the empty result so the case is recoverable later.
    """
    # DjVu container magic is "AT&T" then a form-type tag.
    if pdf_bytes[:4] == b"AT&T":
        return _ocr_djvu(pdf_bytes)

    try:
        text = _extract_with_pypdf(pdf_bytes)
    except Exception:  # noqa: BLE001 — pypdf raises a wide variety on odd files
        text = ""
    if len(text.strip()) >= _OCR_FALLBACK_THRESHOLD_CHARS:
        return text
    ocr_text = _ocr_pdf(pdf_bytes)
    return ocr_text if ocr_text else text


def _extract_with_pypdf(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


def _ocr_pdf(pdf_bytes: bytes) -> str:
    """Render each page to an image and OCR it.

    Returns "" if Tesseract or its Python binding aren't available — letting
    the caller decide how to handle the absence (we don't crash a scrape on
    a single missing dep).
    """
    try:
        import pypdfium2 as pdfium
        import pytesseract
    except ImportError:
        return ""
    _configure_tesseract_path(pytesseract)
    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception:  # noqa: BLE001 — pdfium raises a wide variety
        return ""
    out: list[str] = []
    try:
        for page in pdf:
            # 200 DPI is a good balance: typeset text reads cleanly, file size
            # stays manageable. Lower DPIs lose serif detail; higher gives no
            # accuracy gain on typeset documents.
            bitmap = page.render(scale=200 / 72)
            image = bitmap.to_pil()
            try:
                page_text = pytesseract.image_to_string(image)
            except pytesseract.TesseractNotFoundError:
                # Tesseract binary not on PATH — give up cleanly.
                return ""
            out.append(page_text)
    finally:
        pdf.close()
    return "\n".join(out)


def _strip_footers(text: str) -> str:
    for rx in _PAGE_FOOTER_RES:
        text = rx.sub("", text)
    return text


def _extract_abp_reference(text: str) -> str | None:
    m = _ABP_REF_RE.search(text)
    return m.group(0) if m else None


def _extract_decision_verb(text: str) -> str | None:
    """Look for the operative verb in the 'Decision' section.

    Heuristic: the first uppercase REFUSE/GRANT(S) within the document is reliably
    the Board's decision verb (it precedes the reasons section).
    """
    m = _DECISION_VERB_RE.search(text)
    return m.group(1) if m else None


def _extract_reasons(text: str) -> list[RefusalReason]:
    """Find the reasons section and split it into items.

    Two layouts are common in ACP orders:
      - Numbered: "Reasons and Considerations\\n1. ...\\n2. ...\\n3. ..."
      - Prose: "Reasons and Considerations\\n<paragraph>\\n\\n<paragraph>\\n\\n..."
    Try numbered first; fall back to paragraph splitting if no list found.
    """
    numbered_anchor = _find_numbered_anchor(text)
    if numbered_anchor is not None:
        body = text[numbered_anchor:]
        items = _split_numbered_list(body)
        if items:
            return items

    prose_anchor = _find_reasons_header(text)
    if prose_anchor is None:
        return []
    return _split_prose(text[prose_anchor:])


def _find_numbered_anchor(text: str) -> int | None:
    """Offset of the '1.' that begins a numbered reasons list, if any.

    Walks every 'Reasons and Considerations' header and picks the one whose
    immediate follow-up text contains '1.'. The LAST such match wins —
    long-form orders mention the header twice (legislative preamble, then
    actual reasons).
    """
    candidates = [m.end() for m in _REASONS_HEADER_RE.finditer(text)]
    if not candidates:
        return None
    # Look further than 300 chars — some orders have a substantial contextual
    # preamble between the header and the actual "1." (citing legislation,
    # development plan policies, etc.), but the numbered list still exists.
    # 3000 covers every observed case without false-matching footers/signatures.
    chosen: int | None = None
    for end_pos in candidates:
        window = text[end_pos : end_pos + 3000]
        first_item = re.search(r"(?m)^\s*1\.?\s+[A-Z]", window)
        if first_item is not None:
            chosen = end_pos + first_item.start()
    return chosen


def _find_reasons_header(text: str) -> int | None:
    """Offset just after the LAST 'Reasons and Considerations' header, regardless
    of whether a numbered list follows."""
    last: int | None = None
    for m in _REASONS_HEADER_RE.finditer(text):
        last = m.end()
    return last


# Markers that signal the end of the reasons section in a prose-style order.
# We stop at whichever appears first.
_PROSE_END_MARKERS = (
    re.compile(r"(?im)^\s*Member\s+of\s+An\s+Bord\s+Plean\S*"),
    re.compile(r"(?im)^\s*duly\s+authorised"),
    re.compile(r"(?im)^\s*Dated\s+this\s+"),
    re.compile(r"(?im)^\s*the\s+seal\s+of\s+the\s+Board"),
)
_MIN_PARAGRAPH_CHARS = 80


def _split_prose(body: str) -> list[RefusalReason]:
    """Split a prose-style reasons section into one RefusalReason per paragraph.

    Paragraph boundaries: blank line. We trim everything from the first
    end-of-section marker (signature, "Dated this", etc.). Short fragments
    (signatures, page footers that survived the strip) are discarded.
    """
    end_pos = len(body)
    for rx in _PROSE_END_MARKERS:
        m = rx.search(body)
        if m is not None:
            end_pos = min(end_pos, m.start())
    body = body[:end_pos]
    paragraphs = re.split(r"\n\s*\n+", body)
    reasons: list[RefusalReason] = []
    for p in paragraphs:
        cleaned = _clean_reason_text(p)
        if len(cleaned) < _MIN_PARAGRAPH_CHARS:
            continue
        # Skip the literal header line if it survived the split.
        if cleaned.lower().startswith("reasons and considerations"):
            continue
        reasons.append(RefusalReason(reason_number=len(reasons) + 1, raw_text=cleaned))
    return reasons


def _split_numbered_list(body: str) -> list[RefusalReason]:
    """Walk numbered markers, keeping only an ascending 1, 2, 3, ... sequence."""
    matches = list(_NUMBERED_ITEM_RE.finditer(body))
    if not matches:
        return []
    reasons: list[RefusalReason] = []
    expected = 1
    for i, m in enumerate(matches):
        n = int(m.group(1))
        if n != expected:
            # Sequence broke — stop. Previous numbered list ended.
            break
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        raw = _clean_reason_text(body[start:end])
        if raw:
            reasons.append(RefusalReason(reason_number=n, raw_text=raw))
        expected += 1
    return reasons


def _clean_reason_text(s: str) -> str:
    """Collapse whitespace; preserve sentence breaks."""
    s = s.strip()
    # Replace any run of whitespace (incl. newlines) with a single space.
    s = re.sub(r"\s+", " ", s)
    return s
