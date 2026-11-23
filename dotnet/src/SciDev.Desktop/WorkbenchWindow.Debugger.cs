using System.Windows;
using System.Windows.Input;
using System.Windows.Threading;
using Microsoft.Win32;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class WorkbenchWindow
{
    private bool _selectingDebugger;
    private bool _debuggerWindowClosed;

    private bool HandleDebuggerShortcut(KeyEventArgs e)
    {
        var key = e.Key == Key.System ? e.SystemKey : e.Key;
        if (TerminalDesk.IsDebugging && (Views.SelectedItem == FilesTab || Views.SelectedItem == TerminalTab))
        {
            var command = (key, Keyboard.Modifiers) switch
            {
                (Key.F5, ModifierKeys.None) => "c",
                (Key.F10, ModifierKeys.None) => "n",
                (Key.F11, ModifierKeys.None) => "s",
                (Key.F11, ModifierKeys.Shift) => "r",
                _ => null,
            };
            if (command is not null)
            {
                TerminalDesk.SendDebuggerCommand(command);
                return e.Handled = true;
            }
        }
        if (key != Key.F5 || Keyboard.Modifiers != ModifierKeys.None || Views.SelectedItem != FilesTab ||
            FileDesk.PythonDebugTarget is null)
            return false;
        e.Handled = true;
        StartPythonDebugger();
        return true;
    }

    private async void StartPythonDebugger()
    {
        if (_selectingDebugger || _switchingWorkspace || _terminalClosing || FileDesk.PythonDebugTarget is not { } target)
            return;
        if (TerminalDesk.IsRunning)
        {
            FileDesk.SetEditorNotice("先关闭当前终端，再启动新的 Python 调试会话。");
            return;
        }
        var workspace = _viewModel.Workspace;
        _selectingDebugger = true;
        string? executable;
        try
        {
            executable = await Task.Run(() => PythonDebugger.FindInterpreter(workspace));
            if (_debuggerWindowClosed || !string.Equals(workspace, _viewModel.Workspace, StringComparison.OrdinalIgnoreCase) ||
                _switchingWorkspace || TerminalDesk.IsRunning)
                return;
            if (executable is null)
            {
                var picker = new OpenFileDialog { Title = "选择 Python 解释器", FileName = "python.exe", Filter = "Windows 可执行文件|*.exe" };
                if (picker.ShowDialog(this) != true)
                    return;
                executable = picker.FileName;
            }
        }
        finally { _selectingDebugger = false; }
        if (FileDesk.PythonDebugTarget != target)
        {
            FileDesk.SetEditorNotice("编辑位置已改变，请从当前光标位置重新启动调试。");
            return;
        }
        Views.SelectedItem = TerminalTab;
        await TerminalDesk.StartDebuggerAsync(new(executable, target.Path, target.Line, target.HasUnsavedChanges), ConfirmPythonDebugAsync);
    }

    private async ValueTask<bool> ConfirmPythonDebugAsync(PythonDebugger.Review review, CancellationToken token) =>
        await Dispatcher.InvokeAsync(() =>
        {
            var note = review.HasUnsavedChanges ? "\n编辑器有未保存修改，本次执行磁盘版本。" : "";
            return MessageBox.Show(this, $"使用当前 Windows 用户权限调试 Python 文件？{note}\n\n" +
                $"工作区 {review.Workspace}\n脚本 {review.Path}\n光标行 {review.Line}\n解释器 {review.Interpreter}" +
                $"\n\n解释器 SHA-256 {review.InterpreterSha256}\n脚本 SHA-256 {review.ScriptSha256}",
                "启动 Python 调试", MessageBoxButton.YesNo, MessageBoxImage.Question, MessageBoxResult.No) == MessageBoxResult.Yes;
        }, DispatcherPriority.Normal, token);

    internal void LoadDebuggerCapture()
    {
        Views.SelectedItem = TerminalTab;
        TerminalDesk.LoadDebuggerCapture();
    }
}
