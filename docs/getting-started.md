# Getting started

Flight Search Harness turns a natural-language travel request into a durable, resumable Search Mission. Its three Skills cover random loops, one-way journeys, and round trips. One-way and round-trip requests name a destination, while random-loop requests leave the destination sequence open.

## Requirements

- Windows 10 or later
- PowerShell
- Python 3.10 or later
- Codex desktop with an authorized Browser/computer-use capability for live Provider research

Live searches use public travel pages through the authorized browser and require no paid flight-search API subscription.

The installed Harness uses only the Python standard library. Its launcher resolves the Plugin internally while preserving the caller's current directory, so the default `runs/` Store belongs to the user's workspace rather than the Plugin installation.

Each search Skill contains a small resolver. It uses the bundled Harness in a complete Plugin installation or obtains the matching Harness version on first use when the Skill was installed by itself.

## Use a local checkout

```powershell
Set-Location <flight-search-harness-directory>
python --version
python scripts/run_flight_harness.py --help
python scripts/package_flight_search_harness.py validate
```

Open the same directory as a Codex project and describe the journey rather than choosing scripts yourself. The selected search Skill resolves the local Harness and confirms the launcher before starting. For example:

```text
Find a low-cost flight loop using these airport pools, date window, leg bounds, party size, baggage policy, and Base Currency.
```

```text
Find the lowest-cost one-way journey from my origin region to my destination region within this date window.
```

```text
Compare joint, split, and surface-supported round trips between these two regions.
```

The matching Skill starts from its compact request template, collects the missing hard constraints, and starts exactly one Product Kernel through the Harness. Its resolver returns the matching public request Schema for uncommon optional constraints or request-validation failures; Product Kernel Schemas remain internal.

## Use a packaged ZIP

Extract the release archive without flattening its directory structure:

```powershell
Expand-Archive <flight-search-harness.zip> -DestinationPath <install-directory>
Set-Location <extracted-plugin-directory>
python scripts/package_flight_search_harness.py validate
python scripts/run_flight_harness.py --help
```

The ZIP is the complete Plugin distribution. Keep its directory structure intact so all three search Skills can use the bundled Harness.

Install the extracted Plugin folder through Codex's local Plugin workflow and start a new chat. The three search Skills are then available from any workspace. If a search is interrupted, ask Codex to resume its existing `run_id` instead of starting a replacement Run.

## Install one Skill

Install only the journey shape you need:

```powershell
npx skills add flyblock/flight-search-harness --skill low-cost-round-trip-flight-search
```

Use `low-cost-one-way-travel-flight-search` or `low-cost-random-loop-flight-search` for the other journey shapes. Once the matching GitHub Release is public, the Skill resolver obtains its verified-version Harness archive from the Release asset and returns the launcher, shared runtime, and public request Schema references to Codex. Later uses reuse that runtime. A private repository or draft Release requires installing the complete local Plugin instead because anonymous standalone download is unavailable.

## Start and resume from the CLI

Normal users can let Codex and the selected Skill manage these commands. The direct CLI is useful for inspecting or integrating the lifecycle.

Prepare a request using `request_version: "4.0"` and the `request_schema_ref` returned by the selected Skill resolver. From the workspace that should own the Run, invoke the installed launcher's path and start live research with the External Worker:

The random-loop Product Adapter reads optional installation-level airport data from `FLIGHT_SEARCH_HARNESS_AIRPORT_CATALOG` and `FLIGHT_SEARCH_HARNESS_AIRPORT_TRANSIT_CATALOG`; these file references never belong in a travel request. The Harness obtains ECB reference rates for internal comparison when possible. If rate acquisition fails, only Offers already quoted in the Base Currency are comparable.

```powershell
python <plugin-directory>/scripts/run_flight_harness.py --worker external start --request <request.json>
```

When live research is required, the call returns a JSON `blocked` Outcome containing the `run_id` and active Lease reference. This Worker-directed blocker is an execution handoff, not a result to present to the user. A browser-capable Worker executes the Work Items and writes a Receipt batch. Submit that batch to the same Run:

```powershell
python <plugin-directory>/scripts/run_flight_harness.py --worker external resume --run-id <run-id> --receipt-batch <receipt-batch.json>
```

Repeat the resume command when the Harness publishes another Lease. Inspect progress without changing it:

```powershell
python <plugin-directory>/scripts/run_flight_harness.py status --run-id <run-id>
python <plugin-directory>/scripts/run_flight_harness.py metrics --run-id <run-id>
```

Every Run is stored under `runs/<run-id>/`. A Run may return `draft`, `blocked`, `failed`, or `completed`; only `completed` has passed the selected Product Kernel's finalization rules.

See [CLI reference](cli.md) for every command and [architecture](architecture.md) for the lifecycle and Module boundaries.
