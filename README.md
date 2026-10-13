# Flight Search Harness

Flight Search Harness gives Codex a recoverable, evidence-backed way to research low-cost travel. It routes each request to one independent Product Kernel, preserves the Run across interruptions, and returns a structured Outcome.

The project is tested on Windows with Codex desktop and Python 3.10 or later.
The current Plugin release is `1.3.0` and uses public request protocol `2.0`
with the protocol-3 Run, Lease, Work Item, and Receipt lifecycle.

Live searches use the user's authorized Browser/computer-use capability to research public travel pages. No paid flight-search API subscription is required.

## What it does

| Search type | Use it for | Result |
| --- | --- | --- |
| Random loop | Flexible destinations and dates for an airport-only multi-leg loop | A verified loop, or a bounded no-result Outcome after the required search scope is exhausted |
| One-way | A selected destination with flexible regional gateways and optional surface transport | A complete origin-to-destination journey assembled from admitted Evidence |
| Round trip | A selected destination with an outbound and return journey | A comparison of complete ticket portfolios, including joint, split, open-jaw, nested, and surface-supported options |

The Harness researches and validates travel options. It does not book or pay for travel.

## Quick start

Install the complete Plugin, or install one search Skill directly:

```powershell
npx skills add flyblock/flight-search-harness --skill low-cost-round-trip-flight-search
```

The same command supports `low-cost-one-way-travel-flight-search` and `low-cost-random-loop-flight-search`. After the corresponding v1.3.0 GitHub Release is public, a standalone Skill obtains its matching Harness runtime from that Release asset on first use; a Skill installed through the complete Plugin uses the bundled runtime. While the repository and Release remain private, use the complete local Plugin installation. Then describe the trip in natural language. Codex selects the matching search Skill and resumes the same Run if the search is interrupted.

The complete Plugin contains the Harness runtime, all three Product Kernel modules, and all three search Skills. Extract it without changing its directory structure and install the extracted folder through Codex's local Plugin workflow.

From a developer checkout, confirm the local runtime and package boundary in PowerShell:

```powershell
python --version
python scripts/run_flight_harness.py --help
python scripts/package_flight_search_harness.py validate
```

See [Getting started](docs/getting-started.md) for the first live search and [CLI reference](docs/cli.md) for direct commands.

## Example requests

Random loop:

> Find a low-cost airport loop using the departure pool, date window, leg bounds, baggage policy, and Base Currency I provide. Prioritize verified distance per Base Currency unit.

One-way:

> Find the lowest-cost complete one-way journey using my origin, destination, date window, party size, baggage policy, and transfer constraints. Include eligible surface access when I allow it.

Round trip:

> Compare complete round trips using my origin, destination, outbound and return windows, party size, baggage policy, and transfer constraints. Check joint, split, and open-jaw options.

## How it works

```text
Natural-language request
  -> journey-shape routing
  -> recoverable Search Mission
  -> browser research by a Worker
  -> product-owned Evidence and solving
  -> completed, draft, blocked, or failed Outcome
```

Skills are the natural-language workflow entry points. The authorized Browser/computer-use capability performs live Provider research, while the Harness owns execution, recovery, and persistence. Each Product Kernel independently owns its search state, Evidence admission, solver, and completion criteria.

See [How Flight Search Harness works](docs/how-it-works.md) for the three Product Kernel strategies, low-cost objectives, lifecycle diagram, and research foundations.

## Outcomes and resume

| Outcome | Meaning |
| --- | --- |
| `completed` | The selected Product Kernel passed its finalization criteria and produced the authoritative result. |
| `draft` | Useful work exists, but the search is unfinished and can be resumed. |
| `blocked` | Progress requires a specific external action or user decision. |
| `failed` | The Run is terminal because the active request has no recoverable next action. |

Runs are stored under `runs/`. To continue an interrupted search, resume its existing `run_id`; starting a replacement Run discards the recovery advantage.

## Project layout

```text
plugin.json        Plugin identity and distribution metadata
skills/            Three user-facing search Skills
flight_search_harness/    Shared lifecycle, persistence, Worker, and CLI runtime
product/           Three independent Product Kernel implementations
docs/              Getting-started, architecture, CLI, and development guides
scripts/           Path-independent launcher plus packaging utilities
```

## Documentation

- [Getting started](docs/getting-started.md)
- [How it works](docs/how-it-works.md)
- [Architecture](docs/architecture.md)
- [CLI reference](docs/cli.md)
- [Naming conventions](docs/naming.md)
- [Domain glossary](GLOSSARY.md)

## Development

Validate the runtime whitelist and confirm the launcher starts:

```powershell
python -m unittest discover -s tests -q
python scripts/package_flight_search_harness.py validate
python scripts/run_flight_harness.py --help
```

## License

Flight Search Harness is licensed under the [Apache License 2.0](LICENSE).

## Acknowledgments

Thanks to OpenAI Codex for assistance with the design, implementation, and verification of Flight Search Harness.
