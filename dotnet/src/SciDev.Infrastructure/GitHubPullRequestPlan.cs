using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed record GitHubPullRequestPlan(bool DryRun, string Repo, string Branch, string Base, string Title, bool Draft,
    string Commit, string[][] Commands)
{
    public static async Task<GitHubPullRequestPlan> BuildAsync(string workspace, string? metadataWorkspace, string? baseBranch,
        string? title, bool draft, CancellationToken token)
    {
        var repo = await GitHubRepository.ReadAsync(workspace, metadataWorkspace, token);
        using var snapshot = new GitPullRequestSnapshot(workspace, metadataWorkspace, token);
        return await ReadPlanAsync(snapshot.Git, repo, baseBranch, title, draft, token);
    }

    internal static async Task<GitHubPullRequestPlan> ReadPlanAsync(GitReadContext git, GitHubRepository repo, string? baseBranch,
        string? title, bool draft, CancellationToken token)
    {
        var current = await git.RunAsync(["symbolic-ref", "--quiet", "--short", "HEAD"], token, maxChars: 1024);
        var branch = current.Text.TrimEnd('\r', '\n');
        if (current.Code != 0 || current.Truncated || branch.Length is 0 or > 256 || branch.StartsWith('-') ||
            branch.Equals("main", StringComparison.OrdinalIgnoreCase) || branch.Equals("master", StringComparison.OrdinalIgnoreCase))
            throw new ToolDeniedException("pr_branch_invalid", "创建计划需要处于功能分支上的 Git 工作区。");
        if (string.IsNullOrWhiteSpace(baseBranch))
        {
            var remoteHead = await git.RunAsync(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], token, maxChars: 1024);
            var remote = remoteHead.Text.TrimEnd('\r', '\n');
            const string origin = "refs/remotes/origin/";
            baseBranch = remoteHead.Code == 0 && !remoteHead.Truncated && remote.StartsWith(origin, StringComparison.Ordinal)
                ? remote[origin.Length..] : "main";
        }
        else
            baseBranch = baseBranch.Trim();
        if (baseBranch.Length is 0 or > 256 || baseBranch == branch || baseBranch.StartsWith('-') || baseBranch.Any(char.IsControl))
            throw new ToolDeniedException("pr_base_invalid", "PR base 需要有效且不同于当前的分支。");
        var validBase = await git.RunAsync(["check-ref-format", "refs/heads/" + baseBranch], token, maxChars: 200);
        var validBranch = await git.RunAsync(["check-ref-format", "refs/heads/" + branch], token, maxChars: 200);
        if (validBase.Code != 0)
            throw new ToolDeniedException("pr_base_invalid", "PR base 分支名称无效。");
        if (validBranch.Code != 0)
            throw new ToolDeniedException("pr_branch_invalid", "PR 功能分支名称无效。");
        var resolved = await git.RunAsync(["--no-replace-objects", "rev-parse", "--verify", "refs/heads/" + branch + "^{commit}"], token, maxChars: 128);
        var commit = resolved.Text.TrimEnd('\r', '\n');
        if (resolved.Code != 0 || resolved.Truncated || !IsSha(commit))
            throw new ToolDeniedException("pr_head_invalid", "功能分支没有可推送的 GitHub 提交。");
        var heading = string.Join(' ', (title ?? "").Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        if (heading.Any(char.IsControl))
            throw new ToolDeniedException("pr_title_invalid", "PR 标题须为单行文本。");
        if (heading.Length > 120)
            heading = heading[..(char.IsHighSurrogate(heading[119]) ? 119 : 120)];
        return new(true, repo.Slug, branch, baseBranch, heading.Length == 0 ? branch : heading, draft, commit,
            [
                ["git", .. GitBranchPush.Arguments(repo.PushUrl, branch, commit)],
                ["POST", "https://api.github.com" + repo.ApiRepository + "/pulls"],
            ]);
    }

    internal static bool IsSha(string? value) => value is { Length: 40 } && value.All(char.IsAsciiHexDigitLower);
}
