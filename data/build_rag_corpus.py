"""
Builds the narrative-rule RAG corpus for the LIHTC/Section 42 Tenant Income
Certification program (San Diego Housing Commission).

Scope discipline ("ONE PROGRAM: freeze the rules"): only documents directly
about LIHTC/Section 42 income determination and the Tenant Income
Certification process are included. Numeric thresholds (income/rent limits)
are NOT answered from this corpus -- those stay on the deterministic
structured-lookup path (rules_engine.py) and are excluded here on purpose so
the RAG path can never be asked to recite a number it might get wrong.

Run:  python build_rag_corpus.py
Writes data/rag/chunks.json (chunk_id, file, page, text).
"""
import json
import os
import re

import pdfplumber

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA1 = os.path.join(_HERE, "..", "..", "DATA_1")
_OUT = os.path.join(_HERE, "rag")
os.makedirs(_OUT, exist_ok=True)

# Curated, in-scope source documents (narrative LIHTC/Section 42 + TIC rules only).
CORPUS_FILES = [
    "Section-42.pdf",
    "income-inclusions-and-exclusions.pdf",
    "Interim-FAQ.pdf",
    "Policy-Changes-for-Income-Interims.pdf",
    "hotma_tic.pdf",
    "Zero-Income-Certification.pdf",
    "Verification of Employment (Final).pdf",
    "VerificationOfAssets.pdf",
    "Utility-Allowance-Chart.pdf",
]

CHUNK_SIZE = 900
CHUNK_OVERLAP = 150


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _chunk_page_text(text: str):
    text = _clean(text)
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def build():
    all_chunks = []
    chunk_id = 0
    for fname in CORPUS_FILES:
        path = os.path.join(_DATA1, fname)
        if not os.path.exists(path):
            print(f"SKIP (not found): {fname}")
            continue
        try:
            with pdfplumber.open(path) as pdf:
                for page_num, page in enumerate(pdf.pages, start=1):
                    text = page.extract_text() or ""
                    for piece in _chunk_page_text(text):
                        all_chunks.append({
                            "chunk_id": chunk_id,
                            "file": fname,
                            "page": page_num,
                            "text": piece,
                        })
                        chunk_id += 1
        except Exception as e:
            print(f"ERROR reading {fname}: {e}")

    out_path = os.path.join(_OUT, "chunks.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "corpus_id": "sdhc-lihtc-2026-narrative",
            "corpus_version": "2026.1",
            "source_files": CORPUS_FILES,
            "chunk_count": len(all_chunks),
            "chunks": all_chunks,
        }, f, indent=2)
    print(f"Wrote {len(all_chunks)} chunks from {len(CORPUS_FILES)} files to {out_path}")


if __name__ == "__main__":
    build()
