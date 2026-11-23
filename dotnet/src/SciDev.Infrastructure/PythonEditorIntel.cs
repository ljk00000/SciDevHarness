using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed record PythonCompletion(string Text, string Kind, string Detail);
public sealed record PythonCompletions(int Prefix, IReadOnlyList<PythonCompletion> Items);
public sealed record PythonDefinition(string Path, int Line, int Column);

/// <summary>Runs Jedi in an operator-approved local Python session.</summary>
public sealed class PythonEditorIntel : IAsyncDisposable
{
    public sealed record Review(string Workspace, string Interpreter, string InterpreterSha256);
    private sealed record Response(string Status, string? Version, JsonElement Result);

    private const string DeniedNetwork = "项目断网策略禁止向本机 Python 语言服务发送编辑内容。";
    private static readonly JsonSerializerOptions Wire = new(JsonSerializerDefaults.Web);
    private readonly LocalStdioProcess _process;
    private readonly LanguageServerFrames _frames;
    private readonly WindowsWorkspace _workspace;
    private readonly string _root;
    private readonly SemaphoreSlim _operation = new(1, 1);
    private int _disposed;

    private PythonEditorIntel(string root, WindowsWorkspace workspace, LocalStdioProcess process)
    {
        _root = root;
        _workspace = workspace;
        _process = process;
        _frames = new(process.Input, process.Output);
    }

    public string Version { get; private set; } = "";

