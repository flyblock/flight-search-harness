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

The request must state `base_currency`, `traveler_count`, `baggage_policy`, `overnight_allowed`, `self_transfer_policy`, `fx_to_base`, the leg range, connection limits, `maximum_total_duration_minutes`, and the complete-loop efficiency floor. Whenever `fx_to_base` contains a non-Base-Currency rate or any money value uses a non-Base Currency, include a matching source-attributed `fx_snapshot` that is current when the Run starts; refresh it before completion if it expires during the search. The current independently ticketed Product Kernel requires confirmed values `paid_baggage_allowed: true`, `overnight_allowed: true`, and `self_transfer_policy: "allow"`; the template literals are capability constraints, not defaults to infer. If the user does not accept any of them, stop for a focused decision instead of ignoring the conflict.

Ask one focused question when any required field is missing or ambiguous. Otherwise preserve the user's explicit constraints in the request.

## Run

Start or resume the Search Mission with the resolved launcher and External Worker. Follow each issued Work Item as the active query, Provider, scope, and payload contract, then submit item-bound Receipts through the same Run. A Worker-directed `blocked` Outcome is an execution instruction, not a result to hand to the user. Resume an actionable `draft` while the active request still authorizes progress.

## Deliver

Present the verified loop or bounded-no-result conclusion when the Product Kernel returns a `completed` Outcome. Present `blocked` only when its required action belongs to the user or another unavailable external actor. Treat `failed` as terminal for that Run. Never fabricate Evidence, book or pay for travel, or present a final result before `completed`.
