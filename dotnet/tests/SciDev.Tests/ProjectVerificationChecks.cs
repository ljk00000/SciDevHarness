using SciDev.Core;

internal static class ProjectVerificationChecks
{
    public static async Task RunAsync()
    {
        await AssertResultAsync(new Runner(true, failServer: false), [], "passed", ["packages/server", "packages/ui"], []);
        await AssertResultAsync(new Runner(true, failServer: true), [], "failed", ["packages/ui"], ["packages/server"]);
        await AssertResultAsync(new Runner(false, failServer: false), [new("root", "run_command", ".")],
            "incomplete", [], ["packages/server", "packages/ui"]);
        await AssertResultAsync(new Runner(true, failServer: false), [new("server", "run_command", "packages/server")],
            "passed", ["packages/server", "packages/ui"], []);
        await AssertResultAsync(new Runner(true, failServer: false, testIssue: "no_tests"), [],
            "no_tests", ["packages/ui"], ["packages/server"]);
        await AssertResultAsync(new Runner(true, failServer: false, testIssue: "test_result_unconfirmed"), [],
            "test_result_unconfirmed", ["packages/ui"], ["packages/server"]);
        await AssertCargoWorkspaceAsync(automatic: false, "incomplete", [], [".", "crates/member", "crates/excluded"]);
        await AssertCargoWorkspaceAsync(automatic: true, "incomplete", [".", "crates/member"], ["crates/excluded"]);
        await AssertExternalEditsAsync();
    }

    private static async Task AssertCargoWorkspaceAsync(bool automatic, string status, string[] passed, string[] pending)
    {
        var model = new ScriptedModel([
            new(null, [new("root", "write_file", "Cargo.toml"), new("member", "write_file", "crates/member/src/lib.rs"),
                new("excluded", "write_file", "crates/excluded/src/lib.rs"), new("bare-root-test", "run_command", ".")]),
            new("Done", [])
        ]);
        var runner = new CargoRunner(automatic);
        var result = await new Agent(model, new Tools(), _ => { }, maxTurns: 2).RunAsync("cargo-fixture", "Update Rust workspace", default,
            verificationFingerprint: _ => "fixture-state", automaticVerification: runner);
        Assert(result.Verification is { } report && report.Status == status && report.PassedDirectories!.SequenceEqual(passed) &&
            report.PendingDirectories!.SequenceEqual(pending), "partial cargo workspace coverage must not cover unrelated scopes");
        Assert(runner.Invoked.SequenceEqual(automatic ? ["."] : []), "aggregate evidence skips redundant member tests");
    }

    private static async Task AssertExternalEditsAsync()
    {
        var outside = new ToolCall("outside", "edit_external_file", @"C:\outside\settings.json");
        var model = new ScriptedModel([new(null, [outside]), new("Done", [])]);
        var agent = new Agent(model, new Tools(), _ => { }, maxTurns: 2);
        var runner = new Runner(true, failServer: false);
        var result = await agent.RunAsync("external-fixture", "Edit the outside settings", default,
            verificationFingerprint: _ => "fixture-state", automaticVerification: runner);
        Assert(result.Success && result.Verification is null && runner.Invoked.Count == 0 &&
            result.FilesChanged is [var change] && change.Operation == "external_edit", "approved external files remain outside workspace verification");
    }

    private static async Task AssertResultAsync(Runner runner, ToolCall[] manualChecks, string status,
        string[] passed, string[] pending)
    {
        var model = new ScriptedModel([
            new(null, [new("write-server", "write_file", "packages/server/api.js"),
                new("write-ui", "write_file", "packages/ui/app.js"), .. manualChecks]),
            new("Done", [])
        ]);
        var events = new List<RunEvent>();
        var agent = new Agent(model, new Tools(), events.Add, maxTurns: 2);
        var result = await agent.RunAsync("scope-fixture", "Update both projects", default,
            verificationFingerprint: _ => "fixture-state", automaticVerification: runner);
        var report = result.Verification;
        Assert(result.Success == (status == "passed") && report?.Status == status,
            $"expected {status}, got {result.Status}/{report?.Status}");
        Assert(report!.PassedDirectories!.SequenceEqual(passed) && report.PendingDirectories!.SequenceEqual(pending),
            "verification reports the projects that passed and the projects still pending");
        Assert(status != "failed" || report.Directory == "packages/server", "a passing sibling does not erase an earlier failed test");
        var expectedAutomatic = runner.CanRunAutomatically
            ? manualChecks.Length == 0 ? ["packages/server", "packages/ui"] : new[] { "packages/ui" }
            : Array.Empty<string>();
        Assert(runner.Invoked.SequenceEqual(expectedAutomatic), "automatic checks are scoped and skip a verified project");
    }

    private static void Assert(bool success, string message)
    {
        if (!success)
            throw new InvalidOperationException(message);
    }

    private sealed class ScriptedModel(IEnumerable<ModelTurn> replies) : IModel
    {
        private readonly Queue<ModelTurn> _replies = new(replies);

        public Task<ModelTurn> CompleteAsync(IReadOnlyList<Message> messages, CancellationToken token) =>
            Task.FromResult(_replies.Dequeue());
    }

    private sealed class Tools : IFileTools
    {
        public ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token) => ValueTask.FromResult(call.Name switch
        {
            "run_command" => Evidence(call.Arguments, 0),
            "edit_external_file" => new ToolResult("edited", new(call.Arguments, "external_edit")),
            _ => new ToolResult("written", new(call.Arguments, "write")),
        });
    }

    private sealed class Runner(bool automatic, bool failServer, string? testIssue = null) : IAutomaticVerification
    {
        public readonly List<string> Invoked = [];
        public bool CanRunAutomatically => automatic;

        public VerificationPlan Discover(IReadOnlyList<FileChange> changes, CancellationToken token) => new(
            ["packages/server", "packages/ui"],
            [
                new("test", "packages/server/package.json", "packages/server", ["npm", "test"]),
                new("test", "packages/ui/package.json", "packages/ui", ["npm", "test"])
            ]);

        public ValueTask<ToolResult> RunAsync(string callId, VerificationCandidate candidate, CancellationToken token)
        {
            Invoked.Add(candidate.Directory);
            var server = candidate.Directory == "packages/server";
            return ValueTask.FromResult(Evidence(candidate.Directory, failServer && server ? 1 : 0, server ? testIssue : null));
        }
    }

    private sealed class CargoRunner(bool automatic) : IAutomaticVerification
    {
        public readonly List<string> Invoked = [];
        public bool CanRunAutomatically => automatic;

        public VerificationPlan Discover(IReadOnlyList<FileChange> changes, CancellationToken token) => new(
            [".", "crates/member", "crates/excluded"],
            [
                new("test", "Cargo.toml", ".", ["cargo", "test", "--workspace", "--offline"],
                    TargetDirectories: [".", "crates/member", "crates/excluded"]),
                new("test", "crates/member/Cargo.toml", "crates/member", ["cargo", "test"]),
            ]);

        public ValueTask<ToolResult> RunAsync(string callId, VerificationCandidate candidate, CancellationToken token)
        {
            Invoked.Add(candidate.Directory);
            return ValueTask.FromResult(new ToolResult("cargo fixture", CommandExitCode: 0,
                Verification: new("test", 0, "fixture-state", false, CoveredDirectories: [".", "crates/member"])));
        }
    }

    private static ToolResult Evidence(string directory, int exitCode, string? issue = null) => new("fixture evidence", CommandExitCode: exitCode,
        Verification: new("test", exitCode, "fixture-state", false, Directory: directory, TestIssue: issue));
}
