using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class NetworkApprovalChecks
{
    private const string Url = "https://example.org/docs?query=private-query-marker";
    private const string Key = "fixture-sensitive-model-key";
    private static readonly Uri Model = new("https://model.example/v1/chat/completions");
    private static readonly ToolCall Call = MakeCall(Url);

    public static async Task RunAsync()
    {
        var requests = new List<OperationReview>();
        var entries = new List<RunEvent>();
        var fetched = 0;
        var responseText = new string('好', ApprovedHttpsClient.MaxPreviewChars - 1) + "🐦response-marker";
        var share = false;
        Task<string> Fetch(Uri url, CancellationToken _)
        {
            Check(url.AbsoluteUri == Url, "transport received the approved URL");
            fetched++;
            return Task.FromResult(responseText);
        }

        await Denied(() => new ApprovedNetworkTools(new NoFiles(), null, Model, entries.Add, Key, Fetch)
            .ExecuteAsync(Call, default).AsTask(), "approval_denied");
        Check(entries.Count == 0 && fetched == 0, "disabled approval prevents all I/O");

        var approveFetch = false;
        var tool = new ApprovedNetworkTools(new NoFiles(), new Review(request =>
        {
            requests.Add(request);
            return request.Kind == OperationKind.FetchExternalText ? approveFetch : share;
        }), Model, entries.Add, Key, Fetch);
        await Denied(() => tool.ExecuteAsync(Call, default).AsTask(), "approval_denied");
        Check(requests.Count == 1 && requests[0].Kind == OperationKind.FetchExternalText &&
            requests[0].Target == Url && fetched == 0, "first denial prevents fetch");
        approveFetch = true;
        await Denied(() => tool.ExecuteAsync(Call, default).AsTask(), "approval_denied");
        Check(requests.Count == 3 && fetched == 1, "second denial occurs after one fetch");
        share = true;
        var result = await tool.ExecuteAsync(Call, default);
        var final = requests[^1];
        Check(fetched == 2 && requests.Count == 5 && final.Kind == OperationKind.ShareNetworkText &&
            final.Destination == Model.AbsoluteUri && final.Before == result.Text && result.ExternalDataShared &&
            result.Text == responseText[..(ApprovedHttpsClient.MaxPreviewChars - 1)],
            "second approval shares only the exact, rune-safe preview with the stated destination");
        Check(entries.Count == 10 && entries.Count(entry => entry.Type == "network_review_decided") == 5,
            "both approval stages are audited");
        var log = JsonSerializer.Serialize(entries);
        Check(!log.Contains("private-query-marker") && !log.Contains("response-marker") && !log.Contains("example.org") &&
            !log.Contains(Key), "audit excludes URL, contents and credentials");

        await Denied(() => tool.ExecuteAsync(MakeCall("https://example.org/?key=%66ixture-sensitive-model-key"), default)
            .AsTask(), "network_credential");
        responseText = "response " + Key;
        await Denied(() => tool.ExecuteAsync(Call, default).AsTask(), "network_credential");
        Check(requests.Count == 6 && fetched == 3, "credentials never reach the fetcher or the share preview");

        using var stop = new CancellationTokenSource();
        var cancelled = new ApprovedNetworkTools(new NoFiles(), new Review(_ => { stop.Cancel(); return true; }), Model, _ => { }, Key, Fetch);
        try
        {
            await cancelled.ExecuteAsync(Call, stop.Token);
            throw new InvalidOperationException("Expected cancellation.");
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested) { }
        Check(fetched == 3, "cancelling an approval prevents I/O");

        foreach (var target in new[] { "http://example.org/", "https://example.org:8443/", "https://127.0.0.1/",
            "https://[::1]/", "https://service.local/", "https://user@example.org/", "https://example.org/#fragment" })
            await Denied(() => tool.ExecuteAsync(MakeCall(target), default).AsTask(), "network_target_denied");
        foreach (var blocked in new[] { "0.1.2.3", "10.1.2.3", "100.64.0.1", "127.0.0.42", "169.254.169.254",
            "172.31.255.255", "192.0.0.8", "192.168.12.1", "198.18.0.1", "224.0.0.1", "::1", "::ffff:127.0.0.1",
            "64:ff9b::a00:1", "2001:db8::1", "2002:a00:100::", "3fff::1", "fc00::1", "fe80::1" })
            Check(!ApprovedHttpsClient.IsPublicAddress(IPAddress.Parse(blocked)), "nonpublic address denied");
        Check(ApprovedHttpsClient.IsPublicAddress(IPAddress.Parse("1.1.1.1")) &&
            ApprovedHttpsClient.IsPublicAddress(IPAddress.Parse("2606:4700::1111")), "public addresses allowed");

        using var plain = new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("甲\u202e乙") };
        Check(await ApprovedHttpsClient.ReadTextAsync(plain, default) == "甲乙", "invisible formatting removed from preview");
        using var redirect = new HttpResponseMessage(HttpStatusCode.Redirect);
        await Denied(() => ApprovedHttpsClient.ReadTextAsync(redirect, default), "network_http_status");
        using var large = new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(new string('x', ApprovedHttpsClient.MaxBytes + 1)),
        };
        await Denied(() => ApprovedHttpsClient.ReadTextAsync(large, default), "network_response_limit");
        using var invalid = new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent([0xff]) };
        invalid.Content.Headers.ContentType = new MediaTypeHeaderValue("text/plain");
        await Denied(() => ApprovedHttpsClient.ReadTextAsync(invalid, default), "network_content_type");
        using var binary = new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("data", Encoding.UTF8, "application/octet-stream") };
        await Denied(() => ApprovedHttpsClient.ReadTextAsync(binary, default), "network_content_type");
    }

    private static ToolCall MakeCall(string url) => new("test", "fetch_external_text", JsonSerializer.Serialize(new { url }));

    private static async Task Denied(Func<Task> action, string expected)
    {
        try { await action(); }
        catch (ToolDeniedException ex) when (ex.Status == expected) { return; }
        throw new InvalidOperationException("Expected HTTPS denial: " + expected);
    }

    private static void Check(bool condition, string description)
    {
        if (!condition)
            throw new InvalidOperationException(description);
    }

    private sealed class Review(Func<OperationReview, bool> decide) : IOperationReview
    {
        public ValueTask<bool> ApproveAsync(OperationReview operation, CancellationToken token) => new(decide(operation));
    }

    private sealed class NoFiles : IFileTools
    {
        public ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token) => throw new InvalidOperationException(call.Name);
    }
}
