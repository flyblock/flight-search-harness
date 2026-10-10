---
name: low-cost-round-trip-flight-search
description: Search cheap round-trip flights by comparing complete low-cost ticket portfolios to a selected destination and back. Use when the user wants to return to the origin region.
---

# Low-Cost Round-Trip Flight Search

Use the round-trip Product Kernel through the Flight Search Harness. This workflow compares complete outbound-and-return portfolios rather than reusing one-way frontier state.

## Prepare

1. From the caller workspace, run `python <skill-directory>/scripts/resolve_harness.py`. Keep the returned `launcher_ref`, `mission_runtime_ref`, and `request_schema_ref`; the resolver uses the bundled Harness when present and obtains the matching Harness when this Skill was installed by itself.
2. Copy and fill [the compact request template](assets/request-template.json). Replace every `REPLACE_...` value and store the request in the caller workspace, normally under `runs/requests/`.
3. Read [the round-trip workflow](references/product-workflow.md) and the returned `mission_runtime_ref` for product decisions and the shared Search Mission procedure.
4. Read the returned public `request_schema_ref` only when uncommon optional constraints or a request-validation failure require fields beyond the template. Product Kernel Schemas are internal and are not Skill request contracts.

Always capture `base_currency`, `traveler_count`, the shared `baggage_policy`, `overnight_allowed`, `self_transfer_policy`, and `fx_to_base`; do not infer them from an earlier search or geography. Whenever `fx_to_base` contains a non-Base-Currency rate or any money value uses a non-Base Currency, include a matching source-attributed `fx_snapshot` that is current when the Run starts; refresh it before completion if it expires during the search. Set each regional `max_ground_cost` to an explicit Base Currency amount or `null` for no cost ceiling. Keep `endpoint_policy: "anchor_required"` when surrounding airports require priced access or egress; use `any_admitted_gateway` only when any admitted airport is itself an accepted journey endpoint.

Ask one focused question when a missing origin or destination region, outbound or return boundary, Base Currency or FX basis, traveler count, baggage policy, overnight policy, self-transfer policy, or hard constraint would materially change the search.

## Run

Start or resume the Search Mission with the resolved launcher and External Worker. Follow the Product Kernel's gateway, portfolio, and browser briefs as the active search contract, submit item-bound Receipts, and continue through the same Run. A Worker-directed `blocked` Outcome is an execution instruction, not a result to hand to the user. Resume an actionable `draft` while the active request still authorizes progress.

## Deliver

Present the best verified complete round-trip portfolio within the investigated Evidence when the Product Kernel returns a `completed` Outcome. Present `blocked` only when its required action belongs to the user or another unavailable external actor. Treat `failed` as terminal for that Run. Never fabricate Evidence, book or pay for travel, or present a final result before `completed`.
