---
name: low-cost-one-way-travel-flight-search
description: Search cheap one-way flights and complete low-cost journeys to a selected destination across flexible regional airports. Use when the user does not need to return to the origin.
---

# Low-Cost One-Way Flight Search

Use the one-way Product Kernel through the Flight Search Harness. It may combine verified flights and surface transport, but the journey ends at the user's selected destination and does not return to the origin.

## Prepare

1. From the caller workspace, run `python <skill-directory>/scripts/resolve_harness.py`. Keep the returned `launcher_ref`, `mission_runtime_ref`, and `request_schema_ref`; the resolver uses the bundled Harness when present and obtains the matching Harness when this Skill was installed by itself.
2. Copy and fill [the compact request template](assets/request-template.json). Replace every `REPLACE_...` value and store the request in the caller workspace, normally under `runs/requests/`.
3. Read [the one-way workflow](references/product-workflow.md) and the returned `mission_runtime_ref` for product decisions and the shared Search Mission procedure.
4. Read the returned public `request_schema_ref` only when uncommon optional constraints or a request-validation failure require fields beyond the template. Product Kernel Schemas are internal and are not Skill request contracts.

Record `base_currency`, `adult_count`, `baggage_policy`, `overnight_allowed`, and `self_transfer_policy` explicitly. This Product supports adult travelers only: treat an unspecified age as adult, but stop with a focused limitation when the user explicitly includes a child or infant. Its current deterministic baggage capability is fixed to `cabin_baggage: "none_required"`, `checked_bag_count: 0`, and `paid_baggage_allowed: true`; this does not exclude fares that include baggage. A stated baggage requirement is currently unsupported and must not be presented as verified. Request Provider prices in the Base Currency; the Harness owns any required system FX normalization. Model the origin and selected destination with accepted `anchor_nodes` and optional `seed_gateway_nodes`; do not silently treat a surrounding airport as the user's city endpoint.

Ask one focused question only when a missing origin or destination region, hard date boundary, Base Currency, adult count, overnight permission, self-transfer policy, or hard constraint would materially change the search.

## Run

Start or resume the Search Mission with the resolved launcher and External Worker. Follow the Product Kernel's choices and browser briefs as the active search contract, submit item-bound Receipts, and continue through the same Run. A Worker-directed `blocked` Outcome is an execution instruction, not a result to hand to the user. Resume an actionable `draft` while the active request still authorizes progress.

For a new user search, start a fresh Run and do not inspect or reuse another chat's Run, old Receipt, Session, result, or price. Resume only a Run created earlier in the current task, unless the user explicitly names an older Run to continue.

## Deliver

Present the best verified one-way route within the investigated Evidence when the Product Kernel returns a `completed` Outcome. Present `blocked` only when its required action belongs to the user or another unavailable external actor. Treat `failed` as terminal for that Run. Never fabricate Evidence, book or pay for travel, or present a final result before `completed`.
