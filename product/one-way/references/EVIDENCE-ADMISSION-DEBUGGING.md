# Evidence admission debugging

Use this reference only when a current observation is rejected or admitted unexpectedly. It contains no historical routes or acceptance-run answers.

## Debug order

1. Confirm the Observation belongs to the selected Work Item and current generation.
2. Confirm `evidence_status` matches the payload: research and terminal observations carry no priced edge; verified observations carry exact current movement and payable-offer identity.
3. Confirm every endpoint has a declared or transactionally supplied node record.
4. Confirm local times include UTC offsets and fall inside the Work Item window.
5. Confirm mandatory amount, currency, seller/operator, URL, observation time, baggage, and service identity are present.
6. For an atomic multi-component offer, confirm the full ticketed movement set and one indivisible price. Never invent component prices.
7. For planned exit, confirm used and unused components partition the full ticket and the full price remains attached.
8. Confirm research-only provenance is separate from the later payable seller observation.
9. Confirm duplicate bundle, movement, offer, itinerary, and snapshot identities are stable rather than regenerated from filenames.
10. Retry transactionally: a rejected ingest must leave the previous Session unchanged.

An exact replay of an already admitted Evidence bundle is an idempotent no-op. Reusing the same `bundle_id` with different Worker-authored content is a protocol error. Product-derived source-fulfillment audit fields do not change replay identity.

## Terminal evidence

`no_result` and `rejected` require the actual source, URL, offset-aware observation time, and a non-empty Work Item-scoped reason. They never create a node, movement, Offer, fare, or market-wide absence claim.

## Current-offer checks

An expired snapshot may remain a research lead but cannot form the current winning route. A refreshed seller observation creates a new immutable snapshot under the same physical itinerary/offer identity.

Finalization captures one evaluation time, reselects the cheapest fresh snapshot in every itinerary Offer set, and reruns the deterministic solver from those eligible edges. The exported result comes from that fresh projection rather than from the Session's earlier solver projection. When the previously selected snapshot has expired but another seller snapshot remains fresh, the fresh alternative may still complete; when every supporting snapshot is stale, finalization is blocked.
