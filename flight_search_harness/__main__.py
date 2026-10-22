"""Command line for the Flight Search Harness."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from .adapters import (
    CodexAppServerBrowserAdapter,
    OneWayProductAdapter,
    RandomLoopProductAdapter,
    RoundTripProductAdapter,
)
from .contracts import HarnessProtocolError, result_price_freshness
from .mission_runner import SearchMissionHarness
from .mission_store import MissionStore
from .mission_workers import BrowserMissionWorker, ExternalMissionWorker
from .product_kernel import AdapterProductKernel
from .result_projection import delivery_price_observations, project_delivery
from .run_store import RunStore
from .suite_layout import SuiteLayout
from .worker_projection import project_worker_turn


def _print_json(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        rendered.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        rendered = json.dumps(value, ensure_ascii=True, indent=2)
    sys.stdout.write(rendered + "\n")


def _read_json(path: str | Path, *, label: str) -> dict[str, Any]:
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{label} could not be read: {exc}") from exc
    try:
        value = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{label} is not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _claimed_worker_instance_id(store: RunStore, run_id: str) -> str | None:
    """Return the current external claim owner as a non-authoritative resume hint."""

    try:
        missions = MissionStore(store)
        active = missions.active_lease(missions.load(run_id))
    except Exception:
        # The Harness remains the authority for missing or invalid Run state.
        return None
    claim = active.get("claim") if isinstance(active, dict) else None
    value = claim.get("worker_instance_id") if isinstance(claim, dict) else None
    return value if isinstance(value, str) and value else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", default="runs")
    parser.add_argument(
        "--worker",
        choices=["external", "codex-app-server"],
        default="external",
    )
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--worker-timeout-seconds", type=float, default=900)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--request", required=True)
    start.add_argument("--run-id")
    resume = sub.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    resume.add_argument(
        "--receipt-batch",
        help="compact Receipt submission or full Receipt batch for the active Lease",
    )
    status = sub.add_parser("status")
    status.add_argument("--run-id", required=True)
    metrics = sub.add_parser("metrics")
    metrics.add_argument("--run-id", required=True)
    deliver = sub.add_parser("deliver")
    deliver.add_argument("--run-id", required=True)
    deliver.add_argument(
        "--view",
        choices=["presentation", "full"],
        default="presentation",
        help="presentation is compact; full exposes the complete Product result",
    )
    return parser


def _status(store: RunStore, run_id: str) -> dict[str, Any]:
    run = store.load_manifest(run_id)
    if run.get("status") == "completed" and isinstance(
        run.get("last_outcome"), dict
    ):
        freshness = result_price_freshness(
            str(run["last_outcome"].get("completed_at") or run["updated_at"]),
            price_valid_until=(
                str(run["last_outcome"]["price_valid_until"])
                if run["last_outcome"].get("price_valid_until") is not None
                else None
            ),
        )
        run["last_outcome"].update(freshness)
        if isinstance(run["last_outcome"].get("result"), dict):
            run["last_outcome"]["result"].update(freshness)
    missions = MissionStore(store)
    mission = missions.load(run_id)
    active = missions.active_lease(mission)
    active_work = None
    if isinstance(active, dict):
        remaining = active.get("items", [])
        claim = active.get("claim")
        claim = claim if isinstance(claim, dict) else {}
        active_work = {
            "lease_id": active.get("lease_id"),
            "lease_ref": mission.get("active_lease_ref"),
            "remaining_item_count": len(remaining) if isinstance(remaining, list) else 0,
            "worker_instance_id": claim.get("worker_instance_id"),
            "expires_at": claim.get("expires_at"),
        }
    return {"run": run, "active_work": active_work}


def _delivery_blocked(
    run: dict[str, Any], reason: str, action_required: Any
) -> dict[str, Any]:
    return {
        "delivery_version": "1.0",
        "kind": "delivery_blocked",
        "run_id": run["run_id"],
        "product_id": run["product_id"],
        "reason": reason,
        "action_required": action_required,
    }


def _deliver(
    store: RunStore, run_id: str, *, view: str = "presentation"
) -> dict[str, Any]:
    """Expose a final Result only through the completed, current Run contract."""

    run = store.load_manifest(run_id)
    last_outcome = run.get("last_outcome")
    if (
        run.get("status") != "completed"
        or not isinstance(last_outcome, dict)
        or last_outcome.get("kind") != "completed"
        or run.get("result_ref") != "product/final.json"
    ):
        action_required = (
            copy.deepcopy(last_outcome.get("action_required"))
            if isinstance(last_outcome, dict)
            else None
        )
        return _delivery_blocked(
            run,
            "Run has no completed Outcome and cannot be delivered.",
            action_required or {"type": "resume_search_mission"},
        )
    try:
        result = _read_json(
            store.product_directory(run_id) / "final.json",
            label="final Result",
        )
    except ValueError as exc:
        return _delivery_blocked(
            run,
            str(exc),
            {"type": "repair_final_result"},
        )
    try:
        freshness = result_price_freshness(
            price_observations=delivery_price_observations(result),
            completed_at=str(
                last_outcome.get("completed_at") or run.get("updated_at")
            ),
        )
    except HarnessProtocolError as exc:
        return _delivery_blocked(
            run,
            f"final Result freshness is invalid: {exc}",
            {"type": "repair_final_result"},
        )
    if freshness["price_freshness"] != "current":
        return _delivery_blocked(
            run,
            "Completed Result price Evidence has expired and cannot be delivered as current.",
            {
                "type": "refresh_price_evidence",
                "result_ref": "product/final.json",
                "price_valid_until": freshness["price_valid_until"],
            },
        )
    return {
        "delivery_version": "1.0",
        "kind": "deliverable",
        "run_id": run["run_id"],
        "product_id": run["product_id"],
        **freshness,
        "full_result_ref": "product/final.json",
        "result": (
            result
            if view == "full"
            else project_delivery(
                str(run["product_id"]),
                result,
                full_result_ref="product/final.json",
            )
        ),
    }


def _attach_worker_turn(
    store: RunStore, outcome: dict[str, Any]
) -> dict[str, Any]:
    """Attach a compact, non-persisted external Worker turn to CLI output."""

    action = outcome.get("action_required")
    if not isinstance(action, dict) or action.get("type") != "execute_mission_lease":
        return outcome
    run_id = outcome.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return outcome
    missions = MissionStore(store)
    lease = missions.active_lease(missions.load(run_id))
    if not isinstance(lease, dict):
        return outcome
    projected = copy.deepcopy(outcome)
    worker_turn = project_worker_turn(lease)
    projected["action_required"] = {
        "type": "execute_worker_turn",
        "run_id": run_id,
        "item_count": len(worker_turn["items"]),
        "expires_at": worker_turn.get("expires_at"),
    }
    projected["worker_turn"] = worker_turn
    return projected


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = RunStore(args.runs_root)
    if args.command == "status":
        _print_json(_status(store, args.run_id))
        return 0
    if args.command == "metrics":
        _print_json(store.metrics(args.run_id))
        return 0
    if args.command == "deliver":
        delivery = _deliver(store, args.run_id, view=args.view)
        _print_json(delivery)
        return 0 if delivery["kind"] == "deliverable" else 2
    receipt_batch_path = getattr(args, "receipt_batch", None)
    if receipt_batch_path and args.worker != "external":
        parser.error("--receipt-batch requires --worker external")
    try:
        receipt_submission = (
            _read_json(receipt_batch_path, label="Receipt submission")
            if receipt_batch_path
            else None
        )
        request = (
            _read_json(args.request, label="request")
            if args.command == "start"
            else None
        )
    except ValueError as exc:
        parser.error(str(exc))
    layout = SuiteLayout.discover(anchor=__file__)
    adapters = [
        OneWayProductAdapter(
            product_root=layout.runtime_root(OneWayProductAdapter.product_id),
        ),
        RoundTripProductAdapter(
            product_root=layout.runtime_root(RoundTripProductAdapter.product_id),
        ),
        RandomLoopProductAdapter(
            product_root=layout.runtime_root(RandomLoopProductAdapter.product_id),
        ),
    ]
    kernels = {
        adapter.journey_shape: AdapterProductKernel(adapter)
        for adapter in adapters
    }
    harness = SearchMissionHarness(store, kernels)
    if args.worker == "external":
        worker = ExternalMissionWorker(
            receipt_submission,
            worker_instance_id=(
                _claimed_worker_instance_id(store, args.run_id)
                if receipt_submission is not None and args.command == "resume"
                else None
            ),
        )
    else:
        worker = BrowserMissionWorker(
            CodexAppServerBrowserAdapter(
                cwd=Path.cwd(),
                command=[args.codex_bin, "app-server", "--listen", "stdio://"],
                model=args.model,
                effort=args.effort,
                timeout_seconds=args.worker_timeout_seconds,
            )
        )
    command = (
        {"kind": "start", "request": request, "run_id": args.run_id}
        if args.command == "start"
        else {"kind": "resume", "run_id": args.run_id}
    )
    outcome = harness.run(command, worker)
    if args.worker == "external":
        outcome = _attach_worker_turn(store, outcome)
    _print_json(outcome)
    return 2 if outcome["kind"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
