# Flight Search Harness

Flight Search Harness gives Codex a recoverable, evidence-backed way to research low-cost travel. It routes each request to one independent Product Kernel, preserves the Run across interruptions, and returns a structured Outcome.

The project is tested on Windows with Codex desktop and Python 3.10 or later.
The current Plugin release is `2.0.0` and uses public request protocol `4.0`
with the protocol-3 Run, Lease, Work Item, and Receipt lifecycle.

Live searches operate user-visible Provider pages through the agent host's authorized Browser or equivalent computer-use capability. The workflow is portable across browser-capable agent hosts.

## What it does

| Search type | Use it for | Result |
| --- | --- | --- |
| Random loop | Flexible destinations and dates for an airport-only multi-leg loop | A verified loop, or a bounded no-result Outcome after the required search scope is exhausted |
| One-way | A selected destination with flexible regional Gateways | The lowest verified Mandatory Air Ticket Total within a disclosed Self-access Gateway Pool and Research Credit budget |
| Round trip | A selected destination with an outbound and return journey | The lowest verified Mandatory Air Ticket Total within a disclosed Self-access Gateway Pool and Research Credit budget, plus naturally discovered alternatives |

The Harness researches and validates travel options. It does not book or pay for travel.

## Quick start

Install the complete Plugin from this local source snapshot. Standalone Skills use
the bundled local Harness runtime and do not resolve or download an upstream
repository. Then describe the trip in natural language. Codex selects the
matching search Skill and resumes the same Run if the search is interrupted.

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

> Find the lowest Mandatory Air Ticket Total for a one-way trip using my origin and destination regions, date window, party size, baggage policy, and transfer constraints. Use standard Search Effort and disclose the Self-access Gateway Pool and excluded non-air costs.

Round trip:

> Find the lowest Mandatory Air Ticket Total for a round trip using my origin and destination regions, date windows, party size, baggage policy, and transfer constraints. Use standard Search Effort and disclose the Self-access Gateway Pool and excluded non-air costs.

## How it works

```text
Natural-language request
  -> journey-shape routing
  -> recoverable Search Mission
  -> browser research by a Worker
  -> product-owned Evidence and solving
  -> completed, draft, blocked, or failed Outcome
```

Skills are the natural-language workflow entry points. The host's preferred authorized live-research capability performs Provider research, while the Harness owns execution, recovery, and persistence. Each Product Kernel independently owns its search state, Evidence admission, solver, and completion criteria.

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
