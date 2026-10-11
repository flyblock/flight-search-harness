# Naming conventions

These conventions keep authored content, Python identifiers, JSON contracts, CLI commands, and files aligned with the domain language in [`GLOSSARY.md`](../GLOSSARY.md). Existing public contracts may retain an established spelling until an explicit migration changes every producer, consumer, Schema, and validation path together.

## Canonical prose

- Use the exact glossary term when referring to a domain concept: **Flight Search Harness**, **Plugin**, **Skill**, **Product Kernel**, **Product Adapter**, **Search Mission**, **Work Item**, **Lease**, **Receipt**, **Evidence**, **Observation**, **Offer**, **Run**, **Outcome**, **Worker**, and **Store**.
- Capitalize canonical domain terms in prose. Use lowercase only when a word has its ordinary, non-domain meaning.
- Use **Flight Search Harness** on first mention. Use **Harness** only after the referent is clear.
- Use **Plugin** for the portable installation unit, **Harness** for the running system, and **Skill** for an agent workflow entry point. Do not use the three terms interchangeably.
- Prefer a precise noun over an abbreviation or a local synonym. Do not introduce a second name for a glossary concept.
- Use **Module**, **Interface**, **Implementation**, **Seam**, and **Adapter** for architecture discussions. A Module exposes an Interface at a Seam; an Adapter is a concrete role that satisfies an Interface. Do not substitute *component*, *service*, *API*, *wrapper*, *bridge*, or *boundary* when one of these terms is intended.

## Canonical project identities

Use one identity per role. These names are contracts, not interchangeable aliases.

| Role | Canonical identity |
| --- | --- |
| Project and runtime prose | `Flight Search Harness` |
| Repository, Plugin, cache, archive prefix | `flight-search-harness` |
| Python package | `flight_search_harness` |
| Suite manifest | `FLIGHT-SEARCH-HARNESS-MANIFEST.json` |
| One-way Skill | `low-cost-one-way-travel-flight-search` |
| Random-loop Skill | `low-cost-random-loop-flight-search` |
| Round-trip Skill | `low-cost-round-trip-flight-search` |
| Product Module directories | `product/one-way`, `product/random-loop`, `product/round-trip` |
| Product IDs | `one_way`, `random_loop`, `round_trip` |

Do not reintroduce retired project aliases. The ordinary domain word *travel*
remains valid for journeys, requests, edges, windows, and other travel
concepts.

## Journey-shape language

- Use **Random loop**, **One-way**, and **Round trip** as standalone user-facing search-type labels.
- Use **random-loop**, **one-way**, and **round-trip** when the journey shape modifies another noun, such as *one-way journey* or *round-trip Product Kernel*.
- Describe a selected destination as a request constraint, not as part of a product name. Do not prefix the One-way or Round trip label with *fixed*.
- Keep machine identities unchanged: `random_loop`, `one_way`, and `round_trip` in JSON; `product/random-loop`, `product/one-way`, and `product/round-trip` in paths.

## Python

- Use `snake_case` for modules, functions, methods, parameters, and local variables.
- Use `PascalCase` for classes and protocols. Join canonical prose terms without spaces: `ProductKernel`, `ProductAdapter`, `SearchMission`, `WorkItem`, `RunStore`, and `SearchWorker`.
- Use `UPPER_SNAKE_CASE` for constants.
- Name commands as an imperative verb plus a precise object when an operation mutates or advances state, such as `claim_lease` or `submit_receipt`. Name pure queries with a descriptive verb such as `load_run`, `list_work_items`, or `validate_offer`.
- Name predicates so they read as assertions, normally with `is_`, `has_`, `can_`, or `should_`.
- Name an Adapter for the Interface role it satisfies, not for incidental implementation details. Use forms such as `<Product>ProductAdapter` and `<ExecutionMode>Worker`.
- Treat acronyms as words inside `PascalCase`: use `UrlPolicy` and `JsonReceipt`, not `URLPolicy` or `JSONReceipt`. In `snake_case`, use lowercase forms such as `url`, `json`, `http`, and `id`.
- Do not encode release versions or transient implementation details in Python symbol names unless the symbol intentionally supports parallel protocol versions.

## JSON and Schemas

