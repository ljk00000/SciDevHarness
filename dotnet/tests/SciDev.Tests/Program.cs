using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using SciDev.Core;
using SciDev.Infrastructure;

if (args is ["--host-command-fixture", ..var fixtureArguments])
{
    await HostCommandChecks.FixtureAsync(fixtureArguments);
    return;
}
if (args is ["--mcp-fixture", var behavior])
{
    await McpClientChecks.FixtureAsync(behavior);
    return;
}

var parent = Path.GetFullPath(Path.Combine(Directory.GetCurrentDirectory(), ".tmp-tests"));
var root = Directory.CreateDirectory(Path.Combine(parent, $"scidev-approval-{Guid.NewGuid():N}"));
try
{
    await Approved(root.CreateSubdirectory("approved").FullName);
    await ChangedDuringReview(root.CreateSubdirectory("conflict").FullName, replace: false);
    await ChangedDuringReview(root.CreateSubdirectory("changed-identity").FullName, replace: true);
    await NewFileChangedExternally(root.CreateSubdirectory("external").FullName);
    await Cancelled(root.CreateSubdirectory("cancelled").FullName);
    await RetryAfterReadLock(root.CreateSubdirectory("read-lock").FullName);
    await RestoreEditedFile(root.CreateSubdirectory("checkpoint").FullName, interveningEdit: false);
    await RestoreEditedFile(root.CreateSubdirectory("checkpoint-diverged").FullName, interveningEdit: true);
    await WorkspaceRenameChecks.RunAsync(root.CreateSubdirectory("rename"));
    await ExternalFileApprovalChecks.RunAsync(root.CreateSubdirectory("external-approval"));
    await EditorDiagnosticChecks.RunAsync(root.CreateSubdirectory("editor-diagnostic"));
    await PythonDebuggerChecks.RunAsync(root.CreateSubdirectory("python-debugger"));
    await PythonEditorIntelChecks.RunAsync(root.CreateSubdirectory("python-editor-intel"));
    await NetworkApprovalChecks.RunAsync();
    await EditorDraftChecks(root.CreateSubdirectory("editor-drafts"));
    OperatorDocumentChecks(root.CreateSubdirectory("operator-editor"));
    DesktopPreferencesChecks(root.CreateSubdirectory("desktop-preferences"));
    DesktopTaskQueueChecks(root.CreateSubdirectory("desktop-queue"));
    SandboxVerificationChecks();
    DockerFailureChecks();
    ContainerPrefixChecks(root.CreateSubdirectory("container-prefix"));
    LegacyCommandChecks();
    RunJsonChecks();
    TextToolCallChecks();
    EvaluationSuiteChecks();
    VerificationDiagnosticsChecks();
    VerificationCandidateChecks(root.CreateSubdirectory("verification-project"));
    TestEvidenceChecks.Run(root.CreateSubdirectory("test-evidence"));
    await ProjectVerificationChecks.RunAsync();
    await AgentGitReviewChecks.RunAsync(root.CreateSubdirectory("agent-git-review"));
    await TaskCommitChecks.RunAsync(root.CreateSubdirectory("task-commit"));
    await HostCommandChecks.RunAsync(root.CreateSubdirectory("host-command"));
    await McpClientChecks.RunAsync(root.CreateSubdirectory("mcp-client"));
    await McpHttpChecks.RunAsync(root.CreateSubdirectory("mcp-http"));
    await PullRequestChecks.RunAsync(root.CreateSubdirectory("github-pr"));
    await PullRequestPublishChecks.RunAsync(root.CreateSubdirectory("github-publish"));
    await PreviewUpdateChecks.RunAsync(root.CreateSubdirectory("preview-update"));
    await TerminalScreenChecks.RunAsync(root.CreateSubdirectory("terminal-screen").FullName);
    Console.WriteLine("Workspace, approval, host process, MCP, Git, terminal, updates, verification and state regression checks passed.");
}
finally
{
    if (!string.Equals(root.Parent?.FullName, parent, StringComparison.OrdinalIgnoreCase))
        throw new InvalidOperationException("Unexpected fixture path.");
    root.Delete(recursive: true);
}

static void RunJsonChecks()
{
    var input = new { accepted = true, status = "result true", nested = new[] { "true", "result" } };
    using var json = JsonDocument.Parse(RunJson.SerializeRedacted(input, new(), "true"));
    Assert(json.RootElement.GetProperty("accepted").GetBoolean() &&
        json.RootElement.GetProperty("status").GetString() == "result [redacted]" &&
        json.RootElement.GetProperty("nested")[0].GetString() == "[redacted]", "JSON credential masking preserves bool values");
}

