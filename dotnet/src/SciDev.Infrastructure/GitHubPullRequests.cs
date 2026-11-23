using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Operator-initiated, approved GitHub PR queries and reviews.</summary>
public sealed partial class GitHubPullRequests
{
    private const string Root = "https://api.github.com";
    private const string JsonMedia = "application/vnd.github+json";
    private const string DiffMedia = "application/vnd.github.diff";
    private const string ViewQuery = """
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              number title state url headRefName baseRefName headRefOid baseRefOid reviewDecision mergeable
              author { login __typename ... on User { id name } ... on Bot { id } }
            }
          }
        }
        """;
    private readonly GitHubRepository _repository;
    private readonly string _workspace;
    private readonly IOperationReview _reviewer;
    private readonly string _credential;
    private readonly string? _modelKey;
    private readonly bool _allowNetwork;
    private readonly bool _denyAdditionalNetwork;
    private readonly Func<Uri, HttpClient> _connect;

    internal GitHubPullRequests(string workspace, GitHubRepository repository, IOperationReview reviewer,
        string credential, bool allowNetwork, Func<Uri, HttpClient> connect, string? modelKey = null,
        bool denyAdditionalNetwork = false)
    {
        _workspace = workspace;
        _repository = repository;
        _reviewer = reviewer;
        _credential = credential;
        _allowNetwork = allowNetwork;
        _denyAdditionalNetwork = denyAdditionalNetwork;
        _connect = connect;
        _modelKey = modelKey;
    }

    public static async Task<GitHubPullRequests> OpenAsync(string workspace, string? metadataWorkspace,
        IOperationReview? reviewer, bool allowNetwork, CancellationToken token, string? modelKey = null,
        bool denyAdditionalNetwork = false)
    {
        var full = Path.GetFullPath(workspace);
        _ = CheckNetwork(full, allowNetwork, denyAdditionalNetwork);
        if (reviewer is null)
            throw new ToolDeniedException("approval_required", "GitHub PR 需要在交互终端逐次批准联网。");
        var repository = await GitHubRepository.ReadAsync(full, metadataWorkspace, token);
        var credential = await GitHubCredential.ReadAsync(full, reviewer, token);
        modelKey = string.IsNullOrWhiteSpace(modelKey) ? Environment.GetEnvironmentVariable("SCIDEV_API_KEY")?.Trim() : modelKey.Trim();
        if (credential == modelKey || repository.Slug.Contains(credential, StringComparison.Ordinal) ||
            !string.IsNullOrEmpty(modelKey) && repository.Slug.Contains(modelKey, StringComparison.Ordinal))
            throw new ToolDeniedException("pr_credential", "GitHub 凭据、模型密钥或仓库名称之间存在冲突。");
        return new(full, repository, reviewer, credential, allowNetwork, ApprovedHttpsClient.CreateDirectClient, modelKey, denyAdditionalNetwork);
    }

    public sealed record Diff(int Number, string Text, bool Truncated, string Url);
    public sealed record Review(int Number, string Action, string Url);
    public sealed record Revision(string Head, string Base);
    public sealed record ViewWithDiff(int Number, JsonElement Details, Diff Patch, Revision Revision);

    public async Task<JsonElement> ViewAsync(int number, CancellationToken token) =>
        (await ReadViewAsync(number, token)).Details;

    public async Task<ViewWithDiff> ViewWithDiffAsync(int number, CancellationToken token)
    {
        var (details, head, baseSha) = await ReadViewAsync(number, token);
        if (!GitHubPullRequestPlan.IsSha(head) || !GitHubPullRequestPlan.IsSha(baseSha))
            throw InvalidResponse();
        var revision = new Revision(head!, baseSha!);
        var diff = await DiffAsync(number, 300_000, token);
        await ReadReviewHeadAsync(number, revision, "核对差异所对应的 PR 提交", token);
        return new(number, details, diff, revision);
    }

