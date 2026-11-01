from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from scidev_update import (
    APP_VERSION,
    UpdateError,
    check_for_update,
    compare_versions,
    detect_repo,
    parse_version,
    verify_downloaded_asset,
)


class UpdateTests(unittest.TestCase):
    def test_version_parsing_and_ordering(self) -> None:
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertIsNone(parse_version("latest"))
        self.assertGreater(compare_versions("1.10.0", "1.9.9"), 0)
        self.assertEqual(compare_versions("1.2.3", "v1.2.3"), 0)
        self.assertEqual(compare_versions("bad", "1.0.0"), 0)

    def test_check_for_update_reports_a_newer_release_and_asset(self) -> None:
        calls: list[tuple[str, float]] = []

        def fetch(url: str, timeout: float):
            calls.append((url, timeout))
            return {
                "tag_name": "v0.2.0",
                "html_url": "https://github.com/example/repo/releases/tag/v0.2.0",
                "published_at": "2026-01-01T00:00:00Z",
                "body": "notes",
                "assets": [
                    {"name": "source.zip", "browser_download_url": "https://example/source.zip"},
                    {"name": "SciDevHarness-Setup.exe", "browser_download_url": "https://example/setup.exe",
                     "size": 12345, "digest": "sha256:" + "a" * 64},
                ],
            }

        info = check_for_update(current="0.1.0", repo="example/repo", fetch=fetch)
        self.assertTrue(info.available)
        self.assertEqual(info.latest, "0.2.0")
        self.assertEqual(info.asset_url, "https://example/setup.exe")
        self.assertEqual(info.asset_name, "SciDevHarness-Setup.exe")
        self.assertEqual(info.asset_size, 12345)
        self.assertEqual(info.asset_digest, "sha256:" + "a" * 64)
        self.assertEqual(calls[0][0], "https://api.github.com/repos/example/repo/releases/latest")
        self.assertEqual(info.as_dict()["available"], True)

    def test_current_release_is_not_an_update(self) -> None:
        info = check_for_update(
            current=APP_VERSION,
            repo="example/repo",
            fetch=lambda _url, _timeout: {"tag_name": f"v{APP_VERSION}", "html_url": "u"},
        )
        self.assertFalse(info.available)

    def test_insecure_asset_url_is_not_reported(self) -> None:
        info = check_for_update(
            current="0.1.0",
            repo="example/repo",
            fetch=lambda _url, _timeout: {
                "tag_name": "v0.2.0",
                "html_url": "u",
                "assets": [{"name": "setup.exe", "browser_download_url": "http://example/setup.exe"}],
            },
        )
        self.assertEqual(info.asset_url, "")

    def test_downloaded_asset_digest_is_verified(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory(prefix="scidev-update-", ignore_cleanup_errors=True) as temp:
            path = Path(temp) / "setup.exe"
            path.write_bytes(b"installer")
            digest = hashlib.sha256(b"installer").hexdigest()
            self.assertEqual(verify_downloaded_asset(path, digest), digest)
            with self.assertRaises(UpdateError):
                verify_downloaded_asset(path, "0" * 64)
            with self.assertRaises(UpdateError):
                verify_downloaded_asset(path, "not-a-digest")
            with self.assertRaises(UpdateError):
                verify_downloaded_asset(path, digest, max_bytes=3)

    def test_invalid_repository_and_bad_tag_are_rejected(self) -> None:
        with self.assertRaises(UpdateError):
            check_for_update(repo="../bad", fetch=lambda _u, _t: {})
        with self.assertRaises(UpdateError):
            check_for_update(
                repo="example/repo",
                fetch=lambda _u, _t: {"tag_name": "nightly"},
            )

    def test_repo_detection_uses_origin_and_environment(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory(prefix="scidev-update-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "git@github.com:owner/project.git"],
                cwd=root, check=True,
            )
            self.assertEqual(detect_repo(root), "owner/project")
        self.assertTrue(detect_repo(None))


if __name__ == "__main__":
    unittest.main()