static void DockerFailureChecks()
{
    const string pipe = "npipe:////./pipe/dockerDesktopLinuxEngine";
    const string fallback = "docker_image_probe_failed";
    Assert(DockerProbe.FailureStatus("permission denied while trying to connect to the docker API at " + pipe, fallback) ==
        "docker_engine_access_denied", "the apphost pipe failure is an engine access error");
    Assert(DockerProbe.FailureStatus("error during connect: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.", fallback) ==
        "docker_linux_unavailable", "an absent Linux engine is distinguishable from a missing image");
    Assert(DockerProbe.FailureStatus("\r\nError response from daemon: No such image: fixture:some-image\r\n", fallback) ==
        "image_not_local", "a missing image requires explicit daemon evidence");
    Assert(DockerProbe.FailureStatus("Access denied while reading private-token from config", fallback) == fallback &&
        !DockerProbe.Unavailable(fallback).Message.Contains("private-token", StringComparison.Ordinal),
        "other errors remain unclassified and the published diagnostic never includes Docker stderr");
}

static void EvaluationSuiteChecks()
{
    var smoke = EvaluationSuite.Load(Path.GetFullPath("evals/smoke/suite.json"), credential: null);
    var standard = EvaluationSuite.Load(Path.GetFullPath("evals/standard/suite.json"), credential: null);
    var local = EvaluationSuite.Load(Path.GetFullPath("evals/local/suite.json"), credential: null);
    Assert(smoke.Name == "smoke" && smoke.Tasks.Length == 3 && standard.Name == "standard-20" && standard.Tasks.Length == 20,
        "load shipped Python suite definitions into the C# evaluator");
    Assert(smoke.Tasks[0].Setup.ContainsKey("math_utils.py") && smoke.Tasks[^1].Check.SequenceEqual(["python", "-m", "unittest", "-q"]),
        "setup bytes and Linux check arguments are preserved");
    Assert(local.Tasks.Length == 3 && local.Environment["SCIDEV_TEXT_TOOL_CALL_FALLBACK"] == "1", "the shipped local suite opts into text tools");
    var fixture = standard.Tasks.Single(task => task.Id == "pytest-fixture");
    Assert(fixture.EditableTests.SetEquals(["test_users.py"]) && fixture.CanEdit("test_users.py") &&
        !fixture.CanEdit("test_fixture_contract.py") && !fixture.CanEdit("test_other.py"),
        "the fixture exercise grants only the prepared test, with the separate judge remaining protected");
}

static void TextToolCallChecks()
{
    var tools = JsonNode.Parse("""
        [{"function":{"name":"write_file","parameters":{"type":"object","required":["path","content"],
          "properties":{"path":{"type":"string"},"content":{"type":"string"}}}}}]
        """)!;
    const string call = """{"name":"write_file","arguments":{"path":"notes.txt","content":"hi"}}""";
    var turn = TextToolCalls.Normalize(new("1: <tool_call>\n2: " + call + "\n3: </tool_call>", []), tools);
    Assert(turn.Text is null && turn.Calls is [var parsed] && parsed.Name == "write_file" && parsed.Id.StartsWith("text-"),
        "the opted-in fallback recovers an explicitly marked and numbered call");
    Assert(TextToolCalls.Normalize(new("<tool_request>" + call + "," + call + "</tool_request>", []), tools).Calls.Count == 2,
        "distinct validated calls in a textual envelope are batched");
    foreach (var invalid in new[] { "Example: " + call, call.Replace("write_file", "run_host_command"),
        call.Replace("\"hi\"", "7"), call.Replace("\"hi\"", "\"hi\",\"content\":\"again\""),
        call.Replace("\"hi\"", "\"hi\",\"outside_workspace\":true"), "<tool_call>" + call[..^2] + "</tool_call>" })
        Assert(TextToolCalls.Normalize(new(invalid, []), tools).Calls.Count == 0, "invalid, unsupported and illustrative text stays prose");
}

