using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;
using System.Windows.Threading;
using Microsoft.Win32;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class ProjectWorkspace
{
    private readonly Dictionary<EditorLanguage, EditorLanguageServer> _languageServers = [];
    private DispatcherTimer? _diagnosticTimer;
    private CancellationTokenSource? _languageLaunch;
    private EditorBuffer? _diagnosticBuffer;
    private bool _diagnosticsVisible;
    private bool _diagnosticBusy;
    private int _diagnosticRevision;

    private EditorLanguage? ActiveLanguage => _diagnosticBuffer?.Snapshot is not null
        ? EditorLanguageServer.ForFile(_diagnosticBuffer.Path) : null;

    private record DiagnosticRow(EditorDiagnostic Diagnostic)
    {
        public string Location => $"{Label(Diagnostic.Severity)} {Diagnostic.Line}:{Diagnostic.Column}";
        public string Detail => string.Join(" · ", new[] { Diagnostic.Code, Diagnostic.Message.Split('\n', 2)[0].TrimEnd('\r') }
            .Where(part => part.Length != 0));

        private static string Label(string severity) => severity switch
        {
            "warning" => "警告", "info" => "信息", "hint" => "提示", _ => "错误",
        };
    }

    private void ObserveDiagnosticBuffer(EditorBuffer? buffer)
    {
        if (ReferenceEquals(_diagnosticBuffer, buffer))
        {
            DiagnosticToggle.IsEnabled = _diagnosticsVisible || ActiveLanguage is not null;
            return;
        }
        if (_diagnosticBuffer is not null)
            _diagnosticBuffer.Document.TextChanged -= DiagnosticTextChanged;
        _diagnosticBuffer = buffer;
        if (buffer is not null)
            buffer.Document.TextChanged += DiagnosticTextChanged;
        DiagnosticToggle.IsEnabled = _diagnosticsVisible || ActiveLanguage is not null;
        QueueDiagnostics();
    }

    private void DiagnosticTextChanged(object? sender, EventArgs args) => QueueDiagnostics();

    private void QueueDiagnostics()
    {
        _diagnosticRevision++;
        if (!_diagnosticsVisible)
            return;
        DiagnosticItems.ItemsSource = null;
        DiagnosticItems.Visibility = Visibility.Collapsed;
        _diagnosticTimer?.Stop();
        var language = ActiveLanguage;
        var running = language is { } value && _languageServers.ContainsKey(value);
        DiagnosticEnable.Visibility = language is not null && !running ? Visibility.Visible : Visibility.Collapsed;
        DiagnosticEnable.IsEnabled = _languageLaunch is null;
        DiagnosticStatus.Text = language is null ? "选择 Python、JavaScript 或 TypeScript 文件。" :
            !running ? "启用本机服务后可诊断当前文件。" : "等待当前文本的诊断…";
        if (!running)
            return;
        if (_diagnosticTimer is null)
        {
            _diagnosticTimer = new DispatcherTimer(DispatcherPriority.Background, Dispatcher)
                { Interval = TimeSpan.FromMilliseconds(400) };
            _diagnosticTimer.Tick += RefreshDiagnostics;
        }
        _diagnosticTimer.Start();
    }

    private async void ToggleDiagnostics(object sender, RoutedEventArgs args)
    {
        if (_diagnosticsVisible)
        {
            StopEditorDiagnostics();
            return;
        }
        _diagnosticsVisible = true;
        DiagnosticPanel.Visibility = Visibility.Visible;
        DiagnosticToggle.Content = "隐藏诊断";
        QueueDiagnostics();
        await EnableActiveLanguageAsync();
    }

    private async void EnableDiagnostics(object sender, RoutedEventArgs args) => await EnableActiveLanguageAsync();

    private async Task EnableActiveLanguageAsync()
    {
        if (_languageLaunch is not null || _workspacePath is not { } root ||
            ActiveLanguage is not { } language || _languageServers.ContainsKey(language))
            return;
        using var launch = new CancellationTokenSource();
        _languageLaunch = launch;
        DiagnosticEnable.IsEnabled = false;
        var status = "";
        try
        {
            var command = await Task.Run(() => EditorLanguageServer.Discover(root, language), launch.Token);
            launch.Token.ThrowIfCancellationRequested();
            command ??= ChooseLanguageServer(language);
            if (command is null)
                return;
            DiagnosticStatus.Text = $"正在检查 {command.Name}…";
            var server = await Task.Run(() => EditorLanguageServer.StartAsync(root, command, ConfirmLanguageServerAsync, launch.Token),
                launch.Token);
            if (launch.IsCancellationRequested || !_diagnosticsVisible || !string.Equals(_workspacePath, root, StringComparison.OrdinalIgnoreCase))
            {
                await ReleaseLanguageServerAsync(server);
                return;
            }
            _languageServers.Add(language, server);
        }
        catch (OperationCanceledException) when (launch.IsCancellationRequested) { }
        catch (Exception ex) when (LanguageServerFailed(ex))
        {
            status = ex is ToolDeniedException denied ? denied.Message : "语言服务启动失败，请核对程序和本地配置。";
        }
        finally
        {
            if (ReferenceEquals(_languageLaunch, launch))
            {
                _languageLaunch = null;
                QueueDiagnostics();
                if (_diagnosticsVisible && ActiveLanguage == language && status.Length > 0)
                    DiagnosticStatus.Text = status;
            }
        }
    }

    private EditorServerCommand? ChooseLanguageServer(EditorLanguage language)
    {
        var owner = Window.GetWindow(this);
        var executable = new OpenFileDialog
        {
            Title = language == EditorLanguage.Python ? "选择本机 Ruff 程序" : "选择本机 Node.js 程序",
            FileName = language == EditorLanguage.Python ? "ruff.exe" : "node.exe",
            Filter = "Windows 可执行文件|*.exe",
        };
        if (executable.ShowDialog(owner) != true)
            return null;
        if (language == EditorLanguage.Python)
            return new(language, executable.FileName);
        var entry = new OpenFileDialog
        {
            Title = "选择已安装的 TypeScript Language Server 入口",
            Filter = "JavaScript 入口|*.mjs;*.cjs;*.js",
        };
        return entry.ShowDialog(owner) == true ? new(language, executable.FileName, entry.FileName) : null;
    }

    private async ValueTask<bool> ConfirmLanguageServerAsync(EditorServerReview review, CancellationToken token) =>
        await Dispatcher.InvokeAsync(() =>
        {
            var command = review.Command;
            var entry = command.EntryScript is null ? "" : $"\n入口 {command.EntryScript}\nSHA-256 {review.ScriptSha256}";
            return MessageBox.Show(Window.GetWindow(this),
                $"运行 {command.Name} 以诊断当前工作区的编辑内容？程序以当前 Windows 用户权限运行。\n\n" +
                $"工作区 {review.Workspace}\n程序 {command.Executable}\nSHA-256 {review.ExecutableSha256}{entry}",
                "启动本机语言服务", MessageBoxButton.YesNo, MessageBoxImage.Question, MessageBoxResult.No) == MessageBoxResult.Yes;
        }, DispatcherPriority.Normal, token);

    private async void RefreshDiagnostics(object? sender, EventArgs args)
    {
        _diagnosticTimer?.Stop();
        if (_diagnosticBusy)
        {
            _diagnosticTimer?.Start();
            return;
        }
        if (!_diagnosticsVisible || _diagnosticBuffer is not { } buffer ||
            ActiveLanguage is not { } language || !_languageServers.TryGetValue(language, out var server))
            return;
        var revision = _diagnosticRevision;
        _diagnosticBusy = true;
        try
        {
            var text = buffer.Document.TextLength <= EditorLanguageServer.MaxDocumentBytes ? buffer.Document.Text : null;
            if (text is null)
            {
                DiagnosticStatus.Text = "诊断支持 512 KiB 以内的文本。";
                return;
            }
            if (Window.GetWindow(this) is WorkbenchWindow { EnteredModelKey.Length: > 0 } workbench &&
                text.Contains(workbench.EnteredModelKey, StringComparison.Ordinal))
            {
                DiagnosticStatus.Text = "当前缓冲区含模型凭据，诊断已暂停。";
                return;
            }
            var result = await Task.Run(() => server.DiagnoseAsync(buffer.Path, text));
            if (revision == _diagnosticRevision)
                ShowDiagnostics(result);
        }
        catch (ToolDeniedException ex)
        {
            if (revision == _diagnosticRevision)
                DiagnosticStatus.Text = ex.Message;
        }
        catch (Exception ex) when (LanguageServerFailed(ex))
        {
            if (_languageServers.TryGetValue(language, out var active) && ReferenceEquals(active, server))
            {
                _languageServers.Remove(language);
                await ReleaseLanguageServerAsync(server);
                if (_diagnosticsVisible && ActiveLanguage == language)
                {
                    QueueDiagnostics();
                    DiagnosticStatus.Text = "语言服务连接已结束，可重新启用。";
                }
            }
        }
        finally { _diagnosticBusy = false; }
    }

    private void ShowDiagnostics(IReadOnlyList<EditorDiagnostic> result)
    {
        DiagnosticItems.ItemsSource = result.Select(item => new DiagnosticRow(item)).ToArray();
        DiagnosticItems.Visibility = result.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        DiagnosticStatus.Text = result.Count == 0 ? "当前文件没有诊断。" : $"当前文件 · {result.Count} 条诊断";
    }

    private void JumpToDiagnostic(object sender, MouseButtonEventArgs args) => RevealSelectedDiagnostic();
    private void DiagnosticKey(object sender, KeyEventArgs args)
    {
        if (args.Key == Key.Enter)
            RevealSelectedDiagnostic();
    }

    private void RevealSelectedDiagnostic()
    {
        if (ReferenceEquals(_activePane.Current, _diagnosticBuffer) && DiagnosticItems.SelectedItem is DiagnosticRow row)
            _activePane.Reveal(row.Diagnostic);
    }

    internal void StopEditorDiagnostics()
    {
        var launch = _languageLaunch;
        _languageLaunch = null;
        launch?.Cancel();
        foreach (var server in _languageServers.Values)
            _ = ReleaseLanguageServerAsync(server);
        _languageServers.Clear();
        _diagnosticRevision++;
        _diagnosticTimer?.Stop();
        _diagnosticsVisible = false;
        DiagnosticItems.ItemsSource = null;
        DiagnosticPanel.Visibility = Visibility.Collapsed;
        DiagnosticToggle.Content = "诊断";
        DiagnosticToggle.IsEnabled = ActiveLanguage is not null;
    }

    private static async Task ReleaseLanguageServerAsync(EditorLanguageServer server)
    {
        server.Abort();
        try { await server.DisposeAsync(); }
        catch (Exception ex) when (LanguageServerFailed(ex)) { }
    }

    private static bool LanguageServerFailed(Exception error) => error is
        IOException or InvalidDataException or UnauthorizedAccessException or Win32Exception or JsonException or
        ArgumentException or TimeoutException or InvalidOperationException or ToolDeniedException or OperationCanceledException;

    internal void LoadDiagnosticCapture()
    {
        PrimaryDocuments.Open(new("experiment/normalize.py", "import os\n\nVALUE = 1\n"));
        _diagnosticsVisible = true;
        DiagnosticPanel.Visibility = Visibility.Visible;
        DiagnosticToggle.Content = "隐藏诊断";
        DiagnosticToggle.IsEnabled = true;
        DiagnosticEnable.Visibility = Visibility.Collapsed;
        ShowDiagnostics([new(1, 8, 1, 10, "warning", "“os” imported but unused", "F401", "Ruff")]);
    }
}
