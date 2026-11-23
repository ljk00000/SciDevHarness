using System.ComponentModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class GitWorktreesDialog : Window
{
    private readonly string _workspace;
    private readonly string? _approvedRepository;
    private readonly string _repository;
    private bool _busy;

    internal GitWorktreesDialog(Window owner, string workspace, string? approvedRepository, GitWorktrees.Entry[] worktrees)
    {
        InitializeComponent();
        Owner = owner;
        _workspace = workspace;
        _approvedRepository = approvedRepository;
        _repository = approvedRepository ?? workspace;
        Repository.Text = _repository;
        Repository.ToolTip = _repository;
        var suffix = Guid.NewGuid().ToString("N")[..8];
        WorktreeId.Text = $"gui-{DateTime.Now:yyyyMMdd-HHmmss}-{suffix}";
        Resources.MergedDictionaries.Add(WorkbenchAppearance.Create(
            owner is WorkbenchWindow workbench ? workbench.AppearanceProfile : "paper"));
        Resources.MergedDictionaries.Add(new ResourceDictionary
        {
            Source = new Uri("pack://application:,,,/SciDev.Desktop;component/WorkbenchControls.xaml", UriKind.Absolute),
        });
        Worktrees.ItemsSource = worktrees;
    }

    internal string? OpenPath { get; private set; }

    private async void Create(object sender, RoutedEventArgs args)
    {
        var id = WorktreeId.Text.Trim();
        await RunAsync(async () =>
        {
            Notice.Text = "正在从当前提交创建工作树…";
            var result = await Task.Run(() => GitWorktrees.CreateAsync(_workspace, id, CancellationToken.None, _approvedRepository));
            OpenPath = result.Path;
            DialogResult = true;
        });
    }

    private async void Refresh(object sender, RoutedEventArgs args) => await RunAsync(async () =>
    {
        await RefreshListAsync();
        Notice.Text = "";
    });

    private async Task RefreshListAsync() =>
        Worktrees.ItemsSource = await Task.Run(() => GitWorktrees.ListAsync(_workspace, CancellationToken.None, _approvedRepository));

    private void OpenSelected(object sender, RoutedEventArgs args)
    {
        if (_busy || Worktrees.SelectedItem is not GitWorktrees.Entry { Prunable: false } entry)
            return;
        try
        {
            OpenPath = GitWorktrees.CheckOpenPath(entry.Path, _repository);
            DialogResult = true;
        }
        catch (Exception ex) when (ExpectedFailure(ex)) { ShowFailure(ex); }
    }

    private async void RemoveSelected(object sender, RoutedEventArgs args)
    {
        if (_busy || Worktrees.SelectedItem is not GitWorktrees.Entry entry ||
            GitWorktrees.ManagedId(_workspace, entry) is not { } id || IsOpen(entry.Path))
            return;
        if (MessageBox.Show(this, $"回收此工作树目录并保留分支？\n\n{entry.Path}", "回收工作树",
            MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes)
            return;
        await RunAsync(async () =>
        {
            Notice.Text = "正在检查并回收工作树…";
            var removed = await Task.Run(() => GitWorktrees.RemoveAsync(_workspace, id, CancellationToken.None, _approvedRepository));
            await RefreshListAsync();
            Notice.Text = $"已回收目录，保留分支 {removed.Branch}";
        });
    }

    protected override void OnClosing(CancelEventArgs e)
    {
        if (_busy && OpenPath is null)
            e.Cancel = true;
        base.OnClosing(e);
    }

    private async Task RunAsync(Func<Task> run)
    {
        if (_busy)
            return;
        _busy = true;
        UpdateButtons();
        try { await run(); }
        catch (Exception ex) when (ExpectedFailure(ex)) { ShowFailure(ex); }
        finally
        {
            _busy = false;
            UpdateButtons();
        }
    }

    private void SelectWorktree(object sender, SelectionChangedEventArgs args) => UpdateButtons();

    private void UpdateButtons()
    {
        if (OpenButton is null || RemoveButton is null)
            return;
        CreateButton.IsEnabled = RefreshButton.IsEnabled = WorktreeId.IsEnabled = !_busy;
        OpenButton.IsEnabled = !_busy && Worktrees.SelectedItem is GitWorktrees.Entry { Prunable: false };
        RemoveButton.IsEnabled = !_busy && Worktrees.SelectedItem is GitWorktrees.Entry selected &&
            GitWorktrees.ManagedId(_workspace, selected) is not null && !IsOpen(selected.Path);
    }

    private static bool IsOpen(string path) => Application.Current.Windows.OfType<WorkbenchWindow>().Any(window =>
        string.Equals(Path.TrimEndingDirectorySeparator(Path.GetFullPath(window.ViewModel.Workspace)), path,
            StringComparison.OrdinalIgnoreCase));

    private void ShowFailure(Exception ex) =>
        Notice.Text = ex is ToolDeniedException or ArgumentException ? ex.Message : "工作树操作失败，请刷新并检查目录。";

    private static bool ExpectedFailure(Exception ex) =>
        ex is ToolDeniedException or ArgumentException or IOException or UnauthorizedAccessException or Win32Exception;
}
