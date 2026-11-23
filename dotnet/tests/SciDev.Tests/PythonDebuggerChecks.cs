using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Channels;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class PythonDebuggerChecks
{
    private const string SampleName = "-分析 & debug.py";
    private const string Sample = """
        import os
        assert "SCIDEV_API_KEY" not in os.environ
        def double(value):
            result = value * 2
            return result
        seed = 5
        answer = double(seed)
        print(f"验收:{answer}", flush=True)
        """;

    public static async Task RunAsync(DirectoryInfo fixture)
    {
        var selected = fixture.CreateSubdirectory(Path.Combine(".venv", "Scripts"));
        var preferred = Path.Combine(selected.FullName, "python.exe");
        File.WriteAllBytes(preferred, [0x4D, 0x5A]);
        Assert(PythonDebugger.FindInterpreter(fixture.FullName) == preferred, "workspace venv takes precedence");
        Assert(PythonDebugger.Arguments("pkg/-script.py", 9).SequenceEqual(
            ["-u", "-E", "-X", "utf8", "-m", "pdb", "-c", "break 9", ".\\pkg\\-script.py"]),
            "pdb uses fixed argument boundaries and an unambiguous relative script");

        if (Environment.GetEnvironmentVariable("SCIDEV_TEST_PDB_PYTHON") is not { Length: > 0 } interpreter)
            return;
        var root = fixture.CreateSubdirectory("actual").FullName;
        var sample = Path.Combine(root, SampleName);
        File.WriteAllText(sample, Sample);
        var request = new PythonDebugger.Request(interpreter, SampleName, 7, HasUnsavedChanges: true);
        await RejectionsAsync(root, request, sample);
        using var limit = new CancellationTokenSource(TimeSpan.FromSeconds(25));
        var terminal = new Transcript();
        var input = Channel.CreateUnbounded<TerminalSession.Input>();
        var run = PythonDebugger.RunAsync(root, request, Accept, 110, 28, input, terminal.Append, limit.Token);
        try
        {
            await terminal.ReadThroughAsync("Breakpoint 1 at", limit.Token);
            await terminal.ReadThroughAsync("-> answer = double(seed)", limit.Token);
            await terminal.ReadThroughAsync("(Pdb)", limit.Token);
            input.Writer.TryWrite(new TerminalSession.Input.Resize(132, 35));
            Send(input, "s");
            await terminal.ReadThroughAsync("--Call--", limit.Token);
            await terminal.ReadThroughAsync("-> def double(value):", limit.Token);
            await terminal.ReadThroughAsync("(Pdb)", limit.Token);
            Send(input, "n");
            await terminal.ReadThroughAsync("-> result = value * 2", limit.Token);
            await terminal.ReadThroughAsync("(Pdb)", limit.Token);
            Send(input, "n");
            await terminal.ReadThroughAsync("-> return result", limit.Token);
            await terminal.ReadThroughAsync("(Pdb)", limit.Token);
            Send(input, "r");
            await terminal.ReadThroughAsync("--Return--", limit.Token);
            await terminal.ReadThroughAsync("(Pdb)", limit.Token);
            Send(input, "c");
            await terminal.ReadThroughAsync("验收:10", limit.Token);
            await terminal.ReadThroughAsync("(Pdb)", limit.Token);
            Send(input, "q");
            var exit = await run;
            Assert(exit is { ExitCode: 0, Resized: true } && File.ReadAllText(sample) == Sample,
                "real debugger completed and left the disk contents intact");
        }
        finally
        {
            limit.Cancel();
            try { await run; }
            catch (OperationCanceledException) when (limit.IsCancellationRequested) { }
        }
        await RejectedBreakpointStaysPausedAsync(root, interpreter);
        await CancellationClosesChildAsync(root, interpreter);
        Console.WriteLine("Real Python pdb break, step, next, return, continue, quit and child cleanup passed.");
    }

    private static async Task RejectionsAsync(string root, PythonDebugger.Request request, string file)
    {
        var input = Channel.CreateUnbounded<TerminalSession.Input>();
        Task Run(PythonDebugger.Request desired, Func<PythonDebugger.Review, CancellationToken, ValueTask<bool>> approve) =>
            PythonDebugger.RunAsync(root, desired, approve, 80, 25, input, _ => { }, CancellationToken.None);
        await DeniedAsync(Run(request, (_, _) => new(false)), "approval_denied");
        await DeniedAsync(Run(request with { Path = "../outside.py" }, Accept), "path_denied");
        await DeniedAsync(Run(request with { Line = 500 }, Accept), "debug_target");
        var previous = Environment.GetEnvironmentVariable("SCIDEV_NETWORK_POLICY");
        try
        {
            Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", "deny");
            await DeniedAsync(Run(request, (_, _) => throw new InvalidOperationException("Offline policy should stop before approval.")),
                "network_policy_denied");
        }
        finally { Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", previous); }
        try
        {
            await DeniedAsync(Run(request, (_, _) =>
            {
                File.AppendAllText(file, "\n# change during approval");
                return new(true);
            }), "debug_changed");
        }
        finally { File.WriteAllText(file, Sample); }
    }

    private static async Task RejectedBreakpointStaysPausedAsync(string root, string interpreter)
    {
        const string name = "no-breakpoint.py";
        File.WriteAllText(Path.Combine(root, name), "print('UNEXPECTED_' + 'START')\n# no executable statement\npass\n");
        var input = Channel.CreateUnbounded<TerminalSession.Input>();
        var terminal = new Transcript();
        using var limit = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var run = PythonDebugger.RunAsync(root, new(interpreter, name, 2, false), Accept, 110, 28, input, terminal.Append, limit.Token);
        try
        {
            await terminal.ReadThroughAsync("Blank or comment", limit.Token);
            await terminal.ReadThroughAsync("(Pdb)", limit.Token);
            Send(input, "q");
            Assert((await run).ExitCode == 0 && !terminal.Contains("UNEXPECTED_START"),
                "a rejected breakpoint leaves the script paused without executing top-level code");
        }
        finally
        {
            limit.Cancel();
            try { await run; }
            catch (OperationCanceledException) when (limit.IsCancellationRequested) { }
        }
    }

    private static async Task CancellationClosesChildAsync(string root, string interpreter)
    {
        const string name = "child.py";
        File.WriteAllText(Path.Combine(root, name), """
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            print(f"SCIDEV-PY-CHILD:{child.pid}:READY", flush=True)
            time.sleep(30)
            """);
        var input = Channel.CreateUnbounded<TerminalSession.Input>();
        var terminal = new Transcript();
        using var stop = new CancellationTokenSource(TimeSpan.FromSeconds(18));
        var run = PythonDebugger.RunAsync(root, new(interpreter, name, 1, false), Accept, 110, 28, input, terminal.Append, stop.Token);
        Process? child = null;
        try
        {
            await terminal.ReadThroughAsync("(Pdb)", stop.Token);
            Send(input, "c");
            var output = await terminal.ReadThroughAsync(":READY", stop.Token);
            var marker = Regex.Match(output, @"SCIDEV-PY-CHILD:([0-9]+):READY");
            Assert(marker.Success, "debuggee started its test child");
            child = Process.GetProcessById(int.Parse(marker.Groups[1].Value, CultureInfo.InvariantCulture));
            _ = child.SafeHandle;
        }
        finally
        {
            stop.Cancel();
            try { await run; }
            catch (OperationCanceledException) when (stop.IsCancellationRequested) { }
        }
        using (child)
        using (var wait = new CancellationTokenSource(TimeSpan.FromSeconds(3)))
        {
            await child!.WaitForExitAsync(wait.Token);
            Assert(child.HasExited, "stopping a debugger session reaps the child");
        }
    }

    private static ValueTask<bool> Accept(PythonDebugger.Review review, CancellationToken token) =>
        new(review.InterpreterSha256.Length == 64 && review.ScriptSha256.Length == 64);

    private static async Task DeniedAsync(Task task, string status)
    {
        try { await task; }
        catch (ToolDeniedException ex) when (ex.Status == status) { return; }
        throw new InvalidOperationException($"Expected debugger denial {status}.");
    }

    private static void Send(Channel<TerminalSession.Input> channel, string command) =>
        Assert(channel.Writer.TryWrite(new TerminalSession.Input.Text(command + "\r")), "pdb input was queued");

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }

    private sealed class Transcript
    {
        private readonly TerminalText _filter = new();
        private readonly StringBuilder _received = new();
        private int _cursor;

        public void Append(string terminalOutput)
        {
            lock (_received) _received.Append(_filter.Read(terminalOutput));
        }

        public bool Contains(string text)
        {
            lock (_received) return _received.ToString().Contains(text, StringComparison.Ordinal);
        }

        public async Task<string> ReadThroughAsync(string marker, CancellationToken token)
        {
            try
            {
                while (true)
                {
                    lock (_received)
                    {
                        var text = _received.ToString();
                        var found = text.IndexOf(marker, _cursor, StringComparison.Ordinal);
                        if (found >= 0)
                        {
                            var start = _cursor;
                            _cursor = found + marker.Length;
                            return text[start.._cursor];
                        }
                    }
                    await Task.Delay(25, token);
                }
            }
            catch (OperationCanceledException ex)
            {
                lock (_received) throw new InvalidOperationException($"Did not read {marker}. Terminal: {_received}", ex);
            }
        }
    }
}
