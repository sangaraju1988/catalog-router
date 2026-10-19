"""End-to-end tests through the real MCP protocol (in-memory transport)."""

from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client

from catalog_router.server import build_server

EXAMPLE = str(Path(__file__).resolve().parents[1] / "catalog" / "example-catalog.yaml")


def _run(coro_fn):
    return anyio.run(coro_fn)


def test_tools_are_listed():
    async def go():
        async with Client(build_server(EXAMPLE)) as c:
            tools = await c.list_tools()
            return {t.name for t in tools.tools}

    assert _run(go) == {"route_question", "explain_entity", "list_systems", "reload_catalog"}


def test_route_question_over_mcp():
    async def go():
        async with Client(build_server(EXAMPLE)) as c:
            res = await c.call_tool("route_question", {"question": "How is net revenue calculated?"})
            return res

    res = _run(go)
    assert not res.is_error
    data = res.structured_content
    assert data["primary"]["system"] == "dbt"
    assert data["primary"]["mcp_server"] == "dbt"


def test_list_systems_over_mcp():
    async def go():
        async with Client(build_server(EXAMPLE)) as c:
            return await c.call_tool("list_systems", {})

    data = _run(go).structured_content
    assert {s["id"] for s in data["systems"]} == {"dbt", "looker", "tableau", "superset", "bigquery"}


def test_reload_keeps_previous_catalog_on_error(tmp_path):
    cat = tmp_path / "catalog.yaml"
    cat.write_text(Path(EXAMPLE).read_text())

    async def go():
        server = build_server(str(cat))
        async with Client(server) as c:
            cat.write_text("version: 1\nsystems: []\n")
            bad = await c.call_tool("reload_catalog", {})
            still = await c.call_tool("list_systems", {})
            return bad.structured_content, still.structured_content

    bad, still = _run(go)
    assert bad["reloaded"] is False
    assert len(still["systems"]) == 5


@pytest.mark.parametrize("q", ["", "   "])
def test_empty_question_is_a_tool_error(q):
    async def go():
        async with Client(build_server(EXAMPLE)) as c:
            return await c.call_tool("route_question", {"question": q})

    assert _run(go).is_error