static void LegacyCommandChecks()
{
    using var simple = JsonDocument.Parse("""{"command":"node --test", "timeout_seconds":600, "outside_workspace":false}""");
    var parsed = LegacyContainerCommand.Read(simple.RootElement);
    Assert(parsed.Arguments.SequenceEqual(["/bin/sh", "-c", "node --test"]) &&
        parsed.PolicyArguments?.SequenceEqual(["node", "--test"]) == true && parsed.TimeoutSeconds == 300 && parsed.Directory == ".",
        "plain legacy commands stay eligible for the normal verification policy");
    using var shell = JsonDocument.Parse("""{"command":"printf '%s' 'quoted value'", "timeout_seconds":1, "directory":"src"}""");
    parsed = LegacyContainerCommand.Read(shell.RootElement);
    Assert(parsed.Arguments.SequenceEqual(["/bin/sh", "-c", "printf '%s' 'quoted value'"]) && parsed.PolicyArguments is null &&
        parsed.TimeoutSeconds == 1 && parsed.Directory == "src",
        "the original quoting goes to the container shell");
    using var outside = JsonDocument.Parse("""{"command":"node --test", "outside_workspace":true}""");
    try { _ = LegacyContainerCommand.Read(outside.RootElement); }
    catch (ToolDeniedException error) when (error.Status == "host_approval_required") { return; }
    throw new InvalidOperationException("Legacy outside_workspace must not launch a host process.");
}

static void ContainerPrefixChecks(DirectoryInfo workspace)
{
    var list = ContainerCommandPrefixes.Parse("node verify.cjs; npm run custom-check\nnode separate.cjs");
    var policy = new SandboxCommandApproval(workspace.FullName, ExecutionMode.WorkspaceWrite, null, _ => { }, list);
    Assert(policy.AutomaticCategory(["node", "verify.cjs", "extra"]) == "allowlist", "an exact prefix authorizes additional argv tokens");
    Assert(policy.AutomaticCategory(["node", "verify.cjsx"]) is null && policy.AutomaticCategory(["Node", "verify.cjs"]) is null,
        "argv boundaries and case are exact on Linux");
    Assert(policy.AutomaticCategory(["node", "--test"]) == "test" &&
        SandboxVerification.EvidenceCategory(["node", "verify.cjs"]) is null,
        "the allowlist does not provide verification evidence");
    Assert(new SandboxCommandApproval(workspace.FullName, ExecutionMode.Manual, null, _ => { }, list)
        .AutomaticCategory(["node", "verify.cjs"]) is null, "manual mode cannot apply an allowlist");
    var full = new SandboxCommandApproval(workspace.FullName, ExecutionMode.FullAccess, null, _ => { });
    Assert(full.AutomaticCategory(["node", "verify.cjs"]) == "full_access" && full.AutomaticCategory(["node", "--test"]) == "test" &&
        full.AutomaticCategory(["git", "status"]) == "full_access", "full access recognizes checks and ordinary offline commands");
    foreach (var blocked in new[] { new[] { "git", "-C", ".", "add", "." }, ["/bin/sh", "-c", "git reset --hard"] })
    {
        try { _ = full.AutomaticCategory(blocked); }
        catch (ToolDeniedException error) when (error.Status == "sandbox_command_blocked") { continue; }
        throw new InvalidOperationException("Expected a harness-owned Git command to be denied before starting Docker.");
    }
    var options = new WorkspaceRunOptions(workspace.FullName, "fixture", new Uri("http://localhost:1234/v1/chat/completions"),
        "fixture", Mode: ExecutionMode.WorkspaceWrite, SandboxImage: "node:20-alpine", AllowedCommands: "node verify.cjs");
    WorkspaceRunner.ValidateOptions(options, null, false, false);
    foreach (var incompatible in new[] { options with { Mode = ExecutionMode.Manual }, options with { SandboxImage = null } })
    {
        try { WorkspaceRunner.ValidateOptions(incompatible, null, false, false); }
        catch (ArgumentException) { continue; }
        throw new InvalidOperationException("Expected the allowlist to require workspace-write Docker execution.");
    }
    foreach (var invalid in new[] { "node ../script.js", "node && pwd", "/bin/sh", "X=val npm", "node `pwd`" })
    {
        try { _ = ContainerCommandPrefixes.Parse(invalid); }
        catch (ArgumentException) { continue; }
        throw new InvalidOperationException("Expected invalid command prefix.");
    }
}

