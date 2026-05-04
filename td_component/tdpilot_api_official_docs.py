"""TDPilot API — official TouchDesigner docs lookup + recommendations.

Tier 1 + 2 port from the CLI variant. Eight tools that route through
the existing ``tdpilot_api_knowledge`` BM25 infrastructure. The "official
docs" come from a docsbrain pages.jsonl auto-discovered at
``~/.tdpilot/data/normalized/derivative/``. Without that corpus on disk,
the tools return a structured ``hint`` rather than failing.

Two correctness properties this module enforces:

  1. **Distinguish "no matches" from "no corpus".** Earlier revisions
     conflated the two: any time count==0 we'd return the
     "corpus not installed" hint, which produced false negatives when
     the corpus was installed but the query had no hits. Now we check
     ``_corpus_installed()`` against the auto-discovery output before
     concluding the corpus is missing.

  2. **Soft category filtering instead of strict.** Derivative pages
     have multi-faceted doc_types — a noiseTOP doc page might be
     ``python_api`` (covering the Python class), not ``operator``. A
     strict ``category=operator`` filter would exclude it. Instead we
     run BM25 over the entire corpus and use the caller's ``doc_type``
     as a soft re-rank signal (boost matches whose category equals the
     hint).

Schema cost: ~8 × 200 tokens added to the system prompt per turn. With
DeepSeek's auto-cache, the marginal cost on cache hits is ~0.16%. The
tools execute in single-digit milliseconds (BM25 over ~2.5K pages).
"""

from __future__ import annotations

from typing import Any

# All eight tools delegate to handle_knowledge_search / handle_knowledge_get
# via lazy imports — this module stays decoupled from the knowledge module's
# import-time TD globals so the unit tests can patch the corpus without
# pulling in TD itself.

DERIVATIVE_CORPUS = "derivative"
EXAMPLES_CORPUS = "examples"
PALETTE_CATEGORY = "palette"


def _kb_search(query: str, **filters: Any) -> dict:
    """Route to handle_knowledge_search with the supplied filters.

    Lazy import keeps this module loadable outside TD (for tests). The
    knowledge module exposes the right surface — corpus + category +
    top_k — so we just pass through.
    """
    from tdpilot_api_knowledge import handle_knowledge_search  # type: ignore[import-not-found]

    body = {"query": query}
    body.update(filters)
    return handle_knowledge_search(body)


def _kb_get(name: str) -> dict:
    from tdpilot_api_knowledge import handle_knowledge_get  # type: ignore[import-not-found]

    return handle_knowledge_get({"name": name})


def _corpus_installed(corpus_name: str) -> bool:
    """True iff the named corpus is discoverable via auto-discovery.

    Checks the actual auto-discovery output rather than just looking for
    a directory on disk — that way we agree with what knowledge_search
    will actually search over. Cached at the tdpilot_api_knowledge layer
    so this is cheap to call repeatedly.
    """
    try:
        from tdpilot_api_knowledge import _external_corpora_entries  # type: ignore[import-not-found]

        return any(e.get("corpus") == corpus_name for e in _external_corpora_entries())
    except Exception:
        return False


def _no_corpus_hint(corpus: str) -> dict:
    """Standard "atlas not installed" response. Returned only when the
    corpus is genuinely absent from auto-discovery."""
    return {
        "ok": True,
        "count": 0,
        "matches": [],
        "available": False,
        "hint": (
            f"The '{corpus}' corpus isn't installed locally. "
            f"Drop a pages.jsonl at ~/.tdpilot/data/normalized/{corpus}/ "
            f"to enable this lookup. Without the corpus, knowledge_search "
            f"and friends still cover the bundled markdown set."
        ),
    }


def _soft_rerank(matches: list[dict], doc_type: str | None) -> list[dict]:
    """Stable re-rank: matches whose category equals doc_type bubble up,
    others keep their relative BM25 order. Pure tiebreaker — never
    excludes anything."""
    if not doc_type:
        return matches
    target = doc_type.lower()
    indexed = list(enumerate(matches))
    indexed.sort(key=lambda pair: 0 if (pair[1].get("category") or "").lower() == target else 1)
    return [m for _, m in indexed]


# ---------------------------------------------------------------------------
# Tier 1 — direct lookup wrappers
# ---------------------------------------------------------------------------


