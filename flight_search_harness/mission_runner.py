"""Checkpointed Search Mission Runner over the protocol 3 execution cursor."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from typing import Any, Mapping

from .contracts import (
    OUTCOME_VERSION,
    REQUEST_VERSION,
    HarnessProtocolError,
    result_price_freshness,
)
from .mission_contracts import ProductKernel, SearchWorker, validate_receipt_batch
from .mission_metrics import normalize_checkpoint_metrics
from .mission_store import MissionStore
from .request_values import validate_public_request
from .result_projection import summarize
from .run_store import RunBusyError, RunStore


class _ExistingRunCommandError(HarnessProtocolError):
    """A start command targeted an existing Run it may not replace."""


class SearchMissionHarness:
    """Drive one Product Kernel while the Run remains lifecycle authority."""

    def __init__(
        self,
        store: RunStore,
        product_kernels: Mapping[str, ProductKernel],
        *,
        lease_ttl_seconds: int = 900,
    ):
        self.store = store
        self.missions = MissionStore(store)
        self.kernels = dict(product_kernels)
        self.kernels_by_product = {
            kernel.product_id: kernel for kernel in self.kernels.values()
        }
        self.lease_ttl_seconds = max(1.0, float(lease_ttl_seconds))

    def run(
        self,
        command: dict[str, Any],
        worker: SearchWorker,
    ) -> dict[str, Any]:
        command = copy.deepcopy(command)
        if command.get("kind") == "start" and command.get("run_id") is None:
            command["run_id"] = self.store.new_run_id()
        try:
            kind = command.get("kind")
            if kind == "start":
                manifest, mission, kernel, transition = self._start(command)
            elif kind == "resume":
                run_id = command.get("run_id")
                if not isinstance(run_id, str):
                    raise HarnessProtocolError("Search Mission resume requires run_id")
                with self.store.run_lock(run_id):
                    manifest = self.store.recover_initializing(run_id)
                    kernel = self.kernels_by_product.get(manifest["product_id"])
                    if kernel is None:
                        raise HarnessProtocolError(
                            f"no Product Kernel for {manifest['product_id']}"
                        )
                    terminal = self._terminal_outcome(manifest)
                    if terminal is not None:
                        reopen = getattr(kernel, "reopen_expired_result", None)
                        reopened = bool(
                            terminal.get("kind") == "completed"
                            and terminal.get("price_freshness") == "expired"
                            and callable(reopen)
                            and reopen(self.store.product_directory(run_id))
                        )
                        if not reopened:
                            return terminal
                        manifest = self.store.load_manifest(run_id)
                        manifest["status"] = "running"
                        manifest["last_outcome"] = None
                        self.store.save_manifest(manifest)
                        self._append_event(
                            manifest,
                            "mission_price_refresh_reopened",
                            result_ref=manifest.get("result_ref"),
                        )
                    if self._initialization_incomplete(manifest):
                        manifest, mission = self._initialize_locked(manifest, kernel)
                    else:
                        mission = self.missions.load(run_id)
                        active = self.missions.active_lease(mission)
                        if active is not None and not active["items"]:
                            mission = self.missions.close_lease(mission)
                            active = None
                        replayed = self._replay_pending_effect_locked(
                            manifest, mission
                        )
                        if not replayed and active is None:
                            transition = kernel.advance(
                                self.store.product_directory(run_id), None
                            )
                            self._materialize_transition_locked(
                                manifest, mission, transition
                            )
                            manifest = self.store.load_manifest(run_id)
                            mission = self.missions.load(run_id)
                    transition = None
            else:
                raise HarnessProtocolError("Search Mission command.kind must be start or resume")
            return self._drive(manifest, mission, kernel, transition, worker)
        except RunBusyError as exc:
            return {
                "outcome_version": OUTCOME_VERSION,
                "kind": "blocked",
                "run_id": command.get("run_id"),
                "product_id": None,
                "reason": str(exc),
                "action_required": {"type": "wait_for_active_mission"},
            }
        except Exception as exc:
            run_id = command.get("run_id")
            if isinstance(exc, _ExistingRunCommandError):
                product_id = None
                if isinstance(run_id, str):
                    try:
                        product_id = str(self.store.load_manifest(run_id)["product_id"])
                    except Exception:
                        pass
                return self._invalid_command_outcome(
                    exc, run_id=run_id if isinstance(run_id, str) else None,
                    product_id=product_id,
                )
            if isinstance(exc, HarnessProtocolError) and "legacy" in str(exc):
                return {
                    "outcome_version": OUTCOME_VERSION,
                    "kind": "failed",
                    "run_id": run_id if isinstance(run_id, str) else None,
                    "product_id": None,
                    "error": {"code": "legacy_protocol", "message": str(exc)},
                }
            if isinstance(run_id, str) and self.store.run_directory(run_id).exists():
                try:
                    with self.store.run_lock(run_id):
                        manifest = self.store.load_manifest(run_id)
                        terminal = self._terminal_outcome(manifest)
                        if terminal is not None:
                            return terminal
                        if isinstance(exc, HarnessProtocolError):
                            if manifest.get("status") == "initializing":
                                outcome = {
                                    "outcome_version": OUTCOME_VERSION,
                                    "kind": "failed",
                                    "run_id": run_id,
                                    "product_id": manifest["product_id"],
                                    "error": {
                                        "code": "invalid_mission_request",
                                        "message": str(exc),
                                    },
                                }
                                manifest["status"] = "failed"
                                manifest["last_outcome"] = copy.deepcopy(outcome)
                                self.store.save_manifest(manifest)
                                return outcome
                            return self._invalid_command_outcome(
                                exc,
                                run_id=run_id,
                                product_id=str(manifest["product_id"]),
                            )
                        outcome = self._unfinished_outcome(
                            manifest,
                            "draft",
                            "Search Mission execution was interrupted after durable state "
                            "was saved and can be resumed.",
                            {"type": "resume_mission", "error": str(exc)},
                        )
                        self._append_event(
                            manifest,
                            "mission_interrupted",
                            message=str(exc),
                        )
                        return outcome
                except RunBusyError:
                    return {
                        "outcome_version": OUTCOME_VERSION,
                        "kind": "blocked",
                        "run_id": run_id,
                        "product_id": None,
                        "reason": "Run state changed while handling an exception.",
                        "action_required": {"type": "resume_current_lease"},
                    }
                except Exception:
                    return self._invalid_command_outcome(exc, run_id=run_id)
            return self._invalid_command_outcome(exc)

    @staticmethod
    def _invalid_command_outcome(
        exc: Exception,
        *,
        run_id: str | None = None,
        product_id: str | None = None,
    ) -> dict[str, Any]:
        if run_id is not None:
            return {
                "outcome_version": OUTCOME_VERSION,
                "kind": "blocked",
                "run_id": run_id,
                "product_id": product_id,
                "reason": str(exc),
                "action_required": {
                    "type": "correct_mission_command",
                    "error_code": "invalid_mission_command",
                },
            }
        return {
            "outcome_version": OUTCOME_VERSION,
            "kind": "failed",
            "run_id": None,
            "product_id": None,
            "error": {"code": "invalid_mission_command", "message": str(exc)},
        }

    @staticmethod
    def _worker_instance_id(worker: SearchWorker) -> str:
        value = getattr(worker, "worker_instance_id", None)
        if not isinstance(value, str) or not value:
            raise HarnessProtocolError("Worker must expose worker_instance_id")
        return value

    def _terminal_outcome(
        self, manifest: dict[str, Any]
    ) -> dict[str, Any] | None:
        status = manifest.get("status")
        if status not in {"completed", "failed"}:
            return None
        stored = manifest.get("last_outcome")
        if not isinstance(stored, dict):
            raise HarnessProtocolError(
                f"terminal Run {manifest.get('run_id')} has no persisted Outcome"
            )
        outcome = copy.deepcopy(stored)
        if status == "completed":
            freshness = result_price_freshness(
                str(stored.get("completed_at") or manifest["updated_at"]),
                price_valid_until=(
                    str(stored["price_valid_until"])
                    if stored.get("price_valid_until") is not None
                    else None
                ),
            )
            outcome.update(freshness)
        if status == "completed" and "result" not in outcome:
            result_ref = manifest.get("result_ref")
            if not isinstance(result_ref, str):
                raise HarnessProtocolError("completed Run has no Product final result")
            full_result = self.store.read_result(
                str(manifest["run_id"]), result_ref
            )
            outcome["result"] = summarize(
                str(manifest["product_id"]),
                full_result,
                full_result_ref=result_ref,
            )
        if status == "completed" and isinstance(outcome.get("result"), dict):
            outcome["result"].update(freshness)
        return outcome

    def _append_event(
        self, manifest: dict[str, Any], event_type: str, **details: Any
    ) -> dict[str, Any] | None:
        try:
            return self.store.append_event(manifest, event_type, **details)
        except Exception:
            # Events are diagnostic sidecars, never lifecycle authority.
            return None

    def _start(
        self, command: dict[str, Any]
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        ProductKernel,
        dict[str, Any] | None,
    ]:
        request = validate_public_request(command.get("request"))
        if request.get("request_version") != REQUEST_VERSION:
            raise HarnessProtocolError(f"request_version must be {REQUEST_VERSION}")
        shape = request.get("journey_shape")
        kernel = self.kernels.get(str(shape))
        if kernel is None:
            raise HarnessProtocolError(f"unsupported journey_shape: {shape}")
        run_id = str(command["run_id"])
        try:
            self.store.create(request, kernel.product_id, run_id=run_id)
        except HarnessProtocolError:
            if not self.store.run_directory(run_id).exists():
                raise
        with self.store.run_lock(run_id):
            manifest = self.store.load_manifest(run_id)
            if (
                manifest.get("request") != request
                or manifest.get("product_id") != kernel.product_id
                or manifest.get("journey_shape") != shape
            ):
                raise _ExistingRunCommandError(
                    "start command does not match the existing Run request"
                )
            terminal = self._terminal_outcome(manifest)
            if terminal is not None:
                mission = (
                    self.missions.load(run_id)
                    if self.missions.mission_path(run_id).is_file()
                    else {}
                )
                return manifest, mission, kernel, None
            if manifest.get("status") != "initializing":
                raise _ExistingRunCommandError(
                    "start cannot replace an existing non-terminal Run; use resume"
                )
            manifest, mission = self._initialize_locked(manifest, kernel)
            return manifest, mission, kernel, None

    def _initialization_incomplete(self, manifest: dict[str, Any]) -> bool:
        run_id = str(manifest["run_id"])
        session_ref = str(manifest["product_session_ref"])
        return (
            manifest.get("status") == "initializing"
            or not self.missions.mission_path(run_id).is_file()
            or not (self.store.run_directory(run_id) / session_ref).is_file()
        )

    def _initialize_locked(
        self,
        manifest: dict[str, Any],
        kernel: ProductKernel,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Finish initialization while the caller holds the Run lock."""

        run_id = str(manifest["run_id"])
        if self.missions.mission_path(run_id).is_file():
            mission = self.missions.load(run_id)
        else:
            mission = self.missions.create(manifest)
        active = self.missions.active_lease(mission)
        if active is not None and not active["items"]:
            mission = self.missions.close_lease(mission)
            active = None
        transition: dict[str, Any] | None = None
        replayed = self._replay_pending_effect_locked(manifest, mission)
        if not replayed and active is None:
            session_path = self.store.run_directory(run_id) / str(
                manifest["product_session_ref"]
            )
            transition = (
                kernel.advance(self.store.product_directory(run_id), None)
                if session_path.is_file()
                else kernel.start(
                    copy.deepcopy(manifest["request"]),
                    self.store.product_directory(run_id),
                )
            )
        manifest["status"] = "running"
        manifest["last_outcome"] = None
        self.store.save_manifest(manifest)
        if transition is not None:
            self._materialize_transition_locked(manifest, mission, transition)
        manifest = self.store.load_manifest(run_id)
        mission = self.missions.load(run_id)
        self._append_event(
            manifest,
            "mission_initialized",
            mission_ref=manifest["mission_ref"],
            product_id=kernel.product_id,
        )
        return manifest, mission

    def _materialize_transition_locked(
        self,
        manifest: dict[str, Any],
        mission: dict[str, Any],
        transition: dict[str, Any],
    ) -> dict[str, Any]:
        effect = transition.get("effect") if isinstance(transition, dict) else None
        if not isinstance(effect, dict):
            raise HarnessProtocolError("Product Kernel transition has no effect")
        self.missions.stage_effect(mission, effect)
        self._replay_pending_effect_locked(manifest, mission)
        return {"kind": str(effect["kind"])}

    def _replay_pending_effect_locked(
        self,
        manifest: dict[str, Any],
        mission: dict[str, Any],
    ) -> bool:
        effect = self.missions.pending_effect(mission)
        if effect is None:
            return False
        active = self.missions.active_lease(mission)
        if active is None or effect.get("kind") != "need_work":
            self._materialize_effect(manifest, mission, effect)
            mission = self.missions.load(str(manifest["run_id"]))
        try:
            self.missions.clear_pending_effect(mission)
        except Exception:
            # Once materialized, retaining the exact cursor is safe: the next
            # resume replays the same idempotent effect before advancing.
            pass
        return True

    def _drive(
        self,
        manifest: dict[str, Any],
        mission: dict[str, Any],
        kernel: ProductKernel,
        transition: dict[str, Any] | None,
        worker: SearchWorker,
    ) -> dict[str, Any]:
        worker_instance_id = self._worker_instance_id(worker)
        owned_claim_id: str | None = None
        with self.store.run_lock(manifest["run_id"]):
            manifest = self.store.load_manifest(manifest["run_id"])
            terminal = self._terminal_outcome(manifest)
            if terminal is not None:
                return terminal
            mission = self.missions.load(manifest["run_id"])
            active = self.missions.active_lease(mission)
            if active is not None and not active["items"]:
                mission = self.missions.close_lease(mission)
                active = None
            if self._replay_pending_effect_locked(manifest, mission):
                manifest = self.store.load_manifest(manifest["run_id"])
                terminal = self._terminal_outcome(manifest)
                if terminal is not None:
                    return terminal
                mission = self.missions.load(manifest["run_id"])
                active = self.missions.active_lease(mission)
                transition = None
            if (
                active is None
                and transition is None
                and manifest.get("status") in {"draft", "blocked"}
                and isinstance(manifest.get("last_outcome"), dict)
            ):
                return copy.deepcopy(manifest["last_outcome"])
            elif active is None and transition is None:
                transition = kernel.advance(
                    self.store.product_directory(manifest["run_id"]), None
                )
                self._materialize_transition_locked(manifest, mission, transition)
                manifest = self.store.load_manifest(manifest["run_id"])
                terminal = self._terminal_outcome(manifest)
                if terminal is not None:
                    return terminal
                mission = self.missions.load(manifest["run_id"])
                active = self.missions.active_lease(mission)
                transition = None
            if active is not None:
                step = {"kind": "lease", "lease": active, "lease_ref": mission["active_lease_ref"]}
            else:
                if not isinstance(transition, dict):
                    stored = manifest.get("last_outcome")
                    if manifest.get("status") in {"draft", "blocked"} and isinstance(
                        stored, dict
                    ):
                        return copy.deepcopy(stored)
                    raise HarnessProtocolError("Product Kernel transition is missing")
                effect = transition.get("effect")
                if not isinstance(effect, dict):
                    raise HarnessProtocolError("Product Kernel transition has no effect")
                step = self._materialize_effect(manifest, mission, effect)
            if step.get("kind") == "lease":
                mission = self.missions.load(manifest["run_id"])
                lease = self.missions.claim_lease(
                    mission,
                    worker_instance_id,
                    ttl_seconds=self.lease_ttl_seconds,
                )
                step["lease"] = lease
                step["lease_ref"] = mission["active_lease_ref"]
                owned_claim_id = str(lease["claim"]["claim_id"])
                manifest["status"] = "running"
                manifest["last_outcome"] = None
                self.store.save_manifest(manifest)
                self._append_event(
                    manifest,
                    "mission_lease_claimed",
                    lease_id=lease["lease_id"],
                    worker_instance_id=worker_instance_id,
                    claim_id=lease["claim"]["claim_id"],
                    expires_at=lease["claim"]["expires_at"],
                )
        if step.get("kind") != "lease":
            if step.get("kind") in {"complete", "draft", "blocked"} and isinstance(
                step.get("outcome"), dict
            ):
                return copy.deepcopy(step["outcome"])
            return step
        mission = self.missions.load(manifest["run_id"])
        trusted_checkpoint_outcome: dict[str, Any] | None = None

        def checkpoint(batch: dict[str, Any]) -> dict[str, Any]:
            nonlocal mission, trusted_checkpoint_outcome, owned_claim_id
            with self.store.run_lock(manifest["run_id"]):
                latest_manifest = self.store.load_manifest(manifest["run_id"])
                mission = self.missions.load(manifest["run_id"])
                mission, next_step = self._checkpoint(
                    latest_manifest,
                    mission,
                    kernel,
                    batch,
                    worker_instance_id=worker_instance_id,
                )
                if next_step.get("kind") == "lease":
                    lease = self.missions.claim_lease(
                        self.missions.load(manifest["run_id"]),
                        worker_instance_id,
                        ttl_seconds=self.lease_ttl_seconds,
                    )
                    next_step["lease"] = lease
                    next_step["lease_ref"] = mission["active_lease_ref"]
                    owned_claim_id = str(lease["claim"]["claim_id"])
                    self._append_event(
                        self.store.load_manifest(manifest["run_id"]),
                        "mission_lease_claimed",
                        lease_id=lease["lease_id"],
                        worker_instance_id=worker_instance_id,
                        claim_id=lease["claim"]["claim_id"],
                        expires_at=lease["claim"]["expires_at"],
                    )
                elif next_step.get("kind") in {"complete", "draft", "blocked"} and isinstance(
                    next_step.get("outcome"), dict
                ):
                    trusted_checkpoint_outcome = copy.deepcopy(next_step["outcome"])
            return next_step

        context = {
            "run_id": manifest["run_id"],
            "run_directory": str(self.store.run_directory(manifest["run_id"])),
            "lease_ref": mission["active_lease_ref"],
        }
        stop_heartbeat = threading.Event()

        def maintain_lease() -> None:
            interval = max(0.1, min(30.0, self.lease_ttl_seconds / 3))
            while not stop_heartbeat.wait(interval):
                try:
                    with self.store.run_lock(manifest["run_id"]):
                        current_mission = self.missions.load(manifest["run_id"])
                        current = self.missions.active_lease(current_mission)
                        if current is None:
                            return
                        claim = current.get("claim")
                        if not isinstance(claim, dict) or claim.get(
                            "worker_instance_id"
                        ) != worker_instance_id or claim.get("claim_id") != owned_claim_id:
                            return
                        self.missions.heartbeat_lease(
                            current_mission,
                            worker_instance_id,
                            str(owned_claim_id),
                            ttl_seconds=self.lease_ttl_seconds,
                        )
                except RunBusyError:
                    continue
                except Exception:
                    return

        heartbeat_thread = threading.Thread(
            target=maintain_lease,
            name=f"mission-heartbeat-{manifest['run_id']}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            worker_exit = worker.run(step["lease"], checkpoint, context)
        except Exception as exc:
            return self._settle_worker_exception(
                manifest["run_id"],
                worker_instance_id,
                owned_claim_id,
                exc,
            )
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1)
        if not isinstance(worker_exit, dict):
            return self._settle_worker_exception(
                manifest["run_id"],
                worker_instance_id,
                owned_claim_id,
                HarnessProtocolError("Worker exit must be an object"),
            )
        kind = worker_exit.get("kind")
        if trusted_checkpoint_outcome is not None:
            expected_kind = (
                "complete"
                if trusted_checkpoint_outcome.get("kind") == "completed"
                else trusted_checkpoint_outcome.get("kind")
            )
            if (
                kind == expected_kind
                and worker_exit.get("outcome") == trusted_checkpoint_outcome
            ):
                return copy.deepcopy(trusted_checkpoint_outcome)
            return self._settle_worker_exception(
                manifest["run_id"],
                worker_instance_id,
                owned_claim_id,
                HarnessProtocolError(
                    "Worker altered the Product Kernel checkpoint Outcome"
                ),
            )
        if kind == "complete":
            return self._settle_worker_exception(
                manifest["run_id"],
                worker_instance_id,
                owned_claim_id,
                HarnessProtocolError(
                    "Worker cannot produce a completed Outcome; completion must "
                    "come from the Product Kernel checkpoint"
                ),
            )
        if kind in {"yield", "blocked", "draft"}:
            with self.store.run_lock(manifest["run_id"]):
                current_manifest = self.store.load_manifest(manifest["run_id"])
                terminal = self._terminal_outcome(current_manifest)
                if terminal is not None:
                    return terminal
                current_mission = self.missions.load(manifest["run_id"])
                current = self.missions.active_lease(current_mission)
                claim = current.get("claim") if isinstance(current, dict) else None
                if not isinstance(claim, dict) or claim.get(
                    "worker_instance_id"
                ) != worker_instance_id or claim.get("claim_id") != owned_claim_id:
                    return {
                        "outcome_version": OUTCOME_VERSION,
                        "kind": "blocked",
                        "run_id": current_manifest["run_id"],
                        "product_id": current_manifest["product_id"],
                        "reason": "Worker claim was superseded before exit handling.",
                        "action_required": {"type": "resume_current_lease"},
                    }
                # External handoff keeps the fence; browser exits release it.
                if kind != "yield":
                    self.missions.release_lease(
                        current_mission,
                        worker_instance_id,
                        str(owned_claim_id),
                    )
                outcome_kind = "draft" if kind == "draft" else "blocked"
                outcome = self._unfinished_outcome(
                    current_manifest,
                    outcome_kind,
                    str(
                        worker_exit.get("reason")
                        or "Search Mission Worker yielded or blocked"
                    ),
                    copy.deepcopy(worker_exit.get("action_required")),
                )
                self._append_event(
                    current_manifest,
                    "mission_yielded",
                    worker_instance_id=worker_instance_id,
                    outcome_kind=outcome_kind,
                )
                return outcome
        if kind == "failed":
            retryable = bool(worker_exit.get("retryable", False))
            with self.store.run_lock(manifest["run_id"]):
                current_manifest = self.store.load_manifest(manifest["run_id"])
                terminal = self._terminal_outcome(current_manifest)
                if terminal is not None:
                    return terminal
                current_mission = self.missions.load(manifest["run_id"])
                current = self.missions.active_lease(current_mission)
                claim = current.get("claim") if isinstance(current, dict) else None
                if not isinstance(claim, dict) or claim.get(
                    "worker_instance_id"
                ) != worker_instance_id or claim.get("claim_id") != owned_claim_id:
                    return {
                        "outcome_version": OUTCOME_VERSION,
                        "kind": "blocked",
                        "run_id": current_manifest["run_id"],
                        "product_id": current_manifest["product_id"],
                        "reason": "Worker claim was superseded before failure handling.",
                        "action_required": {"type": "resume_current_lease"},
                    }
                if retryable:
                    self.missions.release_lease(
                        current_mission,
                        worker_instance_id,
                        str(owned_claim_id),
                    )
                    return self._unfinished_outcome(
                        current_manifest,
                        "blocked",
                        "Search Mission Worker failed with a retryable error.",
                        {
                            "type": "retry_worker",
                            "error": copy.deepcopy(worker_exit.get("error")),
                        },
                    )
                outcome = {
                    "outcome_version": OUTCOME_VERSION,
                    "kind": "failed",
                    "run_id": current_manifest["run_id"],
                    "product_id": current_manifest["product_id"],
                    "error": copy.deepcopy(worker_exit.get("error"))
                    if isinstance(worker_exit.get("error"), dict)
                    else {
                        "code": "mission_worker_failed",
                        "message": str(worker_exit.get("error")),
                    },
                }
                current_manifest["status"] = "failed"
                current_manifest["last_outcome"] = copy.deepcopy(outcome)
                self.store.save_manifest(current_manifest)
                self._append_event(current_manifest, "mission_worker_failed")
                return outcome
        return self._settle_worker_exception(
            manifest["run_id"],
            worker_instance_id,
            owned_claim_id,
            HarnessProtocolError(f"unsupported Search Mission Worker exit: {kind}"),
        )

    def _settle_worker_exception(
        self,
        run_id: str,
        worker_instance_id: str,
        claim_id: str | None,
        exc: Exception,
    ) -> dict[str, Any]:
        """Fence Worker error handling with the claim that began execution."""

        try:
            with self.store.run_lock(run_id):
                manifest = self.store.load_manifest(run_id)
                terminal = self._terminal_outcome(manifest)
                if terminal is not None:
                    return terminal
                mission = self.missions.load(run_id)
                active = self.missions.active_lease(mission)
                claim = active.get("claim") if isinstance(active, dict) else None
                owns_claim = (
                    isinstance(claim, dict)
                    and claim.get("worker_instance_id") == worker_instance_id
                    and claim.get("claim_id") == claim_id
                )
                if owns_claim:
                    self.missions.release_lease(
                        mission,
                        worker_instance_id,
                        str(claim_id),
                    )
                    if isinstance(exc, HarnessProtocolError):
                        return self._invalid_command_outcome(
                            exc,
                            run_id=run_id,
                            product_id=str(manifest["product_id"]),
                        )
                    outcome = self._unfinished_outcome(
                        manifest,
                        "draft",
                        "Search Mission Worker was interrupted after durable state "
                        "was saved and can be resumed.",
                        {"type": "resume_mission", "error": str(exc)},
                    )
                    self._append_event(
                        manifest,
                        "mission_worker_interrupted",
                        worker_instance_id=worker_instance_id,
                        message=str(exc),
                    )
                    return outcome
                stored = manifest.get("last_outcome")
                if manifest.get("status") in {"draft", "blocked"} and isinstance(
                    stored, dict
                ):
                    return copy.deepcopy(stored)
                return {
                    "outcome_version": OUTCOME_VERSION,
                    "kind": "blocked",
                    "run_id": run_id,
                    "product_id": manifest["product_id"],
                    "reason": "Worker claim was superseded before error handling.",
                    "action_required": {"type": "resume_current_lease"},
                }
        except RunBusyError:
            return {
                "outcome_version": OUTCOME_VERSION,
                "kind": "blocked",
                "run_id": run_id,
                "product_id": None,
                "reason": "Run state changed while handling a Worker error.",
                "action_required": {"type": "resume_current_lease"},
            }

    def _checkpoint(
        self,
        manifest: dict[str, Any],
        mission: dict[str, Any],
        kernel: ProductKernel,
        batch: dict[str, Any],
        *,
        worker_instance_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        batch = validate_receipt_batch(
            batch, default_worker_instance_id=worker_instance_id
        )
        lease = self.missions.active_lease(mission)
        if lease is None:
            raise HarnessProtocolError("Search Mission checkpoint has no active Lease")
        if batch["lease_id"] != lease["lease_id"]:
            raise HarnessProtocolError("Search Mission checkpoint targets a different Lease")
        if batch["mission_revision"] != lease["mission_revision"]:
            raise HarnessProtocolError("Search Mission checkpoint revision mismatch")
        claim = lease.get("claim")
        if not isinstance(claim, dict) or (
            batch["worker_instance_id"] != worker_instance_id
            or claim.get("worker_instance_id") != worker_instance_id
            or batch["claim_id"] != claim.get("claim_id")
        ):
            raise HarnessProtocolError("Search Mission checkpoint claim does not own the Lease")
        lease = self.missions.heartbeat_lease(
            mission,
            worker_instance_id,
            batch["claim_id"],
            ttl_seconds=self.lease_ttl_seconds,
        )
        pending = {
            (item["item_id"], item["attempt_number"]): item
            for item in lease["items"]
        }
        entries = []
        receipts: dict[str, dict[str, Any]] = {}
        for raw_receipt in batch["receipts"]:
            identity = (raw_receipt["item_id"], raw_receipt["attempt_number"])
            if identity not in pending:
                raise HarnessProtocolError("Receipt does not target a pending Work Attempt")
            item = self.missions.read_item(mission, raw_receipt["item_id"])
            receipt, _, _ = self.missions.write_raw_receipt(
                mission, raw_receipt
            )
            entries.append({"item": item, "receipt": receipt})
            receipts[receipt["item_id"]] = receipt
        transition = kernel.advance(
            self.store.product_directory(manifest["run_id"]),
            {"kind": "receipt_batch", "entries": entries},
        )
        effect = transition.get("effect")
        if not isinstance(effect, dict):
            raise HarnessProtocolError("Product Kernel transition has no effect")
        decisions = {
            result["item_id"]: result
            for result in transition.get("receipt_results", [])
            if isinstance(result, dict) and isinstance(result.get("item_id"), str)
        }
        for identifier, receipt in receipts.items():
            decision = decisions.get(identifier, {})
            self._append_event(
                manifest,
                "mission_receipt_decided",
                receipt_id=receipt.get("receipt_id"),
                item_id=identifier,
                attempt_number=receipt.get("attempt_number"),
                decision=decision.get("status"),
                error=decision.get("error"),
            )
        mission, lease = self.missions.apply_admissions(
            mission, receipts, decisions
        )
        checkpoint_id = hashlib.sha256(
            json.dumps(
                {
                    "lease_id": batch["lease_id"],
                    "receipt_ids": sorted(
                        receipt["receipt_id"] for receipt in receipts.values()
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            checkpoint_metrics = normalize_checkpoint_metrics(
                batch.get("metrics"), list(receipts.values())
            )
            self.missions.record_metrics(
                mission, checkpoint_metrics, checkpoint_id=checkpoint_id
            )
        except Exception:
            pass
        if lease["items"]:
            return mission, {"kind": "lease", "lease": lease}
        self.missions.stage_effect(
            mission,
            effect,
            mission_revision=int(mission["revision"]) + 1,
        )
        mission = self.missions.close_lease(mission)
        self._append_event(
            manifest,
            "mission_checkpoint_committed",
            mission_revision=mission["revision"],
            receipt_ids=sorted(receipt["receipt_id"] for receipt in receipts.values()),
        )
        step = self._materialize_effect(manifest, mission, effect)
        try:
            self.missions.clear_pending_effect(
                self.missions.load(manifest["run_id"])
            )
        except Exception:
            pass
        return self.missions.load(manifest["run_id"]), step

    def _materialize_effect(
        self,
        manifest: dict[str, Any],
        mission: dict[str, Any],
        effect: dict[str, Any],
    ) -> dict[str, Any]:
        kind = effect.get("kind")
        if kind == "need_work":
            mission, lease = self.missions.publish_lease(
                mission, list(effect.get("items", []))
            )
            return {
                "kind": "lease",
                "lease": lease,
                "lease_ref": mission["active_lease_ref"],
            }
        if kind == "complete":
            if self.missions.active_lease(mission) is not None:
                raise HarnessProtocolError("Product Kernel cannot complete with an active Lease")
            result = effect.get("result")
            if not isinstance(result, dict):
                raise HarnessProtocolError("complete effect requires result")
            result_ref = self.store.write_product_final_result(manifest["run_id"], result)
            summary = summarize(manifest["product_id"], result, full_result_ref=result_ref)
            freshness = result_price_freshness(
                price_observations=(
                    result.get("price_observations")
                    if isinstance(result.get("price_observations"), list)
                    else None
                )
            )
            summary.update(freshness)
            manifest = self.store.load_manifest(manifest["run_id"])
            manifest["status"] = "completed"
            manifest["result_ref"] = result_ref
            outcome = {
                "outcome_version": OUTCOME_VERSION,
                "kind": "completed",
                "run_id": manifest["run_id"],
                "product_id": manifest["product_id"],
                "result_ref": result_ref,
                "result": copy.deepcopy(summary),
                **freshness,
            }
            manifest["last_outcome"] = {
                key: copy.deepcopy(value)
                for key, value in outcome.items()
                if key != "result"
            }
            self.store.save_manifest(manifest)
            self._append_event(
                manifest,
                "mission_completed",
                result_ref=result_ref,
                mission_revision=mission["revision"],
            )
            return {"kind": "complete", "outcome": outcome}
        if kind in {"draft", "blocked"}:
            return {
                "kind": kind,
                "outcome": self._unfinished_outcome(
                    manifest,
                    kind,
                    str(effect.get("reason") or f"Search Mission is {kind}."),
                    copy.deepcopy(effect.get("action_required")),
                ),
            }
        raise HarnessProtocolError(f"unsupported Product Kernel effect: {kind}")

    def _unfinished_outcome(
        self,
        manifest: dict[str, Any],
        kind: str,
        reason: str,
        action_required: Any,
    ) -> dict[str, Any]:
        if kind not in {"draft", "blocked"}:
            raise HarnessProtocolError("unfinished Outcome must be draft or blocked")
        outcome = {
            "outcome_version": OUTCOME_VERSION,
            "kind": kind,
            "run_id": manifest["run_id"],
            "product_id": manifest["product_id"],
            "reason": reason,
            "action_required": copy.deepcopy(action_required),
        }
        manifest["status"] = kind
        manifest["last_outcome"] = copy.deepcopy(outcome)
        self.store.save_manifest(manifest)
        return outcome
