"""Replay complete and interrupted model streams against the compiled C# CLI."""
from __future__ import annotations

import argparse
import ast
import base64
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "dotnet/src/SciDev.Cli/bin/Debug/net10.0/SciDev.Cli.dll"
PROMPT = "Generate an SVG of a pelican riding a bicycle"
FAKE_KEY = "migration-test-placeholder-not-a-real-key"
TINY_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
TINY_GIF = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
ORACLE = False
DOCKER_IMAGE: str | None = None
PREVIEW_EXE: Path | None = None
ModelReply = str | Callable[[], str] | tuple[int, str | None] | tuple[int, str | None, str]


def reference_svg() -> str:
    """Read the existing Python fixture without importing its Qt dependencies."""
    source = ast.parse((ROOT / "tests/test_pelican_svg.py").read_text(encoding="utf-8"))
    for node in source.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VALID_PELICAN_SVG" for t in node.targets):
            return ast.literal_eval(node.value).decode()
    raise AssertionError("Python migration fixture is missing")


def frame(delta: dict, finish: str | None = None) -> str:
    payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(payload)}\n\n"


def tool(name: str, call_id: str = "call-1", *, end_stream: bool = True, finish: str = "tool_calls", **args: object) -> str:
    serialized = json.dumps(args, ensure_ascii=False)
    mid = len(serialized) // 2
    return (frame({"tool_calls": [{"index": 0, "id": call_id, "function": {"name": name, "arguments": serialized[:mid]}}]})
            + frame({"tool_calls": [{"index": 0, "function": {"arguments": serialized[mid:]}}]})
            + frame({}, finish) + ("data: [DONE]\n\n" if end_stream else ""))


def stop(text: str = "Done.") -> str:
    return (frame({"content": text}) + frame({}, "stop")
            + 'data: {"choices":[],"usage":{"total_tokens":42}}\n\n'
            + "data: [DONE]\n\n")


def docker_project(workspace: Path) -> str:
    (workspace / ".env").write_text("private project configuration", encoding="utf-8")
    (workspace.parent / "outside.txt").write_text("outside test sentinel", encoding="utf-8")
    (workspace / "verify.cjs").write_text(
        """const assert = require('node:assert/strict');
const fs = require('node:fs');
assert.equal(process.env.SCIDEV_API_KEY, undefined);
for (const path of ['../outside.txt', '/workspace/.env', '/workspace/.research', '.env']) {
  assert.equal(fs.existsSync(path), false);
}
assert.deepEqual(Object.keys(require('node:os').networkInterfaces()), ['lo']);
assert.throws(() => fs.writeFileSync('/workspace/host.txt', 'fixture'), { code: 'EROFS' });
fs.writeFileSync('cache.txt', process.argv[2]);
assert.equal(fs.readFileSync('cache.txt', 'utf8'), process.argv[2]);
console.log('fixture passed: ' + process.argv[2]);
""", encoding="utf-8",
    )
    return "space & dollar $ literal"


