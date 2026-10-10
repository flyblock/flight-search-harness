"""Command line for the Flight Search Harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .adapters import (
    CodexAppServerBrowserAdapter,
    OneWayProductAdapter,
    RandomLoopProductAdapter,
    RoundTripProductAdapter,
)
from .mission_runner import SearchMissionHarness
from .mission_store import MissionStore
from .mission_workers import BrowserMissionWorker, ExternalMissionWorker
from .product_kernel import AdapterProductKernel
from .run_store import RunStore
from .suite_layout import SuiteLayout


def _read_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request JSON must be an object")
    return value


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
        help="JSON Receipt batch for the active Lease",
    )
    status = sub.add_parser("status")
    status.add_argument("--run-id", required=True)
    metrics = sub.add_parser("metrics")
    metrics.add_argument("--run-id", required=True)
    return parser


def _status(store: RunStore, run_id: str) -> dict[str, Any]:
    run = store.load_manifest(run_id)
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
        print(json.dumps(_status(store, args.run_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "metrics":
        print(json.dumps(store.metrics(args.run_id), ensure_ascii=False, indent=2))
        return 0
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
    receipt_batch = getattr(args, "receipt_batch", None)
    if receipt_batch and args.worker != "external":
        parser.error("--receipt-batch requires --worker external")
    if args.worker == "external":
        worker = ExternalMissionWorker(
            _read_json(receipt_batch) if receipt_batch else None
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
        {"kind": "start", "request": _read_json(args.request), "run_id": args.run_id}
        if args.command == "start"
        else {"kind": "resume", "run_id": args.run_id}
    )
    outcome = harness.run(command, worker)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 2 if outcome["kind"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
