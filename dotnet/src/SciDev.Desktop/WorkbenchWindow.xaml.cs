using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using Microsoft.Win32;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class WorkbenchWindow : Window
{
    private readonly WorkbenchViewModel _viewModel;
    private readonly WorkbenchReview _review;
    private bool _closeAfterRun;
    private bool _terminalClosing;
    private bool _switchingWorkspace;
    private bool _reviewingLegacyPolicy;
    private SavedProvider? _savedProvider;
    private readonly Action<string>? _rememberWorkspace;

    internal WorkbenchWindow(string workspace) : this(workspace, null) { }

    internal WorkbenchWindow(string workspace, Action<string>? rememberWorkspace)
    {
        _rememberWorkspace = rememberWorkspace;
        InitializeComponent();
        Views.SelectedItem = DocumentsTab;
        _viewModel = new(workspace, FileDesk.CaptureDrafts);
        FileDesk.PythonDebugRequested += StartPythonDebugger;
        FileDesk.DocumentOpened += _viewModel.ShowOpenedFile;
        Closed += (_, _) =>
        {
            _debuggerWindowClosed = true;
            Reader.Clear();
        };
        DataObject.AddPastingHandler(PromptInput, PasteTaskImage);
        _review = new(this);
        DataContext = _viewModel;
        SettingsHost.Children.Remove(AgentSettingsCards);
        SettingsPage.AgentSettings = AgentSettingsCards;
        SettingsPage.PreferencesChanged += ChangeAppearance;
        SettingsPage.ReturnRequested += (_, _) => ReturnToDocuments();
        SettingsPage.ImportRequested += ImportAppearance;
        SettingsPage.UpdatesRequested += OpenUpdates;
        WireDocuments();
        WireDevelopmentTree();
        ApplyAppearance(DesktopPreferences.Default);
        Loaded += async (_, _) =>
        {
            GitDesk.ChangeWorkspace(workspace);
            _viewModel.RefreshQueue();
            await Task.WhenAll(_viewModel.RefreshAsync(), FileDesk.ChangeWorkspaceAsync(workspace),
                TerminalDesk.ChangeWorkspaceAsync(workspace), LoadAppearanceAsync(workspace), Documents.ChangeWorkspaceAsync(workspace));
        };
    }

    internal WorkbenchViewModel ViewModel => _viewModel;

    private void OpenUpdates(object sender, RoutedEventArgs args) => new UpdateDialog(this).ShowDialog();
    internal string EnteredModelKey => Credential.Password;
    internal FrameworkElement CaptureSurface => Surface;

    internal void ShowSettings() => Views.SelectedItem = SettingsTab;
    private void OpenSettings(object sender, RoutedEventArgs e)
    {
        if (_viewModel.IsBusy)
            return;
        ShowSettings();
        SettingsPage.ResetNavigation();
    }

    private void OpenModelSettings(object sender, RoutedEventArgs e) => ShowAgentSettings(ModelSettingsCard);
    private void OpenTaskSettings(object sender, RoutedEventArgs e) => ShowAgentSettings(PolicySettingsCard);

    private void ShowAgentSettings(FrameworkElement section)
    {
        if (_viewModel.IsBusy)
            return;
        ShowSettings();
        SettingsPage.ShowAgentSettings();
        Dispatcher.BeginInvoke(DispatcherPriority.Loaded, new Action(() =>
        {
            if (Views.SelectedItem == SettingsTab)
                section.BringIntoView();
        }));
    }

    private void NewTask(object sender, RoutedEventArgs e)
    {
        if (_viewModel.IsBusy)
            return;
        ReturnToDocuments();
        History.UnselectAll();
        _viewModel.BeginDraft();
        PromptInput.Focus();
    }

    internal void LoadEditorCapture(bool split = false)
    {
        Views.SelectedItem = FilesTab;
        FileDesk.LoadCaptureSample(split);
    }

    internal void LoadClassicCapture(string? source)
    {
        Views.SelectedItem = FilesTab;
        FileDesk.LoadClassicCapture(source);
        if (source is not null)
            ViewModel.ShowOpenedFile(source);
    }

    internal void LoadDiagnosticsCapture()
    {
        Views.SelectedItem = FilesTab;
        FileDesk.LoadDiagnosticCapture();
    }

    internal void LoadTerminalCapture()
    {
        Views.SelectedItem = TerminalTab;
        TerminalDesk.LoadCaptureSample();
    }

    internal void LoadGitCapture()
    {
        Views.SelectedItem = GitChangesTab;
        GitDesk.LoadCaptureSample();
    }

    internal void LoadVersionCapture()
    {
        Views.SelectedItem = GitTab;
        VersionsPage.LoadCapture();
    }

    private async void OpenWorkspace(object sender, RoutedEventArgs e)
    {
        if (_switchingWorkspace || _reviewingLegacyPolicy || _viewModel.IsBusy)
            return;
        var dialog = new OpenFolderDialog { Title = "打开工作区", InitialDirectory = Directory.Exists(_viewModel.Workspace) ? _viewModel.Workspace : "" };
        if (dialog.ShowDialog(this) != true || !FileDesk.ConfirmDiscard())
            return;
        if (!WorkspaceStartup.CanOpen(dialog.FolderName))
        {
            MessageBox.Show(this, "请选择本机磁盘上可读取的普通目录。", "打开工作区");
            return;
        }
        _switchingWorkspace = true;
        FileDesk.SetCurrentValue(IsEnabledProperty, false);
        _savedProvider = null;
        Credential.Clear();
        try
        {
            await TerminalDesk.ChangeWorkspaceAsync(dialog.FolderName);
            VersionsPage.ChangeWorkspace();
            GitDesk.ChangeWorkspace(dialog.FolderName);
            Reader.Clear();
            _documentFocused = false;
            _readingFocus = false;
            await Task.WhenAll(_viewModel.SelectWorkspaceAsync(dialog.FolderName),
                FileDesk.ChangeWorkspaceAsync(dialog.FolderName), LoadAppearanceAsync(dialog.FolderName),
                Documents.ChangeWorkspaceAsync(dialog.FolderName));
            AdaptClassicLayout();
            _rememberWorkspace?.Invoke(_viewModel.Workspace);
            if (Views.SelectedItem == GitTab)
                await RefreshVersionsAsync();
        }
        finally
        {
            FileDesk.SetCurrentValue(IsEnabledProperty, true);
            _switchingWorkspace = false;
        }
    }

    private async void LoadProvider(object sender, RoutedEventArgs e)
    {
        if (_switchingWorkspace)
            return;
        var workspace = _viewModel.Workspace;
        try
        {
            var provider = await Task.Run(() => SavedProvider.Load(workspace));
            if (_switchingWorkspace || _viewModel.IsBusy || _viewModel.Workspace != workspace)
                return;
            var endpoint = WorkspaceRunner.EndpointFor(provider.Address).AbsoluteUri;
            var message = $"运行时将读取 Windows 凭据「{provider.Profile}」并用于以下地址。\n\n{endpoint}\n\n模型  {provider.Model}";
            if (MessageBox.Show(this, message, "使用保存的模型", MessageBoxButton.OKCancel,
                MessageBoxImage.Question, MessageBoxResult.Cancel) != MessageBoxResult.OK)
                return;
            _savedProvider = provider;
            Credential.Clear();
            _viewModel.UseSavedProvider(provider);
        }
        catch (Exception ex) when (ex is ToolDeniedException or ArgumentException or IOException or UnauthorizedAccessException or JsonException)
        {
            MessageBox.Show(this, ex is ToolDeniedException denied ? denied.Message : "无法读取项目模型配置。", "项目模型");
        }
    }

    private async void LoadLegacyPolicy(object sender, RoutedEventArgs e)
    {
        if (_switchingWorkspace || _reviewingLegacyPolicy || _viewModel.IsBusy)
            return;
        _reviewingLegacyPolicy = true;
        var workspace = _viewModel.Workspace;
        try
        {
            var policy = await Task.Run(() => LegacyExecutionPolicy.Read(workspace));
            if (policy is null)
            {
                MessageBox.Show(this, "当前工作区没有旧版权限配置。", "项目权限");
                return;
            }
            var selectedMode = _viewModel.Mode;
            if (policy.Mode is null && selectedMode != ExecutionMode.FullAccess)
            {
                MessageBox.Show(this, "此项目原来使用完全访问。应用配置前，请先在执行模式中显式选择完全访问。", "项目权限");
                return;
            }
            var mode = policy.Mode ?? selectedMode;
            var image = policy.ImageFor(mode);
            var prefixes = policy.PrefixesFor(mode, image);
            var network = policy.EffectiveNetwork == AgentNetworkPolicy.Deny ? "禁止额外联网" : "额外联网需审批";
            var detail = $"执行模式 {policy.Execution}\n联网策略 {network}\n" +
                (image is not null ? $"断网容器镜像 {image}\n" : "容器镜像未启用\n") +
                (policy.Network == "allow" ? "旧版自动联网在工作台仍需逐次审批。\n" : "") +
                (policy.IgnoredPrefixes > 0 ? $"跳过 {policy.IgnoredPrefixes} 条格式不支持的命令前缀。\n" : "") +
                "区外文件、宿主命令、额外联网和自动提交开关将重置。";
            if (await _review.ConfirmLegacyPolicyAsync(workspace, detail, string.IsNullOrEmpty(prefixes) ? "（空）" : prefixes) &&
                !_viewModel.IsBusy && !_switchingWorkspace && _viewModel.Workspace == workspace &&
                (policy.Mode is not null || _viewModel.Mode == selectedMode))
                _viewModel.ApplyLegacyPolicy(policy, mode);
        }
        catch (Exception ex) when (ex is ToolDeniedException or ArgumentException or IOException or UnauthorizedAccessException)
        {
            MessageBox.Show(this, ex is ToolDeniedException or ArgumentException ? ex.Message : "无法安全读取旧版权限配置。", "项目权限");
        }
        finally { _reviewingLegacyPolicy = false; }
    }

    private async void RefreshHistory(object sender, RoutedEventArgs e) => await _viewModel.RefreshAsync();

    private async void OpenHistory(object sender, SelectionChangedEventArgs e)
    {
        if (History.SelectedItem is RunHistory.Entry run)
            await _viewModel.OpenAsync(run);
    }

    private async void ResumePython(object sender, RoutedEventArgs e)
    {
        if (_viewModel.IsBusy || History.SelectedItem is not RunHistory.Entry { Source: "python" } entry || !CanSubmitTask())
            return;
        try { await _viewModel.ResumePythonAsync(entry, ResolveCredential(), _review); }
        catch (Exception ex) when (ex is ArgumentException or ToolDeniedException)
        {
            MessageBox.Show(this, ex.Message, "续跑 Python 会话");
        }
        if (_closeAfterRun && !_viewModel.IsBusy)
            Close();
    }

    private async void RunTask(object sender, RoutedEventArgs e)
    {
        if (!CanSubmitTask())
            return;
        try { await _viewModel.ExecuteAsync(ResolveCredential(), _review); }
        catch (Exception ex) when (ex is ArgumentException or ToolDeniedException)
        {
            MessageBox.Show(this, ex.Message, "项目模型");
        }
        if (_closeAfterRun && !_viewModel.IsBusy)
            Close();
    }

    private async void ContinueQueue(object sender, RoutedEventArgs e)
    {
        if (!CanSubmitTask())
            return;
        await _viewModel.ContinueQueueAsync(ResolveCredential, _review);
        if (_closeAfterRun && !_viewModel.IsBusy)
            Close();
    }

    private void RemoveFromQueue(object sender, RoutedEventArgs e)
    {
        if (!_switchingWorkspace && PendingTasks.SelectedItem is WorkbenchViewModel.QueueItem item)
            _viewModel.RemoveQueued(item, takeBack: false);
    }

    private void TakeTaskBack(object sender, RoutedEventArgs e)
    {
        if (_switchingWorkspace || PendingTasks.SelectedItem is not WorkbenchViewModel.QueueItem item)
            return;
        if ((string.IsNullOrWhiteSpace(_viewModel.Prompt) && !_viewModel.HasImages) || MessageBox.Show(this, "用队列中的任务替换当前草稿？",
            "取回任务", MessageBoxButton.YesNo, MessageBoxImage.Question, MessageBoxResult.No) == MessageBoxResult.Yes)
        {
            _viewModel.RemoveQueued(item, takeBack: true);
            PromptInput.Focus();
        }
    }

    private bool CanSubmitTask() =>
        !_switchingWorkspace && !_reviewingLegacyPolicy && !FileDesk.IsSaving && (!FileDesk.HasUnsavedChanges || MessageBox.Show(this,
            "任务启动时会捕获未保存的草稿。模型可读取限额内的草稿，草稿文件在本任务中只读。继续吗？", "未保存的文件",
            MessageBoxButton.YesNo, MessageBoxImage.Question, MessageBoxResult.No) == MessageBoxResult.Yes);

    private string ResolveCredential()
    {
        var credential = Credential.Password.Trim();
        if (credential.Length == 0 && _savedProvider is { } saved)
        {
            if (WorkspaceRunner.EndpointFor(_viewModel.ApiBase.Trim()).AbsoluteUri !=
                WorkspaceRunner.EndpointFor(saved.Address).AbsoluteUri)
                throw new ToolDeniedException("credential_mismatch", "API 地址已更改，请重新选择项目模型或输入密钥。");
            var stored = ProviderCredential.Read(saved.Profile);
            if (string.IsNullOrWhiteSpace(stored))
                throw new ToolDeniedException("credential_not_found", "找不到项目模型的 Windows 凭据。");
            credential = stored.Trim();
        }
        if (credential.Length == 0 && string.Equals(_viewModel.ApiBase.Trim(),
            Environment.GetEnvironmentVariable("SCIDEV_API_BASE")?.Trim(), StringComparison.Ordinal))
            credential = Environment.GetEnvironmentVariable("SCIDEV_API_KEY")?.Trim() ?? "";
        return credential;
    }

    private void StopTask(object sender, RoutedEventArgs e) => _viewModel.Cancel();

    private void QuickOpen(object sender, KeyEventArgs e)
    {
        if (HandleDebuggerShortcut(e))
            return;
        if (e.Key == Key.Escape && _readingFocus && Views.SelectedItem == DocumentsTab)
        {
            e.Handled = true;
            SetReadingFocus(false);
            return;
        }
        if (Keyboard.Modifiers != ModifierKeys.Control)
            return;
        if (e.Key == Key.Enter && PromptInput.IsKeyboardFocusWithin)
        {
            e.Handled = true;
            RunTask(sender, e);
        }
        else if (e.Key == Key.P && !TerminalDesk.HandlesKeyboard)
        {
            e.Handled = true;
            FocusSearch(sender, e);
        }
    }

    private void OnClosing(object? sender, CancelEventArgs e)
    {
        if (!_viewModel.IsBusy)
        {
            e.Cancel = !FileDesk.ConfirmDiscard();
            _closeAfterRun = false;
            if (!e.Cancel)
            {
                GitDesk.CancelQueries();
                FileDesk.StopEditorDiagnostics();
                FileDesk.StopPythonIntel();
                if (TerminalDesk.IsRunning)
                {
                    e.Cancel = true;
                    if (!_terminalClosing)
                    {
                        _terminalClosing = true;
                        Dispatcher.BeginInvoke(new Action(async () =>
                        {
                            await TerminalDesk.StopAsync();
                            _terminalClosing = false;
                            Close();
                        }));
                    }
                }
            }
            return;
        }
        e.Cancel = true;
        _closeAfterRun = true;
        _viewModel.Cancel();
    }
}
