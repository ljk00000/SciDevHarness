using System.Diagnostics;
using System.Text;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class EditorDiagnosticChecks
{
    internal static async Task RunAsync(DirectoryInfo fixture)
    {
        await FramingAsync();
        if (Environment.GetEnvironmentVariable("SCIDEV_TEST_RUFF") is { Length: > 0 } ruff)
            await RuffAsync(fixture.CreateSubdirectory("ruff").FullName, ruff);
        if (Environment.GetEnvironmentVariable("SCIDEV_TEST_NODE") is { Length: > 0 } node)
        {
            await ProtocolAsync(fixture.CreateSubdirectory("protocol").FullName, node);
            if (Environment.GetEnvironmentVariable("SCIDEV_TEST_TYPESCRIPT_LSP") is { Length: > 0 } entry)
                await TypeScriptAsync(fixture.CreateSubdirectory("typescript").FullName, node, entry);
        }
    }

    private static async Task FramingAsync()
    {
        using var frames = new MemoryStream();
        var writer = new LanguageServerFrames(frames, Stream.Null);
        await writer.SendAsync(new { id = 1, text = "你好 🐦" }, default);
        await writer.SendAsync(new { id = 2, result = "second" }, default);
        frames.Position = 0;
        var reader = new LanguageServerFrames(Stream.Null, frames);
        Assert((await reader.ReadAsync(default)).GetProperty("text").GetString() == "你好 🐦" &&
            (await reader.ReadAsync(default)).GetProperty("id").GetInt32() == 2, "framing preserves Unicode and adjacent messages");
        using var duplicate = new MemoryStream(Encoding.ASCII.GetBytes("Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}"));
        try
        {
            await new LanguageServerFrames(Stream.Null, duplicate).ReadAsync(default);
            throw new InvalidOperationException("Duplicate LSP length should be rejected.");
        }
        catch (InvalidDataException) { }
        var eof = JsonSerializer.SerializeToElement(EditorLanguageServer.EndOf("first\r\n🐦"));
        Assert(eof.GetProperty("line").GetInt32() == 1 && eof.GetProperty("character").GetInt32() == 2,
            "incremental replacement ranges count CRLF and UTF-16 columns");
    }

    private static async Task RuffAsync(string root, string executable)
    {
        const string name = "draft #中文 %20.py", original = "VALUE = 1\n";
        var path = Path.Combine(root, name);
        File.WriteAllText(path, original);
        File.WriteAllText(Path.Combine(root, "ruff.toml"), "respect-gitignore = false\n[lint]\nselect = ['F401']\n");
        await using var server = await EditorLanguageServer.StartAsync(root, new(EditorLanguage.Python, executable), Accept, default);
        var lint = await server.DiagnoseAsync(name, "import os\r\nVALUE = 1\n");
        Assert(lint.Any(row => row is { Code: "F401", Line: 1, Column: 8, EndColumn: 10, Severity: "warning" }),
            "actual Ruff reads the unsaved buffer and returns Python oracle coordinates");
        var cleared = await server.DiagnoseAsync(name, original);
        Assert(cleared.Count == 0 && File.ReadAllText(path) == original, "a new buffer revision clears the warning without saving");
        Console.WriteLine("Real Ruff editor diagnostics passed.");
    }

    private static async Task TypeScriptAsync(string root, string node, string entry)
    {
        const string name = "module.ts", original = "export const answer: number = 42;\n";
        var path = Path.Combine(root, name);
        File.WriteAllText(path, original);
        File.WriteAllText(Path.Combine(root, "tsconfig.json"),
            """{"compilerOptions":{"strict":true,"allowJs":true,"checkJs":true,"noEmit":true,"target":"ES2020"},"include":["*.ts","*.js"]}""");
        const string javascript = "checked.js", originalJs = "export {};\n";
        File.WriteAllText(Path.Combine(root, javascript), originalJs);
        await using var server = await EditorLanguageServer.StartAsync(root, new(EditorLanguage.TypeScript, node, entry), Accept, default);
        var diagnostic = await server.DiagnoseAsync(name, "export const answer: number = 'invalid';\n");
        Assert(diagnostic.Any(row => row is { Code: "2322", Line: 1, Severity: "error" }),
            "real TypeScript server diagnoses an unsaved type mismatch");
        Assert((await server.DiagnoseAsync(name, original)).Count == 0 && File.ReadAllText(path) == original,
            "correcting the TypeScript buffer removes the error and leaves the file intact");
        var js = await server.DiagnoseAsync(javascript, "export {};\n/** @type {number} */\nlet amount = 'invalid';\n");
        Assert(js.Any(row => row is { Code: "2322", Line: 3, Severity: "error" }) &&
            File.ReadAllText(Path.Combine(root, javascript)) == originalJs, "the same service checks a JavaScript buffer after a tab switch");
        Console.WriteLine("Real TypeScript editor diagnostics passed.");
    }

    private static async Task ProtocolAsync(string root, string node)
    {
        var script = Path.Combine(root, "fixture.cjs");
        File.WriteAllText(Path.Combine(root, "first.ts"), "");
        File.WriteAllText(Path.Combine(root, "second.ts"), "");
        File.WriteAllText(script, Fixture);
        var command = new EditorServerCommand(EditorLanguage.TypeScript, node, script);
        try
        {
            await EditorLanguageServer.StartAsync(root, command, (_, _) => new(false), default);
            throw new InvalidOperationException("Denied language server started.");
        }
        catch (ToolDeniedException ex) when (ex.Status == "approval_denied") { }
        Assert(!File.Exists(Path.Combine(root, "initialized.txt")), "declined service did not start");

        var previousPolicy = Environment.GetEnvironmentVariable("SCIDEV_NETWORK_POLICY");
        try
        {
            Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", "deny");
            try
            {
                await EditorLanguageServer.StartAsync(root, command,
                    (_, _) => throw new InvalidOperationException("Offline policy must stop before approval."), default);
                throw new InvalidOperationException("Offline workspace started the host language server.");
            }
            catch (ToolDeniedException ex) when (ex.Status == "network_policy_denied") { }
            Assert(!File.Exists(Path.Combine(root, "initialized.txt")), "offline policy prevented launch");
        }
        finally { Environment.SetEnvironmentVariable("SCIDEV_NETWORK_POLICY", previousPolicy); }

        int pid;
        await using (var server = await EditorLanguageServer.StartAsync(root, command, Accept, default))
        {
            var first = await server.DiagnoseAsync("first.ts", "emoji 🐦\r\n");
            var edited = await server.DiagnoseAsync("first.ts", "edited");
            var second = await server.DiagnoseAsync("second.ts", "second");
            pid = int.Parse(first.Single().Code);
            Assert(first.Single().Message == "emoji 🐦\r\n" && edited.Single().Message == "edited" &&
                second.Single().Message == "second", "pending stale versions and unrelated files cannot replace current diagnostics");
            Assert(File.ReadAllText(Path.Combine(root, "first.ts")) == "" && File.ReadAllText(Path.Combine(root, "second.ts")) == "",
                "protocol requested workspace edit was declined");
        }
        try
        {
            using var process = Process.GetProcessById(pid);
            Assert(process.WaitForExit(3000), "closing the editor service terminates its process");
        }
        catch (ArgumentException) { }
    }

    private static ValueTask<bool> Accept(EditorServerReview request, CancellationToken token) =>
        new(request.ExecutableSha256.Length == 64 && (request.Command.EntryScript is null || request.ScriptSha256?.Length == 64));

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }

    private const string Fixture = """
        const fs = require('node:fs');
        const assert = require('node:assert/strict');
        assert.equal(process.env.SCIDEV_API_KEY, undefined);
        assert.equal(process.argv[2], '--stdio');
        fs.writeFileSync('initialized.txt', '');
        let buffer = Buffer.alloc(0);
        let opened;
        function send(packet) {
            const json = Buffer.from(JSON.stringify({jsonrpc:'2.0', ...packet}));
            process.stdout.write(Buffer.concat([Buffer.from('Content-Length: ' + json.length + '\r\n\r\n'), json]));
        }
        function diagnostics(uri, version, message) {
            send({method:'textDocument/publishDiagnostics', params:{uri,version,diagnostics:[
                {range:{start:{line:0,character:0},end:{line:0,character:1}},message,code:process.pid,source:'fixture'}
            ]}});
        }
        function receive(p) {
            if (p.method === 'initialize') {
                assert.equal(p.params.initializationOptions.disableAutomaticTypingAcquisition,true);
                send({id:p.id,result:{capabilities:{textDocumentSync:{change:2}}}});
            }
            if (p.method === 'textDocument/didOpen') {
                opened = p.params.textDocument.uri;
                send({id:'do-not-edit',method:'workspace/applyEdit',params:{edit:{changes:{}}}});
                diagnostics('file:///C:/not-the-active-file.ts', p.params.textDocument.version, 'foreign');
                diagnostics(opened, p.params.textDocument.version, p.params.textDocument.text);
            }
            if (p.id === 'do-not-edit') assert.equal(p.result.applied,false);
            if (p.method === 'textDocument/didChange') {
                const {uri,version} = p.params.textDocument;
                assert.deepEqual(p.params.contentChanges[0].range.end,{line:1,character:0});
                diagnostics(uri,version-1,'stale');
                diagnostics(uri,version,p.params.contentChanges[0].text);
            }
            if (p.method === 'textDocument/didClose') assert.equal(p.params.textDocument.uri,opened);
            if (p.method === 'shutdown') send({id:p.id,result:null});
            if (p.method === 'exit') process.exit(0);
        }
        process.stdin.on('data',chunk => {
            buffer=Buffer.concat([buffer,chunk]);
            for (;;) {
                const header=buffer.indexOf('\r\n\r\n');
                if (header<0) break;
                const size=Number(buffer.subarray(0,header).toString().split(':')[1]);
                if (buffer.length<header+4+size) break;
                const packet=JSON.parse(buffer.subarray(header+4,header+4+size).toString());
                buffer=buffer.subarray(header+4+size);
                receive(packet);
            }
        });
        """;
}
