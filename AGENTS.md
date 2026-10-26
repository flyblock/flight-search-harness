# Project instructions

- Use the current user request as the requirement source. Existing code, Schemas, prompts, templates, and documentation do not authorize changing its scope.
- Use [GLOSSARY.md](GLOSSARY.md) for domain meanings and [docs/naming.md](docs/naming.md) for naming conventions. Keep definitions separate from product policies and implementation decisions.
- Distinguish source observations, model judgments, and code checks. Preserve uncertainty and provenance; never invent facts to satisfy a contract.
- Keep the harness generic: no special cases for a provider, website, airport, trip, AI client or model. Hints guide the agent; they never reject a record or block delivery, and metadata problems never discard an observed price.
- If an existing capability cannot satisfy a requirement, identify the mismatch instead of silently substituting another requirement.
- Preserve unrelated changes and original source records.
- Keep historical material outside active project guidance unless the user requests historical inspection.
- Keep generated Requests, Runs, captures, results, scratch scripts, and investigation notes under the suite manifest's external `runtime_data_root`. Keep the Plugin directory for current source, contracts, templates, and maintained guidance.
- Start each new search with a fresh Request and Run. Do not load past Runs, archived conclusions, or Git history as search context; resume an identified Run or inspect history only when requested. Load only the selected current Skill and its relevant workflow.
- Verify the requested observable outcome with the smallest relevant check. Report the evidence and its limits; structural validity alone does not establish product correctness. Recorded Runs make good replay checks: rankings and hints should change only where intended.

## Releases

- Work lands on `main`; no other long-lived branches.
- Every commit gets the next plain numeric version (feature: minor, fix or docs: patch) in all version fields together: `plugin.json`, `FLIGHT-SEARCH-HARNESS-MANIFEST.json`, `flight_search_harness/__init__.py`, and `HARNESS_VERSION` in the three Skill resolvers. Tag it `v<version>`.
- Push the tag with the commit: standalone Skills download the source archive of their exact tag, so an unpushed tag breaks them.
- `python -B scripts/package_flight_search_harness.py validate` must pass before a release.
