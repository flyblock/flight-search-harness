"""Fresh raw-observation cache keyed by exact worker query fingerprints."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import HarnessProtocolError
from .run_store import read_json, write_json_atomic


class ResearchCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @staticmethod
    def _token(scope: str, fingerprint: str) -> str:
        return hashlib.sha256(f"{scope}:{fingerprint}".encode("utf-8")).hexdigest()

    def get(self, scope: str, fingerprint: str) -> dict[str, Any] | None:
        target = self.root / f"{self._token(scope, fingerprint)}.json"
        if not target.exists():
            return None
        try:
            value = read_json(target)
        except (OSError, HarnessProtocolError):
            return None
        if value.get("scope") != scope or value.get("query_fingerprint") != fingerprint:
            return None
        try:
            expires = datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            return None
        if expires <= datetime.now(timezone.utc):
            return None
        payload = value.get("payload")
        if not isinstance(payload, dict):
            return None
        payload_hash = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if value.get("payload_hash") != payload_hash:
            return None
        return copy.deepcopy(payload)

    def put(
        self,
        scope: str,
        fingerprint: str,
        payload: dict[str, Any],
        *,
        ttl_seconds: int,
    ) -> None:
        if payload.get("kind") not in {"observations", "observation_bundle"}:
            return
        cached_payload = _without_admission_identity(payload)
        now = datetime.now(timezone.utc)
        target = self.root / f"{self._token(scope, fingerprint)}.json"
        write_json_atomic(
            target,
            {
                "cache_version": "1.0",
                "scope": scope,
                "query_fingerprint": fingerprint,
                "cached_at": now.isoformat(timespec="seconds"),
                "expires_at": (now + timedelta(seconds=max(1, ttl_seconds))).isoformat(timespec="seconds"),
                "payload_hash": hashlib.sha256(
                    json.dumps(
                        cached_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "payload": cached_payload,
            },
        )


def _without_admission_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Cache Provider observations, never task- or admission-bound identity."""

    result = copy.deepcopy(payload)
    for key in ("bundle_id", "task_id", "request_id", "query_id", "receipt_id"):
        result.pop(key, None)
    product_payload = result.get("product_payload")
    if not isinstance(product_payload, dict):
        return result
    for key in ("bundle_id", "task_id", "request_id", "query_id", "receipt_id"):
        product_payload.pop(key, None)
    groups = product_payload.get("search_results")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            for key in ("task_id", "query_id", "request_id", "parent_id", "receipt_id"):
                group.pop(key, None)
    return result
