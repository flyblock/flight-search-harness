---
name: low-cost-random-loop-flight-search
description: Discover unusually cheap, flexible-date, multi-leg flight loops and optimize verified distance per unit of the selected Base Currency. Use when the destination is open and the trip returns to the origin region.
---

# Low-Cost Random-Loop Flight Search

Use the random-loop Product Kernel through the Flight Search Harness for an open-destination, airport-only loop that returns to the origin region.

## Prepare

1. From the caller workspace, run `python <skill-directory>/scripts/resolve_harness.py`. Keep the returned `launcher_ref`, `mission_runtime_ref`, and `request_schema_ref`; the resolver uses the bundled Harness when present and obtains the matching Harness when this Skill was installed by itself.
2. Copy and fill [the compact request template](assets/request-template.json). Replace every `REPLACE_...` value and store the request in the caller workspace, normally under `runs/requests/`.
3. Read [the random-loop workflow](references/product-workflow.md) and the returned `mission_runtime_ref` for product decisions and the shared Search Mission procedure.
4. Read the returned public `request_schema_ref` only when uncommon optional constraints or a request-validation failure require fields beyond the template. Product Kernel Schemas are internal and are not Skill request contracts.

The request must state `base_currency`, `adult_count`, `baggage_policy`, `overnight_allowed`, `self_transfer_policy`, the leg range, connection limits, `maximum_total_duration_minutes`, and the complete-loop efficiency floor. This Product supports adult travelers only: treat an unspecified age as adult, but stop with a focused limitation when the user explicitly includes a child or infant. Its current deterministic baggage capability is fixed to `cabin_baggage: "none_required"`, `checked_bag_count: 0`, and `paid_baggage_allowed: true`; this does not exclude fares that include baggage. A stated baggage requirement is currently unsupported and must not be presented as verified. Request Provider prices in the Base Currency; the Harness owns any required system FX normalization. The current independently ticketed Product Kernel also requires `overnight_allowed: true` and `self_transfer_policy: "allow"`; the template literals are capability constraints, not defaults to infer. If the user does not accept any of them, stop for a focused decision instead of ignoring the conflict.

Ask one focused question when any required field is missing or ambiguous. Otherwise preserve the user's explicit constraints in the request.

`airport_transit.unknown_policy: "allow_with_warning"` keeps an explicitly unverified candidate; it never establishes that the adult traveler can legally or practically transit. Disclose the unknown status and do not describe it as verified transit eligibility.

## Run

Execute every live research Work Item through an authorized user-visible Browser page.

Start or resume the Search Mission with the resolved launcher and External Worker. Follow each issued Work Item as the active query, Provider, scope, and payload contract, then submit item-bound Receipts through the same Run. A Worker-directed `blocked` Outcome is an execution instruction, not a result to hand to the user. Resume an actionable `draft` while the active request still authorizes progress.

For a new user search, start a fresh Run and do not inspect or reuse another chat's Run, old Receipt, Session, result, or price. Resume only a Run created earlier in the current task, unless the user explicitly names an older Run to continue.

## Deliver

After the Product Kernel returns a `completed` Outcome, run `python <launcher_ref> --runs-root runs deliver --run-id <run-id>` and present only a `kind=deliverable` Result. Never construct the user answer from a private Product Session, raw solver state, truncated JSON, or browser memory. A `delivery_blocked` response requires the stated recovery action. Present `blocked` only when its required action belongs to the user or another unavailable external actor. Treat `failed` as terminal for that Run. Never fabricate Evidence, book or pay for travel, or present a final result before `completed`.
