"""Command line for the Flight Search Harness."""

from __future__ import annotations

import argparse
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
from .contracts import result_price_freshness
from .mission_runner import SearchMissionHarness
from .mission_store import MissionStore
from .mission_workers import BrowserMissionWorker, ExternalMissionWorker
from .product_kernel import AdapterProductKernel
from .run_store import RunStore
from .suite_layout import SuiteLayout


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
    return parser


def _status(store: RunStore, run_id: str) -> dict[str, Any]:
    run = store.load_manifest(run_id)
    if run.get("status") == "completed" and isinstance(
        run.get("last_outcome"), dict
    ):
        freshness = result_price_freshness(
            str(run["last_outcome"].get("completed_at") or run["updated_at"])
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
    _print_json(outcome)
    return 2 if outcome["kind"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