    private async Task<(JsonElement Details, string? Head, string? Base)> ReadViewAsync(int number, CancellationToken token)
    {
        ValidateNumber(number);
        var request = new { query = ViewQuery, variables = new { owner = _repository.Owner, name = _repository.Name, number } };
        var body = await SendAsync(HttpMethod.Post, "/graphql", number, JsonMedia, request, "读取 PR 详情", null,
            (response, deadline) => ReadTextAsync(response, 256_000, deadline), token);
        var root = ReadJson(body);
        var pr = Field(Field(Field(root, "data"), "repository"), "pullRequest");
        if (Field(root, "errors").ValueKind != JsonValueKind.Undefined || ReadNumber(pr) != number ||
            Value(pr, "title") is not { } title || Value(pr, "state") is not { } state)
            throw InvalidResponse();
        var author = Field(pr, "author");
        object? displayAuthor = author.ValueKind == JsonValueKind.Object ? new
        {
            id = Value(author, "id"), is_bot = Value(author, "__typename") == "Bot",
            login = Value(author, "login"), name = Value(author, "name"),
        } : null;
        var details = JsonSerializer.SerializeToElement(new
        {
            number, title, state, url = _repository.PullUrl(number), author = displayAuthor,
            headRefName = Value(pr, "headRefName"), baseRefName = Value(pr, "baseRefName"),
            reviewDecision = Value(pr, "reviewDecision"), mergeable = Value(pr, "mergeable"),
        });
        return (details, Value(pr, "headRefOid"), Value(pr, "baseRefOid"));
    }

    public async Task<Diff> DiffAsync(int number, int maxChars, CancellationToken token)
    {
        ValidateNumber(number);
        var reply = await SendAsync(HttpMethod.Get, _repository.ApiPath(number), number, DiffMedia, null, "读取 PR 差异", null,
            (response, deadline) =>
            {
                var media = response.Content.Headers.ContentType?.MediaType;
                if (media is null || !(media.EndsWith(".diff", StringComparison.OrdinalIgnoreCase) ||
                    media is "text/plain" or "text/x-diff" or "text/x-patch"))
                    throw InvalidResponse();
                return ReadTextAsync(response, Math.Clamp(maxChars, 10_000, 1_000_000), deadline);
            }, token);
        if (string.IsNullOrWhiteSpace(reply.Text))
            throw new ToolDeniedException("pr_diff_empty", "该 PR 没有可显示的差异。");
        return new(number, reply.Text, reply.Truncated, _repository.PullUrl(number));
    }

    public async Task<Review> ReviewAsync(int number, string action, string? body, CancellationToken token, Revision? expectedRevision = null)
    {
        ValidateNumber(number);
        var normalized = action.Trim().ToLowerInvariant().Replace('_', '-');
        var (githubEvent, expectedState) = normalized switch
        {
            "approve" => ("APPROVE", "APPROVED"),
            "comment" => ("COMMENT", "COMMENTED"),
            "request-changes" => ("REQUEST_CHANGES", "CHANGES_REQUESTED"),
            _ => throw new ArgumentException("审阅操作须为 approve、comment 或 request-changes。"),
        };
        body = body?.Trim() ?? "";
        if ((normalized != "approve" && body.Length == 0) || body.Length > 20_000 ||
            body.Any(ch => ch is not ('\r' or '\n' or '\t') && (char.IsControl(ch) ||
                CharUnicodeInfo.GetUnicodeCategory(ch) == UnicodeCategory.Format)))
            throw new ArgumentException("评论或请求修改需要正文，正文最多 20,000 字符。");
        if (body.Contains(_credential, StringComparison.Ordinal) || !string.IsNullOrEmpty(_modelKey) && body.Contains(_modelKey, StringComparison.Ordinal))
            throw new ToolDeniedException("pr_credential", "审阅正文包含当前 GitHub 或模型凭据。");
        var path = _repository.ApiPath(number);
        var head = await ReadReviewHeadAsync(number, expectedRevision, "读取审阅所绑定的 PR 提交", token);

        var data = new { @event = githubEvent, body, commit_id = head };
        var preview = $"审阅操作 {normalized}\n提交 {head}" + (body.Length > 0 ? "\n\n" + body : "");
        await SendAsync(HttpMethod.Post, path + "/reviews", number, JsonMedia, data, "向 GitHub 发布一次 PR 审阅", preview,
            async (reply, deadline) =>
            {
                if (reply.StatusCode != HttpStatusCode.OK)
                    throw Unconfirmed();
                try
                {
                    var receipt = ReadJson(await ReadTextAsync(reply, 256_000, deadline));
                    if (Value(receipt, "commit_id") == head && Value(receipt, "state") == expectedState &&
                        Field(receipt, "id") is { ValueKind: JsonValueKind.Number } id && id.TryGetInt64(out var reviewId) && reviewId > 0)
                        return true;
                }
                catch (ToolDeniedException ex) when (ex.Status == "pr_invalid_response") { }
                throw Unconfirmed();
            }, token);
        return new(number, normalized, _repository.PullUrl(number));
    }

