from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sysconfig
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from scripts.audit_standalone import _license_metadata, build_manifest, main


class StandaloneAuditTests(unittest.TestCase):
    def test_installed_distribution_license_files_match_metadata(self) -> None:
        site_packages = Path(sysconfig.get_paths()["purelib"])
        distributions = _license_metadata(site_packages)
        essentials = [item for item in distributions if item["name"] == "PySide6_Essentials"]

        self.assertEqual(len(essentials), 1)
        self.assertRegex(essentials[0]["version"], r"^6\.")
        self.assertFalse(essentials[0]["missing_license_files"])
        self.assertTrue(essentials[0]["license_files"])

        with tempfile.TemporaryDirectory(prefix="scidev-package-audit-") as temporary:
            deployment, _fake_site_packages = self._fixture(Path(temporary))
            manifest = build_manifest(
                deployment,
                site_packages,
                python_version="3.14",
                pyside_version=essentials[0]["version"],
                nuitka_version="4.1.1",
            )
            self.assertTrue(manifest["runtime_binaries"])
            self.assertEqual(manifest["license_review"]["status"], "manual-review-required")

    def _fixture(self, root: Path) -> tuple[Path, Path]:
        deployment = root / "deployment"
        (deployment / "PySide6").mkdir(parents=True)
        (deployment / "qml").mkdir()
        (deployment / "scidev_client.exe").write_bytes(b"test executable")
        (deployment / "Qt6Core.dll").write_bytes(b"Qt runtime")
        (deployment / "PySide6" / "QtWidgets.pyd").write_bytes(b"binding")
        (deployment / "qml" / "DevelopmentTree.qml").write_text("import QtQuick\n", encoding="utf-8")

        site_packages = root / "site-packages"
        dist_info = site_packages / "PySide6_Essentials-6.11.2.dist-info"
        license_dir = dist_info / "licenses"
        license_dir.mkdir(parents=True)
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.4\n"
            "Name: PySide6_Essentials\n"
            "Version: 6.11.2\n"
            "License: LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only\n"
            "License-File: LicenseRef-Qt-Commercial.txt\n\n",
            encoding="utf-8",
        )
        (license_dir / "LicenseRef-Qt-Commercial.txt").write_text("license notice\n", encoding="utf-8")
        return deployment, site_packages

    def test_manifest_records_runtime_hashes_license_metadata_and_manual_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-package-audit-") as temporary:
            root = Path(temporary)
            deployment, site_packages = self._fixture(root)
            manifest = build_manifest(
                deployment,
                site_packages,
                python_version="3.14",
                pyside_version="6.11.2",
                nuitka_version="4.1.1",
            )

            binaries = {item["path"]: item for item in manifest["runtime_binaries"]}
            self.assertEqual(
                binaries["Qt6Core.dll"]["sha256"],
                hashlib.sha256(b"Qt runtime").hexdigest(),
            )
            self.assertIn("PySide6/QtWidgets.pyd", binaries)
            self.assertEqual(manifest["qml_files"], ["qml/DevelopmentTree.qml"])
            package = manifest["build_environment_license_metadata"][0]
            self.assertEqual(package["name"], "PySide6_Essentials")
            self.assertEqual(package["version"], "6.11.2")
            self.assertEqual(package["license_declarations"][0]["field"], "License")
            self.assertTrue(package["license_files"][0]["path"].endswith("LicenseRef-Qt-Commercial.txt"))
            self.assertEqual(manifest["license_review"]["status"], "manual-review-required")

    def test_cli_writes_a_manifest_and_rejects_missing_declared_license_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-package-audit-") as temporary:
            root = Path(temporary)
            deployment, site_packages = self._fixture(root)
            output = root / "manifest.json"
            result = main(
                [
                    "--deployment", str(deployment),
                    "--site-packages", str(site_packages),
                    "--output", str(output),
                    "--python-version", "3.14",
                    "--pyside-version", "6.11.2",
                    "--nuitka-version", "4.1.1",
                ]
            )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["mode"], "standalone")

            license_file = (
                site_packages
                / "PySide6_Essentials-6.11.2.dist-info"
                / "licenses"
                / "LicenseRef-Qt-Commercial.txt"
            )
            license_file.unlink()
            with redirect_stderr(io.StringIO()):
                result = main(
                    [
                        "--deployment", str(deployment),
                        "--site-packages", str(site_packages),
                        "--output", str(output),
                        "--python-version", "3.14",
                        "--pyside-version", "6.11.2",
                        "--nuitka-version", "4.1.1",
                    ]
                )
            self.assertEqual(result, 1)

    def test_manifest_rejects_incomplete_standalone_outputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-package-audit-") as temporary:
            root = Path(temporary)
            deployment, site_packages = self._fixture(root)
            (deployment / "Qt6Core.dll").unlink()
            with self.assertRaisesRegex(ValueError, "Qt6Core"):
                build_manifest(
                    deployment,
                    site_packages,
                    python_version="3.14",
                    pyside_version="6.11.2",
                    nuitka_version="4.1.1",
                )

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior is platform-specific")
    def test_manifest_refuses_directory_junctions_outside_the_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-package-audit-") as temporary:
            root = Path(temporary)
            deployment, site_packages = self._fixture(root)
            outside = root / "outside"
            outside.mkdir()
            (outside / "private.dll").write_bytes(b"must not be inventoried")
            junction = deployment / "linked_runtime"
            result = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            with self.assertRaisesRegex(ValueError, "linked path"):
                build_manifest(
                    deployment,
                    site_packages,
                    python_version="3.14",
                    pyside_version="6.11.2",
                    nuitka_version="4.1.1",
                )


if __name__ == "__main__":
    unittest.main()