def handle_search_official_docs(body: dict) -> dict:
    """Search the derivative docsbrain corpus by query + optional doc_type.

    Returns BM25-ranked excerpts with source URLs from the entire
    derivative corpus. The optional ``doc_type`` parameter is a soft
    re-rank hint — matches in that category bubble to the top, but
    nothing is excluded. This avoids false negatives when a relevant
    page is filed under an adjacent doc_type (e.g. noiseTOP pages live
    in ``python_api``, not ``operator``).
    """
    query = (body.get("query") or "").strip()
    if not query:
        return {"error": "Missing required field: query"}
    doc_type = (body.get("doc_type") or "").strip().lower() or None
    top_k = body.get("top_k", 5)

    if not _corpus_installed(DERIVATIVE_CORPUS):
        return {**_no_corpus_hint(DERIVATIVE_CORPUS), "query": query}

    out = _kb_search(query, corpus=DERIVATIVE_CORPUS, top_k=top_k)
    matches = _soft_rerank(out.get("matches", []), doc_type)
    return {
        "ok": True,
        "available": True,
        "count": len(matches),
        "matches": matches,
        "doc_type_hint_applied": bool(doc_type),
    }


def handle_get_operator_doc(body: dict) -> dict:
    """Fetch full documentation for a specific operator type.

    Tries an exact-name lookup first across the entire knowledge index.
    If that misses, falls back to a BM25 search restricted to the
    derivative corpus (no category filter — the relevant page may be in
    ``python_api`` for class-level docs).
    """
    op_type = (body.get("op_type") or body.get("name") or "").strip()
    if not op_type:
        return {"error": "Missing required field: op_type"}

    # Exact-name lookup first (cheap, hits any installed corpus).
    direct = _kb_get(op_type)
    if direct.get("ok"):
        return direct

    if not _corpus_installed(DERIVATIVE_CORPUS):
        return {
            **_no_corpus_hint(DERIVATIVE_CORPUS),
            "ok": False,
            "not_found": op_type,
        }

    # BM25 fallback over the whole derivative corpus — soft-rerank
    # toward operator + python_api categories which typically host
    # operator-level docs.
    fallback = _kb_search(op_type, corpus=DERIVATIVE_CORPUS, top_k=5)
    matches = fallback.get("matches", [])
    matches = _soft_rerank(matches, "operator")
    matches = _soft_rerank(matches, "python_api")
    if not matches:
        return {
            "ok": False,
            "not_found": op_type,
            "hint": (
                "No matches in the derivative corpus. The operator name "
                "may be a TD 2025 native op without a docs page yet, or "
                "the corpus index needs rebuilding."
            ),
        }
    return {"ok": True, "approximate": True, "count": len(matches), "matches": matches}


def handle_get_param_help(body: dict) -> dict:
    """Look up parameter-level help for an operator.

    No structured per-parameter index in pages.jsonl, so this is a
    targeted BM25 search with op_type + param_name as the query against
    the entire derivative corpus.
    """
    op_type = (body.get("op_type") or "").strip()
    param_name = (body.get("param") or body.get("param_name") or "").strip()
    if not op_type or not param_name:
        return {"error": "Missing required field: op_type and param (or param_name)"}

    if not _corpus_installed(DERIVATIVE_CORPUS):
        return {
            **_no_corpus_hint(DERIVATIVE_CORPUS),
            "ok": False,
            "not_found": f"{op_type}.{param_name}",
        }

    query = f"{op_type} {param_name} parameter"
    out = _kb_search(query, corpus=DERIVATIVE_CORPUS, top_k=5)
    matches = out.get("matches", [])
    if not matches:
        return {
            "ok": False,
            "not_found": f"{op_type}.{param_name}",
            "hint": (
                "No matches. Fall back to td_get_params on a live op or "
                "td_python_help on the param name for runtime info."
            ),
        }
    return {"ok": True, "count": len(matches), "matches": matches}


def handle_lookup_snippets(body: dict) -> dict:
    """Look up code snippets by topic.

    Searches the entire knowledge index, soft-ranking ``snippet``-typed
    pages first.
    """
    topic = (body.get("topic") or body.get("query") or "").strip()
    if not topic:
        return {"error": "Missing required field: topic"}
    top_k = body.get("top_k", 5)

    out = _kb_search(topic, top_k=top_k * 2)
    matches = _soft_rerank(out.get("matches", []), "snippet")[:top_k]
    return {"ok": True, "count": len(matches), "matches": matches}


