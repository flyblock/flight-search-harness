"""Structured Codex agent Adapter over the documented app-server JSONL transport."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from ..contracts import HARNESS_VERSION, HarnessProtocolError
from ..run_store import read_json, write_json_atomic


CHOICE_SCHEMA = {
    "type": "object",
    "required": ["action_key", "reason"],
    "properties": {
        "action_key": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}


# Product evidence contracts are intentionally richer than the JSON Schema
# subset accepted by Structured Outputs.  Keep that transport constraint at
# the Browser Adapter seam: the model returns the domain payload as JSON text,
# then the Product Adapter performs the authoritative evidence validation.
PAYLOAD_ENVELOPE_SCHEMA = {
    "type": "object",
    "required": ["payload_json"],
    "properties": {
        "payload_json": {
            "type": "string",
            "description": "A JSON-serialized object matching the supplied domain schema.",
        }
    },
    "additionalProperties": False,
}

BATCH_PAYLOAD_ENVELOPE_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["item_id", "payload_json"],
                "properties": {
                    "item_id": {"type": "string"},
                    "payload_json": {"type": "string"},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}


class _ActionRequired(RuntimeError):
    def __init__(self, request: dict[str, Any]):
        super().__init__(str(request.get("method") or "Codex action required"))
        self.request = request


class _JsonRpcProcess:
    def __init__(self, command: Sequence[str], *, timeout_seconds: float):
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creation_flags,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise HarnessProtocolError("Codex app-server did not expose stdio")
        self.timeout_seconds = timeout_seconds
        self.next_id = 1
        self.messages: queue.Queue[Any] = queue.Queue()
        self.pending_notifications: list[dict[str, Any]] = []
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError as exc:
                    self.messages.put(exc)
        finally:
            self.messages.put(EOFError("Codex app-server stdout closed"))

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None and not stream.closed:
                stream.close()

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = self._receive_raw(deadline)
            if message.get("id") == request_id and "method" not in message:
                if message.get("error") is not None:
                    raise HarnessProtocolError(
                        f"Codex app-server {method} failed: {message['error']}"
                    )
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            if "id" in message and "method" in message:
                raise _ActionRequired(message)
            self.pending_notifications.append(message)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def receive(self, deadline: float | None = None) -> dict[str, Any]:
        if self.pending_notifications:
            return self.pending_notifications.pop(0)
        return self._receive_raw(deadline)

    def _receive_raw(self, deadline: float | None = None) -> dict[str, Any]:
        deadline = deadline or (time.monotonic() + self.timeout_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex app-server response timed out")
        try:
            value = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("Codex app-server response timed out") from exc
        if isinstance(value, Exception):
            raise HarnessProtocolError(f"Codex app-server stream failed: {value}") from value
        if not isinstance(value, dict):
            raise HarnessProtocolError("Codex app-server emitted a non-object message")
        return value


class CodexAppServerBrowserAdapter:
    """Use one persisted Codex thread for decisions and browser observations."""

    adapter_id = "codex-app-server-v1"

    def __init__(
        self,
        *,
        cwd: str | Path,
        command: Sequence[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        timeout_seconds: float = 900,
    ):
        self.cwd = Path(cwd).resolve()
        self.command = list(command or ["codex", "app-server", "--listen", "stdio://"])
        self.model = model
        self.effort = effort
        self.timeout_seconds = timeout_seconds

    def choose(
        self, control: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        actions = control.get("actions", [])
        if not isinstance(actions, list) or not actions:
            return {
                "kind": "draft",
                "reason": "Product Adapter published no action and cannot finalize.",
            }
        if len(actions) == 1 and actions[0].get("automatic") is True:
            return {
                **copy.deepcopy(actions[0]),
                "reason": "The product proved this deterministic control transition.",
            }
        prompt = (
            "You are the decision Worker for the Flight Search Harness. Select exactly one "
            "published action_key. Respect product hard states; rankings are advisory. Do not "
            "perform browser work in this turn. Return only the structured choice.\n\n"
            + json.dumps(
                {
                    "product_id": control.get("product_id"),
                    "active_hypothesis_id": control.get("active_hypothesis_id"),
                    "has_complete_result": control.get("has_complete_result"),
                    "can_finalize": control.get("can_finalize"),
                    "finalization_issues": control.get("finalization_issues"),
                    "actions": actions,
                },
                ensure_ascii=False,
            )
        )
        operation_key = "choose:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        result = self._run_structured(
            context,
            operation_key=operation_key,
            prompt=prompt,
            output_schema=CHOICE_SCHEMA,
        )
        if result.get("kind") in {"blocked", "failed"}:
            return result
        selected_key = result.get("action_key")
        action = next(
            (
                item
                for item in actions
                if isinstance(item, dict) and item.get("action_key") == selected_key
            ),
            None,
        )
        if action is None:
            raise HarnessProtocolError(
                f"Codex selected unpublished action_key: {selected_key}"
            )
        return {
            **action,
            "reason": result["reason"],
            "_browser_state": result["_browser_state"],
        }

    def execute(
        self, work: dict[str, Any], action_id: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        output_schema = work.get("attempt_schema")
        if not isinstance(output_schema, dict):
            raise HarnessProtocolError("browser work is missing attempt_schema")
        prompt = (
            "Execute this bounded travel browser task using the visible browser tools available "
            "to you. Do not edit repository files. Preserve only facts visible in the executed "
            "scope described by work_projection, including its Provider, query-scope ladder, "
            "corridor, dates, traveler party, baggage, overnight, and self-transfer constraints. "
            "Treat evidence_rule, provider_batch_protocol, temporal_execution, and "
            "temporal_research_option as binding execution instructions when present. "
            "Unknown, research-only, no-result, and payable observations must remain "
            "distinct. Build one domain object matching required_payload_schema exactly, serialize "
            "that object as JSON, and return the serialized text in payload_json. Do not put "
            "markdown or commentary inside payload_json. If the browser runtime cannot initialize "
            "or cannot load any requested HTTP(S) source page, do not fabricate an unknown "
            "observation with about:blank. Instead encode an object with kind=blocked, a concrete "
            "reason, and action_required.type=retry_browser_runtime in payload_json.\n\n"
            + json.dumps(
                {
                    "action_id": action_id,
                    "product_id": work.get("product_id"),
                    "task_id": work.get("task_id"),
                    "work_projection": work,
                    "required_payload_schema": output_schema,
                },
                ensure_ascii=False,
            )
        )
        result = self._run_structured(
            context,
            operation_key=f"execute:{action_id}",
            prompt=prompt,
            output_schema=PAYLOAD_ENVELOPE_SCHEMA,
        )
        if result.get("kind") in {"blocked", "failed"}:
            return result
        payload_json = result.get("payload_json")
        if isinstance(payload_json, str):
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise HarnessProtocolError(
                    "Codex payload_json is not valid JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise HarnessProtocolError("Codex payload_json must encode an object")
            return {**payload, "_browser_state": result["_browser_state"]}

        raise HarnessProtocolError("Codex structured result is missing payload_json")

    def execute_batch(
        self, requests: list[dict[str, Any]], context: dict[str, Any]
    ) -> dict[str, Any]:
        tasks = []
        for request in requests:
            work = request.get("work")
            if not isinstance(work, dict) or not isinstance(
                work.get("attempt_schema"), dict
            ):
                raise HarnessProtocolError("batch browser work is missing attempt_schema")
            tasks.append(
                {
                    "item_id": request["item_id"],
                    "action_id": request["action_id"],
                    "product_id": work.get("product_id"),
                    "task_id": work.get("task_id"),
                    "work_projection": work,
                    "required_payload_schema": work.get("attempt_schema"),
                }
            )
        prompt = (
            "Execute this compatible travel-research batch. Reuse one Provider interaction "
            "when the supplied work projections share a Provider, date window, and query shape; "
            "otherwise keep the tasks distinct. Return exactly one payload_json per item_id. "
            "Honor each projection's evidence, Provider-batch, and temporal protocol exactly. "
            "Each payload must satisfy that item's required_payload_schema. Do not invent facts "
            "and do not edit repository files.\n\n"
            + json.dumps({"tasks": tasks}, ensure_ascii=False)
        )
        operation_key = "execute-batch:" + hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        result = self._run_structured(
            context,
            operation_key=operation_key,
            prompt=prompt,
            output_schema=BATCH_PAYLOAD_ENVELOPE_SCHEMA,
        )
        if result.get("kind") in {"blocked", "failed", "draft"}:
            return result
        raw_items = result.get("items")
        if not isinstance(raw_items, list):
            raise HarnessProtocolError("Codex batch result is missing items")
        attempts: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                raise HarnessProtocolError("Codex batch item must be an object")
            try:
                payload = json.loads(str(item.get("payload_json")))
            except json.JSONDecodeError as exc:
                raise HarnessProtocolError("Codex batch payload_json is invalid") from exc
            if not isinstance(payload, dict):
                raise HarnessProtocolError("Codex batch payload_json must encode an object")
            attempts[str(item.get("item_id"))] = payload
        response = {
            "attempts_by_item": attempts,
            "_browser_state": result.get("_browser_state", {}),
        }
        if isinstance(result.get("_usage"), dict):
            response["_usage"] = copy.deepcopy(result["_usage"])
        return response

    def _state_path(self, context: dict[str, Any]) -> Path:
        run_directory = context.get("run_directory")
        if not isinstance(run_directory, str):
            raise HarnessProtocolError("Browser context is missing run_directory")
        return Path(run_directory) / "browser" / "codex-app-server.json"

    def _load_state(self, context: dict[str, Any]) -> dict[str, Any]:
        path = self._state_path(context)
        if path.exists():
            return read_json(path)
        embedded = context.get("browser_state")
        return dict(embedded) if isinstance(embedded, dict) else {}

    def _save_state(self, context: dict[str, Any], state: dict[str, Any]) -> None:
        write_json_atomic(self._state_path(context), state)

    def _run_structured(
        self,
        context: dict[str, Any],
        *,
        operation_key: str,
        prompt: str,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        state = self._load_state(context)
        if (
            state.get("operation_key") == operation_key
            and state.get("phase") == "completed"
            and isinstance(state.get("result"), dict)
        ):
            return {
                **state["result"],
                "_browser_state": self._public_state(state),
            }
        client = _JsonRpcProcess(self.command, timeout_seconds=self.timeout_seconds)
        try:
            client.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "flight_search_harness",
                        "title": "Flight Search Harness",
                        "version": HARNESS_VERSION,
                    }
                },
            )
            client.notify("initialized", {})

            def start_thread() -> tuple[str, dict[str, Any]]:
                params: dict[str, Any] = {
                    "cwd": str(self.cwd),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "serviceName": "flight_search_harness",
                }
                if self.model:
                    params["model"] = self.model
                started = client.request("thread/start", params)
                new_thread = started.get("thread", {})
                new_thread_id = new_thread.get("id")
                if not isinstance(new_thread_id, str) or not new_thread_id:
                    raise HarnessProtocolError("thread/start returned no thread id")
                return new_thread_id, new_thread

            thread_id = state.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                try:
                    resumed = client.request("thread/resume", {"threadId": thread_id})
                    thread = resumed.get("thread", {})
                except HarnessProtocolError as exc:
                    active_writer = "already has an active writer" in str(exc).lower()
                    recovering_turn = (
                        state.get("operation_key") == operation_key
                        and state.get("phase") == "in_progress"
                        and isinstance(state.get("turn_id"), str)
                    )
                    if not active_writer or recovering_turn:
                        raise
                    history = state.setdefault("thread_history", [])
                    if isinstance(history, list) and thread_id not in history:
                        history.append(thread_id)
                    thread_id, thread = start_thread()
                    for stale_key in (
                        "turn_id",
                        "result",
                        "error",
                        "action_required",
                    ):
                        state.pop(stale_key, None)
                    state["thread_rotation_reason"] = "prior_thread_active_writer"
            else:
                thread_id, thread = start_thread()
            if (
                state.get("operation_key") == operation_key
                and state.get("phase") == "in_progress"
                and isinstance(state.get("turn_id"), str)
            ):
                recovered = client.request(
                    "thread/read", {"threadId": thread_id, "includeTurns": True}
                )
                turn_snapshot = self._find_turn(
                    recovered.get("thread", {}), state["turn_id"]
                )
                if turn_snapshot is not None:
                    status = str(turn_snapshot.get("status", "failed"))
                    if status == "completed":
                        result = self._parse_structured_text(
                            self._agent_text(turn_snapshot)
                        )
                        state.update({"phase": "completed", "result": result})
                        self._save_state(context, state)
                        return {
                            **result,
                            "_browser_state": self._public_state(state),
                        }
                    if status in {"inProgress", "in_progress"}:
                        text, status, error, usage = self._wait_for_turn(
                            client, thread_id, state["turn_id"]
                        )
                        if status == "completed":
                            result = self._parse_structured_text(text)
                        else:
                            result = {
                                "kind": "failed",
                                "retryable": status == "interrupted",
                                "error": {
                                    "code": f"codex_turn_{status}",
                                    "message": str(
                                        error or f"Codex turn ended as {status}"
                                    ),
                                },
                            }
                        if usage:
                            result["_usage"] = usage
                        state.update({"phase": "completed", "result": result})
                        self._save_state(context, state)
                        return {
                            **result,
                            "_browser_state": self._public_state(state),
                        }
            state.update(
                {
                    "adapter_id": self.adapter_id,
                    "thread_id": thread_id,
                    "operation_key": operation_key,
                    "phase": "thread_ready",
                }
            )
            self._save_state(context, state)
            params = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": str(self.cwd),
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "readOnly",
                    "access": {"type": "fullAccess"},
                },
                "outputSchema": output_schema,
                "summary": "concise",
            }
            if self.model:
                params["model"] = self.model
            if self.effort:
                params["effort"] = self.effort
            started_turn = client.request("turn/start", params)
            turn = started_turn.get("turn", {})
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise HarnessProtocolError("turn/start returned no turn id")
            state.update({"turn_id": turn_id, "phase": "in_progress"})
            state.pop("result", None)
            self._save_state(context, state)
            text, status, error, usage = self._wait_for_turn(client, thread_id, turn_id)
            if status != "completed":
                result = {
                    "kind": "failed",
                    "retryable": status == "interrupted",
                    "error": {
                        "code": f"codex_turn_{status}",
                        "message": str(error or f"Codex turn ended as {status}"),
                    },
                }
            else:
                result = self._parse_structured_text(text)
            if usage:
                result["_usage"] = usage
            state.update({"phase": "completed", "result": result})
            self._save_state(context, state)
            return {**result, "_browser_state": self._public_state(state)}
        except _ActionRequired as exc:
            if isinstance(state.get("thread_id"), str) and isinstance(
                state.get("turn_id"), str
            ):
                try:
                    client.request(
                        "turn/interrupt",
                        {
                            "threadId": state["thread_id"],
                            "turnId": state["turn_id"],
                        },
                    )
                except Exception:
                    pass
            state.update(
                {
                    "phase": "blocked",
                    "action_required": exc.request,
                }
            )
            self._save_state(context, state)
            return {
                "kind": "blocked",
                "reason": "Codex app-server requires external input or approval.",
                "action_required": exc.request,
                "_browser_state": self._public_state(state),
            }
        except (OSError, TimeoutError, HarnessProtocolError) as exc:
            state.update({"phase": "failed", "error": str(exc)})
            self._save_state(context, state)
            return {
                "kind": "failed",
                "retryable": isinstance(exc, (OSError, TimeoutError)),
                "error": {"code": "codex_app_server_error", "message": str(exc)},
                "_browser_state": self._public_state(state),
            }
        finally:
            client.close()

    def _wait_for_turn(
        self, client: _JsonRpcProcess, thread_id: str, turn_id: str
    ) -> tuple[str, str, Any, dict[str, int]]:
        deltas: list[str] = []
        completed_text: str | None = None
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = client.receive(deadline)
            if "id" in message and "method" in message:
                raise _ActionRequired(message)
            method = message.get("method")
            params = message.get("params", {})
            if method == "item/completed":
                item = params.get("item", {}) if isinstance(params, dict) else {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    completed_text = item["text"]
            elif method == "item/agentMessage/delta":
                delta = params.get("delta") if isinstance(params, dict) else None
                if isinstance(delta, str):
                    deltas.append(delta)
            elif method == "turn/completed":
                turn = params.get("turn", {}) if isinstance(params, dict) else {}
                if turn.get("id") != turn_id:
                    continue
                for item in turn.get("items", []):
                    if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                        completed_text = item["text"]
                text = completed_text if completed_text is not None else "".join(deltas)
                return (
                    text,
                    str(turn.get("status", "failed")),
                    turn.get("error"),
                    self._usage(turn.get("usage")),
                )
            elif method == "error":
                text = completed_text if completed_text is not None else "".join(deltas)
                return text, "failed", params.get("error", params), {}

    @staticmethod
    def _usage(raw: Any) -> dict[str, int]:
        if not isinstance(raw, dict):
            return {}
        input_tokens = raw.get("inputTokens", raw.get("input_tokens", 0))
        output_tokens = raw.get("outputTokens", raw.get("output_tokens", 0))
        try:
            return {
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
            }
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _parse_structured_text(text: str) -> dict[str, Any]:
        value = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL)
        if fenced:
            value = fenced.group(1)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise HarnessProtocolError(
                f"Codex structured output is not JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise HarnessProtocolError("Codex structured output must be an object")
        return parsed

    @staticmethod
    def _find_turn(thread: Any, turn_id: str) -> dict[str, Any] | None:
        if not isinstance(thread, dict):
            return None
        turns = thread.get("turns", [])
        if not isinstance(turns, list):
            return None
        return next(
            (
                turn
                for turn in turns
                if isinstance(turn, dict) and turn.get("id") == turn_id
            ),
            None,
        )

    @staticmethod
    def _agent_text(turn: dict[str, Any]) -> str:
        messages = [
            item["text"]
            for item in turn.get("items", [])
            if isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and isinstance(item.get("text"), str)
        ]
        if not messages:
            raise HarnessProtocolError(
                "completed Codex turn has no persisted agentMessage"
            )
        return messages[-1]

    @staticmethod
    def _public_state(state: dict[str, Any]) -> dict[str, Any]:
        return {
            key: state[key]
            for key in (
                "adapter_id",
                "thread_id",
                "turn_id",
                "operation_key",
                "phase",
                "thread_history",
                "thread_rotation_reason",
            )
            if key in state
        }