    private async Task<string> ReadReviewHeadAsync(int number, Revision? expected, string purpose, CancellationToken token)
    {
        var response = await SendAsync(HttpMethod.Get, _repository.ApiPath(number), number, JsonMedia, null, purpose, null,
            (reply, deadline) => ReadTextAsync(reply, 256_000, deadline), token);
        var pr = ReadJson(response);
        var head = Value(Field(pr, "head"), "sha");
        if (ReadNumber(pr) != number || !GitHubPullRequestPlan.IsSha(head))
            throw InvalidResponse();
        if (expected is not null && (expected.Head != head || expected.Base != Value(Field(pr, "base"), "sha")))
            throw new ToolDeniedException("pr_review_revision_changed", "PR 的 head 或 base 已变化，请重新加载差异后再审阅。");
        return head!;
    }

    public string Serialize(object value, JsonSerializerOptions options)
    {
        var json = RunJson.SerializeRedacted(value, options, _credential);
        using var document = JsonDocument.Parse(json);
        return RunJson.SerializeRedacted(document.RootElement, options, _modelKey);
    }

    public string Display(string value)
    {
        var text = WorkspaceFiles.RedactCredential(WorkspaceFiles.RedactCredential(value, _credential), _modelKey);
        return new(text.Select(ch => ch is '\t' or '\r' or '\n' ||
            !char.IsControl(ch) && CharUnicodeInfo.GetUnicodeCategory(ch) != UnicodeCategory.Format ? ch : '�').ToArray());
    }

    private Task<T> SendAsync<T>(HttpMethod method, string path, int number, string accept, object? data,
        string summary, string? preview, Func<HttpResponseMessage, CancellationToken, Task<T>> read, CancellationToken token)
        => SendRequestAsync(method, path, accept, data, preview is null ? OperationKind.ReadGitHubPullRequest : OperationKind.SubmitGitHubReview,
            _repository.PullUrl(number), summary, preview, read, preview is null ? null : Unconfirmed, token);

