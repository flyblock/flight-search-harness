---
name: low-cost-one-way-travel-flight-search
description: Search the lowest verified one-way Mandatory Air Ticket Total across flexible regional airports. Use when the user does not need to return to the origin.
---

# Low-Cost One-Way Flight Search

Use the one-way Product Kernel through the Flight Search Harness. Rank complete feasible flight paths by Mandatory Air Ticket Total; do not turn optional ground transport or accommodation research into a prerequisite for finding the airfare winner.

## Prepare

1. From the caller workspace, run `python <skill-directory>/scripts/resolve_harness.py`. Keep the returned `launcher_ref`, `mission_runtime_ref`, and `request_schema_ref`; the resolver uses the bundled Harness when present and obtains the matching Harness when this Skill was installed by itself.
2. Copy and fill [the compact request template](assets/request-template.json). Replace every `REPLACE_...` value and store the request in the caller workspace, normally under `runs/requests/`.
3. Read [the one-way workflow](references/product-workflow.md) and the returned `mission_runtime_ref` for product decisions and the shared Search Mission procedure.
4. Read the returned public `request_schema_ref` only when uncommon optional constraints or a request-validation failure require fields beyond the template. Product Kernel Schemas are internal and are not Skill request contracts.

Record `base_currency`, `adult_count`, `baggage_policy`, `overnight_allowed`, `self_transfer_policy`, and `search_effort` explicitly. Default `search_effort` to `standard`. This Product supports adult travelers only: treat an unspecified age as adult, but stop with a focused limitation when the user explicitly includes a child or infant. Its current deterministic baggage capability is fixed to `cabin_baggage: "none_required"`, `checked_bag_count: 0`, and `paid_baggage_allowed: true`; this does not exclude fares that include baggage. A stated baggage requirement is currently unsupported and must not be presented as verified. Request Provider prices in the Base Currency; the Harness owns required FX normalization.

For a city or region, let the AI define the finite Self-access Gateway Pool and use `any_admitted_gateway`; there is no numeric airport cap. The user arranges Endpoint Access and Endpoint Egress outside the ranking. Use `anchor_required` only for an exact airport endpoint explicitly requested by the user. Do not create priced Endpoint Work Items.

Ask one focused question only when a missing origin or destination region, hard date boundary, Base Currency, adult count, overnight permission, self-transfer policy, or explicit hard constraint would materially change the search. Omit duration and transfer-count limits unless the user states them; long waits and unprotected or overnight connections are warnings when otherwise allowed.

## Run

Start or resume the Search Mission with the resolved launcher and External Worker. Follow the Product Kernel's browser briefs, submit item-bound Receipts, and continue through the same Run. Representation correction and Evidence replay must reuse captured facts and must not repeat Provider research. A Worker-directed `blocked` Outcome is an execution instruction, not a result to hand to the user. Resume an actionable `draft` while the active request still authorizes progress.

Execute every live research Work Item through an authorized user-visible Browser page.

For a new user search, start a fresh Run and do not inspect or reuse another chat's Run, old Receipt, Session, result, or price. Resume only a Run created earlier in the current task, unless the user explicitly names an older Run to continue.

## Deliver

After the Product Kernel returns a `completed` Outcome, run `python <launcher_ref> --runs-root runs deliver --run-id <run-id>` and present only a `kind=deliverable` Result. State that the winner is the lowest verified Mandatory Air Ticket Total within the disclosed Self-access Gateway Pool, searched scope, Search Effort, and Research Credits. Disclose excluded Endpoint, Internal Surface Transfer, accommodation, and other non-air costs; show booking links, freshness, warnings, and unresolved or unsearched scope. Never construct the answer from private state or browser memory. A `delivery_blocked` response requires its recovery action. Never fabricate Evidence, book, pay, or present a final result before `completed`.
