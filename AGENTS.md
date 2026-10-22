# Repository instructions

- Read [`GLOSSARY.md`](GLOSSARY.md) before changing domain language. Use its canonical terms consistently and do not add synonyms casually.
- Follow [`docs/naming.md`](docs/naming.md) for prose, Python, JSON, CLI, file, status, identifier, reference, version, and acronym names.
- Treat the Flight Search Harness as the runtime product, the Plugin as its portable distribution, and Skills as agent workflow entry points.
- Preserve each Product Kernel as an independent Module behind its Product Adapter Seam. Do not merge product Session, Evidence, solver, or finalization state.
- Keep repository-authored content English-canonical. User input and Provider data may use any language.
- For an actual travel search, load only the selected travel Skill, its compact request template, the shared Mission runtime, and the matching product workflow. Read the full product Schema only for uncommon optional constraints or to resolve a validation failure.
- Preserve unrelated changes and run the smallest relevant verification before handing work off.

## Verification and tests

- Treat tests as evidence for stable externally observable behavior, confirmed regressions, and high-risk invariants.
- Define completion by the requested working outcome. Passing tests provide supporting evidence for that outcome.
- Use the smallest existing verification that can expose the relevant failure.
- Add a test when it protects a named Product requirement, reproduces a confirmed defect, or secures a stable public Interface.
- State the behavior protected by each new test and the concrete defect that would make it fail.
- Verify cross-Module behavior through a vertical path from public Request to Delivery.
- Use unit tests for isolated deterministic rules whose correctness materially affects the Product outcome.
- During architectural replacement, derive tests from the target Interface and current Product requirements. Preserve a legacy test when it still represents a valid requirement.
- Remove or replace tests centered on private state shapes, internal task counts, hard-coded presentation text, mock or fixture echoes, duplicated Schema validation, or superseded implementation details.
- Let test volume follow independent risk coverage. Test count and coverage percentage carry no completion value by themselves.
- Treat a green test suite as one source of evidence rather than proof that the vertical Product path works.
