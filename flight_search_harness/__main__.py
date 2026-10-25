"""CLI for the single-ledger, agent-directed Flight Search Harness."""
from __future__ import annotations
import argparse
import json
import sys
from .research import ResearchStore, read_json, now
from .suite_layout import SuiteLayout


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", help="Defaults to external runtime_data_root/runs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("clock", help="Read the actual UTC clock when collecting source observations; no Run is created")
    start = sub.add_parser("start", help="Start a fresh Request and Run")
    start.add_argument("--request", required=True)
    start.add_argument("--run-id")
    for name in ("status", "resume", "stop", "query", "record", "combine", "assess", "revise", "deliver"):
        command = sub.add_parser(name)
        command.add_argument("--run-id", required=True)
        if name in {"record", "combine", "assess"}:
            command.add_argument("--input", required=True)
        if name == "revise":
            command.add_argument("--request", required=True)
        if name == "stop":
            command.add_argument("--reason", required=True)
        if name == "query":
            command.add_argument("--query", required=True)
            command.add_argument("--retry-reason")
        if name == "deliver":
            command.add_argument("--view", choices=("ranked", "matrix", "full", "ticket-quotes", "presentation"))
            command.add_argument("--limit", type=int)
    legacy = sub.add_parser("import-legacy", help="Import observations from one identified Run, preserving its originals")
    legacy.add_argument("--source-run", required=True)
    legacy.add_argument("--request", required=True)
    legacy.add_argument("--run-id")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "clock":
            print(json.dumps({"utc_now": now(), "time_standard": "ISO 8601 UTC"}))
            return 0
        root = args.runs_root or SuiteLayout.discover(anchor=__file__).runtime_data_root() / "runs"
        store = ResearchStore(root)
        command = args.command
        if command == "start":
            value = store.start(read_json(args.request), args.run_id)
        elif command == "revise":
            value = store.revise(args.run_id, read_json(args.request))
        elif command == "query":
            value = store.query(args.run_id, read_json(args.query), args.retry_reason)
        elif command in {"record", "combine", "assess"}:
            value = getattr(store, command)(args.run_id, read_json(args.input))
        elif command == "deliver":
            view = "ranked" if args.view in {"ticket-quotes", "presentation"} else args.view
            value = store.deliver(args.run_id, view, args.limit)
        elif command == "status":
            value = store.summary(store.load(args.run_id))
        elif command == "resume":
            value = store.set_research(args.run_id, "active")
        elif command == "stop":
            value = store.set_research(args.run_id, "stopped", args.reason)
        else:
            value = store.import_legacy(args.source_run, read_json(args.request), args.run_id)
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"kind": "command_error", "message": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
