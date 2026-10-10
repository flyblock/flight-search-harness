"""Product Kernel implementation backed by a product-specific Adapter."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .contracts import HarnessBlockedError, HarnessProtocolError, ProductAdapter
from .mission_contracts import blocked, complete, draft, need_work


TRANSITION_VERSION = "3.0"


class AdapterProductKernel:
    """Expose one Product Adapter through the Product Kernel Seam."""

    def __init__(self, adapter: ProductAdapter):
        self._adapter = adapter
        self.product_id = adapter.product_id
        self.journey_shape = adapter.journey_shape

    @staticmethod
    def _stable_id(prefix: str, value: Any) -> str:
        digest = hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:20]
        return f"{prefix}:{digest}"

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _choice_admission_path(
        self, product_directory: Path, receipt_id: str
    ) -> Path:
        digest = hashlib.sha256(receipt_id.encode("utf-8")).hexdigest()
        return product_directory / "choice-admissions" / f"{digest}.json"

    def _choice_admission_state_identity(self, product_directory: Path) -> str:
        identity = getattr(self._adapter, "admission_state_identity", None)
        if not callable(identity):
            raise HarnessProtocolError(
                "Product Adapter does not expose an admission state identity"
            )
        value = identity(product_directory)
        if not isinstance(value, str) or not value:
            raise HarnessProtocolError(
                "Product Adapter admission state identity must be a non-empty string"
            )
        return value

    def _apply_choice_mutation_once(
        self,
        product_directory: Path,
        item: dict[str, Any],
        receipt: dict[str, Any],
        selected: dict[str, Any],
        mutation: Any,
    ) -> None:
        """Admit one mutating choice exactly once across a Harness crash."""

        receipt_id = str(receipt.get("receipt_id") or "").strip()
        if not receipt_id:
            receipt_id = self._stable_id(
                "choice-receipt",
                {
                    "item_id": item.get("item_id"),
                    "payload": receipt.get("payload"),
                },
            )
        intent_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "item_id": item.get("item_id"),
                    "receipt_id": receipt_id,
                    "selected": selected,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        target = self._choice_admission_path(product_directory, receipt_id)
        marker = None
        if target.exists():
            try:
                marker = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessProtocolError(
                    f"cannot read choice admission marker: {exc}"
                ) from exc
            if not isinstance(marker, dict):
                raise HarnessProtocolError("choice admission marker must be an object")
            if marker.get("intent_sha256") != intent_sha256:
                raise HarnessProtocolError(
                    "choice Receipt identity was replayed with conflicting intent"
                )
            if marker.get("status") == "committed":
                return
            before_identity = marker.get("state_before_sha256")
            if not isinstance(before_identity, str) or not before_identity:
                raise HarnessProtocolError(
                    "pending choice admission lacks its pre-admission state identity"
                )
            current_identity = self._choice_admission_state_identity(product_directory)
            if current_identity != before_identity:
                marker["status"] = "committed"
                marker["state_after_sha256"] = current_identity
                self._write_json_atomic(target, marker)
                return
        else:
            before_identity = self._choice_admission_state_identity(product_directory)
            marker = {
                "admission_version": "1.0",
                "receipt_id": receipt_id,
                "item_id": item.get("item_id"),
                "intent_sha256": intent_sha256,
                "state_before_sha256": before_identity,
                "status": "pending",
            }
            self._write_json_atomic(target, marker)

        try:
            mutation()
        except Exception:
            after_identity = self._choice_admission_state_identity(product_directory)
            if after_identity != before_identity:
                marker["status"] = "committed"
                marker["state_after_sha256"] = after_identity
                self._write_json_atomic(target, marker)
            raise
        after_identity = self._choice_admission_state_identity(product_directory)
        marker["status"] = "committed"
        marker["state_after_sha256"] = after_identity
        self._write_json_atomic(target, marker)

    def start(
        self, request: dict[str, Any], product_directory: Path
    ) -> dict[str, Any]:
        self._adapter.initialize(request, product_directory)
        return self._transition([], self._next_effect(product_directory))

    def advance(
        self,
        product_directory: Path,
        inbound: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if inbound is None:
            return self._transition([], self._next_effect(product_directory))
        if not isinstance(inbound, dict) or inbound.get("kind") != "receipt_batch":
            raise HarnessProtocolError("ProductKernel.advance requires receipt_batch or null")
        entries = inbound.get("entries")
        if not isinstance(entries, list) or not entries:
            raise HarnessProtocolError("receipt_batch.entries must be non-empty")
        receipt_results = []
        corrections = []
        pause_effect = None
        for entry in entries:
            if not isinstance(entry, dict):
                raise HarnessProtocolError("receipt_batch entry must be an object")
            item = entry.get("item")
            receipt = entry.get("receipt")
            if not isinstance(item, dict) or not isinstance(receipt, dict):
                raise HarnessProtocolError("receipt_batch entry requires item and receipt")
            identifier = str(item.get("item_id") or "")
            try:
                item_pause = self._apply_receipt(product_directory, item, receipt)
                receipt_results.append({"item_id": identifier, "status": "committed"})
                if item_pause is not None:
                    pause_effect = item_pause
            except HarnessBlockedError as exc:
                receipt_results.append(
                    {
                        "item_id": identifier,
                        "status": "blocked",
                        "error": exc.reason,
                    }
                )
                pause_effect = blocked(exc.reason, exc.action_required)
            except (HarnessProtocolError, ValueError) as exc:
                receipt_results.append(
                    {
                        "item_id": identifier,
                        "status": "correction_required",
                        "error": str(exc),
                    }
                )
                # Retry the same bounded requirement as a new Work Attempt.
                # The Store owns attempt identity and attaches the rejection
                # context to the next Lease projection.
                corrections.append(copy.deepcopy(item))
        if corrections:
            effect = need_work(
                corrections,
                reason="One or more Mission receipts require item-level correction.",
            )
        elif pause_effect is not None:
            effect = pause_effect
        else:
            effect = self._next_effect(product_directory)
        return self._transition(receipt_results, effect)

    @staticmethod
    def _transition(
        receipt_results: list[dict[str, Any]], effect: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "transition_version": TRANSITION_VERSION,
            "receipt_results": copy.deepcopy(receipt_results),
            "effect": copy.deepcopy(effect),
        }

    def _apply_receipt(
        self,
        product_directory: Path,
        item: dict[str, Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        if receipt.get("status") != "complete":
            reason = str(receipt.get("reason") or "Mission worker did not complete the item")
            raise HarnessBlockedError(
                reason,
                {
                    "type": "mission_item_blocked",
                    "item_id": item.get("item_id"),
                },
            )
        payload = receipt.get("payload")
        if not isinstance(payload, dict):
            raise HarnessProtocolError("complete Mission receipt requires payload")
        item_kind = item.get("kind")
        internal = item.get("payload", {})
        if item_kind in {"browser_research", "semantic_judgment"}:
            action = copy.deepcopy(internal.get("action"))
            if not isinstance(action, dict):
                raise HarnessProtocolError("research Mission item is missing action")
            action["_receipt_id"] = receipt.get("receipt_id")
            payload = self._adapter.normalize_attempt(payload)
            state = self._adapter.load_state(product_directory)
            if not self._adapter.attempt_committed(state, action, payload):
                self._adapter.ingest_attempt(product_directory, action, payload)
            return None
        if item_kind == "choice":
            action_key = payload.get("action_key")
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise HarnessProtocolError("choice receipt requires reason")
            options = internal.get("actions")
            if not isinstance(options, list):
                raise HarnessProtocolError("choice Mission item is missing actions")
            selected = next(
                (
                    copy.deepcopy(action)
                    for action in options
                    if isinstance(action, dict) and action.get("action_key") == action_key
                ),
                None,
            )
            if selected is None:
                raise HarnessProtocolError(
                    f"choice selected unpublished action_key: {action_key}"
                )
            selected["reason"] = reason.strip()
            kind = selected.get("kind")
            if kind == "investigate":
                items = self._adapter.build_research_items(
                    product_directory,
                    [selected],
                    "AI selected the next checkpointed investigation.",
                    self._stable_id,
                )
                return need_work(
                    items,
                    reason="AI selected the next checkpointed investigation.",
                )
            if kind == "finalize":
                self._apply_choice_mutation_once(
                    product_directory,
                    item,
                    receipt,
                    selected,
                    lambda: self._adapter.finalize(
                        product_directory, reason.strip()
                    ),
                )
                return None
            if kind == "draft":
                return draft(
                    reason.strip(), {"type": "mission_paused_by_choice"}
                )
            self._apply_choice_mutation_once(
                product_directory,
                item,
                receipt,
                selected,
                lambda: self._adapter.apply_control_action(
                    product_directory, selected
                ),
            )
            return None
        raise HarnessProtocolError(f"unsupported Mission item kind: {item_kind}")

    def _next_effect(self, product_directory: Path) -> dict[str, Any]:
        for _ in range(256):
            state = self._adapter.load_state(product_directory)
            final_result = self._adapter.completed_result(product_directory, state)
            if final_result is not None:
                return complete(final_result)
            control = self._adapter.inspect(state)
            plan = self._adapter.plan_transition(state, control)
            if not isinstance(plan, dict):
                raise HarnessProtocolError("Product Adapter transition plan must be an object")
            plan_kind = plan.get("kind")
            if plan_kind == "apply":
                selected = copy.deepcopy(plan.get("action"))
                if not isinstance(selected, dict):
                    raise HarnessProtocolError("apply transition requires action")
                selected["reason"] = str(
                    plan.get("reason")
                    or "ProductKernel applied a deterministic product transition."
                )
                self._adapter.apply_control_action(product_directory, selected)
                continue
            if plan_kind == "investigate":
                investigations = plan.get("actions")
                if not isinstance(investigations, list) or not investigations:
                    raise HarnessProtocolError(
                        "investigate transition requires non-empty actions"
                    )
                reason = str(
                    plan.get("reason")
                    or "ProductKernel published the next checkpointed work bundle."
                )
                items = self._adapter.build_research_items(
                    product_directory,
                    copy.deepcopy(investigations),
                    reason,
                    self._stable_id,
                )
                return need_work(items, reason=reason)
            if plan_kind == "choice":
                actions = plan.get("actions")
                if not isinstance(actions, list) or not actions:
                    raise HarnessProtocolError("choice transition requires non-empty actions")
                item = self._adapter.build_choice_item(
                    state,
                    control,
                    copy.deepcopy(actions),
                    self._stable_id,
                )
                return need_work(
                    [item],
                    reason=str(
                        plan.get("reason")
                        or "AI judgment is required for the next product transition."
                    ),
                )
            if plan_kind == "finalize":
                result = self._adapter.finalize(
                    product_directory,
                    str(
                        plan.get("reason")
                        or "ProductKernel finalized after the product audit passed."
                    ),
                )
                return complete(result)
            if plan_kind == "blocked":
                return blocked(
                    str(
                        plan.get("reason")
                        or "ProductKernel published no work and cannot finalize."
                    ),
                    copy.deepcopy(plan.get("action_required"))
                    if isinstance(plan.get("action_required"), dict)
                    else {"type": "mission_has_no_executable_work"},
                )
            raise HarnessProtocolError(
                f"unsupported Product Adapter transition plan: {plan_kind}"
            )
        raise HarnessProtocolError("ProductKernel exceeded deterministic transition limit")
