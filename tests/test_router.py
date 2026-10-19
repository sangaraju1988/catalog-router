from pathlib import Path

import pytest

from catalog_router.catalog import CatalogError, load_catalog, parse_catalog
from catalog_router.router import explain_entity, route

EXAMPLE = Path(__file__).resolve().parents[1] / "catalog" / "example-catalog.yaml"


@pytest.fixture(scope="module")
def cat():
    return load_catalog(EXAMPLE)


@pytest.mark.parametrize(
    "question, system, capability",
    [
        ("How is net revenue calculated?", "dbt", "metric_definition"),
        ("What is upstream of churn rate?", "dbt", "lineage"),
        ("what breaks if I change fct_orders", "dbt", "lineage"),
        ("Where can I see the exec weekly dashboard?", "looker", "dashboard"),
        ("Show me the finance workbook for revenue", "tableau", "dashboard"),
        ("Query the raw checkout events table for p95 latency by region", "bigquery", "raw_data"),
    ],
)
def test_routes_to_expected_system(cat, question, system, capability):
    r = route(cat, question)
    assert r["decision"] == "route"
    assert r["primary"]["system"] == system
    assert r["primary"]["capability"] == capability


def test_definition_questions_prefer_source_of_truth(cat):
    r = route(cat, "How is net revenue defined?")
    assert r["primary"]["system"] == "dbt"
    assert any("source of truth" in line for line in r["trace"])


def test_dashboard_route_returns_system_specific_ref(cat):
    r = route(cat, "revenue dashboard")
    assert r["primary"]["system"] == "looker"
    refs = {e["id"]: e["ref"] for e in r["primary"]["entities"]}
    # The ref is the Looker one, not the dbt definition.
    assert refs["metric.net_revenue"] == "explore://ecommerce/orders.net_revenue"
    assert "tableau" in {f["system"] for f in r["fallbacks"]}


def test_pii_question_requires_approval_and_excludes_non_pii_systems(cat):
    r = route(cat, "List the emails of customers who churned last month")
    assert r["decision"] == "needs_approval"
    assert r["requires_approval"] is True
    assert r["primary"]["system"] == "bigquery"
    assert all(f["system"] == "bigquery" for f in r["fallbacks"]) or r["fallbacks"] == []
    assert "Get explicit user approval" in r["next_step"]


def test_inflected_words_match(cat):
    r = route(cat, "customers who churned")
    assert "metric.churn_rate" in {e["id"] for e in r["matched_entities"]}


def test_longest_synonym_wins(cat):
    r = route(cat, "how many active customers this year")
    ids = {e["id"] for e in r["matched_entities"]}
    assert "metric.active_customers" in ids
    assert "model.dim_customers" not in ids


def test_unrelated_question_is_no_match(cat):
    r = route(cat, "What is the weather today?")
    assert r["decision"] == "no_match"
    assert r["primary"] is None


def test_intent_only_route_has_low_confidence(cat):
    r = route(cat, "top products")
    assert r["confidence_label"] == "low"


def test_empty_question_rejected(cat):
    with pytest.raises(ValueError):
        route(cat, "   ")


def test_explain_entity_lineage_and_pii(cat):
    info = explain_entity(cat, "active customers")
    assert info["found"]
    up = {e["id"] for e in info["upstream"]}
    assert {"model.dim_customers", "model.fct_orders", "table.raw_customers", "table.raw_orders"} <= up
    assert info["pii_upstream"] == ["raw.customers"]
    down = {e["id"] for e in info["downstream"]}
    assert "dashboard.exec_weekly" in down


def test_explain_entity_suggests_on_miss(cat):
    info = explain_entity(cat, "rev")
    assert info["found"] is False
    assert "Net Revenue" in info["suggestions"]


# -- catalog validation ------------------------------------------------------

BASE = {
    "version": 1,
    "systems": [{"id": "dbt", "capabilities": ["lineage"]}],
    "entities": [],
}


def _with_entities(*ents):
    return {**BASE, "entities": list(ents)}


def test_unknown_system_rejected():
    with pytest.raises(CatalogError, match="unknown system"):
        parse_catalog(_with_entities({"id": "a", "system": "looker"}))


def test_unknown_upstream_rejected():
    with pytest.raises(CatalogError, match="unknown entity"):
        parse_catalog(_with_entities({"id": "a", "system": "dbt", "upstream": ["ghost"]}))


def test_lineage_cycle_rejected():
    with pytest.raises(CatalogError, match="cycle"):
        parse_catalog(
            _with_entities(
                {"id": "a", "system": "dbt", "upstream": ["b"]},
                {"id": "b", "system": "dbt", "upstream": ["a"]},
            )
        )


def test_wrong_version_rejected():
    with pytest.raises(CatalogError, match="version"):
        parse_catalog({**BASE, "version": 2})
