using System.Text;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class ExternalFileApprovalChecks
{
    public static async Task RunAsync(DirectoryInfo fixture)
    {
        var workspace = fixture.CreateSubdirectory("workspace").FullName;
        var outside = Path.Combine(fixture.FullName, "notes.txt");
        var bom = new UTF8Encoding(true);
        const string initial = "private\r\napproved part\r\nthird line\r\n";
        await File.WriteAllTextAsync(outside, initial, bom);
        var endpoint = new Uri("https://provider.example.test/openai/v1/chat/completions");

        OutsideWorkspaceTools Tools(Func<OperationReview, CancellationToken, bool> decision) =>
            new(workspace, new WorkspaceFiles(workspace), new Review(decision), endpoint, _ => { }, "fake api key");

        var observed = new List<OperationReview>();
        var read = Call("read_external_file", new { path = outside, start_line = 2, end_line = 2 });
        foreach (var approvals in new[] { 0, 1 })
        {
            observed.Clear();
            var tools = Tools((request, _) =>
            {
                observed.Add(request);
                return observed.Count <= approvals;
            });
            await Denied(tools, read, "approval_denied");
            Require(observed.Count == approvals + 1, "only the approved preceding steps executed");
            Require(observed[0] is { Kind: OperationKind.OpenExternalText, Before: null, Destination: null },
                "first request is permission to inspect");
        }

        observed.Clear();
        var sharing = Tools((request, _) => { observed.Add(request); return true; });
        var shared = await sharing.ExecuteAsync(read, default);
        Require(shared is { Text: "2: approved part", ExternalDataShared: true, Change: null },
            "only the approved excerpt becomes a tool response");
        Require(observed[1] is { Kind: OperationKind.ShareExternalText, Before: "2: approved part" } &&
            observed[1].Destination == endpoint.AbsoluteUri, "review contains exact excerpt and endpoint");
        await Denied(sharing, Call("read_external_file", new { path = Path.Combine(workspace, "inside.txt") }), "path_denied");
        Require(observed.Count == 2, "workspace text can't be addressed through the outside tool");

        var changedDuringReview = Tools((request, _) =>
        {
            if (request.Kind == OperationKind.ShareExternalText)
                File.WriteAllText(outside, "changed during review\r\n", bom);
            return true;
        });
        await Denied(changedDuringReview, read, "edit_conflict");

        await File.WriteAllTextAsync(outside, initial, bom);
        var edit = Call("edit_external_file", new { path = outside, old_text = "approved part", new_text = "new part" });
        using var stopping = new CancellationTokenSource();
        var cancelled = Tools((request, _) =>
        {
            if (request.Kind == OperationKind.EditExternalText)
                stopping.Cancel();
            return true;
        });
        try
        {
            await cancelled.ExecuteAsync(edit, stopping.Token);
            throw new InvalidOperationException("Outside edit ignored cancellation.");
        }
        catch (OperationCanceledException) when (stopping.IsCancellationRequested) { }
        Require(File.ReadAllText(outside) == initial, "cancelled approval preserved original file");

        observed.Clear();
        var revised = await sharing.ExecuteAsync(edit, default);
        var expected = bom.Preamble.ToArray().Concat("private\r\nnew part\r\nthird line\r\n"u8.ToArray());
        Require(observed.Count == 2 && observed[1] is { Kind: OperationKind.EditExternalText, Before: "approved part", After: "new part" }
            && revised.Change == new FileChange(outside, "external_edit") && File.ReadAllBytes(outside).SequenceEqual(expected),
            "approved replacement preserves BOM and CRLF");
    }

    private static ToolCall Call(string name, object args) => new("fixture", name, JsonSerializer.Serialize(args));

    private static async Task Denied(OutsideWorkspaceTools tools, ToolCall call, string status)
    {
        try
        {
            await tools.ExecuteAsync(call, default);
            throw new InvalidOperationException("Expected denial: " + status);
        }
        catch (ToolDeniedException ex) when (ex.Status == status) { }
    }

    private static void Require(bool passed, string description)
    {
        if (!passed)
            throw new InvalidOperationException("Outside approval check failed: " + description);
    }

    private sealed class Review(Func<OperationReview, CancellationToken, bool> decision) : IOperationReview
    {
        public ValueTask<bool> ApproveAsync(OperationReview operation, CancellationToken token) => new(decision(operation, token));
    }
}
