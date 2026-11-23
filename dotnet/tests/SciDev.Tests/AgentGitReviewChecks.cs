using System.Diagnostics;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class AgentGitReviewChecks
{
    private static readonly Uri Endpoint = new("http://127.0.0.1:1234/v1/chat/completions");
    private const string Credential = "fixture-current-model-secret";
    private static readonly ToolCall Request = new("git-1", "git_diff", "{}");

    internal static async Task RunAsync(DirectoryInfo workspace)
    {
        var git = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Git", "cmd", "git.exe");
        if (!File.Exists(git))
            return;
        var config = Path.Combine(workspace.Parent!.FullName, "git-empty-test-config");
        File.WriteAllText(config, "");
        try { await RunCasesAsync(workspace, git, config); }
        finally
        {
            var metadata = new DirectoryInfo(Path.Combine(workspace.FullName, ".git"));
            if (metadata.Exists)
                foreach (var file in metadata.EnumerateFiles("*", SearchOption.AllDirectories))
                    if (file.IsReadOnly)
                        file.IsReadOnly = false;
        }
    }

    private static async Task RunCasesAsync(DirectoryInfo workspace, string git, string config)
    {
        RunGit(git, workspace.FullName, config, "init", "--initial-branch=main");
        File.WriteAllText(Path.Combine(workspace.FullName, "notes.txt"), "original notes\n");
        File.WriteAllText(Path.Combine(workspace.FullName, "draft.txt"), "original draft\n");
        File.WriteAllText(Path.Combine(workspace.FullName, ".env"), "original private setting\n");
        File.WriteAllText(Path.Combine(workspace.FullName, "retired.txt"), "old token " + Credential + "\n");
        RunGit(git, workspace.FullName, config, "add", "--", "notes.txt", "draft.txt", ".env", "retired.txt");
        RunGit(git, workspace.FullName, config, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "commit", "-m", "Seed documents");
        File.WriteAllText(Path.Combine(workspace.FullName, "notes.txt"), "updated notes\n");
        File.WriteAllText(Path.Combine(workspace.FullName, "draft.txt"), "unshared disk draft\n");
        File.WriteAllText(Path.Combine(workspace.FullName, ".env"), "unshared private setting\n");
        File.WriteAllText(Path.Combine(workspace.FullName, "retired.txt"), "token removed\n");
        File.WriteAllText(Path.Combine(workspace.FullName, "fresh.txt"), "ordinary new file\n");
        File.WriteAllText(Path.Combine(workspace.FullName, "credential.txt"), Credential + "\n");
        File.WriteAllText(Path.Combine(workspace.FullName, "oversized.txt"), new string('x', 130 * 1024));
        var indexPath = Path.Combine(workspace.FullName, ".git/index");
        var indexBefore = File.ReadAllBytes(indexPath);
        var audit = new List<RunEvent>();

        await RequireDeniedAsync(Create(workspace.FullName, null, null, audit), "approval_denied");
        var reject = new Decisions(false);
        await RequireDeniedAsync(Create(workspace.FullName, null, reject, audit), "approval_denied");
        Assert(reject.Requests.Count == 1 && !Directory.Exists(Path.Combine(workspace.FullName, ".research")),
            "denied host approval does not create a Git snapshot");

        var approve = new Decisions(true);
        var drafts = new WorkspaceDrafts([("draft.txt", "unshared editor draft")]);
        var result = await Create(workspace.FullName, null, approve, audit, drafts).ExecuteAsync(Request, default);
        Assert(!result.ExternalDataShared && approve.Requests.Single().Kind == OperationKind.RunGitDiff &&
            approve.Requests[0].Destination == Endpoint.AbsoluteUri, "local Git needs one approval with the model destination");
        Assert(result.Text.Contains("-original notes") && result.Text.Contains("+updated notes") &&
            result.Text.Contains("+ordinary new file"), "Git review returns tracked and untracked text patches");
        Assert(!new[] { "draft.txt", ".env", "credential.txt", "oversized.txt", Credential, "unshared" }.Any(result.Text.Contains),
            "Git review excludes drafts, protected intermediates, credentials, and oversized inputs");
        Assert(result.Text.Contains("-old token [redacted]"), "the committed side of the patch redacts the active credential");
        Assert(indexBefore.SequenceEqual(File.ReadAllBytes(indexPath)), "Git review leaves the live index unchanged");

        File.Move(Path.Combine(workspace.FullName, ".env"), Path.Combine(workspace.FullName, "renamed-configuration.txt"));
        var sensitive = await Create(workspace.FullName, null, new Decisions(true), audit, drafts).ExecuteAsync(Request, default);
        Assert(!sensitive.Text.Contains("renamed-configuration.txt") && !sensitive.Text.Contains("fresh.txt") &&
            sensitive.Text.Contains("+updated notes"), "possible protected renames are not returned");

        var linked = await GitWorktrees.CreateAsync(workspace.FullName, "review-fixture", default);
        File.WriteAllText(Path.Combine(linked.Path, "notes.txt"), "linked update\n");
        var deniedShare = new Decisions(true, false);
        await RequireDeniedAsync(Create(linked.Path, workspace.FullName, deniedShare, audit), "approval_denied");
        Assert(deniedShare.Requests.Select(item => item.Kind).SequenceEqual([OperationKind.RunGitDiff, OperationKind.ShareGitDiff]) &&
            deniedShare.Requests[1].Before!.Contains("+linked update"), "linked worktrees request sharing approval for the exact preview");
        var approvedShare = new Decisions(true, true);
        var linkedResult = await Create(linked.Path, workspace.FullName, approvedShare, audit).ExecuteAsync(Request, default);
        Assert(linkedResult.ExternalDataShared && linkedResult.Text == approvedShare.Requests[1].Before,
            "the approved linked preview is the exact payload returned to the model");
        Assert(!System.Text.Json.JsonSerializer.Serialize(audit).Contains("+linked update"), "audit events omit preview content");

        CheckCommandSnapshot(workspace, git, config);
    }

    private static void CheckCommandSnapshot(DirectoryInfo workspace, string git, string config)
    {
        File.WriteAllText(Path.Combine(workspace.FullName, ".gitignore"), "artifacts/\ncredential.txt\n*.local\n");
        var artifacts = workspace.CreateSubdirectory("artifacts");
        File.WriteAllText(Path.Combine(artifacts.FullName, "kept.txt"), "tracked input within an ignored directory");
        var ignored = Path.Combine(artifacts.FullName, "model-output.txt");
        File.WriteAllText(ignored, Credential);
        File.WriteAllText(Path.Combine(workspace.FullName, "dev.local"), "untracked local state");
        RunGit(git, workspace.FullName, config, "add", "--force", "--", "artifacts/kept.txt");
        var scope = workspace.Parent!.CreateSubdirectory("docker-snapshot-output").FullName;
        var snapshot = new WorkspaceSnapshot(workspace.FullName, Credential);
        var fingerprint = snapshot.Capture(scope, default);
        Assert(File.Exists(Path.Combine(scope, "artifacts/kept.txt")) &&
            !File.Exists(Path.Combine(scope, "artifacts/model-output.txt")) &&
            !File.Exists(Path.Combine(scope, "credential.txt")) &&
            !File.Exists(Path.Combine(scope, "dev.local")), "snapshot includes only tracked entries under ignored directories");
        File.WriteAllText(ignored, "changed local output");
        Assert(snapshot.Capture(null, default) == fingerprint, "ignored writes do not invalidate test evidence");
        File.WriteAllText(Path.Combine(artifacts.FullName, "kept.txt"), "modified tracked input");
        Assert(snapshot.Capture(null, default) != fingerprint, "tracked file changes still invalidate evidence");
    }

    private static ApprovedGitTools Create(string workspace, string? metadata, IOperationReview? review,
        List<RunEvent> audit, WorkspaceDrafts? drafts = null) =>
        new(workspace, metadata, new Unused(), review, Endpoint, audit.Add, drafts ?? new([]), Credential);

    private static async Task RequireDeniedAsync(ApprovedGitTools tools, string status)
    {
        try { await tools.ExecuteAsync(Request, default); }
        catch (ToolDeniedException exception) when (exception.Status == status) { return; }
        throw new InvalidOperationException($"Expected Git review denial ({status}).");
    }

    private static void RunGit(string git, string workspace, string config, params string[] arguments)
    {
        var start = new ProcessStartInfo(git) { WorkingDirectory = workspace, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var argument in arguments)
            start.ArgumentList.Add(argument);
        start.Environment["GIT_CONFIG_NOSYSTEM"] = "1";
        start.Environment["GIT_CONFIG_GLOBAL"] = config;
        using var process = Process.Start(start)!;
        _ = process.StandardOutput.ReadToEnd();
        var error = process.StandardError.ReadToEnd();
        if (!process.WaitForExit(10_000) || process.ExitCode != 0)
            throw new InvalidOperationException("Failed to prepare a local Git fixture: " + error);
    }

    private static void Assert(bool success, string message)
    {
        if (!success)
            throw new InvalidOperationException(message);
    }

    private sealed class Unused : IFileTools
    {
        public ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token) => throw new InvalidOperationException();
    }

    private sealed class Decisions(params bool[] decisions) : IOperationReview
    {
        private readonly Queue<bool> _decisions = new(decisions);
        public List<OperationReview> Requests { get; } = [];

        public ValueTask<bool> ApproveAsync(OperationReview request, CancellationToken token)
        {
            Requests.Add(request);
            return ValueTask.FromResult(_decisions.Dequeue());
        }
    }
}
