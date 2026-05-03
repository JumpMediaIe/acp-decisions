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


def _extract_text(pdf_bytes: bytes) -> str:
    """Extract text from an Order PDF.

    pypdf handles most ACP orders cleanly. Some orders (chiefly mid-2024+) have
    a corrupted text layer — overlapping or wrongly-mapped fonts that no
    text-only extractor (pypdf, pdfminer.six, pypdfium2) can linearise. Those
    cases land with empty `reasons` here; the orchestrator records a
    `pdf_no_text` scrape error so they're recoverable with OCR later.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


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
    """Find the numbered-list reasons section and split it into items."""
    anchor_pos = _find_reasons_anchor(text)
    if anchor_pos is None:
        return []
    body = text[anchor_pos:]
    return _split_numbered_list(body)


def _find_reasons_anchor(text: str) -> int | None:
    """Return the offset *after* the 'Reasons and Considerations' header whose
    immediate next non-empty content begins a numbered list (item 1).
    """
    candidates: list[int] = []
    for m in _REASONS_HEADER_RE.finditer(text):
        candidates.append(m.end())
    if not candidates:
        return None
    # Prefer the LAST candidate that has a "1." within ~300 chars after the header.
    # This handles long-form Orders that mention "Reasons and Considerations" first
    # in a legislative-preamble section without numbered reasons.
    chosen: int | None = None
    for end_pos in candidates:
        window = text[end_pos : end_pos + 300]
        first_item = re.search(r"(?m)^\s*1\.?\s+[A-Z]", window)
        if first_item is not None:
            chosen = end_pos + first_item.start()
    return chosen


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
