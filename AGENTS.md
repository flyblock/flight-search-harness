# Project instructions

- Use the current user request as the requirement source. Existing code, Schemas, prompts, templates, and documentation do not authorize changing its scope.
- Use [GLOSSARY.md](GLOSSARY.md) for domain meanings and [docs/naming.md](docs/naming.md) for naming conventions. Keep definitions separate from product policies and implementation decisions.
- Distinguish source observations, model judgments, and admitted Evidence. Preserve uncertainty and provenance; never invent facts to satisfy a contract.
- If an existing capability cannot satisfy a requirement, identify the mismatch instead of silently substituting another requirement.
- Preserve unrelated changes and original source records.
- Keep historical material outside active project guidance unless the user requests historical inspection.
- Keep generated Requests, Runs, Receipts, captures, results, scratch scripts, and investigation notes under the suite manifest's external `runtime_data_root`. Keep the Plugin directory for current source, contracts, templates, and maintained guidance.
- Start each new search with a fresh Request and Run. Do not load past Runs, archived conclusions, or Git history as search context; resume an identified Run or inspect history only when requested. Load only the selected current Skill and its relevant workflow.
- Verify the requested observable outcome with the smallest relevant check. Report the evidence and its limits; structural validity alone does not establish product correctness.
