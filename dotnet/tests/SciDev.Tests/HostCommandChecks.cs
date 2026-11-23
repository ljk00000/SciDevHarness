using System.Diagnostics;
using System.Text;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class HostCommandChecks
{
    private const string FixtureKey = "host-command-fixture-current-api-key";
    private static readonly Uri Endpoint = new("http://127.0.0.1:1234/v1/chat/completions");
    private static string Executable => Environment.ProcessPath ?? throw new InvalidOperationException();

    private static string[] Arguments(params string[] args) => Path.GetFileNameWithoutExtension(Executable).Equals("dotnet", StringComparison.OrdinalIgnoreCase)
        ? [typeof(HostCommandChecks).Assembly.Location, "--host-command-fixture", ..args] : ["--host-command-fixture", ..args];

    internal static async Task FixtureAsync(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (args is ["child"])
        {
            await Task.Delay(TimeSpan.FromMinutes(1));
            return;
        }
        if (args is ["hold"])
        {
            var start = new ProcessStartInfo(Executable) { UseShellExecute = false, CreateNoWindow = true };
            foreach (var value in Arguments("child"))
                start.ArgumentList.Add(value);
            using var child = Process.Start(start)!;
            File.WriteAllText("processes.pending", $"{Environment.ProcessId}\n{child.Id}\n");
            File.Move("processes.pending", "processes.txt");
            await Task.Delay(TimeSpan.FromMinutes(1));
            return;
        }
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            args, directory = Path.GetFileName(Directory.GetCurrentDirectory()),
            keyAbsent = Environment.GetEnvironmentVariable("SCIDEV_API_KEY") is null,
        }));
        Console.Error.WriteLine("synthetic host stderr");
    }

    internal static async Task RunAsync(DirectoryInfo fixture)
    {
        var scope = fixture.CreateSubdirectory("current directory");
        var audit = new List<RunEvent>();
        var key = Environment.GetEnvironmentVariable("SCIDEV_API_KEY");
        Environment.SetEnvironmentVariable("SCIDEV_API_KEY", FixtureKey);
        try
        {
            ApprovedHostCommands Tool(Reviewer? review) => new(fixture.FullName, new WorkspaceFiles(fixture.FullName),
                review, Endpoint, audit.Add, FixtureKey);
            ToolCall Request(params string[] args) => new("test-host", "run_host_command", JsonSerializer.Serialize(new
            {
                executable = Executable, argv = Arguments(args), directory = scope.Name,
            }));

            await ExpectAsync(Tool(null).ExecuteAsync(Request("echo"), default).AsTask(), "approval_denied");
            Assert(audit.Count == 0, "disabled host tool must do no work");
            var closeFirst = new Reviewer(_ => false);
            await ExpectAsync(Tool(closeFirst).ExecuteAsync(Request("echo"), default).AsTask(), "approval_denied");
            Assert(closeFirst.Requests.Single().Kind == OperationKind.InspectHostCommand && audit.All(item => item.Type != "host_command_starting"),
                "refusing initial inspection cannot launch a process");
            var closeLaunch = new Reviewer(request => request.Kind != OperationKind.RunHostCommand);
            await ExpectAsync(Tool(closeLaunch).ExecuteAsync(Request("echo"), default).AsTask(), "approval_denied");
            Assert(closeLaunch.Requests.Count == 2 && audit.All(item => item.Type != "host_command_starting"),
                "approving inspection alone cannot launch a process");

            var closeOutput = new Reviewer(request => request.Kind != OperationKind.ShareHostCommandOutput);
            await ExpectAsync(Tool(closeOutput).ExecuteAsync(Request("never-log-this"), default).AsTask(), "host_output_not_shared");
            Assert(audit.Count(item => item.Type == "host_command_exited") == 1 &&
                !JsonSerializer.Serialize(audit).Contains("never-log-this"), "never journal host arguments or output");

            var approve = new Reviewer(_ => true);
            var result = await Tool(approve).ExecuteAsync(Request("", "a value with spaces", "quote\"embedded", "trailing\\", "中文"), default);
            Assert(approve.Requests.Select(item => item.Kind).SequenceEqual([OperationKind.InspectHostCommand,
                OperationKind.RunHostCommand, OperationKind.ShareHostCommandOutput]), "a separate approval is needed for each host operation");
            Assert(result.CommandExitCode == 0 && result.ExternalDataShared && result.Text == approve.Requests[^1].Before &&
                approve.Requests[^1].Destination == Endpoint.AbsoluteUri && result.Text.Contains("synthetic host stderr"),
                "successful native execution returns only the reviewed preview and direct exit status");
            using var output = JsonDocument.Parse(result.Text.Split('\n').Single(line => line.StartsWith('{')));
            Assert(output.RootElement.GetProperty("keyAbsent").GetBoolean() && output.RootElement.GetProperty("directory").GetString() == scope.Name &&
                output.RootElement.GetProperty("args").EnumerateArray().Select(arg => arg.GetString()).SequenceEqual(
                    ["", "a value with spaces", "quote\"embedded", "trailing\\", "中文"]), "approved argv is passed exactly and model credential is omitted");

            var rejectCredential = new Reviewer(_ => true);
            await ExpectAsync(Tool(rejectCredential).ExecuteAsync(Request(FixtureKey), default).AsTask(), "host_command_credential");
            Assert(rejectCredential.Requests.Count == 0 && !JsonSerializer.Serialize(audit).Contains(FixtureKey), "request arguments must not contain the model credential");

            await ExpectAsync(HostCommandProcess.RunAsync(Executable, Arguments("child"), scope.FullName,
                TimeSpan.FromMilliseconds(300), default), "host_command_timeout");

            using var stop = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            var running = Tool(new Reviewer(_ => true)).ExecuteAsync(Request("hold"), stop.Token).AsTask();
            var signal = Path.Combine(scope.FullName, "processes.txt");
            while (!File.Exists(signal) && !running.IsCompleted)
                await Task.WhenAny(running, Task.Delay(30, stop.Token));
            if (running.IsCompleted)
            {
                await running;
                throw new InvalidOperationException("Process fixture exited before it could be cancelled.");
            }
            stop.Cancel();
            try { await running; throw new InvalidOperationException("Expected cancellation."); }
            catch (OperationCanceledException) when (stop.IsCancellationRequested) { }
            var pids = File.ReadAllLines(signal).Select(int.Parse).ToArray();
            Assert(pids.Length == 2, "the parent and the child both started");
            foreach (var pid in pids)
            {
                try
                {
                    using var process = Process.GetProcessById(pid);
                    Assert(process.WaitForExit(2_000), "the job must remove every process in the host command tree");
                }
                catch (ArgumentException) { }
            }
        }
        finally { Environment.SetEnvironmentVariable("SCIDEV_API_KEY", key); }
        await LegacyShellAsync(fixture, scope);
    }

    private static async Task LegacyShellAsync(DirectoryInfo fixture, DirectoryInfo scope)
    {
        var audit = new List<RunEvent>();
        ApprovedHostCommands Tool(Reviewer? review, bool denyNetwork = false) => new(fixture.FullName, new WorkspaceFiles(fixture.FullName),
            review, Endpoint, audit.Add, FixtureKey, denyExtraNetwork: denyNetwork, legacyWorkspaceApproval: true);
        ToolCall Request(string command, bool outside = true) => new("legacy-host", "run_command", JsonSerializer.Serialize(new
        {
            command, outside_workspace = outside, directory = scope.Name, timeout_seconds = 15,
        }));

        const string shell = "echo \"legacy shell output\">legacy-host.txt & type legacy-host.txt";
        var path = Path.Combine(scope.FullName, "legacy-host.txt");
        var refuse = new Reviewer(request => request.Kind != OperationKind.RunHostCommand);
        await ExpectAsync(Tool(null).ExecuteAsync(Request(shell), default).AsTask(), "host_approval_required");
        await ExpectAsync(Tool(refuse, denyNetwork: true).ExecuteAsync(Request(shell), default).AsTask(), "network_policy_denied");
        await ExpectAsync(Tool(refuse).ExecuteAsync(Request("GIT -C ./ checkout main"), default).AsTask(), "host_command_blocked");
        await ExpectAsync(Tool(refuse).ExecuteAsync(Request($"echo {FixtureKey}"), default).AsTask(), "host_command_credential");
        Assert(audit.Count == 0 && refuse.Requests.Count == 0, "unavailable and blocked requests do not reach review or launch");
        await ExpectAsync(Tool(refuse).ExecuteAsync(Request(shell), default).AsTask(), "approval_denied");
        Assert(refuse.Requests.Count == 2 && refuse.Requests[^1].Before == shell && !File.Exists(path),
            "host execution requires approval of the exact shell text");

        var doNotShare = new Reviewer(request => request.Kind != OperationKind.ShareHostCommandOutput);
        await ExpectAsync(Tool(doNotShare).ExecuteAsync(Request(shell), default).AsTask(), "host_output_not_shared");
        Assert(doNotShare.Requests.Count == 3 && File.ReadAllText(path).Trim() == "\"legacy shell output\"" &&
            doNotShare.Requests[^1].Before?.Contains("\"legacy shell output\"") == true &&
            !JsonSerializer.Serialize(audit).Contains("legacy shell output"), "shell output requires separate sharing and stays out of audit");

        var approve = new Reviewer(_ => true);
        var result = await Tool(approve).ExecuteAsync(Request("echo legacy-approved"), default);
        Assert(result is { CommandExitCode: 0, ExternalDataShared: true, Verification: null } &&
            result.Text.Contains("legacy-approved") && approve.Requests.Count == 3, "legacy host output is never workspace verification");
        await ExpectAsync(Tool(approve).ExecuteAsync(Request(shell, outside: false), default).AsTask(), "unsupported_tool");
        Assert(approve.Requests.Count == 3, "in-workspace commands never route to the host");

        ApprovedHostCommands Local(Reviewer? review, bool denyNetwork = false) => new(fixture.FullName, new WorkspaceFiles(fixture.FullName),
            review, Endpoint, audit.Add, FixtureKey, denyExtraNetwork: denyNetwork, legacyLocalCommands: true);
        var localRequest = Request("echo local-full-access", outside: false);
        var localReview = new Reviewer(_ => true);
        await ExpectAsync(Local(null).ExecuteAsync(localRequest, default).AsTask(), "host_approval_required");
        await ExpectAsync(Local(localReview, denyNetwork: true).ExecuteAsync(localRequest, default).AsTask(), "network_policy_denied");
        await ExpectAsync(Local(localReview).ExecuteAsync(Request("echo invalid-scope"), default).AsTask(), "host_approval_required");
        Assert(localReview.Requests.Count == 0, "unavailable legacy full-access requests show no host reviews and run nothing");
        var localResult = await Local(localReview).ExecuteAsync(localRequest, default);
        Assert(localResult is { CommandExitCode: 0, ExternalDataShared: true, Verification: null } &&
            localResult.Text.Contains("local-full-access") && localReview.Requests.Count == 3,
            "explicit legacy local mode retains all three host reviews");
    }

    private static async Task ExpectAsync(Task task, string status)
    {
        try { await task; }
        catch (ToolDeniedException error) when (error.Status == status) { return; }
        throw new InvalidOperationException("Expected " + status);
    }

    private sealed class Reviewer(Func<OperationReview, bool> decide) : IOperationReview
    {
        public List<OperationReview> Requests { get; } = [];
        public ValueTask<bool> ApproveAsync(OperationReview operation, CancellationToken token)
        {
            Requests.Add(operation);
            return new(decide(operation));
        }
    }

    private static void Assert(bool value, string message)
    {
        if (!value)
            throw new InvalidOperationException(message);
    }
}
