# Corridor-Independent Date Scheduling (v0.7.1)

## Problem

"The conventional route is cheapest on November 18" does not imply that a transit-corridor route should also be searched only on November 18. Airlines, gateways, and transport modes can have different low-fare dates. Keeping only one global minimum-fare date systematically misses unconventional combinations.

Version 0.7.1 makes the browser query unit a corridor-date surface rather than an entire market on one date or a 30-day daily brute-force scan.

## Date intent

- `exact_dates`: the user accepts only the listed dates. Execute every listed date and do not substitute another date.
- `bounded_window`: the user accepts a finite window. When the Provider offers a calendar or grid, scan that bounded view once and verify a small set of concrete dates.
- `fully_flexible`: the user accepts any date inside the window. Scan the entire hard date window independently for each corridor; do not reuse the cheapest day from another corridor.

`date_window` is always a hard boundary. Date flexibility never expands it.

## Forming corridors

The scheduler reads only admitted nodes and edges:

1. Observed flights leaving the origin-airport pool form forward anchors.
2. Observed flights entering the destination-airport pool form backward anchors.
3. Time-compatible anchors are paired, and node `country_code`, geographic cells, and observed modes derive the corridor and route shape.
4. Observed intermediate paths may participate in a corridor. An unknown connection emits only `corridor_connector_research`; it does not create a TravelEdge.

No airport, country, region, or border is a hard-coded entry point. Each can enter the node and edge sets only through browser or Provider Evidence for the active Search Mission.

## Query budget

Scheduling uses four quotas: corridor, geographic cell, route shape, and date bucket. It also retains capacity for a conventional baseline and anomaly exploration. The default policy does not create 30 daily queries. Output explicitly reports:

- `daily_brute_force_queries: 0`;
- the number of calendar or grid requests;
- discovered and scheduled corridor counts; and
- quota use, budget, and script recommendation reasons, while leaving the full candidate set available to the AI.

The script returns every corridor candidate and marks the suggested subset with stable `hypothesis_id` and `selection_advice` values. The AI keeps several distinct corridors but researches one active hypothesis at a time, switching after completion or after confirming a hard dead end. For a selected `corridor_calendar_scan`, the browser AI:

1. Uses the source's own flexible-date, calendar, or grid view first.
2. Retains multiple price-time Pareto dates required by the Work Item instead of returning only one global minimum.
3. Opens a concrete date and Offer page to verify the carrier, flight number, times, seller, complete mandatory price, and baggage.
4. Treats calendar estimates only as discovery signals, never as `verified_observed` fares.

## Correct claim

This is a budgeted, auditable mechanism for discovering unconventional routes. It proves that one corridor is not incorrectly suppressed by reusing another corridor's dates. It does not claim exhaustive coverage of every corridor, Provider, or date worldwide.
