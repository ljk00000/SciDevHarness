using System.Security.Cryptography;
using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Bounded snapshot used for disposable commands and evaluation templates.</summary>
internal sealed class WorkspaceSnapshot(string workspace, string? credential = null, WorkspaceDrafts? drafts = null)
{
    private const int MaxFiles = 4096;
    private const int MaxFileBytes = 16 * 1024 * 1024;
    private const long MaxSnapshotBytes = 64L * 1024 * 1024;
    private readonly string _root = Path.GetFullPath(workspace);
    private readonly WindowsWorkspace _workspace = new(workspace);
    private readonly byte[] _credential = string.IsNullOrEmpty(credential) ? [] : Encoding.UTF8.GetBytes(credential);

    public string Capture(string? destination, CancellationToken token)
    {
        using var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var remaining = MaxSnapshotBytes;
        var entries = 0;
        var index = WorkspaceGitIndex.Read(_workspace, token);
        var rules = WorkspaceIgnore.ReadRepositoryExcludes(_workspace);
        CopyDirectory("", destination, digest, ref remaining, ref entries, index, rules, trackedOnly: false, token);
        return Convert.ToHexString(digest.GetHashAndReset());
    }

    private void CopyDirectory(string relative, string? destination, IncrementalHash digest, ref long remaining, ref int entries,
        WorkspaceGitIndex? index, WorkspaceIgnore.Rule[] inherited, bool trackedOnly, CancellationToken token)
    {
        // This lease pins the enumerated directory and each of its parents.
        using var source = _workspace.OpenPath(relative.Length == 0 ? "scidev-directory" : relative + "/scidev-directory");
        var folder = new DirectoryInfo(relative.Length == 0 ? _root : Path.GetDirectoryName(source.AbsolutePath)!);
        var rules = trackedOnly ? inherited : WorkspaceIgnore.Read(_workspace, relative.Length == 0 ? null : relative, inherited);
        foreach (var entry in folder.EnumerateFileSystemInfos().Take(MaxFiles + 1).OrderBy(entry => entry.Name, StringComparer.Ordinal))
        {
            token.ThrowIfCancellationRequested();
            if (++entries > MaxFiles)
                throw new ToolDeniedException("sandbox_limit", "工作区副本超出文件数量上限。");
            var child = relative.Length == 0 ? entry.Name : relative + "/" + entry.Name;
            if (Skip(entry) || !WorkspaceDocuments.IsVisible(child) || drafts?.Get(child) is not null || index?.IsSubmodule(child) == true)
                continue;
            var directory = entry is DirectoryInfo;
            var onlyTracked = trackedOnly || WorkspaceIgnore.Excludes(rules, new(entry.Name, child, directory));
            if (onlyTracked && index?.Tracks(child, directory) != true)
                continue;
            var target = destination is null ? null : Path.Combine(destination, entry.Name);
            digest.AppendData(Encoding.UTF8.GetBytes(child + (directory ? "/\0" : "\0")));
            if (directory)
            {
                if (target is not null)
                    Directory.CreateDirectory(target);
                CopyDirectory(child, target, digest, ref remaining, ref entries, index, rules, onlyTracked, token);
                continue;
            }
            using var path = _workspace.OpenPath(child);
            using var handle = WindowsWorkspace.OpenForRead(path.AbsolutePath, FileShare.Read);
            WindowsWorkspace.GetFileIdentity(handle, path.AbsolutePath);
            using var input = new FileStream(handle, FileAccess.Read);
            if (input.Length > MaxFileBytes || input.Length > remaining)
                throw new ToolDeniedException("sandbox_limit", "工作区副本超出大小上限。");
            var bytes = new byte[(int)input.Length];
            input.ReadExactly(bytes);
            if (_credential.Length > 0 && bytes.AsSpan().IndexOf(_credential) >= 0)
                throw new ToolDeniedException("sandbox_content_denied", "工作区文件包含当前模型凭据。");
            digest.AppendData(SHA256.HashData(bytes));
            if (target is not null)
            {
                using var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write);
                output.Write(bytes);
            }
            remaining -= bytes.Length;
        }
    }

    private static bool Skip(FileSystemInfo file) =>
        file.Name.StartsWith('.') || (file.Attributes & (FileAttributes.ReparsePoint | FileAttributes.Device)) != 0;
}