@contextmanager
def model(replies: list[ModelReply]) -> Iterator[tuple[str, list[dict]]]:
    captured: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if (self.path != "/v1/chat/completions"
                    or self.headers.get("Authorization") != f"Bearer {FAKE_KEY}"
                    or len(captured) >= len(replies)):
                self.send_error(400)
                return
            body = self.rfile.read(int(self.headers["Content-Length"]))
            reply = replies[len(captured)]
            captured.append(json.loads(body))
            try:
                if isinstance(reply, tuple):
                    status, retry_after, *message = reply
                    response = json.dumps({"error": {"message": message[0] if message else "fixture response"}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    if retry_after is not None:
                        self.send_header("Retry-After", retry_after)
                else:
                    response = (reply() if callable(reply) else reply).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except ConnectionError:
                pass  # The interrupted-run fixture deliberately closes the connection.

        def log_message(self, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", captured
        finally:
            server.shutdown()
            thread.join()

@contextmanager
def doctor_model(redirect: bool = False) -> Iterator[tuple[str, list[dict]]]:
    captured: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.respond()

        def do_POST(self) -> None:
            self.respond()

        def respond(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            content = json.loads(self.rfile.read(length)) if length else None
            captured.append({"method": self.command, "path": self.path, "content": content,
                             "authorization": self.headers.get("Authorization")})
            if redirect:
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.command == "GET":
                response = {"data": [{"id": "doctor-fixture", "context_length": 32768,
                                      "capabilities": ["vision"], "arbitrary": f"provider echoed {FAKE_KEY}"}]}
            else:
                response = {"choices": [{"message": {"tool_calls": [
                    {"id": "probe-ping", "function": {"name": "ping", "arguments": "{}"}},
                ]}}]}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", captured
        finally:
            server.shutdown()
            thread.join()


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        parent = ROOT / ".tmp-tests"
        parent.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="csharp-replay-", dir=parent)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def run_task(
        self, replies: list[ModelReply], task: str = PROMPT, *, strict: bool = False,
        max_turns: int = 8, restore_on_failure: bool = False, sandbox_image: str | None = None,
        execution_mode: str | None = None, worktree_id: str | None = None, planning: str | None = None,
        no_verify: bool = False, native_svg: bool = False, use_legacy_policy: bool = False, network_policy: str | None = None,
        cli_flags: tuple[str, ...] = (),
    ) -> tuple[subprocess.CompletedProcess[str], dict, list[dict], list[dict]]:
        flags = ["--max-turns", str(max_turns)]
        if strict:
            flags += ["--pelican-validator", str(ROOT / "scripts/csharp_pelican_validator.py"), "--validator-python", sys.executable]
        if native_svg:
            flags.append("--pelican-native")
        if restore_on_failure:
            flags.append("--restore-on-failure")
        if sandbox_image:
            flags.extend(["--sandbox-image", sandbox_image])
        if execution_mode:
            flags.extend(["--execution-mode", execution_mode])
        if worktree_id:
            flags.extend(["--worktree", "--worktree-id", worktree_id])
        if planning:
            flags.extend(["--planning-mode", planning])
        if no_verify:
            flags.append("--no-verify")
        if use_legacy_policy:
            flags.append("--use-legacy-policy")
        if network_policy:
            flags.extend(["--network-policy", network_policy])
        cli = [str(PREVIEW_EXE)] if PREVIEW_EXE else ["dotnet", str(DLL)]
        with model(replies) as (url, requests):
            process = subprocess.run(
                [*cli, "exec", "--workspace", str(self.workspace), "--api-base", url,
                 "--model", "offline-fixture", "--task", task, "--patch-approval", "reject", "--json", *flags, *cli_flags],
                env={**os.environ, "SCIDEV_API_KEY": FAKE_KEY},
                encoding="utf-8", capture_output=True, timeout=25,
            )
        self.assertTrue(process.stdout, process.stderr)
        result = json.loads(process.stdout)
        actual_workspace = Path(result.get("workspace", self.workspace))
        raw = (actual_workspace / result["events_path"]).read_text(encoding="utf-8")
        self.assertNotIn(FAKE_KEY, raw + process.stdout + process.stderr)
        events = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual({event["run_id"] for event in events}, {result["run_id"]})
        finished = [event for event in events if event["type"] == "run_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["data"]["status"], result["status"])
        return process, result, requests, events

    def test_retrying_model_turn_preserves_completed_tool_and_turn_number(self) -> None:
        write_reply = tool("write_file", path="experiment.txt", content="recorded once")
        final_reply = (frame({"content": "Done."}) + frame({}, "stop")
                       + 'data: {"choices":[],"usage":{"input_tokens":35}}\r\n\r\n'
                       + 'data: {"choices":[],"usage":{"output_tokens":7}}\r\n\r\n'
                       + "data: [DONE]\r\n\r\n")
        process, result, requests, events = self.run_task([
            write_reply,
            (429, "0"),
            (503, None),
            final_reply,
        ], max_turns=2)
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual((self.workspace / "experiment.txt").read_text("utf-8"), "recorded once")
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[1], requests[2])
        self.assertEqual(requests[2], requests[3])
        self.assertEqual(requests[3]["messages"][-1]["tool_call_id"], "call-1")
        self.assertEqual(sum(event["type"] == "tool_completed" for event in events), 1)
        self.assertEqual(sum(event["type"] == "model_requested" for event in events), 2)
        self.assertEqual(
            [event["data"] for event in events if event["type"] == "model_retry_scheduled"],
            [{"status": 429, "attempt": 1, "delay_ms": 500}, {"status": 503, "attempt": 2, "delay_ms": 1000}],
        )
        measured = [event["data"] for event in events if event["type"] == "model_request_measured"]
        self.assertEqual([(row["attempt"], row["status_code"], row["success"]) for row in measured],
                         [(1, 200, True), (1, 429, False), (2, 503, False), (3, 200, True)])
        usage = result["usage"]
        self.assertEqual(usage, {
            "prompt_tokens": 35, "completion_tokens": 7, "total_tokens": 42, "token_reports": 1,
            "request_attempts": 4, "retry_attempts": 2, "failed_attempts": 2, "proxy_fallback_attempts": 0,
            "request_body_bytes": sum(row["request_body_bytes"] for row in measured),
            "response_body_bytes": len(write_reply.encode()) + len(final_reply.encode()),
            "elapsed_ms": sum(row["elapsed_ms"] for row in measured),
        })
        self.assertTrue(all(row["request_body_bytes"] > 0 for row in measured))

    def test_interrupted_model_response_preserves_received_usage(self) -> None:
        partial = (frame({"content": "partial"}) + frame({}, "stop")
                   + 'data: {"choices":[],"usage":{"prompt_tokens":13,"completion_tokens":4}}\n\n')
        process, result, _, events = self.run_task([partial], execution_mode="read-only")
        self.assertNotEqual(process.returncode, 0)
        usage = result["usage"]
        self.assertEqual((usage["request_attempts"], usage["failed_attempts"], usage["response_body_bytes"]),
                         (1, 1, len(partial.encode())))
        self.assertEqual((usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"], usage["token_reports"]),
                         (13, 4, 17, 1))
        self.assertFalse(any(event["type"] == "tool_started" for event in events))

    def test_retry_respects_limits_and_permanent_http_errors(self) -> None:
        for failures, sends, expected_error in [
            ([(401, None)], 1, ("auth", False)),
            ([(429, "30")], 1, ("rate_limit", True)),
            ([(502, None)] * 3, 3, ("network", True)),
        ]:
            with self.subTest(failures=failures):
                process, result, requests, events = self.run_task([*failures, stop()], execution_mode="read-only")
                self.assertEqual(process.returncode, 1, result)
                self.assertEqual(result["status"], "model_error")
                self.assertEqual((result["error_kind"], result["retryable"]), expected_error)
                self.assertEqual(len(requests), sends)
                self.assertEqual(sum(event["type"] == "model_retry_scheduled" for event in events), sends - 1)
                usage = result["usage"]
                self.assertEqual((usage["request_attempts"], usage["retry_attempts"], usage["failed_attempts"],
                                  usage["token_reports"]), (sends, sends - 1, sends, 0))
                self.assertFalse(any(event["type"] == "tool_started" for event in events))

    def test_http_failure_classification_and_result_file_ignore_untrusted_detail(self) -> None:
        flags = ("--result-file", ".research/results/failure.json")
        path = self.workspace / ".research/results/failure.json"
        bait = f"HTTP 401: invalid api key {FAKE_KEY}; echoed provider content must stay private."
        for code, classification in [
            (403, ("auth", False)),
            (402, ("rate_limit", True)),
            (408, ("timeout", True)),
            (504, ("timeout", True)),
            (404, ("internal", False)),
        ]:
            with self.subTest(status=code):
                process, result, requests, events = self.run_task(
                    [(code, "30", bait)], execution_mode="read-only", cli_flags=flags)
                self.assertEqual((process.returncode, result["status"], len(requests)), (1, "model_error", 1))
                self.assertEqual((result["error_kind"], result["retryable"]), classification)
                self.assertIn(f"HTTP {code}", result["detail"])
                self.assertTrue(result["user_message"])
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), result)
                saved = process.stdout + process.stderr + path.read_text(encoding="utf-8") + json.dumps(events)
                self.assertNotIn(FAKE_KEY, saved)
                self.assertNotIn("echoed provider content", saved)

    def test_exec_failure_classification_is_additive_and_distinguishes_denials(self) -> None:
        (self.workspace / "exists.txt").write_text("original", encoding="utf-8")
        for replies, status, classification in [
            ([tool("write_file", path="exists.txt", content="cannot overwrite")], "approval_required", ("approval", False)),
            ([tool("write_file", path="../outside.txt", content="cannot escape")], "path_denied", ("workspace", False)),
            ([tool("write_file", path=42, content="bad arguments")], "invalid_tool", ("tool", True)),
            ([stop("Report without changes")], "no_changes", ("no_changes", False)),
        ]:
            with self.subTest(status=status):
                process, result, _, _ = self.run_task(replies)
                self.assertEqual((process.returncode, result["status"]), (1, status))
                self.assertEqual((result["error_kind"], result["retryable"]), classification)
                self.assertTrue(result["detail"])
                if status == "no_changes":
                    self.assertEqual(result["detail"], "no_changes_detected")
        process, result, _, _ = self.run_task([stop("Read-only answer")], execution_mode="read-only")
        self.assertEqual((process.returncode, result["status"]), (0, "completed"))
        self.assertFalse({"error_kind", "user_message", "retryable", "detail"} & set(result))
        self.assertEqual((self.workspace / "exists.txt").read_text(encoding="utf-8"), "original")
        self.assertFalse((self.root / "outside.txt").exists())

    def test_exec_classifies_connection_failure_without_a_provider_body(self) -> None:
        with closing(socket.socket()) as unused:
            unused.bind(("127.0.0.1", 0))
            endpoint = f"http://127.0.0.1:{unused.getsockname()[1]}/v1"
            code, result = self.invoke_session(endpoint, "Read connection state", "--execution-mode", "read-only")
        self.assertEqual((code, result["status"], result["error_kind"], result["retryable"]),
                         (1, "model_error", "network", True))

    def invoke_doctor(self, *flags: str, endpoint: str = "") -> tuple[subprocess.CompletedProcess[str], dict]:
        env = {**os.environ, "SCIDEV_API_BASE": endpoint, "SCIDEV_API_KEY": FAKE_KEY,
               "SCIDEV_MODEL": "doctor-fixture", "SCIDEV_SANDBOX_BACKEND": ""}
        process = subprocess.run(["dotnet", str(DLL), "doctor", "--workspace", str(self.workspace), "--json", *flags],
                                 env=env, capture_output=True, encoding="utf-8", timeout=20)
        return process, json.loads(process.stdout)

    def test_doctor_local_workspace_reports_candidates_without_running_or_writing(self) -> None:
        demo = self.workspace / "client"
        demo.mkdir()
        (demo / "package.json").write_text('{"scripts":{"test":"node sentinel.cjs"}}', encoding="utf-8")
        (demo / "sentinel.cjs").write_text("require('node:fs').writeFileSync('executed.txt','ran')", encoding="utf-8")
        (self.workspace / "test_observations.py").write_text("def test_observation():\n    assert True\n", encoding="utf-8")
        ignored = self.workspace / "ignored"
        ignored.mkdir()
        (ignored / "package.json").write_text('{"scripts":{"test":"node sentinel.cjs"}}', encoding="utf-8")
        (self.workspace / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        with doctor_model() as (endpoint, captured):
            process, doctor = self.invoke_doctor("--tests", endpoint=endpoint)
        self.assertEqual((process.returncode, doctor["ok"], str(self.workspace)), (0, True, doctor["workspace"]))
        self.assertTrue(doctor["provider_key_configured"])
        self.assertFalse(doctor["command_tool_enabled"])
        tests = doctor["tests"]
        self.assertTrue(tests["complete"])
        self.assertEqual({candidate["directory"] for candidate in tests["candidates"]}, {".", "client"})
        self.assertEqual({candidate["arguments"][0] for candidate in tests["candidates"]}, {"python", "npm"})
        self.assertEqual(next(item["status"] for item in doctor["checks"] if item["name"] == "tests"), "ok")
        self.assertEqual(captured, [])
        self.assertFalse((demo / "executed.txt").exists())
        self.assertFalse((self.workspace / ".research").exists())
        self.assertNotIn(FAKE_KEY, process.stdout + process.stderr)

    def test_doctor_online_probes_only_explicit_destination_and_ignores_redirects(self) -> None:
        (self.workspace / "private.txt").write_text("private artifact marker", encoding="utf-8")
        for redirects in (False, True):
            with self.subTest(redirects=redirects), doctor_model(redirect=redirects) as (endpoint, captured):
                process, report = self.invoke_doctor("--online", endpoint=endpoint)
                self.assertEqual((process.returncode, report["ok"]), (0, True))
                self.assertEqual(report["probe"]["tools_supported"], not redirects)
                self.assertEqual([(req["method"], req["path"]) for req in captured],
                                 [("GET", "/v1/models"), ("POST", "/v1/chat/completions")])
                self.assertTrue(all(req["authorization"] == f"Bearer {FAKE_KEY}" for req in captured))
                self.assertNotIn(FAKE_KEY, process.stdout + process.stderr)
                self.assertNotIn("provider echoed", process.stdout)
                body = captured[-1]["content"]
                self.assertEqual(body["tools"][0]["function"]["name"], "ping")
                self.assertGreaterEqual(body["max_tokens"], 10000)
                self.assertNotIn("private artifact marker", json.dumps(body))
                if not redirects:
                    self.assertEqual((report["probe"]["context_length"], report["probe"]["vision"]), (32768, True))

    def test_doctor_saved_provider_requires_approval_and_argument_errors_return_two(self) -> None:
        with doctor_model() as (endpoint, captured):
            research = self.workspace / ".research"
            research.mkdir()
            (research / "provider.json").write_text(json.dumps(
                {"base_url": endpoint, "model": "doctor-fixture", "credential_profile": "doctor-fake-missing"}), encoding="utf-8")
            process, report = self.invoke_doctor("--online")
            self.assertEqual((process.returncode, report["ok"], report["probe"], captured), (0, True, None, []))
            self.assertEqual(next(item["status"] for item in report["checks"] if item["name"] == "provider-probe"), "warn")
            process, denied = self.invoke_doctor("--workspace", str(self.workspace))
            self.assertEqual((process.returncode, denied["status"]), (2, "invalid_workspace_or_arguments"))

    def test_pelican_artifact_and_tool_followup(self) -> None:
        svg = reference_svg()
        process, result, requests, events = self.run_task(
            [frame({"reasoning_content": "internal fixture"}) + tool("write_file", path="pelican.svg", content=svg),
             stop(f"Saved. Echo: {FAKE_KEY}")]
        )
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result["files_changed"], [{"path": "pelican.svg", "operation": "write"}])
        self.assertEqual((self.workspace / "pelican.svg").read_bytes(), svg.encode())
        self.assertEqual(requests[0]["messages"][-1]["content"], PROMPT)
        self.assertEqual(requests[0]["max_tokens"], 12000)
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "call-1")
        self.assertEqual(requests[1]["messages"][-2]["reasoning_content"], "internal fixture")
        self.assertNotIn("internal fixture", json.dumps(events))
        self.assertIn("pelican.svg", requests[1]["messages"][-1]["content"])
        code, reopened = self.diagnostic("inspect-run", "--workspace", str(self.workspace), "--run-id", result["run_id"])
        self.assertEqual(code, 0, reopened)
        self.assertEqual(reopened["task"], PROMPT)
        self.assertEqual(reopened["summary"], "Saved. Echo: [redacted]")
        self.assertEqual([e["type"] for e in events].count("model_completed"), 2)
        if ORACLE:
            from scripts.smoke_pelican_svg import validate_and_render_svg

            preview = self.root / "preview.png"
            metrics = validate_and_render_svg((self.workspace / "pelican.svg").read_bytes(), preview)
            self.assertTrue(preview.exists())
            self.assertGreater(metrics["non_background_samples"], 25)

    def test_pelican_file_can_be_written_but_fail_strict_validation(self) -> None:
        svg = reference_svg().replace('id="left-wheel"', 'id="unexpected-wheel"')
        process, result, _, _ = self.run_task([tool("write_file", path="pelican.svg", content=svg), stop()])
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual((self.workspace / "pelican.svg").read_bytes(), svg.encode())
        if ORACLE:
            from scripts.smoke_pelican_svg import validate_and_render_svg

            with self.assertRaisesRegex(RuntimeError, "left-wheel"):
                validate_and_render_svg((self.workspace / "pelican.svg").read_bytes(), self.root / "preview.png")

    def test_strict_feedback_repairs_two_ids_and_revalidates(self) -> None:
        if not ORACLE:
            self.skipTest("Requires the original Python/Qt validator (--oracle)")
        svg = reference_svg()
        initial = svg.replace('id="left-wheel"', 'id="rear"').replace('id="right-wheel"', 'id="front"')
        process, result, requests, events = self.run_task([
            tool("write_file", path="pelican.svg", content=initial), stop(),
            tool("replace_in_file", "call-2", path="pelican.svg", edits=[
                {"old_text": 'id="rear"', "new_text": 'id="left-wheel"'},
                {"old_text": 'id="front"', "new_text": 'id="right-wheel"'},
            ]),
        ], strict=True)
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result["acceptance"]["status"], "passed")
        self.assertEqual(len(requests), 3)
        self.assertEqual((self.workspace / "pelican.svg").read_bytes(), svg.encode())
        self.assertEqual([e["data"]["status"] for e in events if e["type"] == "artifact_checked"], ["failed", "passed"])
        feedback = requests[2]["messages"][-1]
        self.assertEqual(feedback["role"], "system")
        self.assertIn("left-wheel", feedback["content"])
        self.assertIn("right-wheel", feedback["content"])
        for request in requests:
            self.assertEqual([m["content"] for m in request["messages"] if m["role"] == "user"][-1], PROMPT)

    @unittest.skipUnless(sys.platform == "win32", "Native renderer requires Windows")
    def test_native_svg_geometry_repairs_and_revalidates_final_edit(self) -> None:
        svg = reference_svg()
        initial = svg.replace('id="left-wheel"', 'id="rear"').replace("220 94 Q215", "197 83 Q215")
        process, result, requests, events = self.run_task([
            tool("write_file", path="native.svg", content=initial), stop(),
            tool("replace_in_file", "call-2", path="native.svg", edits=[
                {"old_text": 'id="rear"', "new_text": 'id="left-wheel"'},
                {"old_text": "197 83 Q215", "new_text": "220 94 Q215"},
            ]),
        ], native_svg=True, max_turns=3)
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result["acceptance"]["status"], "passed")
        self.assertIn("visible samples", result["acceptance"]["detail"])
        self.assertEqual((self.workspace / "native.svg").read_bytes(), svg.encode())
        self.assertEqual([e["data"]["status"] for e in events if e["type"] == "artifact_checked"], ["failed", "passed"])
        feedback = requests[2]["messages"][-1]["content"]
        self.assertIn("left-wheel", feedback)
        self.assertIn("first cubic endpoint", feedback)

    @unittest.skipUnless(sys.platform == "win32", "Native renderer requires Windows")
    def test_native_svg_rejects_non_static_and_unbounded_artifacts(self) -> None:
        svg = reference_svg()
        additions = [
            ('<image href="https://example.invalid/external.png" width="100" height="100"/>', "unsupported or active"),
            ('<style>@import "https://example.invalid/external.css";</style>', "external stylesheet"),
            ('<use href="https://example.invalid/external.svg#part"/>', "local definition"),
            ('<g opacity=".5"><rect width="90000" height="90000"/></g>', "compositing budget"),
        ]
        for index, (new_shape, reason) in enumerate(additions):
            with self.subTest(reason=reason):
                process, result, _, _ = self.run_task([
                    tool("write_file", path=f"refused-{index}.svg", content=svg.replace("</svg>", new_shape + "</svg>")),
                ], native_svg=True, max_turns=1)
                self.assertEqual(process.returncode, 1, result)
                self.assertEqual(result["status"], "turn_limit")
                self.assertEqual(result["acceptance"]["status"], "failed")
                self.assertIn(reason, result["acceptance"]["detail"])

    @unittest.skipUnless(sys.platform == "win32", "Native renderer requires Windows")
    def test_native_svg_preview_matches_oracle_on_static_samples(self) -> None:
        svg = reference_svg()
        styled = svg.replace('id="left-wheel" cx="90" cy="150" r="30" fill="none" stroke="#27374a"',
                             'id="left-wheel" class="tire" cx="90" cy="150" r="30"')
        styled = styled.replace("<title>", '<defs><style type="text/css">.tire { fill:none; stroke:#27374a; }</style>'
                                '<linearGradient id="feathers"><stop offset="0" stop-color="#f4a261"/>'
                                '<stop offset="1" stop-color="#d7894a"/></linearGradient></defs><title>')
        styled = styled.replace("</svg>", '<circle cx="25" cy="25" r="8" fill="url(#feathers)"/></svg>')
        samples = [
            ("reference", svg, "passed", ""),
            ("styled", styled, "passed", ""),
            ("body-gradient", styled.replace('fill="#f4a261"', 'fill="url(#feathers)"', 1),
             "failed", "pelican-body"),
            ("leg", svg.replace("188 150", "172 150", 1), "failed", "separate pedal positions"),
            ("body", svg.replace('fill="#f4a261"', 'fill="#f4f7fb"', 1), "failed", "pelican-body"),
            ("beak", svg.replace("270,76", "210,76", 1), "failed", "pelican-beak"),
        ]
        for label, content, status, diagnostic in samples:
            with self.subTest(label=label):
                source = self.root / f"{label}.svg"
                source.write_text(content, encoding="utf-8")
                preview = self.root / f"{label}.png"
                code, verdict = self.diagnostic("check-pelican", "--svg", str(source), "--preview", str(preview))
                self.assertEqual(code, 0 if status == "passed" else 1, verdict)
                self.assertEqual(verdict["status"], status, verdict)
                self.assertIn(diagnostic, verdict["detail"], verdict)
                if label == "reference":
                    samples_prefix = verdict["detail"].split("(", 1)[1]
                    self.assertGreater(int(samples_prefix.split()[0]), 2000)
                header = preview.read_bytes()[:24]
                self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual([int.from_bytes(header[start:start+4], "big") for start in (16, 20)], [1200, 800])
                if ORACLE:
                    from scripts.csharp_pelican_validator import verify

                    self.assertEqual(verify(content.encode())["status"], status)

    @unittest.skipUnless(sys.platform == "win32", "Native renderer requires Windows")
    def test_native_svg_preview_does_not_connect_to_external_resources(self) -> None:
        source = self.root / "external.svg"
        preview = self.root / "preview.png"
        with closing(socket.socket()) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(0.2)
            url = f"http://127.0.0.1:{listener.getsockname()[1]}/external.svg#part"
            source.write_text(reference_svg().replace("</svg>", f'<use href="{url}"/></svg>'), encoding="utf-8")
            code, verdict = self.diagnostic("check-pelican", "--svg", str(source), "--preview", str(preview))
            self.assertEqual(code, 1, verdict)
            self.assertEqual(verdict["status"], "failed")
            self.assertFalse(preview.exists())
            with self.assertRaises(TimeoutError):
                listener.accept()

    def test_strict_geometric_feedback_uses_detected_parts_and_two_repairs(self) -> None:
        if not ORACLE:
            self.skipTest("Requires the original Python/Qt validator (--oracle)")
        svg = reference_svg()
        original = ('<path id="pelican-wing-reaching" d="M190 80 C200 80 215 88 220 94 Q215 96 210 94 '
                    'Q200 87 188 88Z" fill="#e9c46a" stroke="#27374a" stroke-width="2"/>')
        broken = original.replace("220 94", "197 83")
        invalid = svg.replace(original, broken)
        no_id = invalid.replace('id="left-wheel"', 'id="rear"')
        process, result, requests, events = self.run_task([
            tool("write_file", path="pelican.svg", content=no_id), stop(),
            tool("replace_in_file", "call-2", path="pelican.svg", old_text='id="rear"', new_text='id="left-wheel"'),
            tool("replace_in_file", "call-3", path="pelican.svg", old_text=broken, new_text=original),
        ], strict=True, max_turns=4)
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual((self.workspace / "pelican.svg").read_bytes(), svg.encode())
        self.assertEqual(len(requests), 4)
        feedback = requests[3]["messages"][-1]["content"]
        self.assertIn("pelican-wing-reaching:", feedback)
        self.assertIn("first cubic endpoint", feedback)
        self.assertNotIn("Assign this ID", feedback)
        self.assertEqual([e["turn"] for e in events if e["type"] == "artifact_checked"], [2, 3, 4])

    def test_strict_failed_acceptance_cannot_be_reported_as_completed(self) -> None:
        if not ORACLE:
            self.skipTest("Requires the original Python/Qt validator (--oracle)")
        svg = reference_svg().replace('id="left-wheel"', 'id="rear"')
        process, result, requests, events = self.run_task([
            tool("write_file", path="pelican.svg", content=svg), stop(), stop(), stop(),
        ], strict=True)
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result["status"], "artifact_validation_failed")
        self.assertFalse(result["success"])
        self.assertEqual(result["acceptance"]["status"], "failed")
        self.assertEqual((self.workspace / "pelican.svg").read_bytes(), svg.encode())
        self.assertEqual(len(requests), 4)
        self.assertEqual(sum(e["type"] == "artifact_repair_requested" for e in events), 2)

    def test_strict_checks_final_tool_result_without_extra_model_turn(self) -> None:
        if not ORACLE:
            self.skipTest("Requires the original Python/Qt validator (--oracle)")
        svg = reference_svg().replace('id="left-wheel"', 'id="rear"')
        process, result, requests, events = self.run_task([
            tool("write_file", path="pelican.svg", content=svg), stop(),
            tool("replace_in_file", "call-2", path="pelican.svg", old_text='id="rear"', new_text='id="left-wheel"'),
        ], strict=True, max_turns=3)
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result["acceptance"]["status"], "passed")
        self.assertEqual(len(requests), 3)
        self.assertEqual([e["data"]["status"] for e in events if e["type"] == "artifact_checked"], ["failed", "passed"])

    def test_invalid_batch_does_not_apply_the_first_edit(self) -> None:
        process, result, _, _ = self.run_task([
            tool("write_file", path="existing.txt", content="first and second"),
            tool("replace_in_file", "call-2", path="existing.txt", edits=[
                {"old_text": "first", "new_text": "modified"}, {"old_text": "absent", "new_text": "wrong"},
            ]),
        ])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "edit_conflict")
        self.assertEqual((self.workspace / "existing.txt").read_bytes(), b"first and second")

    def test_existing_edit_is_rejected_and_model_does_not_retry(self) -> None:
        original = self.workspace / "module.py"
        original.write_bytes(b"def first():\r\n    return 1\r\n")
        process, result, requests, events = self.run_task([
            tool("read_file", path="module.py"),
            tool("apply_patch", "call-2", path="module.py", old_text="return 1", new_text="return 10"),
        ], "Change first() to return 10.")
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result["status"], "approval_denied")
        self.assertEqual(result["files_changed"], [])
        self.assertEqual(original.read_bytes(), b"def first():\r\n    return 1\r\n")
        self.assertEqual(len(requests), 2)
        self.assertIn("2:     return 1", requests[1]["messages"][-1]["content"])
        self.assertEqual([event["type"] for event in events[-3:]], ["tool_denied", "run_finished", "task_summary"])

    def test_creation_does_not_overwrite(self) -> None:
        target = self.workspace / "existing.txt"
        target.write_bytes(b"keep")
        process, result, _, _ = self.run_task([tool("write_file", path="existing.txt", content="change")])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "approval_required")
        self.assertEqual(target.read_bytes(), b"keep")

    def test_same_task_can_iterate_on_a_new_file(self) -> None:
        process, result, requests, _ = self.run_task([
            tool("write_file", path="generated.txt", content="value = 1\r\n"),
            tool("replace_in_file", "call-2", path="generated.txt", old_text="= 1", new_text="= 2"),
            stop(),
        ])
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual((self.workspace / "generated.txt").read_bytes(), b"value = 2\r\n")
        self.assertEqual([file["operation"] for file in result["files_changed"]], ["write", "edit"])
        self.assertEqual(requests[-1]["messages"][-1]["tool_call_id"], "call-2")

    def test_legacy_replace_cannot_edit_an_existing_file(self) -> None:
        target = self.workspace / "existing.txt"
        target.write_bytes(b"keep")
        process, result, _, _ = self.run_task([
            tool("replace_in_file", path="existing.txt", old_text="keep", new_text="change")
        ])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "approval_required")
        self.assertEqual(target.read_bytes(), b"keep")

    def test_parent_path_is_refused(self) -> None:
        process, result, _, _ = self.run_task([tool("write_file", path="../outside.txt", content="fixture")])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "path_denied")
        self.assertFalse((self.root / "outside.txt").exists())

    def test_hard_link_is_refused(self) -> None:
        outside = self.root / "another-file.txt"
        outside.write_bytes(b"not in workspace")
        os.link(outside, self.workspace / "linked.txt")
        process, result, requests, _ = self.run_task([tool("read_file", path="linked.txt")])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "path_denied")
        self.assertEqual(len(requests), 1)

    def test_incomplete_model_stream_cannot_write(self) -> None:
        process, result, _, events = self.run_task(
            [tool("write_file", path="incomplete.txt", content="fixture", end_stream=False)]
        )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "model_error")
        self.assertFalse((self.workspace / "incomplete.txt").exists())
        self.assertNotIn("tool_started", [e["type"] for e in events])

    def test_token_limit_is_reported_without_executing_partial_tools(self) -> None:
        reply = tool("write_file", finish="length", path="not-written.txt", content="fixture")
        process, result, _, events = self.run_task([reply])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "model_token_limit")
        incomplete = next(entry for entry in events if entry["type"] == "model_incomplete")
        self.assertEqual(incomplete["data"]["finish_reason"], "length")
        self.assertEqual(incomplete["data"]["call_count"], 1)
        self.assertFalse((self.workspace / "not-written.txt").exists())

    def test_commands_are_not_available(self) -> None:
        process, result, requests, _ = self.run_task([tool("run_command", command="fixture placeholder")])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "unsupported_tool")
        self.assertNotIn("run_command", [item["function"]["name"] for item in requests[0]["tools"]])

    def test_required_plan_gates_work_and_is_reopened_from_journal(self) -> None:
        active = [{"step": "Create the report", "status": "in_progress"}]
        completed = [{"step": "Create the report", "status": "completed"}]
        process, result, requests, events = self.run_task([
            tool("write_file", path="early.txt", content="must not run"),
            tool("update_plan", "invalid", plan=[{"step": "Skip", "status": "skipped"}]),
            tool("update_plan", "start-plan", plan=active),
            tool("write_file", "write", path="report.txt", content="verified fixture"),
            stop("Premature summary."),
            tool("update_plan", "completed-plan", plan=completed),
            stop("The report was written."),
        ], planning="required")
        self.assertEqual((process.returncode, result["status"]), (0, "completed"))
        self.assertFalse((self.workspace / "early.txt").exists())
        self.assertIn("update_plan", [item["function"]["name"] for item in requests[0]["tools"]])
        self.assertEqual(result["plan"]["steps"], completed)
        self.assertEqual(result["plan"]["revision"], 2)
        self.assertEqual([row["data"]["status"] for row in events if row["type"] == "tool_denied"],
                         ["plan_required", "invalid_plan"])
        self.assertEqual(sum(row["type"] == "plan_followup" for row in events), 1)
        inspected = subprocess.run(["dotnet", str(DLL), "inspect-run", "--workspace", str(self.workspace),
                                    "--run-id", result["run_id"], "--json"], capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(inspected.returncode, 0)
        self.assertEqual(json.loads(inspected.stdout)["plan"]["steps"], completed)

    def test_unfinished_and_disabled_plans_have_distinct_results(self) -> None:
        process, result, _, _ = self.run_task([
            tool("update_plan", plan=[{"step": "Waiting on a decision", "status": "blocked"}]),
            stop(), stop(), stop(),
        ], planning="auto")
        self.assertEqual((process.returncode, result["status"], result["plan"]["finished"]), (1, "plan_incomplete", False))
        process, result, requests, _ = self.run_task([tool("update_plan", plan=[])], planning="off")
        self.assertEqual((process.returncode, result["status"]), (1, "unsupported_tool"))
        self.assertNotIn("update_plan", [item["function"]["name"] for item in requests[0]["tools"]])

    def test_default_code_task_without_container_reports_checks_not_run(self) -> None:
        process, result, requests, events = self.run_task([
            tool("write_file", path="example.py", content="result = 1\n"), stop(),
        ])
        self.assertEqual((process.returncode, result["status"]), (0, "completed"), result)
        self.assertEqual((self.workspace / "example.py").read_text(encoding="utf-8"), "result = 1\n")
        self.assertEqual(result["verification"]["status"], "not_run")
        self.assertEqual(result["verification"]["reason"], "container_not_configured")
        self.assertEqual(next(row["data"]["reason"] for row in events if row["type"] == "verification_reported"),
                         "container_not_configured")
        policy = next(row["data"] for row in events if row["type"] == "execution_policy")
        self.assertEqual((policy["mode"], policy["container"], policy["host_review"]),
                         ("WorkspaceApproval", False, False))
        self.assertTrue({"run_command", "run_host_command"}.isdisjoint(
            entry["function"]["name"] for entry in requests[0]["tools"]))
        process, result, _, _ = self.run_task([tool("run_command", argv=["fixture-never-run"])])
        self.assertEqual((process.returncode, result["status"]), (1, "unsupported_tool"))

    def test_verification_can_be_explicitly_disabled(self) -> None:
        process, result, _, _ = self.run_task([
            tool("write_file", path="checks_disabled.py", content="result = 2\n"), stop(),
        ], no_verify=True)
        self.assertEqual((process.returncode, result["status"], result["verification"]), (0, "completed", None))

    def test_manual_mode_does_not_auto_verify_manifest_candidates(self) -> None:
        (self.workspace / "package.json").write_text('{"scripts":{"test":"node --test"}}', encoding="utf-8")
        process, result, _, events = self.run_task([
            tool("write_file", path="manual-check.cjs", content="module.exports = 1;\n"), stop(),
        ], execution_mode="manual", sandbox_image="scidev-fixture-missing:never-pull")
        self.assertEqual((process.returncode, result["status"], result["verification"]["status"]),
                         (1, "verification_required", "not_run"))
        self.assertFalse(any(row["type"].startswith("verification_auto_") for row in events))

    def test_read_only_mode_can_report_without_writes(self) -> None:
        (self.workspace / "answer.txt").write_text("42", encoding="utf-8")
        process, result, requests, _ = self.run_task(
            [tool("read_file", path="answer.txt"), stop("Read the answer.")], execution_mode="read-only",
        )
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result["files_changed"], [])
        self.assertEqual([item["function"]["name"] for item in requests[0]["tools"]],
                         ["read_file", "list_files", "search_code", "get_project_context"])
        self.assertIn("42", requests[-1]["messages"][-1]["content"])

    def test_workspace_discovery_only_returns_visible_local_text(self) -> None:
        src = self.workspace / "src"
        src.mkdir()
        (src / "nested").mkdir()
        (src / "cache").mkdir()
        metadata = self.workspace / ".git" / "info"
        metadata.mkdir(parents=True)
        (metadata / "exclude").write_text("private-note.cs\n", encoding="utf-8")
        (self.workspace / ".gitignore").write_text(
            "cache/\n**/*.generated.cs\nsrc/**/skip[0-9].cs\n", encoding="utf-8",
        )
        (src / ".gitignore").write_text("local-*.cs\n!local-safe.cs\n", encoding="utf-8")
        (src / "main.cs").write_bytes(f"before {FAKE_KEY}\r\nCollectKey.(x)\r\nafter\n".encode("utf-8-sig"))
        (src / "local-safe.cs").write_text("collectkey.(x)", encoding="utf-8")
        for path in ["credentials.json", "private.pem", "src/private-note.cs", "src/cache/temp.cs", "src/nested/skip4.cs",
                     "src/skip6.cs", "src/local-secret.cs", "app.generated.cs", "src/app.generated.cs"]:
            (self.workspace / path).write_text("CollectKey.(x) protected marker", encoding="utf-8")
        (src / "binary.cs").write_bytes(b"CollectKey.(x)\x00binary marker")
        (src / "large.cs").write_text("CollectKey.(x) " + "x" * (256 * 1024), encoding="utf-8")
        external = self.root / "outside-code.cs"
        external.write_text("CollectKey.(x) outside marker", encoding="utf-8")
        os.link(external, src / "link.cs")

        process, result, requests, events = self.run_task([
            tool("list_files", depth=3),
            tool("search_code", "call-2", query="CollectKey.(x)", glob="src/**/*.cs", case_sensitive=True),
            tool("search_code", "call-3", query="CollectKey.(x)", path="src", case_sensitive=False, context_lines=0),
            tool("read_file", "call-4", path="src/main.cs"),
            stop("Found the declaration."),
        ], task="Find CollectKey.(x).", execution_mode="read-only")
        self.assertEqual((process.returncode, result["status"], result["files_changed"]), (0, "completed", []))
        listed, exact, insensitive, direct = [request["messages"][-1]["content"] for request in requests[1:]]
        self.assertIn("src/main.cs", listed)
        self.assertIn("src/local-safe.cs", listed)
        for disallowed in ["cache/", ".gitignore", "generated.cs", "skip4.cs", "skip6.cs", "local-secret.cs",
                           "credentials.json", "private.pem", "private-note.cs", "protected marker", "outside marker", FAKE_KEY]:
            self.assertNotIn(disallowed, listed + exact + insensitive + direct)
        self.assertIn("src/main.cs:2", exact)
        self.assertIn("1: before [redacted]", exact)
        self.assertIn("> 2: CollectKey.(x)", exact)
        self.assertIn("3: after", exact)
        self.assertNotIn("local-safe.cs", exact)
        self.assertNotIn("binary.cs", exact)
        self.assertNotIn("large.cs", exact)
        self.assertIn("src/local-safe.cs:1", insensitive)
        self.assertNotIn("after", insensitive)
        self.assertIn("1: before [redacted]", direct)
        self.assertEqual([event["data"]["name"] for event in events if event["type"] == "tool_completed"],
                         ["list_files", "search_code", "search_code", "read_file"])

    def test_workspace_discovery_rejects_protected_or_external_scope(self) -> None:
        for name, args in [
            ("search_code", {"query": "example", "path": "../"}),
            ("get_project_context", {"path": "../"}),
            ("list_files", {"path": "credentials.json"}),
            ("read_file", {"path": "private.pem"}),
            ("write_file", {"path": "secrets.json", "content": "test"}),
        ]:
            with self.subTest(name=name):
                process, result, requests, _ = self.run_task([tool(name, **args)])
                self.assertEqual((process.returncode, result["status"], len(requests)), (1, "path_denied", 1))
        self.assertFalse((self.workspace / "secrets.json").exists())

    def test_project_context_reports_static_manifests_scopes_and_safe_excerpts(self) -> None:
        def write(path: str, source: str) -> None:
            target = self.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")

        write("AGENTS.md", "Root project note.")
        write("packages/ui/AGENTS.md", "Local UI project note.")
        write("packages/other/AGENTS.md", "Other scope only.")
        write("packages/ui/main.ts", "-----BEGIN PRIVATE KEY-----\n"
              "unused-placeholder-key-block\n-----END PRIVATE KEY-----\n"
              "const UniqueMarker = 1;\n")
        write("packages/ui/package.json", json.dumps({
            "packageManager": "pnpm@10.1.0", "engines": {"node": ">=20"},
            "dependencies": {"@sample/kit": "^1.2.0", "internal-lib": "https://placeholder@example.test/private"},
            "scripts": {"test:ui": "touch should-not-exist", "publish": "private publish recipe"},
        }))
        write("packages/ui/pnpm-lock.yaml", "lockfileVersion: '9.0'")
        write("pyproject.toml", "[project]\nname = 'fixture'\nrequires-python = '>=3.12'\n"
              "dependencies = [\n  # 'comment-only-package'\n  'pytest>=8',\n"
              "  \"example[extra]>=1; python_version >= '3.12'\",\n]\n")
        write(".venv/pyvenv.cfg", "home = C:/secret/profile/path\nversion = 3.12.1\ninclude-system-site-packages = false")
        write("tests/test_fixture.py", "def test_fixture():\n    assert True\n")
        write("Tests/Example.Tests.csproj", "<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup>"
              "<ItemGroup><PackageReference Include='Microsoft.NET.Test.Sdk' Version='18.0.0'/></ItemGroup></Project>")
        write("go.mod", "module example.invalid/demo\n\ngo 1.24.1\n")
        write("Cargo.toml", "[package]\nname = 'demo'\nrust-version = '1.85'\n[dependencies]\nserde = '1'\n")
        write(".gitignore", "excluded/\n")
        write("excluded/package.json", '{"dependencies":{"excluded-package":"1"}}')
        write("credentials.json", '{"api_key":"protected-fixture"}')

        process, result, requests, events = self.run_task([
            tool("get_project_context", query="UniqueMarker"),
            tool("get_project_context", "call-2", query="UniqueMarker", path="packages/ui/main.ts"),
            stop("Read the project context."),
        ], task="Read project context around UniqueMarker.", execution_mode="read-only")
        self.assertEqual((process.returncode, result["status"], result["files_changed"]), (0, "completed", []))
        whole = requests[1]["messages"][-1]["content"]
        scoped = requests[2]["messages"][-1]["content"]
        for evidence in ["Python >=3.12", "pytest>=8", "example[extra]>=1",
                         "Node.js >=20", "@sample/kit ^1.2.0", '["pnpm","run","test:ui"]', "Go 1.24.1",
                         "Rust 1.85", "Microsoft.NET.Test.Sdk 18.0.0", ".NET net10.0", "Python 3.12.1"]:
            self.assertIn(evidence, whole)
        for context in [whole, scoped]:
            for evidence in ["4: const UniqueMarker = 1;", "Root project note.", "[packages/ui/AGENTS.md",
                             "Local UI project note.", "packages/ui/pnpm-lock.yaml"]:
                self.assertIn(evidence, context)
            for confidential in ["Other scope only.", "unused-placeholder-key-block", "secret/profile/path",
                                 "example.test/private", "private publish recipe", "excluded-package", "protected-fixture",
                                 "comment-only-package"]:
                self.assertNotIn(confidential, context)
        self.assertFalse((self.workspace / "should-not-exist").exists())
        self.assertEqual([event["data"]["name"] for event in events if event["type"] == "tool_completed"],
                         ["get_project_context", "get_project_context"])

    def test_first_model_request_includes_bounded_redacted_workspace_context(self) -> None:
        (self.workspace / "AGENTS.md").write_text("Scope fixture note.", encoding="utf-8")
        (self.workspace / "info.py").write_text(f"def contextual_marker():\n    return '{FAKE_KEY}'\n", encoding="utf-8")
        prompt = "Explain contextual_marker."
        process, result, requests, events = self.run_task([stop("Analysis complete.")], task=prompt,
                                                          execution_mode="read-only")
        self.assertEqual((process.returncode, result["status"]), (0, "completed"))
        messages = requests[0]["messages"]
        self.assertEqual(messages[-1]["content"], prompt)
        snapshot = messages[-2]
        self.assertEqual(snapshot["role"], "user")
        self.assertIn("Scope fixture note.", snapshot["content"])
        self.assertIn("1: def contextual_marker():", snapshot["content"])
        self.assertNotIn(FAKE_KEY, snapshot["content"])
        context = next(event for event in events if event["type"] == "workspace_context_prepared")
        self.assertLessEqual(context["data"]["characters"], 9000)
        self.assertNotIn("Scope fixture note.", json.dumps(events, ensure_ascii=False))

    def test_unavailable_initial_context_does_not_stop_a_task(self) -> None:
        (self.workspace / ".gitignore").write_text("[", encoding="utf-8")
        process, result, requests, events = self.run_task([stop("Ready.")], execution_mode="read-only")
        self.assertEqual((process.returncode, result["status"]), (0, "completed"))
        self.assertEqual([item["role"] for item in requests[0]["messages"]], ["system", "user"])
        self.assertEqual(next(event for event in events if event["type"] == "workspace_context_unavailable")["data"],
                         {"reason": "context_ignore_pattern"})

    def test_context_uses_real_git_index_without_touching_metadata(self) -> None:
        git = Path(os.environ["ProgramFiles"]) / "Git" / "cmd" / "git.exe"
        if not git.is_file():
            self.skipTest("Git for Windows is required for index fixtures")
        config = self.root / "empty-git-config"
        config.touch()
        env = {**os.environ, "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}
        root = self.workspace
        for algorithm, version in [("sha1", 2), ("sha1", 3), ("sha256", 4)]:
            with self.subTest(algorithm=algorithm, version=version):
                self.workspace = root / f"{algorithm}-{version}"
                self.workspace.mkdir()

                def git_run(*words: str) -> None:
                    subprocess.run([str(git), *words], cwd=self.workspace, env=env, check=True,
                                   capture_output=True, timeout=10)

                git_run("init", "--initial-branch=fixture-index", "--object-format=" + algorithm)
                ignored = self.workspace / "ignored"
                ignored.mkdir()
                (self.workspace / ".gitignore").write_bytes(b"ignored/\n")
                tracked = ignored / "tracked-\u9E48\u9E55.txt"
                tracked.write_bytes(b"index_marker original\n")
                (ignored / "untracked-hidden.txt").write_bytes(b"index_marker hidden marker")
                (self.workspace / "untracked-visible.txt").write_bytes(b"visible marker")
                (self.workspace / "deleted.txt").write_bytes(b"deleted marker")
                (self.workspace / "credentials.json").write_bytes(b"fixture-protected-contents")
                long_name = "b" * 132 + ".txt"
                (self.workspace / long_name).write_bytes(b"long path fixture\n")
                git_run("add", "-f", "--", ".gitignore", tracked.relative_to(self.workspace).as_posix(), "deleted.txt",
                        "credentials.json", long_name)
                if version == 3:
                    git_run("update-index", "--skip-worktree", "--", long_name)
                git_run("update-index", "--index-version", str(version))
                index = self.workspace / ".git" / "index"
                before = index.read_bytes()
                self.assertEqual(int.from_bytes(before[4:8], "big"), version)
                tracked.write_bytes(b"index_marker modified\n")
                (self.workspace / "deleted.txt").unlink()

                process, result, requests, _ = self.run_task([
                    tool("list_files"),
                    tool("search_code", "call-2", path="ignored", query="index_marker"),
                    tool("get_project_context", "call-3"),
                    stop("Git context verified."),
                ], task="Find index_marker.", execution_mode="read-only")
                self.assertEqual((process.returncode, result["status"]), (0, "completed"))
                listing, search, context = (request["messages"][-1]["content"] for request in requests[1:])
                relative = tracked.relative_to(self.workspace).as_posix()
                self.assertIn(relative, listing)
                self.assertIn(relative + ":1", search)
                for evidence in ["Git 索引 · fixture-index", "原始内容与索引不同 · " + relative,
                                 "工作树缺失 · deleted.txt", "未跟踪 · untracked-visible.txt"]:
                    self.assertIn(evidence, context)
                self.assertIn("Git 索引 · fixture-index", requests[0]["messages"][-2]["content"])
                for private in ["untracked-hidden.txt", "hidden marker", "credentials.json", "fixture-protected-contents"]:
                    self.assertNotIn(private, listing + search + context)
                self.assertEqual(index.read_bytes(), before)
                if version == 4:
                    index.write_bytes(before[:-1] + bytes([before[-1] ^ 1]))
                    process, result, requests, _ = self.run_task([stop("Fallback.")], execution_mode="read-only")
                    self.assertEqual((process.returncode, result["status"]), (0, "completed"))
                    fallback = requests[0]["messages"][-2]["content"]
                    self.assertIn("索引校验值或签名无效", fallback)
                    self.assertNotIn(relative, fallback)

    def test_linked_git_marker_stays_out_of_automatic_context(self) -> None:
        outside = self.root / "metadata"
        outside.mkdir()
        (outside / "HEAD").write_text("ref: refs/heads/private-worktree-branch", encoding="utf-8")
        (self.workspace / ".git").write_text("gitdir: " + str(outside), encoding="utf-8")
        (self.workspace / "fixture.txt").write_text("workspace file", encoding="utf-8")
        process, result, requests, _ = self.run_task([stop("Read-only.")], execution_mode="read-only")
        self.assertEqual((process.returncode, result["status"]), (0, "completed"))
        context = requests[0]["messages"][-2]["content"]
        self.assertIn("索引位于链接工作树", context)
        self.assertNotIn("private-worktree-branch", context)
        self.assertNotIn(str(outside), context)

    def test_nested_instructions_are_delivered_before_writing_same_batch(self) -> None:
        folder = self.workspace / "module"
        folder.mkdir()
        (folder / "AGENTS.md").write_text("Use scoped convention fixture.", encoding="utf-8")
        first, second = "module/first.txt", "module/second.txt"
        batched = frame({"tool_calls": [
            {"index": i, "id": f"first-{i}", "function": {"name": "write_file",
             "arguments": json.dumps({"path": path, "content": "new file"})}}
            for i, path in enumerate([first, second])
        ]}) + frame({}, "tool_calls") + "data: [DONE]\n\n"

        def after_guidance():
            self.assertFalse((self.workspace / first).exists())
            self.assertFalse((self.workspace / second).exists())
            (folder / "AGENTS.md").write_text("Use updated convention fixture.", encoding="utf-8")
            return tool("write_file", "after-guidance", path=second, content="new file")

        def after_refresh():
            self.assertFalse((self.workspace / second).exists())
            return tool("write_file", "after-refresh", path=second, content="new file")

        process, result, requests, _ = self.run_task([batched, after_guidance, after_refresh, stop()],
                                                      task="Make a note under module.")
        self.assertEqual((process.returncode, result["status"]), (0, "completed"))
        for response in requests[1]["messages"][-2:]:
            self.assertIn("本次尚未写入", response["content"])
            self.assertIn("Use scoped convention fixture.", response["content"])
        self.assertIn("Use updated convention fixture.", requests[2]["messages"][-1]["content"])
        self.assertEqual([item["path"] for item in result["files_changed"]], [second])

    def test_manual_mode_omits_commands_and_rejects_forged_calls_without_review(self) -> None:
        process, result, requests, events = self.run_task(
            [tool("run_command", argv=["node", "--version"])],
            execution_mode="manual", sandbox_image="scidev-fixture-missing:never-pull",
        )
        self.assertEqual((process.returncode, result["status"]), (1, "approval_denied"))
        names = [item["function"]["name"] for item in requests[0]["tools"]]
        self.assertNotIn("run_command", names)
        self.assertIn("write_file", names)
        self.assertFalse(any(event["type"] in ("tool_completed", "command_review_requested") for event in events))
        self.assertEqual(list((self.workspace / ".research").glob("scidev-command-*")), [])

    def test_workspace_write_defers_other_commands_and_never_falls_back_to_host(self) -> None:
        image = "scidev-fixture-missing:never-pull"
        for argv, expected in [
            (["node", "--version"], "approval_denied"),
            (["node", "--test", "../outside.test.cjs"], "approval_denied"),
            (["node", "--test", "tests/sample.test.cjs"], "sandbox_unavailable"),
        ]:
            with self.subTest(argv=argv):
                process, result, requests, events = self.run_task(
                    [tool("run_command", argv=argv)], execution_mode="workspace-write", sandbox_image=image,
                )
                self.assertEqual((process.returncode, result["status"]), (1, expected))
                self.assertIn("run_command", [item["function"]["name"] for item in requests[0]["tools"]])
                self.assertFalse(any(event["type"] in ("tool_completed", "command_auto_approved") for event in events))
                self.assertEqual(list((self.workspace / ".research").glob("scidev-command-*")), [])

    def test_command_review_option_rejects_noninteractive_output(self) -> None:
        args = ["dotnet", str(DLL), "exec", "--workspace", str(self.workspace), "--task", "Manual command test",
                "--api-base", "http://127.0.0.1:1/v1", "--model", "fixture",
                "--execution-mode", "manual", "--sandbox-image", "node:20-alpine", "--command-approval", "review"]
        plain = subprocess.run(args, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(plain.returncode, 2, plain.stderr)
        self.assertIn("[invalid_input]", plain.stdout)
        structured = subprocess.run([*args, "--json"], encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual((structured.returncode, json.loads(structured.stdout)["status"]), (2, "invalid_input"))
        self.assertFalse((self.workspace / ".research").exists())

    def test_docker_refuses_host_flags_and_model_credentials_in_arguments(self) -> None:
        for arguments, expected in [
            ({"argv": ["node", "--version"], "outside_workspace": True}, "invalid_tool"),
            ({"argv": ["node", FAKE_KEY]}, "sandbox_content_denied"),
            ({"argv": ["node", "--test"], "directory": "../outside"}, "path_denied"),
            ({"argv": ["node", "--test"], "directory": ".research"}, "path_denied"),
            ({"argv": ["node", "--test"], "directory": False}, "invalid_tool"),
        ]:
            with self.subTest(status=expected):
                process, result, _, events = self.run_task(
                    [tool("run_command", **arguments)], sandbox_image="scidev-fixture-missing:never-pull",
                )
                self.assertEqual((process.returncode, result["status"]), (1, expected))
                self.assertFalse(any(event["type"] == "tool_completed" for event in events))
                self.assertEqual(list((self.workspace / ".research").glob("scidev-command-*")), [])

    def test_read_only_mode_rejects_unadvertised_write(self) -> None:
        process, result, _, _ = self.run_task(
            [tool("write_file", path="rejected.txt", content="attempt")], execution_mode="read-only",
        )
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result["status"], "read_only")
        self.assertFalse((self.workspace / "rejected.txt").exists())

    def test_agent_git_diff_requires_the_interactive_approval_entry(self) -> None:
        process, result, requests, events = self.run_task([tool("git_diff")], "Review the Git diff")
        self.assertEqual((process.returncode, result["status"]), (1, "approval_denied"))
        self.assertNotIn("git_diff", [entry["function"]["name"] for entry in requests[0]["tools"]])
        self.assertFalse(any(event["type"].startswith("git_review_") for event in events))
        self.assertEqual(list((self.workspace / ".research").glob("scidev-git-query-*")), [])
        invalid = subprocess.run(["dotnet", str(DLL), "exec", "--workspace", str(self.workspace),
                                  "--task", "Review the Git diff", "--api-base", "http://127.0.0.1:1/v1",
                                  "--model", "fixture", "--git-approval", "review", "--json"],
                                 capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual((invalid.returncode, json.loads(invalid.stdout)["status"]), (2, "invalid_input"))

    def test_external_file_tools_are_denied_headless(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("private fixture", encoding="utf-8")
        process, result, requests, events = self.run_task(
            [tool("edit_external_file", path=str(outside), old_text="private fixture", new_text="changes")],
        )
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result["status"], "approval_denied")
        self.assertNotIn("edit_external_file", [item["function"]["name"] for item in requests[0]["tools"]])
        self.assertNotIn("outside_review_requested", [event["type"] for event in events])
        self.assertEqual(outside.read_text(encoding="utf-8"), "private fixture")

    def test_task_commit_requires_an_interactive_terminal(self) -> None:
        invalid = subprocess.run(["dotnet", str(DLL), "exec", "--workspace", str(self.workspace),
                                  "--task", "Create notes", "--api-base", "http://127.0.0.1:1/v1",
                                  "--model", "fixture", "--auto-commit", "review", "--json"],
                                 capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual((invalid.returncode, json.loads(invalid.stdout)["status"]), (2, "invalid_input"))
        self.assertFalse((self.workspace / ".research").exists())

    def test_cli_result_file_matches_stdout_and_replaces_previous_result(self) -> None:
        report = self.workspace / ".research/results/latest.json"
        flags = ("--allow-no-changes", "--result-file", ".research/results/latest.json")
        process, result, _, _ = self.run_task([stop(f"First report {FAKE_KEY}")], cli_flags=flags)
        self.assertEqual((process.returncode, result["status"], result["text"]), (0, "completed", "First report [redacted]"))
        self.assertEqual(report.read_text(encoding="utf-8"), process.stdout)
        process, result, _, _ = self.run_task([stop("Second report")], cli_flags=flags)
        self.assertEqual((process.returncode, result["text"]), (0, "Second report"))
        self.assertEqual(report.read_text(encoding="utf-8"), process.stdout)
        self.assertEqual(list(report.parent.iterdir()), [report])

    def test_cli_result_file_keeps_workspaces_and_managed_state_separate(self) -> None:
        state = self.workspace / ".research"
        state.mkdir()
        settings = state / "settings.json"
        settings.write_bytes(b'{"existing":true}')
        outside = self.root / "outside.json"
        outside.write_bytes(b"outside fixture")
        linked = self.workspace / "linked.json"
        os.link(outside, linked)
        for path in ("../outside.json", str(outside), ".research/settings.json", "linked.json"):
            with self.subTest(path=path):
                process, result, _, _ = self.run_task([stop("Report text")], cli_flags=("--allow-no-changes", "--result-file", path))
                self.assertEqual((process.returncode, result["status"]), (1, "completed"))
                self.assertIn("result file not written", process.stderr)
                self.assertEqual(outside.read_bytes(), b"outside fixture")
                self.assertEqual(settings.read_bytes(), b'{"existing":true}')

    def test_allow_no_changes_is_explicit_and_still_requires_a_finished_plan(self) -> None:
        process, result, _, _ = self.run_task([stop("An analysis result")])
        self.assertEqual((process.returncode, result["status"]), (1, "no_changes"))
        process, result, _, events = self.run_task([stop("An analysis result")], cli_flags=("--allow-no-changes",))
        self.assertEqual((process.returncode, result["status"], result["files_changed"]), (0, "completed", []))
        self.assertEqual(next(event["data"]["status"] for event in events if event["type"] == "run_finished"), "completed")
        process, result, _, _ = self.run_task([stop("Plan incomplete")], planning="required", max_turns=1,
                                              cli_flags=("--allow-no-changes",))
        self.assertEqual((process.returncode, result["status"]), (1, "plan_incomplete"))

    def test_request_timeout_bounds_a_stalled_model_and_overrides_the_environment(self) -> None:
        release = Event()

        def stalled_reply() -> str:
            release.wait(12)
            return tool("write_file", path="too-late.txt", content="Should never be applied")

        args = ["exec", "--workspace", str(self.workspace), "--api-base", "http://127.0.0.1:1/v1",
                "--task", "Timeout fixture", "--model", "fixture", "--json"]
        with patch.dict(os.environ, {"SCIDEV_REQUEST_TIMEOUT_SECONDS": "4.9"}):
            code, result = self.diagnostic(*args)
            self.assertEqual((code, result["status"]), (2, "invalid_input"))
            self.assertFalse((self.workspace / ".research").exists())
            try:
                process, result, requests, events = self.run_task([stalled_reply], cli_flags=("--request-timeout", "5.0"))
            finally:
                release.set()
        self.assertEqual((process.returncode, result["status"], len(requests)), (1, "model_timeout", 1))
        self.assertFalse(any(event["type"] == "tool_started" for event in events))
        self.assertFalse((self.workspace / "too-late.txt").exists())

    def test_cli_legacy_docker_selector_requires_an_image_and_disallows_local_backend(self) -> None:
        args = ["exec", "--workspace", str(self.workspace), "--api-base", "http://127.0.0.1:1/v1",
                "--task", "Docker fixture", "--model", "fixture", "--json"]
        for backend in ("local", "docker"):
            code, result = self.diagnostic(*args, "--sandbox", backend)
            self.assertEqual((code, result["status"]), (2, "invalid_input"))
        process, result, requests, _ = self.run_task([stop("Configured")], sandbox_image="scidev-fixture-missing:never-pull",
                                                     cli_flags=("--sandbox", "docker", "--allow-no-changes"))
        self.assertEqual((process.returncode, result["status"]), (0, "completed"))
        self.assertIn("run_command", [entry["function"]["name"] for entry in requests[0]["tools"]])

    def test_cli_images_remain_in_multiturn_user_message_without_entering_history(self) -> None:
        first = self.root / "external-attachment.png"
        first.write_bytes(base64.b64decode(TINY_PNG))
        second = self.workspace / "internal-attachment.gif"
        second.write_bytes(base64.b64decode(TINY_GIF))
        (self.workspace / "text.txt").write_text("Plain text file", encoding="utf-8")
        prompt = "Inspect the two images"
        process, result, requests, events = self.run_task([
            tool("read_file", path="text.txt"), stop(f"Image response {TINY_PNG}"),
        ], task=prompt, execution_mode="read-only", cli_flags=("--image", str(first), "--image", str(second)))
        self.assertEqual((process.returncode, result["status"], len(requests)), (0, "completed", 2))
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{TINY_PNG}"}},
                   {"type": "image_url", "image_url": {"url": f"data:image/gif;base64,{TINY_GIF}"}}]
        for request in requests:
            self.assertEqual([msg["content"] for msg in request["messages"] if msg["role"] == "user" and isinstance(msg["content"], list)],
                             [content])
        payload = next(event["data"] for event in events if event["type"] == "image_attachments")
        self.assertEqual(payload["images"], [{"format": "image/png", "byte_count": first.stat().st_size},
                                            {"format": "image/gif", "byte_count": second.stat().st_size}])
        journal = json.dumps(events)
        for private in (first.name, second.name, TINY_PNG, TINY_GIF, "Image response"):
            self.assertNotIn(private, journal)

    def test_cli_rejects_unsupported_and_over_limit_images_before_model_call(self) -> None:
        broken = self.root / "broken.png"
        broken.write_bytes(b"Not an image")
        large = self.root / "large.png"
        with large.open("wb") as file:
            file.write(base64.b64decode(TINY_PNG))
            file.truncate(4 * 1024 * 1024 + 1)
        too_large = self.root / "too-large.png"
        with too_large.open("wb") as file:
            file.write(base64.b64decode(TINY_PNG))
            file.truncate(5 * 1024 * 1024 + 1)
        args = ["exec", "--workspace", str(self.workspace), "--api-base", "http://127.0.0.1:1/v1",
                "--task", "Invalid attachment fixture", "--model", "fixture", "--json"]
        for paths in ([broken], [too_large], [large] * 3, [broken] * 5):
            with self.subTest(count=len(paths), example=paths[0].name):
                flags = [part for path in paths for part in ("--image", str(path))]
                code, result = self.diagnostic(*args, *flags)
                self.assertEqual((code, result["status"]), (2, "invalid_input"))
                self.assertFalse((self.workspace / ".research").exists())

    def test_eval_rejects_escaping_setup_and_suite_provider_overrides_before_running(self) -> None:
        suite = self.workspace / "suite.json"
        args = ["eval", "--suite", str(suite), "--workspace", str(self.workspace), "--api-base", "http://127.0.0.1:1/v1",
                "--model", "fixture", "--sandbox-image", "scidev-fixture-missing:never-pull", "--json"]
        task = {"id": "probe", "prompt": "Create output", "check": ["node", "-e", "0"]}
        for value in ({"tasks": [{**task, "setup": {"../outside.txt": "unexpected"}}]},
                      {"tasks": [task], "environment": {"SCIDEV_API_BASE": "https://example.invalid/"}},
                      {"tasks": [task, task]}):
            suite.write_text(json.dumps(value), encoding="utf-8")
            code, result = self.diagnostic(*args)
            self.assertEqual((code, result["status"]), (2, "invalid_input"))
            self.assertFalse((self.workspace / ".research").exists())
        suite.write_text(json.dumps({"tasks": [task]}), encoding="utf-8")
        code, result = self.diagnostic(*args, "--output", "../outside.json")
        self.assertEqual((code, result["status"]), (1, "path_denied"))
        self.assertFalse((self.root / "outside.json").exists())

    def test_mcp_cli_reads_legacy_configuration_without_launching_unapproved_services(self) -> None:
        command = ["dotnet", str(DLL), "mcp", "--workspace", str(self.workspace), "--json"]
        state = self.workspace / ".research"
        state.mkdir()
        config = state / "mcp.json"
        env = {**os.environ, "SCIDEV_NETWORK_POLICY": "ask"}

        def invoke(*args):
            process = subprocess.run([*command, *args], capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=15)
            self.assertEqual(process.stderr, b"")
            return process.returncode, json.loads(process.stdout)

        config.write_text(json.dumps({"enabled": False, "servers": {"unused": {"command": "missing.exe"}}}), encoding="utf-8")
        self.assertEqual(invoke(), (0, {"tools": [], "resources": [], "prompts": [], "errors": []}))
        config.write_text(json.dumps({"enabled": True, "servers": {
            "local-fixture": {"command": "missing.exe"},
            "remote": {"url": "https://example.invalid/mcp"},
        }}), encoding="utf-8")
        code, payload = invoke()
        self.assertEqual(code, 1)
        self.assertEqual(payload["tools"], [])
        self.assertIn("approval_required", payload["errors"][0])
        self.assertIn("approval_required", payload["errors"][1])
        code, payload = invoke("--read", "local-fixture", "fixture://entry")
        self.assertEqual((code, payload["status"]), (1, "approval_required"))
        code, payload = invoke("--prompt", "missing", "hello", "--arg", "who=pelican")
        self.assertEqual((code, payload["status"]), (1, "mcp_server_missing"))
        code, payload = invoke("--read", "local-fixture", "fixture://entry", "--arg", "who=pelican")
        self.assertEqual((code, payload["status"]), (2, "invalid_input"))

    def test_github_pr_queries_require_a_terminal_and_respect_offline_policy(self) -> None:
        env = {**os.environ, "SCIDEV_NETWORK_POLICY": "ask", "GH_TOKEN": FAKE_KEY}
        for command, extra in [("pr", []), ("pr-view", []), ("pr-diff", ["--max-chars", "30"]),
                               ("pr-review", ["--action", "comment", "--body", "fixture"])]:
            number = [] if command == "pr" else ["--number", "17"]
            result = subprocess.run(["dotnet", str(DLL), command, "--workspace", str(self.workspace),
                                     *number, "--json", *extra], env=env,
                                    capture_output=True, encoding="utf-8", timeout=10)
            self.assertEqual((result.returncode, json.loads(result.stdout)["status"]), (1, "approval_required"))
            self.assertFalse((self.workspace / ".research").exists())
        env["SCIDEV_NETWORK_POLICY"] = "deny"
        args = ["dotnet", str(DLL), "pr-view", "--workspace", str(self.workspace), "--number", "17", "--json"]
        result = subprocess.run(args, env=env, capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual((result.returncode, json.loads(result.stdout)["status"]), (1, "network_policy_denied"))
        result = subprocess.run([*args, "--allow-network"], env=env, capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual((result.returncode, json.loads(result.stdout)["status"]), (1, "approval_required"))

    def test_github_pr_dry_run_matches_legacy_plan_without_repo_changes(self) -> None:
        from scidev.integrations.pr import build_plan

        git = self.worktree_fixture()
        git("remote", "add", "origin", "https://github.com/example/fixture.git")
        git("branch", "feature")
        git("symbolic-ref", "HEAD", "refs/heads/feature")
        git("update-ref", "refs/remotes/origin/stable", git("rev-parse", "HEAD").decode().strip())
        git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/stable")
        index = self.workspace / ".git/index"
        config = self.workspace / ".git/config"
        original = (index.read_bytes(), config.read_bytes())
        expected = build_plan(self.workspace, title="  Example   title ", draft=True)
        code, value = self.diagnostic("pr", "--dry-run", "--workspace", str(self.workspace), "--title", "  Example   title ", "--draft", "--json")
        self.assertEqual(code, 0, value)
        self.assertEqual({key: value[key] for key in ("repo", "branch", "base", "title", "draft")},
                         {key: getattr(expected, key) for key in ("repo", "branch", "base", "title", "draft")})
        self.assertEqual(value["commit"], git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(value["commands"][0][-2:], ["https://github.com/example/fixture.git", value["commit"] + ":refs/heads/feature"])
        self.assertNotIn("--force", value["commands"][0])
        self.assertTrue(value["dry_run"])
        self.assertEqual((index.read_bytes(), config.read_bytes()), original)
        self.assertFalse((self.workspace / ".research").exists())

    def test_mcp_agent_needs_opt_in_and_rejects_forged_tool_names(self) -> None:
        state = self.workspace / ".research"
        state.mkdir()
        (state / "mcp.json").write_text(json.dumps({"enabled": True, "servers": {
            "fixture": {"url": "http://127.0.0.1:9/mcp"},
        }}), encoding="utf-8")
        process, result, requests, _ = self.run_task([tool("mcp__fixture__lookup", value="unapproved")])
        self.assertEqual((process.returncode, result["status"]), (1, "unsupported_tool"))
        self.assertEqual(len(requests), 1)
        self.assertFalse(any(item["function"]["name"].startswith("mcp__") for item in requests[0]["tools"]))
        before = sorted(state.glob("csharp-*.jsonl"))
        invalid = subprocess.run(["dotnet", str(DLL), "exec", "--workspace", str(self.workspace), "--task", "MCP test",
                                  "--api-base", "http://127.0.0.1:1/v1", "--model", "fixture", "--mcp-approval", "review", "--json"],
                                 capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual((invalid.returncode, json.loads(invalid.stdout)["status"]), (2, "invalid_input"))
        self.assertEqual(sorted(state.glob("csharp-*.jsonl")), before)

    def test_mcp_stdio_workspace_tools_recover_after_bad_frames(self) -> None:
        (self.workspace / "notes.txt").write_text(f"你好\n{FAKE_KEY}\n", encoding="utf-8")
        (self.workspace / ".env").write_text("hidden fixture", encoding="utf-8")
        (self.root / "outside.txt").write_text("outside test text", encoding="utf-8")
        (self.workspace / "package.json").write_text('{"name":"mcp-fixture"}', encoding="utf-8")

        def request(id, method, params=None):
            body = {"jsonrpc": "2.0", "id": id, "method": method}
            if params is not None:
                body["params"] = params
            return json.dumps(body, ensure_ascii=False).encode()

        def call(id, name, **args):
            return request(id, "tools/call", {"name": name, "arguments": args})

        frames = [request(0, "server/discover"), request(1, "tools/list"), b"{", b"\xff",
                  request(2, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fixture"}}),
                  b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
                  request(3, "tools/list"), call("unicode", "read_file", path="notes.txt"),
                  call(4, "search_code", query="你好", path="."), call(5, "get_project_context"),
                  call(6, "read_file", path=".env"), call(7, "read_file", path="../outside.txt"),
                  call(8, "list_files", depth=6), call(9, "read_file", path="notes.txt", unknown=42),
                  call(10, "write_file", path="mutated.txt", content="should not be written"),
                  b"{" + b"x" * 65_537, b'{"jsonrpc":"2.0","id":13,"method":"ping","method":"tools/list"}', request(12, "ping")]
        env = {**os.environ, "SCIDEV_API_KEY": FAKE_KEY}
        process = subprocess.run(["dotnet", str(DLL), "mcp-serve", "--workspace", str(self.workspace)],
                                 input=b"\r\n".join(frames) + b"\n", env=env, capture_output=True, timeout=25)
        self.assertEqual((process.returncode, process.stderr), (0, b""))
        self.assertNotIn(FAKE_KEY.encode(), process.stdout)
        self.assertNotIn(b"hidden fixture", process.stdout)
        self.assertNotIn(b"outside test text", process.stdout)
        messages = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([message["error"]["code"] for message in messages if message["id"] is None], [-32700, -32700, -32600, -32700])
        by_id = {message["id"]: message for message in messages if message["id"] is not None}
        self.assertEqual(by_id[0]["error"]["code"], -32601)
        self.assertEqual(by_id[1]["error"]["code"], -32002)
        self.assertEqual(by_id[2]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual({tool["name"] for tool in by_id[3]["result"]["tools"]},
                         {"git_diff", "get_project_context", "list_files", "read_file", "search_code"})
        self.assertIn("你好", by_id["unicode"]["result"]["content"][0]["text"])
        self.assertIn("[redacted]", by_id["unicode"]["result"]["content"][0]["text"])
        self.assertIn("notes.txt", by_id[4]["result"]["content"][0]["text"])
        self.assertIn("package.json", by_id[5]["result"]["content"][0]["text"])
        self.assertTrue(all(by_id[i]["result"]["isError"] for i in (6, 7, 8, 9)))
        self.assertEqual(by_id[10]["error"]["code"], -32602)
        self.assertEqual(by_id[12]["result"], {})
        self.assertFalse((self.workspace / "mutated.txt").exists())
        self.assertFalse((self.workspace / ".research").exists())

    def test_mcp_stdio_connects_with_the_legacy_python_client_and_reads_local_git(self) -> None:
        from scidev.integrations.mcp import MCPClient, MCPError

        self.worktree_fixture()
        (self.workspace / "seed.txt").write_bytes(b"readable changed text\n")
        index = self.workspace / ".git/index"
        before = index.read_bytes()
        client = MCPClient("csharp", "dotnet", [str(DLL), "mcp-serve", "--workspace", str(self.workspace)],
                           self.workspace, env={"TEMP": str(self.root), "TMP": str(self.root)}, timeout=10)
        client.start()
        try:
            client.initialize()
            self.assertEqual(len(client.list_tools()), 5)
            self.assertIn("readable changed text", client.call_tool("read_file", {"path": "seed.txt"}))
            diff = client.call_tool("git_diff", {})
            self.assertIn("-commit baseline", diff)
            self.assertIn("+readable changed text", diff)
            with self.assertRaises(MCPError):
                client.call_tool("run_command", {"argv": ["echo", "unexpected"]})
        finally:
            client.close()
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse((self.workspace / ".research").exists())
        self.assertEqual(list(self.root.glob("scidev-mcp-git-*")), [])

    def session_command(self, endpoint: str, task: str, *flags: str, session: str = "experiment-17") -> list[str]:
        return ["dotnet", str(DLL), "exec", "--workspace", str(self.workspace), "--api-base", endpoint,
                "--model", "offline-fixture", "--task", task, "--session-id", session, "--json", *flags]

    def invoke_session(self, endpoint: str, task: str, *flags: str, session: str = "experiment-17") -> tuple[int, dict]:
        process = subprocess.run(self.session_command(endpoint, task, *flags, session=session),
                                 env={**os.environ, "SCIDEV_API_KEY": FAKE_KEY}, capture_output=True, encoding="utf-8", timeout=20)
        return process.returncode, json.loads(process.stdout)

    def test_named_session_continues_with_prior_answer_and_old_listing_contract(self) -> None:
        state = self.workspace / ".research"
        (state / "sessions").mkdir(parents=True)
        legacy = state / "sessions/session_python.json"
        legacy.write_text('{"session_id":"session_python","status":"completed","prompt":"Legacy Python run"}', encoding="utf-8")
        replies = [tool("write_file", path="notes.txt", content="Existing experiment data"), stop("Prior findings"), stop("Follow-up findings")]
        with model(replies) as (url, requests):
            code, initial = self.invoke_session(url, "Write experiment notes")
            self.assertEqual((code, initial["summary"], initial["turn_count"], initial["usage"]["total_tokens"]), (0, "Prior findings", 2, 42))
            self.assertEqual(initial["checkpoint_id"], initial["run_id"])
            journal = self.workspace / initial["events_path"]
            before = journal.read_bytes()
            code, followup = self.invoke_session(url, "Review the experiment", "--execution-mode", "read-only")
            self.assertEqual((code, followup["session_id"], followup["summary"], followup["checkpoint_id"]),
                             (0, "experiment-17", "Follow-up findings", ""))
            self.assertNotEqual(followup["run_id"], initial["run_id"])
            self.assertEqual(journal.read_bytes(), before)
            previous = [message["content"] for message in requests[-1]["messages"] if "Prior completed attempts" in (message["content"] or "")]
            self.assertEqual(len(previous), 1)
            self.assertIn('"Task":"Write experiment notes"', previous[0])
            self.assertIn('"Answer":"Prior findings"', previous[0])
            code, denied = self.invoke_session(url, "Use a Python session", session="session_python")
            self.assertEqual((code, denied["status"]), (1, "legacy_session_requires_python"))
            self.assertEqual(len(requests), 3)
        code, denied = self.invoke_session("http://127.0.0.1:1/v1", "Change endpoint")
        self.assertEqual((code, denied["status"]), (1, "session_model_endpoint_changed"))
        code, rows = self.diagnostic("sessions", "--workspace", str(self.workspace), "--json")
        self.assertEqual(code, 0)
        self.assertEqual({row["session_id"]: row["prompt"] for row in rows},
                         {"experiment-17": "Review the experiment", "session_python": "Legacy Python run"})
        self.assertTrue(all(set(row) == {"session_id", "status", "prompt", "updated_at"} for row in rows))
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8"))["prompt"], "Legacy Python run")

    def test_generated_session_id_can_resume_a_cli_run(self) -> None:
        with model([stop("First answer"), stop("Second answer")]) as (url, requests):
            args = self.session_command(url, "Ask first", "--execution-mode", "read-only")
            flag = args.index("--session-id")
            del args[flag:flag + 2]
            process = subprocess.run(args, env={**os.environ, "SCIDEV_API_KEY": FAKE_KEY}, capture_output=True, encoding="utf-8", timeout=20)
            initial = json.loads(process.stdout)
            self.assertEqual((process.returncode, initial["session_id"]), (0, initial["run_id"]))
            code, followup = self.invoke_session(url, "Ask second", "--execution-mode", "read-only", session=initial["session_id"])
            self.assertEqual((code, followup["session_id"]), (0, initial["session_id"]))
            self.assertIn("First answer", json.dumps(requests[1]))

    def test_python_session_recovers_persisted_results_without_replay(self) -> None:
        from scidev.agent.session import save_session, session_path
        from scidev.runtime.filelock import FileLock
        from scidev.runtime.helpers import coding_task_lock_path
        from scidev.runtime.ledger import EventLedger

        session = "python-recovery"
        prompt = "Continue the Python experiment"
        ledger = EventLedger(self.workspace)
        path = session_path(self.workspace, session)
        legacy_key = "sk-fixture-old-token-123456"
        calls = [
            {"id": "old-persisted", "type": "function", "function": {"name": "write_file", "arguments":
                json.dumps({"path": "saved.txt", "content": "Original persisted text"})}},
            {"id": "old-started", "type": "function", "function": {"name": "replace_in_file", "arguments":
                json.dumps({"path": "uncertain.txt", "old_text": "prior", "new_text": "do not repeat"})}},
            {"id": "old-pending", "type": "function", "function": {"name": "write_file", "arguments":
                json.dumps({"path": "never.txt", "content": "was never executed"})}},
        ]
        old_plan = {"revision": 2, "explanation": "", "steps": [
            {"step": "Save the first result", "status": "completed"},
            {"step": "Write the follow-up", "status": "in_progress"},
        ]}
        old_run = {
            "session_id": session, "status": "active", "prompt": prompt, "last_prompt": prompt,
            "task_plan": old_plan, "agent_changed_paths": ["saved.txt"],
            "messages": [
                {"role": "system", "content": "Legacy permissions may no longer apply."},
                {"role": "user", "content": f"Original task context {FAKE_KEY} {legacy_key}"},
                {"role": "assistant", "content": "", "reasoning_content": "Historical reasoning", "tool_calls": calls},
            ],
        }
        save_session(old_run, path=path, project_root=self.workspace, ledger=ledger)
        ledger.prepare_tool_calls(session, 2, calls)
        ledger.start_tool_call(session, 2, 0, "old-persisted", "write_file")
        ledger.complete_tool_call(session, 2, 0, "OBSERVATION_MARKER: old output was recorded")
        ledger.start_tool_call(session, 2, 1, "old-started", "replace_in_file")
        (self.workspace / "saved.txt").write_text("Original persisted text", encoding="utf-8")
        (self.workspace / "uncertain.txt").write_text("existing uncertain content", encoding="utf-8")
        before = path.read_bytes(), ledger.db_path.read_bytes()
        replies = [
            tool("write_file", "new-write", path="followup.txt", content="Continued in C#"),
            tool("update_plan", "new-plan", plan=[
                {"step": "Save the first result", "status": "completed"},
                {"step": "Write the follow-up", "status": "completed"},
            ]),
            stop("Finished the resumed task"),
            stop("Followed up after the resumed task"),
        ]
        flags = ("--resume-python-session",)
        with model(replies) as (endpoint, requests):
            with FileLock(coding_task_lock_path(self.workspace), timeout=0):
                code, busy = self.invoke_session(endpoint, prompt, *flags, session=session)
                self.assertEqual((code, busy["status"]), (1, "legacy_session_busy"))
            code, unconfirmed = self.invoke_session(endpoint, prompt, *flags, session=session)
            self.assertEqual((code, unconfirmed["status"]), (1, "legacy_session_needs_inspection"))
            self.assertEqual(requests, [])
            code, inspection = self.diagnostic("inspect-run", "--workspace", str(self.workspace),
                                               "--source", "python", "--run-id", session)
            self.assertEqual(code, 0)
            recovery = inspection["continuation"]
            self.assertTrue(recovery["requires_inspection"])
            self.assertEqual({item["id"]: item["status"] for item in recovery["recovered_tools"]},
                             {"old-persisted": "completed", "old-started": "started", "old-pending": "pending"})
            code, stale = self.invoke_session(endpoint, prompt, *flags, "--accept-python-recovery", "0" * 64,
                                              session=session)
            self.assertEqual((code, stale["status"]), (1, "legacy_session_needs_inspection"))
            code, resumed = self.invoke_session(endpoint, prompt, *flags, "--accept-python-recovery", recovery["revision"],
                                                session=session)
            self.assertEqual((code, resumed["status"], resumed["summary"]), (0, "completed", "Finished the resumed task"), resumed)
            self.assertEqual([item["path"] for item in resumed["files_changed"]], ["followup.txt"])
            history = requests[0]["messages"]
            self.assertEqual([item["role"] for item in history if "Legacy permissions" in (item["content"] or "")], ["user"])
            self.assertEqual([item["tool_call_id"] for item in history if item["role"] == "tool"],
                             ["old-persisted", "old-started", "old-pending"])
            self.assertIn("OBSERVATION_MARKER", next(item["content"] for item in history if item.get("tool_call_id") == "old-persisted"))
            self.assertIn("Effects are uncertain", next(item["content"] for item in history if item.get("tool_call_id") == "old-started"))
            self.assertIn("did not start", next(item["content"] for item in history if item.get("tool_call_id") == "old-pending"))
            self.assertNotIn(FAKE_KEY, json.dumps(requests[0]))
            self.assertNotIn(legacy_key, json.dumps(requests[0]))
            self.assertEqual(history[3]["reasoning_content"], "Historical reasoning")
            self.assertNotIn("OBSERVATION_MARKER", (self.workspace / resumed["events_path"]).read_text(encoding="utf-8"))

            code, next_run = self.invoke_session(endpoint, "Ask a follow-up", *flags, "--execution-mode", "read-only", session=session)
            self.assertEqual((code, next_run["status"]), (0, "completed"))
            self.assertIn("Finished the resumed task", json.dumps(requests[-1]["messages"]))
            self.assertEqual((path.read_bytes(), ledger.db_path.read_bytes()), before)
            self.assertEqual((self.workspace / "uncertain.txt").read_text(encoding="utf-8"), "existing uncertain content")
            self.assertFalse((self.workspace / "never.txt").exists())
            path.write_bytes(before[0] + b"\n")
            code, changed = self.invoke_session(endpoint, "Detect changed old source", *flags, session=session)
            self.assertEqual((code, changed["status"], len(requests)), (1, "legacy_session_changed", 4))
        code, entries = self.diagnostic("sessions", "--workspace", str(self.workspace), "--json")
        self.assertEqual(code, 0)
        self.assertEqual([item["session_id"] for item in entries].count(session), 1)

    def test_python_session_image_and_completed_tool_id_are_preserved_safely(self) -> None:
        from scidev.agent.session import save_session, session_path
        from scidev.runtime.ledger import EventLedger

        ledger = EventLedger(self.workspace)
        session = "python-images"
        path = session_path(self.workspace, session)
        old_task = {
            "session_id": session, "status": "completed", "prompt": "Describe the reference",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Reference image"},
                                             {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{TINY_PNG}"}}]},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "prior-action", "type": "function", "function":
                    {"name": "read_file", "arguments": '{"path":"notes.txt"}'}}]},
                {"role": "tool", "tool_call_id": "prior-action", "content": "Read completed in Python"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "prior-action", "type": "function", "function":
                    {"name": "read_file", "arguments": r'{"path":"\u0073k-old-secret-123456789"}'}}]},
                {"role": "tool", "tool_call_id": "prior-action", "content": "Second historical result"},
                {"role": "assistant", "content": "Historical conclusion"},
            ],
        }
        save_session(old_task, path=path, project_root=self.workspace, ledger=ledger)
        with model([tool("write_file", "prior-action", path="replayed.txt", content="must not occur")]) as (endpoint, requests):
            code, denied = self.invoke_session(endpoint, "Continue with the reference", "--resume-python-session", session=session)
            self.assertEqual((code, denied["status"], len(requests)), (1, "model_error", 1))
            self.assertFalse((self.workspace / "replayed.txt").exists())
            self.assertIn(f"data:image/png;base64,{TINY_PNG}", json.dumps(requests[0]))
            self.assertNotIn(TINY_PNG, (self.workspace / denied["events_path"]).read_text(encoding="utf-8"))
            self.assertIn("Historical conclusion", json.dumps(requests[0]))
            messages = requests[0]["messages"]
            calls = [call for item in messages for call in item.get("tool_calls", [])]
            self.assertNotEqual(calls[0]["id"], calls[1]["id"])
            self.assertEqual([item["tool_call_id"] for item in messages if item["role"] == "tool"], [call["id"] for call in calls])
            self.assertEqual(json.loads(calls[1]["function"]["arguments"])["path"], "[redacted]")

    def test_session_rejects_concurrent_attempt_and_requires_exact_inspection_recovery(self) -> None:
        reached = Event()
        release = Event()

        def stalled() -> str:
            reached.set()
            release.wait(15)
            return stop("Abandoned response")

        with model([tool("write_file", path="already-written.txt", content="keep this"), stalled, stop("Recovery confirmed")]) as (url, requests):
            process = subprocess.Popen(self.session_command(url, "Write once"), env={**os.environ, "SCIDEV_API_KEY": FAKE_KEY},
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertTrue(reached.wait(12), "the first tool should have finished")
                self.assertEqual((self.workspace / "already-written.txt").read_text(encoding="utf-8"), "keep this")
                code, busy = self.invoke_session(url, "Concurrent retry")
                self.assertEqual((code, busy["status"]), (1, "session_busy"))
                run = next((self.workspace / ".research").glob("csharp-*.jsonl"))
                old_id = run.stem.removeprefix("csharp-")
            finally:
                process.kill()
                process.wait(10)
                release.set()
            before = run.read_bytes()
            code, blocked = self.invoke_session(url, "Retry after an interruption")
            self.assertEqual((code, blocked["status"]), (1, "session_needs_inspection"))
            code, mismatch = self.invoke_session(url, "Recover", "--retry-after-inspection", "a" * 32)
            self.assertEqual((code, mismatch["status"]), (1, "session_retry_mismatch"))
            code, inspect = self.diagnostic("inspect-run", "--workspace", str(self.workspace), "--run-id", old_id)
            self.assertEqual((code, inspect["status"]), (1, "interrupted"))
            code, after = self.invoke_session(url, "Confirm state after inspection", "--execution-mode", "read-only",
                                              "--retry-after-inspection", old_id)
            self.assertEqual((code, after["status"], len(requests)), (0, "completed", 3))
            self.assertEqual(run.read_bytes(), before)
            self.assertEqual((self.workspace / "already-written.txt").read_text(encoding="utf-8"), "keep this")
            entries = [json.loads(line) for line in (self.workspace / after["events_path"]).read_text(encoding="utf-8").splitlines()]
            link = next(entry["data"] for entry in entries if entry["type"] == "session_attempt")
            self.assertEqual(link["reviewed_run_id"], old_id)
            self.assertIsNone(link["previous_run_id"])

    def test_named_session_reuses_managed_worktree_and_excludes_images_from_later_context(self) -> None:
        self.worktree_fixture()
        image = self.root / "survey.png"
        image.write_bytes(base64.b64decode(TINY_PNG))
        with model([stop("Image-derived details"), stop("New answer")]) as (url, requests):
            code, first = self.invoke_session(url, "Look at a private image", "--worktree", "--execution-mode", "read-only", "--image", str(image))
            self.assertEqual((code, first["status"]), (0, "completed"))
            code, mismatch = self.invoke_session(url, "Leave the worktree", "--execution-mode", "read-only")
            self.assertEqual((code, mismatch["status"]), (1, "session_workspace_changed"))
            code, second = self.invoke_session(url, "Continue in the same worktree", "--worktree", "--execution-mode", "read-only")
            self.assertEqual((code, second["worktree"], second["worktree_base_sha"]),
                             (0, first["worktree"], first["worktree_base_sha"]))
            self.assertEqual(len(requests), 2)
            self.assertNotIn("Image-derived details", json.dumps(requests[1]))
            self.assertNotIn(TINY_PNG, json.dumps(requests[1]))
        code, rows = self.diagnostic("sessions", "--workspace", str(self.workspace), "--json")
        self.assertEqual((code, len(rows), rows[0]["session_id"], rows[0]["prompt"]),
                         (0, 1, "experiment-17", "Continue in the same worktree"))

    def test_host_command_is_denied_when_it_was_not_enabled(self) -> None:
        command = tool("run_host_command", executable=r"C:\fixture-does-not-exist.exe", argv=[])
        for mode in (None, "read-only", "manual", "workspace-write"):
            with self.subTest(mode=mode):
                process, result, requests, events = self.run_task([command], execution_mode=mode)
                self.assertEqual((process.returncode, result["status"]), (1, "approval_denied"))
                self.assertNotIn("run_host_command", [entry["function"]["name"] for entry in requests[0]["tools"]])
                self.assertFalse(any(event["type"].startswith("host_") for event in events))

    def test_host_review_option_rejects_noninteractive_output(self) -> None:
        invalid = subprocess.run(["dotnet", str(DLL), "exec", "--workspace", str(self.workspace), "--task", "Host test",
                                  "--api-base", "http://127.0.0.1:1/v1", "--model", "fixture", "--host-approval", "review", "--json"],
                                 capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual((invalid.returncode, json.loads(invalid.stdout)["status"]), (2, "invalid_input"))
        self.assertFalse((self.workspace / ".research").exists())

    def test_legacy_policy_restores_read_only_and_denies_extra_network(self) -> None:
        state = self.workspace / ".research"
        state.mkdir()
        settings = state / "settings.json"
        saved = '{"schema_version":2,"execution_mode":"read-only","network_policy":"deny","sandbox_backend":"docker","sandbox_image":"node:20-alpine"}'
        settings.write_text(saved, encoding="utf-8")
        process, result, requests, events = self.run_task([stop("Legacy policy applied.")], use_legacy_policy=True)
        self.assertEqual((process.returncode, result["status"]), (0, "completed"), result)
        self.assertEqual({item["function"]["name"] for item in requests[0]["tools"]},
                         {"read_file", "list_files", "search_code", "get_project_context"})
        self.assertEqual(next(event["data"] for event in events if event["type"] == "execution_policy"),
                         {"mode": "ReadOnly", "network": "Deny", "container": False, "host_review": False, "network_review": False})
        for call in (tool("fetch_external_text", url="https://example.invalid/"),
                     tool("run_host_command", executable=r"C:\fixture-does-not-exist.exe", argv=[])):
            process, result, _, events = self.run_task([call], use_legacy_policy=True, execution_mode="workspace-approval")
            self.assertEqual((process.returncode, result["status"]), (1, "network_policy_denied"))
            self.assertFalse(any(event["type"].startswith(("host_", "network_review_")) for event in events))
        self.assertEqual(settings.read_text(encoding="utf-8"), saved)

    def test_legacy_full_access_requires_an_explicit_mode_and_profiles_do_not_apply_implicitly(self) -> None:
        state = self.workspace / ".research"
        state.mkdir()
        (state / "settings.json").write_text('{"execution_mode":"full-access","network_policy":"allow"}', encoding="utf-8")
        args = ["dotnet", str(DLL), "exec", "--workspace", str(self.workspace), "--task", "Inspect old policy",
                "--api-base", "http://127.0.0.1:1/v1", "--model", "fixture", "--use-legacy-policy", "--json"]
        refused = subprocess.run(args, capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual((refused.returncode, json.loads(refused.stdout)["status"]), (2, "invalid_input"))
        self.assertFalse(list(state.glob("csharp-*.jsonl")))
        without_backend = subprocess.run(args + ["--execution-mode", "full-access"], capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual((without_backend.returncode, json.loads(without_backend.stdout)["status"]), (2, "invalid_input"))
        process, result, requests, events = self.run_task([stop("Read-only description.")], use_legacy_policy=True, execution_mode="read-only")
        self.assertEqual((process.returncode, result["status"]), (0, "completed"))
        self.assertEqual(next(event["data"]["network"] for event in events if event["type"] == "execution_policy"), "Ask")
        self.assertNotIn("fetch_external_text", [item["function"]["name"] for item in requests[0]["tools"]])
        (state / "settings.json").write_text('{"execution_mode":"read-only","network_policy":"deny"}', encoding="utf-8")
        process, result, _, _ = self.run_task([tool("write_file", path="explicit-workspace.txt", content="current opt-in"), stop()])
        self.assertEqual((process.returncode, result["status"]), (0, "completed"))

    def test_full_access_preflights_managed_git_and_denies_external_tools(self) -> None:
        for call, expected in (
            (tool("run_command", argv=["git", "-C", ".", "add", "."]), "sandbox_command_blocked"),
            (tool("run_host_command", executable=r"C:\fixture-does-not-exist.exe", argv=[]), "network_policy_denied"),
            (tool("fetch_external_text", url="https://example.invalid/"), "network_policy_denied"),
        ):
            process, result, requests, events = self.run_task(
                [call], execution_mode="full-access", sandbox_image="scidev-fixture-missing:never-pull", network_policy="deny",
            )
            self.assertEqual((process.returncode, result["status"]), (1, expected), result)
            tools = {item["function"]["name"] for item in requests[0]["tools"]}
            self.assertIn("run_command", tools)
            self.assertTrue(tools.isdisjoint({"run_host_command", "fetch_external_text", "read_external_file"}))
            self.assertFalse(any(event["type"].startswith(("command_auto_approved", "host_review_", "network_review_")) for event in events))

    def test_legacy_command_schema_requires_opt_in_and_never_selects_the_host(self) -> None:
        image = "scidev-fixture-missing:never-pull"
        state = self.workspace / ".research"
        state.mkdir()
        (state / "settings.json").write_text(json.dumps({"execution_mode": "workspace-approval", "sandbox_backend": "docker",
                                                       "sandbox_image": image, "network_policy": "deny"}), encoding="utf-8")
        request = tool("run_command", command="echo fixture", outside_workspace=True)
        for enabled, expected in ((False, "invalid_tool"), (True, "network_policy_denied")):
            with self.subTest(enabled=enabled):
                process, result, requests, events = self.run_task([request], use_legacy_policy=enabled, sandbox_image=image)
                self.assertEqual((process.returncode, result["status"]), (1, expected))
                command = next(item["function"] for item in requests[0]["tools"] if item["function"]["name"] == "run_command")
                self.assertEqual("command" in command["parameters"]["properties"], enabled)
                self.assertFalse(any(event["type"].startswith(("host_", "command_review_")) for event in events))
        (state / "settings.json").write_text('{"execution_mode":"workspace-approval"}', encoding="utf-8")
        process, result, requests, events = self.run_task([request], use_legacy_policy=True)
        self.assertEqual((process.returncode, result["status"]), (1, "host_approval_required"))
        self.assertNotIn("run_command", [entry["function"]["name"] for entry in requests[0]["tools"]])
        self.assertFalse(any(event["type"].startswith("host_") for event in events))

    def test_extra_https_tool_requires_interactive_review(self) -> None:
        process, result, requests, events = self.run_task([tool("fetch_external_text", url="https://example.org/")])
        self.assertEqual((process.returncode, result["status"]), (1, "approval_denied"))
        self.assertNotIn("fetch_external_text", [item["function"]["name"] for item in requests[0]["tools"]])
        self.assertNotIn("network_review_requested", [event["type"] for event in events])

        process = subprocess.run(
            ["dotnet", str(DLL), "exec", "--workspace", str(self.workspace), "--task", "Network review fixture",
             "--api-base", "http://127.0.0.1:1/v1", "--model", "fixture", "--network-approval", "review", "--json"],
            capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual((process.returncode, json.loads(process.stdout)["status"]), (2, "invalid_input"))

    def test_windows_credential_requires_explicit_endpoint(self) -> None:
        process = subprocess.run(
            ["dotnet", str(DLL), "exec", "--workspace", str(self.workspace), "--task", "Credential test",
             "--model", "fixture", "--credential-profile", "never-read-this-fixture", "--json"],
            env={**os.environ, "SCIDEV_API_BASE": "http://127.0.0.1:1/v1", "SCIDEV_API_KEY": FAKE_KEY},
            capture_output=True, encoding="utf-8", timeout=10,
        )
        result = json.loads(process.stdout)
        self.assertEqual((process.returncode, result["status"]), (2, "invalid_input"))
        self.assertIn("--api-base", result["text"])
        self.assertFalse((self.workspace / ".research").exists())

    def test_history_lists_and_opens_csharp_and_legacy_transcripts(self) -> None:
        sessions = self.workspace / ".research" / "sessions"
        sessions.mkdir(parents=True)
        legacy = sessions / "session_legacy.json"
        legacy.write_text(json.dumps({
            "session_id": "session_legacy", "prompt": "Old task", "status": "completed",
            "updated_at": "2025-01-01T00:00:00+00:00",
            "task_plan": {"steps": [{"step": "Review old fixtures", "status": "completed"}], "revision": 2,
                          "updated_at": "2025-01-01T00:00:00+00:00"},
            "verification_report": {
                "status": "failed",
                "current_fingerprint": "legacy database fingerprint",
                "latest": {"check_status": "failed", "kind": "test", "exit_code": 1,
                           "command": "legacy command with private arguments"},
            },
            "messages": [
                {"role": "assistant", "content": "Old result."},
                {"role": "tool", "content": "Internal tool details."},
            ],
        }), encoding="utf-8")
        os.utime(legacy, (1735689600, 1735689600))
        process, result, _, _ = self.run_task([stop("New result.")], "New task", execution_mode="read-only")
        self.assertEqual(process.returncode, 0, result)
        args = ("--workspace", str(self.workspace))
        code, history = self.diagnostic("history", *args, "--limit", "2")
        self.assertEqual(code, 0, history)
        self.assertEqual([(run["source"], run["run_id"], run["task"]) for run in history["runs"]],
                         [("csharp", result["run_id"], "New task"), ("python", "session_legacy", "Old task")])
        code, opened = self.diagnostic("inspect-run", *args, "--run-id", "session_legacy", "--source", "python")
        self.assertEqual(code, 0, opened)
        self.assertEqual(opened["summary"], "Old result.")
        self.assertEqual(opened["plan"]["steps"], [{"step": "Review old fixtures", "status": "completed"}])
        self.assertEqual((opened["verification"]["status"], opened["verification"]["category"],
                          opened["verification"]["exit_code"]), ("failed", "test", 1))
        self.assertIsNone(opened["verification"]["passed_directories"])
        for hidden in ["Internal tool details.", "legacy database fingerprint", "legacy command with private arguments"]:
            self.assertNotIn(hidden, json.dumps(opened))

        old_id = "1" * 32
        old_journal = sessions.parent / f"csharp-{old_id}.jsonl"
        rows = [
            {"type": "verification_reported", "data": {"status": "passed", "category": "test", "exit_code": 0}},
            {"type": "run_finished", "data": {"status": "completed"}},
        ]
        old_journal.write_text("".join(json.dumps({"run_id": old_id, **row}) + "\n" for row in rows), encoding="utf-8")
        code, opened = self.diagnostic("inspect-run", *args, "--run-id", old_id)
        self.assertEqual(code, 0, opened)
        self.assertEqual(opened["verification"]["status"], "passed")
        self.assertIsNone(opened["verification"]["passed_directories"])

    def test_git_status_and_diff_are_read_only_and_hide_protected_paths(self) -> None:
        git = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/cmd/git.exe"
        if not git.is_file():
            self.skipTest("Requires a local Git for Windows installation")
        empty_config = self.root / "git-config"
        empty_config.touch()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty_config),
               "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}

        def local_git(*words: str) -> None:
            subprocess.run([str(git), *words], cwd=self.workspace, env=env,
                           capture_output=True, timeout=10, check=True)

        local_git("init", "--initial-branch=main")
        (self.workspace / "modified.txt").write_bytes(b"one\n")
        (self.workspace / "old name.txt").write_bytes(b"a stable rename target\n")
        (self.workspace / ".env").write_bytes(b"protected value\n")
        local_git("add", "--", "modified.txt", "old name.txt", ".env")
        local_git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture")
        (self.workspace / "modified.txt").write_bytes(b"staged\n")
        local_git("add", "--", "modified.txt")
        (self.workspace / "modified.txt").write_bytes(b"working\n")
        local_git("mv", "old name.txt", "new name.txt")
        (self.workspace / ".env").write_bytes(b"updated protected value\n")
        (self.workspace / "new file.txt").write_bytes(b"new fixture\n")
        local_git("config", "include.path", str(self.root / "not-selected-config"))
        sparse = self.workspace / ".git"
        protected = [sparse / "index", sparse / "HEAD", sparse / "config", self.workspace / "modified.txt"]
        before = [file.read_bytes() for file in protected]
        args = ("--workspace", str(self.workspace))

        code, status = self.diagnostic("git-status", *args)
        self.assertEqual(code, 0, status)
        self.assertEqual(status["branch"], "main")
        self.assertEqual({row["path"]: (row["code"], row["original"]) for row in status["changes"]},
                         {"modified.txt": ("MM", None), "new name.txt": ("R ", "old name.txt"),
                          "new file.txt": ("??", None)})
        code, changed = self.diagnostic("git-diff", *args, "--path", "modified.txt")
        self.assertEqual(code, 0, changed)
        self.assertIn("-one", changed["text"])
        self.assertIn("+working", changed["text"])
        code, renamed = self.diagnostic("git-diff", *args, "--path", "new name.txt", "--original", "old name.txt")
        self.assertEqual(code, 0, renamed)
        self.assertIn("rename from old name.txt", renamed["text"])
        code, new = self.diagnostic("git-diff", *args, "--path", "new file.txt")
        self.assertEqual(code, 0, new)
        self.assertIn("+new fixture", new["text"])
        code, denied = self.diagnostic("git-diff", *args, "--path", ".env")
        self.assertEqual((code, denied["status"]), (1, "path_denied"))
        self.assertEqual([file.read_bytes() for file in protected], before)
        self.assertEqual(list((self.workspace / ".research").glob("scidev-git-query-*")), [])
        linked = self.root / "linked"
        local_git("worktree", "add", "--detach", str(linked), "HEAD")
        (linked / "modified.txt").write_bytes(b"linked change\n")
        linked_args = ("--workspace", str(linked))
        code, required = self.diagnostic("git-status", *linked_args)
        self.assertEqual((code, required["status"]), (1, "git_linked_worktree"))
        code, rejected = self.diagnostic("git-status", *linked_args, "--metadata-workspace", str(self.root))
        self.assertEqual((code, rejected["status"]), (1, "git_worktree_mismatch"))
        approval = ("--metadata-workspace", str(self.workspace))
        code, linked_status = self.diagnostic("git-status", *linked_args, *approval)
        self.assertEqual(code, 0, linked_status)
        self.assertIsNone(linked_status["branch"])
        self.assertEqual([row["path"] for row in linked_status["changes"]], ["modified.txt"])
        code, linked_diff = self.diagnostic("git-diff", *linked_args, "--path", "modified.txt", *approval)
        self.assertEqual(code, 0, linked_diff)
        self.assertIn("+linked change", linked_diff["text"])

        # The linked index changes independently of the main repository index.
        main_index = (self.workspace / ".git/index").read_bytes()
        code, required = self.diagnostic("git-stage", *linked_args, "--path", "modified.txt")
        self.assertEqual((code, required["status"]), (1, "git_linked_worktree"))
        code, staged = self.diagnostic("git-stage", *linked_args, "--path", "modified.txt", *approval)
        self.assertEqual((code, staged["status"]), (0, "staged"))
        code, staged_status = self.diagnostic("git-status", *linked_args, *approval)
        self.assertEqual([(row["path"], row["code"]) for row in staged_status["changes"]], [("modified.txt", "M ")])
        (linked / "extra.txt").write_text("linked worktree addition\n", encoding="utf-8")
        code, required = self.diagnostic("git-stage-all", *linked_args)
        self.assertEqual((code, required["status"]), (1, "git_linked_worktree"))
        code, staged_all = self.diagnostic("git-stage-all", *linked_args, *approval)
        self.assertEqual((code, staged_all["paths"]), (0, ["extra.txt"]))
        self.assertEqual(main_index, (self.workspace / ".git/index").read_bytes())

        # Respect a lock already owned by another Git invocation.
        index_lock = self.workspace / ".git/index.lock"
        index_lock.write_bytes(b"other operation")
        code, locked = self.diagnostic("git-stage", *args, "--path", "new file.txt")
        self.assertEqual((code, locked["status"]), (1, "git_index_busy"))
        self.assertEqual(index_lock.read_bytes(), b"other operation")
        index_lock.unlink()

        code, staged = self.diagnostic("git-stage", *args, "--path", "new file.txt")
        self.assertEqual((code, staged["status"]), (0, "staged"))
        code, staged_status = self.diagnostic("git-status", *args)
        self.assertIn(("new file.txt", "A "), [(row["path"], row["code"]) for row in staged_status["changes"]])
        code, unstaged = self.diagnostic("git-unstage", *args, "--path", "new file.txt")
        self.assertEqual((code, unstaged["status"]), (0, "unstaged"))
        self.assertEqual((self.workspace / "new file.txt").read_bytes(), b"new fixture\n")
        current_index = (self.workspace / ".git/index").read_bytes()
        (self.workspace / ".gitattributes").write_text("filtered.txt filter=example\n", encoding="utf-8")
        (self.workspace / "filtered.txt").write_text("fixture", encoding="utf-8")
        code, filtered = self.diagnostic("git-stage", *args, "--path", "filtered.txt")
        self.assertEqual((code, filtered["status"]), (1, "git_filter_required"))
        self.assertEqual((self.workspace / ".git/index").read_bytes(), current_index)
        self.assertFalse(index_lock.exists())

    def test_git_stage_all_uses_current_visible_files_and_publishes_atomically(self) -> None:
        git = self.worktree_fixture()
        args = ("--workspace", str(self.workspace))
        for name in ("rename old.txt", "gone.txt", "already-staged.txt", ".env"):
            (self.workspace / name).write_bytes(f"fixture {name}\n".encode())
        git("add", "--", "rename old.txt", "gone.txt", "already-staged.txt", ".env")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "Add bulk stage fixtures")
        staged_only = self.workspace / "already-staged.txt"
        staged_only.write_bytes(b"retain this staged file\n")
        target = self.workspace / "seed.txt"
        target.write_bytes(b"partly staged fixture\n")
        git("add", "--", "already-staged.txt", "seed.txt")
        target.write_bytes(b"current working version\n")
        (self.workspace / "rename old.txt").rename(self.workspace / "rename new.txt")
        (self.workspace / "gone.txt").unlink()
        (self.workspace / ".env").write_bytes(b"protected local edit\n")
        (self.workspace / "new 中文 file.txt").write_bytes(b"new fixture\n")
        (self.workspace / "ignored.tmp").write_bytes(b"ignored fixture\n")
        (self.workspace / ".research").mkdir(exist_ok=True)
        (self.workspace / ".research/run.jsonl").write_bytes(b"local state\n")

        code, staged = self.diagnostic("git-stage-all", *args)
        self.assertEqual((code, staged["status"], staged["skipped"]), (0, "staged", 1), staged)
        self.assertEqual(set(staged["paths"]), {"seed.txt", "rename old.txt", "rename new.txt", "gone.txt", "new 中文 file.txt"})
        self.assertEqual(git("show", ":seed.txt"), b"current working version\n")
        self.assertEqual(git("show", ":already-staged.txt"), b"retain this staged file\n")
        self.assertEqual(git("show", ":.env"), b"fixture .env\n")
        self.assertEqual(set(git("diff", "--cached", "--name-only", "--no-renames", "-z").decode().split("\0")) - {""},
                         {*staged["paths"], "already-staged.txt"})

        index_path = self.workspace / ".git/index"
        before = index_path.read_bytes()
        (self.workspace / "ordinary.txt").write_bytes(b"new fixture\n")
        (self.workspace / ".gitattributes").write_bytes(b"filter-required.txt filter=fixture\n")
        (self.workspace / "filter-required.txt").write_bytes(b"requires named filter\n")
        code, filtered = self.diagnostic("git-stage-all", *args)
        self.assertEqual((code, filtered["status"]), (1, "git_filter_required"), filtered)
        self.assertEqual(index_path.read_bytes(), before)
        lock = self.workspace / ".git/index.lock"
        self.assertFalse(lock.exists())
        lock.write_bytes(b"external Git lock fixture")
        code, locked = self.diagnostic("git-stage-all", *args)
        self.assertEqual((code, locked["status"]), (1, "git_index_busy"))
        self.assertEqual(lock.read_bytes(), b"external Git lock fixture")

    def test_git_init_refuses_an_enclosing_repo_and_keeps_existing_metadata(self) -> None:
        self.worktree_fixture()
        index = self.workspace / ".git/index"
        config = self.workspace / ".git/config"
        before = (index.read_bytes(), config.read_bytes())
        child = self.workspace / "nested" / "child"
        child.mkdir(parents=True)
        code, refused = self.diagnostic("git-init", "--workspace", str(child))
        self.assertEqual((code, refused["status"]), (1, "git_nested_workspace"))
        self.assertFalse((child / ".git").exists())
        self.assertFalse((child / ".research").exists())

        code, existing = self.diagnostic("git-init", "--workspace", str(self.workspace), "--initial-branch", "unused")
        self.assertEqual((code, existing["status"], existing["branch"]), (0, "already_initialized", "main"))
        self.assertEqual((index.read_bytes(), config.read_bytes()), before)

    def test_git_init_creates_an_independent_repository_without_inherited_settings(self) -> None:
        temp = Path(tempfile.gettempdir()).resolve()
        if any((folder / ".git").exists() for folder in (temp, *temp.parents)):
            self.skipTest("Initialization requires an independent temporary directory outside the checkout")
        with tempfile.TemporaryDirectory(prefix="scidev-init-", dir=temp) as folder:
            workspace = Path(folder) / "repo"
            workspace.mkdir()
            redirected = Path(folder) / "redirected"
            template = Path(folder) / "template"
            template.mkdir()
            (template / "extraneous-setting").write_bytes(b"fixture")
            process = subprocess.run(
                ["dotnet", str(DLL), "git-init", "--workspace", str(workspace), "--initial-branch", "research/demo"],
                env={**os.environ, "GIT_DIR": str(redirected), "GIT_TEMPLATE_DIR": str(template)},
                capture_output=True, encoding="utf-8", timeout=15,
            )
            self.assertEqual((process.returncode, json.loads(process.stdout)),
                             (0, {"status": "initialized", "branch": "research/demo"}))
            self.assertEqual((workspace / ".git/HEAD").read_text().strip(), "ref: refs/heads/research/demo")
            self.assertIn("scidev@localhost", (workspace / ".git/config").read_text())
            self.assertFalse((workspace / ".git/extraneous-setting").exists())
            self.assertFalse(redirected.exists())
            self.assertEqual(list((workspace / ".research").iterdir()), [])

    def test_git_stage_all_skips_potential_protected_renames(self) -> None:
        git = self.worktree_fixture()
        args = ("--workspace", str(self.workspace))
        harness = self.workspace / ".research"
        harness.mkdir(exist_ok=True)
        hidden = harness / "private.bin"
        hidden.write_bytes(b"internal fixture\n")
        (self.workspace / ".env").write_bytes(b"credential fixture\n")
        git("add", "--force", "--", ".env", ".research/private.bin")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "Add protected path fixtures")
        index = self.workspace / ".git/index"
        before = index.read_bytes()
        hidden.rename(self.workspace / "moved-history.txt")
        code, suppressed = self.diagnostic("git-stage-all", *args)
        self.assertEqual((code, suppressed["status"], suppressed["paths"], suppressed["skipped"]),
                         (0, "nothing_to_stage", [], 1), suppressed)
        self.assertEqual(index.read_bytes(), before)

        (self.workspace / ".env").rename(self.workspace / "moved-configuration.txt")
        (self.workspace / "seed.txt").write_bytes(b"independent tracked edit\n")
        code, visible = self.diagnostic("git-stage-all", *args)
        self.assertEqual((code, visible["status"], visible["paths"]), (0, "staged", ["seed.txt"]), visible)
        self.assertGreaterEqual(visible["skipped"], 2)
        self.assertEqual(git("diff", "--cached", "--name-only", "-z"), b"seed.txt\0")

    def test_git_commit_only_publishes_the_reviewed_index_and_approved_worktree(self) -> None:
        git = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/cmd/git.exe"
        if not git.is_file():
            self.skipTest("Requires a local Git for Windows installation")
        config = self.root / "empty-config"
        config.touch()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config),
               "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}

        def git_run(*words: str, cwd: Path = self.workspace) -> bytes:
            return subprocess.run([str(git), *words], cwd=cwd, env=env, capture_output=True,
                                  timeout=10, check=True).stdout

        args = ("--workspace", str(self.workspace))
        author = ("--name", "Fixture Author", "--email", "fixture@example.test")
        metadata = self.workspace / ".git"
        tracked = self.workspace / "experiment.txt"
        git_run("init", "--initial-branch=main")
        tracked.write_bytes(b"staged first\n")
        git_run("add", "--", tracked.name)
        tracked.write_bytes(b"unstaged after preview\n")
        (self.workspace / "other.txt").write_bytes(b"untracked\n")
        index = (metadata / "index").read_bytes()

        code, preview = self.diagnostic("git-commit-preview", *args)
        self.assertEqual((code, preview["reference"], preview["parent"], preview["paths"]),
                         (0, "refs/heads/main", None, [tracked.name]))
        code, first = self.diagnostic("git-commit", *args, "--message", "实验首个提交", *author)
        self.assertEqual((code, first["status"], first["file_count"]), (0, "committed", 1))
        self.assertEqual(git_run("log", "-1", "--format=%s").strip().decode("utf-8"), "实验首个提交")
        self.assertEqual(git_run("show", "HEAD:experiment.txt"), b"staged first\n")
        self.assertEqual(git_run("ls-tree", "--name-only", "HEAD").strip(), b"experiment.txt")
        self.assertEqual(tracked.read_bytes(), b"unstaged after preview\n")
        self.assertEqual((metadata / "index").read_bytes(), index)
        code, empty = self.diagnostic("git-commit", *args, "--message", "no stages", *author)
        self.assertEqual((code, empty["status"]), (1, "git_no_staged"))
        (self.workspace / "intended.txt").write_bytes(b"working tree only\n")
        git_run("add", "-N", "--", "intended.txt")
        code, intention = self.diagnostic("git-commit-preview", *args)
        self.assertEqual((code, intention["status"]), (1, "git_no_staged"))
        index = (metadata / "index").read_bytes()

        linked = self.root / "second-worktree"
        git_run("worktree", "add", "-b", "review/topic", str(linked))
        (linked / tracked.name).write_bytes(b"linked worktree change\n")
        git_run("add", "--", tracked.name, cwd=linked)
        linked_args = ("--workspace", str(linked))
        code, denied = self.diagnostic("git-commit", *linked_args, "--message", "linked scope", *author)
        self.assertEqual((code, denied["status"]), (1, "git_linked_worktree"))
        code, linked_commit = self.diagnostic("git-commit", *linked_args, "--metadata-workspace", str(self.workspace),
                                             "--message", "linked scope", *author)
        self.assertEqual((code, linked_commit["status"], linked_commit["reference"]), (0, "committed", "refs/heads/review/topic"))
        self.assertEqual(git_run("rev-parse", "HEAD", cwd=linked).strip().decode(), linked_commit["commit_id"])
        self.assertEqual(git_run("rev-parse", "HEAD").strip().decode(), first["commit_id"])
        self.assertEqual((metadata / "index").read_bytes(), index)

        hooks = metadata / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "reference-transaction").write_bytes(b"#!/bin/sh\necho hook > hook-marker.txt\n")
        git_run("add", "--", tracked.name)
        code, further = self.diagnostic("git-commit", *args, "--message", "second commit", *author)
        self.assertEqual((code, further["status"]), (0, "committed"))
        self.assertFalse((self.workspace / "hook-marker.txt").exists())
        self.assertEqual(git_run("rev-parse", "HEAD^").strip().decode(), first["commit_id"])
        self.assertNotIn(b"intended.txt", git_run("ls-tree", "--name-only", "HEAD"))
        self.assertEqual(git_run("reflog", "-1", "--format=%H").strip().decode(), further["commit_id"])
        self.assertFalse((metadata / "index.lock").exists())

    def test_git_commit_rejects_protected_paths_and_unsafe_metadata(self) -> None:
        git = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/cmd/git.exe"
        if not git.is_file():
            self.skipTest("Requires a local Git for Windows installation")
        config = self.root / "empty-config"
        config.touch()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config)}

        def git_run(*words: str) -> None:
            subprocess.run([str(git), *words], cwd=self.workspace, env=env,
                           capture_output=True, timeout=10, check=True)

        args = ("--workspace", str(self.workspace), "--message", "fixture", "--name", "Fixture", "--email", "f@example.test")
        metadata = self.workspace / ".git"
        git_run("init", "--initial-branch=main")
        (self.workspace / ".env").write_bytes(b"protected fixture\n")
        git_run("add", "--", ".env")
        code, protected = self.diagnostic("git-commit", *args)
        self.assertEqual((code, protected["status"]), (1, "git_protected_index"))
        git_run("rm", "--cached", "--", ".env")
        (self.workspace / "example.txt").write_bytes(b"normal fixture\n")
        git_run("add", "--", "example.txt")

        marker = metadata / "MERGE_HEAD"
        marker.touch()
        code, busy = self.diagnostic("git-commit", *args)
        self.assertEqual((code, busy["status"]), (1, "git_commit_in_progress"))
        marker.unlink()
        for key, value, expected in [
            ("include.path", str(self.root / "external-config"), "git_commit_config_include"),
            ("commit.gpgsign", "true", "git_commit_signing"),
        ]:
            git_run("config", "--local", key, value)
            code, denied = self.diagnostic("git-commit", *args)
            self.assertEqual((code, denied["status"]), (1, expected))
            git_run("config", "--local", "--unset", key)
        held = metadata / "index.lock"
        held.write_bytes(b"other operation")
        code, locked = self.diagnostic("git-commit", *args)
        self.assertEqual((code, locked["status"]), (1, "git_index_busy"))
        self.assertEqual(held.read_bytes(), b"other operation")
        self.assertFalse((metadata / "refs/heads/main").exists())

    def test_managed_worktree_uses_commit_and_only_removes_a_clean_directory(self) -> None:
        git = self.worktree_fixture()
        tracked = self.workspace / "seed.txt"
        tracked.write_bytes(b"root staged edit\n")
        git("add", "--", "seed.txt")
        tracked.write_bytes(b"root later edit\n")
        index = (self.workspace / ".git/index").read_bytes()
        sha = git("rev-parse", "HEAD").decode().strip()
        args = ("--workspace", str(self.workspace))
        git("config", "--local", "filter.fixture.smudge", "fixture-filter-command")
        code, filtered = self.diagnostic("git-worktree-create", *args, "--worktree-id", "review-one")
        self.assertEqual((code, filtered["status"]), (1, "git_worktree_filter"))
        self.assertFalse((self.workspace / ".git/refs/heads/scidev/agent-review-one").exists())
        git("config", "--local", "--unset", "filter.fixture.smudge")

        directory = self.workspace / ".research/worktrees/already-present"
        directory.mkdir(parents=True)
        sentinel = directory / "user-file.txt"
        sentinel.write_bytes(b"keep")
        code, occupied = self.diagnostic("git-worktree-create", *args, "--worktree-id", "already-present")
        self.assertEqual((code, occupied["status"]), (1, "git_worktree_path_exists"))
        self.assertEqual(sentinel.read_bytes(), b"keep")

        code, created = self.diagnostic("git-worktree-create", *args, "--worktree-id", "review-one")
        self.assertEqual((code, created["branch"], created["base_sha"]), (0, "scidev/agent-review-one", sha))
        isolated = Path(created["path"])
        self.assertEqual(isolated, self.workspace / ".research/worktrees/review-one")
        self.assertEqual((isolated / "seed.txt").read_bytes(), b"commit baseline\n")
        self.assertFalse((isolated / "checkout-hook-ran.txt").exists())
        self.assertEqual(tracked.read_bytes(), b"root later edit\n")
        self.assertEqual((self.workspace / ".git/index").read_bytes(), index)
        code, duplicate = self.diagnostic("git-worktree-create", *args, "--worktree-id", "review-one")
        self.assertEqual((code, duplicate["status"]), (1, "git_worktree_branch_exists"))
        code, external = self.diagnostic("git-worktree-list", "--workspace", str(isolated))
        self.assertEqual((code, external["status"]), (1, "git_linked_worktree"))
        code, listed = self.diagnostic("git-worktree-list", "--workspace", str(isolated),
                                       "--metadata-workspace", str(self.workspace))
        self.assertEqual(code, 0, listed)
        self.assertEqual({Path(item["path"]) for item in listed["worktrees"]}, {self.workspace, isolated})
        approved_args = ("--workspace", str(isolated), "--metadata-workspace", str(self.workspace))
        code, nested_tree = self.diagnostic("git-worktree-create", *approved_args, "--worktree-id", "nested-review")
        self.assertEqual(code, 0, nested_tree)
        self.assertEqual(Path(nested_tree["path"]), isolated / ".research/worktrees/nested-review")
        code, nested_removed = self.diagnostic("git-worktree-remove", *approved_args, "--worktree-id", "nested-review")
        self.assertEqual((code, nested_removed["status"]), (0, "removed"))
        self.assertTrue(isolated.is_dir())

        ignored = isolated / "ignored.tmp"
        ignored.write_bytes(b"ignored user data")
        code, refused = self.diagnostic("git-worktree-remove", *args, "--worktree-id", "review-one")
        self.assertEqual((code, refused["status"]), (1, "git_worktree_dirty"))
        self.assertEqual(ignored.read_bytes(), b"ignored user data")
        ignored.unlink()
        retained_log = isolated / ".research/run.jsonl"
        retained_log.parent.mkdir(exist_ok=True)
        retained_log.write_bytes(b"retained local run")
        code, retained = self.diagnostic("git-worktree-remove", *args, "--worktree-id", "review-one")
        self.assertEqual((code, retained["status"]), (1, "git_worktree_dirty"))
        self.assertEqual(retained_log.read_bytes(), b"retained local run")
        retained_log.unlink()
        code, removed = self.diagnostic("git-worktree-remove", *args, "--worktree-id", "review-one")
        self.assertEqual((code, removed["status"]), (0, "removed"))
        self.assertFalse(isolated.exists())
        self.assertEqual(git("rev-parse", "refs/heads/scidev/agent-review-one").decode().strip(), sha)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(tracked.read_bytes(), b"root later edit\n")

    def test_cli_exec_worktree_keeps_task_writes_and_history_in_the_linked_workspace(self) -> None:
        git = self.worktree_fixture()
        (self.workspace / "seed.txt").write_bytes(b"uncommitted original workspace\n")
        process, result, requests, events = self.run_task([
            tool("read_file", path="seed.txt"),
            tool("write_file", call_id="write-report", path="report.txt", content="isolated result\n"),
            stop(),
        ], worktree_id="task-result", cli_flags=("--result-file", "audit/result.json"))
        self.assertEqual((process.returncode, result["worktree_branch"]), (0, "scidev/agent-task-result"))
        self.assertEqual(result["worktree_base_sha"], git("rev-parse", "HEAD").decode().strip())
        isolated = Path(result["worktree"])
        self.assertEqual(isolated, Path(result["workspace"]))
        self.assertEqual((isolated / "audit/result.json").read_text(encoding="utf-8"), process.stdout)
        self.assertFalse((self.workspace / "audit").exists())
        self.assertIn("commit baseline", requests[1]["messages"][-1]["content"])
        self.assertEqual((isolated / "report.txt").read_bytes(), b"isolated result\n")
        self.assertFalse((self.workspace / "report.txt").exists())
        self.assertTrue(any(event["type"] == "tool_completed" for event in events))
        code, retained = self.diagnostic("git-worktree-remove", "--workspace", str(self.workspace), "--worktree-id", "task-result")
        self.assertEqual((code, retained["status"]), (1, "git_worktree_dirty"))
        self.assertTrue((isolated / result["events_path"]).exists())

    def worktree_fixture(self) -> Callable[..., bytes]:
        executable = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/cmd/git.exe"
        if not executable.is_file():
            self.skipTest("Requires a local Git for Windows installation")
        empty = self.root / "git-config-empty"
        empty.touch()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty),
               "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}

        def git(*args: str) -> bytes:
            return subprocess.run([str(executable), *args], cwd=self.workspace, env=env,
                                  capture_output=True, check=True, timeout=10).stdout

        git("init", "--initial-branch=main")
        (self.workspace / "seed.txt").write_bytes(b"commit baseline\n")
        (self.workspace / ".gitignore").write_bytes(b".research/\nignored.tmp\n")
        git("add", "--", "seed.txt", ".gitignore")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture")
        hooks = self.workspace / ".git/hooks"
        (hooks / "post-checkout").write_bytes(b"#!/bin/sh\necho marker > checkout-hook-ran.txt\n")
        return git

    def diagnostic(self, *args: str) -> tuple[int, dict]:
        process = subprocess.run(
            ["dotnet", str(DLL), *args], encoding="utf-8", capture_output=True, timeout=10,
        )
        self.assertTrue(process.stdout, process.stderr)
        return process.returncode, json.loads(process.stdout)

    def test_interrupted_file_write_is_flagged_for_inspection(self) -> None:
        run_id = "a" * 32
        state = self.workspace / ".research"
        state.mkdir()
        journal = state / f"csharp-{run_id}.jsonl"
        start = {"run_id": run_id, "type": "tool_started", "data": {"id": "call-1", "name": "write_file"}}
        raw = json.dumps(start) + '\n{"type":"tool_completed"'
        journal.write_text(raw, encoding="utf-8")
        code, inspection = self.diagnostic("inspect-run", "--workspace", str(self.workspace), "--run-id", run_id)
        self.assertEqual(code, 1, inspection)
        self.assertEqual(inspection["status"], "needs_inspection")
        self.assertEqual(inspection["unconfirmed_tools"], [{"id": "call-1", "name": "write_file"}])
        self.assertEqual(journal.read_text(encoding="utf-8"), raw)

    def test_restore_after_interrupted_write(self) -> None:
        written, release = Event(), Event()

        def wait_for_stop() -> str:
            written.set()
            release.wait(timeout=20)
            return stop()

        with model([tool("write_file", path="created.txt", content="task output"), wait_for_stop]) as (url, _):
            with subprocess.Popen(
                ["dotnet", str(DLL), "exec", "--workspace", str(self.workspace), "--api-base", url,
                 "--model", "offline-fixture", "--task", "Create the fixture", "--json"],
                env={**os.environ, "SCIDEV_API_KEY": FAKE_KEY},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ) as process:
                try:
                    self.assertTrue(written.wait(timeout=12))
                    journal, = (self.workspace / ".research").glob("csharp-*.jsonl")
                    run_id = journal.stem.removeprefix("csharp-")
                    args = ("--workspace", str(self.workspace), "--run-id", run_id)
                    self.assertEqual(self.diagnostic("restore-run", *args), (1, {"status": "run_active"}))
                    self.assertEqual((self.workspace / "created.txt").read_text(encoding="utf-8"), "task output")
                finally:
                    process.kill()
                    process.wait(timeout=5)
                    release.set()
        # The CLI was terminated after publication. Simulate loss of its completion entry.
        lines = journal.read_text(encoding="utf-8").splitlines()
        last = next(i for i, line in enumerate(lines) if json.loads(line)["type"] == "tool_started")
        journal.write_text("\n".join(lines[:last + 1]) + "\n", encoding="utf-8")
        self.assertEqual(self.diagnostic("inspect-run", *args)[1]["status"], "needs_inspection")
        code, restored = self.diagnostic("restore-run", *args)
        self.assertEqual(code, 0, restored)
        self.assertEqual(restored["deleted"], ["created.txt"])
        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertEqual(self.diagnostic("restore-run", *args)[1]["deleted"], [])

    def test_failure_restore_preserves_external_edit(self) -> None:
        def edit_before_failure() -> str:
            (self.workspace / "keep.txt").write_text("user edit", encoding="utf-8")
            return tool("read_file", "failure", path="../outside.txt")

        process, result, _, events = self.run_task(
            [tool("write_file", path="remove.txt", content="generated"),
             tool("write_file", "second", path="keep.txt", content="generated"), edit_before_failure],
            restore_on_failure=True,
        )
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result["status"], "path_denied")
        self.assertEqual(result["checkpoint_restore"], {
            "status": "restore_conflict", "restored": [], "deleted": ["remove.txt"],
            "failed": {"keep.txt": "edit_conflict"},
        })
        self.assertEqual(events[-1]["type"], "checkpoint_restore")
        self.assertFalse((self.workspace / "remove.txt").exists())
        self.assertEqual((self.workspace / "keep.txt").read_text(encoding="utf-8"), "user edit")

    def test_legacy_preview_reads_sqlite_wal_without_changing_data(self) -> None:
        state = self.workspace / ".research"
        (state / "sessions").mkdir(parents=True)
        (state / "sessions" / "older.json").write_text('{}', encoding="utf-8")
        (state / "settings.json").write_text('{"schema_version":2}', encoding="utf-8")
        (state / "provider.json").write_text(json.dumps({
            "model": "legacy-model", "api_key": FAKE_KEY, "base_url": "https://example.test/v1",
        }), encoding="utf-8")
        with closing(sqlite3.connect(state / "runs.db")) as connection, connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA wal_autocheckpoint=0;
                CREATE TABLE events(id INTEGER);
                INSERT INTO events VALUES (1), (2);
                CREATE TABLE sessions(id TEXT);
                INSERT INTO sessions VALUES ('older');
                CREATE TABLE tool_call_journal(id TEXT);
                INSERT INTO tool_call_journal VALUES ('call-1');
            """)
            paths = [state / "runs.db", state / "runs.db-wal", state / "settings.json"]
            before = [path.read_bytes() for path in paths]
            code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--dry-run")
            self.assertEqual(code, 0, result)
            self.assertEqual(result["status"], "ready_to_import")
            self.assertTrue(result["provider_config_present"])
            self.assertEqual(result["session_files"], 1)
            self.assertEqual(result["indexed_events"], 2)
            self.assertEqual(result["indexed_sessions"], 1)
            self.assertEqual(result["tool_journal_entries"], 1)
            self.assertEqual([path.read_bytes() for path in paths], before)
            snapshot = self.root / "converted"
            code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--snapshot", str(snapshot))
            self.assertEqual(code, 0, result)
            self.assertEqual(result["status"], "imported")
            self.assertEqual(result["counts"]["indexed_events"], 2)
            copied = snapshot / ".research"
            self.assertEqual((copied / "sessions" / "older.json").read_text(encoding="utf-8"), '{}')
            self.assertEqual(json.loads((copied / "provider.json").read_bytes()), {
                "model": "legacy-model", "base_url": "https://example.test/v1",
            })
            with closing(sqlite3.connect(copied / "runs.db")) as exported, exported:
                self.assertEqual(exported.execute("SELECT id FROM events ORDER BY id").fetchall(), [(1,), (2,)])
                exported.execute("DELETE FROM events")
            self.assertEqual(connection.execute("SELECT count(*) FROM events").fetchone()[0], 2)
            self.assertEqual([path.read_bytes() for path in paths], before)
            code, retry = self.diagnostic("migrate", "--workspace", str(self.workspace), "--snapshot", str(snapshot))
            self.assertEqual(code, 1, retry)
            self.assertEqual(retry["status"], "destination_exists")

    def test_legacy_import_does_not_create_sidecars_next_to_frozen_database(self) -> None:
        state = self.workspace / ".research"
        state.mkdir()
        with closing(sqlite3.connect(state / "runs.db")) as connection, connection:
            connection.executescript("PRAGMA journal_mode=WAL; CREATE TABLE events (id); INSERT INTO events VALUES (1);")
        before = {file.name: file.read_bytes() for file in state.iterdir()}
        self.assertEqual(set(before), {"runs.db"})
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--snapshot", str(self.root / "imported"))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["counts"]["indexed_events"], 1)
        self.assertEqual({file.name: file.read_bytes() for file in state.iterdir()}, before)

    def test_legacy_preview_rejects_unknown_schema(self) -> None:
        state = self.workspace / ".research"
        state.mkdir()
        settings = state / "settings.json"
        settings.write_bytes(b'{"schema_version":99}')
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--dry-run")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["status"], "unsupported_schema")
        self.assertEqual(settings.read_bytes(), b'{"schema_version":99}')

    def test_legacy_policy_migration_audits_supported_prefixes_without_printing_them(self) -> None:
        state = self.workspace / ".research"
        state.mkdir()
        settings = state / "settings.json"
        saved = {
            "schema_version": 2, "execution_mode": "workspace_write", "network_policy": "off",
            "sandbox_backend": "docker", "sandbox_image": "node:20-alpine",
            "allowed_commands": ["node probe.cjs", "node probe.cjs", "node key-check.cjs; disallowed-fixture", "node ../other.cjs"],
        }
        settings.write_text(json.dumps(saved), encoding="utf-8")
        expected = {"status": "available", "execution": "workspace-write", "network": "deny", "backend": "docker",
                    "docker_image_present": True, "container_prefixes": 1, "ignored_prefixes": 2, "effective_network": "deny"}
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--dry-run")
        self.assertEqual((code, result["policy"]), (0, expected), result)
        self.assertNotIn("probe.cjs", json.dumps(result))
        destination = self.root / "policy-import"
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--snapshot", str(destination))
        self.assertEqual((code, result["counts"]["policy"]), (0, expected), result)
        self.assertEqual((destination / ".research/settings.json").read_bytes(), settings.read_bytes())
        saved["network_policy"] = "unknown"
        settings.write_text(json.dumps(saved), encoding="utf-8")
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--dry-run")
        self.assertEqual((code, result["policy"]["status"]), (0, "invalid"))

    def test_legacy_qt_preferences_export_import_and_backup(self) -> None:
        if not ORACLE:
            self.skipTest("Requires the original PySide6 QSettings (--oracle)")
        from PySide6.QtCore import QSettings

        ini = self.root / "copied-qt.ini"
        source = QSettings(str(ini), QSettings.Format.IniFormat)
        source.setValue("uiProfile", "focus")
        source.setValue("workbenchRatios/v2/focus", [0.24, 0.19])
        source.setValue("workbenchRatios/focus", [0.20, 0.20])
        source.setValue("workbenchRatios/studio", [0.81, 0.15])
        source.setValue("lastWorkspace", "C:/private-directory")
        source.setValue("unrelated", FAKE_KEY)
        source.sync()
        original = ini.read_bytes()
        exported = self.root / "qt-preferences.json"
        process = subprocess.run(
            [sys.executable, str(ROOT / "scripts/export_qt_preferences.py"), "--ini", str(ini), "--output", str(exported)],
            capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        expected = {"version": 1, "profile": "focus", "qt_layouts": {"focus": [0.24, 0.19], "studio": [0.81, 0.15]}}
        self.assertEqual(json.loads(exported.read_bytes()), expected)
        self.assertEqual(ini.read_bytes(), original)
        (self.workspace / ".research").mkdir()
        target = self.root / "appearance-snapshot"
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--snapshot", str(target),
                                       "--qt-preferences", str(exported))
        self.assertEqual(code, 0, result)
        self.assertTrue(result["qt_preferences_imported"])
        self.assertEqual(json.loads((target / ".research/desktop-preferences.json").read_bytes()), expected)
        self.assertEqual(list((self.workspace / ".research").iterdir()), [])
        exported.write_text('{"version":99,"profile":"paper"}', encoding="utf-8")
        failed = self.root / "unsupported-snapshot"
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--snapshot", str(failed),
                                       "--qt-preferences", str(exported))
        self.assertEqual((code, result["status"]), (1, "preferences_version"))
        self.assertFalse(failed.exists())
        self.assertEqual(ini.read_bytes(), original)

    def test_legacy_checkpoints_import_validates_and_preserves_backup_bytes(self) -> None:
        from scidev.workspace.checkpoint import WorkspaceCheckpoint

        original = self.workspace / "original.txt"
        original.write_bytes(b"initial\r\n")
        checkpoint = WorkspaceCheckpoint(self.workspace, "session-ckpt1")
        checkpoint.capture(original)
        checkpoint.capture(self.workspace / "new.txt")
        original.write_bytes(b"modified\r\n")
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--dry-run")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["checkpoints"], result["checkpoint_backups"]), (1, 1))
        snapshot = self.root / "checkpoint-snapshot"
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--snapshot", str(snapshot))
        self.assertEqual(code, 0, result)
        copied = snapshot / ".research" / "checkpoints" / "session-ckpt1"
        self.assertEqual((copied / "manifest.json").read_bytes(), checkpoint.manifest_path.read_bytes())
        backup, = checkpoint.files_dir.glob("*.bin")
        self.assertEqual((copied / "files" / backup.name).read_bytes(), b"initial\r\n")
        self.assertEqual(original.read_bytes(), b"modified\r\n")

        backup.write_bytes(b"corrupt\r\n")
        bad = self.root / "invalid-snapshot"
        code, result = self.diagnostic("migrate", "--workspace", str(self.workspace), "--snapshot", str(bad))
        self.assertEqual((code, result["status"]), (1, "invalid_checkpoint"))
        self.assertFalse(bad.exists())

    def test_enabled_command_with_missing_image_fails_without_host_fallback(self) -> None:
        process, result, requests, _ = self.run_task(
            [tool("run_command", argv=["cmd.exe", "/c", "echo fixture > host.txt"])],
            sandbox_image="scidev-fixture-missing:never-pull",
        )
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result["status"], "sandbox_unavailable")
        self.assertEqual(requests[0]["tools"][-1]["function"]["name"], "run_command")
        self.assertFalse((self.workspace / "host.txt").exists())

    def test_project_command_uses_disposable_read_only_offline_workspace(self) -> None:
        if not DOCKER_IMAGE:
            self.skipTest("Requires Docker Desktop and a local Node image (--docker-image)")
        literal = docker_project(self.workspace)
        process, result, requests, events = self.run_task(
            [tool("run_command", argv=["node", "verify.cjs", literal]), stop()], sandbox_image=DOCKER_IMAGE,
        )
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result["files_changed"], [])
        self.assertIn(f"fixture passed: {literal}", requests[1]["messages"][-1]["content"])
        completed = next(event for event in events if event["type"] == "tool_completed")
        self.assertEqual(completed["data"]["command_exit_code"], 0)
        self.assertFalse((self.workspace / "cache.txt").exists())
        self.assertFalse((self.workspace / "host.txt").exists())
        self.assertEqual(list((self.workspace / ".research").glob("scidev-command-*")), [])

    def test_node_zero_tests_do_not_satisfy_code_verification(self) -> None:
        if not DOCKER_IMAGE:
            self.skipTest("Requires Docker Desktop and a local Node image (--docker-image)")
        (self.workspace / "package.json").write_text('{"name":"scidev-fixture"}', encoding="utf-8")

        def run(filename):
            return self.run_task([
                tool("write_file", path=filename, content="module.exports = 42;\n"),
                tool("run_command", "check", argv=["node", "--test"]),
                stop("Checked the project."),
            ], task="Create a project module and test it", max_turns=3, sandbox_image=DOCKER_IMAGE)

        process, result, _, events = run("module.cjs")
        self.assertEqual((process.returncode, result["status"]), (1, "verification_required"), result)
        self.assertEqual(result["verification"]["status"], "no_tests")
        check = next(event["data"] for event in events if event["type"] == "tool_completed" and event["data"]["id"] == "check")
        self.assertEqual((check["command_exit_code"], check["verification"]["test_issue"]), (0, "no_tests"))
        (self.workspace / "module.test.cjs").write_text(
            'require("node:test")("module", () => require("node:assert/strict").equal(require("./module.cjs"), 42));\n',
            encoding="utf-8",
        )
        process, result, _, _ = run("second.cjs")
        self.assertEqual((process.returncode, result["status"], result["verification"]["status"]), (0, "completed", "passed"), result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll", type=Path, default=DLL)
    parser.add_argument("--oracle", action="store_true", help="Also run the existing Python/Qt SVG validator")
    parser.add_argument("--docker-image", help="Also run the isolated Node fixture using an already-local image")
    parser.add_argument("--preview-exe", type=Path, help="Test the default flow in a packaged Windows CLI without Docker")
    flags = parser.parse_args()
    DLL = flags.dll.resolve()
    ORACLE = flags.oracle
    DOCKER_IMAGE = flags.docker_image
    PREVIEW_EXE = flags.preview_exe.resolve() if flags.preview_exe else None
    if PREVIEW_EXE and (flags.oracle or flags.docker_image or not PREVIEW_EXE.is_file()):
        parser.error("--preview-exe needs an existing CLI EXE and runs without the Qt oracle or Docker.")
    if not PREVIEW_EXE and not DLL.is_file():
        parser.error("Build dotnet/SciDevHarness.slnx first.")
    sys.path.insert(0, str(ROOT))
    if ORACLE:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    release_tests = [
        "ReplayTests.test_default_code_task_without_container_reports_checks_not_run",
        "ReplayTests.test_creation_does_not_overwrite",
        "ReplayTests.test_host_command_is_denied_when_it_was_not_enabled",
        "ReplayTests.test_external_file_tools_are_denied_headless",
    ]
    unittest.main(argv=[sys.argv[0], *(release_tests if PREVIEW_EXE else [])], verbosity=2)
