# catalog-router

**An MCP server that decides which BI system should answer a data question, and runs as a locked-down Docker container.**

Most teams that connect AI agents to their data stack end up with several MCP servers: dbt, Looker, Tableau, Superset, BigQuery. The agent then has to guess which one to call. It asks Tableau for a metric definition. It goes to the raw warehouse when a governed Looker explore already exists. It pulls customer emails without anyone noticing.

`catalog-router` removes the guessing. You describe your BI stack once, in a YAML **semantic catalog graph**. The graph lists your systems, the business entities each one owns, the lineage between those entities, and which entities carry PII. The router then gives the agent a routing decision it can explain:

```
"How is net revenue calculated?"          -> dbt       (metric_definition)  source of truth
"Where can I see the exec weekly dashboard?" -> looker  (dashboard)          ref dashboard://42
"What breaks if I change fct_orders?"     -> dbt       (lineage)
"List emails of customers who churned"    -> bigquery  NEEDS APPROVAL (PII: Customer Email)
```

It never queries data itself. It tells the agent **where** to go, **which reference** to use in that system, and **why**.

## Quick start

### One-line install (Claude Desktop, any MCP client)

```sh
curl -fsSL https://raw.githubusercontent.com/sangaraju1988/catalog-router/main/install.sh | sh -s -- --configure-claude
```

The installer pulls the image and writes an example catalog to `~/.catalog-router/catalog.yaml`. It then registers the server in Claude Desktop, after making a timestamped backup of your config. Restart Claude Desktop and ask *"Which system should answer: how is net revenue calculated?"*

### Run it yourself

```sh
# stdio (for MCP clients)
docker run -i --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  -v ~/.catalog-router/catalog.yaml:/catalog/catalog.yaml:ro \
  catalog-router:0.1.2

# HTTP (long-running, for gateways and shared use)
docker compose up -d --build
curl localhost:8080/healthz         # {"status":"ok",...}
# MCP endpoint: http://localhost:8080/mcp
```

### Build from source

```sh
git clone https://github.com/sangaraju1988/catalog-router && cd catalog-router
docker build -t catalog-router:0.1.2 .
./install.sh            # falls back to the local build when the image can't be pulled
```

## Tools

| Tool | What it does |
|---|---|
| `route_question(question, top_k=3)` | Returns the primary system, ranked fallbacks, matched entities with the system-specific ref, a confidence score, PII/approval flags, and a step-by-step `trace`. |
| `explain_entity(name)` | Shows where a metric, dashboard, model, table or field is defined and exposed, its full upstream and downstream lineage, and any PII upstream. |
| `list_systems()` | Lists the connected BI systems with their capabilities and governance status. |
| `reload_catalog()` | Re-reads the mounted YAML. If the new file is invalid, the previous catalog stays active. |

Example `route_question` result (trimmed):

```json
{
  "decision": "route",
  "primary": {
    "system": "looker", "mcp_server": "looker", "capability": "dashboard",
    "entities": [{"id": "dashboard.exec_weekly", "ref": "dashboard://42"}]
  },
  "fallbacks": [{"system": "tableau"}, {"system": "superset"}],
  "confidence": 0.67, "confidence_label": "medium",
  "requires_approval": false,
  "trace": [
    "Matched entities: Executive Weekly (dashboard.exec_weekly) via 'exec weekly'",
    "Detected intents: dashboard=3",
    "Scores: looker=6, tableau=3, superset=3",
    "Chose looker: +3 supports dashboard; +3 home of Executive Weekly"
  ],
  "next_step": "Call a tool on the 'looker' MCP server for dashboard using dashboard://42."
}
```

## How routing works

The router is deterministic and fully traceable. There are no LLM calls, no embeddings and no API keys. Every decision can be reproduced and unit-tested.

1. **Entity matching.** It finds catalog entities mentioned in the question. The longest synonym wins and matches don't overlap, so "active customers" beats "customers". Light inflection is handled: "churned" matches "churn".
2. **Intent detection.** Trigger phrases score capabilities such as `metric_definition`, `lineage`, `dashboard`, `governed_metric`, `ad_hoc_sql` and `raw_data`.
3. **Scoring.** Each system earns points for:
   - each capability it supports;
   - being the *home* of a matched entity;
   - *exposing* a matched entity.

   For definition and lineage questions, the owning system also gets a **source-of-truth** bonus. This is because the place a metric is *defined* is a better answer than every place it is *displayed*.
4. **Governance.**
   - If the question touches a PII entity, only systems with `pii_allowed: true` stay eligible, and the decision becomes `needs_approval`.
   - On a near-tie, a governed semantic layer beats an ungoverned system.

**Confidence** measures how far the winner is ahead of the runner-up. 0.5 is a coin flip, 1.0 means there was no competitor. Two semantic layers that both expose a metric will score close together, and either is a valid answer, so try the fallbacks in order.

## Writing your catalog

See [`catalog/example-catalog.yaml`](catalog/example-catalog.yaml), which is a fully commented fictional e-commerce stack. The catalog has four sections:

- `systems`: one per downstream MCP server, with its `capabilities`, `governed` and `pii_allowed`.
- `intents`: the phrases that signal each capability, with a weight.
- `entities`: metrics, dashboards, models, tables and fields. Each has `synonyms`, a home `system`, a `ref`, optional `also_in` exposures, `upstream` lineage, and `pii`.
- `governance`: the governed-preference margin and the PII approval rule.

The server reads the catalog from `CATALOG_PATH`. If that file doesn't exist, for example when the server is enabled in Docker's MCP Toolkit without choosing a catalog, it starts with the bundled example instead. `list_systems` then reports `"fell_back_to_example": true`, so the agent can tell you it's routing against demo data.

Validate a catalog without starting the server:

```sh
docker run --rm --network none -v ./my-catalog.yaml:/catalog/catalog.yaml:ro catalog-router:0.1.2 --check
```

Validation rejects:
- unknown systems;
- dangling lineage edges;
- duplicate ids;
- **lineage cycles**, which it reports with the cycle path.

## Security posture

The router only needs to read one YAML file and talk over stdio, so the default launch removes almost everything else:

| Control | Why |
|---|---|
| `--network none` (stdio mode) | The router makes no outbound calls. A compromised dependency can't exfiltrate anything. |
| `--read-only` + catalog mounted `:ro` | The container can't modify itself or the catalog. |
| `--cap-drop ALL`, `no-new-privileges` | It has no Linux capabilities and can't escalate. |
| non-root UID 10001 | The image runs as a non-root user. |
| Compose binds `127.0.0.1` only | HTTP mode isn't exposed on your LAN by default. |
| Multi-stage image, no compiler | The runtime image contains only the installed wheels. |

## Roadmap

- [ ] Publish multi-arch image to GHCR (CI is ready; it runs on tag `v0.1.2`)
- [ ] Register in a Docker MCP Gateway custom catalog, alongside the dbt, Looker, Tableau, Superset and BigQuery servers
- [ ] `catalog-router import`: generate the catalog from a dbt `manifest.json` and LookML
- [ ] Optional embedding-based synonym matching, with the deterministic path kept as the default
- [ ] Docker Hardened Images base

## Development

```sh
pip install -e ".[dev]"
pytest -q
catalog-router --catalog catalog/example-catalog.yaml --transport http --port 8080
```

## License

Apache-2.0
