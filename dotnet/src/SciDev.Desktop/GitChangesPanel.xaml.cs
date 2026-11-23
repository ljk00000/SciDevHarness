using System.ComponentModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class GitChangesPanel : UserControl
{
    private string? _workspace;
    private CancellationTokenSource? _status;
    private CancellationTokenSource? _diff;
    private CancellationTokenSource? _mutation;
    private bool _canInitialize;
    private string _commitMessage = "";
    private string? _commitName;
    private string? _commitEmail;

    public GitChangesPanel() => InitializeComponent();

    public static readonly DependencyProperty GitStatusProperty = DependencyProperty.Register(
        nameof(GitStatus), typeof(string), typeof(GitChangesPanel), new PropertyMetadata("Git · 未初始化 Git 仓库"));
    public string GitStatus => (string)GetValue(GitStatusProperty);
    internal event Action<GitWorkspace.Status?, bool, string?>? InspectionChanged;

    public void ChangeWorkspace(string workspace)
    {
        CancelQueries();
        _workspace = workspace;
        _canInitialize = !Path.Exists(Path.Combine(workspace, ".git"));
        _commitMessage = "";
        _commitName = _commitEmail = null;
        Branch.Text = "Git";
        SetValue(GitStatusProperty, _canInitialize ? "Git · 未初始化 Git 仓库" : "Git");
        Notice.Text = "选择改动查看差异";
        Changes.ItemsSource = null;
        Patch.Text = "";
        UpdateIndexButtons();
        InspectionChanged?.Invoke(null, _canInitialize, null);
    }

    public void CancelQueries()
    {
        _status?.Cancel();
        _diff?.Cancel();
        _mutation?.Cancel();
    }

    internal void LoadCaptureSample()
    {
        Branch.Text = "main";
        SetValue(GitStatusProperty, "Git · main");
        Changes.ItemsSource = new GitWorkspace.Change[]
        {
            new("M ", "experiment/observations.csv", null),
            new(" M", "README.md", null),
        };
        Changes.SelectedIndex = 0;
        Patch.Text = "diff --git a/experiment/observations.csv b/experiment/observations.csv\n"
            + "@@ -1,2 +1,3 @@\n sample,value\n A,41.2\n+B,42.0";
        Notice.Text = "experiment/observations.csv";
        WorktreesButton.IsEnabled = true;
    }

    private async void Refresh(object sender, RoutedEventArgs args) => await RefreshAsync();

    internal async Task RefreshAsync(string? completion = null, bool approveLinkedRepository = true)
    {
        if (_mutation is not null || _workspace is not { } workspace)
            return;
        CancelQueries();
        using var current = new CancellationTokenSource();
        _status = current;
        Notice.Text = "正在读取 Git 状态…";
        try
        {
            var result = approveLinkedRepository
                ? await QueryWithApprovalAsync(repository => GitWorkspace.StatusAsync(workspace, current.Token, repository), current.Token)
                : await GitWorkspace.StatusAsync(workspace, current.Token);
            if (current.IsCancellationRequested || _status != current)
                return;
            Branch.Text = result.Branch ?? "分离 HEAD";
            SetValue(GitStatusProperty, $"Git · {Branch.Text}");
            _canInitialize = false;
            Changes.ItemsSource = result.Changes;
            Patch.Text = "";
            Notice.Text = completion ?? (result.Changes.Length == 0 ? "工作区干净" : "选择改动查看差异");
            UpdateIndexButtons();
            InspectionChanged?.Invoke(result, _canInitialize, completion);
        }
        catch (OperationCanceledException) when (current.IsCancellationRequested) { }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (!current.IsCancellationRequested && _status == current)
            {
                Notice.Text = Message(ex);
                _canInitialize = ex is ToolDeniedException { Status: "git_not_initialized" };
                UpdateIndexButtons();
                InspectionChanged?.Invoke(null, _canInitialize, Notice.Text);
            }
        }
        finally
        {
            if (_status == current)
                _status = null;
        }
    }

    private async void SelectChange(object sender, SelectionChangedEventArgs args)
    {
        UpdateIndexButtons();
        _diff?.Cancel();
        if (_mutation is not null || _workspace is not { } workspace || Changes.SelectedItem is not GitWorkspace.Change selected)
            return;
        using var current = new CancellationTokenSource();
        _diff = current;
        Patch.Text = "";
        Notice.Text = "正在读取差异…";
        try
        {
            var result = await QueryWithApprovalAsync(repository =>
                GitWorkspace.DiffAsync(workspace, selected.Path, selected.Original, current.Token, repository), current.Token);
            if (current.IsCancellationRequested || _diff != current)
                return;
            Patch.Text = result.Text;
            Notice.Text = result.Truncated ? "差异超过显示上限" : result.Text.Length == 0 ? "没有文本差异" : selected.Path;
        }
        catch (OperationCanceledException) when (current.IsCancellationRequested) { }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (!current.IsCancellationRequested && _diff == current)
                Notice.Text = Message(ex);
        }
        finally
        {
            if (_diff == current)
                _diff = null;
        }
    }

    private async void Stage(object sender, RoutedEventArgs args) => await ChangeIndexAsync(stage: true);
    private async void Unstage(object sender, RoutedEventArgs args) => await ChangeIndexAsync(stage: false);
    private async void StageAll(object sender, RoutedEventArgs args) => await ChangeIndexAsync(stage: true, allVisible: true);

    private async void InitializeGit(object sender, RoutedEventArgs args) => await InitializeAsync();

    internal async Task InitializeAsync()
    {
        if (_mutation is not null || !_canInitialize || _workspace is not { } workspace)
            return;
        CancelQueries();
        using var current = new CancellationTokenSource();
        _mutation = current;
        UpdateIndexButtons();
        GitInitialization.Result? result = null;
        try
        {
            Notice.Text = "正在初始化 Git…";
            result = await Task.Run(() => GitInitialization.InitializeAsync(workspace, GitInitialization.DefaultBranch, current.Token), current.Token);
        }
        catch (OperationCanceledException) when (current.IsCancellationRequested) { }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (!current.IsCancellationRequested)
                Notice.Text = Message(ex);
        }
        finally
        {
            _mutation = null;
            UpdateIndexButtons();
        }
        if (result is not null && !current.IsCancellationRequested && _workspace == workspace)
            await RefreshAsync(result.Status == "initialized" ? "Git 仓库已初始化" : "Git 仓库已存在");
    }

    private async Task ChangeIndexAsync(bool stage, bool allVisible = false)
    {
        var selected = Changes.SelectedItem as GitWorkspace.Change;
        if (_mutation is not null || _workspace is not { } workspace || (!allVisible && selected is null))
            return;
        CancelQueries();
        using var current = new CancellationTokenSource();
        _mutation = current;
        GitIndexEditor.Result? updated = null;
        UpdateIndexButtons();
        try
        {
            var result = await QueryWithApprovalAsync(repository => allVisible
                ? GitIndexEditor.StageAllAsync(workspace, current.Token, repository)
                : GitIndexEditor.ChangeAsync(workspace, selected!.Path, selected.Original, stage, current.Token, repository),
                current.Token, title: "选择主仓库，批准本次暂存范围内的写入");
            if (!current.IsCancellationRequested && _workspace == workspace)
                updated = result;
        }
        catch (OperationCanceledException) when (current.IsCancellationRequested) { }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (!current.IsCancellationRequested)
                Notice.Text = Message(ex);
        }
        finally
        {
            _mutation = null;
            UpdateIndexButtons();
        }
        if (updated is not null)
        {
            var completion = !allVisible ? null : updated.Paths.Count == 0 ? "没有可批量暂存的改动" : $"已暂存 {updated.Paths.Count} 个路径";
            if (allVisible && updated.Skipped > 0)
                completion += $" · 跳过 {updated.Skipped} 项需单独核对的改动";
            await RefreshAsync(completion);
        }
    }

    private async void CommitStaged(object sender, RoutedEventArgs args) => await CommitStagedAsync();

    internal async Task CommitStagedAsync(string? initialMessage = null)
    {
        if (_mutation is not null || _workspace is not { } workspace || Window.GetWindow(this) is not { } owner)
            return;
        CancelQueries();
        using var current = new CancellationTokenSource();
        _mutation = current;
        UpdateIndexButtons();
        GitCommit.Result? committed = null;
        try
        {
            Notice.Text = "正在核对暂存区…";
            var reviewed = await QueryWithApprovalAsync(async repository =>
                (Preview: await GitCommit.PreviewAsync(workspace, current.Token, repository), Repository: repository),
                current.Token, title: "选择主仓库以批准此次本地提交");
            current.Token.ThrowIfCancellationRequested();
            var dialog = new GitCommitDialog(owner, reviewed.Preview, initialMessage ?? _commitMessage, _commitName, _commitEmail);
            var confirmed = dialog.ShowDialog() == true;
            _commitMessage = dialog.Message;
            _commitName = dialog.DraftName;
            _commitEmail = dialog.DraftEmail;
            if (confirmed)
            {
                Notice.Text = "正在提交已确认的暂存区…";
                var (message, name, email) = (dialog.Message, dialog.DraftName, dialog.DraftEmail);
                committed = await Task.Run(() => GitCommit.CommitAsync(workspace, reviewed.Preview,
                    message, name, email, current.Token, reviewed.Repository), current.Token);
                _commitMessage = "";
            }
            else
                Notice.Text = "已取消提交";
        }
        catch (OperationCanceledException) when (current.IsCancellationRequested) { }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (!current.IsCancellationRequested)
                Notice.Text = ex is ArgumentException ? ex.Message : Message(ex);
        }
        finally
        {
            _mutation = null;
            UpdateIndexButtons();
        }
        if (committed is not null && !current.IsCancellationRequested && _workspace == workspace)
            await RefreshAsync($"已提交 {committed.CommitId[..12]}");
    }

    internal void SelectPath(string path)
    {
        Changes.SelectedItem = Changes.Items.Cast<GitWorkspace.Change>()
            .FirstOrDefault(change => change.Path == path);
    }

    private async void OpenWorktrees(object sender, RoutedEventArgs args)
    {
        if (_mutation is not null || _workspace is not { } workspace || Window.GetWindow(this) is not { } owner)
            return;
        CancelQueries();
        using var current = new CancellationTokenSource();
        _mutation = current;
        UpdateIndexButtons();
        try
        {
            var listed = await QueryWithApprovalAsync(async repository =>
                (Items: await GitWorktrees.ListAsync(workspace, current.Token, repository), Repository: repository),
                current.Token, title: "选择主仓库以核对工作树");
            current.Token.ThrowIfCancellationRequested();
            var dialog = new GitWorktreesDialog(owner, workspace, listed.Repository, listed.Items);
            if (dialog.ShowDialog() != true || dialog.OpenPath is not { } path)
                return;
            var workbench = Application.Current.Windows.OfType<WorkbenchWindow>().FirstOrDefault(window =>
                string.Equals(Path.TrimEndingDirectorySeparator(Path.GetFullPath(window.ViewModel.Workspace)), path,
                    StringComparison.OrdinalIgnoreCase));
            if (workbench is null)
            {
                workbench = new WorkbenchWindow(path);
                workbench.Show();
            }
            if (workbench.WindowState == WindowState.Minimized)
                workbench.WindowState = WindowState.Normal;
            workbench.Activate();
            Notice.Text = "已打开工作树";
        }
        catch (OperationCanceledException) when (current.IsCancellationRequested) { }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (!current.IsCancellationRequested)
                Notice.Text = Message(ex);
        }
        finally
        {
            _mutation = null;
            UpdateIndexButtons();
        }
    }

    private void UpdateIndexButtons()
    {
        if (InitializeButton is null || StageButton is null || UnstageButton is null || StageAllButton is null ||
            CommitButton is null || WorktreesButton is null || PullRequestsButton is null || Changes is null)
            return;
        var selected = _mutation is null ? Changes.SelectedItem as GitWorkspace.Change : null;
        InitializeButton.Visibility = _canInitialize ? Visibility.Visible : Visibility.Collapsed;
        InitializeButton.IsEnabled = _mutation is null && _workspace is not null;
        StageButton.IsEnabled = selected is not null && selected.Code[1] != ' ';
        UnstageButton.IsEnabled = selected is not null && selected.Code[0] is not (' ' or '?' or '!');
        StageAllButton.IsEnabled = _mutation is null && Changes.Items.OfType<GitWorkspace.Change>().Any(item => item.Code[1] != ' ');
        CommitButton.IsEnabled = _mutation is null &&
            Changes.Items.OfType<GitWorkspace.Change>().Any(item => item.Code[0] is not (' ' or '?' or '!'));
        WorktreesButton.IsEnabled = _mutation is null && _workspace is not null;
        PullRequestsButton.IsEnabled = _mutation is null && _workspace is not null && !_canInitialize;
    }

    private void OpenPullRequests(object sender, RoutedEventArgs args)
    {
        if (_mutation is not null || _workspace is not { } workspace || Window.GetWindow(this) is not { } owner)
            return;
        new GitPullRequestDialog(owner, workspace, (owner as WorkbenchWindow)?.EnteredModelKey).ShowDialog();
    }

    private async Task<T> QueryWithApprovalAsync<T>(Func<string?, Task<T>> query, CancellationToken token,
        string title = "选择主仓库，批准本次读取 Git 元数据")
    {
        try { return await Task.Run(() => query(null), token); }
        catch (ToolDeniedException ex) when (ex.Status == "git_linked_worktree")
        {
            token.ThrowIfCancellationRequested();
            var approval = new OpenFolderDialog { Title = title };
            if (approval.ShowDialog(Window.GetWindow(this)) != true)
                throw new ToolDeniedException("approval_denied", "未批准此次 Git 元数据读取。");
            token.ThrowIfCancellationRequested();
            return await Task.Run(() => query(approval.FolderName), token);
        }
    }

    private static string Message(Exception ex) => ex is ToolDeniedException denied ? denied.Message : "Git 操作失败。";
    private static bool ExpectedFailure(Exception ex) => ex is ToolDeniedException or IOException or ArgumentException or
        InvalidDataException or UnauthorizedAccessException or Win32Exception;
}
