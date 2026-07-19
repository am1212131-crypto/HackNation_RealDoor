"""
Embedding index over the frozen narrative-rule corpus (data/rag/chunks.json).

This index is built ONLY from organizer-provided regulatory reference PDFs
(see data/build_rag_corpus.py) -- never from renter-uploaded documents. It is
safe to cache to disk because it contains no renter data at all.

Fails closed: if OPENAI_API_KEY is not configured, or embeddings can't be
built/loaded, retrieve() returns an empty list and the caller (rag_engine.py)
abstains rather than guessing.
"""
import json
import math
import os

from . import openai_client

_HERE = os.path.dirname(os.path.abspath(__file__))
_CHUNKS_PATH = os.path.join(_HERE, "..", "data", "rag", "chunks.json")
_EMBEDDINGS_PATH = os.path.join(_HERE, "..", "data", "rag", "embeddings.json")

_chunks = None
_vectors = None  # list[list[float]], aligned by index with _chunks


def _load_chunks():
    global _chunks
    if _chunks is None:
        with open(_CHUNKS_PATH, "r", encoding="utf-8") as f:
            _chunks = json.load(f)["chunks"]
    return _chunks


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed_batch(texts: list) -> list:
    client = openai_client.get_client()
    resp = client.embeddings.create(model=openai_client.EMBEDDING_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def is_available() -> bool:
    return openai_client.is_configured() and os.path.exists(_CHUNKS_PATH)


def ensure_index_built():
    """Builds (or loads a cached) embedding index. Returns True if the index
    is ready to query, False if unavailable (e.g. no API key)."""
    global _vectors
    if not is_available():
        return False

    chunks = _load_chunks()

    if _vectors is not None and len(_vectors) == len(chunks):
        return True

    if os.path.exists(_EMBEDDINGS_PATH):
        with open(_EMBEDDINGS_PATH, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("chunk_count") == len(chunks) and cached.get("model") == openai_client.EMBEDDING_MODEL:
            _vectors = cached["vectors"]
            return True

    # (Re)build. Batch to stay well under request size limits.
    texts = [c["text"] for c in chunks]
    vectors = []
    batch_size = 64
    try:
        for i in range(0, len(texts), batch_size):
            vectors.extend(_embed_batch(texts[i:i + batch_size]))
    except Exception:
        return False

    _vectors = vectors
    os.makedirs(os.path.dirname(_EMBEDDINGS_PATH), exist_ok=True)
    with open(_EMBEDDINGS_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "model": openai_client.EMBEDDING_MODEL,
            "chunk_count": len(chunks),
            "vectors": vectors,
        }, f)
    return True


def retrieve(question: str, top_k: int = 4, min_score: float = 0.25):
    """Returns up to top_k chunks with cosine similarity >= min_score,
    sorted best-first. Empty list if the index isn't available or nothing
    clears the threshold (caller must abstain in that case)."""
    if not ensure_index_built():
        return []

    chunks = _load_chunks()
    try:
        q_vec = _embed_batch([question])[0]
    except Exception:
        return []

    scored = [
        {**chunks[i], "score": _cosine(q_vec, _vectors[i])}
        for i in range(len(chunks))
    ]
    scored.sort(key=lambda c: c["score"], reverse=True)
    results = [c for c in scored[:top_k] if c["score"] >= min_score]
    return results
