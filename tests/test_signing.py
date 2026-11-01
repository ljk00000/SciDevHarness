from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sign_release.ps1"


class SigningScriptTests(unittest.TestCase):
    def test_requires_https_timestamp_and_explicit_thumbprint(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Thumbprint", text)
        self.assertIn("^[0-9A-F]{40}$", text)
        self.assertIn("TimestampUrl must use HTTPS", text)
        self.assertIn('/fd SHA256 /tr $TimestampUrl /td SHA256 /sha1', text)
        self.assertIn("/pa /v", text)
        self.assertNotIn("ConvertTo-SecureString", text)
        self.assertNotIn("pfxpassword", text.lower())
        self.assertNotIn("-pfx", text.lower())

    def test_restricts_targets_and_is_not_used_by_the_build(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('@(".exe", ".dll", ".msi")', text)
        self.assertIn("VerifyOnly", text)
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("sign_release.ps1", workflow)


if __name__ == "__main__":
    unittest.main()
