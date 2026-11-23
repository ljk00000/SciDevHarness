using System.Net;
using System.Text;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class PullRequestPublishChecks
{
    private const string Credential = "fixture-github-push-credential";
    private const string ModelKey = "fixture-model-do-not-upload-key";
    private const string Branch = "feature/pinned";
    private const string Slug = "example/fixture";
    private readonly record struct Request(HttpMethod Method, string Path, string? Body);

    internal static async Task RunAsync(DirectoryInfo root)
    {
        var previous = Environment.GetEnvironmentVariable("SCIDEV_NETWORK_POLICY");
        Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", "ask");
        try { await ExerciseAsync(root); }
        finally
        {
            Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", previous);
            // Git for Windows may mark loose objects read-only in either fixture.
            foreach (var directory in new[] { "source/.git/objects", "remote/objects" })
            {
                var objects = Path.Combine(root.FullName, directory);
                if (!Directory.Exists(objects))
                    continue;
                foreach (var file in new DirectoryInfo(objects).EnumerateFiles("*", new EnumerationOptions
                    { RecurseSubdirectories = true, AttributesToSkip = FileAttributes.ReparsePoint }))
                    if ((file.Attributes & FileAttributes.ReadOnly) != 0)
                        file.Attributes &= ~FileAttributes.ReadOnly;
            }
        }
    }

    private static async Task ExerciseAsync(DirectoryInfo root)
    {
        var source = root.CreateSubdirectory("source").FullName;
        var bare = root.CreateSubdirectory("remote").FullName;
        var empty = Path.Combine(root.FullName, "empty-git-config");
        File.WriteAllText(empty, "");
        var localRemote = new Uri(bare + Path.DirectorySeparatorChar).AbsoluteUri;
        await LocalGitAsync(bare, ["init", "--bare", "--initial-branch=main"]);
        await LocalGitAsync(source, ["init", "--initial-branch=main"]);
        File.WriteAllText(Path.Combine(source, "seed.txt"), "base\n");
        await LocalGitAsync(source, ["add", "seed.txt"]);
        var baseCommit = await CommitAsync("fixture base");
        await LocalGitAsync(source, ["push", localRemote, $"{baseCommit}:refs/heads/main"]);
        await LocalGitAsync(source, ["branch", Branch]);
        await LocalGitAsync(source, ["symbolic-ref", "HEAD", "refs/heads/" + Branch]);
        await LocalGitAsync(source, ["config", "remote.origin.url", "ssh://git@ssh.github.com:443/example/fixture.git"]);
        await LocalGitAsync(source, ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"]);
        File.WriteAllText(Path.Combine(source, "feature.txt"), "approved changes\n");
        await LocalGitAsync(source, ["add", "feature.txt"]);
        var approvedHead = await CommitAsync("fixture feature body");
        var repo = await GitHubRepository.ReadAsync(source, null, default);
        Assert(repo.Slug == Slug, "production origin parsing");
        var config = Path.Combine(source, ".git", "config");
        var savedConfig = File.ReadAllBytes(config);
        var index = Path.Combine(source, ".git", "index");
        var savedIndex = File.ReadAllBytes(index);
        File.WriteAllText(Path.Combine(source, "feature.txt"), "uncommitted fixture work\n");
        var remoteRef = Path.Combine(bare, "refs", "heads", "feature", "pinned");
        var requests = new List<Request>();
        var pushAttempts = new List<string>();
        var wrongHead = false;
        var wrongReceipt = false;
        var pushFails = false;

        GitHubPullRequests Client(Reviewer reviewer) => new(source, repo, reviewer, Credential, false,
            _ => new HttpClient(new Handler(Respond)), ModelKey);

        Task<GitHubPullRequests.Created> Publish(Reviewer reviewer, string? body = null) =>
            Client(reviewer).PublishAsync(null, null, "Example title", body, true, PushLocallyAsync, default);

        var deniedAtStart = new Reviewer(_ => false);
        await ExpectAsync(Publish(deniedAtStart), "approval_denied");
        Assert(requests.Count == 0 && pushAttempts.Count == 0, "declining base lookup does nothing remotely");
        var noPush = new Reviewer(operation => operation.Kind != OperationKind.PushGitHubBranch);
        await ExpectAsync(Publish(noPush), "approval_denied");
        Assert(requests.Count == 1 && pushAttempts.Count == 0 && !File.Exists(remoteRef), "declining push never invokes Git transport");
        var noCreate = new Reviewer(operation => operation.Kind != OperationKind.CreateGitHubPullRequest);
        requests.Clear();
        await ExpectAsync(Publish(noCreate), "pr_create_denied_after_push");
        Assert(pushAttempts.SequenceEqual([approvedHead]) && requests.Select(request => request.Method).SequenceEqual([HttpMethod.Get, HttpMethod.Get]) &&
            File.ReadAllText(remoteRef).Trim() == approvedHead, "declining create keeps only the approved push");

        requests.Clear();
        var approved = new Reviewer(_ => true);
        var created = await Publish(approved);
        Assert(created is { DryRun: false, Branch: Branch, Draft: true } && created.Commit == approvedHead &&
            created.Url == $"https://github.com/{Slug}/pull/19", "legacy output plus pinned SHA");
        Assert(approved.Operations.Select(request => request.Kind).SequenceEqual([OperationKind.ReadGitHubPullRequest,
            OperationKind.PushGitHubBranch, OperationKind.ReadGitHubPullRequest, OperationKind.CreateGitHubPullRequest]),
            "base read, push, head read and create each require approval");
        Assert(approved.Operations[1].Before!.Contains(approvedHead) && approved.Operations[3].Before!.Contains("fixture feature body"),
            "reviewed push SHA and generated commit body are visible");
        using (var document = JsonDocument.Parse(requests[^1].Body!))
            Assert(document.RootElement.GetProperty("body").GetString() == "fixture feature body", "missing PR body derives from pinned commit");
        var committedContent = await LocalGitAsync(bare, ["show", $"refs/heads/{Branch}:feature.txt"]);
        Assert(committedContent == "approved changes" && File.ReadAllText(Path.Combine(source, "feature.txt")) == "uncommitted fixture work\n" &&
            File.ReadAllBytes(config).SequenceEqual(savedConfig) && File.ReadAllBytes(index).SequenceEqual(savedIndex),
            "push contains only pinned committed content and does not mutate local Git or working files");

        requests.Clear();
        var sourceRef = Path.Combine(source, ".git", "refs", "heads", "feature", "pinned");
        var movesLocalBranch = new Reviewer(operation =>
        {
            if (operation.Kind == OperationKind.PushGitHubBranch)
                File.WriteAllText(sourceRef, baseCommit + "\n");
            return true;
        });
        var pinned = await Publish(movesLocalBranch);
        Assert(pinned.Commit == approvedHead && File.ReadAllText(remoteRef).Trim() == approvedHead,
            "local HEAD movement during approval cannot change the pinned push");
        await LocalGitAsync(source, ["update-ref", "refs/heads/" + Branch, approvedHead]);

        requests.Clear();
        wrongHead = true;
        await ExpectAsync(Publish(approved), "pr_head_changed");
        Assert(requests.All(request => request.Method == HttpMethod.Get), "head mismatch prevents creation");
        wrongHead = false;
        requests.Clear();
        wrongReceipt = true;
        await ExpectAsync(Publish(approved, "explicit body"), "pr_create_unconfirmed");
        Assert(requests.Count(request => request.Method == HttpMethod.Post) == 1, "uncertain publication is never retried");
        wrongReceipt = false;
        requests.Clear();
        pushFails = true;
        await ExpectAsync(Publish(approved), "pr_push_unconfirmed");
        Assert(requests.Count == 1, "unconfirmed push prevents even the post-push lookup");
        pushFails = false;

        requests.Clear();
        var pushesBefore = pushAttempts.Count;
        await ExpectAsync(Publish(approved, "bad " + Credential), "pr_credential");
        Assert(requests.Count == 0 && pushAttempts.Count == pushesBefore, "known credentials in PR metadata are blocked before the first request");
        File.WriteAllText(Path.Combine(source, "hidden-in-history.bin"), new string('x', 16_375) + ModelKey);
        await LocalGitAsync(source, ["add", "hidden-in-history.bin"]);
        _ = await CommitAsync("historical fixture");
        await LocalGitAsync(source, ["rm", "hidden-in-history.bin"]);
        _ = await CommitAsync("remove historical fixture from HEAD");
        await ExpectAsync(Publish(approved), "pr_credential");
        Assert(requests.Count == 1 && pushAttempts.Count == pushesBefore && File.ReadAllText(remoteRef).Trim() == approvedHead,
            "credential split across buffers in an old blob blocks push even after HEAD deletes the file");

        HttpResponseMessage Respond(Request request)
        {
            requests.Add(request);
            return request.Path switch
            {
                "/repos/example/fixture/git/ref/heads/main" => Document(new { @ref = "refs/heads/main", @object = new { type = "commit", sha = baseCommit } }),
                "/repos/example/fixture/git/ref/heads/feature/pinned" => Document(new
                {
                    @ref = "refs/heads/" + Branch,
                    @object = new { type = "commit", sha = wrongHead ? baseCommit : File.ReadAllText(remoteRef).Trim() },
                }),
                "/repos/example/fixture/pulls" when request.Method == HttpMethod.Post => Document(new
                {
                    number = 19, draft = true,
                    head = new { @ref = Branch, sha = wrongReceipt ? baseCommit : File.ReadAllText(remoteRef).Trim(), repo = new { full_name = Slug } },
                    @base = new { @ref = "main", repo = new { full_name = Slug } },
                }, HttpStatusCode.Created),
                _ => throw new InvalidOperationException(request.Path),
            };
        }

        async Task PushLocallyAsync(GitReadContext git, GitHubRepository target, GitHubPullRequestPlan plan, string key, CancellationToken token)
        {
            pushAttempts.Add(plan.Commit);
            if (pushFails)
                throw new ToolDeniedException("pr_push_unconfirmed", "Fixture push stopped.");
            var setup = GitBranchPush.Prepare(git, target.PushUrl, key);
            var args = GitBranchPush.Arguments(localRemote, plan.Branch, plan.Commit);
            var header = setup.Environment.Single(item => item.Value?.StartsWith("Authorization: Basic ", StringComparison.Ordinal) == true);
            Assert(setup.Environment["GIT_ALLOW_PROTOCOL"] == "https" && setup.Environment.Values.Contains("http.followRedirects") &&
                setup.Environment.Values.Contains($"http.{target.PushUrl}.extraHeader") &&
                args.All(arg => !arg.Contains(key, StringComparison.Ordinal)) && args[^1] == $"{plan.Commit}:refs/heads/{Branch}",
                "production push fixes HTTPS target and config and keeps credentials out of argv");
            setup.Environment[header.Key] = "";
            setup.Environment["GIT_ALLOW_PROTOCOL"] = "file"; // Only the test may substitute a disposable local transport.
            var result = await GitProcess.RunAsync(setup, args, token);
            Assert(GitBranchPush.Confirmed(result, plan.Branch), "real Git porcelain confirms exact local fixture ref");
        }

        async Task<string> LocalGitAsync(string cwd, string[] arguments)
        {
            var start = GitProcess.Prepare(GitProcess.RequireExecutable(), cwd, empty);
            start.Environment["GIT_ALLOW_PROTOCOL"] = "file";
            var result = await GitProcess.RunAsync(start, arguments, default);
            Assert(result.Code == 0, "local Git fixture command " + arguments[0]);
            return result.Text.TrimEnd('\r', '\n');
        }

        async Task<string> CommitAsync(string message)
        {
            await LocalGitAsync(source, ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", message]);
            return await LocalGitAsync(source, ["rev-parse", "HEAD"]);
        }
    }

    private sealed class Handler(Func<Request, HttpResponseMessage> reply) : HttpMessageHandler
    {
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            Assert(request.RequestUri is { Scheme: "https", Host: "api.github.com" } &&
                request.Headers.Authorization?.Parameter == Credential, "approved GitHub API request");
            return reply(new(request.Method, request.RequestUri!.AbsolutePath,
                request.Content is null ? null : await request.Content.ReadAsStringAsync(token)));
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

    private static HttpResponseMessage Document(object value, HttpStatusCode status = HttpStatusCode.OK) => new(status)
    { Content = new StringContent(JsonSerializer.Serialize(value), Encoding.UTF8, "application/json") };

    private static async Task ExpectAsync(Task task, string status)
    {
        try { await task; }
        catch (ToolDeniedException ex) when (ex.Status == status) { return; }
        throw new InvalidOperationException("Expected " + status);
    }

    private static void Assert(bool condition, string description)
    {
        if (!condition)
            throw new InvalidOperationException(description);
    }
}
