"""MCP server exposing the catalog router as tools."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .catalog import Catalog, CatalogError, load_catalog
from .router import explain_entity as _explain_entity
from .router import route as _route

DEFAULT_CATALOG = "/catalog/catalog.yaml"

INSTRUCTIONS = """\
catalog-router decides WHICH BI system should answer a data question.
Workflow: (1) call route_question with the user's question; (2) if decision is
"needs_approval", ask the user before touching PII; (3) call the downstream MCP
server named in primary.mcp_server, passing primary.entities[].ref; (4) if that
fails, try fallbacks in order. Use explain_entity for definitions and lineage,
and list_systems to see what is connected. This server never queries data itself.
"""

log = logging.getLogger("catalog_router")


def resolve_catalog_path(requested: str, bundled: str = DEFAULT_CATALOG) -> tuple[str, bool]:
    """Return (path to load, fell_back_to_example).

    When the requested catalog isn't a readable file (for example, the MCP
    Toolkit was enabled without choosing a catalog), fall back to the example
    bundled in the image so the server still starts and lists its tools.
    """
    if Path(requested).is_file() or requested == bundled or not Path(bundled).is_file():
        return requested, False
    log.warning("catalog %s not found; using the bundled example catalog %s", requested, bundled)
    return bundled, True


def build_server(catalog_path: str, fell_back_to_example: bool = False) -> MCPServer:
    state: dict[str, Any] = {
        "catalog": load_catalog(catalog_path),
        "path": catalog_path,
        "fell_back_to_example": fell_back_to_example,
    }

    def catalog_info() -> dict[str, Any]:
        info: dict[str, Any] = {"path": state["path"], "fell_back_to_example": state["fell_back_to_example"]}
        if state["fell_back_to_example"]:
            info["note"] = (
                "No catalog was configured, so the bundled example (a fictional company) is active. "
                "Point CATALOG_PATH at your own catalog to route questions for your real BI stack."
            )
        return info

    def catalog() -> Catalog:
        return state["catalog"]

    mcp = MCPServer(
        name="catalog-router",
        title="BI Catalog Router",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    @mcp.tool(title="Route a BI question")
    def route_question(question: str, top_k: int = 3) -> dict[str, Any]:
        """Decide which BI MCP server (dbt, Looker, Tableau, Superset, BigQuery, ...)
        should answer a natural-language data question.

        Returns the primary system, ranked fallbacks, matched catalog entities with
        the reference to use in the target system, a confidence score, governance
        flags (PII / approval), and a step-by-step trace of the decision.
        """
        return _route(catalog(), question, top_k=max(1, min(top_k, 10)))

    @mcp.tool(title="Explain a catalog entity")
    def explain_entity(name: str) -> dict[str, Any]:
        """Look up a metric, dashboard, model, table, or field by name or synonym.
        Returns where it is defined, where else it is exposed, its full upstream and
        downstream lineage, and whether any upstream source carries PII.
        """
        return _explain_entity(catalog(), name)

    @mcp.tool(title="List connected BI systems")
    def list_systems() -> dict[str, Any]:
        """List every BI system in the catalog with its MCP server name,
        capabilities, governance status, and how many entities it owns."""
        cat = catalog()
        owned: dict[str, int] = {}
        for e in cat.entities.values():
            owned[e.system] = owned.get(e.system, 0) + 1
        return {
            "systems": [
                {
                    "id": s.id,
                    "kind": s.kind,
                    "mcp_server": s.mcp_server,
                    "description": s.description,
                    "capabilities": list(s.capabilities),
                    "governed": s.governed,
                    "pii_allowed": s.pii_allowed,
                    "entities_owned": owned.get(s.id, 0),
                }
                for s in cat.systems.values()
            ],
            "entity_count": len(cat.entities),
            "catalog": catalog_info(),
        }

    @mcp.tool(title="Reload the catalog")
    def reload_catalog() -> dict[str, Any]:
        """Re-read the catalog file from disk (after editing the mounted YAML).
        On a validation error the previous catalog stays active."""
        try:
            state["catalog"] = load_catalog(state["path"])
        except CatalogError as exc:
            return {"reloaded": False, "error": str(exc)}
        cat = catalog()
        return {"reloaded": True, "systems": len(cat.systems), "entities": len(cat.entities)}

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        cat = catalog()
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "systems": len(cat.systems),
                "entities": len(cat.entities),
                "catalog": catalog_info(),
            }
        )

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalog-router", description=__doc__)
    parser.add_argument(
        "--catalog",
        default=os.environ.get("CATALOG_PATH", DEFAULT_CATALOG),
        help=f"path to catalog YAML (env CATALOG_PATH, default {DEFAULT_CATALOG})",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="stdio for Claude Desktop / Docker MCP Gateway; http for Compose (env MCP_TRANSPORT)",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--check", action="store_true", help="validate the catalog and exit")
    parser.add_argument("--version", action="version", version=f"catalog-router {__version__}")
    args = parser.parse_args(argv)

    # stdout belongs to the MCP protocol in stdio mode: log to stderr only.
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO"))

    # --check validates exactly the file you name; serving falls back to the
    # bundled example so an unconfigured install still starts.
    path, fell_back = (args.catalog, False) if args.check else resolve_catalog_path(args.catalog)

    try:
        server = build_server(path, fell_back_to_example=fell_back)
    except CatalogError as exc:
        print(f"catalog error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(f"catalog OK: {path}", file=sys.stderr)
        return 0

    log.info("catalog-router %s starting (%s) with %s", __version__, args.transport, path)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port, stateless_http=True)
    return 0
