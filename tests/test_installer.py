from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_installer.ps1"
ISS = ROOT / "packaging" / "windows" / "scidev.iss"


class InstallerBuildTests(unittest.TestCase):
    def test_installer_is_per_user_and_includes_the_application_tree(self) -> None:
        text = ISS.read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", text)
        self.assertIn("AppVersion={#AppVersion}", text)
        self.assertIn("Source: \"{#SourceDir}\\*\"", text)
        self.assertIn("recursesubdirs createallsubdirs", text)
        self.assertIn("scidev_client.exe", text)
        self.assertIn("OutputBaseFilename=SciDevHarness-Setup-{#AppVersion}", text)

    def test_build_script_validates_inputs_and_never_downloads_or_installs(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("scidev_client.exe", text)
        self.assertIn("^[0-9]+\\.[0-9]+\\.[0-9]+$", text)
        self.assertIn("ISCC.exe", text)
        self.assertIn("sign_release.ps1", text)
        self.assertIn("Get-FileHash", text)
        self.assertNotIn("Invoke-WebRequest", text)
        self.assertNotIn("Start-Process", text)
        self.assertNotIn("Start-BitsTransfer", text)
        self.assertNotIn("curl ", text)
        self.assertNotIn("msiexec", text.lower())


if __name__ == "__main__":
    unittest.main()
