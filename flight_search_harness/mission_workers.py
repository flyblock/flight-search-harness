"""SearchWorker adapters for external desktop work and app-server execution."""

from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .contracts import BrowserAdapter, HarnessProtocolError
from .research_batch import compile_batches
from .research_cache import ResearchCache
from .run_store import read_json
from .worker_projection import canonical_bytes, digest, project_work


def _rebind_cached_payload(
    payload: dict[str, Any],
    work: dict[str, Any],
    *,
    operation_id: str | None = None,
) -> dict[str, Any]:
    rebound = copy.deepcopy(payload)
    binding = digest({"operation_id": operation_id})[:20] if operation_id else None
    if binding and rebound.get("kind") == "observation_bundle":
        rebound["bundle_id"] = f"cache-bundle-{binding}"
    product_payload = rebound.get("product_payload")
    if isinstance(product_payload, dict):
        for key in ("bundle_id", "task_id", "request_id", "query_id"):
            product_payload.pop(key, None)
        if binding:
            product_payload["bundle_id"] = f"cache-bundle-{binding}"
    if work.get("product_id") != "random_loop":
        return rebound
    request = work.get("brief", {}).get("request", {})
    if not isinstance(request, dict) or not request:
        request = work.get("query", {})
    if not isinstance(request, dict):
        return rebound
    product_payload = rebound.get("product_payload")
    groups = (
        product_payload.get("search_results")
        if isinstance(product_payload, dict)
        else None
    )
    if not isinstance(groups, list):
        return rebound
    for group in groups:
        if not isinstance(group, dict):
            continue
        group["request_id"] = request.get("request_id")
        group["parent_id"] = request.get("parent_id")
        if binding:
            group["query_id"] = f"cache-query-{binding}"
        else:
            group.pop("query_id", None)
        acquisition = group.get("acquisition")
        if not isinstance(acquisition, dict):
            acquisition = {}
        group["acquisition"] = {
            **copy.deepcopy(acquisition),
            "method": "cache",
            "reused_within_freshness_ttl": True,
        }
    return rebound


