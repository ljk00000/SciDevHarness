using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Read bounded local workspace metadata without running project commands.</summary>
public sealed class WorkspaceDoctor(string path)
{
    private readonly WindowsWorkspace _workspace = new(path);

    public sealed record GitInfo(string Status, string? Branch = null, string? Head = null);

    public GitInfo ReadGit()
    {
        try { _ = GitProcess.RequireExecutable(); }
        catch (ToolDeniedException) { return new("missing"); }

        using var marker = _workspace.OpenOperatorPath(".git")!;
        FileAttributes attributes;
        try { attributes = File.GetAttributes(marker.AbsolutePath); }
        catch (FileNotFoundException) { return new("not_repository"); }
        if ((attributes & FileAttributes.ReparsePoint) != 0)
            return new("unavailable");
        if ((attributes & FileAttributes.Directory) == 0)
            return new("linked");

        var head = Read(".git/HEAD", 1024);
        if (IsSha(head))
            return new("ready", Head: head);
        const string prefix = "ref: refs/heads/";
        if (head is null || !head.StartsWith(prefix, StringComparison.Ordinal) || head.Length is <= 16 or > 200 || head.Any(char.IsControl))
            return new("unavailable");

        var reference = head["ref: ".Length..];
        var branch = head[prefix.Length..];
        var loose = Read(".git/" + reference, 1024);
        if (IsSha(loose))
            return new("ready", branch, loose);
        var packed = Read(".git/packed-refs", 1024 * 1024);
        if (packed is not null)
            foreach (var line in packed.Split('\n'))
            {
                var separator = line.IndexOf(' ');
                if (separator is not (40 or 64) || !line.AsSpan(separator + 1).TrimEnd().SequenceEqual(reference))
                    continue;
                var sha = line[..separator];
                if (IsSha(sha))
                    return new("ready", branch, sha);
            }
        return new("ready", branch);
    }

    public VerificationPlan ReadVerificationPlan(CancellationToken token) =>
        new WorkspaceDiscovery(_workspace, credential: null, WorkspaceDrafts.Empty, WorkspaceGitIndex.Read(_workspace, token))
            .InspectVerificationPlan(token);

    public string[] FindLanguageTools()
    {
        var directories = new[] { Path.Combine(path, ".venv", "Scripts") }.Concat(
            (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries)
                .Select(item => item.Trim().Trim('"')).Where(Path.IsPathFullyQualified).Take(80));
        return new[] { "ruff", "pyright", "typescript-language-server" }
            .Where(name => directories.Any(directory => Exists(Path.Combine(directory, name + ".exe")) ||
                Exists(Path.Combine(directory, name + ".cmd")))).ToArray();
    }

    private static bool Exists(string path)
    {
        try
        {
            using var target = WindowsWorkspace.OpenAbsoluteFilePath(path);
            return WindowsWorkspace.FileExists(target.AbsolutePath);
        }
        catch (Exception ex) when (ex is ArgumentException or IOException or UnauthorizedAccessException or ToolDeniedException) { return false; }
    }

    private string? Read(string relative, int limit)
    {
        using var target = _workspace.OpenOperatorPath(relative, allowMissingParents: true);
        if (target is null || !WindowsWorkspace.FileExists(target.AbsolutePath))
            return null;
        using var snapshot = WorkspaceFiles.OpenSnapshot(target.AbsolutePath, FileShare.Read, maxBytes: limit);
        try { return WorkspaceFiles.Utf8.GetString(snapshot.Bytes).TrimEnd('\r', '\n'); }
        catch (DecoderFallbackException) { return null; }
    }

    private static bool IsSha(string? value) => value is { Length: 40 or 64 } && value.All(char.IsAsciiHexDigitLower);
}
