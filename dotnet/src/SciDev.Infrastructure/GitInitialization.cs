using SciDev.Core;

namespace SciDev.Infrastructure;

public static class GitInitialization
{
    public const string DefaultBranch = "main";
    public sealed record Result(string Status, string? Branch);

    public static async Task<Result> InitializeAsync(string workspace, string branch, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        if (branch.Length is 0 or > 128 || branch.StartsWith('-') ||
            WindowsWorkspace.NormalizeOperatorPath("refs/heads/" + branch) != "refs/heads/" + branch)
            throw new ArgumentException("请输入有效的初始 Git 分支名。");

        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace));
        var guard = new WindowsWorkspace(root);
        using var destination = guard.OpenOperatorPath(".git")!;
        if (Exists(destination.AbsolutePath))
        {
            if (!Directory.Exists(destination.AbsolutePath))
                throw new ToolDeniedException("git_linked_worktree", "当前目录已有 Git 工作树记录，请在主仓库管理。");
            var existing = await GitWorkspace.StatusAsync(root, token);
            return new("already_initialized", existing.Branch);
        }
        RequireOutsideRepository(root);
        var git = GitProcess.RequireExecutable();

        var state = guard.EnsureStateDirectory();
        using var scratchLease = state.OpenPath("scidev-git-init-" + Guid.NewGuid().ToString("N"));
        var scratch = Directory.CreateDirectory(scratchLease.AbsolutePath);
        try
        {
            var template = scratch.CreateSubdirectory("empty-template");
            var config = Path.Combine(scratch.FullName, "empty-config");
            File.WriteAllText(config, "");
            var start = GitProcess.Prepare(git, scratch.FullName, config);
            start.Environment["GIT_DIR"] = ".git";
            start.Environment["GIT_WORK_TREE"] = ".";
            var created = await GitProcess.RunAsync(start,
                ["init", "--quiet", $"--initial-branch={branch}", $"--template={template.Name}"], token);
            if (created.Code != 0 || created.Truncated)
                throw new ToolDeniedException("git_init_failed", "Git 未能初始化仓库，请核对分支名和目录权限。");

            using var source = new WindowsWorkspace(scratch.FullName).OpenOperatorPath(".git")!;
            var metadata = new WindowsWorkspace(source.AbsolutePath);
            using (var head = metadata.OpenPath("HEAD"))
            using (var snapshot = WorkspaceFiles.OpenSnapshot(head.AbsolutePath, FileShare.Read, maxBytes: 4096))
                if (WorkspaceFiles.Utf8.GetString(snapshot.Bytes).TrimEnd('\r', '\n') != $"ref: refs/heads/{branch}")
                    throw new ToolDeniedException("git_init_failed", "Git 创建的初始分支与请求不一致。");

            using (var localConfig = metadata.OpenPath("config"))
                File.AppendAllText(localConfig.AbsolutePath, "\n[user]\n\tname = SciDevHarness\n\temail = scidev@localhost\n");
            Directory.CreateDirectory(Path.Combine(source.AbsolutePath, "info"));
            using (var exclude = metadata.OpenPath("info/exclude"))
                File.WriteAllText(exclude.AbsolutePath, "/.research/\n");

            RequireOutsideRepository(root);
            token.ThrowIfCancellationRequested();
            try { Directory.Move(source.AbsolutePath, destination.AbsolutePath); }
            catch (IOException) when (Exists(destination.AbsolutePath))
            {
                throw new ToolDeniedException("git_init_conflict", "初始化期间出现了新的 Git 元数据，请刷新后核对。");
            }
            return new("initialized", branch);
        }
        finally
        {
            // Delete only the scratch directory created by this invocation.
            if (scratch.Parent?.FullName.Equals(Path.Combine(root, ".research"), StringComparison.OrdinalIgnoreCase) == true &&
                scratch.Name.StartsWith("scidev-git-init-", StringComparison.Ordinal) &&
                (File.GetAttributes(scratch.FullName) & FileAttributes.ReparsePoint) == 0)
                scratch.Delete(recursive: true);
        }
    }

    private static void RequireOutsideRepository(string workspace)
    {
        for (var ancestor = Directory.GetParent(workspace); ancestor is not null; ancestor = ancestor.Parent)
            if (Exists(Path.Combine(ancestor.FullName, ".git")))
                throw new ToolDeniedException("git_nested_workspace", "当前目录位于已有 Git 工作区内，请从该仓库根目录操作。");
    }

    private static bool Exists(string path)
    {
        try { _ = File.GetAttributes(path); return true; }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
    }
}
