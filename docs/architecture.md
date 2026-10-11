# Architecture

Flight Search Harness is the runtime product. The Plugin is its portable distribution, and Skills are the natural-language workflow entry points used by an agent.

## Runtime layers

```text
Plugin
├── Skills: select and guide one user workflow
├── Flight Search Harness: owns the Search Mission lifecycle
├── Product Adapters: translate at the Product Adapter Seam
└── Product Kernels
    ├── Random loop
    ├── One-way
    └── Round trip
```

A Skill is not a solver and the Plugin is not another runtime layer. Installing the Plugin makes the Skills, Harness, and product Modules available together.

## Search Mission lifecycle

1. A Skill converts the user's constraints into a request with one `journey_shape`.
2. The Harness creates a Run and starts the matching Product Kernel.
3. The Product Kernel publishes bounded Work Items through its Product Adapter.
4. The Harness publishes Work Attempts in a time-limited Lease whose file is
   the sole claim authority.
5. A Worker performs Provider research or a deterministic choice and returns immutable Receipts.
6. The Product Kernel admits valid Observations and Offers as Evidence, advances its own state, and either publishes more Work Items or finalizes.
7. The Harness checkpoints the Run after each transition and returns an Outcome.

`draft` and `blocked` Outcomes are resumable. `failed` is terminal. `completed`
means the active Product Kernel's finalization criteria passed. The Outcome
contains a small inline projection plus a reference to the sole full result at
`product/final.json`.

## Module responsibilities

### Flight Search Harness

`flight_search_harness/` owns Run creation, checkpointing, Lease ownership and expiry,
Receipt persistence, shared Worker execution, result projection, and lifecycle
Outcomes. `run.json` is the only lifecycle authority. Mission state is only an
execution cursor, while events and metrics are optional sidecars. The Harness
does not decide whether a route is valid or whether a
product search is complete.

### Product Kernels

Each directory under `product/` owns one journey shape's Session, Evidence admission, hypotheses, deterministic calculations, search rules, and finalization gate. Product Kernels do not import or share each other's Session or solver state.

The three journey shapes intentionally differ:

- Random loop searches airport-only multi-leg loops and enforces its ordered Provider and loop-quality rules.
- One-way search reaches a selected destination through independent flight and surface-route hypotheses.
- Round-trip search compares complete atomic ticket portfolios, including joint, split, nested, open-jaw, mixed, and surface-supported structures.

### Cross-product parity

The one-way and round-trip Product Kernels preserve equivalent observable behavior for their shared contracts without sharing search state or requiring identical implementations. Shared behavior covers regional anchors and gateways; date intent; Provider research policy; Evidence identity; flight and service identity; airport transit; FX, baggage, accommodation, entitlement, budget, duration, overnight, and self-transfer constraints; seller snapshots and equivalent Evidence variants; hypothesis and query tracking; challenger review; and finalization.

One-way search owns a single origin-to-destination movement, forward frontier and residual-suffix search, and one-way ranking. Round-trip search owns the ordered outbound and return movements plus complete ticket portfolios. An atomic joint Offer remains one total and is never divided into directional prices.

A shared-contract change must preserve the observable behavior of both the one-way and round-trip Product Kernels without merging their state or implementations.

### Product Adapter Seam

The Harness sees a deep `ProductKernel.start`/`ProductKernel.advance` Interface. A Product Adapter translates generic Work Items and Receipts to the existing product-specific Session and Evidence contracts. This keeps the lifecycle uniform without flattening product behavior into a shared solver.

### Workers

A Worker executes a Lease but does not decide product completion. The current Implementations are:

- external Worker for a Codex desktop thread with native browser control;
- Codex app-server Worker for structured Codex turns.

Workers return Observations, Offers, or choices as Receipts. A rejected Receipt
becomes a new attempt of the same Work Item rather than a correction node. Only
a Product Kernel can admit a Receipt as Evidence.

### Store

The default Store is `runs/` relative to the directory where the Harness is
launched. A Run keeps its manifest, optional events, Search Mission cursor,
Work Items, Leases, raw Receipts, product Session, and browser
artifacts in one `runs/<run-id>/` tree. The Product Module
owns the only full result. Persisted artifact references are relative to their
owning Run rather than absolute local paths.

The state ownership is deliberately narrow:

```text
run.json                    lifecycle and Outcome
mission/mission.json        revision and execution indexes
mission/pending-effect.json uncommitted Product effect; never Outcome authority
mission/leases/*.json       claim and pending Work Attempts
mission/receipts/raw/*      immutable Worker submissions
product/session.json        Product Session and Evidence
product/final.json          authoritative full result
```

## Repository layout

```text
skills/             installed user workflow entry points
flight_search_harness/     shared Harness lifecycle and contracts
product/            three independent Product Kernel Modules
scripts/            launcher and release packaging commands
docs/               user, architecture, and CLI documentation
```

The detailed runtime contract is in [`flight_search_harness/CONTRACT.md`](../flight_search_harness/CONTRACT.md), and naming rules are in [naming conventions](naming.md).
