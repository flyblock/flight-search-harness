# Search Mission runtime

This is the shared operating procedure for the three travel-search Skills. Each Product Kernel keeps ownership of its product inputs, search decisions, Evidence admission, solver, and completion gate. The Product Adapter translates the public request into its Product Kernel's internal input; an agent must not construct internal Session or solver state.

## Load the travel-search context

For a travel search, load this runtime together with:

1. the selected travel Skill;
2. its `assets/request-template.json`;
3. its `references/product-workflow.md`; and
4. the public `request_schema_ref` returned by its resolver only when validation or an uncommon optional constraint requires it.

The compact template covers the normal request path. Product Kernel Schemas, maintainer planning, release documents, and repository design materials remain outside live travel-search context.

The current release is tested on Windows with Codex and Python 3.10 or later. Live research uses the user's authorized Browser/computer-use capability and requires no paid flight-search API subscription.

## Resolve the launcher and workspace

Run the active Skill's `scripts/resolve_harness.py`. `launcher_ref` identifies the matching Harness launcher whether the Skill came from the complete Plugin or a standalone installation. `request_schema_ref` identifies the public request contract for that journey shape.

Keep the shell working directory at the caller workspace. The launcher adds its Plugin root to Python's import path while relative request paths and the default `runs/` Store remain in the caller workspace.

```powershell
python "<launcher-ref>" --help
```

## Materialize the request

Copy the selected Skill's compact template to `runs/requests/<run-id>-request.json`. Replace every `REPLACE_...` value, remove an explicitly optional placeholder field when the user did not supply it, and add only user-requested constraints supported by the public request Schema.

Every request uses `request_version: "2.0"` and exactly one `journey_shape`: `random_loop`, `one_way`, or `round_trip`. Shared user semantics use `base_currency`, `traveler_count`, `baggage_policy`, `overnight_allowed`, `self_transfer_policy`, and `fx_to_base`. One-way and round-trip requests distinguish accepted `anchor_nodes` from optional `seed_gateway_nodes`.

The request carries user intent. The Product Kernel creates hypotheses, Observations, Offers, Evidence, Work Items, and the Outcome during the Run.

## Start live research

Use the External Worker for an authorized browser search:

```powershell
python "<launcher-ref>" `
  --runs-root runs `
  --worker external `
  start `
  --request "runs/requests/<run-id>-request.json" `
  --run-id "<run-id>"
```

Retain the returned `run_id` and use the same Run throughout recovery or context compaction. A normal external handoff is a `blocked` Outcome with `action_required.type: "execute_mission_lease"`. This is a Worker instruction for the active agent, not a user-facing result.

## Execute a Lease

Resolve `lease_ref` beneath `runs/<run-id>/`. Its compact projections normally contain the required execution context. Follow `full_item_ref` only when a projection or retry requires additional detail.

Handle each Work Item by kind:

- `browser_research`: use the authorized browser and capture the exact structured Observation payload requested by the Work Item.
- `semantic_judgment`: produce the requested product-shaped proposal and identify it as a judgment rather than Provider Evidence.
- `choice`: select one published `action_key` and include a concrete reason.
- A retry keeps the original Work Item ID, increments `attempt_number`, and includes the prior admission error. Correct only that Work Attempt; do not repeat committed siblings.

Execute every independent compatible Work Item in the Lease. Work Items with the same `execution.batch_key` may share one Provider interaction; each receives its own item-bound Receipt. Capture current Provider values, URLs, and observation time from the actual interaction.

Write the Receipt batch under the caller workspace, normally `runs/requests/<run-id>-receipts-<revision>.json`:

```json
{
  "lease_id": "lease-000004",
  "mission_revision": 3,
  "claim_id": "REPLACE_WITH_ACTIVE_CLAIM_ID",
  "worker_instance_id": "external:<unique-instance-id>",
  "metrics": {
    "provider_query_count": 1,
    "items": {}
  },
  "receipts": [
    {
      "receipt_version": "2.0",
      "item_id": "work:example",
      "attempt_number": 1,
      "status": "complete",
      "payload": {
        "kind": "observations",
        "product_payload": {}
      }
    }
  ]
}
```

For a `choice`, use `{"action_key": "<published-action-key>", "reason": "<concrete-reason>"}`. For research and retries, follow the Work Item's expected payload shape. The Harness derives Receipt identity from `lease_id + item_id + attempt_number`; replaying that identity with different content is rejected. Keep both `claim_id` and `worker_instance_id` unchanged for the active claim.

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
```

## Interpret the Outcome

- `completed`: present the finalized summary, follow `result_ref` when the full result is useful, and disclose investigated and unsearched scope.
- `draft`: resume while useful work remains and the active request authorizes it; present progress only when the current execution must stop.
- `blocked`: execute it directly when `action_required` belongs to the Worker; present it only when the required action belongs to the user or another unavailable external actor.
- `failed`: the Run is terminal. Report the structured error; do not resume that Run.

Only the selected Product Kernel may produce `completed`. Never fabricate Evidence, book or pay for travel, or present a candidate as final before that Outcome.