def handle_lookup_palette_component(body: dict) -> dict:
    """Look up a TouchDesigner Palette component by name or topic.

    Searches the entire knowledge index, soft-ranking ``palette``-typed
    pages first. Falls back to a name-augmented query if the first pass
    yields nothing.
    """
    name = (body.get("name") or body.get("query") or "").strip()
    if not name:
        return {"error": "Missing required field: name (or query)"}
    top_k = body.get("top_k", 5)

    out = _kb_search(name, top_k=top_k * 2)
    matches = _soft_rerank(out.get("matches", []), PALETTE_CATEGORY)[:top_k]
    if matches:
        return {"ok": True, "count": len(matches), "matches": matches}

    # Fallback: augment query with "palette component" and try again.
    fallback = _kb_search(f"{name} palette component", top_k=top_k)
    return {"ok": True, "count": fallback.get("count", 0), "matches": fallback.get("matches", [])}


# ---------------------------------------------------------------------------
# Tier 2 — LLM-flavored recommendation tools
# ---------------------------------------------------------------------------


def handle_recommend_official_component(body: dict) -> dict:
    """Given a goal description, surface relevant official Palette components,
    operator-level docs, and code snippets the agent can reason over before
    building from scratch.

    Three soft-ranked passes over the goal: palette-leaning, operator-leaning,
    snippet-leaning. Each pass searches the whole corpus and re-ranks toward
    the requested category.
    """
    goal = (body.get("goal") or body.get("description") or "").strip()
    if not goal:
        return {"error": "Missing required field: goal (or description)"}

    raw = _kb_search(goal, top_k=20)
    pool = raw.get("matches", [])

    palette_hits = _soft_rerank(list(pool), PALETTE_CATEGORY)[:3]
    op_hits = _soft_rerank(list(pool), "operator")[:3]
    snippet_hits = _soft_rerank(list(pool), "snippet")[:2]

    return {
        "ok": True,
        "goal": goal,
        "palette_components": palette_hits,
        "operators": op_hits,
        "snippets": snippet_hits,
        "hint": (
            "Reason over these matches before calling td_create_node. "
            "If a Palette component fits, prefer td_copy_node from the Palette "
            "container over building from scratch."
        ),
    }


def handle_find_official_example(body: dict) -> dict:
    """Find TouchDesigner-shipped examples for a topic.

    Searches the 'examples' corpus first if installed; falls back to
    soft-ranking ``snippet``/``operator`` hits in the derivative corpus.
    """
    topic = (body.get("topic") or body.get("query") or "").strip()
    if not topic:
        return {"error": "Missing required field: topic"}
    top_k = body.get("top_k", 5)

    if _corpus_installed(EXAMPLES_CORPUS):
        ex = _kb_search(topic, corpus=EXAMPLES_CORPUS, top_k=top_k)
        if ex.get("count", 0) > 0:
            return {"ok": True, "source": EXAMPLES_CORPUS, **ex}

    if _corpus_installed(DERIVATIVE_CORPUS):
        out = _kb_search(topic, corpus=DERIVATIVE_CORPUS, top_k=top_k * 2)
        matches = _soft_rerank(out.get("matches", []), "snippet")
        matches = _soft_rerank(matches, "operator")[:top_k]
        if matches:
            return {
                "ok": True,
                "source": "derivative:soft-rerank",
                "count": len(matches),
                "matches": matches,
            }

    return {**_no_corpus_hint(EXAMPLES_CORPUS), "topic": topic}


def handle_explain_better_way(body: dict) -> dict:
    """Given a current approach + goal, surface canonical alternatives
    from the docs corpus.

    Constructs two queries — one biased toward "best practice" + the
    goal, one biased toward the current approach + "alternative" — and
    returns the union of matches sorted by score. The model then explains
    the trade-off.
    """
    current = (body.get("current_approach") or body.get("current") or "").strip()
    goal = (body.get("goal") or "").strip()
    if not goal:
        return {"error": "Missing required field: goal"}

    queries: list[str] = [f"{goal} best practice canonical"]
    if current:
        queries.append(f"{current} alternative {goal}")

    seen_names: set[str] = set()
    merged: list[dict] = []
    for q in queries:
        out = _kb_search(q, top_k=4)
        for match in out.get("matches", []):
            key = match.get("name") or ""
            if key in seen_names:
                continue
            seen_names.add(key)
            merged.append(match)

    return {
        "ok": True,
        "goal": goal,
        "current_approach": current or None,
        "queries_used": queries,
        "matches": merged[:6],
        "hint": (
            "Each match is an excerpt from the docs corpus. Prefer the "
            "canonical TD pattern unless the user has explicit context "
            "for the current approach."
        ),
    }