- Use `snake_case` for object keys.
- Use singular nouns for one value and plural nouns for arrays or maps of multiple values.
- Use lower `snake_case` for enumerated string values, including product names, Work Item kinds, and Outcome statuses.
- Use `<owner>_id` for a logical identifier, `<artifact>_ref` for a retrievable artifact reference, and `<content>_hash` for an integrity digest. Do not call a reference an ID.
- Name booleans as assertions that are unambiguous at the use site, such as `coverage_complete` or `requires_user_input`. Avoid vague names such as `enabled`, `valid`, or `done` without a subject.
- Give Schema titles the canonical prose spelling. Keep file and field naming independent of title capitalization.
- Do not silently rename a persisted field. Contract changes require coordinated Schema, producer, consumer, migration, and validation updates.

## CLI

- Use lower `kebab-case` for subcommands and long options: `status`, `--run-id`, and `--receipt-batch`.
- Name mutating commands with an imperative verb and read-only commands with the noun or query they expose.
- Use the JSON spelling for machine values passed through the CLI, including product IDs and Outcome statuses.
- Use angle-bracket placeholders in documentation, such as `<run-id>` and `<request.json>`. Do not use a real user's path or identifier in an example.
- Keep stdout machine-readable when a command advertises JSON output; send explanatory diagnostics to stderr.

## Files and directories

- Use lowercase `snake_case.py` for Python files and packages.
- Use lower `kebab-case.md` for ordinary documentation files. Reserve conventional uppercase names for repository entry points and contracts: `README.md`, `AGENTS.md`, `GLOSSARY.md`, `SKILL.md`, `LICENSE`, and `PRODUCT-MANIFEST.json`.
- Use lower `kebab-case.schema.json` for JSON Schema files and lower `kebab-case.json` for examples.
- Name directories for their owned concept or artifact collection. Avoid generic containers such as `misc`, `common`, or `utils` when a precise owner exists.
- Keep generated Runs, release archives, caches, and planning instances outside tracked product content.

## Statuses

- Use exactly `draft`, `blocked`, `failed`, and `completed` for persisted Outcome statuses.
- `draft` means useful but unfinished work exists.
- `blocked` means progress requires an external action or user decision.
- `failed` means the current attempt ended without a recoverable next action under the active request.
- `completed` means the selected Product Kernel's finalization criteria passed.
- Do not use `success`, `done`, `partial`, or `in_progress` as synonyms for an Outcome status.

## IDs, references, and hashes

- Treat every `*_id` as an opaque stable identity. Never derive business meaning from its text.
- Treat every `*_ref` as a portable locator interpreted by the owning Store. Prefer suite-relative references over absolute filesystem paths.
- Use `*_hash` for content identity or integrity, and include the algorithm in the field name when it is not fixed by the contract, such as `sha256` or `content_sha256`.
- Reuse an ID only for the same logical object. Reuse an idempotency key only for byte-equivalent intent.
- In prose, write **ID** and **reference**. In code and JSON, use the suffixes `_id` and `_ref`.

## Versions

- Use Semantic Versioning (`MAJOR.MINOR.PATCH`) for Plugin, Harness, and product releases.
- Prefix a release with `v` only in human-facing tags or prose, such as `v1.0.0`; store the machine value as `1.0.0`.
- Version persisted protocols and Schemas independently from release versions. Use a protocol version only when readers need it to select compatible behavior.
- Increase the major version for incompatible public contract changes, the minor version for backward-compatible capabilities, and the patch version for backward-compatible fixes.
- Do not copy ephemeral validation results or release-validation timestamps into product version metadata.

## Currency

- Require the request to declare its three-letter `base_currency`; do not infer it from geography, language, or a product default.
- Normalize monetary values with Harness-owned `fx_to_base`, where the Base Currency rate is exactly `1.0`; public requests declare only the Base Currency.
- Name normalized amounts with `_in_base_currency` when the surrounding object does not already make the unit unambiguous.
- Name distance-to-price ratios with `_km_per_base_currency_unit`. Do not encode a concrete currency in a field name.
- Use `XTS` only for synthetic examples. Provider Observations and user requests retain their actual currency codes.

## Acronyms

- In prose, use established uppercase forms: **CLI**, **JSON**, **URL**, **HTTP**, **HTTPS**, **ID**, **SHA-256**, and **FX**.
- Spell out a project-specific phrase on first use unless it is already a glossary term.
- Do not invent abbreviations for domain terms. Use **Search Mission**, not *SM*; use **Product Kernel**, not *PK*.
- Preserve an external Provider's official brand spelling.
