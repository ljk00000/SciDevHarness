using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading.Channels;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class TerminalPanel : UserControl
{
    private readonly DispatcherTimer _updates;
    private TerminalScreen? _terminal;
    private long _rendered = -1;
    private int _scrollOffset;
    private (int Columns, int Rows)? _resize;
    private (int Column, int Row)? _lastMouse;
    private bool _selecting;
    private ChannelWriter<TerminalSession.Input>? _input;
    private CancellationTokenSource? _stop;
    private Task? _running;
    private string? _workspace;
    private bool _debugging;

    public TerminalPanel()
    {
        InitializeComponent();
        _updates = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromMilliseconds(50) };
        _updates.Tick += (_, _) => RefreshScreen();
        Screen.MetricsChanged += (_, _) => QueueResize();
    }

    public bool IsRunning => _running is { IsCompleted: false };
    internal bool IsDebugging => _debugging && IsRunning;
    internal bool HandlesKeyboard => IsRunning && Screen.IsKeyboardFocusWithin;

    public async Task ChangeWorkspaceAsync(string workspace)
    {
        await StopAsync();
        _workspace = workspace;
        _terminal = null;
        Screen.Frame = null;
        CommandInput.Clear();
        Status.Text = "PowerShell · 手动操作";
    }

    public async Task StopAsync()
    {
        if (_running is not { } running)
            return;
        Status.Text = "正在关闭终端…";
        _stop?.Cancel();
        await running;
    }

    private async void StartTerminal(object sender, RoutedEventArgs e)
    {
        await StartSessionAsync(null, null);
    }

    internal Task StartDebuggerAsync(PythonDebugger.Request request,
        Func<PythonDebugger.Review, CancellationToken, ValueTask<bool>> approve) => StartSessionAsync(request, approve);

    private async Task StartSessionAsync(PythonDebugger.Request? debug,
        Func<PythonDebugger.Review, CancellationToken, ValueTask<bool>>? approve)
    {
        if (_workspace is not { } workspace || _running is not null)
            return;
        _debugging = debug is not null;
        _running = RunAsync(workspace, debug, approve);
        try { await _running; }
        finally { _running = null; _debugging = false; }
    }

    private async Task RunAsync(string workspace, PythonDebugger.Request? debug,
        Func<PythonDebugger.Review, CancellationToken, ValueTask<bool>>? approve)
    {
        using var stop = new CancellationTokenSource();
        _stop = stop;
        var channel = Channel.CreateBounded<TerminalSession.Input>(64);
        _input = channel.Writer;
        _scrollOffset = 0;
        _rendered = -1;
        _resize = null;
        SetActive(true);
        var kind = debug is null ? "终端" : "调试";
        Status.Text = debug is null ? "PowerShell · 宿主机" : $"pdb · {debug.Path} · 第 {debug.Line} 行";
        if (LineMode.IsChecked == true)
            CommandInput.Focus();
        else
            Screen.Focus();
        _updates.Start();
        try
        {
            var (columns, rows) = Screen.Capacity();
            var terminal = new TerminalScreen(columns, rows);
            _terminal = terminal;
            void Read(string text) => terminal.Read(text, reply => channel.Writer.TryWrite(new TerminalSession.Input.Text(reply)));
            var exit = debug is null
                ? await TerminalSession.RunScreenAsync(workspace, columns, rows, channel.Reader, Read, stop.Token)
                : await PythonDebugger.RunAsync(workspace, debug, approve!, columns, rows, channel, Read, stop.Token);
            Status.Text = $"{kind}已退出 · {exit.ExitCode}";
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested) { Status.Text = $"{kind}已关闭"; }
        catch (ToolDeniedException denied) { Status.Text = denied.Message; }
        catch (Exception ex) when (ex is Win32Exception or COMException or IOException or ArgumentException or
            UnauthorizedAccessException or PlatformNotSupportedException)
        {
            Status.Text = ex is Win32Exception windows ? $"{kind}错误 · Windows {windows.NativeErrorCode}" : $"{kind}启动或通信失败";
        }
        finally
        {
            channel.Writer.TryComplete();
            _input = null;
            _stop = null;
            _resize = null;
            _updates.Stop();
            RefreshScreen();
            SetActive(false);
        }
    }

    private void SetActive(bool active)
    {
        StartButton.IsEnabled = !active;
        StopButton.IsEnabled = active;
        InterruptButton.IsEnabled = active;
        LineMode.IsEnabled = active;
        SendButton.IsEnabled = active;
        CommandInput.IsEnabled = active;
        Screen.Active = active;
        DebugControls.Visibility = active && _debugging ? Visibility.Visible : Visibility.Collapsed;
    }

    private void DebugCommand(object sender, RoutedEventArgs e)
    {
        if (sender is Button { Tag: string command })
            SendDebuggerCommand(command);
    }

    internal bool SendDebuggerCommand(string command) => IsDebugging && command is ("c" or "n" or "s" or "r" or "q") &&
        Send(command + "\r");

    private void ToggleLineMode(object sender, RoutedEventArgs e)
    {
        LineInput.Visibility = LineMode.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
        if (LineMode.IsChecked == true)
            CommandInput.Focus();
        else
            Screen.Focus();
    }

    private void SendInput(object sender, RoutedEventArgs e) => SubmitLine();
    private async void StopTerminal(object sender, RoutedEventArgs e) => await StopAsync();
    private void InterruptTerminal(object sender, RoutedEventArgs e) => Send("\x03");

    private void InputKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter)
            return;
        e.Handled = true;
        SubmitLine();
    }

    private void SubmitLine()
    {
        if (!Send(CommandInput.Text + "\r"))
            return;
        CommandInput.Clear();
        CommandInput.Focus();
    }

    private bool Send(string value)
    {
        if (_input is not { } input)
            return false;
        if (!input.TryWrite(new TerminalSession.Input.Text(value)))
        {
            Status.Text = "输入尚未发送，终端正忙。";
            return false;
        }
        if (_scrollOffset > 0)
            ScrollTo(0);
        return true;
    }

    private void ScreenKeyDown(object sender, KeyEventArgs e)
    {
        var key = e.Key == Key.System ? e.SystemKey : e.Key;
        var modifiers = Keyboard.Modifiers;
        if (modifiers == (ModifierKeys.Control | ModifierKeys.Shift) && key is Key.C or Key.V)
        {
            e.Handled = true;
            try
            {
                if (key == Key.C)
                {
                    if (Screen.CopyText() is { Length: > 0 } text)
                        Clipboard.SetText(text);
                }
                else if (_terminal is { } terminal && Clipboard.ContainsText() &&
                         TerminalKeys.Paste(Clipboard.GetText(), terminal.Modes.BracketedPaste) is { Length: > 0 } paste)
                    Send(paste);
            }
            catch (ExternalException) { Status.Text = "剪贴板暂不可用。"; }
            return;
        }
        if (modifiers == ModifierKeys.Shift && key is Key.PageUp or Key.PageDown && Screen.Frame?.Alternate == false)
        {
            e.Handled = true;
            ScrollTo(_scrollOffset + (key == Key.PageUp ? 1 : -1) * (Screen.Frame?.Rows ?? 1));
            return;
        }
        if (_input is not null && _terminal is { } active && !(key == Key.Tab && modifiers.HasFlag(ModifierKeys.Control)) &&
            !(key == Key.F4 && modifiers == ModifierKeys.Alt) &&
            TerminalKeys.Encode(key, modifiers, active.Modes) is { } physical)
        {
            e.Handled = true;
            Send(physical);
        }
    }

    private void ScreenTextInput(object sender, TextCompositionEventArgs e)
    {
        if (_input is not null && e.Text.Length is > 0 and <= 4096 && !e.Text.Any(char.IsControl))
        {
            e.Handled = true;
            Send(e.Text);
        }
    }

    private void ScreenWheel(object sender, MouseWheelEventArgs e)
    {
        e.Handled = true;
        if (!ReportMouse(e.GetPosition(Screen), e.Delta > 0 ? 64 : 65, pressed: true))
            ScrollTo(_scrollOffset + (e.Delta > 0 ? 3 : -3));
    }

    private void ScreenMouseDown(object sender, MouseButtonEventArgs e)
    {
        Screen.Focus();
        if (MouseCode(e.ChangedButton) is { } code)
            e.Handled = ReportMouse(e.GetPosition(Screen), code, pressed: true);
        if (!e.Handled && e.ChangedButton == MouseButton.Left && Screen.BeginSelection(e.GetPosition(Screen)))
        {
            _selecting = Screen.CaptureMouse();
            e.Handled = _selecting;
        }
    }

    private void ScreenMouseUp(object sender, MouseButtonEventArgs e)
    {
        _lastMouse = null;
        if (_selecting && e.ChangedButton == MouseButton.Left)
        {
            Screen.ExtendSelection(e.GetPosition(Screen));
            _selecting = false;
            Screen.ReleaseMouseCapture();
            e.Handled = true;
            return;
        }
        if (MouseCode(e.ChangedButton) is { } code && _terminal?.Modes.MouseMode != 9)
            e.Handled = ReportMouse(e.GetPosition(Screen), code, pressed: false);
    }

    private void ScreenMouseMove(object sender, MouseEventArgs e)
    {
        if (_selecting && Screen.IsMouseCaptured && e.LeftButton == MouseButtonState.Pressed)
        {
            Screen.ExtendSelection(e.GetPosition(Screen));
            return;
        }
        _selecting = false;
        if (_terminal?.Modes.MouseMode is not (1002 or 1003) || !Screen.IsKeyboardFocusWithin)
            return;
        var button = e.LeftButton == MouseButtonState.Pressed ? 0
            : e.MiddleButton == MouseButtonState.Pressed ? 1 : e.RightButton == MouseButtonState.Pressed ? 2 : 3;
        if (button == 3 && _terminal.Modes.MouseMode != 1003)
            return;
        var point = e.GetPosition(Screen);
        if (Screen.Hit(point) is { } hit && hit != _lastMouse)
        {
            _lastMouse = hit;
            ReportMouse(point, 32 + button, pressed: true);
        }
    }

    private bool ReportMouse(Point position, int code, bool pressed)
    {
        if (_input is null || _terminal?.Modes is not { MouseMode: > 0 } modes || _scrollOffset > 0 ||
            Screen.Hit(position) is not { } hit)
            return false;
        var modifiers = Keyboard.Modifiers;
        if (modifiers.HasFlag(ModifierKeys.Shift))
            return false;
        var modifier = (modifiers.HasFlag(ModifierKeys.Alt) ? 8 : 0) + (modifiers.HasFlag(ModifierKeys.Control) ? 16 : 0);
        if (modes.SgrMouse)
            return Send(FormattableString.Invariant($"\u001b[<{code + modifier};{hit.Column};{hit.Row}{(pressed ? 'M' : 'm')}"));
        return hit.Column <= 95 && hit.Row <= 95 &&
            Send("\u001b[M" + (char)(32 + modifier + (pressed ? code : 3)) + (char)(32 + hit.Column) + (char)(32 + hit.Row));
    }

    private static int? MouseCode(MouseButton button) => button switch
    {
        MouseButton.Left => 0, MouseButton.Middle => 1, MouseButton.Right => 2, _ => null,
    };

    private void ResizeTerminal(object sender, SizeChangedEventArgs e) => QueueResize();

    private void QueueResize()
    {
        if (_input is not null && Screen.IsVisible && Screen.ActualWidth > 0 && Screen.ActualHeight > 0)
            _resize = Screen.Capacity();
        else if (_input is null && _terminal is { } screen && Screen.IsVisible && Screen.ActualWidth > 0 && Screen.ActualHeight > 0)
        {
            var (columns, rows) = Screen.Capacity();
            screen.Resize(columns, rows);
            RefreshScreen();
        }
    }

    private void RefreshScreen()
    {
        if (_terminal is not { } screen)
            return;
        if (_resize is { } size && _input?.TryWrite(new TerminalSession.Input.Resize(size.Columns, size.Rows)) == true)
        {
            _resize = null;
            screen.Resize(size.Columns, size.Rows);
        }
        if (screen.Capture(_rendered, _scrollOffset) is { } frame)
        {
            if (frame.Alternate && Screen.Frame?.Alternate != true && CommandInput.IsKeyboardFocusWithin)
                Screen.Focus();
            _rendered = frame.Revision;
            _scrollOffset = frame.ScrollOffset;
            Screen.Frame = frame;
        }
    }

    private void ScrollTo(int offset)
    {
        _scrollOffset = offset;
        _rendered = -1;
        RefreshScreen();
    }

    internal void LoadCaptureSample()
    {
        Status.Text = "PowerShell · 宿主机";
        var (columns, rows) = Screen.Capacity();
        _terminal = new TerminalScreen(columns, rows);
        _terminal.Read("PS C:\\Projects\\SciDevHarness> dotnet build dotnet/SciDevHarness.slnx\r\n\r\n" +
            "\u001b[32m生成成功。\u001b[0m\r\n0 个警告\r\n0 个错误\r\n\r\nPS C:\\Projects\\SciDevHarness> ");
        _rendered = -1;
        RefreshScreen();
        SetActive(true);
    }

    internal void LoadDebuggerCapture()
    {
        _debugging = true;
        Status.Text = "pdb · analysis.py · 第 5 行";
        var (columns, rows) = Screen.Capacity();
        _terminal = new TerminalScreen(columns, rows);
        _terminal.Read("Breakpoint 1 at C:\\Projects\\SciDevHarness\\analysis.py:5\r\n" +
            "> C:\\Projects\\SciDevHarness\\analysis.py(5)<module>()\r\n" +
            "-> result = normalize(observations)\r\n(Pdb) ");
        _rendered = -1;
        RefreshScreen();
        SetActive(true);
    }
}
