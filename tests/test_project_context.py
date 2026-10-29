from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scidev_context import ProjectContext
from scidev_core import (
    CodingAgent, CodingToolbox, EventLedger, GitManager, OpenAICompatibleProvider,
    PermanentError, RetryableError, SummarySettings,
)
from scidev_patching import PatchError


class ProjectContextTests(unittest.TestCase):
    def context(self, root: Path, **kwargs) -> ProjectContext:
        return ProjectContext(root, CodingToolbox.is_protected_path, **kwargs)

    def test_task_context_finds_deep_symbols_dependencies_and_scoped_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            module = root / "src" / "domain" / "inventory" / "manager.py"
            module.parent.mkdir(parents=True)
            module.write_text("class InventoryManager:\n    def price(self):\n        return 42\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("Keep public interfaces stable.", encoding="utf-8")
            (module.parent / "AGENTS.md").write_text("Inventory changes require unit tests.", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                '[project]\nname="demo"\nrequires-python=">=3.12"\ndependencies=["numpy>=2"]\n', encoding="utf-8",
            )
            result = self.context(root).build("Fix InventoryManager price calculation")
            self.assertIn("src/domain/inventory/manager.py", result["sources"])
            self.assertIn("InventoryManager@1", result["text"])
            self.assertIn("Keep public interfaces stable", result["text"])
            self.assertIn("Inventory changes require unit tests", result["text"])
            self.assertIn("numpy", result["text"])
            self.assertLessEqual(len(result["text"]), 18000)

    def test_context_redacts_common_inline_credentials_without_changing_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "src" / "settings.py"
            source.parent.mkdir(parents=True)
            original = (
                'API_KEY = "sk_live_context_secret_1234567890"\n'
                'password: "a private phrase"\n'
                'token = ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCDEF\n'
                'ACCESS_TOKEN = abcdefghijklmnopqrstuvwxyz123456\n'
                'endpoint = "https://user:pass@example.test/api"\n'
                'credential_source = os.getenv("API_TOKEN")\n'
                'private_key = """-----BEGIN PRIVATE KEY-----\\nprivate-material\\n'
                '-----END PRIVATE KEY-----"""\n'
            )
            source.write_text(original, encoding="utf-8")

            context = self.context(root)
            snapshot = context.build(
                "inspect API_KEY password token endpoint private_key",
                focus_paths=["src/settings.py"],
            )
            sanitized = context._sanitize_context_text(original)

            for value in (
                "sk_live_context_secret_1234567890", "a private phrase",
                "ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCDEF", "user:pass@",
                "abcdefghijklmnopqrstuvwxyz123456", "private-material",
            ):
                self.assertNotIn(value, snapshot["text"])
                self.assertNotIn(value, sanitized)
            self.assertIn("[REDACTED_SECRET]", snapshot["text"])
            self.assertIn("[REDACTED_TOKEN]", snapshot["text"])
            self.assertIn("[REDACTED_PRIVATE_KEY]", sanitized)
            self.assertIn('os.getenv("API_TOKEN")', sanitized)
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_mixed_language_retrieval_ranks_matching_code_for_chinese_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus = {
                "auth/token_manager.py": (
                    "def refresh_access_token(session):\n"
                    "    if session.token_expiry <= now():\n"
                    "        return renew_token(session)\n"
                ),
                "db/connection_pool.py": "def acquire_connection(pool):\n    return pool.checkout()\n",
                "storage/upload_client.py": "def upload_file(path):\n    return client.send(path, timeout=30)\n",
                "config/loader.py": "def load_config(path):\n    return read_settings(path)\n",
                "auth/login_handler.py": "def authenticate_user(credentials):\n    return create_session(credentials)\n",
                "payments/error_report.py": "def report_failed_charge(order):\n    return make_error_report(order)\n",
            }
            for relative, source in corpus.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")

            context = self.context(root)
            cases = (
                ("修复登录令牌过期后自动续期失败", "auth/token_manager.py"),
                ("数据库连接池泄漏", "db/connection_pool.py"),
                ("文件上传超时", "storage/upload_client.py"),
                ("配置读取失败", "config/loader.py"),
                ("refresh access token", "auth/token_manager.py"),
            )
            for query, expected in cases:
                with self.subTest(query=query):
                    sources = context.build(query)["sources"]
                    self.assertTrue(sources, f"no files retrieved for {query!r}")
                    self.assertEqual(sources[0], expected, f"wrong top retrieval for {query!r}: {sources}")

            long_query = "".join(chr(0x4E00 + index) for index in range(40)) + "登录令牌过期续期"
            weighted_terms = context._terms(long_query)
            self.assertIn("token", weighted_terms)
            self.assertIn("refresh", weighted_terms)

    def test_bilingual_retrieval_benchmark_covers_paraphrases_and_identifier_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corpus = {
                "commerce/cancel_order.py": "def cancel_order(order):\n    rollback_inventory(order)\n",
                "commerce/order_history.py": "def get_order_status(order):\n    return order.state\n",
                "media/avatar_thumbnail.py": "def generate_avatar_thumbnail(profile):\n    return resize(profile.picture)\n",
                "media/upload_client.py": "def upload_file(path):\n    return client.send(path)\n",
                "storage/temp_cleanup.py": "def cleanup_temporary_files(root):\n    purge(root)\n",
                "storage/file_cache.py": "def cache_file(path):\n    return cached_path(path)\n",
                "payments/idempotency.py": "def reject_duplicate_payment(key):\n    return idempotent_request(key)\n",
                "payments/charge.py": "def process_payment(order):\n    return charge(order)\n",
                "concurrency/race_guard.py": "def prevent_write_race(resource):\n    return acquire_mutex(resource)\n",
                "concurrency/worker_pool.py": "def run_workers(queue):\n    return parallel_map(queue)\n",
                "auth/authorization.py": "def require_permission(user):\n    return forbidden()\n",
                "auth/login.py": "def authenticate_user(credentials):\n    return create_session(credentials)\n",
                "reports/csv_export.py": "def export_csv_report(rows):\n    return write_csv(rows)\n",
                "reports/csv_reader.py": "def read_csv_report(path):\n    return parse_csv(path)\n",
                "cache/invalidation.py": "def invalidate_stale_cache(key):\n    return evict(key)\n",
                "cache/loader.py": "def load_cached_value(key):\n    return read_cache(key)\n",
                "net/retry_policy.py": "def retry_with_exponential_backoff(request):\n    return retry(request)\n",
                "net/http_client.py": "def send_request_with_timeout(request):\n    return client.send(request)\n",
                "media/image_optimizer.py": "def compress_image(image):\n    return optimize(image)\n",
                "media/gallery.py": "def list_images(album):\n    return album.images\n",
            }
            for relative, source in corpus.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")

            cases = (
                ("取消订单并回滚库存", "commerce/cancel_order.py"),
                ("头像上传后生成缩略图", "media/avatar_thumbnail.py"),
                ("清理临时文件", "storage/temp_cleanup.py"),
                ("防止重复支付请求", "payments/idempotency.py"),
                ("避免并发写入的竞态", "concurrency/race_guard.py"),
                ("未授权时返回403", "auth/authorization.py"),
                ("导出CSV报告", "reports/csv_export.py"),
                ("让过期缓存失效", "cache/invalidation.py"),
                ("请求失败后使用指数退避重试", "net/retry_policy.py"),
                ("压缩图片体积", "media/image_optimizer.py"),
                ("retry_with_exponential_backoff", "net/retry_policy.py"),
            )
            context = self.context(root)
            top1 = 0
            recall_at_5 = 0
            top1_misses = []
            for query, expected in cases:
                with self.subTest(query=query):
                    sources = context.build(query)["sources"]
                    top1 += bool(sources and sources[0] == expected)
                    recall_at_5 += expected in sources[:5]
                    if not sources or sources[0] != expected:
                        top1_misses.append((query, sources[:5]))
                    self.assertIn(expected, sources[:5], f"target missing from top five: {sources}")
            self.assertEqual(top1, len(cases), f"top-one accuracy: {top1}/{len(cases)}; misses={top1_misses}")
            self.assertEqual(recall_at_5, len(cases), f"recall@5: {recall_at_5}/{len(cases)}")
            self.assertEqual(context.build("量子纠缠态")["sources"], [])

    def test_tetris_rotation_and_collision_task_retrieves_engine_and_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = root / "game" / "tetris_engine.py"
            tests = root / "game" / "test_tetris_engine.py"
            engine.parent.mkdir(parents=True)
            engine.write_text(
                "class TetrisGame:\n"
                "    def _can_place(self, piece, x, y):\n"
                "        return True\n"
                "    def rotate(self):\n"
                "        return self.current_piece.rotated_clockwise()\n",
                encoding="utf-8",
            )
            tests.write_text(
                "def test_piece_rotation_keeps_all_four_blocks():\n"
                "    assert len(piece.rotated_clockwise().cells) == 4\n",
                encoding="utf-8",
            )

            result = self.context(root).build("修复俄罗斯方块旋转与碰撞检测")

            self.assertEqual(result["sources"][0], "game/tetris_engine.py")
            self.assertIn("game/test_tetris_engine.py", result["sources"][:5])

    def test_project_context_exposes_safe_static_test_command_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "pyproject.toml").write_text(
                '[project]\nname = "demo"\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
                encoding="utf-8",
            )
            (root / "tests" / "test_feature.py").write_text(
                "def test_feature():\n    assert True\n", encoding="utf-8",
            )
            result = self.context(root).build()

            self.assertEqual(result["test_discovery"]["candidates"][0]["command"], "python -m pytest")
            self.assertIn("python -m pytest", result["text"])
            self.assertIn("不会执行命令或安装依赖", result["text"])
            self.assertIn("逐条用户批准", result["text"])

    def test_environment_snapshot_separates_host_runtime_from_project_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text(
                '[project]\nname="demo"\nrequires-python=">=3.12,<3.15"\n'
                'dependencies=["numpy>=2", "private-wheel @ https://user:secret@example.test/pkg.whl"]\n'
                '[project.optional-dependencies]\ntest=["pytest>=8"]\n',
                encoding="utf-8",
            )
            (root / "requirements-dev.txt").write_text(
                "pytest>=8\n--index-url https://user:secret@example.test/simple\n", encoding="utf-8",
            )
            (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            env_root = root / ".venv"
            interpreter = env_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"test-interpreter-placeholder")
            (env_root / "pyvenv.cfg").write_text(
                "home = C:\\private\\runtime\\Python313\n"
                "include-system-site-packages = false\n"
                "version = 3.13.2\n"
                "executable = C:\\private\\runtime\\python.exe\n",
                encoding="utf-8",
            )

            context = self.context(root)
            snapshot = context.build("private-wheel")
            with patch("scidev_context.subprocess.run", side_effect=AssertionError("must not execute commands")):
                environment = context._environment_snapshot({
                    "pyproject.toml", "requirements-dev.txt", "uv.lock",
                })

            self.assertEqual(environment, snapshot["environment"])
            candidates = {item["path"]: item for item in environment["python_environment_candidates"]}
            self.assertEqual(environment["host"]["python"], sys.version.split()[0])
            self.assertFalse(environment["host"]["prefix_inside_project"])
            self.assertNotEqual(environment["host"]["executable"], str(Path(sys.executable).resolve()))
            self.assertEqual(environment["python_requirement"], ">=3.12,<3.15")
            self.assertEqual(environment["declared_runtimes"][0]["runtime"], "Python")
            self.assertTrue(environment["dependency_manifests"])
            self.assertIn("requirements-dev.txt", environment["dependency_manifests"])
            self.assertEqual(environment["lockfiles"], ["uv.lock"])
            self.assertFalse(environment["installed_dependencies_checked"])
            self.assertIn("解释器文件存在，版本/依赖未核验", candidates[".venv"]["state"])
            self.assertEqual(candidates[".venv"]["configured_python_version"], "3.13.2")
            self.assertFalse(candidates[".venv"]["include_system_site_packages"])
            self.assertIn("配置 Python 3.13.2", snapshot["text"])
            self.assertIn("共享系统包=否", snapshot["text"])
            self.assertNotIn("C:\\private", snapshot["text"])
            self.assertIn("private-wheel", snapshot["text"])
            self.assertIn("pytest", snapshot["text"])
            self.assertIn("numpy>=2", snapshot["text"])
            self.assertNotIn("secret", snapshot["text"])
            self.assertNotIn("example.test", snapshot["text"])
            self.assertIn("不会自动安装依赖", snapshot["text"])
            self.assertEqual(context.search("secret")["results"], [])
            self.assertIn("[REDACTED_URL]", snapshot["text"])

    def test_environment_snapshot_reports_missing_and_incomplete_venvs_without_executing_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "venv").mkdir()
            context = self.context(root)
            snapshot = context.build()
            candidates = {item["path"]: item for item in snapshot["environment"]["python_environment_candidates"]}

            self.assertEqual(candidates[".venv"]["state"], "不存在")
            self.assertEqual(candidates["venv"]["state"], "目录存在但未找到解释器")
            self.assertEqual(candidates["env"]["state"], "不存在")
            self.assertEqual(snapshot["environment"]["declared_runtimes"], [])
            self.assertIn("未发现常见运行时清单", snapshot["text"])

    def test_environment_snapshot_does_not_follow_venv_configuration_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_root = root / ".venv"
            interpreter = env_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"placeholder")
            config = env_root / "pyvenv.cfg"
            config.write_text("version = 3.12.4\n", encoding="utf-8")
            context = self.context(root)
            original_is_symlink = Path.is_symlink

            def guarded_is_symlink(path: Path) -> bool:
                try:
                    if Path(path).resolve() == config.resolve():
                        return True
                except OSError:
                    pass
                return original_is_symlink(path)

            with patch.object(Path, "is_symlink", guarded_is_symlink):
                environment = context._environment_snapshot(set())

            candidate = next(
                item for item in environment["python_environment_candidates"] if item["path"] == ".venv"
            )
            self.assertEqual(candidate["state"], "解释器文件存在，版本/依赖未核验")
            self.assertEqual(candidate["configuration_state"], "pyvenv.cfg 是链接，未读取")
            self.assertEqual(candidate["configured_python_version"], "")

    def test_environment_snapshot_reads_node_rust_and_go_version_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                '{"name":"web","engines":{"node":">=20 <22"},"dependencies":{"react":"^18"}}',
                encoding="utf-8",
            )
            (root / "Cargo.toml").write_text(
                '[package]\nname="native"\nversion="0.1.0"\nrust-version="1.78"\n', encoding="utf-8",
            )
            (root / "go.mod").write_text("module example.test/service\ngo 1.23\n", encoding="utf-8")
            (root / "package-lock.json").write_text("{}", encoding="utf-8")

            snapshot = self.context(root).build()
            runtimes = {item["runtime"]: item["version"] for item in snapshot["environment"]["declared_runtimes"]}

            self.assertEqual(runtimes, {"Node.js": ">=20 <22", "Rust": "1.78", "Go": "1.23"})
            self.assertIn("react", snapshot["text"])
            self.assertIn("Rust 1.78", snapshot["text"])
            self.assertIn("Go 1.23", snapshot["text"])
            self.assertIn("package-lock.json", snapshot["environment"]["lockfiles"])
            self.assertFalse(snapshot["environment"]["installed_dependencies_checked"])

    def test_focused_new_file_inherits_directory_rules_without_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            (root / "src" / "AGENTS.md").write_text("New modules must use type hints.", encoding="utf-8")
            result = self.context(root).build("a" * 6000, focus_paths=["src/new_module.py"])
            self.assertIn("New modules must use type hints", result["text"])

    def test_focused_directory_prioritizes_descendants_and_includes_code_without_query_hits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "src" / "domain" / "target.py"
            target.parent.mkdir(parents=True)
            target.write_text("class DirectoryTarget:\n    value = 'focused-code'\n", encoding="utf-8")
            (root / "unrelated.py").write_text("def something_else():\n    return 0\n", encoding="utf-8")

            result = self.context(root).build("Explain behavior", focus_paths=["src/domain"])

            self.assertIn("src/domain/target.py", result["sources"])
            self.assertIn("value = 'focused-code'", result["text"])
            self.assertNotIn("unrelated.py] 符号", result["text"])

    def test_unsaved_editor_snapshot_overrides_disk_for_context_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "src" / "module.py"
            source.parent.mkdir(parents=True)
            source.write_text("disk_version = 'stale'\n", encoding="utf-8")
            context = self.context(root)
            snapshot = context.build(
                "buffer_marker",
                focus_paths=["src/module.py"],
                content_overrides={"src/module.py": "def buffer_marker():\n    return 'fresh'\n"},
            )
            results = context.search(
                "buffer_marker", path="src", content_overrides={
                    "src/module.py": "def buffer_marker():\n    return 'fresh'\n",
                },
            )

            self.assertEqual(snapshot["editor_overrides"], ["src/module.py"])
            self.assertIn("buffer_marker", snapshot["text"])
            self.assertNotIn("disk_version", snapshot["text"])
            self.assertEqual([(item["path"], item["line"]) for item in results["results"]], [
                ("src/module.py", 1),
            ])
            self.assertEqual(source.read_text(encoding="utf-8"), "disk_version = 'stale'\n")

    def test_unsaved_project_manifest_updates_runtime_and_dependency_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "pyproject.toml"
            manifest.write_text(
                '[project]\nname = "demo"\nrequires-python = ">=3.10"\n'
                'dependencies = ["numpy>=1"]\n',
                encoding="utf-8",
            )
            snapshot = self.context(root).build(
                "project setup",
                content_overrides={
                    "pyproject.toml": (
                        '[project]\nname = "demo"\nrequires-python = ">=3.13"\n'
                        'dependencies = ["pandas>=2"]\n'
                    ),
                },
            )

            self.assertEqual(snapshot["environment"]["python_requirement"], ">=3.13")
            self.assertIn("pandas>=2", snapshot["text"])
            self.assertNotIn("numpy>=1", snapshot["text"])
            self.assertIn(">=3.13", snapshot["text"])
            self.assertEqual(manifest.read_text(encoding="utf-8").count(">=3.10"), 1)

    def test_budget_excluded_unsaved_file_never_falls_back_to_stale_disk_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_text("disk_secret_marker = 1\n", encoding="utf-8")
            context = self.context(root)

            snapshot = context.build("disk_secret_marker", excluded_paths=["module.py"])
            results = context.search("disk_secret_marker", excluded_paths=["module.py"])

            self.assertEqual(snapshot["editor_excluded"], ["module.py"])
            self.assertNotIn("disk_secret_marker", snapshot["text"])
            self.assertEqual(results["results"], [])
            with self.assertRaises(ValueError):
                context.build("x", content_overrides={"../outside.py": "unsafe"})

    def test_search_returns_real_lines_with_literal_case_glob_and_directory_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            (root / "src" / "module.py").write_text("before\nNeedle(.*)\nafter\n", encoding="utf-8")
            (root / "other.py").write_text("needle(.*)\n", encoding="utf-8")
            context = self.context(root)
            record = context.search("needle(.*)", path="src", glob="**/*.py")
            self.assertEqual([(r["path"], r["line"]) for r in record["results"]], [("src/module.py", 2)])
            self.assertEqual([r["line"] for r in record["results"][0]["context"]], [1, 2, 3])
            self.assertEqual(context.search("needle(.*)", path="src", case_sensitive=True)["results"], [])
            self.assertIn("src/module.py:2: Needle(.*)", context.format_search(record))

    def test_long_line_search_excerpt_contains_the_actual_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "minified.js").write_text("x" * 1000 + "TargetSymbol" + "y" * 1000, encoding="utf-8")
            context = self.context(root)
            record = context.search("TargetSymbol", context_lines=0)
            self.assertEqual(record["results"][0]["line"], 1)
            self.assertIn("TargetSymbol", record["results"][0]["text"])
            self.assertIn("TargetSymbol", context.format_search(record))
            self.assertIn("TargetSymbol", context.build("TargetSymbol")["text"])

    def test_ignored_sensitive_binary_and_large_sources_are_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text("ignored.py\ncache/\nREADME.md\n", encoding="utf-8")
            protected = [root / ".env", root / "ignored.py", root / "large.py", root / "cache" / "hidden.py", root / "README.md"]
            (root / "cache").mkdir()
            for path in protected:
                path.write_text("Needle secret" if path.name != "large.py" else "x" * 1001, encoding="utf-8")
            (root / "binary.py").write_bytes(b"Needle\0secret")
            (root / "bad_utf8.py").write_bytes(b"Needle\xff")
            (root / "visible.py").write_text("Needle visible\n", encoding="utf-8")
            original_open = Path.open

            def guarded_open(path: Path, *args, **kwargs):
                if path in protected:
                    raise AssertionError(f"should not read {path.name}")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", guarded_open):
                context = self.context(root, max_file_bytes=1000)
                result = context.search("Needle")
                snapshot = context.build("Needle")
            self.assertEqual([r["path"] for r in result["results"]], ["visible.py"])
            self.assertTrue(result["limited"])
            self.assertTrue(snapshot["limited"])
            self.assertNotIn("secret", snapshot["text"])
            self.assertNotIn(str(root), snapshot["text"])
            self.assertIn("不向模型发送本机绝对路径", snapshot["text"])
            with self.assertRaises(ValueError):
                context.search("Needle", path="../outside")
            with self.assertRaises(ValueError):
                context.search("Needle", path=".env")

    def test_git_ignore_rules_and_negation_work_in_real_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            GitManager(root).init()
            (root / ".gitignore").write_text("*.py\n!keep.py\n", encoding="utf-8")
            (root / "keep.py").write_text("Needle retained", encoding="utf-8")
            (root / "hidden.py").write_text("Needle ignored", encoding="utf-8")
            result = self.context(root).search("Needle")
            self.assertEqual([r["path"] for r in result["results"]], ["keep.py"])

    def test_incremental_cache_observes_edits_creation_deletion_and_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "a.py"
            source.write_text("token_old\n", encoding="utf-8")
            context = self.context(root)
            self.assertEqual(len(context.search("token_old")["results"]), 1)
            original_open = Path.open

            def cached_open(path: Path, *args, **kwargs):
                if path == source:
                    raise AssertionError("unchanged source was reread")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", cached_open):
                self.assertEqual(len(context.search("token_old")["results"]), 1)
            source.write_text("token_new\n", encoding="utf-8")
            self.assertEqual(context.search("token_old")["results"], [])
            target = root / "b.py"
            source.rename(target)
            self.assertEqual([r["path"] for r in context.search("token_new")["results"]], ["b.py"])
            target.unlink()
            self.assertEqual(context.search("token_new")["results"], [])

    def test_limits_are_visible_and_outputs_stay_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source.py").write_text("needle\n" * 10, encoding="utf-8")
            context = self.context(root, max_context_chars=300)
            result = context.search("needle", max_results=2)
            self.assertEqual(len(result["results"]), 2)
            self.assertTrue(result["limited"])
            snapshot = context.build("needle")
            self.assertLessEqual(len(snapshot["text"]), 300)
            self.assertTrue(snapshot["limited"])
            self.assertIn("长度预算", snapshot["text"])

    def test_list_paths_filters_suffixes_and_reports_inventory_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            (root / "src" / "module.PY").write_text("value = 1\n", encoding="utf-8")
            (root / "README.md").write_text("notes\n", encoding="utf-8")

            result = self.context(root).list_paths({".py"})

            self.assertEqual(result["paths"], ["src/module.PY"])
            self.assertTrue(result["complete"])

    def test_exact_file_limit_is_not_reported_as_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "only.py").write_text("value = 1\n", encoding="utf-8")

            result = self.context(root, max_files=1).list_paths()

            self.assertEqual(result["paths"], ["only.py"])
            self.assertTrue(result["complete"])

    def test_narrowing_search_scope_can_find_files_beyond_global_inventory_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(4):
                (root / f"a{index}.py").write_text("irrelevant", encoding="utf-8")
            folder = root / "z_src"
            folder.mkdir()
            (folder / "match.py").write_text("needle matched", encoding="utf-8")
            context = self.context(root, max_files=2)
            self.assertTrue(context.build()["limited"])
            record = context.search("needle", path="z_src")
            self.assertEqual([row["path"] for row in record["results"]], ["z_src/match.py"])
            record = context.search("needle", glob="z_src/*.py")
            self.assertEqual([row["path"] for row in record["results"]], ["z_src/match.py"])

    def test_larger_source_is_searchable_and_reads_only_the_requested_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "large_client.py"
            source.write_text("# padding\n" * 30000 + "def TargetSymbol():\n    return 42\n", encoding="utf-8")
            self.assertGreater(source.stat().st_size, CodingToolbox.MAX_READ_BYTES)
            toolbox = CodingToolbox(root, EventLedger(root))
            result = toolbox.search_code("TargetSymbol", context_lines=0)
            self.assertIn("large_client.py:30001", result)
            snippet = toolbox.read_file("large_client.py", start_line=30001, end_line=30002)
            self.assertEqual(snippet, "30001: def TargetSymbol():\n30002:     return 42")

    def test_read_window_output_budget_preserves_start_and_reports_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "long.py").write_text("# " + "x" * 30000 + "\n", encoding="utf-8")
            toolbox = CodingToolbox(root, EventLedger(root))
            result = toolbox.read_file("long.py")
            self.assertIn("输出达到长度预算", result)
            self.assertLess(len(result), 30000)

    def test_external_and_internal_symlinks_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            outside = Path(temp) / "outside.py"
            outside.write_text("Needle private", encoding="utf-8")
            try:
                os.symlink(outside, root / "linked.py")
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            result = self.context(root).search("Needle")
            self.assertEqual(result["results"], [])
            with self.assertRaises(ValueError):
                self.context(root).search("Needle", path="linked.py")


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.histories = []
        self.tools = []

    def chat(self, messages, tools=None, max_tokens=12000, request_id=""):
        self.histories.append(json.loads(json.dumps(messages)))
        self.tools.append({item["function"]["name"] for item in tools or []})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def tool(name, arguments):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_" + name, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


