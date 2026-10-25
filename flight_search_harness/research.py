"""A single durable ledger for agent-directed travel research.

The agent chooses queries. This Module records facts, deduplicates attempts and
projects comparisons; it does not schedule provider work or declare an optimum.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .findings import normalize_observation
from .search_request import normalize_request


SESSION_VERSION = "7.0"
RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,95}\Z")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: str | Path) -> Any:
    def invalid_constant(value: str):
        raise ValueError(f"JSON contains a non-finite number: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=invalid_constant)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:24]


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    """Reuse the existing Store's atomic replace algorithm without its lifecycle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ResearchStore:
    """Own one session.json; source observations are never rewritten by views."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def directory(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must be a safe lowercase logical identifier")
        return self.root / run_id

    def path(self, run_id: str) -> Path:
        return self.directory(run_id) / "product" / "session.json"

    @contextmanager
    def locked(self, run_id: str):
        """OS-released advisory lock; no durable claim/Lease recovery protocol."""
        directory = self.directory(run_id)
        if not directory.is_dir():
            raise ValueError(f"Run does not exist: {run_id}")
        with (directory / ".write-lock").open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("Run is being written; retry the same operation") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def load(self, run_id: str) -> dict[str, Any]:
        state = read_json(self.path(run_id))
        if not isinstance(state, dict) or state.get("session_version") != SESSION_VERSION:
            raise ValueError("Legacy Run is read-only. Use import-legacy with an explicit Request to continue it in a new Run.")
        if state.get("run_id") != run_id:
            raise ValueError("Run identity does not match its directory")
        return state

    def save(self, state: dict[str, Any]) -> None:
        from .exploration import settle_challenges
        settle_challenges(state, now())
        state["updated_at"] = now()
        write_atomic(self.path(state["run_id"]), state)

    def start(self, request: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
        raw_request = copy.deepcopy(request)
        request = normalize_request(request)
        run_id = run_id or f"run-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        directory = self.directory(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        instant = now()
        state = {
            "session_version": SESSION_VERSION, "run_id": run_id,
            "created_at": instant, "updated_at": instant,
            "requests": [{"revision": 1, "request": request, "raw_request": raw_request, "recorded_at": instant}],
            "observations": [], "queries": [], "combinations": [], "assessments": [],
            "research": {"status": "active", "reason": None},
        }
        self.save(state)
        return self.summary(state)

    def revise(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        raw_request = copy.deepcopy(request)
        request = normalize_request(request)
        with self.locked(run_id):
            state = self.load(run_id)
            if request["journey_shape"] != state["requests"][-1]["request"]["journey_shape"]:
                raise ValueError("A different journey shape requires a new Run or explicit import")
            if state["requests"][-1]["request"] != request:
                state["requests"].append({"revision": len(state["requests"]) + 1, "request": request, "raw_request": raw_request, "recorded_at": now()})
                self.save(state)
            return self.summary(state)

    @staticmethod
    def _query_key(query: dict[str, Any], intent: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if not isinstance(query, dict) or not isinstance(query.get("source"), dict):
            raise ValueError("query.source must identify the Provider")
        provider = query["source"].get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("query.source.provider is required")
        if not isinstance(query.get("scope"), dict) or not query["scope"]:
            raise ValueError("query.scope must describe actual airports, dates and query conditions")
        page_url = query["source"].get("page_url")
        try:
            host = (urlsplit(page_url).hostname or "").casefold() if isinstance(page_url, str) else ""
        except ValueError:
            host = ""
        context = {key: intent.get(key) for key in ("adult_count", "baggage_policy")}
        source_identity = {
            "provider": provider.strip().casefold(),
            "host": host,
        }
        return fingerprint({"source": source_identity, "scope": query["scope"], "traveler_context": context}), context

    @staticmethod
    def _query_limits(state: dict[str, Any]) -> list[str]:
        limits = state["requests"][-1]["request"]["intent"].get("search_limits", {})
        issues = []
        if state["research"]["status"] != "active":
            issues.append("Research is stopped; resume the Run before starting another query")
        if limits.get("max_queries") is not None and len(state["queries"]) >= limits["max_queries"]:
            issues.append("The user-specified query limit has been reached")
        if limits.get("max_elapsed_minutes") is not None:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(state["created_at"])).total_seconds() / 60
            if elapsed >= limits["max_elapsed_minutes"]:
                issues.append("The user-specified elapsed-time limit has been reached")
        return issues

    def _register_query(self, state: dict[str, Any], query: dict[str, Any],
                        retry_reason: str | None = None, *, observed: bool = False
                        ) -> tuple[dict[str, Any], bool, list[str]]:
        if retry_reason is not None and (not isinstance(retry_reason, str) or not retry_reason.strip()):
            raise ValueError("retry_reason must explain refresh or another attempt")
        key, context = self._query_key(query, state["requests"][-1]["request"]["intent"])
        previous = [item for item in state["queries"] if item["query_key"] == key]
        if previous and not retry_reason:
            return previous[-1], False, []
        issues = self._query_limits(state)
        if issues and not observed:
            raise ValueError("; ".join(issues) + "; saved findings remain deliverable")
        instant = now()
        item = {
            "query_id": "query-" + uuid.uuid4().hex[:16], "query_key": key,
            "request_revision": state["requests"][-1]["revision"], "query": copy.deepcopy(query),
            "traveler_context": context, "status": "observed" if observed else "running",
            "started_at": None if observed else instant, "registered_at": instant,
            "observation_ids": [], "retry_reason": retry_reason,
        }
        if issues:
            item["registration_warnings"] = list(issues)
        purpose = query.get("purpose", "lookup")
        if not isinstance(purpose, str) or purpose not in {"lookup", "challenge", "airport_discovery"}:
            issues.append("Unrecognized query purpose; saved work cannot establish a challenge or airport discovery.")
        elif purpose in {"challenge", "airport_discovery"} and (not isinstance(query.get("reason"), str) or not query["reason"].strip()):
            issues.append("A challenge or airport-discovery query needs a reason describing its expected value.")
        elif purpose == "challenge":
            from .exploration import attach_challenge
            attach_challenge(state, item, instant)
        if issues:
            item["registration_warnings"] = list(issues)
        state["queries"].append(item)
        return item, True, issues

    def query(self, run_id: str, query: dict[str, Any], retry_reason: str | None = None) -> dict[str, Any]:
        with self.locked(run_id):
            state = self.load(run_id)
            item, created, _ = self._register_query(state, query, retry_reason)
            if created:
                self.save(state)
            from .exploration import research_feedback
            return {"kind": "query_ready" if created else "query_exists", "run_id": run_id,
                    "query": copy.deepcopy(item), "repeat_requires_reason": not created,
                    "research_feedback": research_feedback(state, evaluation_at=now())}

    def record(self, run_id: str, submission: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(submission, dict):
            raise ValueError("record input must be an object")
        observations = submission.get("observations", [])
        if not isinstance(observations, list):
            raise ValueError("observations must be an array")
        if not observations and not submission.get("query_result"):
            raise ValueError("record requires observations or a query_result")
        with self.locked(run_id):
            state = self.load(run_id)
            query_id = submission.get("query_id")
            query = next((q for q in state["queries"] if q["query_id"] == query_id), None)
            result = submission.get("query_result")
            query_errors = []
            query_warnings = []
            if "query" in submission:
                if query_id is not None:
                    query_errors.append("Supply query or query_id, not both; observations saved without association")
                    query = None
                else:
                    try:
                        query, _, query_warnings = self._register_query(
                            state, submission["query"], submission.get("retry_reason"), observed=True)
                        query_id = query["query_id"]
                    except (ValueError, TypeError) as exc:
                        query_errors.append(str(exc) + "; observations saved without association")
            if query_id is not None and query is None:
                query_errors.append("query_id does not belong to this Run; observations saved without association")
            if result is not None:
                if query is None or not isinstance(result, dict):
                    query_errors.append("query_result requires an object and this Run's query_id")
                elif not isinstance(result.get("status"), str) or result["status"] not in {"completed", "no_result", "unsupported", "failed"}:
                    query_errors.append("query_result has an unsupported status")
                elif not str(result.get("reason", "")).strip():
                    query_errors.append("query_result.reason must describe actual coverage or failure")
                elif result["status"] == "no_result" and not observations and not query["observation_ids"]:
                    query_errors.append("no_result requires a saved source observation; technical failure is unsupported or failed")
            saved, duplicates = [], []
            existing = {item["observation_id"]: item for item in state["observations"]}
            captures = submission.get("captures", [])
            captures = captures if isinstance(captures, list) else []
            for raw in observations:
                context = None
                bound = copy.deepcopy(raw)
                if isinstance(raw, dict) and raw.get("capture_id"):
                    matches = [c for c in captures if isinstance(c, dict) and c.get("capture_id") == raw["capture_id"]]
                    if len(matches) == 1:
                        context = copy.deepcopy(matches[0])
                        # Bind only an explicit Capture ID; never guess by list position.
                        bound.setdefault("source", context.get("source"))
                        if not bound.get("observed_at") and not bound.get("captured_at"):
                            bound["captured_at"] = context.get("captured_at")
                        if "provider_quote_time" not in bound and "provider_quote_time" in context:
                            bound["provider_quote_time"] = context["provider_quote_time"]
                normalized = normalize_observation(bound, received_at=now())
                normalized["raw"] = copy.deepcopy(raw)
                if context is not None:
                    normalized["source_context"] = context
                identifier = normalized["observation_id"]
                old = existing.get(identifier)
                if old is not None:
                    if canonical(old["raw"]) == canonical(raw) and old.get("source_context") == context:
                        duplicates.append(identifier)
                        continue
                    # Preserve the new source record instead of overwriting an old identity.
                    normalized["reported_observation_id"] = identifier
                    identifier = f"{identifier}-{fingerprint({'raw': raw, 'source_context': context})}"
                    normalized["observation_id"] = identifier
                    if identifier in existing:
                        duplicates.append(identifier)
                        continue
                normalized["request_revision"] = state["requests"][-1]["revision"]
                normalized["query_id"] = query_id if query is not None else None
                state["observations"].append(normalized)
                existing[identifier] = normalized
                saved.append(identifier)
            if query is not None:
                query["observation_ids"] = list(dict.fromkeys([*query["observation_ids"], *saved, *duplicates]))
                for identifier in [*saved, *duplicates]:
                    if existing[identifier].get("query_id") is None:
                        existing[identifier]["query_id"] = query["query_id"]
                if result is not None and not query_errors:
                    query.update(status=result["status"], reason=result["reason"], finished_at=now())
            from .exploration import research_feedback
            self.save(state)
            return {
                "kind": "findings_recorded", "run_id": run_id, "saved": saved, "duplicates": duplicates,
                "admissions": [{"observation_id": identifier, **existing[identifier]["admission"]} for identifier in [*saved, *duplicates]],
                "research": state["research"], "query_errors": query_errors,
                "query": copy.deepcopy(query), "query_warnings": query_warnings,
                "unlinked_observation_ids": [identifier for identifier in [*saved, *duplicates] if existing[identifier].get("query_id") is None],
                "research_feedback": research_feedback(state, evaluation_at=now()),
            }

    def combine(self, run_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        from .comparison import candidate_id
        refs = spec.get("offer_refs")
        if not isinstance(refs, list) or not refs or not str(spec.get("reason", "")).strip():
            raise ValueError("combine requires nonempty offer_refs and an exploration reason")
        if any(not isinstance(ref, dict) or set(ref) != {"observation_id", "offer_id"} for ref in refs):
            raise ValueError("Each offer_ref requires exactly observation_id and offer_id")
        if len({canonical(ref) for ref in refs}) != len(refs):
            raise ValueError("The same purchase cannot be included twice")
        with self.locked(run_id):
            state = self.load(run_id)
            known = {(o["observation_id"], offer["offer_id"]) for o in state["observations"] for offer in o["offers"]}
            if any((ref["observation_id"], ref["offer_id"]) not in known for ref in refs):
                raise ValueError("Combination references an unknown Offer")
            identifier = candidate_id(refs)
            if not any(c["candidate_id"] == identifier for c in state["combinations"]):
                state["combinations"].append({"candidate_id": identifier, "offer_refs": copy.deepcopy(refs), "reason": spec["reason"], "created_at": now()})
                self.save(state)
            from .exploration import research_feedback
            return {"kind": "combination_recorded", "run_id": run_id, "candidate_id": identifier,
                    "research_feedback": research_feedback(state, evaluation_at=now())}

    def assess(self, run_id: str, assessment: dict[str, Any]) -> dict[str, Any]:
        from .comparison import evaluate_session
        with self.locked(run_id):
            state = self.load(run_id)
            revision = state["requests"][-1]["revision"]
            if assessment.get("request_revision") != revision:
                raise ValueError("Assessment must name the current Request revision")
            if assessment.get("status") not in {"satisfied", "violated", "unknown"}:
                raise ValueError("Assessment status must be satisfied, violated or unknown")
            if not str(assessment.get("reason", "")).strip():
                raise ValueError("Assessment requires an evidence-based reason")
            requirements = state["requests"][-1]["request"]["intent"].get("additional_requirements", [])
            if assessment.get("requirement_id") not in {r["requirement_id"] for r in requirements}:
                raise ValueError("Assessments apply only to declared additional_requirements, never to arithmetic checks")
            projection = evaluate_session(state, evaluation_at=now())
            candidates = [*projection.get("entries", []), *projection.get("alternatives", [])]
            known_candidates = {c["candidate_id"] for c in candidates}
            known_candidates.update(v["candidate_id"] for c in candidates for v in c.get("seller_variants", []))
            if assessment.get("candidate_id") not in known_candidates:
                raise ValueError("Assessment references an unknown Candidate")
            refs = assessment.get("evidence_refs", [])
            known = {o["observation_id"] for o in state["observations"]}
            if not isinstance(refs, list) or any(ref not in known for ref in refs):
                raise ValueError("evidence_refs must reference saved observations")
            if assessment["status"] != "unknown" and not refs:
                raise ValueError("A conclusive model assessment requires saved source observations")
            item = {**copy.deepcopy(assessment), "judged_by": "ai", "recorded_at": now()}
            state["assessments"].append(item)
            self.save(state)
            from .exploration import research_feedback
            return {"kind": "assessment_recorded", "run_id": run_id, "assessment": item,
                    "research_feedback": research_feedback(state, evaluation_at=now())}

    def set_research(self, run_id: str, status: str, reason: str | None = None) -> dict[str, Any]:
        if status == "stopped" and not (reason or "").strip():
            raise ValueError("Stopping requires the actual reason; it does not establish exhaustive coverage")
        with self.locked(run_id):
            state = self.load(run_id)
            state["research"] = {"status": status, "reason": reason, "changed_at": now()}
            self.save(state)
            return self.summary(state)

    def summary(self, state: dict[str, Any]) -> dict[str, Any]:
        from .exploration import research_feedback
        feedback = research_feedback(state, evaluation_at=now())
        return {
            "kind": "research_state", "run_id": state["run_id"], "request_revision": state["requests"][-1]["revision"],
            "request": state["requests"][-1]["request"], "research": state["research"],
            "observation_count": len(state["observations"]), "query_count": len(state["queries"]),
            "queries": state["queries"],
            "unlinked_observation_ids": [o["observation_id"] for o in state["observations"] if not o.get("query_id")],
            "research_feedback": feedback,
            "next_action": feedback["next_actions"][0] if feedback["next_actions"] else "Inspect the saved ranking and remaining search scope.",
        }

    def deliver(self, run_id: str, view: str | None = None, limit: int | None = None) -> dict[str, Any]:
        from .comparison import evaluate_session
        state = self.load(run_id)
        output = state["requests"][-1]["request"]["intent"].get("output", {})
        view = view or output.get("view", "ranked")
        limit = limit if limit is not None else output.get("limit")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError("limit must be a positive integer")
        result = evaluate_session(state, evaluation_at=now())
        from .exploration import research_feedback
        result["research_feedback"] = research_feedback(state, result, evaluation_at=now())
        result["recommendations"] = {
            "primary_candidate_id": result["entries"][0]["candidate_id"] if result["entries"] else None,
            "runner_up_candidate_ids": [e["candidate_id"] for e in result["entries"][1:]],
            "rule": "Present useful existing eligible alternatives alongside the current best. Leads and repeated snapshots are not runners-up; do not research solely to fill a quota.",
        }
        result.update(run_id=run_id, research=state["research"], claim_scope="observed_sources_and_queries", queries=state["queries"])
        result["unlinked_observation_ids"] = [o["observation_id"] for o in state["observations"] if not o.get("query_id")]
        if view == "full":
            return {"kind": "findings_view", "result": result, "session": state}
        if view == "matrix":
            # A matrix is a view of observed cells, never inferred coverage.
            cells: dict[str, list[dict[str, Any]]] = {}
            for entry in [*result.get("entries", []), *result.get("alternatives", [])]:
                dates = entry.get("dates", {})
                key = canonical(dates) if dates else entry["candidate_id"]
                cells.setdefault(key, []).append(entry)
            result["cells"] = [{"date_key": key, "candidates": entries} for key, entries in cells.items()]
            result["coverage_note"] = "Only saved observations are represented; absent cells are unsearched, not no_result. Signals retain their original date scope."
        elif view != "ranked":
            raise ValueError("view must be ranked, matrix or full")
        result["total_ranked"] = len(result.get("entries", []))
        if limit is not None:
            result["entries"] = result.get("entries", [])[:limit]
        return {"kind": "findings_view", "result": result}

    def import_legacy(self, source_run: str | Path, request: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
        """Explicitly copy source observations from one identified Run, never its conclusions."""
        source = Path(source_run).resolve()
        old = read_json(source / "product" / "session.json")
        observations = []
        submissions = []
        if old.get("session_version") == SESSION_VERSION:
            observations = [o["raw"] for o in old.get("observations", [])]
        else:
            # Prefer immutable raw submitted facts, including rejected submissions.
            for path in sorted((source / "mission" / "receipts" / "raw").glob("*.json")):
                receipt = read_json(path)
                payload = receipt.get("payload") or {}
                observed = (payload.get("product_payload") or {}).get("observations", [])
                observations.extend(observed)
                if observed:
                    submissions.append({"observations": observed, "captures": (receipt.get("evidence_context") or {}).get("captures", [])})
            if not observations:
                for bundle in old.get("evidence_bundles", []):
                    observations.extend(bundle.get("observations", []))
        created = self.start(request, run_id)
        target_id = created["run_id"]
        if submissions:
            imported = [self.record(target_id, submission) for submission in submissions]
        else:
            imported = self.record(target_id, {"observations": observations}) if observations else {"saved": []}
        with self.locked(target_id):
            state = self.load(target_id)
            state["imported_from"] = {
                "source_run": str(source), "imported_at": now(), "scope": "source_observations_only",
                "source_record_refs": [str(p) for p in sorted((source / "mission" / "receipts" / "raw").glob("*.json"))],
            }
            self.save(state)
        return {"kind": "legacy_imported", "run_id": target_id, "source_run": str(source), "recording": imported}
