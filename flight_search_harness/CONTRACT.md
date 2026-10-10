# Flight Search Harness contract

This document defines protocol 3 for the Flight Search Harness. The JSON
Schemas in [`schemas/`](schemas/) are the machine-readable authority. Each
Product Kernel continues to own its Product Session, Evidence, solver, and
finalization rules.

## Runtime model

A Search Mission is the durable, resumable lifecycle of one request. Its Run
is the sole persisted lifecycle authority: only `run.json` owns the current
`initializing`, `running`, `draft`, `blocked`, `failed`, or `completed` state
and the current Outcome.

`mission/mission.json` is not a second lifecycle record. It is a small
execution cursor containing only:

- `run_id`;
- the current Mission `revision` and next Lease sequence;
- `active_lease_ref`;
- minimal Work Item and admission indexes needed for replay.

The three Product Kernels do not share Product Session state, Evidence,
hypotheses, solvers, or finalization state.

## Public Python Interface

```python
outcome = harness.run({"kind": "start", "request": request}, worker)
outcome = harness.run({"kind": "resume", "run_id": run_id}, worker)
```

The caller may supply `run_id` on `start`; otherwise the Store creates one.
`resume` selects the Product Kernel recorded by the Run. Resuming a terminal
`completed` or `failed` Run is read-only and returns its persisted Outcome.

Protocol 3 is a breaking cutover. An active Run with a legacy Mission or Run
protocol is rejected with an explicit instruction to start a new Run. The
Harness does not mutate or migrate the legacy artifacts in place.

## Outcome

Every public call returns an Outcome with `outcome_version`, `kind`, `run_id`,
and `product_id`:

- `draft`: useful but unfinished work exists and the Run may be resumed;
- `blocked`: progress requires an external action or user decision and the Run
  may be resumed;
- `failed`: the active request has no recoverable next action; it is terminal;
- `completed`: the selected Product Kernel's finalization criteria passed.

Only a Product Kernel `complete` effect can produce `completed`. A Worker,
browser observation, Offer, or model statement cannot bypass finalization.

A completed Outcome contains a small result summary and the authoritative
Product result reference:

- `result_ref` is always `product/final.json`, the sole authoritative full
  result;
- `result_summary_ref`, when the optional summary sidecar was written, is
  `result/summary.json`. Its absence never changes `completed`; the same
  summary is regenerated from `product/final.json` when needed.

## Product Kernel Seam

```python
transition = kernel.start(request, product_directory)
transition = kernel.advance(product_directory, receipt_batch_or_none)
```

A transition contains per-Receipt admission decisions and one effect:
`need_work`, `draft`, `blocked`, or `complete`. The Harness does not inspect
Product Session fields or branch on product identity to interpret search state.

## Worker Seam

```python
worker_exit = worker.run(lease, checkpoint, context)
```

Every Worker instance has a unique `worker_instance_id`. Worker execution runs
outside the Run lock. The Worker researches bounded Work Attempts and submits a
Receipt batch through `checkpoint`; it never mutates Product Session state or
admits Evidence.

The complete Work Item is immutable under `mission/items/`. A Lease contains a
compact projection plus `full_item_ref`, `full_item_hash`, and
`attempt_number`. Protocol 3 has no Work Item dependency graph and no
correction Work Item kind. A rejected Receipt causes the same Work Item to be
published as the next attempt with an incremented number and `last_error`.

## Lease and Receipt transaction

The Lease file is the only claim authority. It contains `lease_id`, `run_id`,
Mission revision, pending Work Attempts, and either a null claim or:

```json
{
  "claim_id": "opaque fence",
  "worker_instance_id": "unique execution instance",
  "expires_at": "date-time"
}
```

There is no duplicated Lease status, heartbeat timestamp, attempt counter,
checkpoint policy, or takeover history. Takeover information may be recorded
as a non-authoritative event.

A Receipt batch carries the Lease fencing facts once: `lease_id`,
`mission_revision`, `claim_id`, and `worker_instance_id`. Each Receipt contains
only `item_id`, `attempt_number`, `complete|blocked`, and its payload or reason.
The Harness derives `receipt_id` deterministically from the Lease and Work
Attempt; a Worker does not supply an arbitrary idempotency key or a duplicate
revision.

The admission order is:

1. validate Lease revision, claim, Work Item, and attempt;
2. atomically persist immutable raw Receipts;
3. ask the Product Kernel to admit them idempotently;
4. persist immutable admission-decision sidecars;
5. update the pending Work Attempts once and close an empty Lease;
6. materialize the Product Kernel's next effect;
7. write optional metrics, cache, and event sidecars without changing the
   admission or Outcome.

If execution stops after raw Receipt persistence or Product admission, replay
uses the same derived Receipt identity. A replay with different content is
rejected. Product admission remains idempotent, so Evidence is not duplicated.
Admission sidecars replace committed markers and do not claim a false Product
Session revision.

## Store layout

```text
<store-root>/<run-id>/
|-- run.json                         lifecycle and Outcome authority
|-- events.jsonl                     optional observability
|-- mission/
|   |-- mission.json                 execution cursor
|   |-- pending-effect.json          uncommitted Product effect cursor
|   |-- items/                       immutable full Work Items
|   |-- leases/                      Lease and claim authority
|   |-- receipts/
|   |   |-- raw/                     immutable Worker submissions
|   |   `-- admissions/              immutable Product Kernel decisions
|   `-- metrics/checkpoints/         optional replay-safe metrics
|-- product/
|   |-- session.json                 product state authority
|   `-- final.json                   sole authoritative full result
|-- browser/                         Browser Adapter-owned checkpoints
`-- result/
    `-- summary.json                 regenerable Harness projection
```

Cache, metrics, events, and the result summary are non-authoritative sidecars.
Their absence or write failure cannot change Evidence admission or lifecycle
Outcome.

`mission/pending-effect.json` is a crash-recovery cursor for one Product effect
that has not yet been materialized as a Lease or Outcome. It is bound to the
Run and Mission revision, is replayed before another Product transition, and
never owns lifecycle status or an Outcome.

JSON checkpoints use atomic replacement. Run transitions use one operating-
system advisory lock. Provider and Worker execution never hold that lock.

## Portable references

Persisted `*_ref` values are forward-slash paths relative to the owning Run,
for example `mission/mission.json`, `product/session.json`, and
`product/final.json`. Store reads reject references that escape their owned
artifact directory. Process-local absolute paths may be passed to an active
Worker as execution context, but they are not persisted identities.

## Protocol versions

- request: `2.0`;
- Outcome: `2.0`;
- Run and Search Mission: `3.0`;
- Lease and Work Item projection: `3.0`;
- Receipt and Receipt batch: `2.0`.

Protocol versions are independent of Plugin, Harness, and product release
versions.
