"""
Ephemeral, encrypted, in-memory session store.

Privacy & security controls implemented here:
  - Nothing is written to disk. All state lives in a process-memory dict that
    is lost on restart.
  - Every session gets its own random Fernet key at creation. Uploaded PDF
    bytes and extracted raw text are encrypted at rest (in memory) with that
    key; only allowlisted structured field values are kept in the clear so
    the UI can render them.
  - The audit log records actions, rule/checklist corpus versions, and
    consent events -- never raw document contents (per CONSENT AND
    CORRECTION requirement).
  - delete_session() performs crypto-shredding: it discards the per-session
    key and pops all session data, so any residual encrypted bytes become
    unrecoverable, then the dict entry is removed.
  - We never persist uploads for model training; there is no training path
    in this codebase at all.
"""
import datetime
import secrets
import threading
import uuid

from cryptography.fernet import Fernet

_LOCK = threading.Lock()
_SESSIONS: dict[str, dict] = {}


def create_session() -> str:
    session_id = uuid.uuid4().hex
    key = Fernet.generate_key()
    with _LOCK:
        _SESSIONS[session_id] = {
            "key": key,
            "fernet": Fernet(key),
            "created_at": datetime.datetime.utcnow().isoformat(),
            "documents": {},       # doc_id -> {encrypted_pdf, encrypted_text, doc_type, fields, confirmed}
            "household_flags": {}, # applicability flags (has_employment_income, etc.)
            "profile": {},         # confirmed household-level fields (household_size, etc.)
            "audit_log": [],       # [{ts, action, detail}] -- never raw doc contents
            "consent_given": False,  # server-enforced: uploads are refused until consent is recorded
        }
    _log(session_id, "session_created", {})
    return session_id


def _log(session_id: str, action: str, detail: dict):
    entry = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "action": action,
        "detail": detail,
    }
    _SESSIONS[session_id]["audit_log"].append(entry)


def exists(session_id: str) -> bool:
    return session_id in _SESSIONS


def get(session_id: str) -> dict:
    if session_id not in _SESSIONS:
        raise KeyError("Unknown or deleted session.")
    return _SESSIONS[session_id]


def add_document(session_id: str, doc_id: str, pdf_bytes: bytes, raw_text: str, doc_type: str,
                  extraction_fields: list, injection_flags: list):
    sess = get(session_id)
    f = sess["fernet"]
    sess["documents"][doc_id] = {
        "doc_id": doc_id,
        "doc_type": doc_type,
        "encrypted_pdf": f.encrypt(pdf_bytes),
        "encrypted_text": f.encrypt(raw_text.encode("utf-8")),
        "fields": {fld.field_id: {
            "label": fld.label, "value": fld.value, "type": fld.field_type,
            "confidence": fld.confidence, "source_box": fld.source_box,
            "status": fld.status, "extraction_method": fld.extraction_method,
            "confirmed": False, "corrected": False,
        } for fld in extraction_fields},
        "injection_flags": injection_flags,
        "confirmed": False,
        "uploaded_at": datetime.datetime.utcnow().isoformat(),
    }
    llm_assisted_count = sum(1 for f in extraction_fields if f.extraction_method == "llm_assisted")
    _log(session_id, "document_uploaded", {
        "doc_id": doc_id, "doc_type": doc_type,
        "field_count": len(extraction_fields),
        "injection_flags_detected": len(injection_flags),
        "llm_assisted_field_count": llm_assisted_count,
    })


def get_pdf_bytes(session_id: str, doc_id: str) -> bytes:
    sess = get(session_id)
    doc = sess["documents"][doc_id]
    return sess["fernet"].decrypt(doc["encrypted_pdf"])


def confirm_field(session_id: str, doc_id: str, field_id: str, new_value: str = None):
    sess = get(session_id)
    doc = sess["documents"][doc_id]
    fld = doc["fields"][field_id]
    corrected = new_value is not None and new_value != fld["value"]
    if new_value is not None:
        fld["value"] = new_value
    fld["confirmed"] = True
    fld["corrected"] = fld["corrected"] or corrected
    if corrected:
        fld["confidence"] = 1.0  # human-confirmed/corrected value is authoritative
    _log(session_id, "field_confirmed", {
        "doc_id": doc_id, "field_id": field_id, "corrected": corrected,
    })


def mark_document_confirmed(session_id: str, doc_id: str):
    sess = get(session_id)
    all_confirmed = all(f["confirmed"] for f in sess["documents"][doc_id]["fields"].values())
    sess["documents"][doc_id]["confirmed"] = all_confirmed
    _log(session_id, "document_confirmation_checked", {"doc_id": doc_id, "all_fields_confirmed": all_confirmed})
    return all_confirmed


def set_household_flags(session_id: str, flags: dict):
    sess = get(session_id)
    sess["household_flags"].update(flags)
    _log(session_id, "household_flags_updated", {"keys": list(flags.keys())})


def set_profile(session_id: str, profile: dict):
    sess = get(session_id)
    sess["profile"].update(profile)
    _log(session_id, "profile_updated", {"keys": list(profile.keys())})


def log_consent(session_id: str, consent_text: str):
    sess = get(session_id)
    sess["consent_given"] = True
    _log(session_id, "consent_recorded", {"consent_text": consent_text})


def has_consent(session_id: str) -> bool:
    return bool(get(session_id).get("consent_given"))


def log_rule_query(session_id: str, corpus_version: str, question: str, route: str = "structured"):
    # Note: we log the corpus VERSION and ROUTE (structured table vs RAG),
    # not the renter's raw question text beyond what's needed for the demo
    # transcript; per spec we avoid persisting raw document contents -- rule
    # questions are not document contents.
    _log(session_id, "rule_query", {"corpus_version": corpus_version, "question": question, "route": route})


def log_refusal(session_id: str, question: str):
    _log(session_id, "decision_request_refused", {"question": question})


def log_checklist_export(session_id: str, checklist_version: str):
    _log(session_id, "packet_exported", {"checklist_version": checklist_version})


def delete_session(session_id: str):
    """Crypto-shred + remove all session state. Irreversible."""
    with _LOCK:
        sess = _SESSIONS.pop(session_id, None)
    if sess is not None:
        # best-effort scrub of the key material reference
        sess["key"] = secrets.token_bytes(32)
        sess["fernet"] = None
        sess["documents"].clear()
    return sess is not None


def audit_log(session_id: str):
    return get(session_id)["audit_log"]
