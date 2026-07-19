import datetime
import os
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import checklist, extraction, llm_extraction, packet, rag_engine, rules_engine, session_store

app = FastAPI(title="RealDoor Application-Readiness Copilot (Research Prototype)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def _sess_or_404(session_id: str):
    if not session_store.exists(session_id):
        raise HTTPException(status_code=404, detail="Unknown or deleted session.")
    return session_store.get(session_id)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

@app.post("/api/session")
def create_session():
    session_id = session_store.create_session()
    return {"session_id": session_id, "created_at": datetime.datetime.utcnow().isoformat()}


class ConsentBody(BaseModel):
    consent_text: str


@app.post("/api/session/{session_id}/consent")
def record_consent(session_id: str, body: ConsentBody):
    _sess_or_404(session_id)
    session_store.log_consent(session_id, body.consent_text)
    return {"ok": True}


@app.delete("/api/session/{session_id}")
def delete_session(session_id: str):
    ok = session_store.delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown or already-deleted session.")
    return {"deleted": True, "session_id": session_id}


@app.get("/api/session/{session_id}/audit-log")
def get_audit_log(session_id: str):
    _sess_or_404(session_id)
    return {"audit_log": session_store.audit_log(session_id)}


# ---------------------------------------------------------------------------
# Stage 1: Profile (human-confirmed extraction)
# ---------------------------------------------------------------------------

DOC_TYPE_ALLOWLIST = set(extraction.DOC_SCHEMAS.keys())


@app.post("/api/session/{session_id}/upload")
async def upload_document(session_id: str, file: UploadFile = File(...), doc_type: str = Form(None)):
    _sess_or_404(session_id)

    if not session_store.has_consent(session_id):
        raise HTTPException(
            status_code=403,
            detail="Consent has not been recorded for this session yet. Call POST "
                   "/api/session/{id}/consent before uploading a document.",
        )

    if doc_type and doc_type not in DOC_TYPE_ALLOWLIST:
        raise HTTPException(status_code=400, detail=f"doc_type must be one of {sorted(DOC_TYPE_ALLOWLIST)}")

    if file.content_type not in ("application/pdf",) and not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported in this prototype.")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (10MB limit for this prototype).")

    result = extraction.extract_fields_from_pdf(pdf_bytes, forced_doc_type=doc_type)
    if result.doc_type == "unknown":
        if result.is_rasterized:
            raise HTTPException(
                status_code=422,
                detail="This looks like a scanned (image-only) page with no text layer. Pick a "
                       "document type from the dropdown and re-upload so RealDoor knows what to look for.",
            )
        raise HTTPException(
            status_code=422,
            detail="Could not recognize this document as a supported type (application summary, pay "
                   "stub, employment letter, benefit letter, or gig statement). Nothing was extracted.",
        )

    doc_id = uuid.uuid4().hex
    # Recover the raw text for audit/injection-flag purposes and as the
    # (untrusted) input to the optional LLM backfill below; it is encrypted
    # at rest and never surfaced to the UI as a "field".
    import pdfplumber, io as _io
    with pdfplumber.open(_io.BytesIO(pdf_bytes)) as pdf:
        raw_text = pdf.pages[0].extract_text() or ""

    llm_used_for = []
    if llm_extraction.is_configured():
        schema = extraction.DOC_SCHEMAS[result.doc_type]["fields"]
        missing_ids = [f.field_id for f in result.fields if f.status == "not_found"]
        if result.is_rasterized and missing_ids:
            # No text layer at all -- fall back to a vision read of the rendered page
            # instead of the text-based backfill.
            image_b64, _ = extraction.render_page_png_b64(pdf_bytes)
            backfilled = llm_extraction.vision_backfill(image_b64, schema, missing_ids)
            method = "llm_vision"
        else:
            backfilled = llm_extraction.backfill_missing_fields(raw_text, schema, missing_ids)
            method = "llm_assisted"
        for fld in result.fields:
            if fld.field_id in backfilled:
                fld.value = backfilled[fld.field_id]
                fld.status = "llm_suggested"
                fld.extraction_method = method
                fld.confidence = 0.55  # deliberately lower + distinct from regex matches; always needs confirmation
                fld.source_box = None  # no bounding box available for a model-derived value
                llm_used_for.append(fld.field_id)

    session_store.add_document(
        session_id, doc_id, pdf_bytes, raw_text, result.doc_type, result.fields, result.injection_flags,
    )

    return {
        "doc_id": doc_id,
        "doc_type": result.doc_type,
        "doc_type_display": extraction.DOC_SCHEMAS[result.doc_type]["display_name"],
        "doc_type_confidence": result.doc_type_confidence,
        "fields": [
            {
                "field_id": fld.field_id, "label": fld.label, "value": fld.value,
                "type": fld.field_type, "confidence": fld.confidence,
                "source_box": fld.source_box, "status": fld.status,
                "extraction_method": fld.extraction_method,
                "purpose": extraction.DOC_SCHEMAS[result.doc_type]["fields"].get(fld.field_id, {}).get("purpose"),
            }
            for fld in result.fields
        ],
        "llm_assisted_fields": llm_used_for,
        "injection_flags_detected": result.injection_flags,
        "injection_flag_note": (
            "RealDoor detected text patterns resembling an instruction aimed at an AI reader. "
            "This text was NOT executed, NOT used to change any field value or system behavior, "
            "and is shown here only for transparency."
        ) if result.injection_flags else None,
    }


