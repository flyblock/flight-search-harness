# Airport Operating Topology

`scripts/airport_transit.py` determines whether two journey legs can connect through an airport's physical layout and operating hours. It does not decide whether a traveler is eligible to enter any country or region.

## Scope

The Module handles:

- terminal and zone opening intervals;
- `domestic`, `schengen`, `non_schengen`, and `international` zones;
- airside connections, landside exits, border-control steps, and security rescreening;
- directional minimum transfer times;
- airside overnight stays; and
- lounge terminal and zone locations, opening intervals, and maximum stays.

`requires_border_control` means only that the airport path includes a border-control step. The Product Kernel returns that fact to its caller and includes it in transfer time, but does not use it to decide or infer traveler eligibility. `eligibility_assessed` is always `false`.

## Data model

The catalog follows `schemas/airport-transit.schema.json`:

- `airport` contains a time zone, terminals, connection edges, and lounges;
- `terminal` has its own opening intervals;
- `zone` belongs to one terminal and has an independent zone type, opening intervals, and airside-overnight rule;
- `connection` is a directional airport-internal edge; add a separate reverse edge when travel in the opposite direction is possible; and
- `lounge.location` identifies an exact terminal and zone.

Daily opening intervals use local wall-clock time. `days` identifies the weekday on which the interval starts. `00:00`-`24:00` means all day; an end time not later than the start time means the interval crosses midnight. Request timestamps must be ISO 8601 values with a UTC offset so local times observed on a page are not mistaken for UTC.

## Commands

Validate a catalog:

```powershell
python scripts/airport_transit.py validate `
  --catalog <airport-transit-catalog.json>
```

Evaluate a set of cases:

```powershell
python scripts/airport_transit.py evaluate `
  --catalog <airport-transit-catalog.json> `
  --cases <airport-transit-cases.json>
```

The Python entry points are `load_catalog()`, `validate_catalog()`, `evaluate_transfer()`, and `evaluate_cases()`. The Module uses only the Python standard library.

## Decision order

1. Find an airport-internal topology path between the two requested points. Exclude edges that require a landside exit when `airside_only=true`.
2. Compare the available connection time with the path's minimum transfer time.
3. Check that every terminal, zone, and connection edge along the path stays open for the required interval.
4. When a zone is occupied across a local date boundary, check whether that zone permits an airside overnight stay.
5. If the user explicitly requires a lounge, check its location, continuous opening interval, and maximum stay.

The result provides reason codes, selected connection edges, the minimum transfer time, and operating steps. An upstream route engine may reject a physically infeasible candidate from this result, but must not extend it into a traveler-eligibility conclusion.

Collect real airport information dynamically through the browser or another Provider, record its source and observation time, and then map it into the same catalog structure. Do not encode one airport's operating facts in the algorithm.
