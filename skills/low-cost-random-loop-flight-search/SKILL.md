---
name: low-cost-random-loop-flight-search
description: Search open-destination multi-leg airport loops using accepted endpoints and distance per currency. Use when the journey returns to the accepted origin airports.
---

# Random-loop flight search

Goal: find multi-leg loops from the user's accepted departure airports back to their accepted return airports with the best distance per unit of base currency (or their stated objective), and show useful runners-up, with dates, airports, flight numbers, prices and key restrictions.

1. Run `python -B <skill-directory>/scripts/resolve_harness.py`. Keep the returned paths. Read `workflow_ref` once; it is the full reference. You do not need the Request Schema or implementation files. If this Skill was installed on its own, the resolver downloads the matching framework release from github.com/flyblock/flight-search-harness into `~/.flight-search-harness` on first use (it says so on stderr); tell the user once.
2. Write a fresh Request from [the template](assets/request-template.json) and the user's words, then `start`. Do not open old Runs.
   - `journey_shape: random_loop`; `departure_airports`, `return_airports`, `first_departure_window`. Add `min_legs`/`max_legs`, connection buffers, maximum waits, earliest local departure or `airport_transit` only if the user set them.
   - Departure and return airport sets are hard endpoints; intermediate destinations are open and borders do not limit them.
3. Search and `record` each leg's findings as you go (workflow §2); single legs are useful leads before a loop is complete. Read `clock` for each observation's `captured_at`.
4. Propose promising next destinations and return connections from external route and fare sources. When you have a complete loop, `combine` its saved Offers with a reason. Challenge the best loop with new intermediates, structures and dates (workflow §3).
5. `deliver`, then answer as in workflow §6.

Loop specifics:
- Efficiency = distance / price in base currency. Record distance (`distance_km`) with its source; missing distance or FX means unknown efficiency.
- A joint fare covering several legs counts once; never rank a loop by one cheap leg.
- Disclose separate-ticket and transit risks; unknown transit permission stays unknown.
