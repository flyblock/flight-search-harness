---
name: low-cost-round-trip-flight-search
description: Search outbound-and-return journeys to a selected destination using the user's endpoints, constraints, and objective. Use when the journey must return to the origin.
---

# Round-trip flight search

Search outbound and return dates together. Compare complete dated return journeys, including accepted open-jaw alternatives. Preserve each direction's airports and distinguish return departure from actual return arrival.

Run `python -B <skill-directory>/scripts/resolve_harness.py`. Read its `workflow_ref` and [this journey's workflow](references/product-workflow.md). Keep the returned launcher, Request Schema, record-template and external runtime paths; use the same `--runs-root` for the whole Run. The returned quote, signal, challenge-query and airport-discovery templates show the public format without reading implementation files.

Create a fresh Request and Run from the current user request, using [the request template](assets/request-template.json) where appropriate. Preserve city Anchors and the user's original words; airport-only template values require accepted airport endpoints. Read no unrelated old Runs or conclusions. The shared workflow defines the common fields and rules, including default ticket-cost comparison and optional full journey costs.

Use the best known eligible candidate as a benchmark to challenge. Choose research from saved findings, external discovery and the user's objective; a first usable ticket is not by itself a reason to stop. Save each actual query together with its findings using inline `query` in `record`, or associate a previously registered `query_id`. Preserve partially checked and higher prices as well as useful complete Offers. Missing details do not justify dropping an observed price. Read a tool clock (or the portable `clock` command) while observing the source and reuse that timestamp when saving. Reference and time metadata problems must not discard useful facts; screenshots and page attachments are optional.

Use `purpose: "challenge"` with a reason on competitive queries, and consult `research_feedback` after saving. Where the user permits flexible endpoints, discover reachable airports from external sources rather than treating the first airport list or an administrative border as the search boundary. Follow the shared workflow for `airport_discovery`. Adjust the method when a source cannot execute it. Present the current best and useful complete alternatives already found, including beaten baselines; do not research solely to fill ranks or present unverified signals as bookable runners-up. Include actual dates, airports, flight identities, prices and restrictions from the saved result, including `verification` and `required_disclosures`. Ticket eligibility, booking-page stage, connection protection and endpoint feasibility are separate facts. Record a reason when stopping; neither a stopped Run nor a ranked ticket establishes a global optimum.