static void VerificationCandidateChecks(DirectoryInfo workspace)
{
    var server = workspace.CreateSubdirectory("packages/server");
    var ui = workspace.CreateSubdirectory("packages/ui");
    File.WriteAllText(Path.Combine(workspace.FullName, "package.json"), """{"scripts":{"test":"node --test"}}""");
    File.WriteAllText(Path.Combine(server.FullName, "package.json"),
        """{"scripts":{"test;unsafe":"exit 1","test:server":"node --test","lint":"eslint ."}}""");
    File.WriteAllText(Path.Combine(ui.FullName, "package.json"), """{"scripts":{"test:ui":"node --test"}}""");
    var files = new WorkspaceFiles(workspace.FullName);
    var focused = files.FindVerificationPlan([new("packages/server/api.ts", "edit")], default);
    Assert(focused.Complete && focused.Directories.SequenceEqual(["packages/server"]) &&
        focused.Candidates.Count == 2 && focused.Candidates.All(item => item.Directory == "packages/server") &&
        focused.Candidates[0].Arguments.SequenceEqual(["npm", "run", "test:server"]) && focused.Candidates[1].Category == "lint",
        "nearest applicable manifest supplies one safe check per category");
    var combined = files.FindVerificationPlan([new("packages/server/api.ts", "edit"), new("packages/ui/app.ts", "edit")], default);
    Assert(combined.Complete && combined.Directories.SequenceEqual(["packages/server", "packages/ui"]) &&
        combined.Candidates.Select(item => (item.Directory, item.Category)).SequenceEqual([
            ("packages/server", "test"), ("packages/ui", "test"), ("packages/server", "lint")]),
        "each affected manifest gets its own check before secondary checks");
    Directory.CreateDirectory(Path.Combine(workspace.FullName, "packages", "shared"));
    var rootAndChild = files.FindVerificationPlan([new("packages/shared/config.ts", "edit"), new("packages/ui/app.ts", "edit")], default);
    Assert(rootAndChild.Complete && rootAndChild.Directories.SequenceEqual([".", "packages/ui"]) &&
        rootAndChild.Candidates.Select(item => item.Manifest).SequenceEqual(["package.json", "packages/ui/package.json"]),
        "root evidence is required only for edits assigned to the root project");

    var change = new FileChange("packages/server/api.ts", "edit");
    var package = Path.Combine(server.FullName, "package.json");
    File.WriteAllText(package, """{"scripts":{"test":"echo 'Error: No test specified' && exit 0","test-ci:unit":"node --test","test_null":null,"test-empty":" "}}""");
    var parentLock = Path.Combine(workspace.FullName, "pnpm-lock.yaml");
    File.WriteAllText(parentLock, "lockfileVersion: '9.0'\n");
    var inherited = files.FindVerificationPlan([change], default);
    Assert(inherited.Complete && inherited.Candidates.Single().Arguments.SequenceEqual(["pnpm", "run", "test-ci:unit"]),
        "use parent lockfile, skip placeholders and permit explicit test scripts");
    File.WriteAllText(Path.Combine(workspace.FullName, "yarn.lock"), "# conflicting lock\n");
    var ambiguous = files.FindVerificationPlan([change], default);
    Assert(ambiguous.Complete && ambiguous.Candidates.Count == 0, "conflicting locks yield no automatic candidate");
    File.WriteAllText(package, """{"packageManager":"bun@1.2.0","scripts":{"test":"bun test"}}""");
    var unsupported = files.FindVerificationPlan([change], default);
    Assert(unsupported.Complete && unsupported.Candidates.Count == 0, "unsupported explicit manager does not fall back to npm");
    File.WriteAllText(package, """{"packageManager":"Yarn@4.0.0","scripts":{"test:extra":"node --test extra.js","test":"node --test"}}""");
    var declared = files.FindVerificationPlan([change], default);
    Assert(declared.Candidates.Single().Arguments.SequenceEqual(["yarn", "run", "test"]),
        "a supported explicit manager wins and the canonical test script takes priority");

    var go = workspace.CreateSubdirectory("golang");
    var nested = go.CreateSubdirectory("nested");
    File.WriteAllText(Path.Combine(go.FullName, "go.mod"), "module example.test/root\ngo 1.23\n");
    File.WriteAllText(Path.Combine(nested.FullName, "go.mod"), "module example.test/nested\ngo 1.23\n");
    File.WriteAllText(Path.Combine(nested.FullName, "inner_test.go"), "package nested\n");
    var goEdit = new FileChange("golang/main.go", "edit");
    var nestedEdit = new FileChange("golang/nested/main.go", "edit");
    Assert(files.FindVerificationPlan([goEdit], default).Candidates.Count == 0,
        "nested Go module tests do not verify the parent module");
    Assert(files.FindVerificationPlan([nestedEdit], default).Candidates.Single().Arguments.SequenceEqual(["go", "test", "./..."]),
        "Go project gets a candidate when it owns a test file");
    var internalTests = go.CreateSubdirectory("internal");
    File.WriteAllText(Path.Combine(internalTests.FullName, "main_test.go"), "package internal\n");
    Assert(files.FindVerificationPlan([goEdit], default).Candidates.Single().Directory == "golang",
        "descendant tests belong to the nearest module");
}

