# Flight Search Harness glossary

This file defines meanings. Product policies, ranking objectives, search budgets, protocol layouts, and implementation choices belong to their own contracts.

## Distribution and roles

**Flight Search Harness**:
The application that coordinates travel research and its durable results.

**Plugin**:
A portable distribution of the application and its agent entry points.

**Skill**:
Instructions that guide an agent in using a capability.

## Search records

**Search Mission**:
The durable context of research undertaken for one travel request.

**Run**:
The persisted execution record of a Search Mission.

**Outcome**:
The result state reported for a Run. Completion is relative to the requested scope.

**Store**:
The durable record of Runs and their artifacts.

## Observations and choices

**Observation**:
A structured record of what was encountered at a source.

**Offer**:
A concrete travel option and the price or terms disclosed for it.

**Evidence**:
An Observation or Offer admitted to support a calculation or decision.

**Provider Capture**:
A record of facts visibly collected from a Provider page at a particular time.

**Source Identity**:
The Provider identity and exact observed page URL associated with a capture. Seller and carrier identities describe different roles.

**Value State**:
Whether a value is known, unknown, or not applicable. An unknown amount is not a zero amount.

**Journey Alternative**:
One user-selectable journey option satisfying the requested journey scope.

**Ticketing Structure**:
The arrangement of tickets and their joint, separate, protected, or self-transfer relationships.

**Seller Variant**:
A Seller-specific payable option for a Ticketing Structure.

**Offer Snapshot**:
An Observation of a Seller Variant at a particular time.

**Candidate Slate**:
A ranked set of Journey Alternatives for a request. Its ranking objective and cost scope are explicit properties of that request and result.

## Endpoints and movements

**Anchor**:
A real-world endpoint declared by the user, such as a city, address, station, or airport.

**Gateway**:
A transport node used to begin or end the Core Itinerary. A Gateway can coincide with an Anchor or require a connection to it.

**Self-access Gateway Pool**:
A set of Gateways the user has accepted reaching or leaving independently.

**Endpoint Policy**:
The requirement for connecting one side of the journey to its Anchor or accepting a Gateway as that endpoint.

**Core Itinerary**:
The mandatory journey between the first and final selected Gateways, including every movement and purchase needed to make that journey feasible.

**Internal Surface Transfer**:
A mandatory non-flight movement inside the Core Itinerary.

**Endpoint Access**:
Movement from an Anchor to the first selected Gateway before the Core Itinerary.

**Endpoint Egress**:
Movement from the final selected Gateway to an Anchor after the Core Itinerary.

## Money and cost scope

**Base Currency**:
The currency selected for monetary comparison within a Search Mission.

**Currency-Unit Efficiency**:
Travel distance divided by the applicable price expressed in the Base Currency.

**Gateway-complete Cost**:
The total mandatory cost of the Core Itinerary, excluding Endpoint Access and Endpoint Egress.

**Endpoint Completion Cost**:
The total mandatory cost of the Endpoint Access and Endpoint Egress required by the request.

**Anchor-complete Cost**:
The total mandatory cost of the journey connecting the requested Anchors.

**Mandatory Air Ticket Total**:
The total payable flight-ticket amount within the stated itinerary scope, including required baggage and mandatory airline fees. It does not represent excluded non-air costs.

## Time

**Local Date**:
A calendar date interpreted in an explicit location timezone.

**Offset Timestamp**:
A date and time carrying a UTC offset that identifies an instant.

**Capture Time**:
The time at which a Provider page was actually observed.

**Provider Quote Time**:
The creation or update time explicitly disclosed by a Provider for its quote. An undisclosed quote time remains unknown.

**Recorded Time**:
The time at which an Observation was saved. It is distinct from Capture Time.

**Destination Stay Nights**:
The difference between the destination-arrival and return-departure Local Dates at the destination required by the request.
