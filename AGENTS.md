# Repository instructions

- Read [`GLOSSARY.md`](GLOSSARY.md) before changing domain language. Use its canonical terms consistently and do not add synonyms casually.
- Follow [`docs/naming.md`](docs/naming.md) for prose, Python, JSON, CLI, file, status, identifier, reference, version, and acronym names.
- Treat the Flight Search Harness as the runtime product, the Plugin as its portable distribution, and Skills as agent workflow entry points.
- Preserve each Product Kernel as an independent Module behind its Product Adapter Seam. Do not merge product Session, Evidence, solver, or finalization state.
- Keep repository-authored content English-canonical. User input and Provider data may use any language.
- For an actual travel search, load only the selected travel Skill, its compact request template, the shared Mission runtime, and the matching product workflow. Read the full product Schema only for uncommon optional constraints or to resolve a validation failure.
- Preserve unrelated changes and run the smallest relevant verification before handing work off.