static void VerificationDiagnosticsChecks()
{
    const string output = """
        src/file.ts(7,13): error TS2322: Invalid string assignment
        src/app.py:2:5: F401 imported but unused
        src/module.py:3: error: Incompatible type [assignment]
        /tmp/project/shared/other.cs(14,9): warning CS0168: Variable unused
          location: '/tmp/project/packages/api/tests/check.cjs:2:1'
        FAILED tests/test_service.py::test_request - mismatch
        credentials.json:1: private
        /etc/sample.py:2: outside
        ../outside.py:3: outside
        """;
    var entries = VerificationDiagnostics.Parse("test", output, "packages/api");
    Assert(entries.Length == 6, "only workspace-local diagnostic paths are accepted");
    Assert(entries[0] == new VerificationDiagnostic("packages/api/src/file.ts", 7, 13, "error", "TS2322", "Invalid string assignment"),
        "TypeScript diagnostic has a scoped path, line, column and code");
    Assert(entries[1].Code == "F401" && entries[2].Code == "assignment" && entries[3].Severity == "warning",
        "lint, mypy and compiler diagnostic classes");
    Assert(entries[4].File == "packages/api/tests/check.cjs" && entries[4].Line == 2 &&
        entries[5].File == "packages/api/tests/test_service.py", "Node TAP and pytest locations");
}

static async Task EditorDraftChecks(DirectoryInfo root)
{
    var file = Path.Combine(root.FullName, "draft.txt");
    await File.WriteAllTextAsync(file, "disk_only");
    await File.WriteAllTextAsync(Path.Combine(root.FullName, "large.txt"), "disk_only");
    var entry = new List<(string Path, string? Text)>
    {
        ("draft.txt", "one\r\ndraft_marker fake-current-model-key\r\n"),
        ("large.txt", new string('x', WorkspaceDrafts.MaxFileBytes + 1)),
    };
    var drafts = new WorkspaceDrafts(entry);
    entry[0] = ("draft.txt", "new draft should not appear");
    var reviewed = false;
    var tools = new WorkspaceFiles(root.FullName, new Review((_, _) =>
    {
        reviewed = true;
        return new(true);
    }), credential: "fake-current-model-key", drafts: drafts);
    var context = tools.InitialContext("Locate draft_marker", default) ?? "";
    Assert(context.Contains("2: draft_marker [redacted]") && context.Contains("large.txt · 超出快照预算") &&
        !context.Contains("fake-current-model-key") && !context.Contains("disk_only"), "initial snapshot redacts and excludes stale disk");
    var read = await tools.ExecuteAsync(new("read", "read_file", """{"path":"DRAFT.txt"}"""), default);
    Assert(read.Text.Contains("2: draft_marker [redacted]") && read.Text.Contains("只读"), "focused read uses the captured draft");
    var found = await tools.ExecuteAsync(new("search", "search_code", """{"query":"draft_marker"}"""), default);
    Assert(found.Text.Contains("draft.txt:2（未保存草稿）"), "search uses the same captured draft");
    var stale = await tools.ExecuteAsync(new("stale", "search_code", """{"query":"disk_only"}"""), default);
    Assert(!stale.Text.Contains("disk_only"), "search skips both dirty disk files");
    await Denied(tools, new("over-budget", "read_file", """{"path":"large.txt"}"""), "editor_draft");
    await Denied(tools, EditCall("apply_patch", "DRAFT.txt", "disk_only", "new data"), "editor_draft");
    await Denied(tools, EditCall("replace_in_file", "draft.txt", "disk_only", "new data"), "editor_draft");
    await Denied(tools, new("write", "write_file", """{"path":"draft.txt","content":"new data"}"""), "editor_draft");
    Assert(!reviewed && File.ReadAllText(file) == "disk_only", "no write reaches patch preview or disk");
}

