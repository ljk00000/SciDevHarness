using SciDev.Core;
using SciDev.Infrastructure;

internal static class PythonEditorIntelChecks
{
    public static async Task RunAsync(DirectoryInfo fixture)
    {
        if (Environment.GetEnvironmentVariable("SCIDEV_TEST_JEDI_PYTHON") is not { Length: > 0 } python)
            return;
        var root = fixture.FullName;
        const string name = "分析 & editor.py";
        const string original = "SAVED = True\n";
        var file = Path.Combine(root, name);
        File.WriteAllText(file, original);
        File.WriteAllText(Path.Combine(root, "utilities.py"), "# sample\n\ndef dataset():\n    return 4\n");

        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(45));
        var token = deadline.Token;
        await DeniedAsync(PythonEditorIntel.StartAsync(root, python, (_, _) => new(false), token), "approval_denied");
        var approvals = 0;
        await using var service = await PythonEditorIntel.StartAsync(root, python, (review, _) =>
        {
            Check(review.Workspace == root && review.InterpreterSha256.Length == 64, "reviewed interpreter and workspace");
            approvals++;
            return new(true);
        }, token);
        Check(service.Version.Length > 0 && approvals == 1, "real Jedi worker started after approval");

        const string member = "value = '🪶'; items.ap";
        var completion = await service.CompleteAsync(name, "items = []\r\n" + member, 2, member.Length, token);
        Check(completion.Prefix == 2 && completion.Items.Any(item => item.Text == "append" && item.Kind.Length != 0),
            "Jedi resolves the draft member with a UTF-16 cursor and CRLF");

        const string local = "prefix = '🪶'; local_value = 3";
        var definition = await service.DefinitionAsync(name, local + "\nlocal_value\n", 2, 4, token);
        Check(definition == new PythonDefinition(name, 1, local.IndexOf("local_value", StringComparison.Ordinal) + 1),
            "Jedi returns the UTF-16 column of a definition in the active draft");

        var crossFile = await service.DefinitionAsync(name,
            "from utilities import dataset\nresult = dataset()\n", 2, 12, token);
        Check(crossFile == new PythonDefinition("utilities.py", 3, 5), "Jedi follows a workspace import");
        var outside = await service.DefinitionAsync(name, "from pathlib import Path\nresult = Path('.')\n", 2, 11, token);
        Check(outside is null, "outside definitions are excluded");
        await DeniedAsync(service.CompleteAsync("../other.py", "a", 1, 1, token), "path_denied");
        await DeniedAsync(service.CompleteAsync(name, new string('界', EditorLanguageServer.MaxDocumentBytes / 2), 1, 0, token),
            "python_document_limit");
        var previous = Environment.GetEnvironmentVariable("SCIDEV_NETWORK_POLICY");
        try
        {
            Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", "deny");
            await DeniedAsync(service.CompleteAsync(name, "a", 1, 1, token), "network_policy_denied");
        }
        finally { Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", previous); }
        Check(File.ReadAllText(file) == original, "Jedi leaves saved bytes intact");

        await service.DisposeAsync();
        try
        {
            await service.CompleteAsync(name, "a", 1, 1, token);
            throw new InvalidOperationException("Disposed Jedi worker accepted a request.");
        }
        catch (ObjectDisposedException) { }
        Console.WriteLine($"Real Jedi {service.Version} editor checks passed.");
    }

    private static async Task DeniedAsync(Task task, string status)
    {
        try { await task; }
        catch (ToolDeniedException error) when (error.Status == status) { return; }
        throw new InvalidOperationException($"Jedi editor check expected {status}.");
    }

    private static void Check(bool condition, string description)
    {
        if (!condition)
            throw new InvalidOperationException("Jedi editor check failed: " + description);
    }
}
