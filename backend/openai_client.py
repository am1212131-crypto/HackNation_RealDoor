"""
Single shared place that reads OPENAI_API_KEY from the environment (via a
local, gitignored .env -- see .env.example) and hands out a lazily-created
OpenAI client. Every other module that wants to call OpenAI imports from
here instead of reading the env var itself, so there is exactly one place
the key touches the process.

The key is never logged, never included in any API response body, and never
sent to the frontend. If it's unset, is_configured() is False and every
caller in this codebase is required to fail closed (fall back to the
deterministic path) rather than error.
"""
import os

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
CHAT_MODEL = os.environ.get("REALDOOR_LLM_MODEL", "gpt-4o-mini")
EMBEDDING_MODEL = os.environ.get("REALDOOR_EMBEDDING_MODEL", "text-embedding-3-small")

_client = None


def is_configured() -> bool:
    return bool(API_KEY)


def get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=API_KEY)
    return _client