@app.get("/api/session/{session_id}/document/{doc_id}/page-image")
def get_page_image(session_id: str, doc_id: str):
    sess = _sess_or_404(session_id)
    if doc_id not in sess["documents"]:
        raise HTTPException(status_code=404, detail="Unknown document.")
    pdf_bytes = session_store.get_pdf_bytes(session_id, doc_id)
    b64, resolution = extraction.render_page_png_b64(pdf_bytes)
    return {"page_image_b64": b64, "resolution": resolution, "resolution_scale": resolution / 72.0}


class FieldConfirmBody(BaseModel):
    value: str | None = None


@app.post("/api/session/{session_id}/document/{doc_id}/field/{field_id}/confirm")
def confirm_field(session_id: str, doc_id: str, field_id: str, body: FieldConfirmBody):
    sess = _sess_or_404(session_id)
    if doc_id not in sess["documents"]:
        raise HTTPException(status_code=404, detail="Unknown document.")
    if field_id not in sess["documents"][doc_id]["fields"]:
        raise HTTPException(status_code=404, detail="Unknown field.")
    session_store.confirm_field(session_id, doc_id, field_id, body.value)
    all_confirmed = session_store.mark_document_confirmed(session_id, doc_id)
    return {"ok": True, "all_fields_confirmed": all_confirmed,
            "field": sess["documents"][doc_id]["fields"][field_id]}


@app.get("/api/session/{session_id}/documents")
def list_documents(session_id: str):
    sess = _sess_or_404(session_id)
    out = []
    for doc_id, doc in sess["documents"].items():
        out.append({
            "doc_id": doc_id, "doc_type": doc["doc_type"], "confirmed": doc["confirmed"],
            "fields": doc["fields"], "injection_flags": doc["injection_flags"],
            "uploaded_at": doc["uploaded_at"],
        })
    return {"documents": out}


# ---------------------------------------------------------------------------
# Stage 2: Understand (cited rules + deterministic math)
# ---------------------------------------------------------------------------

@app.get("/api/rules/meta")
def rules_meta():
    return rules_engine.corpus_meta()


class RuleQueryBody(BaseModel):
    question: str
    household_size: int | None = None
    ami_tier: str | None = None


@app.post("/api/session/{session_id}/rules/query")
def rules_query(session_id: str, body: RuleQueryBody):
    _sess_or_404(session_id)

    # Router: numeric household-size/AMI-tier questions always go through the
    # deterministic structured table (exact, never hallucinated). Only
    # questions with no numeric hint at all fall through to the RAG path,
    # which is grounded/citation-forced over a separate narrative-rules
    # corpus and never touches the numeric threshold tables itself.
    answer = rules_engine.answer_rule_question(body.question, body.household_size, body.ami_tier)

    if answer["type"] == "route_to_rag":
        answer = rag_engine.answer_narrative_question(body.question)
        answer["route"] = "rag"
    else:
        answer["route"] = "structured"

    if answer["type"] == "refusal":
        session_store.log_refusal(session_id, body.question)
    else:
        corpus_version = (
            rules_engine.RULES["corpus_version"] if answer["route"] == "structured"
            else "sdhc-lihtc-2026-narrative:2026.1"
        )
        session_store.log_rule_query(session_id, corpus_version, body.question, route=answer["route"])
    return answer


