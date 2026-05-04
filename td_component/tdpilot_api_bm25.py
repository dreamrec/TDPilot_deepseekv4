"""TDPilot API — shared BM25 scorer.

Pure-Python BM25 (no external deps). Sufficient for ~hundreds of small
docs. Used by tdpilot_api_memory, tdpilot_api_knowledge, and
tdpilot_api_recipes. Previously inlined in all three; extracted to one
place during the 2026-05-04 audit.

Public API:

    bm25_score(query, docs, index_fields=("name","description","text"),
               top_k=5, k1=1.5, b=0.75) -> list[tuple[dict, float]]

Returns ``(doc, score)`` pairs sorted by score descending, top_k entries
max, filtered to score > 0. Each caller formats its own match dicts
afterwards — separating scoring from output shape avoids the prior
"every module ships a near-identical 60-line BM25 loop with subtly
different output dicts" duplication.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Split text into lowercased word tokens. Used by callers that need
    to compute their own snippets / highlights."""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def bm25_score(
    query: str,
    docs: list[dict],
    index_fields: Iterable[str] = ("name", "description", "text"),
    top_k: int = 5,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[dict, float]]:
    """Score docs against query using BM25 and return top_k (doc, score) pairs.

    `index_fields` is the ordered set of dict keys whose values are
    folded into the searchable token stream for each doc. Default keys
    match the most common shape (`name`/`description`/`text`); recipes
    passes `('name','description','tags','text')` so tag lists are also
    indexed. Values are coerced via `str()` so list-typed fields like
    `tags=['foo','bar']` tokenize sensibly (`'foo bar'` after the
    surrounding `[]`/`,`/`'` are stripped by the `\\w+` regex).

    Docs scoring 0 are filtered out. The result is sorted by descending
    score and capped to top_k. The default k1=1.5, b=0.75 are the
    canonical BM25 constants and match the prior inline implementations.
    """
    query_terms = tokenize(query)
    if not query_terms or not docs:
        return []

    fields = tuple(index_fields)
    doc_tokens = [tokenize(" ".join(str(d.get(f, "")) for f in fields)) for d in docs]
    avg_dl = sum(len(t) for t in doc_tokens) / max(1, len(doc_tokens))
    n_docs = len(docs)

    df: dict[str, int] = {}
    for tokens in doc_tokens:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    scored: list[tuple[int, float]] = []
    for i, tokens in enumerate(doc_tokens):
        dl = len(tokens)
        if dl == 0:
            scored.append((i, 0.0))
            continue
        tf: dict[str, int] = {}
        for term in tokens:
            tf[term] = tf.get(term, 0) + 1
        score = 0.0
        for term in query_terms:
            f = tf.get(term, 0)
            if f == 0:
                continue
            n_qi = df.get(term, 0)
            idf = math.log((n_docs - n_qi + 0.5) / (n_qi + 0.5) + 1)
            num = f * (k1 + 1)
            denom = f + k1 * (1 - b + b * dl / avg_dl)
            score += idf * (num / denom)
        scored.append((i, score))

    scored.sort(key=lambda x: -x[1])
    return [(docs[idx], score) for idx, score in scored[:top_k] if score > 0]
