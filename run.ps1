if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& ".\.venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if (-not (Test-Path "data\rag\chunks.json")) {
    & ".\.venv\Scripts\python.exe" data\build_rag_corpus.py
}
& ".\.venv\Scripts\python.exe" -m uvicorn backend.main:app --port 8000 --reload
