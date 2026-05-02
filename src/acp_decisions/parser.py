"""Parse a single ACP case page into structured Decision metadata.

The case page (https://www.pleanala.ie/en-ie/case/{id}) is a Foundation-grid
layout. Field/value pairs are encoded as:

    <div class="grid-x grid-padding-x">
      <div class="medium-3 cell"><p class="case-sub">FieldName</p></div>
      <div class="medium-9 cell"><p class="case-summary">FieldValue</p></div>
    </div>

This parser produces a Decision with metadata only — refusal reasons live in
the linked Order PDF and are filled in by a separate parser. The
`decision_outcome` field is set to the raw string here; the outcome normaliser
overwrites it with a canonical bucket later.

See docs/discovery-2026-05-02.md for full structural notes.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from acp_decisions.models import Decision, DocumentLink


_BASE_URL = "https://www.pleanala.ie"

_PA_REF_RE = re.compile(r"Case reference:\s*([A-Z]{2,4}\d*\.\d+)")
_DDMMYYYY_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def parse_case_page(
    html: str,
    *,
    case_id_url: int,
    scraped_at: str,
) -> tuple[Decision, list[DocumentLink]]:
    """Parse one ACP case-page HTML string.

    Returns the Decision (with refusal_reasons empty — filled in by PDF parser
    downstream) and a list of DocumentLinks for every PDF the page references.
    """
    tree = HTMLParser(html)

    pa_reference = _extract_pa_reference(tree)
    site_address = _text_of(tree.css_first("p.address"))
    council = _text_of(tree.css_first("p.council"))
    pairs = _extract_field_pairs(tree)

    description = pairs.get("Description", "")
    case_type = pairs.get("Case type")
    decision_raw = pairs.get("Decision", "")
    date_signed_raw = pairs.get("Date signed", "")
    decision_date = _parse_dd_mm_yyyy(date_signed_raw)

    applicant = _extract_applicant_name(tree)
    documents = _extract_documents(tree)

    decision = Decision(
        case_id_url=case_id_url,
        decision_date=decision_date,
        county_raw=council or "",
        development_type_raw=description,
        decision_outcome=decision_raw,        # placeholder — normaliser overwrites
        decision_outcome_raw=decision_raw,
        scraped_at=scraped_at,
        pa_reference=pa_reference,
        site_address=site_address,
        case_type_raw=case_type,
        applicant_name_raw=applicant,
    )
    return decision, documents


def _text_of(node: Node | None) -> str | None:
    if node is None:
        return None
    text = node.text(deep=True, strip=False).strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


def _extract_pa_reference(tree: HTMLParser) -> str | None:
    title = tree.css_first("h3.section-title")
    if title is None:
        return None
    text = title.text(deep=True)
    m = _PA_REF_RE.search(text)
    return m.group(1) if m else None


def _extract_field_pairs(tree: HTMLParser) -> dict[str, str]:
    """Extract every <case-sub> → <case-summary> pair on the page."""
    pairs: dict[str, str] = {}
    for sub in tree.css("p.case-sub"):
        # Walk up to the outer grid-x wrapper, then find the sibling case-summary.
        outer = sub.parent  # medium-3 cell
        if outer is None:
            continue
        wrapper = outer.parent  # grid-x
        if wrapper is None:
            continue
        summary = wrapper.css_first("p.case-summary")
        if summary is None:
            continue
        label = sub.text(deep=True, strip=True)
        value = _text_of(summary) or ""
        if label and label not in pairs:
            pairs[label] = value
    return pairs


def _parse_dd_mm_yyyy(s: str) -> str:
    """'05/09/2024' → '2024-09-05'. Returns '' if unparseable."""
    s = s.strip()
    m = _DDMMYYYY_RE.match(s)
    if not m:
        return ""
    dd, mm, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


def _extract_applicant_name(tree: HTMLParser) -> str | None:
    """Find the first party in <ul class="case-list"> whose role contains 'Applicant'."""
    for li in tree.css("ul.case-list li"):
        text = _text_of(li) or ""
        if "(Applicant)" not in text:
            continue
        # Strip the trailing role markers, e.g. "Foo Ltd. (Applicant) (Active)" → "Foo Ltd."
        name = re.sub(r"\s*\([^)]*\)\s*", " ", text).strip()
        return name or None
    return None


def _extract_documents(tree: HTMLParser) -> list[DocumentLink]:
    """Find the Documents section and return one DocumentLink per PDF link."""
    docs: list[DocumentLink] = []
    docs_sub = None
    for sub in tree.css("p.case-sub"):
        if sub.text(deep=True, strip=True) == "Documents":
            docs_sub = sub
            break
    if docs_sub is None:
        return docs
    grid_x = docs_sub.parent.parent if docs_sub.parent else None  # type: ignore[union-attr]
    if grid_x is None:
        return docs
    for a in grid_x.css("a"):
        href = a.attributes.get("href") or ""
        if not href.lower().endswith(".pdf"):
            continue
        url = urljoin(_BASE_URL, href)
        docs.append(DocumentLink(doc_type=_classify_doc_url(href), url=url))
    return docs


def _classify_doc_url(href: str) -> str:
    """Map a PDF URL path to one of the canonical doc_type buckets."""
    h = href.lower()
    if "/cases/orders/" in h:
        return "order"
    if "/cases/reports/" in h:
        return "inspector_report"
    if "/cases/directions/" in h:
        return "direction"
    if "/cases/bmr/" in h:
        return "bmr"
    return "other"