    private async Task<T> SendRequestAsync<T>(HttpMethod method, string path, string accept, object? data, OperationKind kind,
        string target, string summary, string? preview, Func<HttpResponseMessage, CancellationToken, Task<T>> read,
        Func<ToolDeniedException>? unconfirmed, CancellationToken token)
    {
        var uri = new Uri(Root + path);
        await RequireRemoteApprovalAsync(kind, target, $"{summary}。直连 {method} {uri.AbsoluteUri}。", preview, token);

        using var http = _connect(uri);
        using var request = new HttpRequestMessage(method, uri)
        {
            Version = HttpVersion.Version11, VersionPolicy = HttpVersionPolicy.RequestVersionExact,
            Content = data is null ? null : new StringContent(JsonSerializer.Serialize(data), Encoding.UTF8, "application/json"),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _credential);
        request.Headers.UserAgent.ParseAdd("SciDevHarness-CSharp/0.1");
        request.Headers.Accept.Add(new(accept));
        if (path != "/graphql")
            request.Headers.Add("X-GitHub-Api-Version", "2026-03-10");
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(token);
        budget.CancelAfter(TimeSpan.FromSeconds(60));
        try
        {
            using var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, budget.Token);
            if (unconfirmed is not null && (int)response.StatusCode >= 500)
                throw unconfirmed();
            if (!response.IsSuccessStatusCode)
                throw new ToolDeniedException("pr_http_status", $"GitHub 返回 HTTP {(int)response.StatusCode}。");
            return await read(response, budget.Token);
        }
        catch (OperationCanceledException) when (unconfirmed is not null) { throw unconfirmed(); }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        { throw new ToolDeniedException("pr_timeout", "GitHub PR 请求超时。"); }
        catch (Exception ex) when (ex is IOException or HttpRequestException or SocketException)
        {
            throw unconfirmed is not null ? unconfirmed() : new ToolDeniedException("pr_network_error", "无法直连读取 GitHub PR。", ex);
        }
    }

    private async Task RequireRemoteApprovalAsync(OperationKind kind, string target, string detail, string? preview, CancellationToken token)
    {
        var denied = CheckNetwork(_workspace, _allowNetwork, _denyAdditionalNetwork);
        var warning = denied ? " 本次按显式选项覆盖工作区断网策略。" : "";
        await GitHubCredential.RequireAsync(_reviewer, new("github-pr", kind, target, detail + warning, Before: preview), token);
        if (!denied && CheckNetwork(_workspace, _allowNetwork, _denyAdditionalNetwork))
            throw new ToolDeniedException("network_policy_denied", "审批期间工作区切换到了断网策略，请重新发起操作。");
    }

    private static bool CheckNetwork(string workspace, bool allowNetwork, bool denyAdditionalNetwork)
    {
        var denied = denyAdditionalNetwork || WorkspaceNetworkPolicy.IsDenied(workspace);
        if (denied && !allowNetwork)
            throw new ToolDeniedException("network_policy_denied", "工作区策略禁止联网。--allow-network 可申请单次覆盖。");
        return denied;
    }

    private sealed record BoundedText(string Text, bool Truncated);

    private static async Task<BoundedText> ReadTextAsync(HttpResponseMessage response, int limit, CancellationToken token)
    {
        if (response.Content.Headers.ContentEncoding.Count != 0)
            throw InvalidResponse();
        await using var stream = await response.Content.ReadAsStreamAsync(token);
        using var reader = new StreamReader(stream, new UTF8Encoding(false, true), detectEncodingFromByteOrderMarks: false);
        var buffer = new char[limit + 1];
        int count;
        try { count = await reader.ReadBlockAsync(buffer.AsMemory(), token); }
        catch (DecoderFallbackException) { throw InvalidResponse(); }
        var length = Math.Min(count, limit);
        if (length > 0 && char.IsHighSurrogate(buffer[length - 1]))
            length--;
        return new(new string(buffer, 0, length), count > length);
    }

    private static JsonElement ReadJson(BoundedText reply)
    {
        if (reply.Truncated)
            throw InvalidResponse();
        try
        {
            using var json = JsonDocument.Parse(reply.Text, new JsonDocumentOptions { MaxDepth = 32, AllowDuplicateProperties = false });
            if (json.RootElement.ValueKind == JsonValueKind.Object)
                return json.RootElement.Clone();
        }
        catch (JsonException) { }
        throw InvalidResponse();
    }

    private static JsonElement Field(JsonElement value, string name) =>
        value.ValueKind == JsonValueKind.Object && value.TryGetProperty(name, out var field) ? field : default;
    private static string? Value(JsonElement value, string name) =>
        Field(value, name) is { ValueKind: JsonValueKind.String } field ? field.GetString() : null;
    private static int ReadNumber(JsonElement value) =>
        Field(value, "number") is { ValueKind: JsonValueKind.Number } field && field.TryGetInt32(out var number) ? number : 0;
    private static void ValidateNumber(int number)
    {
        if (number is < 1 or > 1_000_000_000)
            throw new ArgumentException("PR 编号须为 1 到 1,000,000,000 的整数。");
    }

    private static ToolDeniedException InvalidResponse() => new("pr_invalid_response", "GitHub PR 响应无效或超出上限。");
    private static ToolDeniedException Unconfirmed() =>
        new("pr_review_unconfirmed", "无法确认 GitHub 是否已收到本次审阅。请核对该 PR 的审阅记录后再试。");
}
