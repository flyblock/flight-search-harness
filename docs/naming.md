# Naming conventions

Use [the glossary](../GLOSSARY.md) for domain meanings. This file defines spelling and naming conventions.

## Prose

- Use a glossary term consistently when referring to its domain concept. Define a new concept before introducing a competing name.
- Use English for repository-authored prose. User input and Provider data may retain their original language.
- Preserve official external names and established acronyms such as CLI, JSON, URL, HTTP, ID, SHA-256, and FX.
- A Module exposes an Interface; its Implementation supplies the behavior, and an Adapter satisfies an Interface at a Seam.

## Code and commands

- Use `snake_case` for Python modules, functions, parameters, and local variables.
- Use `PascalCase` for classes and protocols and `UPPER_SNAKE_CASE` for constants.
- Treat acronyms as words in Python names, such as `UrlPolicy` and `url_policy`.
- Use verbs for operations and clear predicates such as `is_`, `has_`, or `can_` for booleans.
- Use lower `kebab-case` for CLI commands and long options.
- Keep machine-readable command output separate from explanatory diagnostics.

## Data and files

- Use `snake_case` for JSON keys and lower `snake_case` for enumeration values.
- Use `*_id` for logical identity, `*_ref` for a locator, and `*_hash` for a content digest. Include the algorithm when the surrounding contract does not fix it.
- Treat IDs as opaque; reuse an ID only for the same logical object.
- Name quantities with explicit units and monetary values with an explicit currency context.
- Use lowercase `snake_case.py` for Python files and lower `kebab-case` for ordinary documentation and JSON filenames.
- Keep conventional entry-point names such as `AGENTS.md`, `GLOSSARY.md`, `SKILL.md`, and `PRODUCT-MANIFEST.json`.
- Use descriptive directories with a clear owner. Keep generated Runs, caches, and historical material outside authored product guidance.
- Use generic placeholders in examples instead of a real user's identifiers or paths.

## Contract changes

- Change a persisted name together with its Schema, producers, consumers, and compatibility handling.
- Version releases and persisted contracts according to their own compatibility requirements.
- Keep transient verification results and dates out of release identity metadata.
