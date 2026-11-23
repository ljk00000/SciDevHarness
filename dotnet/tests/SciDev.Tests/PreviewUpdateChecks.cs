using System.Net;
using System.Security.Cryptography;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class PreviewUpdateChecks
{
    private const string Repo = "example/updates";
    private const string Tag = "csharp-preview-v999.2.0";
    private const string Name = "SciDevHarness-CSharp-Preview-Setup-999.2.0.exe";
    private static readonly byte[] Data = "local update fixture"u8.ToArray();
    private static readonly string Digest = Convert.ToHexStringLower(SHA256.HashData(Data));

    public static async Task RunAsync(DirectoryInfo root)
    {
        var decisions = new Review();
        var downloads = new List<Uri>();
        var checksum = Digest;
        var redirected = "https://release-assets.githubusercontent.com/asset?fixture=temporary";
        var malformedSize = false;
        var reportedDigest = "sha256:" + Digest;
        var updater = new PreviewUpdates(root.FullName, decisions, _ => new HttpClient(new Handler(request =>
        {
            var url = request.RequestUri!;
            downloads.Add(url);
            Assert(request.Headers.Authorization is null && !request.Headers.Contains("Cookie"), "update did not pass credentials");
            if (url.Host == "api.github.com")
            {
                Assert(url.Query == "?per_page=30", "bounded release metadata request");
                return Metadata(reportedDigest);
            }
            if (url.Host == "github.com")
                return new(HttpStatusCode.Redirect) { Headers = { Location = new Uri(redirected) } };
            var blob = checksum == Digest ? Data : new byte[Data.Length];
            var response = new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(blob) };
            if (malformedSize)
                response.Content.Headers.ContentLength = Data.Length - 1;
            return response;
        })));

        var offline = new PreviewUpdates(root.FullName, decisions, denyAdditionalNetwork: true);
        await Rejected(() => offline.CheckAsync(Repo, default), "network_policy_denied");
        Assert(decisions.Seen.Count == 0, "offline policy blocked before approval");
        var profile = root.CreateSubdirectory(".research");
        var policy = Path.Combine(profile.FullName, "settings.json");
        File.WriteAllText(policy, "{\"network_policy\":\"deny\"}");
        await Rejected(() => updater.CheckAsync(Repo, default), "network_policy_denied");
        Assert(decisions.Seen.Count == 0, "saved workspace policy was enforced");
        File.Delete(policy);
        decisions.Allow = false;
        await Rejected(() => updater.CheckAsync(Repo, default), "denied");
        Assert(downloads.Count == 0, "refusing metadata prevented request");
        var exception = new PreviewUpdates(root.FullName, decisions, allowNetwork: true, denyAdditionalNetwork: true);
        await Rejected(() => exception.CheckAsync(Repo, default), "denied");
        Assert(decisions.Seen[^1].Detail.Contains("覆盖工作区断网策略", StringComparison.Ordinal),
            "explicit network exception was shown during approval");
        decisions.Allow = true;
        var release = await updater.CheckAsync(Repo, default);
        Assert(release is { Available: true, Latest: "999.2.0", AssetName: Name } &&
            release.AssetSize == Data.Length && release.AssetDigest == reportedDigest, "selected only matching C# installer");

        var target = Path.Combine(root.FullName, Name);
        var attempted = downloads.Count;
        var approvals = decisions.Seen.Count;
        await Rejected(() => updater.DownloadAsync(release, @"\\unapproved.invalid\share\setup.exe", null, null, default),
            "path_denied");
        Assert(decisions.Seen.Count == approvals && downloads.Count == attempted, "UNC destination rejected before approval and HTTP");
        await Rejected(() => updater.DownloadAsync(release, target, new string('b', 64), null, default), "update_failed");
        Assert(downloads.Count == attempted, "disagreement with published digest prevented download");

        decisions.RejectHost = "release-assets.githubusercontent.com";
        await Rejected(() => updater.DownloadAsync(release, target, null, null, default), "denied");
        Assert(downloads.Count == attempted + 1 && !File.Exists(target), "refusing redirect prevented second request and write");
        decisions.RejectHost = null;
        redirected = "http://release-assets.githubusercontent.com/asset";
        await Rejected(() => updater.DownloadAsync(release, target, null, null, default), "update_failed");
        Assert(downloads.Count == attempted + 2 && !File.Exists(target), "HTTPS downgrade was blocked");
        redirected = "https://release-assets.githubusercontent.com/asset?fixture=temporary";

        checksum = "";
        await Rejected(() => updater.DownloadAsync(release, target, null, null, default), "update_failed");
        Assert(!File.Exists(target) && !root.EnumerateFiles("*.part").Any(), "digest failure cleaned up temporary content");
        checksum = Digest;
        malformedSize = true;
        await Rejected(() => updater.DownloadAsync(release, target, null, null, default), "update_failed");
        malformedSize = false;
        Assert(!File.Exists(target) && !root.EnumerateFiles("*.part").Any(), "size mismatch did not create a package");

        using (var stop = new CancellationTokenSource())
        {
            try
            {
                await updater.DownloadAsync(release, target, null, new InlineProgress(stop.Cancel), stop.Token);
                throw new InvalidOperationException("Expected download cancellation.");
            }
            catch (OperationCanceledException) { }
        }
        Assert(!File.Exists(target) && !root.EnumerateFiles("*.part").Any(), "cancellation cleaned up temporary content");

        reportedDigest = "";
        release = await updater.CheckAsync(Repo, default);
        await Rejected(() => updater.DownloadAsync(release, target, null, null, default), "update_failed");
        var saved = await updater.DownloadAsync(release, root.FullName, Digest.ToUpperInvariant(), null, default);
        Assert(saved.Path == target && saved.Sha256 == Digest && File.ReadAllBytes(target).SequenceEqual(Data),
            "explicit digest fallback and existing directory download");
        attempted = downloads.Count;
        await Rejected(() => updater.DownloadAsync(release, target, Digest, null, default), "update_failed");
        Assert(downloads.Count == attempted && File.ReadAllBytes(target).SequenceEqual(Data), "existing file was preserved");

        decisions.Allow = false;
        await Rejected(() => updater.LaunchAsync(saved, default), "denied");
        Assert(decisions.Seen[^1] is { Kind: OperationKind.LaunchUpdate, Before: var digest } && digest == Digest,
            "installer launch had separate approval");
        var started = 0;
        var result = await UpdateAssetFiles.LaunchAsync(saved, path =>
        {
            Assert(path == target, "verified filename was passed to the launcher");
            return ++started;
        }, default);
        Assert(result == 1 && started == 1, "verified bytes reached the test launcher");
        File.WriteAllBytes(target, new byte[Data.Length]);
        await Rejected(() => UpdateAssetFiles.LaunchAsync(saved, _ => ++started, default), "update_failed");
        Assert(started == 1, "tampering was caught before process start");
        Assert(decisions.Seen.All(item => item.Destination?.Contains("temporary", StringComparison.Ordinal) != true),
            "signed redirect query was excluded from approval text");
    }

    private static HttpResponseMessage Metadata(string digest)
    {
        object Asset(string name, string tag, string repository) => new
        {
            name, state = "uploaded", size = Data.Length, digest,
            browser_download_url = $"https://github.com/{repository}/releases/download/{tag}/{name}",
        };
        const string wrong = "csharp-preview-v999.3.0";
        var releases = new[]
        {
            new { tag_name = "v999.4.0", draft = false, assets = new[] { Asset("SciDevHarness-Setup-999.4.0.exe", "v999.4.0", Repo) } },
            new { tag_name = wrong, draft = false, assets = new[] { Asset("SciDevHarness-CSharp-Preview-Setup-999.3.0.exe", wrong, "other/repo") } },
            new { tag_name = Tag, draft = true, assets = new[] { Asset(Name, Tag, Repo) } },
            new { tag_name = Tag, draft = false, assets = new[] { Asset(Name, Tag, Repo) } },
        };
        return new(HttpStatusCode.OK) { Content = new StringContent(JsonSerializer.Serialize(releases)) };
    }

    private static async Task Rejected(Func<Task> action, string status)
    {
        try { await action(); }
        catch (ToolDeniedException error) when (error.Status == status) { return; }
        throw new InvalidOperationException($"Expected update rejection {status}.");
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }

    private sealed class Handler(Func<HttpRequestMessage, HttpResponseMessage> reply) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(reply(request));
    }

    private sealed class InlineProgress(Action cancel) : IProgress<(long Done, long Total)>
    {
        public void Report((long Done, long Total) value) => cancel();
    }

    private sealed class Review : IOperationReview
    {
        public bool Allow { get; set; } = true;
        public string? RejectHost { get; set; }
        public List<OperationReview> Seen { get; } = [];

        public ValueTask<bool> ApproveAsync(OperationReview operation, CancellationToken token)
        {
            Seen.Add(operation);
            return ValueTask.FromResult(Allow && (RejectHost is null || operation.Destination != RejectHost));
        }
    }
}
