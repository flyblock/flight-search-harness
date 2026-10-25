---
name: low-cost-round-trip-flight-search
description: Search outbound-and-return journeys to a selected destination using the user's endpoints, constraints, and objective. Use when the journey must return to the origin.
---

# Round-trip flight search

Goal: find the cheapest bookable return journey that fits the user's request (or their stated objective), and show the useful runners-up you found on the way, with dates, airports, flight numbers, price and key restrictions.

1. Run `python -B <skill-directory>/scripts/resolve_harness.py`. Keep the returned paths. Read `workflow_ref` once; it is the full reference. You do not need the Request Schema or implementation files.
2. Write a fresh Request from [the template](assets/request-template.json) and the user's words, then `start`. Do not open old Runs.
   - `journey_shape: round_trip`; dates: `outbound_departure_window`, `return_departure_window` (or exact dates / `travel_windows` for disjoint options); stay: `minimum_stay_nights`, `maximum_stay_nights`.
   - A city or region endpoint ("Frankfurt city", "Greater Bay Area") becomes an `origin`/`destination` region declaration (workflow §1), not an airport list.
   - Origin rules govern outbound access and return egress; destination rules govern outbound egress and return access.
3. Search and `record` each finding as you go (workflow §2). Read `clock` for each observation's `captured_at`.
4. Explore (workflow §3): challenge the current best; discover airports beyond borders on both sides; try open-jaw (fly into one airport, home from another) and separate outbound/return tickets via `combine` when allowed; follow cheaper leads; cross-check sellers. Search both date windows, not just the first attractive pair.
5. `deliver`, then answer as in workflow §6.

Round-trip specifics:
- One seller's joint outbound-and-return price is one Offer covering all its flights; never split it into invented one-way prices.
- Stay nights count from destination arrival date to return departure date (local). A return departure date says nothing about arrival time back home.
- For recurring presence rules ("must be in the city on Mondays"), translate into concrete `presence_intervals` for this search horizon.
- A cheap option that later violates a revised requirement stays saved and shows as an alternative with its violation.