static void SandboxVerificationChecks()
{
    foreach (var command in new[]
    {
        new[] { "node", "--test", "tests/suite.test.cjs" }, ["python", "-m", "pytest", "-q", "tests/test_app.py::test_case"],
        ["npm", "run", "test:unit"], ["go", "test", "./..."], ["dotnet", "test", "--no-restore"],
        ["ruff", "check", "scidev"], ["tsc", "--noEmit"],
    })
        Assert(SandboxVerification.Classify(command) is not null, "routine verification recognized");
    foreach (var command in new[]
    {
        new[] { "node", "-e", "console.log(1)" }, ["pytest", "../other"], ["pytest", "/tmp/test_file.py"],
        ["pytest", "--override-ini", "option=value"], ["pytest", @"C:\tmp\test.py"],
        ["npm", "run", "publish"], ["npm", "test", "--", "argument"], ["go", "test", "host.invalid/collection?"],
    })
        Assert(SandboxVerification.Classify(command) is null, "other command requires review");
}

static void DesktopTaskQueueChecks(DirectoryInfo directory)
{
    var first = new DesktopTaskQueue.Entry(Guid.NewGuid().ToString("N"), "第一个任务");
    var second = new DesktopTaskQueue.Entry(Guid.NewGuid().ToString("N"), "第二个任务");
    var queue = new DesktopTaskQueue(directory.FullName);
    Assert(queue.Load().Length == 0, "queue starts empty");
    queue.Add(first);
    queue.Add(second);
    var reopened = new DesktopTaskQueue(directory.FullName);
    Assert(reopened.Load().SequenceEqual([first, second]), "queued prompts survive restart in order");
    ExpectQueueDenial(() => reopened.Start(second), "queue_changed");
    var started = first with { Started = true };
    Assert(queue.Start(first).SequenceEqual([started, second]), "claim is durable before execution");
    ExpectQueueDenial(() => reopened.Start(first), "queue_changed");

    var state = Path.Combine(directory.FullName, ".research");
    using (File.Open(Path.Combine(state, "desktop-queue.lock"), FileMode.Open, FileAccess.Read, FileShare.None))
        ExpectQueueDenial(() => reopened.Remove(second), "queue_changed");
    Assert(reopened.Remove(started).SequenceEqual([second]), "a completed claim leaves the rest of the queue");
    Assert(queue.Remove(second).Length == 0 && reopened.Load().Length == 0, "remove persists");

    var file = Path.Combine(state, "desktop-queue.json");
    var withImages = first with { ImageCount = 2 };
    queue.Add(withImages);
    using (var pending = JsonDocument.Parse(File.ReadAllBytes(file)))
        Assert(pending.RootElement.GetProperty("version").GetInt32() == 2 &&
            pending.RootElement.GetProperty("entries")[0].GetProperty("image_count").GetInt32() == 2,
            "queued attachments persist only the count and are not silently run by older builds");
    Assert(reopened.Load().SequenceEqual([withImages]), "attachment requirements survive restart");
    queue.Remove(withImages);
    using (var cleared = JsonDocument.Parse(File.ReadAllBytes(file)))
        Assert(cleared.RootElement.GetProperty("version").GetInt32() == 1, "queues without attachments stay compatible");
    const string unknown = """{"version":3,"entries":[]}""";
    File.WriteAllText(file, unknown);
    ExpectQueueDenial(() => queue.Add(first), "queue_invalid");
    Assert(File.ReadAllText(file) == unknown, "unknown queue format preserved");

    static void ExpectQueueDenial(Action mutate, string status)
    {
        try { mutate(); }
        catch (ToolDeniedException ex) when (ex.Status == status) { return; }
        throw new InvalidOperationException("Expected task queue denial: " + status);
    }
}

static async Task Approved(string directory)
{
    var target = Path.Combine(directory, "existing.txt");
    await File.WriteAllTextAsync(target, "alpha\r\nold\r\n", new UTF8Encoding(true));
    FileEditPreview? shown = null;
    var requests = 0;
    var workspace = new WorkspaceFiles(directory, new Review((preview, _) =>
    {
        shown = preview;
        return new(++requests == 1);
    }));
    var result = await workspace.ExecuteAsync(EditCall("apply_patch", "existing.txt", "old", "new"), default);
    Assert(result.Change?.Operation == "edit" && shown == new FileEditPreview("existing.txt", 2, "old", "new"),
        "exact approved edit");
    await Denied(workspace, EditCall("apply_patch", "existing.txt", "new", "another"), "approval_denied");
    var expected = Encoding.UTF8.Preamble.ToArray().Concat("alpha\r\nnew\r\n"u8.ToArray()).ToArray();
    Assert(requests == 2 && File.ReadAllBytes(target).SequenceEqual(expected), "single use, BOM and CRLF preserved");
}

