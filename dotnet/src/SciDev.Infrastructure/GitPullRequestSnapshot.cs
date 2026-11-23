using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class GitPullRequestSnapshot : IDisposable
{
    private const string Prefix = "scidev-pr-plan-";
    private readonly string _parent = Path.TrimEndingDirectorySeparator(Path.GetFullPath(Path.GetTempPath()));
    private readonly DirectoryInfo _scratch;

    public GitReadContext Git { get; }

    public GitPullRequestSnapshot(string workspace, string? metadataWorkspace, CancellationToken token)
    {
        var relative = Path.GetRelativePath(workspace, _parent);
        if (!Path.IsPathFullyQualified(relative) && relative != ".." && !relative.StartsWith("..\\", StringComparison.Ordinal))
            throw new ToolDeniedException("git_temp_scope", "PR 需要工作区外的系统临时目录存放本地 Git 副本。");
        _scratch = Directory.CreateTempSubdirectory(Prefix);
        try
        {
            Git = new(workspace, token, metadataWorkspace, scratchWorkspace: _scratch.FullName);
        }
        catch
        {
            RemoveScratch();
            throw;
        }
    }

    public void Dispose()
    {
        try { Git.Dispose(); }
        finally { RemoveScratch(); }
    }

    private void RemoveScratch()
    {
        if (_scratch.Name.StartsWith(Prefix, StringComparison.Ordinal) &&
            string.Equals(_scratch.Parent?.FullName, _parent, StringComparison.OrdinalIgnoreCase) &&
            (_scratch.Attributes & FileAttributes.ReparsePoint) == 0)
            _scratch.Delete(recursive: true);
    }
}
