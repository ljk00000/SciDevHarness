using System.Globalization;
using System.Text;
using System.Threading.Channels;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Starts an operator-approved Python debugger in a dedicated ConPTY session.</summary>
public static class PythonDebugger
{
    public sealed record Request(string Interpreter, string Path, int Line, bool HasUnsavedChanges);
    public sealed record Review(string Interpreter, string Workspace, string Path, int Line, bool HasUnsavedChanges,
        string InterpreterSha256, string ScriptSha256);

    public static string? FindInterpreter(string workspace)
    {
        var path = (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries)
            .Select(item => item.Trim().Trim('"')).Where(Path.IsPathFullyQualified).Take(80);
        foreach (var directory in path.Prepend(Path.Combine(workspace, "venv", "Scripts"))
            .Prepend(Path.Combine(workspace, ".venv", "Scripts")))
        {
            try
            {
                using var candidate = WindowsWorkspace.OpenAbsoluteFilePath(Path.Combine(directory, "python.exe"));
                if (WindowsWorkspace.FileExists(candidate.AbsolutePath))
                    return candidate.AbsolutePath;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException or ArgumentException) { }
        }
        return null;
    }

    public static async Task<TerminalSession.Result> RunAsync(string workspace, Request request,
        Func<Review, CancellationToken, ValueTask<bool>> approve, int columns, int rows,
        Channel<TerminalSession.Input> terminal, Action<string> output, CancellationToken token)
    {
        var root = Path.GetFullPath(workspace);
        var window = new WindowsWorkspace(root);
        WorkspaceNetworkPolicy.RequireAllowed(root, "项目断网策略禁止启动宿主 Python 调试器。");
        var relative = WorkspaceDocuments.VisiblePath(request.Path);
        if (!relative.EndsWith(".py", StringComparison.OrdinalIgnoreCase) || request.Line < 1)
            throw new ToolDeniedException("debug_target", "请选择工作区内的 Python 源文件。");
        if (!Path.GetExtension(request.Interpreter).Equals(".exe", StringComparison.OrdinalIgnoreCase))
            throw new ToolDeniedException("debug_interpreter", "请选择本机 Windows Python 解释器。");
        var (review, stamp) = await InspectAsync(root, window, relative, request, token).ConfigureAwait(false);
        if (!await approve(review, token).ConfigureAwait(false))
            throw new ToolDeniedException("approval_denied", "本次 Python 调试未获批准。");
        var arguments = Arguments(relative, request.Line);
        var breakpoint = request.Line > 1 ? new StartupBreakpoint(Path.Combine(root, relative.Replace('/', '\\')),
            request.Line, terminal.Writer) : null;
        return await TerminalSession.RunDebuggerAsync(root, columns, rows, terminal.Reader,
            text => { output(text); breakpoint?.Observe(text); }, token,
            async (console, job, directory, startToken) =>
            {
                WorkspaceNetworkPolicy.RequireAllowed(root, "项目断网策略禁止启动宿主 Python 调试器。");
                using var script = window.OpenOperatorPath(relative)!;
                using var approvedScript = WorkspaceFiles.OpenSnapshot(script.AbsolutePath, FileShare.Read, maxBytes: 8 * 1024 * 1024);
                using var interpreter = await InspectedHostExecutable.OpenAsync(review.Interpreter, startToken).ConfigureAwait(false);
                if (approvedScript.Stamp != stamp || interpreter.Sha256 != review.InterpreterSha256)
                    throw new ToolDeniedException("debug_changed", "脚本或解释器在确认后发生改动，请重新启动调试。");
                interpreter.VerifyUnchanged();
                startToken.ThrowIfCancellationRequested();
                return ConPtyNative.StartConsoleProcess(console, job, directory, interpreter.Path,
                    HostCommandProcess.BuildCommandLine(interpreter.Path, arguments));
            }).ConfigureAwait(false);
    }

    private static async Task<(Review Review, WorkspaceFiles.FileStamp Stamp)> InspectAsync(string root,
        WindowsWorkspace window, string relative, Request request, CancellationToken token)
    {
        using var script = window.OpenOperatorPath(relative)!;
        using var snapshot = WorkspaceFiles.OpenSnapshot(script.AbsolutePath, FileShare.Read, maxBytes: 8 * 1024 * 1024);
        var lineCount = 1;
        for (var index = 0; index < snapshot.Bytes.Length; index++)
            if (snapshot.Bytes[index] == '\n' || snapshot.Bytes[index] == '\r' &&
                (index + 1 == snapshot.Bytes.Length || snapshot.Bytes[index + 1] != '\n'))
                lineCount++;
        if (request.Line > lineCount)
            throw new ToolDeniedException("debug_target", "光标行超出磁盘文件范围。保存文件或移动光标后再调试。");
        using var interpreter = await InspectedHostExecutable.OpenAsync(request.Interpreter, token).ConfigureAwait(false);
        return (new(interpreter.Path, root, relative, request.Line, request.HasUnsavedChanges,
            interpreter.Sha256, snapshot.Stamp.ContentHash), snapshot.Stamp);
    }

    internal static string[] Arguments(string relative, int line)
    {
        string[] options = ["-u", "-E", "-X", "utf8", "-m", "pdb"];
        var script = ".\\" + relative.Replace('/', '\\');
        return line == 1 ? [..options, script] :
            [..options, "-c", "break " + line.ToString(CultureInfo.InvariantCulture), script];
    }

    private sealed class StartupBreakpoint(string path, int line, ChannelWriter<TerminalSession.Input> input)
    {
        private readonly string _location = FormattableString.Invariant($" at {path}:{line}");
        private readonly TerminalText _filter = new();
        private readonly StringBuilder _start = new();
        private bool _finished;

        public void Observe(string rawOutput)
        {
            if (_finished)
                return;
            _start.Append(_filter.Read(rawOutput));
            var text = _start.ToString();
            var prompt = text.IndexOf("\n(Pdb) ", StringComparison.Ordinal);
            if (prompt < 0 && _start.Length <= 16_384)
                return;
            _finished = true;
            _start.Clear();
            // The CLI's startup commands run even after an earlier command fails.
            // Continue only after an actual breakpoint is reported before the first prompt.
            if (prompt >= 0 && Confirmed(text[..prompt]))
                input.TryWrite(new TerminalSession.Input.Text("c\r"));
        }

        private bool Confirmed(string output)
        {
            // ConPTY can insert physical line breaks into long source paths.
            const string prefix = "Breakpoint ";
            var flattened = output.Replace("\n", "", StringComparison.Ordinal);
            var start = flattened.IndexOf(prefix, StringComparison.Ordinal);
            if (start < 0)
                return false;
            var number = start + prefix.Length;
            var location = number;
            while (location < flattened.Length && char.IsAsciiDigit(flattened[location]))
                location++;
            var end = location + _location.Length;
            return location > number && flattened.AsSpan(location).StartsWith(_location, StringComparison.OrdinalIgnoreCase) &&
                (end == flattened.Length || !char.IsAsciiDigit(flattened[end]));
        }
    }
}