class AgentProjectContextTests(unittest.TestCase):
    def test_agent_searches_reads_edits_and_refreshes_persisted_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
            module = root / "src" / "price.py"
            module.parent.mkdir()
            module.write_text("class InventoryManager:\n    value = 41\n", encoding="utf-8")
            (module.parent / "AGENTS.md").write_text("Only edit the requested calculation.", encoding="utf-8")
            ledger = EventLedger(root)
            git = GitManager(root)
            git.init()
            git.commit_changes("baseline")
            events = []
            agent = CodingAgent(root, ledger, git, event_callback=lambda name, payload: events.append((name, payload)),
                                summary_settings=SummarySettings(enabled=False), require_verification=False,
                                patch_review=lambda preview: {int(hunk["id"]) for hunk in preview["hunks"]})
            provider = ScriptedProvider([
                tool("search_code", {"query": "InventoryManager"}),
                tool("read_file", {"path": "src/price.py"}),
                tool("apply_patch", {"patch": "*** Begin Patch\n*** Update File: src/price.py\n@@\n class InventoryManager:\n-    value = 41\n+    value = 42\n*** End Patch"}),
                tool("git_diff", {}),
                {"role": "assistant", "content": "Done", "tool_calls": []},
            ])
            with patch.object(OpenAICompatibleProvider, "from_env", return_value=provider):
                result = agent.run({"payload": {"session_id": "context_test", "prompt": "Fix InventoryManager", "context_files": ["src/price.py"]}})
            self.assertIn("search_code", provider.tools[0])
            self.assertIn("src/price.py:1", provider.histories[1][-1]["content"])
            self.assertIn("Only edit the requested calculation", provider.histories[2][-1]["content"])
            self.assertIn("value = 42", module.read_text(encoding="utf-8"))
            self.assertTrue(result["git_result_sha"])
            session = json.loads((root / ".research" / "sessions" / "context_test.json").read_text(encoding="utf-8"))
            self.assertIn("src/price.py", session["project_context"]["sources"])
            self.assertIn("host", session["project_context"]["environment"])
            self.assertTrue(any(name == "project_context_updated" for name, _ in events))
            self.assertEqual(session["messages"][0]["content"].count(ProjectContext.CONTEXT_PREFIX), 1)

    def test_retry_replaces_stale_context_without_adding_user_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            source = root / "src" / "state.py"
            source.write_text("token = 'old_state'\n", encoding="utf-8")
            ledger = EventLedger(root)
            agent = CodingAgent(root, ledger, GitManager(root), summary_settings=SummarySettings(enabled=False),
                                single_user_prompt_only=True)
            task = {"payload": {"session_id": "retry_context", "prompt": "Inspect token", "context_files": ["src/state.py"]}}
            with patch.object(OpenAICompatibleProvider, "from_env", return_value=ScriptedProvider([RetryableError("offline")])):
                with self.assertRaises(RetryableError):
                    agent.run(task)
            source.write_text("token = 'new_state'\n", encoding="utf-8")
            provider = ScriptedProvider([{ "role": "assistant", "content": "Reviewed", "tool_calls": []}])
            with patch.object(OpenAICompatibleProvider, "from_env", return_value=provider):
                agent.run(task)
            messages = provider.histories[0]
            self.assertIn("new_state", messages[0]["content"])
            self.assertNotIn("old_state", messages[0]["content"])
            self.assertEqual(sum(message["role"] == "user" for message in messages), 1)
            self.assertEqual(messages[0]["content"].count(ProjectContext.CONTEXT_PREFIX), 1)

    def test_explicit_context_tool_excludes_secrets_and_rejects_external_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("SECRET=hidden_value", encoding="utf-8")
            toolbox = CodingToolbox(root, EventLedger(root))
            self.assertNotIn("hidden_value", toolbox.execute("get_project_context", {}))
            with self.assertRaises(PermanentError):
                toolbox.execute("search_code", {"query": "SECRET", "path": "../outside"})

    def test_unsaved_buffer_is_readable_but_agent_write_tools_cannot_overwrite_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_text("disk_version = 1\n", encoding="utf-8")
            toolbox = CodingToolbox(root, EventLedger(root))
            toolbox.set_editor_context(
                {"module.py": "buffer_version = 2\n"}, ["module.py"],
            )

            self.assertIn("buffer_version", toolbox.read_file("module.py"))
            self.assertIn("buffer_version", toolbox.search_code("buffer_version"))
            self.assertIn("buffer_version", toolbox.get_project_context("buffer_version"))
            with self.assertRaises(PermanentError):
                toolbox.write_file("module.py", "overwritten = True\n")
            with self.assertRaises(PermanentError):
                toolbox.replace_in_file("module.py", "disk_version", "overwritten")
            with self.assertRaises(PatchError):
                toolbox.apply_patch(
                    "*** Begin Patch\n*** Update File: module.py\n@@\n-disk_version = 1\n+overwritten = True\n*** End Patch"
                )
            self.assertEqual(source.read_text(encoding="utf-8"), "disk_version = 1\n")
            toolbox.clear_editor_context()
            with self.assertRaises(PermanentError):
                toolbox.read_file("../outside.py")

    def test_oversized_unsaved_buffer_is_excluded_and_not_read_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_text("stale_disk_text = True\n", encoding="utf-8")
            toolbox = CodingToolbox(root, EventLedger(root))
            toolbox.set_editor_context({}, ["module.py"])

            with self.assertRaises(PermanentError):
                toolbox.read_file("module.py")
            self.assertNotIn("stale_disk_text", toolbox.search_code("stale_disk_text"))
            self.assertNotIn("stale_disk_text", toolbox.get_project_context("stale_disk_text"))
            with self.assertRaises(PermanentError):
                toolbox.replace_many_in_file("module.py", [{"old_text": "stale", "new_text": "new"}])

    def test_agent_task_uses_and_then_clears_its_editor_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_text("disk_value = 'old'\n", encoding="utf-8")
            agent = CodingAgent(
                root,
                EventLedger(root),
                GitManager(root),
                summary_settings=SummarySettings(enabled=False),
                require_verification=False,
            )
            provider = ScriptedProvider([
                tool("read_file", {"path": "module.py"}),
                tool("replace_in_file", {
                    "path": "module.py", "old_text": "buffer_value", "new_text": "changed_value",
                }),
                {"role": "assistant", "content": "已读取；请先保存缓冲区后再修改。", "tool_calls": []},
            ])
            task = {"payload": {
                "session_id": "unsaved-buffer-session",
                "prompt": "Review this file",
                "context_files": ["module.py"],
                "context_overrides": {"module.py": "buffer_value = 'new'\n"},
                "context_readonly_paths": ["module.py"],
            }}
            with patch.object(OpenAICompatibleProvider, "from_env", return_value=provider):
                agent.run(task)

            self.assertIn("buffer_value", provider.histories[1][-1]["content"])
            self.assertIn("未保存", provider.histories[2][-1]["content"])
            self.assertEqual(source.read_text(encoding="utf-8"), "disk_value = 'old'\n")
            self.assertEqual(agent.toolbox.context_overrides, {})
            self.assertEqual(agent.toolbox.context_excluded_paths, [])


class ProjectContextRepositoryRegressionTests(unittest.TestCase):
    def test_representative_workflows_retrieve_implementation_from_this_repository(self) -> None:
        root = Path(__file__).resolve().parents[1]
        context = ProjectContext(root, CodingToolbox.is_protected_path)
        cases = (
            ("review approved patch hunks", "scidev_patching.py", []),
            ("retry after HTTP 429 server delay", "scidev_core.py", []),
            ("project memory index summaries", "scidev_memory.py", []),
            ("task planning update statuses", "scidev_planning.py", []),
            ("nested test command discovery", "scidev_verification.py", []),
            ("responsive theme QML version tree", "scidev_client.py", []),
            (
                "俄罗斯方块网页画布键盘控制",
                "examples/tetris/index.html",
                ["examples/tetris"],
            ),
        )

        for query, expected, focus in cases:
            with self.subTest(query=query):
                result = context.build(query, focus_paths=focus)
                # Ranking is a bounded lexical heuristic over a live repository;
                # new files can shift neighbours without losing the right answer.
                self.assertIn(expected, result["sources"][:6], result["sources"])


if __name__ == "__main__":
    unittest.main()
