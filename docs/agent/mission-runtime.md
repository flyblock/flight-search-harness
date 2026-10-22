# Search Mission runtime

This is the shared operating procedure for the three travel-search Skills. Each Product Kernel keeps ownership of its product inputs, search decisions, Evidence admission, solver, and completion gate. The Product Adapter translates the public request into its Product Kernel's internal input; an agent must not construct internal Session or solver state.

## Load the travel-search context

For a travel search, load this runtime together with:

1. the selected travel Skill;
2. its `assets/request-template.json`;
3. its `references/product-workflow.md`; and
4. the public `request_schema_ref` returned by its resolver only when validation or an uncommon optional constraint requires it.

The compact template covers the normal request path. Product Kernel Schemas, maintainer planning, release documents, and repository design materials remain outside live travel-search context.

The current release is tested on Windows with Codex and Python 3.10 or later. The workflow is host-capability based rather than Codex-specific: other agents may use an equivalent authorized Browser.

## Choose the live research capability

For every `browser_research` Work Item, use an authorized capability that operates a user-visible Provider page and can satisfy the complete published query and Evidence contract:

1. Prefer the host's built-in interactive browser. Reuse its authorized session and visible Provider state when available.
2. Otherwise use an equivalent host-provided browser automation capability with current page access and exact URL, time, price, and itinerary capture.
3. Otherwise use native computer-use to operate a browser when it can preserve exact page identity and URL evidence.
Capability names are host-specific. Do not require Codex, `computer-use`, or any particular browser implementation in a portable Skill. If none of the available Browser capabilities can execute the Work Item, return a blocked Receipt with the concrete capability gap. Never convert missing browser capability, authentication, or Provider support into `no_result` market Evidence.

## Resolve the launcher and workspace

Run the active Skill's `scripts/resolve_harness.py`. `launcher_ref` identifies the matching Harness launcher whether the Skill came from the complete Plugin or a standalone installation. `request_schema_ref` identifies the public request contract for that journey shape.

Keep the shell working directory at the caller workspace. The launcher adds its Plugin root to Python's import path while relative request paths and the default `runs/` Store remain in the caller workspace.

```powershell
python "<launcher-ref>" --help
```

## Materialize the request

Copy the selected Skill's compact template to `runs/requests/<run-id>-request.json`. Replace every `REPLACE_...` value, remove an explicitly optional placeholder field when the user did not supply it, and add only user-requested constraints supported by the public request Schema.

Every request uses `request_version: "4.0"` and exactly one `journey_shape`: `random_loop`, `one_way`, or `round_trip`. Shared user semantics use `base_currency`, `adult_count`, `baggage_policy`, `overnight_allowed`, and `self_transfer_policy`; One-way and Round-trip also accept `search_effort`. The current Products support adults only; explicit child or infant travel is unsupported. `baggage_policy.cabin_baggage` is one of `none_required`, `personal_item`, `cabin_bag`, or `personal_item_and_cabin_bag`; `none_required` means no minimum cabin-baggage entitlement, not that included baggage is forbidden. One-way and random-loop requests currently require `none_required`; round-trip requests can use all four values. The Harness obtains ECB reference rates for internal comparison when possible; failure leaves only Base-Currency Offers comparable and never causes an invented rate. Regional One-way and Round-trip requests distinguish accepted `anchor_nodes` from optional `seed_gateway_nodes` and default to user-arranged Endpoint Access/Egress through `any_admitted_gateway`.

The request carries user intent. The Product Kernel creates hypotheses, Observations, Offers, Evidence, Work Items, and the Outcome during the Run.

## Start live research

Treat every new user search as a clean room. Create a new Run in the current
task's Run root and do not inspect, select, or resume another chat's Run merely
because its request looks similar. Resume an existing Run only when the user
explicitly asks to continue that exact Run or the current task itself created
it earlier.

Use the External Worker for an authorized browser search:

```powershell
python "<launcher-ref>" `
  --runs-root runs `
  --worker external `
  start `
  --request "runs/requests/<run-id>-request.json" `
  --run-id "<run-id>"
