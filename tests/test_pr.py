from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from scidev_pr import (
    PullRequestError,
    PullRequestPlan,
    build_plan,
    create_pull_request,
    parse_github_remote,
    pull_request_diff,
    review_pull_request,
    view_pull_request,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "PR Test")
    _git(root, "config", "user.email", "pr@example.invalid")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "remote", "add", "origin", "git@github.com:example/project.git")
    _git(root, "checkout", "-q", "-b", "feature/task")


class PullRequestTests(unittest.TestCase):
    def test_remote_parsing(self) -> None:
        self.assertEqual(parse_github_remote("git@github.com:owner/repo.git"), ("owner", "repo"))
        self.assertEqual(parse_github_remote("https://github.com/owner/repo"), ("owner", "repo"))
        self.assertIsNone(parse_github_remote("https://gitlab.com/owner/repo"))

    def test_plan_uses_feature_branch_and_github_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-pr-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            _repo(root)
            plan = build_plan(root, title="Feature task", body="body")
            self.assertEqual(plan.repo, "example/project")
            self.assertEqual(plan.branch, "feature/task")
            self.assertEqual(plan.base, "main")
            self.assertEqual(plan.title, "Feature task")

    def test_plan_rejects_base_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-pr-base-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            _repo(root)
            _git(root, "checkout", "-q", "master")
            with self.assertRaises(PullRequestError):
                build_plan(root)

    def test_create_respects_network_policy_and_builds_commands(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-pr-create-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            _repo(root)
            (root / ".research").mkdir()
            (root / ".research" / "settings.json").write_text(
                '{"network_policy": "deny"}', encoding="utf-8",
            )
            plan = PullRequestPlan(
                workspace=root,
                repo="example/project",
                branch="feature/task",
                base="main",
                title="Title",
                body="",
                draft=True,
                remote="git@github.com:example/project.git",
            )
            with self.assertRaises(PullRequestError):
                create_pull_request(plan)
            calls: list[list[str]] = []

            def runner(args, **kwargs):
                calls.append(list(args))
                return subprocess.CompletedProcess(args, 0, stdout="https://github.com/example/project/pull/1\n", stderr="")

            result = create_pull_request(plan, runner=runner, allow_network=True)
            self.assertEqual(result["url"], "https://github.com/example/project/pull/1")
            self.assertEqual(calls[0][:4], ["git", "push", "-u", "origin"])
            self.assertIn("gh", calls[1][0])
            self.assertIn("--draft", calls[1])
            self.assertIn("--fill", calls[1])


    def test_pr_diff_is_bounded_and_network_gated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-pr-diff-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            _repo(root)
            calls: list[list[str]] = []

            def runner(args, **kwargs):
                calls.append(list(args))
                return subprocess.CompletedProcess(args, 0, stdout="diff --git a/x b/x\n+hello\n", stderr="")

            result = pull_request_diff(root, "7", runner=runner, max_chars=1000)
            self.assertEqual(result["number"], 7)
            self.assertIn("diff --git", result["diff"])
            self.assertFalse(result["truncated"])
            self.assertEqual(calls[0][:4], ["gh", "pr", "diff", "7"])
            self.assertIn("--color=never", calls[0])

            (root / ".research").mkdir()
            (root / ".research" / "settings.json").write_text('{"network_policy": "deny"}', encoding="utf-8")
            with self.assertRaises(PullRequestError):
                pull_request_diff(root, 7, runner=runner)
            with self.assertRaises(PullRequestError):
                pull_request_diff(root, 0, runner=runner, allow_network=True)

    def test_view_and_review_pull_request(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-pr-review-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            _repo(root)
            calls: list[list[str]] = []

            def runner(args, **kwargs):
                calls.append(list(args))
                if "view" in args:
                    return subprocess.CompletedProcess(
                        args, 0,
                        stdout='{"number": 7, "title": "Task", "state": "OPEN", "url": "https://github.com/example/project/pull/7"}',
                        stderr="",
                    )
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            viewed = view_pull_request(root, 7, runner=runner)
            self.assertEqual(viewed["number"], 7)
            self.assertEqual(calls[0][:4], ["gh", "pr", "view", "7"])
            reviewed = review_pull_request(
                root, 7, "comment", body="Please add a test", runner=runner,
            )
            self.assertEqual(reviewed["number"], 7)
            self.assertEqual(calls[1][:5], ["gh", "pr", "review", "7", "--comment"])
            self.assertIn("Please add a test", calls[1])
            with self.assertRaises(PullRequestError):
                review_pull_request(root, 7, "comment", body="", runner=runner)
            with self.assertRaises(PullRequestError):
                review_pull_request(root, 7, "dismiss", runner=runner)
            with self.assertRaises(PullRequestError):
                view_pull_request(root, 0, runner=runner)


if __name__ == "__main__":
    unittest.main()