static async Task ChangedDuringReview(string directory, bool replace)
{
    var target = Path.Combine(directory, "existing.txt");
    await File.WriteAllTextAsync(target, "old value");
    var judge = new Review((_, _) =>
    {
        if (replace)
        {
            File.Move(target, Path.Combine(directory, "original.txt"));
            File.WriteAllText(target, "old value");
        }
        else
            File.WriteAllText(target, "new external value");
        return new(true);
    });
    var workspace = new WorkspaceFiles(directory, judge);
    await Denied(workspace, EditCall("apply_patch", "existing.txt", "old value", "requested value"), "edit_conflict");
    Assert(File.ReadAllText(target) == (replace ? "old value" : "new external value"), "external change preserved");
}

static async Task NewFileChangedExternally(string directory)
{
    var workspace = new WorkspaceFiles(directory);
    var create = new ToolCall("create", "write_file", JsonSerializer.Serialize(new { path = "new.txt", content = "old" }));
    await workspace.ExecuteAsync(create, default);
    File.WriteAllText(Path.Combine(directory, "new.txt"), "external value");
    await Denied(workspace, EditCall("replace_in_file", "new.txt", "external", "model"), "edit_conflict");
    Assert(File.ReadAllText(Path.Combine(directory, "new.txt")) == "external value", "unreviewed edit rejected");
}

static async Task Cancelled(string directory)
{
    var target = Path.Combine(directory, "existing.txt");
    await File.WriteAllTextAsync(target, "old");
    using var cancellation = new CancellationTokenSource();
    var workspace = new WorkspaceFiles(directory, new Review((_, _) =>
    {
        cancellation.Cancel();
        return new(true);
    }));
    try
    {
        await workspace.ExecuteAsync(EditCall("apply_patch", "existing.txt", "old", "new"), cancellation.Token);
        throw new InvalidOperationException("Cancellation was ignored");
    }
    catch (OperationCanceledException) when (cancellation.IsCancellationRequested) { }
    Assert(File.ReadAllText(target) == "old", "cancellation preserved file");
}

static async Task RetryAfterReadLock(string directory)
{
    var target = Path.Combine(directory, "existing.txt");
    await File.WriteAllTextAsync(target, "old");
    Task release = Task.CompletedTask;
    var workspace = new WorkspaceFiles(directory, new Review((_, _) =>
    {
        var locked = File.Open(target, FileMode.Open, FileAccess.Read, FileShare.Read);
        release = Task.Run(async () =>
        {
            using (locked)
                await Task.Delay(80);
        });
        return new(true);
    }));
    try
    {
        await workspace.ExecuteAsync(EditCall("apply_patch", "existing.txt", "old", "new"), default);
        Assert(File.ReadAllText(target) == "new", "retry after a reader releases its handle");
    }
    finally { await release; }
}

static async Task RestoreEditedFile(string directory, bool interveningEdit)
{
    Directory.CreateDirectory(Path.Combine(directory, ".research"));
    var target = Path.Combine(directory, "existing.txt");
    var encoding = new UTF8Encoding(true);
    await File.WriteAllTextAsync(target, "before\r\n", encoding);
    var original = await File.ReadAllBytesAsync(target);
    var checkpoint = new WorkspaceCheckpoint(directory, new string('a', 32));
    var files = new WorkspaceFiles(directory, new Review((_, _) => new(true)), checkpoint);
    await files.ExecuteAsync(EditCall("apply_patch", "existing.txt", "before", "first edit"), default);
    if (interveningEdit)
        await File.WriteAllTextAsync(target, "user correction\r\n", encoding);
    var before = interveningEdit ? "user correction" : "first edit";
    await files.ExecuteAsync(EditCall("apply_patch", "existing.txt", before, "last edit"), default);
    var undo = checkpoint.Restore(default);
    if (interveningEdit)
        Assert(undo.Failed["existing.txt"] == "edit_conflict" && File.ReadAllText(target) == "last edit\r\n",
            "no restoration when it would remove an intervening user edit");
    else
    {
        Assert(undo.Restored.SequenceEqual(["existing.txt"]) && File.ReadAllBytes(target).SequenceEqual(original),
            "restore original bytes before the first approved edit");
        Assert(checkpoint.Restore(default).Restored.Length == 0, "second restoration is a no-op");
    }
}