```

Retain the returned `run_id` and use the same Run throughout recovery or context compaction. A normal External Worker CLI handoff is a `blocked` Outcome with `action_required.type: "execute_worker_turn"` and an inline `worker_turn`. This is a Worker instruction for the active agent, not a user-facing result.

## Execute a Lease

The External Worker CLI response includes a non-persisted `worker_turn`. Use its
`items` and prefilled `receipt_submission` as the normal execution Interface.
Lease fencing, Mission revision, claim identity, Receipt version, attempt
identity, receive time, and immutable Receipt identity are Harness-derived and
do not belong in the Worker submission. Read the persisted Lease or complete
Work Item only when a targeted correction cannot be resolved from the compact
turn.

Handle each Work Item by kind:

- `browser_research`: apply the capability order above and capture the exact structured Observation payload requested by the Work Item.
- `semantic_judgment`: produce the requested product-shaped proposal and identify it as a judgment rather than Provider Evidence.
- `choice`: select one published `action_key` and include a concrete reason.
- A retry keeps the original Work Item ID, increments `attempt_number`, and includes the prior admission error. Correct only that Work Attempt; do not repeat committed siblings.

Execute every independent compatible Work Item in the Lease. Work Items with the same `execution.batch_key` may share one Provider interaction; each receives its own item-bound Receipt. Capture current Provider values, URLs, and observation time from the actual interaction.

For a lowest-price objective, establish the lowest compatible result in the Browser through the Provider's lowest-price view, ascending price sort, or comparison of all visible results. Record `price_selection_method`; default recommended ordering and an unopened lower-price label are not lowest-price Evidence.

Copy the returned `worker_turn.receipt_submission` to a compact Receipt file,
normally `runs/requests/<run-id>-receipts-<revision>.json`, and replace its
placeholders with observed facts. Supply only each Work Item's identity, status,
payload or blocked reason, plus optional metrics. The External Worker binds all
protocol and fencing fields when it resumes the Run:

```json
{
  "metrics": {
    "provider_query_count": 1,
    "items": {}
  },
  "receipts": [
    {
      "item_id": "work:example",
      "status": "complete",
      "payload": {
        "kind": "observations",
        "product_payload": {}
      }
    }
  ]
}
```

For a `choice`, use `{"action_key": "<published-action-key>", "reason": "<concrete-reason>"}`. For research and retries, start from the compact Receipt template. Load the complete `attempt_schema` only to resolve a validation failure; the Harness remains the admission authority. Omit `execution_origin` for ordinary current research and non-Provider work—the Harness derives the normal value. Set it explicitly to `evidence_replay` only when no new Provider interaction occurred. A blocked Receipt omits `payload` and includes a non-empty `reason`. The Harness rejects unknown or duplicate Work Item IDs and any caller-supplied protocol field that conflicts with the current Lease. Existing full Receipt batches remain accepted for compatibility.

Checkpoint metrics keep `provider_query_count`, `evidence_replay_count`, and `receipt_retry_count` separate. When any Receipt is an `evidence_replay`, the Harness rebuilds the checkpoint's Provider count from current per-item metrics so copied historical totals cannot become new Provider queries.

## Checkpoint and continue

Submit the Receipt batch and resume the same Run:

```powershell
python "<launcher-ref>" `
  --runs-root runs `
  --worker external `
  resume `
  --run-id "<run-id>" `
  --receipt-batch "runs/requests/<run-id>-receipts-<revision>.json"
```

Continue through Leases and Receipts while the active request still authorizes progress. Accepted sibling Receipts remain committed when another item needs a new attempt. Respect Lease ownership, `claim_id`, and revision; an expired or replaced Worker claim cannot submit stale work.

Inspect progress without advancing it:

```powershell
python "<launcher-ref>" --runs-root runs status --run-id "<run-id>"
python "<launcher-ref>" --runs-root runs metrics --run-id "<run-id>"
python "<launcher-ref>" --runs-root runs deliver --run-id "<run-id>"
python "<launcher-ref>" --runs-root runs deliver --run-id "<run-id>" --view full
```

## Interpret the Outcome

- `completed`: present the finalized summary, follow `result_ref` when the full result is useful, disclose investigated and unsearched scope, and describe the price as current only while `price_freshness` is `current`. Freshness is anchored to the oldest selected Offer Observation or its explicit expiry, never to Run completion time.
- `draft`: resume while useful work remains and the active request authorizes it; present progress only when the current execution must stop.
- `blocked`: execute it directly when `action_required` belongs to the Worker; present it only when the required action belongs to the user or another unavailable external actor.
- `failed`: the Run is terminal. Report the structured error; do not resume that Run.

Only the selected Product Kernel may produce `completed`. After completion, use
`deliver` as the sole user Result Interface. Its default `presentation` view is
a compact projection with ranked Journey Alternatives, purchase groups,
Movements, current prices, booking-link roles, and derived burden warnings. Use
`--view full` only for audit or debugging. Present only `kind=deliverable`; never
derive a user answer from a private Product Session, raw Solver state, truncated
output, or browser memory. `delivery_blocked` contains no candidate prices and
requires the stated recovery action. Never fabricate Evidence, book or pay for
travel, or present a candidate as final before that Outcome.
