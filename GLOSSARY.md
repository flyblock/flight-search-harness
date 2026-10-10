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
