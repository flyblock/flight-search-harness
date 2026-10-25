---
name: low-cost-one-way-travel-flight-search
description: Search one-way journeys to a selected destination using the user's endpoints, constraints, and objective. Use when no return to the origin is required.
---

# One-way flight search

Goal: find the cheapest bookable one-way journey that fits the user's request (or their stated objective), and show the useful runners-up you found on the way, with dates, airports, flight numbers, price and key restrictions.

1. Run `python -B <skill-directory>/scripts/resolve_harness.py`. Keep the returned paths. Read `workflow_ref` once; it is the full reference. You do not need the Request Schema or implementation files. If this Skill was installed on its own, the resolver downloads the matching framework release from github.com/flyblock/flight-search-harness into `~/.flight-search-harness` on first use (it says so on stderr); tell the user once.
2. Write a fresh Request from [the template](assets/request-template.json) and the user's words, then `start`. Do not open old Runs.
   - `journey_shape: one_way`; dates: `departure_window` or `exact_departure_dates`; optional `arrival_window` for a real arrival deadline. Never add a return.
   - A city or region endpoint becomes an `origin`/`destination` region declaration (workflow §1), not an airport list.
3. Search and `record` each finding as you go (workflow §2). Read `clock` for each observation's `captured_at`.
4. Explore (workflow §3): challenge the current best; discover airports beyond borders on both sides; search from both ends (cheapest flights out of the origin to anywhere, cheapest flights into the destination from anywhere) and connect them by air, rail or bus, recording each priced piece and joining them with `combine`; follow cheaper leads; cross-check sellers.
5. `deliver`, then answer as in workflow §6.

One-way specifics:
- With `complete_journey_cost` (only if the user asked for door-to-door cost), record mandatory access, egress and transfer movements with their prices; unknown costs stay unknown.
- A separate-ticket combination keeps its self-transfer and baggage risks in the answer.
