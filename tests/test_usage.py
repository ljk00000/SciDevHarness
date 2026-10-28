from __future__ import annotations

import unittest

from scidev_usage import ModelRequestUsage


class ModelRequestUsageTests(unittest.TestCase):
    def test_aggregates_payload_bytes_retries_and_reported_tokens_without_content(self) -> None:
        session = {}
        first = ModelRequestUsage.record(
            session,
            logical_request_key="task-1:coding:turn-2",
            kind="coding",
            model="local-coder",
            metrics={
                "request_body_bytes": 1200,
                "response_body_bytes": 410,
                "response_body_complete": True,
                "elapsed_ms": 900,
                "success": True,
                "status_code": 200,
                "prompt_tokens": 300,
                "completion_tokens": 70,
                "total_tokens": 370,
                "prompt": "must never be stored",
                "api_key": "must never be stored",
            },
        )
        second = ModelRequestUsage.record(
            session,
            logical_request_key="task-1:coding:turn-2",
            kind="coding",
            model="local-coder",
            metrics={
                "request_body_bytes": 1200,
                "response_body_bytes": 24,
                "response_body_complete": True,
                "elapsed_ms": 2000,
                "success": False,
                "status_code": 429,
                "transport_route": "proxy_fallback",
                "error": "credential-like value must never be stored",
            },
        )

        self.assertEqual(first["record"]["attempt"], 1)
        self.assertEqual(second["record"]["attempt"], 2)
        self.assertEqual(second["usage"]["request_attempts"], 2)
        self.assertEqual(second["usage"]["retry_attempts"], 1)
        self.assertEqual(second["usage"]["failed_attempts"], 1)
        self.assertEqual(second["usage"]["proxy_fallback_attempts"], 1)
        self.assertEqual(second["usage"]["request_body_bytes"], 2400)
        self.assertEqual(second["usage"]["response_body_bytes"], 434)
        self.assertEqual(second["usage"]["total_tokens"], 370)
        self.assertNotIn("prompt", second["record"])
        self.assertNotIn("api_key", second["record"])
        self.assertNotIn("error", second["record"])
        self.assertEqual(second["record"]["transport_route"], "proxy_fallback")
        self.assertNotIn("must never be stored", repr(session))

    def test_recent_request_records_and_retry_keys_are_bounded(self) -> None:
        session = {}
        for index in range(ModelRequestUsage.MAX_RECENT_RECORDS + 12):
            ModelRequestUsage.record(
                session,
                logical_request_key=f"task:{index}",
                kind="summary_checkpoint",
                model="provider-model",
                metrics={"request_body_bytes": 1, "response_body_bytes": 2, "success": True},
            )

        self.assertEqual(len(session["model_usage_records"]), ModelRequestUsage.MAX_RECENT_RECORDS)
        self.assertLessEqual(len(session["model_usage_request_keys"]), ModelRequestUsage.MAX_REQUEST_KEYS)
        self.assertEqual(ModelRequestUsage.snapshot(session)["request_attempts"], ModelRequestUsage.MAX_RECENT_RECORDS + 12)

    def test_invalid_and_missing_counts_are_clamped(self) -> None:
        session = {"model_usage": {"request_body_bytes": "bad", "request_attempts": -7}}
        self.assertEqual(ModelRequestUsage.snapshot(session)["request_body_bytes"], 0)
        self.assertEqual(ModelRequestUsage.snapshot(session)["request_attempts"], 0)
        self.assertEqual(ModelRequestUsage.format_bytes(0), "0 B")
        self.assertEqual(ModelRequestUsage.format_bytes(1536), "1.5 KB")


if __name__ == "__main__":
    unittest.main()
