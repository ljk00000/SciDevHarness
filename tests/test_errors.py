from __future__ import annotations

import unittest

from scidev_errors import classify_exception, classify_message


class ErrorClassificationTests(unittest.TestCase):
    def test_known_failure_families(self) -> None:
        cases = {
            "HTTP 401 invalid api key": ("auth", False),
            "429 too many requests": ("rate_limit", True),
            "request timed out": ("timeout", True),
            "connection refused by proxy": ("network", True),
            "tool arguments do not match schema": ("tool", True),
            "pytest failed with exit code 1": ("verification", True),
            "git worktree path missing": ("workspace", False),
            "unexpected shutdown": ("internal", True),
        }
        for message, (kind, retryable) in cases.items():
            with self.subTest(message=message):
                info = classify_message(message)
                self.assertEqual(info.kind, kind)
                self.assertEqual(info.retryable, retryable)

    def test_exception_classifier_handles_timeout_and_auth(self) -> None:
        self.assertEqual(classify_exception(TimeoutError("slow")).kind, "timeout")
        self.assertEqual(classify_exception(PermissionError("403 forbidden")).kind, "auth")
        self.assertEqual(classify_exception(RuntimeError("tool failed")).kind, "tool")
        self.assertEqual(classify_exception(RuntimeError("mystery")).kind, "internal")

    def test_detail_is_bounded_and_serializable(self) -> None:
        info = classify_message("x" * 100_000)
        self.assertLessEqual(len(info.detail), 4000)
        self.assertEqual(info.as_dict()["error_kind"], "internal")


if __name__ == "__main__":
    unittest.main()
