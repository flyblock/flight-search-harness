"""Small interfaces shared by the Runner and its concrete adapters."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


HARNESS_VERSION = "1.3.0"
REQUEST_VERSION = "4.0"
OUTCOME_VERSION = "2.0"
OUTCOME_KINDS = {"draft", "blocked", "failed", "completed"}
RESULT_FRESHNESS_TTL_SECONDS = 900


def result_price_freshness(
    completed_at: str | None = None,
    *,
    now_utc: datetime | None = None,
) -> dict[str, str]:
    completed = (
        datetime.now(timezone.utc)
        if completed_at is None
        else datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    )
    if completed.tzinfo is None or completed.utcoffset() is None:
        raise HarnessProtocolError("completed_at must include a timezone")
    completed = completed.astimezone(timezone.utc)
    valid_until = completed + timedelta(seconds=RESULT_FRESHNESS_TTL_SECONDS)
    current = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "completed_at": completed.isoformat(timespec="seconds"),
        "price_valid_until": valid_until.isoformat(timespec="seconds"),
        "price_freshness": "current" if current < valid_until else "expired",
    }


class HarnessError(RuntimeError):
    """Base error for a persisted Harness run."""


class HarnessProtocolError(HarnessError):
    """Raised when a caller or adapter violates the unified interface."""


class HarnessBlockedError(HarnessError):
    """Raised when persisted input proves an external retryable blocker."""

    def __init__(self, reason: str, action_required: dict[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.action_required = action_required


def _plan_adapter_transition(
    control: dict[str, Any],
    investigations: list[dict[str, Any]],
    selected_investigations: list[dict[str, Any]],
    *,
    choose_when_mixed: bool,
) -> dict[str, Any]:
    """Build a generic transition plan from Adapter-owned priorities."""

    actions = [
        copy.deepcopy(action)
        for action in control.get("actions", [])
        if isinstance(action, dict) and action.get("action_key")
    ]
    automatic = [action for action in actions if action.get("automatic") is True]
    if automatic:
        selected = automatic[0]
        if selected.get("kind") == "finalize":
            return {"kind": "finalize"}
        return {"kind": "apply", "action": selected}
    complete_dispositions = [
        copy.deepcopy(action)
        for action in control.get("disposition_actions", [])
        if isinstance(action, dict) and action.get("disposition") == "complete"
    ]
    if control.get("has_complete_result") and complete_dispositions:
        return {
            "kind": "apply",
            "action": complete_dispositions[0],
            "reason": (
                "ProductKernel closed a hypothesis whose product completion gate passed."
            ),
        }
    selectable = [action for action in actions if action.get("kind") != "investigate"]
    if choose_when_mixed and investigations and selectable:
        return {"kind": "choice", "actions": actions}
    if investigations:
        if not selected_investigations:
            raise HarnessProtocolError(
                "Product Adapter selected no investigation from published work"
            )
        return {
            "kind": "investigate",
            "actions": copy.deepcopy(selected_investigations),
        }
    if control.get("can_finalize"):
        return {"kind": "finalize"}
    if selectable:
        return {"kind": "choice", "actions": selectable}
    return {
        "kind": "blocked",
        "reason": str(
            control.get("draft_reason")
            or "ProductKernel published no work and cannot finalize."
        ),
        "action_required": copy.deepcopy(control.get("blocked_action"))
        if isinstance(control.get("blocked_action"), dict)
        else {"type": "mission_has_no_executable_work"},
    }


def _build_adapter_choice_item(
    control: dict[str, Any],
    actions: list[dict[str, Any]],
    identify: Callable[[str, Any], str],
    *,
    session_identity: Any,
    round_identity: Any,
    browser_attempt_count: int,
) -> dict[str, Any]:
    """Materialize a choice Work Item from Adapter-owned Session context."""

    identifier = identify(
        "choice",
        {
            "session": session_identity,
            "round": round_identity,
            "action_keys": [action["action_key"] for action in actions],
        },
    )
    return {
        "item_id": identifier,
        "kind": "choice",
        "payload": {
            "actions": copy.deepcopy(actions),
            "control_summary": {
                "product_id": control.get("product_id"),
                "active_hypothesis_id": control.get("active_hypothesis_id"),
                "has_complete_result": control.get("has_complete_result"),
                "can_finalize": control.get("can_finalize"),
                "finalization_issues": copy.deepcopy(
                    control.get("finalization_issues", [])
                ),
                "investigation_actions": copy.deepcopy(
                    control.get("investigation_actions", [])
                ),
                "disposition_actions": copy.deepcopy(
                    control.get("disposition_actions", [])
                ),
                "frontier_actions": copy.deepcopy(control.get("frontier_actions", [])),
                "browser_attempt_count": browser_attempt_count,
            },
        },
    }


def _build_adapter_research_item(
    *,
    task_id: str,
    attempt_number: int,
    session_identity: Any,
    item_kind: str,
    work: dict[str, Any],
    hypothesis_id: Any,
    research_kind: str,
    journey_scope: Any,
    identify: Callable[[str, Any], str],
) -> dict[str, Any]:
    """Materialize one Work Item after an Adapter interpreted its Session."""

    action_id = identify(
        "mission-action",
        {
            "task_id": task_id,
            "attempt_number": attempt_number,
            "session": session_identity,
        },
    ).replace(":", "-")
    return {
        "item_id": identify(
            "work",
            {
                "task_id": task_id,
                "attempt_number": attempt_number,
                "kind": item_kind,
            },
        ),
        "kind": item_kind,
        "payload": {
            "action": {
                "action_id": action_id,
                "task_id": task_id,
                "kind": "investigate",
            },
            "work": copy.deepcopy(work),
            "task_id": task_id,
            "hypothesis_id": hypothesis_id,
            "research_kind": research_kind,
            "journey_scope": journey_scope,
        },
    }


def _observation_bundle_schema() -> dict[str, Any]:
    """Return the compact raw Observation alternative advertised by an Adapter."""

    return {
        "type": "object",
        "required": ["kind", "bundle_id", "source", "observations"],
        "properties": {
            "kind": {"const": "observation_bundle"},
            "bundle_id": {"type": "string", "minLength": 1},
            "source": {"type": "object"},
            "coverage_complete": {"type": "boolean"},
            "discovered_nodes": {"type": "array"},
            "observations": {"type": "array", "minItems": 1},
        },
        "additionalProperties": False,
    }


class BrowserAdapter(Protocol):
    """AI/browser seam used by live Codex browser execution."""

    adapter_id: str

    def choose(
        self, control: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        """Choose one generic control action from the product projection."""

    def execute(
        self, work: dict[str, Any], action_id: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute one selected browser brief and return observations or a terminal state."""


