using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class GitMetadataFiles : IDisposable
{
    private readonly List<IDisposable> _leases = [];

    public string HoldFile(WindowsWorkspace metadata, string relative, bool required = false, bool mayAppend = false,
        bool createParents = false)
    {
        var holder = Acquire(metadata, relative, createParents);
        _leases.Add(holder);
        var path = holder.AbsolutePath;
        if (WindowsWorkspace.FileExists(path))
        {
            var file = WindowsWorkspace.OpenForRead(path, mayAppend ? FileShare.ReadWrite : FileShare.Read);
            _leases.Add(file);
            WindowsWorkspace.GetFileIdentity(file, path);
        }
        else if (required)
            throw new ToolDeniedException("git_commit_metadata", "Git 仓库缺少所需的元数据文件。");
        return path;
    }

    public void CheckWritableRef(WindowsWorkspace metadata, string reference)
    {
        var target = Acquire(metadata, reference, createParents: true);
        _leases.Add(target);
        if (!WindowsWorkspace.FileExists(target.AbsolutePath))
            return;
        using var file = WindowsWorkspace.OpenForRead(target.AbsolutePath, FileShare.Read);
        WindowsWorkspace.GetFileIdentity(file, target.AbsolutePath);
    }

    private static WindowsWorkspace.Lease Acquire(WindowsWorkspace metadata, string relative, bool createParents)
    {
        if (createParents)
        {
            var prefix = "";
            foreach (var part in relative.Split('/')[..^1])
            {
                prefix += part;
                using var parent = metadata.OpenPath(prefix);
                Directory.CreateDirectory(parent.AbsolutePath);
                prefix += '/';
            }
        }
        return metadata.OpenPath(relative);
    }

    public void Dispose()
    {
        for (var i = _leases.Count - 1; i >= 0; i--)
            _leases[i].Dispose();
    }
}
