using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class ProjectWorkspace : UserControl
{
    private WorkspaceDocuments? _workspace;
    private string? _workspacePath;
    private int _treeVersion;
    private CancellationTokenSource? _search;

    public ProjectWorkspace()
    {
        InitializeComponent();
        InitializeEditors();
    }

    public event RoutedEventHandler? SearchRequested;
    public event RoutedEventHandler? TerminalRequested;
    internal event Action<string>? DocumentOpened;

    public static readonly DependencyProperty CaretStatusProperty = DependencyProperty.Register(
        nameof(CaretStatus), typeof(string), typeof(ProjectWorkspace), new PropertyMetadata(""));
    public static readonly DependencyProperty EditorLanguageLabelProperty = DependencyProperty.Register(
        nameof(EditorLanguageLabel), typeof(string), typeof(ProjectWorkspace), new PropertyMetadata("Plain Text"));
    public string CaretStatus => (string)GetValue(CaretStatusProperty);
    public string EditorLanguageLabel => (string)GetValue(EditorLanguageLabelProperty);

    internal void SetExplorerWidth(double width)
    {
        ExplorerPanel.Visibility = ExplorerDivider.Visibility = width == 0 ? Visibility.Collapsed : Visibility.Visible;
        FolderColumn.MinWidth = 0;
        FolderColumn.Width = new(width);
        ExplorerHandleColumn.Width = new(width == 0 ? 0 : 5);
    }

    internal void SetSearch(string search) => SearchInput.Text = search;
    internal Task RefreshAsync() => RefreshTreeAsync();
    internal void SetEditorScale(double fontSize)
    {
        PrimaryDocuments.SetFontSize(fontSize);
        SecondaryDocuments.SetFontSize(fontSize);
    }

    internal void ApplyEditorOptions(DesktopAppearanceOptions options)
    {
        PrimaryDocuments.ApplyOptions(options);
        SecondaryDocuments.ApplyOptions(options);
    }

    internal async Task OpenSearchResultAsync()
    {
        var query = SearchInput.Text;
        _search?.Cancel();
        await RefreshTreeAsync();
        if (query != SearchInput.Text)
            return;
        if (FileTree.Items.OfType<TreeViewItem>().FirstOrDefault(item =>
                item.Tag is WorkspaceDocuments.Entry { IsDirectory: false })?.Tag is WorkspaceDocuments.Entry entry)
            await OpenAsync(entry.RelativePath, _activePane);
    }

    internal void ApplyAppearance(string profile, DesktopPreferences.QtLayout? layout)
    {
        var fraction = layout is { CanApply: true }
            ? Math.Clamp(layout.Left / (1 - layout.Chat), 0.15, 0.45)
            : profile == "focus" ? 0.22 : 0.29;
        FolderColumn.Width = new GridLength(fraction, GridUnitType.Star);
        EditorColumn.Width = new GridLength(1 - fraction, GridUnitType.Star);
        PrimaryDocuments.ApplyAppearance(profile);
        SecondaryDocuments.ApplyAppearance(profile);
    }

    public async Task ChangeWorkspaceAsync(string workspace)
    {
        _treeVersion++;
        _search?.Cancel();
        StopEditorDiagnostics();
        StopPythonIntel();
        _workspace = null;
        _workspacePath = null;
        FileTree.Items.Clear();
        SearchInput.Clear();
        ResetEditors();
        Notice.Text = "";
        WelcomeProject.Text = Path.GetFileName(Path.TrimEndingDirectorySeparator(workspace));
        try
        {
            _workspace = new WorkspaceDocuments(workspace);
            _workspacePath = Path.GetFullPath(workspace);
            await RefreshTreeAsync();
        }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            Notice.Text = Describe(ex, "无法打开工作区文件。");
        }
    }

    private async Task RefreshTreeAsync(CancellationToken token = default)
    {
        var workspace = _workspace;
        if (workspace is null)
            return;
        var version = ++_treeVersion;
        var query = SearchInput.Text.Trim();
        try
        {
            var result = await Task.Run(() => query.Length == 0
                ? new WorkspaceDocuments.FileSearch(workspace.List(null, token), false) : workspace.FindFiles(query, token), token);
            if (version != _treeVersion || token.IsCancellationRequested)
                return;
            FileTree.Items.Clear();
            var entries = FileTree.Items;
            if (query.Length == 0 && _workspacePath is { } root)
            {
                var header = BuildRoot(Path.GetFileName(Path.TrimEndingDirectorySeparator(root)));
                FileTree.Items.Add(header);
                entries = header.Items;
                WelcomeIndex.Visibility = result.Matches.Any(item => !item.IsDirectory && item.Name == "index.html")
                    ? Visibility.Visible : Visibility.Collapsed;
            }
            foreach (var entry in result.Matches)
            {
                var item = BuildItem(workspace, version, entry);
                if (query.Length != 0)
                    item.Header = entry.RelativePath;
                entries.Add(item);
            }
            if (result.Limited || query.Length > 0 && result.Matches.Length == 0)
                Notice.Text = result.Limited ? "匹配结果较多，可输入更具体的路径。" : "没有匹配文件";
        }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (version == _treeVersion)
                Notice.Text = Describe(ex, "无法读取目录。");
        }
    }

    private static TreeViewItem BuildRoot(string title) => new()
    {
        Header = new TextBlock { Text = title, TextTrimming = TextTrimming.CharacterEllipsis, FontWeight = FontWeights.SemiBold, MaxWidth = 220 },
        IsExpanded = true, ToolTip = title, Margin = new Thickness(0, 4, 0, 4),
    };

    private void WelcomeSearch(object sender, RoutedEventArgs args) => SearchRequested?.Invoke(this, args);
    private async void OpenIndex(object sender, RoutedEventArgs args) => await OpenAsync("index.html", _activePane);
    private void ShowTerminal(object sender, RoutedEventArgs args) => TerminalRequested?.Invoke(this, args);
    private void FindInEditor(object sender, RoutedEventArgs args) => _activePane.ShowFind();
    private void OpenEditorMenu(object sender, RoutedEventArgs args) => EditorMenuPopup.IsOpen = true;
    private void DismissEditorMenu(object sender, RoutedEventArgs args) => EditorMenuPopup.IsOpen = false;

    private async void CreateFile(object sender, RoutedEventArgs args)
    {
        if (_workspace is not { } workspace || _workspacePath is not { } root || _saving || _reloading)
            return;
        var dialog = new SaveFileDialog { Title = "新建文件", InitialDirectory = root, AddExtension = false, OverwritePrompt = false };
        if (dialog.ShowDialog(Window.GetWindow(this)) != true)
            return;
        try
        {
            var relative = Path.GetRelativePath(root, dialog.FileName);
            await Task.Run(() => workspace.Create(relative, CancellationToken.None));
            if (ReferenceEquals(_workspace, workspace))
            {
                await RefreshTreeAsync();
                await OpenAsync(relative, _activePane);
            }
        }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            Notice.Text = Describe(ex, "无法新建文件。请选择工作区内尚不存在的文件名。");
        }
    }

    private TreeViewItem BuildItem(WorkspaceDocuments workspace, int version, WorkspaceDocuments.Entry entry)
    {
        var item = new TreeViewItem { Header = entry.Name, Tag = entry, ToolTip = entry.RelativePath };
        if (!entry.IsDirectory)
        {
            var menu = new ContextMenu();
            var openSide = new MenuItem { Header = "在右侧打开" };
            openSide.Click += async (_, _) =>
            {
                if (ReferenceEquals(workspace, _workspace) && version == _treeVersion && !_saving)
                {
                    OpenSplit();
                    await OpenAsync(entry.RelativePath, SecondaryDocuments);
                }
            };
            menu.Items.Add(openSide);
            item.ContextMenu = menu;
            return item;
        }
        item.Items.Add(new TreeViewItem { Header = "读取中…" });
        item.Expanded += async (_, args) =>
        {
            if (!ReferenceEquals(args.OriginalSource, item) || item.Items.Count != 1 ||
                item.Items[0] is not TreeViewItem { Tag: null })
                return;
            // Replace the placeholder while in flight to suppress duplicate reads.
            ((TreeViewItem)item.Items[0]).Tag = entry;
            try
            {
                var entries = await Task.Run(() => workspace.List(entry.RelativePath, CancellationToken.None));
                if (version != _treeVersion)
                    return;
                item.Items.Clear();
                foreach (var child in entries)
                    item.Items.Add(BuildItem(workspace, version, child));
            }
            catch (Exception ex) when (ExpectedFailure(ex))
            {
                if (version != _treeVersion)
                    return;
                Notice.Text = Describe(ex, "无法读取目录。");
                item.Items.Clear();
                item.Items.Add(new TreeViewItem { Header = "展开重试" });
            }
        };
        return item;
    }

    private async void RefreshTree(object sender, RoutedEventArgs args) => await RefreshTreeAsync();

    private async void FilterFiles(object sender, TextChangedEventArgs args)
    {
        _search?.Cancel();
        if (_workspace is null)
            return;
        using var pending = new CancellationTokenSource();
        _search = pending;
        try
        {
            await Task.Delay(180, pending.Token);
            await RefreshTreeAsync(pending.Token);
        }
        catch (OperationCanceledException) when (pending.IsCancellationRequested) { }
        finally
        {
            if (_search == pending)
                _search = null;
        }
    }

    private async void SelectFile(object sender, RoutedPropertyChangedEventArgs<object> args)
    {
        if (args.NewValue is TreeViewItem { Tag: WorkspaceDocuments.Entry { IsDirectory: false } file })
            await OpenAsync(file.RelativePath, _activePane);
    }

    private static string Describe(Exception ex, string fallback) => ex is ToolDeniedException denied ? denied.Message : fallback;
    private static bool ExpectedFailure(Exception ex) => ex is ToolDeniedException or ArgumentException or IOException or
        UnauthorizedAccessException or DecoderFallbackException or EncoderFallbackException;

}
