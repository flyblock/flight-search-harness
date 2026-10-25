# Round-trip workflow

Use `journey_shape: round_trip`. Explore both departure windows, not just the first attractive pair. Destination Stay Nights are destination arrival to return departure local dates; actual elapsed stay needs offset timestamps. A return departure date does not establish arrival at the required endpoint before a presence deadline.

For airport endpoints accepted by the user, use `origin_airports` and `destination_airports`. Otherwise preserve explicit `origin` and `destination` Anchor declarations from the shared Schema. Origin requirements govern outbound access and return egress; destination requirements govern outbound egress and return access. A destination airport concession does not waive the origin city requirement.

Use `outbound_departure_window` and, when specified, `return_departure_window`. Disjoint alternatives can use `travel_windows`; do not also supply conflicting top-level departure windows. Preserve user stay limits, actual arrival deadlines, budget and desired objective. With `objective: stay_duration`, code orders applicable options using known destination stay; it must not substitute departure-date differences for actual elapsed time.

For requirements such as recurring presence in a city, translate the user's rule into concrete `presence_intervals` for the requested search horizon. Include the location node and offset timestamps. Preserve any necessary endpoint connection; unresolved arrival into the city remains unknown. Do not turn this request-specific interval into a permanent weekday prohibition.

Record one seller's outbound-and-return ticket as an atomic Offer whose `movement_ids` cover its actual flights. Do not divide a jointly priced ticket into fictitious independent fares. Use `combine` only for separately observed purchases with a reasoned feasible connection. A separate-ticket option retains its transfer and baggage limitations.

Read `deliver` after findings and requirement revisions. A saved cheap option may later become nonconforming without being deleted. A higher-priced alternative can be shown with its budget violation. Show a date matrix only when requested; do not claim every date or seller has been searched merely because several exact quotes were found.

For flexible city or region endpoints, discover reachable alternative airports through external airport, route or ground-transport information. An airport across a national, state or municipal boundary is not automatically out of scope; preserve explicit user restrictions and the original Anchor. Record airport facts separately from the model's access/price-potential judgment. Query promising dates and routes as challengers to the current best, then let the existing comparison retain useful former baselines as alternatives. Unknown access cost stays separate from airfare and does not prove a lower complete journey cost.