static ToolCall EditCall(string tool, string path, string oldText, string replacement) =>
    new("test", tool, JsonSerializer.Serialize(new { path, old_text = oldText, new_text = replacement }));

static async Task Denied(WorkspaceFiles workspace, ToolCall call, string status)
{
    try
    {
        await workspace.ExecuteAsync(call, default);
        throw new InvalidOperationException("Expected denial: " + status);
    }
    catch (ToolDeniedException ex) when (ex.Status == status) { }
}

static void Assert(bool condition, string check)
{
    if (!condition)
        throw new InvalidOperationException("Check failed: " + check);
}

static void OperatorDocumentChecks(DirectoryInfo directory)
{
    var settings = Path.Combine(directory.FullName, ".env");
    var sample = Path.Combine(directory.FullName, ".env.example");
    var path = Path.Combine(directory.FullName, "observations.txt");
    File.WriteAllText(settings, "kept private");
    File.WriteAllText(sample, "AN_EXAMPLE=1");
    File.WriteAllText(path, "一\r\n二\n" + new string('x', 700_000), new UTF8Encoding(true));
    var documents = new WorkspaceDocuments(directory.FullName);
    Assert(documents.List(null, default).Select(x => x.Name).SequenceEqual([".env.example", "observations.txt"]),
        "operator tree exclusions");
    var document = documents.Open("observations.txt");
    Assert(document.Text.StartsWith("一\r\n二\n", StringComparison.Ordinal), "editor opens large UTF-8 mixed line endings");
    var saved = documents.Save(document, document.Text.Replace("二", "三", StringComparison.Ordinal), default);
    var bytes = File.ReadAllBytes(path);
    Assert(bytes.AsSpan().StartsWith(Encoding.UTF8.Preamble) &&
        Encoding.UTF8.GetString(bytes.AsSpan(Encoding.UTF8.Preamble.Length)).StartsWith("一\r\n三\n", StringComparison.Ordinal),
        "operator save retains BOM and line history");
    File.WriteAllText(path, "another edit");
    try
    {
        documents.Save(saved, "my edit", default);
        throw new InvalidOperationException("The editor replaced a newer file.");
    }
    catch (ToolDeniedException ex) when (ex.Status == "edit_conflict") { }
    Assert(File.ReadAllText(path) == "another edit", "operator save preserves a newer user's edit");

    var pdf = Path.Combine(directory.FullName, "result.pdf");
    File.WriteAllBytes(pdf, [37, 80, 68, 70, 0, 255]);
    var preview = documents.OpenPreview("result.pdf");
    Assert(preview.Kind == PreviewKind.Pdf && preview.Bytes.SequenceEqual(File.ReadAllBytes(pdf)),
        "preview reads bounded binary files without changing their bytes");
    try
    {
        documents.Open("result.pdf");
        throw new InvalidOperationException("The text editor accepted binary data.");
    }
    catch (ToolDeniedException ex) when (ex.Status == "file_error") { }
}

static void DesktopPreferencesChecks(DirectoryInfo directory)
{
    var state = directory.CreateSubdirectory(".research").FullName;
    var path = Path.Combine(state, "desktop-preferences.json");
    File.WriteAllText(path, """{"version":1,"profile":"focus","qt_layouts":{"focus":[0.19,0.20],"studio":[0.85,0.2]}}""");
    var result = DesktopPreferences.SaveProfile(directory.FullName, "paper", default);
    Assert(result.Profile == "paper" && result.QtLayouts["focus"].CanApply && !result.QtLayouts["studio"].CanApply,
        "theme change preserves independent legacy layout data");
    var read = DesktopPreferences.Load(directory.FullName);
    Assert(read?.Profile == "paper" && read.QtLayouts["studio"].Left == 0.85, "stored preferences read back without layout clamping");
    const string future = """{"version":99,"profile":"paper"}""";
    File.WriteAllText(path, future);
    try
    {
        DesktopPreferences.SaveProfile(directory.FullName, "focus", default);
        throw new InvalidOperationException("Unsupported preference format overwritten.");
    }
    catch (ToolDeniedException ex) when (ex.Status == "preferences_version") { }
    Assert(File.ReadAllText(path) == future, "unsupported preference file is unchanged");
}

sealed class Review(Func<FileEditPreview, CancellationToken, ValueTask<bool>> handle) : IFileEditApproval
{
    public ValueTask<bool> ApproveAsync(FileEditPreview preview, CancellationToken token) => handle(preview, token);
}
