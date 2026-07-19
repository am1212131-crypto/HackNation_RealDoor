"""
Deterministic, allowlisted field extraction from the official Hack-Nation
RealDoor starter-pack PDFs.

These fixtures use a fixed "zonal" template per document type: an ALL-CAPS
header label sits on one line, and the value sits a fixed vertical offset
below it, in a column anchored at roughly the same x-position as the header.
Each page also carries a large rotated "TRAINING FIXTURE" watermark rendered
in oversized fonts (18pt+) interleaved with the real content -- we strip it
by filtering on character size (all real content is <=15pt) before doing any
field matching.

Design (Non-Negotiable Requirements -> implementation):
  - UNTRUSTED INPUT: we never send document text to an instruction-following
    model as the primary path. We only pattern-match a fixed set of
    allowlisted (header -> field) zones. Any other text on the page
    (including the fixtures' deliberately embedded "Ignore prior
    instructions..." adversarial sentences) is inert data: it is never
    parsed as a field, never executed, and never changes control flow. We
    separately flag (but do not act on) text that looks like a
    prompt-injection attempt, purely so the UI can show the renter it was
    seen and ignored.
  - PROFILE/Field allowlists: DOC_SCHEMAS below is the complete list of
    fields RealDoor will ever extract. Nothing outside this list is
    extracted.
  - Some fixtures are deliberately rasterized (scanned-image) pages with no
    extractable text layer at all. For those, extract_fields_from_pdf()
    reports every field as "not_found" (status) with is_rasterized=True; the
    optional LLM vision fallback (llm_extraction.vision_backfill) can then
    read the rendered page image instead -- still gated behind
    OPENAI_API_KEY, still never trusted without renter confirmation.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

import pdfplumber

# Real page content in these fixtures never exceeds this font size; the
# "TRAINING FIXTURE" watermark is rendered at 18pt+.
_WATERMARK_SIZE_CUTOFF = 15.0
_LINE_TOLERANCE = 3.0
_VALUE_TOP_OFFSET = (6, 24)  # (min, max) points below the header's top

# ---------------------------------------------------------------------------
# Field allowlists (source of truth for "extract only allowlisted fields")
# Each field: header_tokens (must appear as a contiguous run on one line,
# case-insensitive), and x_range (the column the value is expected to fall
# into, in PDF points from the left edge).
# ---------------------------------------------------------------------------

DOC_SCHEMAS = {
    "application_summary": {
        "display_name": "Application summary",
        "detect_marker": "Application Summary",
        "fields": {
            "person_name": {"label": "Applicant", "type": "text", "header_tokens": ["APPLICANT"], "x_range": (25, 350),
                             "purpose": "Identifies whose documents these are, so fields aren't mixed across people. Never used to infer protected characteristics."},
            "household_size": {"label": "Household Size", "type": "integer", "header_tokens": ["HOUSEHOLD", "SIZE"], "x_range": (350, 520),
                                "purpose": "Looks up the frozen income threshold for your household size. Nothing else."},
            "address": {"label": "Mailing Address", "type": "text", "header_tokens": ["MAILING", "ADDRESS"], "x_range": (25, 520),
                        "purpose": "Displayed back to you for your own reference only. Never used in any calculation, comparison, or filter."},
            "application_date": {"label": "Application Date", "type": "date", "header_tokens": ["APPLICATION", "DATE"], "x_range": (25, 520),
                                  "purpose": "Checks whether this document is within the 60-day currency window."},
        },
    },
    "pay_stub": {
        "display_name": "Pay stub",
        "detect_marker": "Pay Stub",
        "fields": {
            "person_name": {"label": "Employee", "type": "text", "header_tokens": ["EMPLOYEE"], "x_range": (25, 320),
                             "purpose": "Identifies whose pay this is."},
            "pay_date": {"label": "Pay Date", "type": "date", "header_tokens": ["PAY", "DATE"], "x_range": (320, 520),
                         "purpose": "Checks document currency (must be within 60 days of the event date)."},
            "pay_period_start": {"label": "Pay Period Start", "type": "date", "header_tokens": ["PAY", "PERIOD"], "x_range": (25, 190),
                                  "purpose": "Shown for your reference so you can verify the stub covers the period it claims."},
            "pay_period_end": {"label": "Pay Period End (Through)", "type": "date", "header_tokens": ["THROUGH"], "x_range": (190, 350),
                                "purpose": "Shown for your reference so you can verify the stub covers the period it claims."},
            "pay_frequency": {"label": "Pay Frequency", "type": "text", "header_tokens": ["PAY", "FREQUENCY"], "x_range": (350, 520),
                               "purpose": "Multiplies your per-period wage into an annual figure (e.g. x52 for weekly)."},
            "regular_hours": {"label": "Regular Hours", "type": "number", "header_tokens": ["REGULAR", "HOURS"], "x_range": (40, 180),
                               "purpose": "Multiplied by hourly rate to compute your annualized wage income."},
            "hourly_rate": {"label": "Hourly Rate", "type": "currency", "header_tokens": ["HOURLY", "RATE"], "x_range": (180, 330),
                             "purpose": "Multiplied by regular hours to compute your annualized wage income."},
            "gross_pay": {"label": "Gross Pay", "type": "currency", "header_tokens": ["GROSS", "PAY"], "x_range": (330, 450),
                          "purpose": "Cross-checked against hours x rate only to flag inconsistent pay stubs for human review -- never used as the calculation basis itself."},
            "net_pay": {"label": "Net Pay", "type": "currency", "header_tokens": ["NET", "PAY"], "x_range": (450, 560),
                        "purpose": "Shown for your reference only. Not used in any calculation."},
        },
    },
    "employment_letter": {
        "display_name": "Employment letter",
        "detect_marker": "Employment Letter",
        "fields": {
            "person_name": {"label": "Employee", "type": "text", "header_tokens": ["EMPLOYEE"], "x_range": (25, 340),
                             "purpose": "Identifies whose employment this describes."},
            "document_date": {"label": "Letter Date", "type": "date", "header_tokens": ["LETTER", "DATE"], "x_range": (350, 520),
                               "purpose": "Checks document currency (must be within 60 days of the event date)."},
            "weekly_hours": {"label": "Hours Per Week", "type": "number", "header_tokens": ["HOURS", "PER", "WEEK"], "x_range": (25, 240),
                              "purpose": "Shown for your reference to help corroborate the pay stub's hours -- not used as the primary calculation input."},
            "hourly_rate": {"label": "Hourly Rate", "type": "currency", "header_tokens": ["HOURLY", "RATE"], "x_range": (240, 520),
                             "purpose": "Shown for your reference to help corroborate the pay stub's rate -- not used as the primary calculation input."},
        },
    },
    "benefit_letter": {
        "display_name": "Benefit letter",
        "detect_marker": "Benefit Letter",
        "fields": {
            "person_name": {"label": "Recipient", "type": "text", "header_tokens": ["RECIPIENT"], "x_range": (25, 340),
                             "purpose": "Identifies who receives this benefit."},
            "document_date": {"label": "Letter Date", "type": "date", "header_tokens": ["LETTER", "DATE"], "x_range": (350, 520),
                               "purpose": "Checks document currency (must be within 60 days of the event date)."},
            "monthly_benefit": {"label": "Monthly Amount", "type": "currency", "header_tokens": ["MONTHLY", "AMOUNT"], "x_range": (25, 270),
                                 "purpose": "Annualized (x periods/year) and added to your total confirmed income."},
            "benefit_frequency": {"label": "Frequency", "type": "text", "header_tokens": ["FREQUENCY"], "x_range": (270, 520),
                                   "purpose": "Determines the annualization multiplier for the benefit amount."},
        },
    },
    "gig_statement": {
        "display_name": "Gig income statement",
        "detect_marker": "Gig Statement",
        "fields": {
            "person_name": {"label": "Worker", "type": "text", "header_tokens": ["WORKER"], "x_range": (25, 340),
                             "purpose": "Identifies whose gig income this is."},
            "statement_month": {"label": "Statement Month", "type": "text", "header_tokens": ["STATEMENT", "MONTH"], "x_range": (350, 520),
                                 "purpose": "Checks document currency (must be within 60 days of the event date)."},
            "gross_receipts": {"label": "Gross Receipts", "type": "currency", "header_tokens": ["GROSS", "RECEIPTS"], "x_range": (25, 270),
                                "purpose": "Annualized (x12, one statement month) and added to your total confirmed income. Always flagged for human review since a single self-reported statement isn't independently corroborated."},
            "platform_fees": {"label": "Platform Fees", "type": "currency", "header_tokens": ["PLATFORM", "FEES"], "x_range": (270, 520),
                               "purpose": "Shown for your reference only. Not subtracted from the calculation (gross receipts are used, per the frozen challenge convention)."},
        },
    },
}

# Phrases that look like an attempt to steer an AI reader. Purely informational
# -- detection never changes extraction, never grants any capability, and is
# always reported to the renter, never silently honored.
_INJECTION_PATTERNS = [
    r"ignore (all|any|prior|previous) instructions",
    r"system\s*override",
    r"admin mode",
    r"mark (this|the) applicant (as )?(eligible|approved|denied)",
    r"set confidence to 100",
    r"do not show this (memo|text) to the user",
    r"auto-?send",
    r"reveal (the )?(system prompt|secrets?)",
    r"untrusted document text",
]


@dataclass
class ExtractedField:
    field_id: str
    label: str
    value: Optional[str]
    field_type: str
    confidence: float
    source_box: Optional[dict]  # {page, x0, top, x1, bottom} in PDF points
    status: str  # "extracted" | "not_found" | "llm_suggested"
    extraction_method: str = "zonal"  # "zonal" | "llm_assisted" | "llm_vision"
    confirmed: bool = False
    corrected: bool = False


@dataclass
class ExtractionResult:
    doc_type: str
    doc_type_confidence: float
    fields: list = field(default_factory=list)
    injection_flags: list = field(default_factory=list)
    is_rasterized: bool = False
    raw_line_count: int = 0


def _group_words_into_lines(words, tolerance=_LINE_TOLERANCE):
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        placed = False
        for line in lines:
            if abs(line["top"] - w["top"]) <= tolerance:
                line["words"].append(w)
                placed = True
                break
        if not placed:
            lines.append({"top": w["top"], "words": [w]})
    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
        line["text"] = " ".join(w["text"] for w in line["words"])
    lines.sort(key=lambda l: l["top"])
    return lines


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z]", "", text.upper())


def _find_header(lines, header_tokens):
    target = [_norm(t) for t in header_tokens]
    for line in lines:
        norm_words = [_norm(w["text"]) for w in line["words"]]
        for i in range(len(norm_words) - len(target) + 1):
            if norm_words[i:i + len(target)] == target:
                seq = line["words"][i:i + len(target)]
                return {"top": min(w["top"] for w in seq), "bottom": max(w["bottom"] for w in seq)}
    return None


def _detect_doc_type(full_text: str):
    for doc_type, schema in DOC_SCHEMAS.items():
        if schema["detect_marker"].lower() in full_text.lower():
            return doc_type, 0.98
    return "unknown", 0.0


def _find_injection_flags(full_text: str):
    hits = []
    lowered = full_text.lower()
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, lowered):
            hits.append(pattern)
    return hits


def extract_fields_from_pdf(pdf_bytes: bytes, forced_doc_type: Optional[str] = None) -> ExtractionResult:
    import io

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        full_text = page.extract_text() or ""
        injection_flags = _find_injection_flags(full_text)

        small_page = page.filter(lambda o: o.get("size", 999) <= _WATERMARK_SIZE_CUTOFF)
        all_words = small_page.extract_words()
        is_rasterized = len(all_words) == 0

        doc_type, doc_conf = (forced_doc_type, 1.0) if forced_doc_type else _detect_doc_type(full_text)
        if doc_type not in DOC_SCHEMAS:
            if is_rasterized:
                # Can't even read the watermark/title text to classify a scanned
                # page without OCR/vision; report unknown-but-rasterized so the
                # caller can offer the vision fallback with an explicit type pick.
                return ExtractionResult(
                    doc_type="unknown", doc_type_confidence=0.0, fields=[],
                    injection_flags=injection_flags, is_rasterized=True,
                )
            return ExtractionResult(
                doc_type="unknown", doc_type_confidence=0.0, fields=[],
                injection_flags=injection_flags, raw_line_count=0,
            )

        schema = DOC_SCHEMAS[doc_type]
        lines = _group_words_into_lines(all_words)
        extracted = []

        for field_id, spec in schema["fields"].items():
            header = _find_header(lines, spec["header_tokens"])
            value_words = []
            box = None
            if header:
                lo, hi = _VALUE_TOP_OFFSET
                x_lo, x_hi = spec["x_range"]
                value_words = [
                    w for w in all_words
                    if header["top"] + lo <= w["top"] <= header["top"] + hi
                    and x_lo <= w["x0"] < x_hi
                ]
                value_words.sort(key=lambda w: w["x0"])

            if value_words:
                text_value = " ".join(w["text"] for w in value_words)
                x0 = min(w["x0"] for w in value_words)
                x1 = max(w["x1"] for w in value_words)
                top = min(w["top"] for w in value_words)
                bottom = max(w["bottom"] for w in value_words)
                box = {"page": 1, "x0": round(x0, 1), "top": round(top, 1), "x1": round(x1, 1), "bottom": round(bottom, 1)}
                extracted.append(ExtractedField(
                    field_id=field_id, label=spec["label"], value=text_value,
                    field_type=spec["type"], confidence=0.95, source_box=box, status="extracted",
                ))
            else:
                extracted.append(ExtractedField(
                    field_id=field_id, label=spec["label"], value=None,
                    field_type=spec["type"], confidence=0.0, source_box=None, status="not_found",
                ))

        return ExtractionResult(
            doc_type=doc_type, doc_type_confidence=doc_conf, fields=extracted,
            injection_flags=injection_flags, is_rasterized=is_rasterized, raw_line_count=len(lines),
        )


def render_page_png_b64(pdf_bytes: bytes, resolution: int = 150) -> tuple:
    """Render page 1 to a PNG (base64) for the evidence viewer / vision fallback."""
    import base64
    import io

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        image = page.to_image(resolution=resolution)
        buf = io.BytesIO()
        image.original.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii"), resolution