class ProductAdapter(Protocol):
    """Product-specific state Interface used by AdapterProductKernel."""

    product_id: str
    journey_shape: str

    def initialize(self, request: dict[str, Any], product_directory: Path) -> dict[str, Any]:
        ...

    def load_state(self, product_directory: Path) -> dict[str, Any]:
        ...

    def inspect(self, state: dict[str, Any]) -> dict[str, Any]:
        ...

    def completed_result(
        self,
        product_directory: Path,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Translate product-owned finalization state into a completed result."""

    def plan_transition(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
    ) -> dict[str, Any]:
        """Select the next generic Product Kernel transition."""

    def build_choice_item(
        self,
        state: dict[str, Any],
        control: dict[str, Any],
        actions: list[dict[str, Any]],
        identify: Callable[[str, Any], str],
    ) -> dict[str, Any]:
        """Translate product-owned choice context into one Work Item."""

    def build_research_items(
        self,
        product_directory: Path,
        investigations: list[dict[str, Any]],
        reason: str,
        identify: Callable[[str, Any], str],
    ) -> list[dict[str, Any]]:
        """Translate selected product investigations into Work Items."""

    def normalize_attempt(self, attempt: dict[str, Any]) -> dict[str, Any]:
        """Translate a compact Worker attempt into the product-owned form."""

    def ingest_attempt(
        self,
        product_directory: Path,
        action: dict[str, Any],
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def attempt_committed(
        self,
        state: dict[str, Any],
        action: dict[str, Any],
        attempt: dict[str, Any],
    ) -> bool:
        ...

    def apply_control_action(
        self,
        product_directory: Path,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def finalize(self, product_directory: Path, reason: str) -> dict[str, Any]:
        ...