class ProfileBody(BaseModel):
    household_id: str = "SELF"
    household_size: int
    ami_tier: str | None = None
    flags: dict = {}


@app.post("/api/session/{session_id}/profile")
def set_profile(session_id: str, body: ProfileBody):
    _sess_or_404(session_id)
    session_store.set_profile(session_id, {
        "household_id": body.household_id,
        "household_size": body.household_size,
        "ami_tier": body.ami_tier or rules_engine.SCORED_TIER,
    })
    session_store.set_household_flags(session_id, body.flags)
    return {"ok": True}


def _confirmed_docs_for_readiness(sess):
    return [
        {"doc_id": doc_id, "doc_type": d["doc_type"], "fields": {k: v["value"] for k, v in d["fields"].items()}}
        for doc_id, d in sess["documents"].items() if d["confirmed"]
    ]


@app.get("/api/session/{session_id}/calculation")
def get_calculation(session_id: str):
    """Returns a submission.schema.json-shaped object: household_id,
    annualized_income, comparison, readiness_status, citations -- plus extra
    UI detail (contributions, review_reasons, threshold) that additional
    fields in the schema permit."""
    sess = _sess_or_404(session_id)
    profile = sess["profile"]
    if "household_size" not in profile:
        raise HTTPException(status_code=400, detail="Set household_size via /profile first.")

    unconfirmed = [doc_id for doc_id, d in sess["documents"].items() if not d["confirmed"]]
    if unconfirmed:
        return {
            "type": "abstain",
            "message": "One or more uploaded documents are not yet confirmed by you. "
                       "Confirm every field before RealDoor will calculate a total.",
            "unconfirmed_doc_ids": unconfirmed,
        }

    docs = _confirmed_docs_for_readiness(sess)
    readiness = checklist.evaluate_readiness(docs)
    comparison = rules_engine.compare_income_to_limit(
        readiness["annualized_income_usd"], profile["household_size"], profile.get("ami_tier"),
    )

    citations = []
    if comparison.get("threshold"):
        citations.append({
            "rule_id": "HUD-MTSP-002" if profile.get("ami_tier", rules_engine.SCORED_TIER) == "60" else "HUD-MTSP-003",
            "source": comparison["threshold"]["source"],
            "effective_date": comparison["threshold"]["effective_date"],
        })
    citations.append({"rule_id": "CH-INCOME-001", "source": {"title": "Frozen RealDoor challenge rules", "file": "rules/RULES_README.md"}})

    return {
        "type": "calculation",
        "household_id": profile.get("household_id", "SELF"),
        "annualized_income": readiness["annualized_income_usd"],
        "comparison": comparison["comparison"],
        "readiness_status": readiness["readiness_status"],
        "review_reasons": readiness["review_reasons"],
        "citations": citations,
        "contributions": readiness["contributions"],
        "threshold": comparison.get("threshold"),
        "disclaimer": comparison["disclaimer"],
    }


# ---------------------------------------------------------------------------
# Stage 3: Prepare (renter-controlled packet)
# ---------------------------------------------------------------------------

@app.get("/api/session/{session_id}/checklist")
def get_checklist(session_id: str):
    sess = _sess_or_404(session_id)
    docs = _confirmed_docs_for_readiness(sess)
    return checklist.evaluate_checklist(docs, sess["household_flags"])


def _build_packet_payload(session_id: str):
    sess = _sess_or_404(session_id)
    profile = sess["profile"]
    if "household_size" not in profile:
        raise HTTPException(status_code=400, detail="Complete the Understand step before exporting a packet.")

    calc = get_calculation(session_id)
    docs = _confirmed_docs_for_readiness(sess)
    checklist_result = checklist.evaluate_checklist(docs, sess["household_flags"])

    docs_summary = [
        {"doc_id": doc_id, "doc_type": d["doc_type"],
         "fields": {k: v["value"] for k, v in d["fields"].items()}, "confirmed": d["confirmed"]}
        for doc_id, d in sess["documents"].items()
    ]
    return sess, profile, calc, checklist_result, docs_summary


