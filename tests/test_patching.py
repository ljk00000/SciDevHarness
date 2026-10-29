from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scidev_patching import PatchEngine, PatchError


class PatchEngineTests(unittest.TestCase):
    def engine(self, root: Path) -> PatchEngine:
        root = root.resolve()

        def resolve(relative: str) -> Path:
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                raise ValueError("outside project")
            if any(part.casefold() in {".git", ".research", ".venv", "secrets"} for part in target.relative_to(root).parts):
                raise ValueError("protected path")
            return target

        return PatchEngine(root, resolve, max_file_bytes=100_000)

    @staticmethod
    def two_hunk_patch(path: str = "module.py") -> str:
        return f"""*** Begin Patch
*** Update File: {path}
@@
 def first():
-    return 1
+    return 10
@@
 def second():
-    return 2
+    return 20
*** End Patch"""

    def test_preview_is_read_only_and_selected_hunks_apply_with_original_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "module.py"
            original = "def first():\r\n    return 1\r\n\r\ndef second():\r\n    return 2\r\n"
            target.write_bytes(original.encode("utf-8"))
            engine = self.engine(root)

            plan = engine.prepare(self.two_hunk_patch())

            self.assertEqual(target.read_bytes(), original.encode("utf-8"))
            self.assertEqual([hunk["id"] for hunk in plan.hunks], [1, 2])
            self.assertIn("-    return 1", plan.hunks[0]["diff"])
            result = engine.apply(plan, {1})

            updated = target.read_bytes()
            self.assertIn(b"return 10\r\n", updated)
            self.assertIn(b"return 2\r\n", updated)
            self.assertNotIn(b"\n", updated.replace(b"\r\n", b""))
            self.assertEqual(result["applied_hunks"], [1])
            self.assertEqual(result["declined_hunks"], [2])

    def test_stale_file_is_never_overwritten_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "module.py"
            target.write_text("def first():\n    return 1\n\n", encoding="utf-8")
            engine = self.engine(root)
            patch_text = """*** Begin Patch
*** Update File: module.py
@@
 def first():
-    return 1
+    return 10
*** End Patch"""
            plan = engine.prepare(patch_text)
            target.write_text("user edit\n", encoding="utf-8")

            with self.assertRaisesRegex(PatchError, "预览后文件已变化"):
                engine.apply(plan, {1})

            self.assertEqual(target.read_text(encoding="utf-8"), "user edit\n")

    def test_empty_file_can_be_patched_and_end_marker_is_not_written_as_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "empty.py"
            target.write_bytes(b"")
            patch_text = """*** Begin Patch
*** Update File: empty.py
@@
+value = 1
*** End of File
*** End Patch"""

            plan = self.engine(root).prepare(patch_text)
            self.assertNotIn("End of File", plan.hunks[0]["diff"])
            self.engine(root).apply(plan, {1})

            self.assertEqual(target.read_bytes(), b"value = 1\n")

    def test_all_files_are_prevalidated_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "good.py"
            target.write_text("value = 1\n", encoding="utf-8")
            patch_text = """*** Begin Patch
*** Update File: good.py
@@
-value = 1
+value = 2
*** Update File: missing.py
@@
-old = 1
+old = 2
*** End Patch"""

            with self.assertRaisesRegex(PatchError, "现有普通文件"):
                self.engine(root).prepare(patch_text)

            self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")

    def test_add_and_delete_are_individually_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "old.py"
            old.write_text("obsolete\n", encoding="utf-8")
            patch_text = """*** Begin Patch
*** Add File: src/new.py
+print('new')
*** Delete File: old.py
*** End Patch"""
            engine = self.engine(root)
            plan = engine.prepare(patch_text)
            result = engine.apply(plan, {1})

            self.assertEqual((root / "src" / "new.py").read_text(encoding="utf-8"), "print('new')\n")
            self.assertTrue(old.exists())
            self.assertEqual(result["applied_paths"], ["src/new.py"])
            self.assertEqual(result["declined_hunks"], [2])

    def test_ambiguous_context_and_paths_outside_workspace_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "module.py").write_text("value = 1\nvalue = 1\n", encoding="utf-8")
            ambiguous = """*** Begin Patch
*** Update File: module.py
@@
-value = 1
+value = 2
*** End Patch"""
            with self.assertRaisesRegex(PatchError, "匹配 2 处"):
                self.engine(root).prepare(ambiguous)
            with self.assertRaisesRegex(PatchError, "非法片段"):
                self.engine(root).prepare(self.two_hunk_patch("../outside.py"))

    def test_rejected_all_hunks_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "module.py"
            target.write_text("def first():\n    return 1\n", encoding="utf-8")
            engine = self.engine(root)
            plan = engine.prepare("""*** Begin Patch
*** Update File: module.py
@@
 def first():
-    return 1
+    return 2
*** End Patch""")

            result = engine.apply(plan, set())

            self.assertEqual(target.read_text(encoding="utf-8"), "def first():\n    return 1\n")
            self.assertEqual(result["applied_paths"], [])
            self.assertEqual(result["declined_hunks"], [1])

    def test_multi_file_write_failure_rolls_back_already_replaced_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("value = 1\n", encoding="utf-8")
            second.write_text("value = 2\n", encoding="utf-8")
            patch_text = """*** Begin Patch
*** Update File: first.py
@@
-value = 1
+value = 10
*** Update File: second.py
@@
-value = 2
+value = 20
*** End Patch"""
            engine = self.engine(root)
            plan = engine.prepare(patch_text)
            import scidev_patching

            original_replace = scidev_patching.os.replace
            replace_calls = 0

            def fail_second_replace(source, destination):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError("simulated replace failure")
                return original_replace(source, destination)

            with patch("scidev_patching.os.replace", side_effect=fail_second_replace):
                with self.assertRaisesRegex(PatchError, "已尝试回滚"):
                    engine.apply(plan, {1, 2})

            self.assertEqual(first.read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "value = 2\n")
            self.assertFalse(list(root.glob(".scidev-patch-*")))

    def test_multi_file_failure_restores_a_deleted_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deleted = root / "obsolete.py"
            updated = root / "active.py"
            deleted.write_text("obsolete\n", encoding="utf-8")
            updated.write_text("value = 1\n", encoding="utf-8")
            patch_text = """*** Begin Patch
*** Delete File: obsolete.py
*** Update File: active.py
@@
-value = 1
+value = 2
*** End Patch"""
            engine = self.engine(root)
            plan = engine.prepare(patch_text)
            import scidev_patching

            original_replace = scidev_patching.os.replace
            fail_once = True

            def fail_update(source, destination):
                nonlocal fail_once
                if Path(destination).resolve() == updated.resolve() and fail_once:
                    fail_once = False
                    raise OSError("simulated replacement failure")
                return original_replace(source, destination)

            with patch("scidev_patching.os.replace", side_effect=fail_update):
                with self.assertRaisesRegex(PatchError, "已尝试回滚"):
                    engine.apply(plan, {1, 2})

            self.assertEqual(deleted.read_text(encoding="utf-8"), "obsolete\n")
            self.assertEqual(updated.read_text(encoding="utf-8"), "value = 1\n")

    def test_patch_paths_refuse_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            outside = Path(temp) / "outside.py"
            outside.write_text("value = 1\n", encoding="utf-8")
            try:
                (root / "linked.py").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            with self.assertRaisesRegex(PatchError, "不跟随符号链接"):
                self.engine(root).prepare(self.two_hunk_patch("linked.py"))


if __name__ == "__main__":
    unittest.main()
