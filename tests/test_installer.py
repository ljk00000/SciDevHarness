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
        self.assertIn('[string]$TimestampUrl = "http://timestamp.digicert.com"', text)
        self.assertIn("TimestampUrl must use HTTP or HTTPS", text)
        self.assertIn("Get-FileHash", text)
        self.assertNotIn("Invoke-WebRequest", text)
        self.assertNotIn("Start-Process", text)
        self.assertNotIn("Start-BitsTransfer", text)
        self.assertNotIn("curl ", text)
        self.assertNotIn("msiexec", text.lower())

    def test_package_workflow_builds_and_uploads_the_installer(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "windows-package.yml").read_text(encoding="utf-8")
        self.assertIn("choco install innosetup", workflow)
        self.assertIn("build_installer.ps1", workflow)
        self.assertIn("SciDevHarness-Windows-x64-installer", workflow)
        self.assertIn("Get-FileHash", workflow)
        self.assertIn("WINDOWS_SIGNING_PFX_BASE64", workflow)
        self.assertIn("SCIDEV_SIGN_THUMBPRINT", workflow)
        self.assertIn("Remove signing certificate", workflow)

    def test_release_workflow_is_tag_triggered_and_publishes_the_installer(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        package = (ROOT / ".github" / "workflows" / "windows-package.yml").read_text(encoding="utf-8")
        self.assertIn("tags:", workflow)
        self.assertIn("gh run download", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("secrets: inherit", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("workflow_call:", package)
        self.assertIn("Validate release tag matches APP_VERSION", package)


if __name__ == "__main__":
    unittest.main()