class BrowserMissionWorker:
    """Run Mission items through any existing BrowserAdapter in one worker lease."""

    def __init__(
        self, browser: BrowserAdapter, *, worker_instance_id: str | None = None
    ):
        self.browser = browser
        self.worker_instance_id = (
            worker_instance_id or f"{browser.adapter_id}:{uuid.uuid4()}"
        )
        self._browser_state: dict[str, Any] = {}

    def run(
        self,
        lease: dict[str, Any],
        checkpoint: Callable[[dict[str, Any]], dict[str, Any]],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        current = copy.deepcopy(lease)
        self._browser_state = {}
        for _ in range(512):
            receipts = []
            metrics = _empty_worker_metrics()
            metrics["worker_calls"] = 1
            terminal_exit = None
            for group in compile_batches(list(current.get("items", []))):
                execution_context = {**copy.deepcopy(context), "lease_id": current["lease_id"]}
                results = self._perform_group(group, execution_context)
                if len(group) > 1 and results.get("provider_queries", 0):
                    metrics["coalesced_item_count"] += len(group)
                metrics["provider_query_count"] += int(results.get("provider_queries", 0))
                for item in group:
                    entry = results["items"][item["item_id"]]
                    payload = entry["payload"]
                    _merge_worker_metrics(metrics, item["item_id"], entry["metrics"])
                    if payload.get("kind") in {"blocked", "failed", "draft"}:
                        terminal_exit = payload
                        continue
                    receipts.append(
                        {
                            "receipt_version": "2.0",
                            "item_id": item["item_id"],
                            "attempt_number": int(item["attempt_number"]),
                            "status": "complete",
                            "payload": payload,
                        }
                    )
            if terminal_exit is not None and not receipts:
                return terminal_exit
            if not receipts:
                raise HarnessProtocolError("Mission worker produced no receipts")
            next_step = checkpoint(
                {
                    "lease_id": current["lease_id"],
                    "mission_revision": current["mission_revision"],
                    **_claim_fields(current, self.worker_instance_id),
                    "receipts": receipts,
                    "metrics": metrics,
                }
            )
            if terminal_exit is not None:
                return terminal_exit
            if next_step.get("kind") == "lease":
                current = copy.deepcopy(next_step["lease"])
                continue
            return next_step
        raise HarnessProtocolError("Mission worker exceeded lease continuation limit")

    def _perform_group(
        self, items: list[dict[str, Any]], context: dict[str, Any]
    ) -> dict[str, Any]:
        entries: dict[str, dict[str, Any]] = {}
        pending = []
        cache = ResearchCache(context["cache_root"]) if context.get("cache_root") else None
        for item in items:
            work = item.get("payload", {}).get("work", {})
            execution = work.get("execution", {}) if isinstance(work, dict) else {}
            cached = None
            if cache and isinstance(execution, dict):
                scope = execution.get("cache_scope")
                fingerprint = execution.get("query_fingerprint")
                if isinstance(scope, str) and isinstance(fingerprint, str):
                    product_scope = f"{scope}:{work.get('product_id')}"
                    cached = cache.get(product_scope, fingerprint)
            if cached is not None:
                cached = _rebind_cached_payload(
                    cached,
                    work,
                    operation_id=_operation_id(item, context),
                )
                entries[item["item_id"]] = {
                    "payload": cached,
                    "metrics": {
                        "cache_hit_count": 1,
                        "work_payload_bytes": len(canonical_bytes(work)),
                        "receipt_payload_bytes": len(canonical_bytes(cached)),
                    },
                }
            else:
                pending.append(item)
        provider_queries = 0
        execute_batch = getattr(self.browser, "execute_batch", None)
        if len(pending) > 1 and callable(execute_batch):
            started = time.perf_counter()
            requests = []
            for item in pending:
                payload = item.get("payload", {})
                requests.append(
                    {
                        "item_id": item["item_id"],
                        "work": copy.deepcopy(payload.get("work")),
                        "action_id": _operation_id(item, context),
                    }
                )
            batch_result = execute_batch(requests, self._browser_context(context))
            duration = int((time.perf_counter() - started) * 1000)
            if batch_result.get("kind") in {"blocked", "failed", "draft"}:
                self._capture_browser_state(batch_result)
                provider_queries = 1
                for item in pending:
                    entries[item["item_id"]] = {
                        "payload": copy.deepcopy(batch_result),
                        "metrics": _timing_metrics(
                            started,
                            len(canonical_bytes(item.get("payload", {}).get("work", {}))),
                            batch_result,
                            _pop_usage(batch_result),
                        ),
                    }
                return {"items": entries, "provider_queries": provider_queries}
            attempts = batch_result.get("attempts_by_item") if isinstance(batch_result, dict) else None
            if not isinstance(attempts, dict):
                raise HarnessProtocolError("batch Browser Adapter must return attempts_by_item")
            self._capture_browser_state(batch_result)
            batch_usage = _pop_usage(batch_result)
            provider_queries = 1
            for item_index, item in enumerate(pending):
                payload = attempts.get(item["item_id"])
                if not isinstance(payload, dict):
                    raise HarnessProtocolError(
                        f"batch Browser Adapter omitted item {item['item_id']}"
                    )
                usage = _pop_usage(payload)
                entries[item["item_id"]] = {
                    "payload": payload,
                    "metrics": {
                        "worker_duration_ms": duration,
                        "work_payload_bytes": len(canonical_bytes(item.get("payload", {}).get("work", {}))),
                        "receipt_payload_bytes": len(canonical_bytes(payload)),
                        **usage,
                        **(batch_usage if item_index == 0 else {}),
                    },
                }
        else:
            for item in pending:
                payload, item_metrics, queried = self._perform_item_with_metrics(item, context, cache)
                provider_queries += queried
                entries[item["item_id"]] = {
                    "payload": payload,
                    "metrics": item_metrics,
                }
        return {"items": entries, "provider_queries": provider_queries}

    def _perform_item_with_metrics(
        self,
        item: dict[str, Any],
        context: dict[str, Any],
        cache: ResearchCache | None,
    ) -> tuple[dict[str, Any], dict[str, Any], int]:
        payload = item.get("payload", {})
        browser_context = self._browser_context(context)
        started = time.perf_counter()
        work_bytes = len(canonical_bytes(payload))
        if item.get("kind") == "choice":
            control_summary = copy.deepcopy(payload.get("control_summary", {}))
            control_summary["actions"] = copy.deepcopy(payload.get("actions", []))
            control_summary["investigation_actions"] = [
                copy.deepcopy(action)
                for action in control_summary["actions"]
                if action.get("kind") == "investigate"
            ]
            control_summary["disposition_actions"] = [
                copy.deepcopy(action)
                for action in control_summary["actions"]
                if action.get("kind") in {"dispose", "dispose_inactive"}
            ]
            choice = self.browser.choose(control_summary, browser_context)
            self._capture_browser_state(choice)
            if choice.get("kind") in {"blocked", "failed", "draft"}:
                return choice, _timing_metrics(started, work_bytes, choice), 0
            result = {
                "action_key": choice.get("action_key"),
                "reason": str(choice.get("reason") or "Mission worker selected the action."),
            }
            return result, _timing_metrics(started, work_bytes, result, _pop_usage(choice)), 0
        if item.get("kind") in {"browser_research", "semantic_judgment"}:
            work = payload.get("work")
            action = payload.get("action")
            if not isinstance(work, dict) or not isinstance(action, dict):
                raise HarnessProtocolError("research Mission item is incomplete")
            execution_work = copy.deepcopy(work)
            if item.get("last_error") is not None:
                execution_work["correction_context"] = {
                    "attempt_number": item.get("attempt_number"),
                    "last_error": str(item.get("last_error")),
                }
            operation_id = _operation_id(item, context)
            result = self.browser.execute(
                execution_work, operation_id, browser_context
            )
            if result.get("kind") == "needs_full_context":
                full = self._load_full_item(item, context)
                full_payload = full.get("payload", {})
                result = self.browser.execute(
                    copy.deepcopy(full_payload.get("work")),
                    operation_id,
                    browser_context,
                )
            self._capture_browser_state(result)
            usage = _pop_usage(result)
            return result, _timing_metrics(started, work_bytes, result, usage), 1
        raise HarnessProtocolError(f"unsupported Mission item kind: {item.get('kind')}")

    def _browser_context(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            **copy.deepcopy(context),
            "browser_state": copy.deepcopy(self._browser_state),
        }

    @staticmethod
    def _load_full_item(item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        reference = item.get("full_item_ref")
        run_directory = context.get("run_directory")
        if not isinstance(reference, str) or not isinstance(run_directory, str):
            raise HarnessProtocolError("compact WorkItem has no readable full_item_ref")
        target = (Path(run_directory) / reference).resolve()
        root = Path(run_directory).resolve()
        if root not in target.parents:
            raise HarnessProtocolError("full_item_ref escapes the run directory")
        full = read_json(target)
        if digest(full) != item.get("full_item_hash"):
            raise HarnessProtocolError("full WorkItem hash mismatch")
        return full

    def _capture_browser_state(self, payload: dict[str, Any]) -> None:
        browser_state = payload.pop("_browser_state", None)
        if browser_state is None:
            return
        if not isinstance(browser_state, dict):
            raise HarnessProtocolError("_browser_state must be an object")
        self._browser_state = copy.deepcopy(browser_state)


class ExternalMissionWorker:
    """Yield a persisted lease to a desktop-native worker or submit its receipts."""

    def __init__(
        self,
        receipt_batch: dict[str, Any] | None = None,
        *,
        worker_instance_id: str | None = None,
    ):
        self.receipt_batch = copy.deepcopy(receipt_batch)
        supplied = (
            self.receipt_batch.get("worker_instance_id")
            if isinstance(self.receipt_batch, dict)
            else None
        )
        self.worker_instance_id = str(
            worker_instance_id or supplied or f"external:{uuid.uuid4()}"
        )

    def run(
        self,
        lease: dict[str, Any],
        checkpoint: Callable[[dict[str, Any]], dict[str, Any]],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if self.receipt_batch is None:
            cache = ResearchCache(context["cache_root"]) if context.get("cache_root") else None
            cached_receipts = []
            metrics = _empty_worker_metrics()
            metrics["worker_calls"] = 1
            if cache is not None:
                for item in lease.get("items", []):
                    work = item.get("payload", {}).get("work", {})
                    execution = work.get("execution", {}) if isinstance(work, dict) else {}
                    scope = execution.get("cache_scope") if isinstance(execution, dict) else None
                    fingerprint = (
                        execution.get("query_fingerprint")
                        if isinstance(execution, dict)
                        else None
                    )
                    cached = None
                    if isinstance(scope, str) and isinstance(fingerprint, str):
                        cached = cache.get(f"{scope}:{work.get('product_id')}", fingerprint)
                    if cached is None:
                        continue
                    cached = _rebind_cached_payload(
                        cached,
                        work,
                        operation_id=(
                            f"{lease['lease_id']}:{item['item_id']}:attempt-"
                            f"{int(item.get('attempt_number', 1))}"
                        ),
                    )
                    cached_receipts.append(
                        {
                            "receipt_version": "2.0",
                            "item_id": item["item_id"],
                            "attempt_number": int(item["attempt_number"]),
                            "status": "complete",
                            "payload": cached,
                        }
                    )
                    _merge_worker_metrics(
                        metrics,
                        item["item_id"],
                        {
                            "cache_hit_count": 1,
                            "work_payload_bytes": len(canonical_bytes(work)),
                            "receipt_payload_bytes": len(canonical_bytes(cached)),
                        },
                    )
            if not cached_receipts:
                return self._yield(lease, context.get("lease_ref"))
            next_step = checkpoint(
                {
                    "lease_id": lease["lease_id"],
                    "mission_revision": lease["mission_revision"],
                    **_claim_fields(lease, self.worker_instance_id),
                    "receipts": cached_receipts,
                    "metrics": metrics,
                }
            )
            if next_step.get("kind") == "lease":
                return self._yield(next_step["lease"], next_step.get("lease_ref"))
            return next_step
        batch = copy.deepcopy(self.receipt_batch)
        if batch.get("lease_id") != lease.get("lease_id"):
            raise HarnessProtocolError("external receipts target a different lease")
        if batch.get("mission_revision", lease["mission_revision"]) != lease["mission_revision"]:
            raise HarnessProtocolError("external receipts target a stale Mission revision")
        expected_claim = _claim_fields(lease, self.worker_instance_id)
        if batch.get("claim_id") != expected_claim["claim_id"]:
            raise HarnessProtocolError("external receipts target a stale Lease claim")
        if batch.get("worker_instance_id") != expected_claim["worker_instance_id"]:
            raise HarnessProtocolError("external receipts target a different Worker instance")
        metrics = batch.setdefault("metrics", {})
        if not isinstance(metrics, dict):
            raise HarnessProtocolError("external receipt metrics must be an object")
        metrics.setdefault("worker_calls", 1)
        metrics.setdefault("items", {})
        metrics.setdefault(
            "work_payload_bytes",
            sum(
                len(canonical_bytes(item.get("payload", {}).get("work", {})))
                for item in lease.get("items", [])
            ),
        )
        metrics.setdefault("receipt_payload_bytes", len(canonical_bytes(batch.get("receipts", []))))
        next_step = checkpoint(batch)
        if next_step.get("kind") == "lease":
            return self._yield(next_step["lease"], next_step.get("lease_ref"))
        return next_step

    @staticmethod
    def _yield(lease: dict[str, Any], lease_ref: Any) -> dict[str, Any]:
        claim = lease.get("claim")
        if not isinstance(claim, dict):
            raise HarnessProtocolError("external handoff requires a claimed Lease")
        return {
            "kind": "yield",
            "reason": "A desktop Mission worker must execute the active lease.",
            "action_required": {
                "type": "execute_mission_lease",
                "lease_id": lease["lease_id"],
                "mission_revision": lease["mission_revision"],
                "claim_id": claim["claim_id"],
                "worker_instance_id": claim["worker_instance_id"],
                "expires_at": claim["expires_at"],
                "lease_ref": lease_ref,
                "item_count": len(lease.get("items", [])),
                "items": [
                    {
                        "item_id": item.get("item_id"),
                        "kind": item.get("kind"),
                        "task_id": item.get("payload", {}).get("task_id"),
                        "research_kind": item.get("payload", {}).get(
                            "research_kind"
                        ),
                    }
                    for item in lease.get("items", [])
                ],
            },
        }


def _claim_fields(
    lease: dict[str, Any], worker_instance_id: str
) -> dict[str, str]:
    claim = lease.get("claim")
    if not isinstance(claim, dict):
        raise HarnessProtocolError("Worker received an unclaimed Lease")
    if claim.get("worker_instance_id") != worker_instance_id:
        raise HarnessProtocolError("Worker does not own the Lease claim")
    claim_id = claim.get("claim_id")
    if not isinstance(claim_id, str) or not claim_id:
        raise HarnessProtocolError("Lease claim has no claim_id")
    return {
        "claim_id": claim_id,
        "worker_instance_id": worker_instance_id,
    }


def _operation_id(item: dict[str, Any], context: dict[str, Any]) -> str:
    lease_id = context.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        raise HarnessProtocolError("Worker operation requires lease_id")
    return (
        f"{lease_id}:{item['item_id']}:attempt-"
        f"{int(item.get('attempt_number', 1))}"
    )


def _empty_worker_metrics() -> dict[str, Any]:
    return {
        "worker_calls": 0,
        "provider_query_count": 0,
        "cache_hit_count": 0,
        "coalesced_item_count": 0,
        "work_payload_bytes": 0,
        "receipt_payload_bytes": 0,
        "worker_duration_ms": 0,
        "model_input_tokens": 0,
        "model_output_tokens": 0,
        "model_token_measurements": 0,
        "items": {},
    }


def _merge_worker_metrics(
    aggregate: dict[str, Any], identifier: str, values: dict[str, Any]
) -> None:
    target = aggregate["items"].setdefault(identifier, {})
    for key, value in values.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            aggregate[key] = aggregate.get(key, 0) + value
            target[key] = target.get(key, 0) + value
        else:
            target[key] = copy.deepcopy(value)


def _timing_metrics(
    started: float,
    work_bytes: int,
    payload: dict[str, Any],
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "worker_duration_ms": int((time.perf_counter() - started) * 1000),
        "work_payload_bytes": work_bytes,
        "receipt_payload_bytes": len(canonical_bytes(payload)),
        **(usage or {}),
    }


def _pop_usage(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.pop("_usage", None)
    if not isinstance(raw, dict):
        return {}
    input_tokens = int(raw.get("input_tokens", 0) or 0)
    output_tokens = int(raw.get("output_tokens", 0) or 0)
    return {
        "model_input_tokens": input_tokens,
        "model_output_tokens": output_tokens,
        "model_token_measurements": 1,
    }
