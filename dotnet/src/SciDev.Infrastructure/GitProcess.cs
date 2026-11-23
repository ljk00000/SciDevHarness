using System.Diagnostics;
using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class GitProcess
{
    internal static string RequireExecutable()
    {
        var git = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Git", "cmd", "git.exe");
        if (!File.Exists(git))
            throw new ToolDeniedException("git_cli_missing", "未找到 Git for Windows。");
        return git;
    }

    internal static ProcessStartInfo Prepare(string git, string workingDirectory, string emptyConfig)
    {
        var start = new ProcessStartInfo(git)
        {
            WorkingDirectory = workingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        start.Environment.Clear();
        foreach (var key in new[] { "SystemRoot", "WINDIR", "TEMP", "TMP" })
            if (Environment.GetEnvironmentVariable(key) is { } value)
                start.Environment[key] = value;
        start.Environment["GIT_CONFIG_GLOBAL"] = emptyConfig;
        start.Environment["GIT_CONFIG_NOSYSTEM"] = "1";
        start.Environment["GIT_ATTR_NOSYSTEM"] = "1";
        start.Environment["GIT_OPTIONAL_LOCKS"] = "0";
        start.Environment["GIT_NO_LAZY_FETCH"] = "1";
        start.Environment["GIT_ALLOW_PROTOCOL"] = "";
        start.Environment["GIT_TERMINAL_PROMPT"] = "0";
        return start;
    }

    internal static async Task<GitReadContext.Output> RunAsync(ProcessStartInfo start, IReadOnlyList<string> args,
        CancellationToken token, int maxChars = 4096, string? input = null, int timeoutSeconds = 10)
    {
        if (input is not null)
        {
            start.RedirectStandardInput = true;
            start.StandardInputEncoding = new UTF8Encoding(false);
        }
        start.ArgumentList.Add("--no-pager");
        foreach (var arg in args)
            start.ArgumentList.Add(arg);

        token.ThrowIfCancellationRequested();
        using var process = Process.Start(start) ?? throw new IOException("无法启动 Git。");
        var stdout = ReadBoundedAsync(process.StandardOutput, maxChars);
        var stderr = ReadBoundedAsync(process.StandardError, 16_384);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(timeoutSeconds));
        try
        {
            if (input is not null)
            {
                await process.StandardInput.WriteAsync(input.AsMemory(), deadline.Token);
                process.StandardInput.Close();
            }
            await process.WaitForExitAsync(deadline.Token);
            var output = await stdout;
            await stderr;
            return new(process.ExitCode, output.Text, output.Truncated);
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        {
            throw new ToolDeniedException("git_timeout", "Git 操作超时，请刷新并核对仓库状态。");
        }
        finally
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync(CancellationToken.None);
            }
        }
    }

    private static async Task<(string Text, bool Truncated)> ReadBoundedAsync(StreamReader reader, int limit)
    {
        var output = new StringBuilder();
        var buffer = new char[4096];
        var truncated = false;
        int count;
        while ((count = await reader.ReadAsync(buffer)) != 0)
        {
            var take = Math.Min(count, limit - output.Length);
            output.Append(buffer, 0, take);
            truncated |= take < count;
        }
        return (output.ToString(), truncated);
    }
}
