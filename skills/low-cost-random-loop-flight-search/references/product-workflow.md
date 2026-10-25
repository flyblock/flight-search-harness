# Random-loop workflow

Use `journey_shape: random_loop`. The user accepts departure and return airport sets while intermediate destinations remain open. Preserve the user's actual accepted endpoints; do not infer permission to return to an unrelated airport.

Use `departure_airports`, `return_airports`, and `first_departure_window`. Add leg counts, minimum connection time, maximum wait, total duration, earliest local departures or transit requirements only when applicable to the user's request. Unknown transit facts require an explicit unresolved judgment, not an invented transit permission.

The default objective is `distance_per_currency`, computed using the chosen price scope in `base_currency`. Record supported distance values with provenance. Missing distance or FX means efficiency is unknown. Do not rank a loop by one attractive leg, mix currencies without the saved FX source, or count an entire joint fare for every leg.

Actively propose and search promising next destinations and returning connections. Save individual observations before a loop is complete; they remain useful leads. Use `combine` with explicit saved Offer references and a reason once you have a proposed complete loop. Preserve joint ticket boundaries and disclose separate-ticket risks.

The code compares explicit saved candidates; it does not independently search every possible airport or enumerate the global optimum. The AI chooses which combinations to investigate. Use `deliver` to examine known efficiency, prices, continuity and constraints before deciding the next query. Stop with an honest reason and remaining search scope, not a claim of exhaustive global optimization.

Challenge the current best complete loop with new intermediate destinations, route structures and dates informed by external route and fare sources. Country or state borders do not automatically constrain the open intermediate destinations, but the accepted departure and return airport sets remain hard endpoints. Retain complete beaten loops as useful lower-ranked alternatives; do not fill a ranking with incomplete single-leg prices.
