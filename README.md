# Flight Search Harness

A small, client-independent Python runtime for AI-directed flight research. An agent chooses dates, airports, providers and combinations; the runtime preserves findings, avoids accidental repeat queries, compares known options and produces ranked results.

It works with any AI client or a person using the JSON interface. Codex, MCP, a specific browser, a particular booking provider and hosted services are not required. Use the caller's available browser, HTTP or API tools to collect observations. The runtime does not control those tools.

## Start

Use Python 3.10 or later. Install the standard IANA timezone data package for consistent cross-platform `zoneinfo` support, then run from this directory:

```text
python -m pip install -r requirements.txt
python -B -m flight_search_harness --help
python -B -m flight_search_harness --runs-root <external-runs-directory> start --request <request.json>
```

From another working directory, use the portable launcher:

```text
python -B <plugin-directory>/scripts/run_flight_harness.py --runs-root <external-runs-directory> status --run-id <run-id>
```

Without timezone data, source records can still be saved; checks that need an unavailable location timezone remain unknown rather than passing by assumption. No client SDK is required. User limits on client tool calls remain the caller's responsibility; the harness's `max_queries` counts recorded logical query attempts, not every browser or API interaction.

The optional `--runs-root` selects the data location independently of the caller's client or platform. By default the runtime resolves the configurable external `runtime_data_root` in [the suite manifest](FLIGHT-SEARCH-HARNESS-MANIFEST.json) and uses its `runs` child. Keep generated Requests, captures and results outside the source directory.

## One interface

All clients use the same JSON Request, query, observation and result structures. Commands read UTF-8 JSON files and return compact JSON on stdout (`--verbose` or `deliver --view full` for complete records); diagnostics go to stderr. There is no separate vendor or provider adapter contract.

```text
start → external discovery → challenge current best → record → compare and continue → deliver
```

`record` can save the actual `query`, its observations and outcome together. It also accepts a previously registered query ID or an unlinked incidental discovery. Use [the complete quote example](examples/record-quote.json) and [the incomplete price lead example](examples/record-signal.json) as the public submission reference; replace every placeholder with observed facts. `revise` reevaluates saved findings under changed requirements. `combine` records an explicit combination of saved purchases. `status`, `stop`, and `resume` preserve continuity without claiming exhaustive search. Sources do not need to appear in a whitelist. Missing or unusable links and uncertain capture times are metadata notices, not reasons to discard useful flight facts; raw inputs are retained. Screenshots and original-page attachments are optional.

A challenge query names its purpose and reason; the runtime records the baseline and uses the same ledger to compare the result. `research_feedback` makes open discovery and challenge opportunities visible. The prior best remains a ranked alternative when beaten. For flexible city/region endpoints, the agent discovers realistically reachable airports from external information rather than relying only on memory or administrative borders. Explicit user restrictions remain binding.

Use [the challenge query example](examples/query-challenge.json) and [the airport-discovery record](examples/record-airport-discovery.json). `python -B <plugin-directory>/scripts/run_flight_harness.py clock` returns a UTC tool time without starting a Run; sample it while observing the source, not later while saving. Flight schedules retain local times and offsets.

See [the workflow and JSON interface](docs/search-workflow.md) and [the Request Schema](flight_search_harness/schemas/search-request.schema.json). Three optional [Skills](skills) supply guidance for one-way travel, round trips and random loops; they are not runtime dependencies. Their `agents/openai.yaml` files are optional client display metadata.

The default comparison is mandatory flight-ticket cost. Full door-to-door cost, budgets, time restrictions and output preferences follow the user's actual request. Observed facts, AI judgments, missing data and eligibility checks remain distinguishable. Delivery preserves the source verification stage, connection status and unverified endpoint connections; an eligible ticket does not certify a complete journey. Delivery normally includes the best and useful complete alternatives already found, without additional searches merely to fill a top-three list. Results describe saved sources and queries, not a guaranteed global optimum.

Repository contributors should also read [AGENTS.md](AGENTS.md), [domain definitions](GLOSSARY.md) and [naming conventions](docs/naming.md). These maintained source instructions do not override the user's search requirements.
