using System.ComponentModel;
using System.Text;
using SciDev.Core;
using static SciDev.Infrastructure.ConPtyNative;

namespace SciDev.Infrastructure;

internal static class HostCommandProcess
{
    internal const int OutputLimit = 8_000;
    internal sealed record Result(int ExitCode, string Output, bool Truncated);

    internal static Task<Result> RunAsync(string executable, string[] arguments, string directory,
        TimeSpan timeout, CancellationToken token, IReadOnlyDictionary<string, string>? extraEnvironment = null) =>
        RunAsync(executable, BuildCommandLine(executable, arguments), directory, timeout, token, extraEnvironment);

    internal static Task<Result> RunShellAsync(string executable, string command, string directory, TimeSpan timeout, CancellationToken token)
    {
        // CMD interprets the remainder of the command line after /c with its own quoting rules.
        var codePageHelper = Path.Combine(Environment.SystemDirectory, "chcp.com");
        var arguments = BuildCommandLine(executable, []).Append(" /d /v:off /c @\"").Append(codePageHelper)
            .Append("\" 65001>nul 2>&1 & ").Append(command);
        return RunAsync(executable, arguments, directory, timeout, token);
    }

    private static async Task<Result> RunAsync(string executable, StringBuilder arguments, string directory,
        TimeSpan timeout, CancellationToken token, IReadOnlyDictionary<string, string>? extraEnvironment = null)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(timeout);
        using var job = NewJob();
        using var pipes = new Pipes();
        deadline.Token.ThrowIfCancellationRequested();
        using var process = StartApprovedProcess(executable, arguments, directory,
            EnvironmentBlock(extraEnvironment), job, pipes);
        pipes.Input.Dispose();
        pipes.Writer.Dispose();
        pipes.Output.Dispose();
        var output = Task.Run(() => ReadOutput(pipes), CancellationToken.None);
        uint exitCode;
        try
        {
            while (true)
            {
                deadline.Token.ThrowIfCancellationRequested();
                var state = WaitForSingleObject(process, 0);
                if (state == 0)
                    break;
                if (state != 258)
                    throw new Win32Exception();
                await Task.Delay(30, deadline.Token);
            }
            if (!GetExitCodeProcess(process, out exitCode))
                throw new Win32Exception();
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        {
            throw new ToolDeniedException("host_command_timeout", "宿主命令超过时间上限，已结束本次进程树。");
        }
        finally
        {
            job.Dispose();
            await output.WaitAsync(TimeSpan.FromSeconds(5), CancellationToken.None);
        }
        var (text, truncated) = await output;
        return new(unchecked((int)exitCode), text, truncated);
    }

    internal static StringBuilder BuildCommandLine(string program, IEnumerable<string> args)
    {
        var command = new StringBuilder();
        foreach (var value in args.Prepend(program))
        {
            if (command.Length != 0)
                command.Append(' ');
            command.Append('"');
            var slashes = 0;
            foreach (var character in value)
            {
                if (character == '\\')
                {
                    slashes++;
                    continue;
                }
                command.Append('\\', character == '"' ? 2 * slashes + 1 : slashes).Append(character);
                slashes = 0;
            }
            command.Append('\\', 2 * slashes).Append('"');
        }
        return command;
    }

    internal static string EnvironmentBlock(IReadOnlyDictionary<string, string>? extra = null)
    {
        var system = Environment.SystemDirectory;
        var windows = Path.GetDirectoryName(system)!;
        var programs = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        var variables = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["SystemRoot"] = windows, ["WINDIR"] = windows, ["ComSpec"] = Path.Combine(system, "cmd.exe"),
            ["PATH"] = string.Join(';', system, windows, Path.Combine(programs, "Git", "cmd"), Path.Combine(programs, "dotnet")),
            ["PATHEXT"] = ".COM;.EXE;.BAT;.CMD",
        };
        foreach (var key in new[] { "TEMP", "TMP", "ProgramFiles", "ProgramData" })
            if (Environment.GetEnvironmentVariable(key) is { } value)
                variables[key] = value;
        if (extra is not null)
            foreach (var (key, value) in extra)
                variables[key] = value;
        return string.Join('\0', variables.Select(item => $"{item.Key}={item.Value}")) + "\0\0";
    }

    private static (string Text, bool Truncated) ReadOutput(Pipes pipes)
    {
        using var stream = new FileStream(pipes.Reader, FileAccess.Read);
        using var reader = new StreamReader(stream, Encoding.UTF8);
        var output = new StringBuilder();
        var buffer = new char[4096];
        var truncated = false;
        int count;
        while ((count = reader.Read(buffer, 0, buffer.Length)) > 0)
        {
            var take = Math.Min(count, OutputLimit - output.Length);
            output.Append(buffer, 0, take);
            truncated |= take < count;
        }
        if (output.Length > 0 && char.IsHighSurrogate(output[^1]))
            output.Length--;
        return (output.ToString(), truncated);
    }
}
