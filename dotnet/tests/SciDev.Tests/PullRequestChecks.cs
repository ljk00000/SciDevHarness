using System.Net;
using System.Text;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class PullRequestChecks
{
    private const string Key = "github-api-fixture-credential";
    private const string ModelKey = "model-api-fixture-credential";
    private static readonly string Head = new('a', 40);
    private static readonly string Base = new('b', 40);

    internal static async Task RunAsync(DirectoryInfo workspace)
    {
        foreach (var git in new[] { "https://github.com/example/fixture.git", "git@github.com:example/fixture",
            "ssh://git@ssh.github.com:443/example/fixture.git" })
            Assert(GitHubRepository.Parse(git)?.Slug == "example/fixture", "valid GitHub HTTPS and SSH remotes");
        foreach (var invalid in new[] { "https://notgithub.com/example/fixture", "https://other.test/github.com/example/fixture",
            "https://name:password@github.com/example/fixture", "https://github.com/example/../fixture",
            "https://github.com/example/fixture?secret=1", "https://github.com:8000/example/fixture", "https://github.com/example/.." })
            Assert(GitHubRepository.Parse(invalid) is null, "invalid or ambiguous origin");

        var policy = Environment.GetEnvironmentVariable("SCIDEV_NETWORK_POLICY");
        Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", "ask");
        try { await ExerciseAsync(workspace); }
        finally { Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", policy); }
    }

    private static async Task ExerciseAsync(DirectoryInfo workspace)
    {
        var state = workspace.CreateSubdirectory(".research");
        var config = Path.Combine(workspace.CreateSubdirectory(".git").FullName, "config");
        File.WriteAllText(config, """
            [remote "origin"]
                url = ssh://git@ssh.github.com:443/example/fixture.git
            [include]
                path = unsafe-config
            [url "https://example.invalid/"]
                insteadOf = ssh://git@ssh.github.com:443/
            """);
        var before = File.ReadAllText(config);
        var repo = await GitHubRepository.ReadAsync(workspace.FullName, null, default);
        Assert(repo.Slug == "example/fixture" && File.ReadAllText(config) == before, "origin reads local config without URL rewrites or mutation");
        File.AppendAllText(config, "\n[remote \"origin\"]\n\turl = https://github.com/example/other\n");
        await ExpectAsync(GitHubRepository.ReadAsync(workspace.FullName, null, default), "pr_origin_invalid");
        File.WriteAllText(config, before);

        var requests = new List<Request>();
        int? mismatchedNumber = null;
        var currentHead = Head;
        var currentBase = Base;
        var publishStatus = HttpStatusCode.OK;
        var acknowledge = true;
        GitHubPullRequests Client(Reviewer reviewer, bool allowNetwork = false) => new(workspace.FullName, repo, reviewer,
            Key, allowNetwork, _ => new HttpClient(new Handler(requests, (verb, path) => path switch
            {
                "/graphql" => Document(new { data = new { repository = new { pullRequest = new
                {
                    number = 17, title = "Example title", state = "OPEN",
                    author = new { login = "operator", name = "Example", id = "U_test", __typename = "User" },
                    headRefName = "feature", baseRefName = "main", headRefOid = Head, baseRefOid = Base,
                    reviewDecision = "REVIEW_REQUIRED", mergeable = "MERGEABLE",
                } } } }),
                "/repos/example/fixture/pulls/17" when verb.Accept == "application/vnd.github.diff" =>
                    new(HttpStatusCode.OK) { Content = new StringContent("diff --git a/文.txt b/文.txt\n+" + new string('x', 11_000),
                        Encoding.UTF8, "application/vnd.github.v3.diff") },
                "/repos/example/fixture/pulls/17" => Document(new
                { number = mismatchedNumber ?? 17, head = new { sha = currentHead }, @base = new { sha = currentBase } }),
                "/repos/example/fixture/pulls/17/reviews" when publishStatus == HttpStatusCode.OK && acknowledge => ReviewReceipt(verb.Body!),
                "/repos/example/fixture/pulls/17/reviews" => new(publishStatus),
                _ => throw new InvalidOperationException(path),
            })), ModelKey);

        var deny = new Reviewer(_ => false);
        await ExpectAsync(Client(deny).ViewAsync(17, default), "approval_denied");
        Assert(requests.Count == 0 && deny.Operations.Count == 1, "denied read sends no traffic");

        var allow = new Reviewer(_ => true);
        var client = Client(allow);
        var view = await client.ViewAsync(17, default);
        Assert(view.GetProperty("reviewDecision").GetString() == "REVIEW_REQUIRED" && view.GetProperty("headRefName").GetString() == "feature",
            "old gh view fields are preserved");
        using (var query = JsonDocument.Parse(requests.Single().Body!))
            Assert(query.RootElement.GetProperty("variables").GetProperty("name").GetString() == "fixture", "GraphQL variables bind the local origin");
        var diff = await client.DiffAsync(17, 1, default);
        Assert(diff is { Number: 17, Truncated: true } && diff.Text.Length == 10_000 && diff.Text.Contains("文.txt") &&
            requests[^1].Method == HttpMethod.Get && requests[^1].Accept == "application/vnd.github.diff", "bounded UTF-8 diff with legacy clamp");

        requests.Clear();
        var browser = await client.ViewWithDiffAsync(17, default);
        Assert(browser.Revision == new GitHubPullRequests.Revision(Head, Base) && browser.Patch.Text.Contains("文.txt") &&
            requests.Select(request => request.Method).SequenceEqual([HttpMethod.Post, HttpMethod.Get, HttpMethod.Get]),
            "desktop queries metadata and diff then validates the reported revisions");
        currentBase = new string('c', 40);
        requests.Clear();
        await ExpectAsync(client.ReviewAsync(17, "approve", null, default, browser.Revision), "pr_review_revision_changed");
        Assert(requests.Count == 1 && requests[0].Method == HttpMethod.Get, "changed base stops review before publication");
        currentBase = Base;
        currentHead = new string('d', 40);
        requests.Clear();
        await ExpectAsync(client.ViewWithDiffAsync(17, default), "pr_review_revision_changed");
        Assert(requests.Count == 3, "changed head during load invalidates the diff");
        currentHead = Head;

        requests.Clear();
        var deniedReview = new Reviewer(operation => operation.Kind != OperationKind.SubmitGitHubReview);
        await ExpectAsync(Client(deniedReview).ReviewAsync(17, "comment", "example review", default), "approval_denied");
        Assert(requests.Count == 1 && requests[0].Method == HttpMethod.Get && deniedReview.Operations.Count == 2 &&
            deniedReview.Operations[1].Before!.Contains(Head), "metadata approval is not approval to publish");
        requests.Clear();
        await ExpectAsync(client.ReviewAsync(17, "comment", $"key: {ModelKey}", default), "pr_credential");
        Assert(requests.Count == 0, "model credential and GitHub bearer are never sent as review text");
        mismatchedNumber = 18;
        await ExpectAsync(client.ReviewAsync(17, "approve", null, default), "pr_invalid_response");
        Assert(requests.Count == 1 && requests[0].Method == HttpMethod.Get, "mismatched PR identity prevents a review");
        mismatchedNumber = null;
        foreach (var (action, githubEvent) in new[] { ("approve", "APPROVE"), ("comment", "COMMENT"), ("request-changes", "REQUEST_CHANGES") })
        {
            requests.Clear();
            var result = await client.ReviewAsync(17, action, "review 正文", default);
            Assert(result.Action == action && result.Url == "https://github.com/example/fixture/pull/17" && requests.Count == 2,
                "each review publishes exactly once to the selected PR");
            using var post = JsonDocument.Parse(requests[^1].Body!);
            Assert(requests[^1].Method == HttpMethod.Post && post.RootElement.GetProperty("event").GetString() == githubEvent &&
                post.RootElement.GetProperty("commit_id").GetString() == Head &&
                post.RootElement.GetProperty("body").GetString() == "review 正文", "post binds exact action, body and inspected commit");
        }

        publishStatus = HttpStatusCode.ServiceUnavailable;
        requests.Clear();
        await ExpectAsync(client.ReviewAsync(17, "comment", "verify before retrying", default), "pr_review_unconfirmed");
        Assert(requests.Count == 2, "ambiguous publication never retries");
        requests.Clear();
        publishStatus = HttpStatusCode.OK;
        acknowledge = false;
        await ExpectAsync(client.ReviewAsync(17, "approve", null, default), "pr_review_unconfirmed");
        Assert(requests.Count == 2, "a successful HTTP status without a review receipt does not confirm publication");
        Assert(!client.Serialize(new { key = Key, model = ModelKey }, new()).Contains("fixture-credential") &&
            !client.Display($"\x1b{Key}/{ModelKey}").Contains('\x1b'), "keys and terminal controls are removed from display");

        requests.Clear();
        var settings = Path.Combine(state.FullName, "settings.json");
        File.WriteAllText(settings, """{"network_policy":"deny"}""");
        await ExpectAsync(client.ViewAsync(17, default), "network_policy_denied");
        Assert(requests.Count == 0, "work area deny prevents requests");
        var overrideReview = new Reviewer(_ => true);
        _ = await Client(overrideReview, allowNetwork: true).ViewAsync(17, default);
        Assert(requests.Count == 1 && overrideReview.Operations.Single().Detail.Contains("覆盖工作区断网策略"), "override still requires a specific approval");
        File.Delete(settings);
    }

    private sealed record Request(HttpMethod Method, Uri Uri, string Accept, string? Body);

    private sealed class Handler(List<Request> requests, Func<Request, string, HttpResponseMessage> reply) : HttpMessageHandler
    {
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage message, CancellationToken token)
        {
            Assert(message.RequestUri is { Scheme: "https", Host: "api.github.com" } &&
                message.Headers.Authorization?.Parameter == Key, "credential is sent only to the selected GitHub API");
            var request = new Request(message.Method, message.RequestUri!, message.Headers.Accept.Single().MediaType!,
                message.Content is null ? null : await message.Content.ReadAsStringAsync(token));
            requests.Add(request);
            return reply(request, request.Uri.AbsolutePath);
        }
    }

    private sealed class Reviewer(Func<OperationReview, bool> decide) : IOperationReview
    {
        public List<OperationReview> Operations { get; } = [];

        public ValueTask<bool> ApproveAsync(OperationReview operation, CancellationToken token)
        {
            Operations.Add(operation);
            return new(decide(operation));
        }
    }

    private static HttpResponseMessage Document(object value) => new(HttpStatusCode.OK)
    { Content = new StringContent(JsonSerializer.Serialize(value), Encoding.UTF8, "application/json") };

    private static HttpResponseMessage ReviewReceipt(string body)
    {
        using var input = JsonDocument.Parse(body);
        var state = input.RootElement.GetProperty("event").GetString() switch
        {
            "APPROVE" => "APPROVED",
            "COMMENT" => "COMMENTED",
            "REQUEST_CHANGES" => "CHANGES_REQUESTED",
            _ => throw new InvalidOperationException("Unexpected review action."),
        };
        return Document(new { id = 123456789L, commit_id = Head, state });
    }

    private static async Task ExpectAsync(Task task, string code)
    {
        try { await task; }
        catch (ToolDeniedException error) when (error.Status == code) { return; }
        throw new InvalidOperationException("Expected " + code);
    }

    private static void Assert(bool condition, string description)
    {
        if (!condition)
            throw new InvalidOperationException(description);
    }
}
