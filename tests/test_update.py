from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path
import unittest

from scidev_update import (
    APP_VERSION,
    DownloadedAsset,
    UpdateError,
    UpdateInfo,
    check_for_update,
    compare_versions,
    detect_repo,
    download_asset,
    launch_installer,
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

    def test_download_asset_streams_and_verifies_without_executing(self) -> None:
        payload = b"installer-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        info = UpdateInfo(
            current="0.1.0",
            latest="0.2.0",
            url="https://example/release",
            published_at="",
            notes="",
            asset_url="https://example/setup.exe",
            asset_name="setup.exe",
            asset_size=len(payload),
            asset_digest="sha256:" + digest,
        )

        class Response:
            def __init__(self, data: bytes):
                self._stream = io.BytesIO(data)
                self.headers = {"Content-Length": str(len(data))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self._stream.close()

            def read(self, size: int = -1) -> bytes:
                return self._stream.read(size)

            def geturl(self) -> str:
                return "https://example/setup.exe"

        with tempfile.TemporaryDirectory(prefix="scidev-download-", ignore_cleanup_errors=True) as temp:
            target = Path(temp) / "setup.exe"
            result = download_asset(info, target, open_url=lambda _url, _timeout: Response(payload))
            self.assertEqual(result.path, target)
            self.assertEqual(result.sha256, digest)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(result.as_dict()["size"], len(payload))

            with self.assertRaisesRegex(UpdateError, "SHA-256"):
                download_asset(
                    info,
                    Path(temp) / "bad.exe",
                    open_url=lambda _url, _timeout: Response(b"x" * len(payload)),
                )
            self.assertFalse((Path(temp) / "bad.exe").exists())
            with self.assertRaisesRegex(UpdateError, "HTTPS"):
                download_asset(
                    UpdateInfo("0.1.0", "0.2.0", "u", "", "", asset_url="http://example/setup.exe"),
                    Path(temp) / "http.exe",
                    open_url=lambda _url, _timeout: Response(payload),
                )
            with self.assertRaisesRegex(UpdateError, "大小"):
                download_asset(info, Path(temp) / "big.exe", max_bytes=3,
                               open_url=lambda _url, _timeout: Response(payload))

    def test_download_can_be_cancelled_before_streaming(self) -> None:
        payload = b"installer-bytes"
        info = UpdateInfo(
            current="0.1.0",
            latest="0.2.0",
            url="https://example/release",
            published_at="",
            notes="",
            asset_url="https://example/setup.exe",
            asset_name="setup.exe",
            asset_size=len(payload),
            asset_digest=hashlib.sha256(payload).hexdigest(),
        )

        class Response:
            headers = {"Content-Length": str(len(payload))}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size: int = -1) -> bytes:
                return payload

            def geturl(self) -> str:
                return "https://example/setup.exe"

        calls = 0

        def cancel() -> bool:
            nonlocal calls
            calls += 1
            return calls > 1

        with tempfile.TemporaryDirectory(prefix="scidev-download-", ignore_cleanup_errors=True) as temp:
            target = Path(temp) / "setup.exe"
            with self.assertRaisesRegex(UpdateError, "取消"):
                download_asset(
                    info,
                    target,
                    open_url=lambda _url, _timeout: Response(),
                    cancel=cancel,
                )
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(temp).glob("*.part")), [])

    def test_cli_downloads_and_reports_a_verified_asset(self) -> None:
        import contextlib
        import json
        from unittest.mock import patch

        import scidev_cli

        info = UpdateInfo(
            current="0.1.0", latest="0.2.0", url="https://example/release", published_at="", notes="",
            asset_url="https://example/setup.exe", asset_name="setup.exe",
            asset_size=7, asset_digest="sha256:" + "a" * 64,
        )
        downloaded = DownloadedAsset(Path("setup.exe"), "a" * 64, 7)
        output = io.StringIO()
        with (
            patch.object(scidev_cli, "check_for_update", return_value=info),
            patch.object(scidev_cli, "download_asset", return_value=downloaded),
            patch.object(scidev_cli, "launch_installer", return_value=4321) as launch,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            code = scidev_cli.main([
                "update", "--workspace", ".", "--json", "--download", "setup.exe", "--install",
            ])
        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["downloaded"]["sha256"], "a" * 64)
        self.assertEqual(payload["downloaded"]["path"], "setup.exe")
        self.assertEqual(payload["installer_pid"], 4321)
        launch.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "installer launch is Windows-only")
    def test_launch_installer_verifies_before_spawning(self) -> None:
        payload = b"installer"
        digest = hashlib.sha256(payload).hexdigest()
        calls: list[list[str]] = []

        class Process:
            pid = 99

        with tempfile.TemporaryDirectory(prefix="scidev-launch-", ignore_cleanup_errors=True) as temp:
            path = Path(temp) / "setup.exe"
            path.write_bytes(payload)
            pid = launch_installer(path, expected_sha256=digest, open_process=lambda argv: calls.append(argv) or Process())
            self.assertEqual(pid, 99)
            self.assertEqual(calls, [[str(path)]])
            with self.assertRaises(UpdateError):
                launch_installer(path, expected_sha256="0" * 64, open_process=lambda argv: calls.append(argv) or Process())
            self.assertEqual(len(calls), 1)

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
