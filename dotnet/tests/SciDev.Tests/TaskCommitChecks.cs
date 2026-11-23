using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class TaskCommitChecks
{
    private static readonly Uri Endpoint = new("http://127.0.0.1:1234/v1/chat/completions");

    internal static async Task RunAsync(DirectoryInfo fixture)
    {
        string git;
        try { git = GitProcess.RequireExecutable(); }
        catch (ToolDeniedException ex) when (ex.Status == "git_cli_missing") { return; }
        var config = Path.Combine(fixture.FullName, "empty-git-config");
        File.WriteAllText(config, "");

        async Task<string> Git(string root, params string[] args)
        {
            var result = await GitProcess.RunAsync(GitProcess.Prepare(git, root, config), args, default);
            Assert(result.Code == 0 && !result.Truncated, "Git fixture command failed: " + string.Join(' ', args));
            return result.Text.TrimEnd('\r', '\n');
        }

        try
        {
            var workspace = fixture.CreateSubdirectory("primary").FullName;
            await Git(workspace, "init", "--initial-branch=main");
            File.WriteAllText(Path.Combine(workspace, "notes.txt"), "original notes\n");
            File.WriteAllText(Path.Combine(workspace, "user.txt"), "original user file\n");
            File.WriteAllText(Path.Combine(workspace, "staged.txt"), "original staged file\n");
            File.WriteAllText(Path.Combine(workspace, ".gitignore"), "ignored.txt\n.research/\n");
            await Git(workspace, "add", "--all");
            await Git(workspace, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "Seed");
            var parent = await Git(workspace, "rev-parse", "HEAD");
            File.WriteAllText(Path.Combine(workspace, "user.txt"), "already edited by operator\n");
            File.WriteAllText(Path.Combine(workspace, "staged.txt"), "original user staging\n");
            await Git(workspace, "add", "--", "staged.txt");
            var stagedEntry = await Git(workspace, "ls-files", "--stage", "--", "staged.txt");
            File.WriteAllText(Path.Combine(workspace, "staged.txt"), "later user edit\n");

            var task = await CreateAsync(workspace, "Update project notes", [
                ("notes.txt", "original notes", "new task notes"),
                ("user.txt", "already edited by operator", "task mixed with earlier edit"),
                ("fresh [中文].txt", null, "new task file\n"),
                ("ignored.txt", null, "ignored contents\n"),
            ]);
            var log = new List<RunEvent>();
            var index = Path.Combine(workspace, ".git/index");
            var before = await File.ReadAllBytesAsync(index);
            var disallow = new Review(request => request.Kind == OperationKind.PrepareGitCommit);
            var denied = await ExecuteAsync(task, disallow, log);
            var afterDenial = await File.ReadAllBytesAsync(index);
            Assert(denied.Status == "approval_denied" && disallow.Requests.Count == 2 &&
                before.SequenceEqual(afterDenial) && await Git(workspace, "rev-parse", "HEAD") == parent,
                $"denying publication must preserve index and branch, got {denied.Status}");

            var approve = new Review(_ => true);
            var committed = await ExecuteAsync(task, approve, log);
            Assert(committed is { Status: "committed", FileCount: 2, Omitted: 2 }, $"expected two approved files, got {committed}");
            var preview = approve.Requests.Single(item => item.Kind == OperationKind.PublishGitCommit).Before!;
            Assert(preview.Contains("-original notes") && preview.Contains("+new task notes") &&
                preview.Contains("fresh [中文].txt") && !preview.Contains("earlier edit") && !preview.Contains("ignored contents"),
                "only the eligible exact diffs are presented");
            Assert(await Git(workspace, "rev-parse", "HEAD") == committed.CommitId &&
                await Git(workspace, "show", "HEAD:notes.txt") == "new task notes" &&
                await Git(workspace, "show", "HEAD:fresh [中文].txt") == "new task file" &&
                await Git(workspace, "show", "HEAD:user.txt") == "original user file" &&
                await Git(workspace, "show", "HEAD:staged.txt") == "original staged file" &&
                await Git(workspace, "ls-files", "--stage", "--", "staged.txt") == stagedEntry &&
                await Git(workspace, "diff", "--cached", "--name-only") == "staged.txt" &&
                File.ReadAllText(Path.Combine(workspace, "staged.txt")) == "later user edit\n",
                "commit uses a separate index and leaves the user's staged and unstaged changes available");
            Assert(!JsonSerializer.Serialize(log).Contains("new task notes"), "commit preview is not written to the audit trail");

            var race = await CreateAsync(workspace, "Create more notes", [("race.txt", null, "task content\n")]);
            var interfere = new Review(request =>
            {
                if (request.Kind == OperationKind.PublishGitCommit)
                    File.WriteAllText(Path.Combine(workspace, "race.txt"), "operator changed during approval\n");
                return true;
            });
            var changed = await ExecuteAsync(race, interfere, log);
            Assert(changed.Status == "git_task_commit_changed" && await Git(workspace, "rev-parse", "HEAD") == committed.CommitId &&
                await Git(workspace, "ls-files", "--stage", "--", "staged.txt") == stagedEntry,
                $"external changes during approval abort publication, got {changed.Status}");

            var indexRace = await CreateAsync(workspace, "Prepare index race notes", [("index-race.txt", null, "safe content\n")]);
            var newer = new Review(request =>
            {
                if (request.Kind == OperationKind.PublishGitCommit)
                    Git(workspace, "add", "--", "staged.txt").GetAwaiter().GetResult();
                return true;
            });
            var changedIndex = await ExecuteAsync(indexRace, newer, log);
            Assert(changedIndex.Status == "git_task_commit_changed" && await Git(workspace, "rev-parse", "HEAD") == committed.CommitId &&
                await Git(workspace, "show", ":staged.txt") == "later user edit", "index changes after review must survive");

            var interrupted = Guid.NewGuid().ToString("N");
            using (var journal = new RunJournal(workspace, interrupted, null))
            {
                journal.Record(new("task_submitted", 0, new { task = "Make a commit" }));
                journal.Record(new("run_finished", 0, new { status = "completed" }));
                journal.Record(new("task_summary", 0, new { text = "Done" }));
                journal.Record(new("git_task_commit_publishing", 0,
                    new { reference = "refs/heads/main", commit = committed.CommitId, paths = new[] { "notes.txt" } }));
            }
            var inspection = RunInspection.Check(workspace, interrupted);
            Assert(inspection.Status == "needs_inspection" && inspection.GitCommit?.CommitId == committed.CommitId,
                "an interrupted publication remains visible even after the Agent finished its task");

            var veto = await CreateAsync(workspace, "创建草稿，不要提交 Git", [("draft.txt", null, "dev notes\n")]);
            var unexpected = new Review(_ => throw new InvalidOperationException());
            Assert((await ExecuteAsync(veto, unexpected, log)).Status == "skipped_user" && unexpected.Requests.Count == 0,
                "user wording overrides the opt-in without querying Git");

            var linked = await GitWorktrees.CreateAsync(workspace, "task", default);
            var worktree = await CreateAsync(linked.Path, "Add isolated notes", [("linked.txt", null, "isolated notes\n")], workspace);
            var isolated = await ExecuteAsync(worktree, new Review(_ => true), log);
            Assert(isolated.Status == "committed" && await Git(linked.Path, "show", "HEAD:linked.txt") == "isolated notes" &&
                await Git(workspace, "rev-parse", "HEAD") == committed.CommitId, $"linked publication stays on its own branch, got {isolated}");

            var empty = fixture.CreateSubdirectory("unborn").FullName;
            await Git(empty, "init", "--initial-branch=main");
            var first = await CreateAsync(empty, "Create the first file", [("first.txt", null, "first content\n")]);
            var initialized = await ExecuteAsync(first, new Review(_ => true), log);
            Assert(initialized.Status == "committed" && await Git(empty, "show", "HEAD:first.txt") == "first content" &&
                await Git(empty, "diff", "--cached", "--name-only") == "", $"first task creates the initial commit, got {initialized}");

            var moves = fixture.CreateSubdirectory("moves").FullName;
            await Git(moves, "init", "--initial-branch=main");
            File.WriteAllText(Path.Combine(moves, "seed.sh"), "echo seed\n");
            File.WriteAllText(Path.Combine(moves, "operator.txt"), "original\n");
            File.WriteAllText(Path.Combine(moves, ".gitignore"), "ignored.sh\n.research/\n");
            await Git(moves, "add", "--all");
            await Git(moves, "update-index", "--chmod=+x", "--", "seed.sh");
            await Git(moves, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "Seed");
            File.WriteAllText(Path.Combine(moves, "operator.txt"), "unrelated staging\n");
            await Git(moves, "add", "--", "operator.txt");
            var moved = await CreateRenameAsync(moves, ("seed.sh", "middle.sh"), ("middle.sh", "moved.sh"));
            var moveReview = new Review(_ => true);
            var moveCommit = await ExecuteAsync(moved, moveReview, log);
            var moveDiff = moveReview.Requests.Last().Before ?? "";
            Assert(moveCommit is { Status: "committed", FileCount: 2 } && moveDiff.Contains("seed.sh") && moveDiff.Contains("moved.sh") &&
                !moveDiff.Contains("middle.sh") && await Git(moves, "diff-tree", "--no-commit-id", "-r", "--name-status", "-M", "HEAD") ==
                "R100\tseed.sh\tmoved.sh" && (await Git(moves, "ls-tree", "HEAD", "--", "moved.sh")).StartsWith("100755 ", StringComparison.Ordinal) &&
                await Git(moves, "diff", "--cached", "--name-only") == "operator.txt", $"commit includes both endpoints and the original mode, got {moveCommit}");

            var ignoredMove = await CreateRenameAsync(moves, ("moved.sh", "ignored.sh"));
            var ignoredReview = new Review(_ => true);
            var omittedMove = await ExecuteAsync(ignoredMove, ignoredReview, log);
            Assert(omittedMove.Status == "nothing_to_commit" && ignoredReview.Requests.Count == 1 &&
                await Git(moves, "rev-parse", "HEAD") == moveCommit.CommitId, $"an ignored target excludes both endpoints, got {omittedMove}");

            File.Move(Path.Combine(moves, "ignored.sh"), Path.Combine(moves, "moved.sh"));
            var contestedMove = await CreateRenameAsync(moves, ("moved.sh", "contested.sh"));
            var contestedByUser = new Review(request =>
            {
                if (request.Kind == OperationKind.PublishGitCommit)
                    File.WriteAllText(Path.Combine(moves, "moved.sh"), "recreated by operator");
                return true;
            });
            var contested = await ExecuteAsync(contestedMove, contestedByUser, log);
            Assert(contested.Status == "git_task_commit_changed" && await Git(moves, "rev-parse", "HEAD") == moveCommit.CommitId &&
                File.ReadAllText(Path.Combine(moves, "moved.sh")) == "recreated by operator", "a reappearing source aborts publication");
        }
        finally
        {
            foreach (var file in fixture.EnumerateFiles("*", SearchOption.AllDirectories))
                if (file.IsReadOnly)
                    file.IsReadOnly = false;
        }
    }

    private static async Task<FixtureRun> CreateRenameAsync(string workspace, params (string Path, string Destination)[] moves)
    {
        var id = Guid.NewGuid().ToString("N");
        using var journal = new RunJournal(workspace, id, null);
        var checkpoint = new WorkspaceCheckpoint(workspace, id);
        var tools = new WorkspaceFiles(workspace, new AcceptPatch(), checkpoint);
        var files = new List<FileChange>();
        foreach (var (path, destination) in moves)
        {
            var result = await tools.ExecuteAsync(WorkspaceRenameChecks.Rename(path, destination), default);
            files.Add(result.Change!);
            files.AddRange(result.RelatedChanges!);
        }
        return new(new(workspace, "Move the script", Endpoint, "fixture", AutoCommit: true),
            new(id, true, "completed", "Done", files), checkpoint);
    }

    private static async Task<FixtureRun> CreateAsync(string workspace, string prompt,
        (string Path, string? Before, string After)[] edits, string? repository = null)
    {
        var id = Guid.NewGuid().ToString("N");
        using var journal = new RunJournal(workspace, id, null);
        var checkpoint = new WorkspaceCheckpoint(workspace, id, null);
        var tools = new WorkspaceFiles(workspace, new AcceptPatch(), checkpoint);
        var changes = new List<FileChange>();
        foreach (var (path, before, after) in edits)
        {
            var callId = Guid.NewGuid().ToString("N");
            var call = before is null
                ? new ToolCall(callId, "write_file", JsonSerializer.Serialize(new { path, content = after }))
                : new ToolCall(callId, "apply_patch", JsonSerializer.Serialize(new { path, old_text = before, new_text = after }));
            var result = await tools.ExecuteAsync(call, default);
            changes.Add(result.Change ?? throw new InvalidOperationException("Expected a checkpointed change."));
        }
        return new(new(workspace, prompt, Endpoint, "fixture", GitMetadataWorkspace: repository, AutoCommit: true),
            new(id, true, "completed", "Done", changes), checkpoint);
    }

    private static Task<TaskCommitResult> ExecuteAsync(FixtureRun task, IOperationReview review, List<RunEvent> log) =>
        TaskCommitWorkflow.RunAsync(task.Options, task.Result, task.Checkpoint, review, null, null, log.Add, default);

    private sealed record FixtureRun(WorkspaceRunOptions Options, RunResult Result, WorkspaceCheckpoint Checkpoint);

    private sealed class Review(Func<OperationReview, bool> decide) : IOperationReview
    {
        public List<OperationReview> Requests { get; } = [];
        public ValueTask<bool> ApproveAsync(OperationReview operation, CancellationToken token)
        {
            Requests.Add(operation);
            return new(decide(operation));
        }
    }

    private sealed class AcceptPatch : IFileEditApproval
    {
        public ValueTask<bool> ApproveAsync(FileEditPreview preview, CancellationToken token) => new(true);
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }
}
