"""Load and validate the semantic catalog graph."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class CatalogError(ValueError):
    """Raised when the catalog file is malformed or internally inconsistent."""


@dataclass(frozen=True)
class System:
    id: str
    kind: str
    mcp_server: str
    description: str
    capabilities: tuple[str, ...]
    governed: bool
    pii_allowed: bool


@dataclass(frozen=True)
class Intent:
    capability: str
    weight: float
    phrases: tuple[str, ...]


@dataclass(frozen=True)
class Entity:
    id: str
    type: str
    name: str
    synonyms: tuple[str, ...]
    system: str
    ref: str
    also_in: dict[str, str] = field(default_factory=dict)
    upstream: tuple[str, ...] = ()
    pii: bool = False
    pii_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class Governance:
    prefer_governed_margin: float = 1.0
    pii_requires_approval: bool = True


@dataclass
class Catalog:
    systems: dict[str, System]
    intents: dict[str, Intent]
    entities: dict[str, Entity]
    governance: Governance
    downstream: dict[str, list[str]] = field(default_factory=dict)

    # -- graph helpers -----------------------------------------------------

    def upstream_closure(self, entity_id: str) -> list[str]:
        """All transitive upstream entity ids, nearest first."""
        return self._walk(entity_id, lambda e: self.entities[e].upstream)

    def downstream_closure(self, entity_id: str) -> list[str]:
        """All transitive downstream entity ids, nearest first."""
        return self._walk(entity_id, lambda e: self.downstream.get(e, []))

    def _walk(self, start: str, nxt) -> list[str]:
        seen: list[str] = []
        frontier = list(nxt(start))
        while frontier:
            node = frontier.pop(0)
            if node in seen or node == start:
                continue
            seen.append(node)
            frontier.extend(nxt(node))
        return seen

    def find_entity(self, text: str) -> Entity | None:
        """Look up an entity by id, name, or synonym (case-insensitive)."""
        needle = normalize(text)
        for ent in self.entities.values():
            if needle in {normalize(ent.id), normalize(ent.name), *map(normalize, ent.synonyms)}:
                return ent
        return None


_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def _req(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise CatalogError(f"{where}: missing required field '{key}'")
    return obj[key]


def load_catalog(path: str | Path) -> Catalog:
    path = Path(path)
    if not path.is_file():
        raise CatalogError(f"catalog file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CatalogError(f"catalog is not valid YAML: {exc}") from exc
    return parse_catalog(raw)


def parse_catalog(raw: dict[str, Any]) -> Catalog:
    if raw.get("version") != 1:
        raise CatalogError("catalog 'version' must be 1")

    systems: dict[str, System] = {}
    for i, s in enumerate(raw.get("systems") or []):
        where = f"systems[{i}]"
        sid = _req(s, "id", where)
        if sid in systems:
            raise CatalogError(f"{where}: duplicate system id '{sid}'")
        systems[sid] = System(
            id=sid,
            kind=s.get("kind", sid),
            mcp_server=s.get("mcp_server", sid),
            description=s.get("description", ""),
            capabilities=tuple(_req(s, "capabilities", where)),
            governed=bool(s.get("governed", False)),
            pii_allowed=bool(s.get("pii_allowed", False)),
        )
    if not systems:
        raise CatalogError("catalog must define at least one system")

    intents: dict[str, Intent] = {}
    for cap, spec in (raw.get("intents") or {}).items():
        intents[cap] = Intent(
            capability=cap,
            weight=float(spec.get("weight", 1)),
            phrases=tuple(normalize(p) for p in _req(spec, "phrases", f"intents.{cap}")),
        )

    entities: dict[str, Entity] = {}
    for i, e in enumerate(raw.get("entities") or []):
        where = f"entities[{i}]"
        eid = _req(e, "id", where)
        if eid in entities:
            raise CatalogError(f"{where}: duplicate entity id '{eid}'")
        system = _req(e, "system", where)
        if system not in systems:
            raise CatalogError(f"{where} ({eid}): unknown system '{system}'")
        also_in = dict(e.get("also_in") or {})
        for other in also_in:
            if other not in systems:
                raise CatalogError(f"{where} ({eid}): also_in references unknown system '{other}'")
        entities[eid] = Entity(
            id=eid,
            type=e.get("type", "entity"),
            name=e.get("name", eid),
            synonyms=tuple(normalize(x) for x in e.get("synonyms") or []),
            system=system,
            ref=e.get("ref", ""),
            also_in=also_in,
            upstream=tuple(e.get("upstream") or []),
            pii=bool(e.get("pii", False)),
            pii_fields=tuple(e.get("pii_fields") or []),
        )

    downstream: dict[str, list[str]] = {}
    for ent in entities.values():
        for up in ent.upstream:
            if up not in entities:
                raise CatalogError(f"entity '{ent.id}': upstream references unknown entity '{up}'")
            downstream.setdefault(up, []).append(ent.id)

    _check_acyclic(entities)

    gov = raw.get("governance") or {}
    governance = Governance(
        prefer_governed_margin=float(gov.get("prefer_governed_margin", 1.0)),
        pii_requires_approval=bool(gov.get("pii_requires_approval", True)),
    )
    return Catalog(systems, intents, entities, governance, downstream)


def _check_acyclic(entities: dict[str, Entity]) -> None:
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(entities, WHITE)

    def visit(node: str, path: list[str]) -> None:
        color[node] = GREY
        for up in entities[node].upstream:
            if color[up] == GREY:
                cycle = " -> ".join([*path, node, up])
                raise CatalogError(f"lineage cycle detected: {cycle}")
            if color[up] == WHITE:
                visit(up, [*path, node])
        color[node] = BLACK

    for eid in entities:
        if color[eid] == WHITE:
            visit(eid, [])
