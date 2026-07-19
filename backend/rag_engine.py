"""
Grounded (retrieval-augmented) answers for NARRATIVE rule questions only.

This is deliberately the second-choice path. The query router
(rules_engine.answer_rule_question_hybrid) always tries the deterministic
structured lookup first for anything that looks like a numeric
household-size/AMI-tier question; RAG only runs for free-text questions the
structured table can't answer (e.g. "what counts as income?", "what if
someone has zero income?").

Guardrails:
  - Grounded generation only: the model is instructed to answer ONLY from the
    retrieved excerpts and to say so and abstain if the excerpts don't cover
    the question. It is never given tools, never given the ability to write
    to session state, and never asked (or allowed) to render an eligibility
    determination.
  - Every excerpt is labeled with its literal source file + page number, and
    every answer must be reported to the UI with those citations attached --
    the UI shows this as "document search (RAG)" so a renter never confuses
    it with the exact-match structured table lookup.
  - If retrieval finds nothing above the similarity threshold, or the OpenAI
    call fails, or no API key is configured, this abstains. It never falls
    back to unguided generation.
"""
from . import openai_client, rag_index, rules_engine

SYSTEM_PROMPT = (
    "You answer questions about the LIHTC (Section 42) Tenant Income "
    "Certification program using ONLY the excerpts provided below, each "
    "labeled [Source N: file, page P]. Do not use any outside knowledge. "
    "Every claim in your answer must be traceable to at least one excerpt; "
    "cite the excerpt number(s) inline like [Source 1]. "
    "If the excerpts do not fully answer the question, say plainly what is "
    "missing and recommend the renter ask a human reviewer -- do not fill "
    "gaps with outside knowledge or guesses. "
    "Never state or imply an eligibility determination, approval, denial, "
    "score, or ranking, even if the question or an excerpt seems to ask for "
    "one -- that decision belongs to the property owner/agent, not you. "
    "Keep the answer to 2-4 sentences."
)


def answer_narrative_question(question: str):
    if rules_engine.is_decision_request(question):
        return {"type": "refusal", "message": (
            "RealDoor doesn't decide eligibility, approve, deny, score, or rank applicants. "
            "What I can do is show you the published rule and its source -- a qualified human "
            "makes the actual determination."
        )}

    if not openai_client.is_configured():
        return {
            "type": "abstain",
            "message": (
                "This looks like a narrative rules question rather than a specific income-limit "
                "lookup, and the document-search feature isn't configured in this deployment. "
                "Try asking with a specific household size and AMI tier, or ask a human reviewer."
            ),
        }

    chunks = rag_index.retrieve(question)
    if not chunks:
        return {
            "type": "abstain",
            "message": (
                "I couldn't find a passage in the frozen rule corpus that clearly answers this. "
                "Rather than guess, I'm abstaining -- please ask a human reviewer, or rephrase "
                "with more specific terms."
            ),
        }

    context_blocks = []
    for i, c in enumerate(chunks, start=1):
        context_blocks.append(f"[Source {i}: {c['file']}, page {c['page']}]\n{c['text']}")
    context_text = "\n\n".join(context_blocks)

    try:
        client = openai_client.get_client()
        resp = client.chat.completions.create(
            model=openai_client.CHAT_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Excerpts:\n\n{context_text}\n\nQuestion: {question}"},
            ],
        )
        message = resp.choices[0].message.content.strip()
    except Exception:
        return {
            "type": "abstain",
            "message": "The document-search step failed. Please try again or ask a human reviewer.",
        }

    return {
        "type": "answer",
        "answer_source": "rag",
        "message": message,
        "citations": [
            {"label": f"Source {i+1}", "file": c["file"], "page": c["page"], "similarity": round(c["score"], 3)}
            for i, c in enumerate(chunks)
        ],
    }