    public static async Task<PythonEditorIntel> StartAsync(string workspace, string interpreter,
        Func<Review, CancellationToken, ValueTask<bool>> approve, CancellationToken token)
    {
        workspace = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspace));
        WorkspaceNetworkPolicy.RequireAllowed(workspace, DeniedNetwork);
        var root = new WindowsWorkspace(workspace);
        using var cwd = root.OpenPath("scidev-editor-jedi-cwd");
        if (!Path.GetExtension(interpreter).Equals(".exe", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("请选择本机 Windows Python 解释器。", nameof(interpreter));
        using var executable = await InspectedHostExecutable.OpenAsync(interpreter, token).ConfigureAwait(false);
        if (!await approve(new(workspace, executable.Path, executable.Sha256), token).ConfigureAwait(false))
            throw new ToolDeniedException("approval_denied", "本机 Python 语言服务未获批准。");
        token.ThrowIfCancellationRequested();
        WorkspaceNetworkPolicy.RequireAllowed(workspace, DeniedNetwork);
        executable.VerifyUnchanged();
        using var resource = typeof(PythonEditorIntel).Assembly.GetManifestResourceStream("SciDev.PythonIntelWorker.py")!;
        using var reader = new StreamReader(resource, Encoding.UTF8);
        var worker = await reader.ReadToEndAsync(token).ConfigureAwait(false);
        var system = Environment.SystemDirectory;
        var cacheRoot = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrEmpty(cacheRoot))
            throw new ToolDeniedException("python_cache_unavailable", "无法定位 Windows 本地应用数据目录，Jedi 无法启动。");
        var environment = new Dictionary<string, string>
        {
            ["PATH"] = string.Join(';', system, Path.GetDirectoryName(system), Path.GetDirectoryName(executable.Path)),
            // Parso needs an absolute cache base on Windows even when no parser cache is written.
            ["LOCALAPPDATA"] = cacheRoot,
        };
        var process = LocalStdioProcess.Start(executable.Path, ["-I", "-B", "-u", "-X", "utf8", "-c", worker],
            workspace, environment);
        var session = new PythonEditorIntel(workspace, root, process);
        try
        {
            var greeting = await session.ExchangeAsync(null, token).ConfigureAwait(false);
            if (greeting.Status == "missing_jedi")
                throw new ToolDeniedException("missing_jedi", "当前解释器未安装 Jedi。请选择已有 Jedi 的 Python 解释器。");
            if (greeting.Status == "initialization_failed")
                throw new ToolDeniedException("python_initialization_failed", "Jedi 初始化失败，请检查解释器和已安装的 Jedi。");
            if (greeting is not { Status: "ready", Version.Length: > 0 and <= 32 })
                throw new InvalidDataException("本机 Python 语言服务未就绪。");
            session.Version = greeting.Version;
            return session;
        }
        catch
        {
            await session.DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    public async Task<PythonCompletions> CompleteAsync(string path, string text, int line, int column,
        CancellationToken token = default)
    {
        var result = await AnalyzeAsync("complete", path, text, line, column, token).ConfigureAwait(false);
        var completions = result.Deserialize<PythonCompletions>(Wire);
        if (completions is null || completions.Prefix < 0 || completions.Prefix > column ||
            completions.Items is null || completions.Items.Count > 50 ||
            completions.Items.Any(item => item?.Text is not { Length: > 0 and <= 480 } ||
                item.Text.Any(char.IsControl) || item.Kind is not { Length: <= 64 } || item.Detail is not { Length: <= 480 }))
            throw new InvalidDataException("Python 语言服务返回了无效的补全候选。");
        return completions;
    }

    public async Task<PythonDefinition?> DefinitionAsync(string path, string text, int line, int column,
        CancellationToken token = default)
    {
        var result = await AnalyzeAsync("definition", path, text, line, column, token).ConfigureAwait(false);
        var definition = result.Deserialize<PythonDefinition>(Wire);
        if (definition is null)
            return null;
        if (string.IsNullOrEmpty(definition.Path) || definition.Line < 1 || definition.Column < 1)
            throw new InvalidDataException("Python 语言服务返回了无效的定义位置。");
        try
        {
            var relative = WorkspaceDocuments.VisiblePath(definition.Path);
            using var target = _workspace.OpenOperatorPath(relative)!;
            return WindowsWorkspace.FileExists(target.AbsolutePath) ? definition with { Path = relative } : null;
        }
        catch (Exception ex) when (ex is ToolDeniedException or IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    private async Task<JsonElement> AnalyzeAsync(string operation, string path, string text, int line, int column,
        CancellationToken token)
    {
        if (!path.EndsWith(".py", StringComparison.OrdinalIgnoreCase) || line < 1 || column < 0)
            throw new ArgumentException("请选择 Python 文件中的有效位置。");
        if (Encoding.UTF8.GetByteCount(text) > EditorLanguageServer.MaxDocumentBytes)
            throw new ToolDeniedException("python_document_limit", "Python 补全和定义支持 512 KiB 以内的文本。");
        await _operation.WaitAsync(token).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);
            WorkspaceNetworkPolicy.RequireAllowed(_root, DeniedNetwork);
            using var source = _workspace.OpenOperatorPath(WorkspaceDocuments.VisiblePath(path))!;
            if (!WindowsWorkspace.FileExists(source.AbsolutePath))
                throw new ToolDeniedException("python_source_missing", "当前 Python 文件已移除。");
            var result = await ExchangeAsync(new { operation, path = source.AbsolutePath, text, line, column }, token)
                .ConfigureAwait(false);
            if (result.Status == "analysis_failed")
                throw new ToolDeniedException("python_analysis_failed", "Jedi 无法分析当前位置。");
            if (result.Status != "ok" || result.Result.ValueKind == JsonValueKind.Undefined)
                throw new InvalidDataException("Python 语言服务返回了无效的响应。");
            return result.Result;
        }
        finally { _operation.Release(); }
    }

    private async Task<Response> ExchangeAsync(object? request, CancellationToken token)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(10));
        using var abort = deadline.Token.Register(_process.Abort);
        try
        {
            if (request is not null)
                await _frames.SendAsync(request, deadline.Token).ConfigureAwait(false);
            var packet = await _frames.ReadAsync(deadline.Token).ConfigureAwait(false);
            deadline.Token.ThrowIfCancellationRequested();
            return packet.Deserialize<Response>(Wire) ?? throw new InvalidDataException("Python 语言服务返回了空响应。");
        }
        catch (Exception ex) when (deadline.IsCancellationRequested && ex is IOException or OperationCanceledException)
        {
            token.ThrowIfCancellationRequested();
            throw new TimeoutException("Python 语言服务响应超时。", ex);
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
            return;
        _process.Abort();
        await _operation.WaitAsync().ConfigureAwait(false);
        try { await _process.DisposeAsync().ConfigureAwait(false); }
        finally { _operation.Release(); }
    }
}
