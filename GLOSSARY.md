# Flight Search Harness

This glossary defines the domain language used to describe the Flight Search Harness and its travel-search lifecycle. Use these terms consistently in product documentation, Skills, Schemas, and user-facing outcomes.

## Distribution and operation

**Flight Search Harness**:
The complete system that coordinates one travel-search product from a user request to a durable Outcome.
_Avoid_: Flight Skill, Skill suite, travel-search framework

**Plugin**:
The portable distribution that installs the Flight Search Harness, its product modules, and its user-facing Skills as one package.
_Avoid_: Harness, Skill bundle

**Skill**:
A natural-language workflow entry point that teaches an agent when and how to use one capability of the Flight Search Harness.
_Avoid_: Plugin, Harness, prompt file

**Product Kernel**:
The independent decision authority for one journey shape, including its search state, evidence admission, solving rules, and completion criteria.
_Avoid_: Product engine, shared solver

**Product Adapter**:
The product-specific translator between the shared Harness lifecycle and one Product Kernel.
_Avoid_: Product wrapper, product bridge

## Search lifecycle

**Search Mission**:
The durable, resumable lifecycle of one Harness search request handled by exactly one Product Kernel.
_Avoid_: Job, task, search session

**Work Item**:
One bounded unit of research, choice, correction, or judgment issued by a Product Kernel for a Worker to complete.
_Avoid_: Task, action

**Lease**:
A time-limited claim that assigns a set of unfinished Work Items to one Worker while preserving safe recovery and takeover.
_Avoid_: Lock, reservation

**Receipt**:
An immutable submission that records a Work Item's result for Product Kernel admission.
_Avoid_: Response, callback, report

**Run**:
The persisted execution record for one Search Mission, including its state, artifacts, and Outcome.
_Avoid_: Search Mission, session

**Outcome**:
The authoritative current result state of a Run: `draft`, `blocked`, `failed`, or `completed`.
_Avoid_: Status report, answer

## Research and evidence

**Worker**:
The actor that executes Work Items and returns Receipts without deciding product validity or completion.
_Avoid_: Solver, Product Kernel, browser

**Observation**:
A structured record of what a Worker encountered while researching a source.
_Avoid_: Evidence, Offer, result

**Offer**:
A concrete travel option represented in an Observation, such as a priced flight or surface segment.
_Avoid_: Observation, itinerary, Evidence

**Evidence**:
An Observation or Offer that a Product Kernel has admitted for deterministic calculation or finalization.
_Avoid_: Raw result, Receipt, claim

**Journey Alternative**:
A complete user-selectable Core Itinerary grouped by its actual Gateway pattern and mandatory internal Movement structure. Exact travel dates, Seller Variants, Offer Snapshots, and Endpoint Completion variants do not create additional Journey Alternatives; the cheapest verified date represents the Journey Alternative.
_Avoid_: Seller result, Endpoint completion option, fare row, research signal

**Ticketing Structure**:
The atomic ticket arrangement used to purchase one Journey Alternative, including joint, split, nested, protected, or self-transfer relationships.
_Avoid_: Journey Alternative, Seller Variant

**Seller Variant**:
A Seller-specific payable option for one Ticketing Structure whose price and booking terms may differ without changing the journey geometry.
_Avoid_: Journey Alternative, Provider result

**Offer Snapshot**:
One time-bounded Observation of a Seller Variant. A newer Offer Snapshot may supersede an older snapshot of the same Seller Variant without creating a new Ticketing Structure or Journey Alternative.
_Avoid_: Seller Variant, refreshed route, duplicate Offer

**Candidate Slate**:
The Product Kernel's bounded, price-ranked projection of distinct complete Journey Alternatives already produced while challenging the current winner. It is a Result projection, never a required count or a research quota.
_Avoid_: Raw Portfolio list, fare signals, Top-K search scope

**Search Effort**:
The request policy that selects a Research Credit budget and percentage Price Challenge Threshold, normally `economy`, `standard`, or `deep`.
_Avoid_: Reasoning effort, model effort, timeout

