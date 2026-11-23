using System.Globalization;
using System.Net;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed partial class GitHubPullRequests
{
    public sealed record Created(bool DryRun, string Repo, string Branch, string Base, string Url, bool Draft, string Commit);

    public Task<Created> PublishAsync(string? metadataWorkspace, string? baseBranch, string? title, string? body,
        bool draft, CancellationToken token) =>
        PublishAsync(metadataWorkspace, baseBranch, title, body, draft, GitBranchPush.PushAsync, token);

    internal async Task<Created> PublishAsync(string? metadataWorkspace, string? baseBranch, string? title, string? body, bool draft,
        Func<GitReadContext, GitHubRepository, GitHubPullRequestPlan, string, CancellationToken, Task> push,
        CancellationToken token)
    {
        using var snapshot = new GitPullRequestSnapshot(_workspace, metadataWorkspace, token);
        var plan = await GitHubPullRequestPlan.ReadPlanAsync(snapshot.Git, _repository, baseBranch, title, draft, token);
        var description = await PublicationBodyAsync(snapshot.Git, plan.Commit, body, token);
        CheckPublicationText([plan.Branch, plan.Base, plan.Title, description]);

        var baseSha = await ReadRefAsync(plan.Base, "核对目标 base 分支", token);
        var count = await GitPushCredentialScan.CheckAsync(snapshot.Git, plan.Commit, baseSha, _credential, _modelKey, token);
        if (count == 0)
            throw new ToolDeniedException("pr_no_changes", "功能分支没有相对远端 base 可发布的提交。");
        var reference = $"refs/heads/{plan.Branch}";
        var pushPreview = $"仓库 {plan.Repo}\n目标 {reference}\n提交 {plan.Commit}\n已检查 {count} 个 Git 对象中的当前凭据";
        await RequireRemoteApprovalAsync(OperationKind.PushGitHubBranch, _repository.PushUrl,
            "通过 HTTPS 推送已核对的提交。仅允许常规推送，Git 不会强制覆盖远端分支。", pushPreview, token);
        await push(snapshot.Git, _repository, plan, _credential, token);
        try
        {
            var remoteHead = await ReadRefAsync(plan.Branch, "核对已推送的远端 head", token);
            if (remoteHead != plan.Commit)
                throw new ToolDeniedException("pr_head_changed", "远端 head 已变化，未创建 PR。");
            return await CreateAsync(plan, description, token);
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            throw new ToolDeniedException("pr_create_cancelled_after_push", "分支已推送，PR 创建已取消。");
        }
        catch (ToolDeniedException ex)
        {
            var status = ex.Status == "approval_denied" ? "pr_create_denied_after_push" : ex.Status;
            throw new ToolDeniedException(status, $"分支 {plan.Branch} 已推送。{ex.Message}");
        }
    }

    private async Task<string> ReadRefAsync(string branch, string purpose, CancellationToken token)
    {
        var escaped = string.Join('/', branch.Split('/').Select(Uri.EscapeDataString));
        var target = $"https://github.com/{_repository.Slug}/tree/{escaped}";
        var reply = await SendRequestAsync(HttpMethod.Get, $"{_repository.ApiRepository}/git/ref/heads/{escaped}",
            JsonMedia, null, OperationKind.ReadGitHubPullRequest, target, purpose, null,
            (response, deadline) => ReadTextAsync(response, 256_000, deadline), null, token);
        var root = ReadJson(reply);
        var obj = Field(root, "object");
        var sha = Value(obj, "sha");
        if (Value(root, "ref") != "refs/heads/" + branch || Value(obj, "type") != "commit" || !GitHubPullRequestPlan.IsSha(sha))
            throw InvalidResponse();
        return sha!;
    }

    private async Task<Created> CreateAsync(GitHubPullRequestPlan plan, string body, CancellationToken token)
    {
        var preview = $"仓库 {plan.Repo}\n分支 {plan.Branch} → {plan.Base}\n提交 {plan.Commit}\n草稿 {(plan.Draft ? "是" : "否")}" +
            $"\n标题 {plan.Title}\n\n{body}";
        var data = new { head = plan.Branch, @base = plan.Base, title = plan.Title, body, draft = plan.Draft };
        return await SendRequestAsync(HttpMethod.Post, $"{_repository.ApiRepository}/pulls", JsonMedia, data,
            OperationKind.CreateGitHubPullRequest, $"https://github.com/{_repository.Slug}/pulls", "创建一次 GitHub PR",
            preview, async (reply, deadline) =>
            {
                if (reply.StatusCode != HttpStatusCode.Created)
                    throw CreateUnconfirmed();
                JsonElement receipt;
                try { receipt = ReadJson(await ReadTextAsync(reply, 256_000, deadline)); }
                catch (ToolDeniedException ex) when (ex.Status == "pr_invalid_response") { throw CreateUnconfirmed(); }
                var number = ReadNumber(receipt);
                var head = Field(receipt, "head");
                var baseRef = Field(receipt, "base");
                if (number is < 1 or > 1_000_000_000 || !MatchesRef(head, plan.Branch) || !MatchesRef(baseRef, plan.Base) ||
                    Value(head, "sha") != plan.Commit || Field(receipt, "draft").ValueKind != (plan.Draft ? JsonValueKind.True : JsonValueKind.False))
                    throw CreateUnconfirmed();
                return new Created(false, plan.Repo, plan.Branch, plan.Base, _repository.PullUrl(number), plan.Draft, plan.Commit);
            }, CreateUnconfirmed, token);
    }

    private bool MatchesRef(JsonElement value, string branch) => Value(value, "ref") == branch &&
        _repository.Slug.Equals(Value(Field(value, "repo"), "full_name"), StringComparison.OrdinalIgnoreCase);

    private static async Task<string> PublicationBodyAsync(GitReadContext git, string head, string? body, CancellationToken token)
    {
        var text = body?.Trim() ?? "";
        if (text.Length == 0)
        {
            var message = await git.RunAsync(["--no-replace-objects", "show", "--no-patch", "--no-notes", "--format=%B", head, "--"],
                token, maxChars: 8001);
            if (message.Code != 0 || message.Truncated)
                throw new ToolDeniedException("pr_body_required", "无法从提交生成完整正文，请显式提供 --body。");
            text = message.Text.Trim();
        }
        if (text.Length > 8000)
            throw new ArgumentException("PR 正文最多 8,000 字符。");
        return text;
    }

    private void CheckPublicationText(string[] values)
    {
        foreach (var value in values)
        {
            if (value.Contains(_credential, StringComparison.Ordinal) ||
                !string.IsNullOrEmpty(_modelKey) && value.Contains(_modelKey, StringComparison.Ordinal))
                throw new ToolDeniedException("pr_credential", "PR 分支、标题或正文包含当前 GitHub 或模型凭据。");
            if (value.Any(ch => ch is not ('\r' or '\n' or '\t') && (char.IsControl(ch) ||
                CharUnicodeInfo.GetUnicodeCategory(ch) == UnicodeCategory.Format)))
                throw new ArgumentException("PR 内容包含无法安全显示的控制字符。");
        }
    }

    private static ToolDeniedException CreateUnconfirmed() =>
        new("pr_create_unconfirmed", "无法确认 GitHub 是否已创建 PR。请核对该仓库的 PR 列表后再试。");
}
