"""Deterministic, explainable routing over the semantic catalog graph.

Given a natural-language question, decide which downstream BI MCP server
(dbt, Looker, Tableau, Superset, BigQuery, ...) should answer it.

The algorithm is intentionally simple and fully traceable:

1. Entity matching  - find catalog entities mentioned in the question
                      (longest synonym wins, no overlapping matches).
2. Intent detection - score capabilities (metric_definition, lineage,
                      dashboard, ...) from trigger phrases.
3. Scoring          - each system earns points for intents it supports and
                      for being the home (or an exposure point) of matched
                      entities.
4. Governance       - PII-touching questions are restricted to systems that
                      may serve PII and flagged for approval; governed systems
                      win near-ties against ungoverned ones.

Every step appends a human-readable line to `trace`, so an agent (or a
reviewer) can see exactly why a route was chosen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .catalog import Catalog, Entity, normalize

HOME_SYSTEM_POINTS = 3.0
EXPOSED_IN_POINTS = 2.5
INTENT_CAP_MULTIPLIER = 2.0  # an intent can contribute at most weight * this
# For "what does X mean / where does X come from" questions, the system that
# OWNS the definition is the source of truth, not every place that displays it.
AUTHORITATIVE_INTENTS = frozenset({"metric_definition", "lineage"})
AUTHORITY_POINTS = 2.0


@dataclass
class EntityMatch:
    entity: Entity
    matched_text: str


@dataclass
class Candidate:
    system_id: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    excluded: str | None = None


_SUFFIX = r"(?:s|es|d|ed|ing)?"  # light inflection: churn->churned, dashboard->dashboards


@lru_cache(maxsize=4096)
def _phrase_regex(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w])" + re.escape(phrase) + _SUFFIX + r"(?![\w])")


def match_entities(catalog: Catalog, question: str) -> list[EntityMatch]:
    q = normalize(question)
    hits: list[tuple[int, int, Entity, str]] = []
    for ent in catalog.entities.values():
        for syn in {*ent.synonyms, normalize(ent.name), normalize(ent.id)}:
            if not syn:
                continue
            for m in _phrase_regex(syn).finditer(q):
                hits.append((m.start(), m.end(), ent, syn))
    # Longest match first; accept only non-overlapping spans.
    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
    taken: list[tuple[int, int]] = []
    chosen: dict[str, EntityMatch] = {}
    for start, end, ent, syn in hits:
        if any(start < e and end > s for s, e in taken):
            continue
        taken.append((start, end))
        chosen.setdefault(ent.id, EntityMatch(ent, syn))
    return list(chosen.values())


def detect_intents(catalog: Catalog, question: str) -> dict[str, float]:
    q = normalize(question)
    scores: dict[str, float] = {}
    for cap, intent in catalog.intents.items():
        n = sum(1 for p in intent.phrases if _phrase_regex(p).search(q))
        if n:
            scores[cap] = min(n * intent.weight, intent.weight * INTENT_CAP_MULTIPLIER)
    return scores


def route(catalog: Catalog, question: str, top_k: int = 3) -> dict[str, Any]:
    question = question.strip()
    if not question:
        raise ValueError("question must not be empty")

    trace: list[str] = []
    matches = match_entities(catalog, question)
    intents = detect_intents(catalog, question)

    if matches:
        trace.append(
            "Matched entities: "
            + ", ".join(f"{m.entity.name} ({m.entity.id}) via '{m.matched_text}'" for m in matches)
        )
    else:
        trace.append("No catalog entities matched.")
    if intents:
        trace.append(
            "Detected intents: " + ", ".join(f"{k}={v:g}" for k, v in sorted(intents.items(), key=lambda kv: -kv[1]))
        )
    else:
        trace.append("No intent phrases detected.")

    candidates = {sid: Candidate(sid) for sid in catalog.systems}

    for cap, pts in intents.items():
        for sid, sys in catalog.systems.items():
            if cap in sys.capabilities:
                candidates[sid].score += pts
                candidates[sid].reasons.append(f"+{pts:g} supports {cap}")

    for m in matches:
        ent = m.entity
        candidates[ent.system].score += HOME_SYSTEM_POINTS
        candidates[ent.system].reasons.append(f"+{HOME_SYSTEM_POINTS:g} home of {ent.name}")
        if AUTHORITATIVE_INTENTS & intents.keys():
            candidates[ent.system].score += AUTHORITY_POINTS
            candidates[ent.system].reasons.append(f"+{AUTHORITY_POINTS:g} source of truth for {ent.name}")
        for sid in ent.also_in:
            candidates[sid].score += EXPOSED_IN_POINTS
            candidates[sid].reasons.append(f"+{EXPOSED_IN_POINTS:g} exposes {ent.name}")

    # -- governance -----------------------------------------------------------
    governance_notes: list[str] = []
    pii_entities = [m.entity for m in matches if m.entity.pii]
    requires_approval = False
    if pii_entities:
        names = ", ".join(e.name for e in pii_entities)
        governance_notes.append(f"Question touches PII: {names}.")
        for sid, cand in candidates.items():
            if not catalog.systems[sid].pii_allowed:
                cand.excluded = "not permitted to serve PII"
        if catalog.governance.pii_requires_approval:
            requires_approval = True
            governance_notes.append("PII access requires approval before the downstream call is made.")
        trace.append(f"Governance: PII detected ({names}); restricted to PII-permitted systems.")

    ranked = sorted(
        (c for c in candidates.values() if c.score > 0 and c.excluded is None),
        key=lambda c: (-c.score, list(catalog.systems).index(c.system_id)),
    )

    # Prefer a governed system over an ungoverned one on a near-tie.
    if len(ranked) >= 2 and not catalog.systems[ranked[0].system_id].governed:
        margin = catalog.governance.prefer_governed_margin
        for i, cand in enumerate(ranked[1:], start=1):
            if ranked[0].score - cand.score > margin:
                break
            if catalog.systems[cand.system_id].governed:
                trace.append(
                    f"Governance: preferred governed '{cand.system_id}' over ungoverned "
                    f"'{ranked[0].system_id}' (within margin {margin:g})."
                )
                ranked.insert(0, ranked.pop(i))
                break

    for c in candidates.values():
        if c.excluded:
            trace.append(f"Excluded {c.system_id}: {c.excluded}.")

    if not ranked:
        return {
            "question": question,
            "decision": "no_match",
            "primary": None,
            "fallbacks": [],
            "confidence": 0.0,
            "confidence_label": "none",
            "requires_approval": requires_approval,
            "governance_notes": governance_notes,
            "matched_entities": [_entity_view(m.entity, None) for m in matches],
            "detected_intents": intents,
            "trace": trace,
            "next_step": (
                "No system could be chosen. Ask the user which metric, dashboard, or table they mean, "
                "or call list_systems to show what is available."
            ),
        }

    top = ranked[0]
    second = ranked[1].score if len(ranked) > 1 else 0.0
    # 0.5 = coin flip between top two, 1.0 = no competitor at all.
    confidence = 0.5 + 0.5 * (top.score - second) / top.score
    if not matches:
        confidence *= 0.6  # intent-only routes are weaker: no entity grounding
    confidence = round(confidence, 2)
    label = "high" if confidence >= 0.7 else "medium" if confidence >= 0.55 else "low"

    sys = catalog.systems[top.system_id]
    capability = _best_capability(sys.capabilities, intents)
    primary_entities = [_entity_view(m.entity, top.system_id) for m in matches]
    trace.append("Scores: " + ", ".join(f"{c.system_id}={c.score:g}" for c in ranked))
    trace.append(f"Chose {top.system_id}: " + "; ".join(top.reasons))

    decision = "needs_approval" if requires_approval else "route"
    return {
        "question": question,
        "decision": decision,
        "primary": {
            "system": sys.id,
            "mcp_server": sys.mcp_server,
            "kind": sys.kind,
            "capability": capability,
            "governed": sys.governed,
            "score": round(top.score, 2),
            "entities": primary_entities,
        },
        "fallbacks": [
            {
                "system": c.system_id,
                "mcp_server": catalog.systems[c.system_id].mcp_server,
                "score": round(c.score, 2),
            }
            for c in ranked[1:top_k]
        ],
        "confidence": confidence,
        "confidence_label": label,
        "requires_approval": requires_approval,
        "governance_notes": governance_notes,
        "matched_entities": [_entity_view(m.entity, None) for m in matches],
        "detected_intents": intents,
        "trace": trace,
        "next_step": _next_step(sys.mcp_server, capability, primary_entities, requires_approval),
    }


def _best_capability(caps: tuple[str, ...], intents: dict[str, float]) -> str:
    supported = [(intents[c], c) for c in caps if c in intents]
    return max(supported)[1] if supported else "lookup"


def _entity_view(ent: Entity, system_id: str | None) -> dict[str, Any]:
    view: dict[str, Any] = {"id": ent.id, "name": ent.name, "type": ent.type, "home_system": ent.system}
    if system_id is not None:
        if system_id == ent.system:
            view["ref"] = ent.ref
        elif system_id in ent.also_in:
            view["ref"] = ent.also_in[system_id]
        else:
            view["ref"] = None
    else:
        view["ref"] = ent.ref
    if ent.pii:
        view["pii"] = True
    return view


def _next_step(server: str, capability: str, entities: list[dict[str, Any]], approval: bool) -> str:
    refs = [e["ref"] for e in entities if e.get("ref")]
    ref_txt = f" using {', '.join(refs)}" if refs else ""
    step = f"Call a tool on the '{server}' MCP server for {capability}{ref_txt}."
    if approval:
        step = "Get explicit user approval for PII access first. Then: " + step
    return step


def explain_entity(catalog: Catalog, name: str) -> dict[str, Any]:
    ent = catalog.find_entity(name)
    if ent is None:
        q = normalize(name)
        suggestions = sorted(
            {e.name for e in catalog.entities.values() if q and any(q in s for s in (*e.synonyms, normalize(e.name)))}
        )
        return {"found": False, "query": name, "suggestions": suggestions}

    def brief(eid: str) -> dict[str, Any]:
        e = catalog.entities[eid]
        return {"id": e.id, "name": e.name, "type": e.type, "system": e.system, **({"pii": True} if e.pii else {})}

    up = catalog.upstream_closure(ent.id)
    down = catalog.downstream_closure(ent.id)
    return {
        "found": True,
        "entity": {
            "id": ent.id,
            "name": ent.name,
            "type": ent.type,
            "home_system": ent.system,
            "ref": ent.ref,
            "also_in": ent.also_in,
            "pii": ent.pii,
            "pii_fields": list(ent.pii_fields),
        },
        "upstream": [brief(e) for e in up],
        "downstream": [brief(e) for e in down],
        "pii_upstream": [catalog.entities[e].name for e in up if catalog.entities[e].pii],
    }
