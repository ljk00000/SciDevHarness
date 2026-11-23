using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Immutable editor state captured on the UI thread when a task starts.</summary>
public sealed class WorkspaceDrafts
{
    public const int MaxContentFiles = 8;
    public const int MaxFileBytes = 256 * 1024;
    private const int MaxTotalBytes = 1_000_000;
    internal sealed record Draft(string? Text, int Bytes);
    internal static readonly WorkspaceDrafts Empty = new([]);
    private readonly Dictionary<string, Draft> _files = new(StringComparer.OrdinalIgnoreCase);

    public WorkspaceDrafts(IEnumerable<(string Path, string? Text)> files)
    {
        var content = 0;
        var remaining = MaxTotalBytes;
        foreach (var (rawPath, text) in files)
        {
            var path = WorkspaceDocuments.VisiblePath(rawPath);
            if (!_files.TryAdd(path, new(null, 0)))
                continue;
            if (content >= MaxContentFiles || text is null || text.Length > MaxFileBytes || text.Contains('\0') ||
                path.Split('/').Any(part => part[0] == '.'))
                continue;
            int bytes;
            try { bytes = WorkspaceFiles.Utf8.GetByteCount(text); }
            catch (EncoderFallbackException) { continue; }
            if (bytes > MaxFileBytes || bytes > remaining)
                continue;
            _files[path] = new(text, bytes);
            remaining -= bytes;
            content++;
        }
        ContentCount = content;
    }

    internal int ContentCount { get; }
    internal int Count => _files.Count;
    internal IEnumerable<string> VisiblePaths => _files.Keys.Where(path => !path.Split('/').Any(part => part[0] == '.'));
    internal Draft? Get(string path) => _files.GetValueOrDefault(path);
    internal void RequireWritable(string path)
    {
        if (_files.ContainsKey(path))
            throw new ToolDeniedException("editor_draft", "此文件有未保存的编辑，任务内只读。保存后可重新提交修改。");
    }
}
