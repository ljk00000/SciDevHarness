using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>File browsing and text edits explicitly initiated by the desktop user.</summary>
public sealed class WorkspaceDocuments(string workspace)
{
    private const int MaxEntries = 4_096;
    private const int MaxTextBytes = 8 * 1024 * 1024;
    private static readonly HashSet<string> ProtectedNames = new(StringComparer.OrdinalIgnoreCase)
    {
        ".git", ".research", ".agents", ".codex", ".tmp-tests", ".pytest_cache", "__pycache__", "node_modules",
        ".venv", "venv", ".aws", ".azure", ".env", ".envrc", ".git-credentials", ".netrc", ".npmrc", ".pypirc", ".ssh",
        "credentials", "credentials.json", "id_ed25519", "id_rsa", "secrets", "secrets.json",
    };
    private static readonly string[] SafeEnvSuffixes = [".example", ".sample", ".template", ".dist"];
    private readonly WindowsWorkspace _workspace = new(workspace);

    public sealed record Entry(string Name, string RelativePath, bool IsDirectory);
    public sealed record FileSearch(Entry[] Matches, bool Limited);

    public sealed class Document
    {
        internal Document(WorkspaceDocuments owner, string path, string text, bool bom, WorkspaceFiles.FileStamp stamp) =>
            (Owner, Path, Text, HasBom, Stamp) = (owner, path, text, bom, stamp);

        internal WorkspaceDocuments Owner { get; }
        internal bool HasBom { get; }
        internal WorkspaceFiles.FileStamp Stamp { get; }
        public string Path { get; }
        public string Text { get; }
    }

    public Entry[] List(string? directory, CancellationToken token) => List(_workspace, directory, token);

    internal static Entry[] List(WindowsWorkspace workspace, string? directory, CancellationToken token)
    {
        var parent = directory is null ? "" : VisiblePath(directory) + "/";
        using var lease = workspace.OpenOperatorPath(parent + "scidev-list")!;
        var folder = new DirectoryInfo(Path.GetDirectoryName(lease.AbsolutePath)!);
        var result = new List<Entry>();
        var count = 0;
        foreach (var item in folder.EnumerateFileSystemInfos())
        {
            token.ThrowIfCancellationRequested();
            if (++count > MaxEntries)
                throw new ToolDeniedException("directory_limit", "此目录条目过多，请选择更小的工作区。");
            var path = parent + item.Name;
            if ((item.Attributes & (FileAttributes.ReparsePoint | FileAttributes.Device)) == 0 && IsVisible(path))
                result.Add(new(item.Name, path, item is DirectoryInfo));
        }
        return result.OrderByDescending(x => x.IsDirectory).ThenBy(x => x.Name, StringComparer.OrdinalIgnoreCase).ToArray();
    }

    public Document Open(string relative)
    {
        using var target = _workspace.OpenOperatorPath(VisiblePath(relative))!;
        using var input = WorkspaceFiles.OpenSnapshot(target.AbsolutePath, FileShare.Read, maxBytes: MaxTextBytes);
        var bom = input.Bytes.AsSpan().StartsWith(Encoding.UTF8.Preamble);
        var text = WorkspaceFiles.Utf8.GetString(input.Bytes.AsSpan(bom ? Encoding.UTF8.Preamble.Length : 0));
        return new(this, target.RelativePath, text, bom, input.Stamp);
    }

    public Document Create(string relative, CancellationToken token)
    {
        using var target = _workspace.OpenOperatorPath(VisiblePath(relative))!;
        var stamp = WorkspaceFiles.Save(target.AbsolutePath, [], token);
        return new(this, target.RelativePath, "", false, stamp);
    }

    public FileSearch FindFiles(string query, CancellationToken token)
    {
        const int maxMatches = 100, maxScanned = 20_000;
        query = query.Trim().Replace('\\', '/');
        if (query.Length == 0)
            return new([], false);
        var folders = new Stack<string?>();
        folders.Push(null);
        var matches = new List<Entry>();
        var scanned = 0;
        while (folders.TryPop(out var folder))
        {
            foreach (var entry in List(folder, token))
            {
                if (++scanned > maxScanned || matches.Count == maxMatches)
                    return new(matches.ToArray(), true);
                if (entry.IsDirectory)
                    folders.Push(entry.RelativePath);
                else if (entry.RelativePath.Contains(query, StringComparison.OrdinalIgnoreCase))
                    matches.Add(entry);
            }
        }
        return new(matches.ToArray(), false);
    }

    public PreviewDocument OpenPreview(string relative, bool asText = false)
    {
        var kind = asText ? PreviewKind.Text : PreviewDocument.KindOf(relative);
        var limit = kind switch
        {
            PreviewKind.Pdf => 64 * 1024 * 1024,
            PreviewKind.Image => 24 * 1024 * 1024,
            PreviewKind.Markdown or PreviewKind.Text => 1024 * 1024,
            _ => throw new ToolDeniedException("preview_unsupported", "此文件类型没有文档预览。"),
        };
        using var target = _workspace.OpenOperatorPath(VisiblePath(relative))!;
        using var snapshot = WorkspaceFiles.OpenSnapshot(target.AbsolutePath, FileShare.Read, maxBytes: limit, allowBinary: true);
        return new(target.RelativePath, kind, snapshot.Bytes);
    }

    public Document Save(Document original, string text, CancellationToken token)
    {
        if (!ReferenceEquals(original.Owner, this))
            throw new ArgumentException("文档不属于当前工作区。");
        if (text == original.Text)
            return original;
        var prefix = original.HasBom ? Encoding.UTF8.Preamble : ReadOnlySpan<byte>.Empty;
        var length = WorkspaceFiles.Utf8.GetByteCount(text);
        if (length + prefix.Length > MaxTextBytes || text.Contains('\0'))
            throw new ToolDeniedException("file_error", "编辑器只保存 8 MiB 以内的 UTF-8 文本。");
        var output = new byte[length + prefix.Length];
        prefix.CopyTo(output);
        WorkspaceFiles.Utf8.GetBytes(text, output.AsSpan(prefix.Length));
        using var target = _workspace.OpenOperatorPath(original.Path)!;
        var stamp = WorkspaceFiles.Save(target.AbsolutePath, output, token, original.Stamp, maxCurrentBytes: MaxTextBytes);
        return new(this, original.Path, text, original.HasBom, stamp);
    }

    internal static bool IsVisible(string path)
    {
        try { VisiblePath(path); return true; }
        catch (ToolDeniedException) { return false; }
    }

    internal static string VisiblePath(string path) => VisiblePath(path, allowHidden: true);

    internal static string VisiblePath(string path, bool allowHidden)
    {
        var relative = WindowsWorkspace.NormalizeOperatorPath(path);
        if (relative.Split('/').Any(name => IsProtected(name) || (!allowHidden && name[0] == '.')))
            throw new ToolDeniedException("path_denied", "工作区内部状态和凭据路径不可在工作台中打开。");
        return relative;
    }

    private static bool IsProtected(string name) => ProtectedNames.Contains(name) ||
        (name.StartsWith(".env.", StringComparison.OrdinalIgnoreCase) &&
         !SafeEnvSuffixes.Any(suffix => name.EndsWith(suffix, StringComparison.OrdinalIgnoreCase))) ||
        name.EndsWith(".pem", StringComparison.OrdinalIgnoreCase) ||
        name.EndsWith(".pfx", StringComparison.OrdinalIgnoreCase) ||
        name.EndsWith(".key", StringComparison.OrdinalIgnoreCase);
}
