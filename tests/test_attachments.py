from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from scidev_attachments import (
    AttachmentError,
    MAX_IMAGES,
    image_content_parts,
    load_image_file,
    normalize_images,
)
from scidev_core import CodingAgent, EventLedger, SummarySettings

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII="
)


class AttachmentTests(unittest.TestCase):
    def test_load_and_normalize_png(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-image-") as temp:
            path = Path(temp) / "pixel.png"
            path.write_bytes(PNG_BYTES)
            loaded = load_image_file(path)
        self.assertEqual(loaded["mime"], "image/png")
        self.assertEqual(loaded["name"], "pixel.png")
        normalized = normalize_images([loaded])
        self.assertEqual(len(normalized), 1)
        self.assertEqual(
            image_content_parts(normalized)[0]["image_url"]["url"][:22],
            "data:image/png;base64,",
        )

    def test_rejects_wrong_mime_and_too_many_images(self) -> None:
        with self.assertRaises(AttachmentError):
            normalize_images([{
                "name": "fake.png",
                "data_url": "data:image/png;base64," + base64.b64encode(b"not an image").decode(),
            }])
        with self.assertRaises(AttachmentError):
            normalize_images([{}] * (MAX_IMAGES + 1))

    def test_rejects_path_like_names_without_escaping(self) -> None:
        image = {
            "name": "../../secret.png",
            "data_url": "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode(),
        }
        normalized = normalize_images([image])
        self.assertEqual(normalized[0]["name"], "secret.png")

    def test_non_image_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-image-") as temp:
            path = Path(temp) / "notes.txt"
            path.write_text("hello", encoding="utf-8")
            with self.assertRaises(AttachmentError):
                load_image_file(path)

    def test_new_session_message_contains_image_parts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-image-session-") as temp:
            root = Path(temp)
            image = {
                "name": "pixel.png",
                "mime": "image/png",
                "size_bytes": len(PNG_BYTES),
                "data_url": "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode(),
            }
            agent = CodingAgent.__new__(CodingAgent)
            agent.project_root = root
            agent.ledger = EventLedger(root)
            agent.git = Mock(head_sha=Mock(return_value=""), status_paths=Mock(return_value=[]))
            agent.summary_settings = SummarySettings(enabled=False)
            session = agent._load_session("image-session", "看图", [image])
            content = session["messages"][1]["content"]
            self.assertIsInstance(content, list)
            self.assertEqual(content[0]["type"], "text")
            self.assertEqual(content[1]["type"], "image_url")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertEqual(session["attachments"][0]["name"], "pixel.png")


if __name__ == "__main__":
    unittest.main()
