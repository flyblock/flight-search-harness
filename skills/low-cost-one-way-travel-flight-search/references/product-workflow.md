# One-way workflow

Use `journey_shape: one_way`. Preserve the selected destination, accepted origin alternatives, departure window, and any actual arrival deadline. Do not add a return requirement.

Use `origin_airports` and `destination_airports` only for accepted airport endpoints. For city, address, or station requirements use explicit `origin` and `destination` Anchor declarations from the shared Schema. Airport search geography and the user's actual endpoint requirement are separate choices.

The default price comparison is `mandatory_air_ticket_total`. If the user requests full door-to-door expense, choose `complete_journey_cost` and collect mandatory access, egress and transfer costs as applicable. Unknown costs stay unknown and do not invalidate saving an otherwise useful flight observation.

Explore neighboring dates, accepted airports, sellers and feasible combinations. Save each actual observation immediately. Joint ticket totals remain indivisible. Explicit `combine` selections may include multiple saved purchases; carry their connection, baggage and self-transfer evidence into comparison without claiming unobserved protection.

Use `deliver` for the saved ranking, then choose the next query based on price and unresolved requirements. User budget and travel-duration goals take precedence over template defaults. A source's unsupported query becomes a reason to change the search approach, not an automatic end to the Run.

For flexible city or region endpoints, discover reachable alternative airports through external airport, route or ground-transport information. An airport across a national, state or municipal boundary is not automatically out of scope; preserve explicit user restrictions and the original Anchor. Record airport facts separately from the model's access/price-potential judgment. Query promising dates and routes as challengers to the current best, then let the existing comparison retain useful former baselines as alternatives. Unknown access cost stays separate from airfare and does not prove a lower complete journey cost.
