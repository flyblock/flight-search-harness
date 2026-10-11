from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from flight_search_harness.contracts import HarnessProtocolError
from flight_search_harness.request_values import validate_public_request


ROOT = Path(__file__).resolve().parents[1]
SKILLS = {
    "low-cost-one-way-travel-flight-search": (
        "one_way",
        "one-way-request.schema.json",
    ),
    "low-cost-random-loop-flight-search": (
        "random_loop",
        "random-loop-request.schema.json",
    ),
    "low-cost-round-trip-flight-search": (
        "round_trip",
        "round-trip-request.schema.json",
    ),
}
SHARED_INTENT_FIELDS = {
    "base_currency",
    "adult_count",
    "baggage_policy",
    "overnight_allowed",
    "self_transfer_policy",
}
INTERNAL_ONLY_FIELDS = {
    "intent_id",
    "trip_scope",
    "trip_mode",
    "currency_decimal_places",
    "product_version",
    "intent_version",
    "node_scope",
    "beam_width",
    "history_limit",
    "source_strategy",
    "diversity",
    "max_portfolio_offers",
    "max_solver_offers",
}


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def valid_one_way_request() -> dict:
    def node(identifier: str, node_type: str = "airport") -> dict:
        return {
            "node_id": identifier,
            "node_type": node_type,
            "location": identifier,
            "country_or_region": "Testland",
            "country_code": "XT",
            "timezone": "Etc/UTC",
            "utc_offset": "+00:00",
        }

    def side(identifier: str, airport: str) -> dict:
        return {
            "region_id": identifier,
            "label": identifier,
            "country_codes": ["XT"],
            "policy": "exact",
            "endpoint_policy": "anchor_required",
            "anchor_nodes": [node(airport)],
            "seed_gateway_nodes": [],
            "max_ground_cost": None,
        }

    return {
        "request_version": "4.0",
        "journey_shape": "one_way",
        "intent": {
            "base_currency": "XTS",
            "adult_count": 1,
            "baggage_policy": {
                "cabin_baggage": "none_required",
                "checked_bag_count": 0,
                "paid_baggage_allowed": True,
            },
            "overnight_allowed": True,
            "self_transfer_policy": "allow",
            "origin": side("origin", "AAA"),
            "destination": side("destination", "BBB"),
            "departure_window": {
                "start": "2027-01-01T08:00:00+00:00",
                "end": "2027-01-01T12:00:00+00:00",
            },
        },
    }


class PublicRequestContractTests(unittest.TestCase):
    def test_runtime_enforces_the_published_schema_before_run_creation(self) -> None:
        baseline = valid_one_way_request()
        self.assertEqual(baseline, validate_public_request(baseline))
        invalid_requests = []

        invalid = copy.deepcopy(baseline)
        invalid["metadata"] = {}
        invalid_requests.append(invalid)
        invalid = copy.deepcopy(baseline)
        invalid["request_id"] = ""
        invalid_requests.append(invalid)
        invalid = copy.deepcopy(baseline)
        invalid["intent"]["allowed_modes"] = ["rail"]
        invalid_requests.append(invalid)
        invalid = copy.deepcopy(baseline)
        invalid["intent"]["maximum_provider_queries"] = None
        invalid_requests.append(invalid)
        invalid = copy.deepcopy(baseline)
        invalid["intent"]["departure_window"] = {
            "start": "2027-01-01T23:00:00+00:00",
            "end": "2027-01-01T01:00:00+00:00",
        }
        invalid_requests.append(invalid)
        invalid = copy.deepcopy(baseline)
        invalid["intent"]["origin"]["anchor_nodes"][0]["node_type"] = (
            "city_destination"
        )
        invalid_requests.append(invalid)

        for index, request in enumerate(invalid_requests):
            with self.subTest(index=index):
                with self.assertRaises(HarnessProtocolError):
                    validate_public_request(request)

    def test_runtime_and_schema_accept_the_same_top_level_intent_fields(self) -> None:
        from flight_search_harness import request_values

        for _, (journey_shape, schema_name) in SKILLS.items():
            schema = read_json(
                ROOT / "flight_search_harness" / "schemas" / "requests" / schema_name
            )
            schema_fields = set(
                schema["properties"]["intent"]["properties"]
            )
            runtime_fields = (
                request_values._SHARED_FIELDS
                | request_values._PRODUCT_FIELDS[journey_shape]
            )
            self.assertEqual(schema_fields, runtime_fields)

    def test_templates_use_current_public_shared_semantics(self) -> None:
        for skill_name, (journey_shape, schema_name) in SKILLS.items():
            with self.subTest(skill=skill_name):
                template = read_json(
                    ROOT / "skills" / skill_name / "assets" / "request-template.json"
                )
                schema = read_json(
                    ROOT / "flight_search_harness" / "schemas" / "requests" / schema_name
                )
                self.assertEqual(template["request_version"], "4.0")
                self.assertEqual(template["journey_shape"], journey_shape)
                self.assertEqual(
                    schema["properties"]["request_version"]["const"], "4.0"
                )
                self.assertEqual(
                    schema["properties"]["journey_shape"]["const"], journey_shape
                )
                intent = template["intent"]
                self.assertTrue(SHARED_INTENT_FIELDS <= set(intent))
                self.assertFalse(INTERNAL_ONLY_FIELDS & set(intent))

