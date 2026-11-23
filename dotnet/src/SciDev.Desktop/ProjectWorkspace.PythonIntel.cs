using System.Windows;
using System.Windows.Threading;
using Microsoft.Win32;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class ProjectWorkspace
{
    private const string PythonLookup = "正在分析当前 Python 文本…";
    private PythonEditorIntel? _pythonIntel;
    private CancellationTokenSource? _pythonLookup;
    private bool _choosePythonIntelInterpreter;

    private void CompletePython(object sender, RoutedEventArgs args) =>
        RequestPythonIntel(_activePane, PythonIntelAction.Complete);

    private void GoToPythonDefinition(object sender, RoutedEventArgs args) =>
        RequestPythonIntel(_activePane, PythonIntelAction.Definition);

    private async void RequestPythonIntel(EditorPane pane, PythonIntelAction action) =>
        await LookupPythonAsync(pane, action, ConfirmPythonIntelAsync);

    private async Task LookupPythonAsync(EditorPane pane, PythonIntelAction action,
        Func<PythonEditorIntel.Review, CancellationToken, ValueTask<bool>> approve)
    {
        if (_pythonLookup is not null || _workspacePath is not { } root || PythonDebugTarget is null ||
            !ReferenceEquals(pane, _activePane) || pane.Current is not { } buffer)
            return;
        if (buffer.Document.TextLength > EditorLanguageServer.MaxDocumentBytes)
        {
            Notice.Text = "Python 补全和定义支持 512 KiB 以内的文本。";
            return;
        }
        var source = buffer.Document.Text;
        if (Window.GetWindow(this) is WorkbenchWindow { EnteredModelKey.Length: > 0 } window &&
            source.Contains(window.EnteredModelKey, StringComparison.Ordinal))
        {
            Notice.Text = "当前缓冲区含模型凭据，分析已暂停。";
            return;
        }
        var position = pane.CaretPosition;
        var version = buffer.Document.Version;
        using var lookup = new CancellationTokenSource();
        _pythonLookup = lookup;
        pane.ClosePythonCompletions();
        Notice.Text = PythonLookup;
        bool Current() => ReferenceEquals(_pythonLookup, lookup) && ReferenceEquals(_activePane, pane) &&
            ReferenceEquals(pane.Current, buffer) && ReferenceEquals(buffer.Document.Version, version) &&
            pane.CaretPosition == position && !_saving && !_reloading;

        try
        {
            if (_pythonIntel is null)
            {
                var interpreter = _choosePythonIntelInterpreter ? null :
                    await Task.Run(() => PythonDebugger.FindInterpreter(root), lookup.Token);
                if (!Current())
                    return;
                interpreter ??= ChoosePythonIntelInterpreter();
                if (interpreter is null)
                    return;
                var service = await Task.Run(() => PythonEditorIntel.StartAsync(root, interpreter, approve, lookup.Token), lookup.Token);
                if (!ReferenceEquals(_pythonLookup, lookup))
                {
                    await ReleasePythonIntelAsync(service);
                    return;
                }
                _pythonIntel = service;
                if (!Current())
                    return;
            }
            var active = _pythonIntel;
            if (action == PythonIntelAction.Complete)
            {
                var result = await Task.Run(() => active.CompleteAsync(buffer.Path, source, position.Line, position.Column,
                    lookup.Token), lookup.Token);
                if (!Current())
                    return;
                Notice.Text = result.Items.Count == 0 ? "当前位置没有补全候选。" : "";
                if (result.Items.Count != 0)
                    pane.ShowPythonCompletions(result);
            }
            else
            {
                var result = await Task.Run(() => active.DefinitionAsync(buffer.Path, source, position.Line, position.Column,
                    lookup.Token), lookup.Token);
                if (!Current())
                    return;
                if (result is null)
                    Notice.Text = "工作区内没有找到定义。";
                else
                    await OpenAsync(result.Path, pane, result, Current);
            }
        }
        catch (OperationCanceledException) when (lookup.IsCancellationRequested) { }
        catch (ToolDeniedException ex)
        {
            if (ReferenceEquals(_pythonLookup, lookup) && ex.Status is "missing_jedi" or "python_initialization_failed")
                _choosePythonIntelInterpreter = true;
            if (Current())
                Notice.Text = ex.Message;
        }
        catch (Exception ex) when (LanguageServerFailed(ex))
        {
            if (ReferenceEquals(_pythonLookup, lookup))
            {
                var lost = _pythonIntel;
                _pythonIntel = null;
                if (Current())
                    Notice.Text = "Python 语言服务连接失败，可重试或检查解释器。";
                if (lost is not null)
                    await ReleasePythonIntelAsync(lost);
            }
        }
        finally
        {
            if (ReferenceEquals(_pythonLookup, lookup))
            {
                _pythonLookup = null;
                if (Notice.Text == PythonLookup)
                    Notice.Text = "";
            }
        }
    }

    private string? ChoosePythonIntelInterpreter()
    {
        var dialog = new OpenFileDialog
        {
            Title = "选择已安装 Jedi 的 Python 解释器",
            FileName = "python.exe",
            Filter = "Windows 可执行文件|*.exe",
        };
        return dialog.ShowDialog(Window.GetWindow(this)) == true ? dialog.FileName : null;
    }

    private async ValueTask<bool> ConfirmPythonIntelAsync(PythonEditorIntel.Review review, CancellationToken token) =>
        await Dispatcher.InvokeAsync(() => MessageBox.Show(Window.GetWindow(this),
            "启动 Jedi 进行补全和跳转到定义？服务以当前 Windows 用户权限运行，可读取编辑草稿和项目导入。\n\n" +
            $"工作区 {review.Workspace}\n解释器 {review.Interpreter}\nSHA-256 {review.InterpreterSha256}",
            "启动本机 Python 语言服务", MessageBoxButton.YesNo, MessageBoxImage.Question,
            MessageBoxResult.No) == MessageBoxResult.Yes, DispatcherPriority.Normal, token);

    internal void StopPythonIntel()
    {
        var lookup = _pythonLookup;
        _pythonLookup = null;
        lookup?.Cancel();
        var service = _pythonIntel;
        _pythonIntel = null;
        if (service is not null)
            _ = ReleasePythonIntelAsync(service);
        _choosePythonIntelInterpreter = false;
        PrimaryDocuments.ClosePythonCompletions();
        SecondaryDocuments.ClosePythonCompletions();
    }

    private static async Task ReleasePythonIntelAsync(PythonEditorIntel service)
    {
        try { await service.DisposeAsync(); }
        catch (Exception ex) when (LanguageServerFailed(ex)) { }
    }
}
