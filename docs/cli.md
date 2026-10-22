# CLI reference

Run the Flight Search Harness through the path-independent Plugin launcher:

```powershell
python <plugin-directory>/scripts/run_flight_harness.py [global-options] <command> [command-options]
```

The launcher imports the installed Harness without changing the caller's current directory. Relative request, Receipt, and `--runs-root` paths therefore resolve against the caller workspace. From a developer checkout, `python -m flight_search_harness` exposes the same parser when run from the repository root.

Global options must appear before the command. Commands write machine-readable JSON to stdout.

## Lifecycle commands

| Command | Purpose | Required options |
| --- | --- | --- |
| `start` | Start a checkpointed Search Mission. | `--request <request.json>` |
| `resume` | Resume an existing Search Mission, optionally submitting its active Lease's Receipt batch. | `--run-id <run-id>` |
| `status` | Read the authoritative Run and a compact active-work summary. | `--run-id <run-id>` |
| `metrics` | Read persisted execution and Worker metrics. | `--run-id <run-id>` |

## Worker selection

Choose one Worker with `--worker`:

| Value | Behavior |
| --- | --- |
| `external` | Default. Returns a compact `worker_turn` for an authorized external browser-capable Worker, or binds and submits `resume --receipt-batch`. |
| `codex-app-server` | Starts `codex app-server --listen stdio://` and executes the Lease through structured Codex turns. |

Examples:

```powershell
python <plugin-directory>/scripts/run_flight_harness.py --worker external start --request <request.json>
python <plugin-directory>/scripts/run_flight_harness.py --worker external resume --run-id <run-id> --receipt-batch <receipt-batch.json>
python <plugin-directory>/scripts/run_flight_harness.py --worker codex-app-server --model <model> --effort <effort> start --request <request.json>
```

The request must use `request_version: "4.0"`, one of `random_loop`, `one_way`, or `round_trip` as `journey_shape`, and the matching public request Schema returned by the selected Skill's resolver.

## Global options

| Option | Meaning |
| --- | --- |
| `--runs-root <directory>` | Store Runs in this directory instead of `runs/`. |
| `--worker <worker>` | Select `external` or `codex-app-server`; default is `external`. |
| `--codex-bin <command>` | Select the Codex executable; default is `codex`. |
| `--model <model>` | Pass a model override to the Codex app-server Worker. |
| `--effort <effort>` | Pass a reasoning-effort override to the Codex app-server Worker. |
| `--worker-timeout-seconds <seconds>` | Set the app-server Worker timeout; default is `900`. |

`--receipt-batch <json>` belongs to `resume` rather than the global options. It is valid only with `--worker external`. Start from the returned `worker_turn.receipt_submission`; the compact JSON requires `receipts`, with only `item_id`, `status`, and `payload` or blocked `reason` for each entry. `metrics` is optional. The External Worker derives the current Lease, Mission revision, claim, Worker instance, Receipt version, and attempt number. Existing full Receipt batches remain accepted when all supplied protocol fields match the current Lease.

## Read a Run

```powershell
python <plugin-directory>/scripts/run_flight_harness.py status --run-id <run-id>
python <plugin-directory>/scripts/run_flight_harness.py metrics --run-id <run-id>
python <plugin-directory>/scripts/run_flight_harness.py deliver --run-id <run-id>
python <plugin-directory>/scripts/run_flight_harness.py deliver --run-id <run-id> --view full
```

`status` returns the current Run manifest under `run` and a non-authoritative `active_work` summary. A completed Outcome exposes `completed_at`, `price_valid_until`, and `price_freshness`; validity is derived from selected Offer Observations or their explicit expiry rather than from Run completion. The active-work summary contains only the active Lease identity, reference, remaining item count, Worker instance, and expiry; it does not create a second lifecycle status. `metrics` reports Provider-query Receipts, Evidence replays, and Receipt retries separately. `deliver` is the only user-delivery Interface: its default `presentation` view returns the compact ranked result when rank one's price Evidence is current. `--view full` returns the complete Product Result for audit. Otherwise it returns `delivery_blocked` without candidate prices.

The CLI exits with code `2` when a lifecycle command returns a terminal `failed` Outcome or when `deliver` returns `delivery_blocked`. `draft`, `blocked`, and `completed` lifecycle Outcomes exit with code `0` and must be distinguished by their JSON `kind`.

## Package commands

Plugin packaging is deliberately separate from the lifecycle CLI:

```powershell
python scripts/package_flight_search_harness.py validate
python scripts/package_flight_search_harness.py install --destination <directory>
python scripts/package_flight_search_harness.py archive --destination <flight-search-harness.zip>
```

Validation checks the explicit file whitelist, public request contracts, byte-identical standalone resolver copies, version mapping, and manifest closure. Install destinations and archive paths must not already exist.