class DistributionContractTests(unittest.TestCase):
    def test_resolver_uses_release_asset_and_rejects_unsafe_zip_members(self) -> None:
        resolver_path = (
            ROOT
            / "skills"
            / "low-cost-one-way-travel-flight-search"
            / "scripts"
            / "resolve_harness.py"
        )
        spec = importlib.util.spec_from_file_location("skill_resolver", resolver_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("/releases/download/v1.1.0/", module.ARCHIVE_URL)
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            archive_path = root / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "unsafe")
            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaises(module.HarnessResolutionError):
                    module._safe_extract(archive, root / "output")

    def test_resolver_recovers_an_abandoned_install_lock(self) -> None:
        resolver_path = (
            ROOT
            / "skills"
            / "low-cost-one-way-travel-flight-search"
            / "scripts"
            / "resolve_harness.py"
        )
        spec = importlib.util.spec_from_file_location("skill_resolver_lock", resolver_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw_directory:
            lock_path = Path(raw_directory) / ".install.lock"
            lock_path.write_text("abandoned", encoding="utf-8")
            old = time.time() - module.INSTALL_LOCK_STALE_SECONDS - 1
            os.utime(lock_path, (old, old))

            token = module._acquire_install_lock(lock_path)
            value = read_json(lock_path)
            self.assertEqual(token, value["token"])
            module._release_install_lock(lock_path, token)
            self.assertFalse(lock_path.exists())

    def test_package_validation_closes_over_public_contracts(self) -> None:
        path = ROOT / "scripts" / "package_flight_search_harness.py"
        spec = importlib.util.spec_from_file_location("package_flight_search_harness", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        result = module.validate(ROOT)
        packaged = set(result["package_files"])
        for skill_name, (_, schema_name) in SKILLS.items():
            self.assertIn(
                f"skills/{skill_name}/scripts/resolve_harness.py", packaged
            )
            self.assertIn(
                f"flight_search_harness/schemas/requests/{schema_name}", packaged
            )

    def test_cli_exposes_only_the_simplified_lifecycle_surface(self) -> None:
        from flight_search_harness.__main__ import build_parser

        parser = build_parser()
        root_destinations = {action.dest for action in parser._actions}
        self.assertIn("worker", root_destinations)
        self.assertNotIn("browser", root_destinations)
        self.assertNotIn("mission_receipts", root_destinations)
        subparsers = next(
            action for action in parser._actions if action.dest == "command"
        )
        commands = subparsers.choices
        self.assertEqual(set(commands), {"start", "resume", "status", "metrics"})
        resume_destinations = {
            action.dest for action in commands["resume"]._actions
        }
        self.assertIn("receipt_batch", resume_destinations)

    def test_cli_reports_malformed_receipt_json_without_traceback(self) -> None:
        from flight_search_harness.__main__ import main

        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt_path = Path(temporary_directory) / "receipts.json"
            receipt_path.write_text('{"receipts": [', encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as raised:
                main(
                    [
                        "--runs-root",
                        str(Path(temporary_directory) / "runs"),
                        "--worker",
                        "external",
                        "resume",
                        "--run-id",
                        "example-run",
                        "--receipt-batch",
                        str(receipt_path),
                    ]
                )

        self.assertEqual(2, raised.exception.code)
        self.assertIn("Receipt submission is not valid JSON", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_json_output_falls_back_when_console_cannot_encode_yen(self) -> None:
        from flight_search_harness.__main__ import _print_json

        output = io.BytesIO()
        console = io.TextIOWrapper(output, encoding="gbk")
        with mock.patch("sys.stdout", console):
            _print_json({"price": "¥6,576"})
            console.flush()
            rendered = output.getvalue().decode("gbk")

        self.assertIn(r"\u00a5", rendered)
        self.assertNotIn("¥", rendered)


if __name__ == "__main__":
    unittest.main()
