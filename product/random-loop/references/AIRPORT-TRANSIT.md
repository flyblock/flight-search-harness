# Airport operating topology

`scripts/airport_transit.py` determines whether two travel segments can connect within an airport's physical layout and opening hours. It does not assess whether a passenger may enter any country or region.

For the random-loop Product Kernel, this Module validates only airport-internal connections between adjacent flight segments. It does not introduce rail, bus, ferry, or city-destination nodes, so the product preserves its `airports_only` boundary.

## Boundary

The Module handles:

- terminal and zone opening intervals;
- domestic and international zones;
- airside connections, landside exits, border-control processes, and security rechecks;
- directional minimum transfer times;
- airside overnight stays; and
- lounge terminal and zone locations, opening intervals, and maximum stay durations.

`requires_border_control` means only that the airport path includes a border-control step. The engine returns this fact to its caller and includes it in the transfer time, but it does not use it to assess or infer passenger eligibility. `eligibility_assessed` is always `false` in the result.

## Data structure

The catalog conforms to `references/airport-transit.schema.json`:

- `airport` contains the time zone, terminals, connection edges, and lounges;
- `terminal` has its own opening intervals;
- `zone` belongs to one terminal and has its own zone type, opening intervals, and airside-overnight rule;
- `connection` is a directed airport-internal edge; a traversable reverse direction requires a separate edge; and
- `lounge.location` identifies a specific terminal and zone.

Daily opening intervals use local wall-clock time. `days` identifies the weekday on which the interval begins. `00:00`–`24:00` means all day; an end time at or before the start time means the interval crosses midnight. Request times must be ISO 8601 values with a UTC offset so that local times observed on a web page are not mistaken for UTC.

The Python entry points are `load_catalog()`, `validate_catalog()`, and `evaluate_transfer()`. The Module uses only the Python standard library.

## Evaluation order

1. Find an airport-internal topology path between the request endpoints. When `airside_only=true`, exclude edges that require a landside exit.
2. Compare the available connection time with the path's minimum transfer time.
3. Check that every terminal, zone, and connection edge on the path remains open for the applicable interval.
4. When occupancy of a zone crosses a local-date boundary, check whether that zone permits an airside overnight stay.
5. When the user explicitly requires a lounge, check its location, continuous opening interval, and maximum stay duration.

The result contains reason codes, selected connection edges, the minimum transfer time, and operational steps. The route engine may use this result to reject a physically infeasible candidate, but it must not extend the result into a passenger-eligibility conclusion.

Collect real airport data dynamically through the browser or a Provider, record its source and observation time, and map it into the same catalog structure. Do not encode one airport's operating facts in the algorithm.
