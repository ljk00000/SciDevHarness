using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed record EditorServerCommand(EditorLanguage Language, string Executable, string? EntryScript = null)
{
    public string Name => Language == EditorLanguage.Python ? "Ruff" : "TypeScript Language Server";
}

public sealed record EditorServerReview(EditorServerCommand Command, string Workspace, string ExecutableSha256, string? ScriptSha256);

public sealed partial class EditorLanguageServer
{
    public static async Task<EditorLanguageServer> StartAsync(string workspace, EditorServerCommand command,
        Func<EditorServerReview, CancellationToken, ValueTask<bool>> approve, CancellationToken token)
    {
        workspace = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace));
        WorkspaceNetworkPolicy.RequireAllowed(workspace, "项目断网策略禁止启动本机语言服务。");
        var root = new WindowsWorkspace(workspace);
        using var cwd = root.OpenPath("scidev-editor-lsp-cwd");
        if (!Path.GetExtension(command.Executable).Equals(".exe", StringComparison.OrdinalIgnoreCase) ||
            command.Language == EditorLanguage.Python && command.EntryScript is not null ||
            command.Language == EditorLanguage.TypeScript && command.EntryScript is null ||
            command.Language is not (EditorLanguage.Python or EditorLanguage.TypeScript))
            throw new ArgumentException("语言服务需要 Windows EXE，TypeScript 还需要 JavaScript 入口。");

        using var executable = await InspectedHostExecutable.OpenAsync(command.Executable, token).ConfigureAwait(false);
        using var script = command.EntryScript is null ? null : OpenScript(command.EntryScript);
        using var entry = script is null ? null : WorkspaceFiles.OpenSnapshot(script.AbsolutePath, FileShare.Read, maxBytes: 16 * 1024 * 1024);
        command = command with { Executable = executable.Path, EntryScript = script?.AbsolutePath };
        if (!await approve(new(command, workspace, executable.Sha256, entry?.Stamp.ContentHash), token).ConfigureAwait(false))
            throw new ToolDeniedException("approval_denied", "本机语言服务未获批准。");
        token.ThrowIfCancellationRequested();
        WorkspaceNetworkPolicy.RequireAllowed(workspace, "项目断网策略禁止启动本机语言服务。");
        executable.VerifyUnchanged();
        string[] args = script is null ? ["server"] : [script.AbsolutePath, "--stdio"];
        var system = Environment.SystemDirectory;
        var environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["PATH"] = string.Join(';', system, Path.GetDirectoryName(system), Path.GetDirectoryName(executable.Path)),
            ["NO_COLOR"] = "1",
        };
        var service = new EditorLanguageServer(workspace, root, command.Language,
            LocalStdioProcess.Start(executable.Path, args, workspace, environment));
        try
        {
            await service.InitializeAsync(token).ConfigureAwait(false);
            return service;
        }
        catch
        {
            service.Abort();
            await service.DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    private static WindowsWorkspace.Lease OpenScript(string path)
    {
        if (Path.GetExtension(path).ToLowerInvariant() is not (".mjs" or ".cjs" or ".js"))
            throw new ArgumentException("TypeScript Language Server 入口须为普通 JavaScript 文件。");
        return WindowsWorkspace.OpenAbsoluteFilePath(path);
    }

    public static EditorServerCommand? Discover(string workspace, EditorLanguage language)
    {
        var path = (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries)
            .Select(entry => entry.Trim().Trim('"')).Where(Path.IsPathFullyQualified).Take(80).ToArray();
        if (language == EditorLanguage.Python)
        {
            var ruff = FindFile(path.Prepend(Path.Combine(workspace, ".venv", "Scripts")), "ruff.exe");
            return ruff is null ? null : new(language, ruff);
        }

        var node = FindFile(path, "node.exe");
        var npm = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "npm");
        var entries = path.Prepend(npm).Prepend(workspace);
        var script = FindFile(entries, Path.Combine("node_modules", "typescript-language-server", "lib", "cli.mjs")) ??
            FindFile(entries, Path.Combine("node_modules", "typescript-language-server", "lib", "cli.js"));
        return node is null || script is null ? null : new(language, node, script);
    }

    private static string? FindFile(IEnumerable<string> directories, string name)
    {
        foreach (var directory in directories)
        {
            try
            {
                using var candidate = WindowsWorkspace.OpenAbsoluteFilePath(Path.Combine(directory, name));
                if (WindowsWorkspace.FileExists(candidate.AbsolutePath))
                    return candidate.AbsolutePath;
            }
            catch (Exception ex) when (ex is ArgumentException or IOException or UnauthorizedAccessException or ToolDeniedException) { }
        }
        return null;
    }
}