@app.get("/api/session/{session_id}/packet/preview")
def preview_packet(session_id: str):
    """Renter-facing preview of exactly what the packet will contain, with
    NOTHING written to disk and no zip built yet -- lets the renter review
    (and go back and edit) before committing to a download, per the 'let the
    renter preview, edit, download, and delete' requirement."""
    _sess, profile, calc, checklist_result, docs_summary = _build_packet_payload(session_id)
    return {
        "profile": profile,
        "calculation": calc,
        "checklist": checklist_result,
        "documents": docs_summary,
        "corpus": rules_engine.corpus_meta(),
    }


@app.get("/api/session/{session_id}/packet")
def get_packet(session_id: str):
    sess, profile, calc, checklist_result, docs_summary = _build_packet_payload(session_id)

    confirmed_pdfs = []
    for doc_id, d in sess["documents"].items():
        if d["confirmed"]:
            pdf_bytes = session_store.get_pdf_bytes(session_id, doc_id)
            confirmed_pdfs.append((f"{doc_id}_{d['doc_type']}.pdf", pdf_bytes))

    zip_bytes = packet.build_packet_zip(
        session_id, profile, docs_summary, calc, checklist_result, rules_engine.corpus_meta(), confirmed_pdfs,
    )
    session_store.log_checklist_export(session_id, rules_engine.RULES["corpus_version"])

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=realdoor_packet.zip"},
    )


# ---------------------------------------------------------------------------
# Official organizer starter-pack households (synthetic) -- convenience for
# the acceptance demo. 6 households x up to 4 documents each.
# ---------------------------------------------------------------------------

_OFFICIAL_DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "official_documents")

HOUSEHOLD_SCENARIOS = {
    "HH-001": {"scenario": "regular_hourly", "household_size": 1, "summary": "Single earner, regular hours, clean evidence"},
    "HH-002": {"scenario": "overtime_variance", "household_size": 2, "summary": "Pay stub totals don't reconcile with hours x rate"},
    "HH-003": {"scenario": "benefits_plus_wages", "household_size": 3, "summary": "Wages + a benefit letter, no employment letter"},
    "HH-004": {"scenario": "gig_and_wages", "household_size": 4, "summary": "Wages + uncorroborated gig income"},
    "HH-005": {"scenario": "expired_letter", "household_size": 5, "summary": "Employment letter is more than 60 days old"},
    "HH-006": {"scenario": "near_threshold", "household_size": 6, "summary": "Wages + benefits, income close to the frozen threshold"},
}

DOC_TYPE_LABELS = {
    "application_summary": "Application summary",
    "pay_stub": "Pay stub",
    "employment_letter": "Employment letter",
    "benefit_letter": "Benefit letter",
    "gig_statement": "Gig income statement",
}


def _list_official_documents():
    import csv
    manifest_path = os.path.join(_OFFICIAL_DOCS_DIR, "document_manifest.csv")
    rows = []
    with open(manifest_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


@app.get("/api/households")
def list_households():
    rows = _list_official_documents()
    by_household = {}
    for row in rows:
        hh = row["household_id"]
        by_household.setdefault(hh, []).append({
            "filename": row["file_name"],
            "doc_type": row["document_type"],
            "doc_type_label": DOC_TYPE_LABELS.get(row["document_type"], row["document_type"]),
            "rasterized": row["rasterized"] == "True",
            "contains_adversarial_text": row["contains_adversarial_text"] == "True",
        })
    households = []
    for hh, docs in sorted(by_household.items()):
        meta = HOUSEHOLD_SCENARIOS.get(hh, {})
        households.append({"household_id": hh, **meta, "documents": docs})
    return {"households": households}


@app.get("/api/households/{household_id}/documents/{filename}")
def get_household_document(household_id: str, filename: str):
    rows = _list_official_documents()
    match = next((r for r in rows if r["household_id"] == household_id and r["file_name"] == filename), None)
    if not match:
        raise HTTPException(status_code=404, detail="Unknown household document.")
    path = os.path.join(_OFFICIAL_DOCS_DIR, filename)
    return FileResponse(path, media_type="application/pdf", filename=filename)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