**Research Credit**:
A Product-owned budget unit charged only for new external research. Evidence replay, representation repair, and Harness-caused retries do not consume Research Credits.
_Avoid_: Token, request count, elapsed time

**Price Challenge Threshold**:
The minimum percentage by which a candidate's optimistic Mandatory Air Ticket Total must improve on the current winner before further research is justified.
_Avoid_: Savings guarantee, budget, price cap

**Store**:
The durable local record that owns persisted Runs and their lifecycle artifacts.
_Avoid_: Cache, archive, database

## Money and value

**Base Currency**:
The user-selected currency into which monetary values are normalized for deterministic comparison within one Search Mission.
_Avoid_: Quality currency, default currency

**Currency-Unit Efficiency**:
Verified travel distance divided by mandatory price expressed in the Base Currency.
_Avoid_: Currency-specific efficiency, currency score

## Journey cost scope

**Anchor**:
A user-declared real-world journey endpoint, such as a city, address, station, or exact airport, to which Endpoint completion may be required.
_Avoid_: Gateway

**Gateway**:
An admitted journey endpoint at which the flight-centered itinerary may begin or end, normally an airport or another explicitly accepted transport node. Its stable Node ID identifies it; names and location labels describe it but do not create another Gateway.
_Avoid_: Anchor, home location

**Self-access Gateway Pool**:
The disclosed, AI-defined set of Gateways that the user is assumed to reach or leave independently. It has no numeric airport cap; Search Effort limits external research rather than semantic membership.
_Avoid_: Local airport list, remote-airport boundary, capped Gateway list

**Core Itinerary**:
The complete mandatory journey between the first and final selected Gateways, including flights, positioning tickets, Internal Surface Transfers, required baggage, and unavoidable accommodation.
_Avoid_: Airfare only, flight segments only

**Internal Surface Transfer**:
A mandatory non-flight movement inside the Core Itinerary whose absence would break the selected ticket or movement sequence.
_Avoid_: Endpoint Access, optional local transport

**Endpoint Access**:
A movement from a user Anchor to the first selected Gateway before the Core Itinerary begins.
_Avoid_: Internal Surface Transfer

**Endpoint Egress**:
A movement from the final selected Gateway to a user Anchor after the Core Itinerary ends.
_Avoid_: Internal Surface Transfer

**Gateway-complete Cost**:
The mandatory Base Currency cost of the Core Itinerary, excluding Endpoint Access and Endpoint Egress.
_Avoid_: Airfare, ticket price

**Endpoint Completion Cost**:
The mandatory Base Currency cost of the Endpoint Access and Endpoint Egress required by the request's Endpoint Policies.
_Avoid_: Gateway-complete Cost

**Anchor-complete Cost**:
The mandatory Base Currency cost from origin Anchor to destination Anchor when both sides require Endpoint completion.
_Avoid_: Gateway-complete Cost, door-to-door estimate

**Mandatory Air Ticket Total**:
The Base Currency total of every payable flight Offer required after the first selected Gateway, including air positioning, self-transfer flights, required baggage, and mandatory airline fees. It excludes Endpoint Access, Endpoint Egress, Internal Surface Transfer prices, accommodation, and other non-air costs.
_Avoid_: Complete trip price, Anchor-complete Cost, door-to-door total

**Endpoint Policy**:
The per-side request rule that either requires connection to the user Anchor or accepts any admitted Gateway as the journey endpoint.
_Avoid_: Evidence source, transport mode

## Time

**Local Date**:
A calendar date interpreted in one explicit location timezone; it is not an instant.
_Avoid_: UTC date, Offset Timestamp

**Offset Timestamp**:
A date and time carrying a UTC offset that identifies an instant for one observed event.
_Avoid_: Local Date

**Destination Stay Nights**:
The Product Kernel-derived Local Date difference from Destination Arrival to Return Departure under the request's destination Endpoint Policy.
_Avoid_: Departure-date gap, Worker-reported stay
