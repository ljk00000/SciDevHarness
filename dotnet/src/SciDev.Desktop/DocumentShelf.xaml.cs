using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class DocumentShelf : UserControl
{
    private WorkspaceDocuments? _workspace;
    private CancellationTokenSource? _listing;
    private string _query = "";
    private string? _displayedQuery;
    public static readonly DependencyProperty ActivePathProperty = DependencyProperty.Register(
        nameof(ActivePath), typeof(string), typeof(DocumentShelf));
    public string? ActivePath
    {
        get => (string?)GetValue(ActivePathProperty);
        set => SetValue(ActivePathProperty, value);
    }

    internal event Action<string>? PreviewRequested;
    internal event Action<string>? TextRequested;
    internal event RoutedEventHandler? CloseRequested;

    public DocumentShelf() => InitializeComponent();

    internal async Task ChangeWorkspaceAsync(string workspace)
    {
        _workspace = null;
        _listing?.Cancel();
        Files.Items.Clear();
        try
        {
            _workspace = new(workspace);
            await RefreshAsync(_query);
        }
        catch (Exception ex) when (ReadError(ex))
        {
            ShowNotice(Message(ex));
        }
    }

    internal async Task RefreshAsync(string query)
    {
        _query = query.Trim();
        _listing?.Cancel();
        if (_workspace is not { } workspace)
            return;
        using var listing = new CancellationTokenSource();
        _listing = listing;
        var term = _query;
        try
        {
            var result = await Task.Run(() => term.Length == 0
                ? new WorkspaceDocuments.FileSearch(workspace.List(null, listing.Token), false)
                : workspace.FindFiles(term, listing.Token), listing.Token);
            if (_listing != listing || listing.IsCancellationRequested || _workspace != workspace)
                return;
            if (_displayedQuery != term)
            {
                Files.Items.Clear();
                _displayedQuery = term;
            }
            UpdateRows(Files.Items, result.Matches, showPath: term.Length > 0);
            ShowNotice(result.Limited ? "搜索已达上限，可输入更具体的文件名。" :
                result.Matches.Length == 0 ? term.Length > 0 ? "没有匹配的文件。" : "工作区为空。" : "");
            if (term.Length == 0)
                await RefreshExpandedFoldersAsync(Files.Items, listing.Token);
        }
        catch (OperationCanceledException) when (listing.IsCancellationRequested) { }
        catch (Exception ex) when (ReadError(ex))
        {
            if (_listing == listing && _workspace == workspace)
            {
                Files.Items.Clear();
                ShowNotice(Message(ex));
            }
        }
        finally
        {
            if (_listing == listing)
                _listing = null;
        }
    }

    private void UpdateRows(ItemCollection items, WorkspaceDocuments.Entry[] entries, bool showPath = false)
    {
        var requested = entries.ToHashSet();
        var current = new Dictionary<WorkspaceDocuments.Entry, TreeViewItem>();
        for (var index = items.Count - 1; index >= 0; index--)
        {
            if (items[index] is TreeViewItem { Tag: WorkspaceDocuments.Entry entry } row && requested.Contains(entry))
                current.Add(entry, row);
            else
                items.RemoveAt(index);
        }

        for (var index = 0; index < entries.Length; index++)
        {
            if (!current.TryGetValue(entries[index], out var row))
                row = MakeItem(entries[index], showPath);
            else if (index < items.Count && ReferenceEquals(items[index], row))
                continue;
            else
                items.Remove(row);
            items.Insert(index, row);
        }
    }

    private async Task RefreshExpandedFoldersAsync(ItemCollection items, CancellationToken token)
    {
        foreach (var row in items.OfType<TreeViewItem>().Where(row => row.IsExpanded).ToArray())
        {
            token.ThrowIfCancellationRequested();
            if (row.Tag is WorkspaceDocuments.Entry { IsDirectory: true } folder)
            {
                await LoadChildrenAsync(row, folder, token);
                if (row.IsExpanded)
                    await RefreshExpandedFoldersAsync(row.Items, token);
            }
        }
    }

    internal async Task OpenFirstResultAsync(string query)
    {
        await RefreshAsync(query);
        if (query.Trim() != _query || _displayedQuery != _query ||
            Files.Items.OfType<TreeViewItem>().FirstOrDefault() is not { } first ||
            first.Tag is not WorkspaceDocuments.Entry { IsDirectory: false } entry)
            return;
        first.IsSelected = true;
        first.Focus();
        RequestPreview(entry);
    }

    private TreeViewItem MakeItem(WorkspaceDocuments.Entry entry, bool showPath = false)
    {
        var label = new TextBlock
        {
            Text = showPath ? entry.RelativePath : entry.Name,
            TextTrimming = TextTrimming.CharacterEllipsis,
            VerticalAlignment = VerticalAlignment.Center,
            FontWeight = entry.IsDirectory ? FontWeights.Medium : FontWeights.Normal,
        };
        var icon = new System.Windows.Shapes.Path
        {
            Data = Geometry.Parse(FileGlyph(entry)), Width = 15, Height = 16, Stretch = Stretch.Uniform,
            StrokeThickness = 1.2, StrokeLineJoin = PenLineJoin.Round, Margin = new(0, 0, 7, 0), VerticalAlignment = VerticalAlignment.Center,
        };
        icon.SetResourceReference(System.Windows.Shapes.Shape.StrokeProperty, entry.IsDirectory ? "WorkbenchMuted" : "WorkbenchAccent");
        var header = new DockPanel();
        header.Children.Add(icon);
        header.Children.Add(label);
        var item = new TreeViewItem { Header = header, Tag = entry, ToolTip = entry.RelativePath };
        if (entry.IsDirectory)
        {
            item.Items.Add(new TreeViewItem { Header = "正在读取…", IsEnabled = false });
            item.Expanded += async (_, e) =>
            {
                if (ReferenceEquals(e.Source, item))
                    await LoadChildrenAsync(item, entry);
            };
        }
        else
        {
            var menu = new ContextMenu();
            if (PreviewDocument.CanOpen(entry.RelativePath))
            {
                var preview = new MenuItem
                {
                    Header = PreviewDocument.KindOf(entry.RelativePath) == PreviewKind.Markdown ? "预览 Markdown" : "预览文档",
                };
                preview.Click += (_, _) => RequestPreview(entry);
                menu.Items.Add(preview);
                item.MouseDoubleClick += (_, e) =>
                {
                    if (e.ChangedButton != MouseButton.Left)
                        return;
                    RequestPreview(entry);
                    e.Handled = true;
                };
            }
            var text = new MenuItem { Header = "以纯文本打开" };
            text.Click += (_, _) => TextRequested?.Invoke(entry.RelativePath);
            menu.Items.Add(text);
            item.ContextMenu = menu;
            item.PreviewMouseRightButtonDown += (_, _) => item.IsSelected = true;
        }
        return item;
    }

    private static string FileGlyph(WorkspaceDocuments.Entry entry) => entry.IsDirectory
        ? "M 1,4 H 6 L 8,6 H 15 V 14 H 1 Z"
        : PreviewDocument.KindOf(entry.RelativePath) switch
        {
            PreviewKind.Image => "M 1,2 H 15 V 14 H 1 Z M 2,12 L 6,7 L 9,10 L 11,8 L 14,12 M 11,5 h 1",
            PreviewKind.Pdf => "M 2,1 H 9 L 14,6 V 16 H 2 Z M 9,1 V 6 H 14 M 4,12 H 12 M 4,9 H 8",
            _ => "M 2,1 H 9 L 14,6 V 16 H 2 Z M 9,1 V 6 H 14 M 5,9 H 11 M 5,12 H 11",
        };

    private async Task LoadChildrenAsync(TreeViewItem node, WorkspaceDocuments.Entry folder, CancellationToken token = default)
    {
        if (_workspace is not { } workspace)
            return;
        try
        {
            var children = await Task.Run(() => workspace.List(folder.RelativePath, token), token);
            if (_workspace != workspace || token.IsCancellationRequested)
                return;
            UpdateRows(node.Items, children);
            if (children.Length == 0)
                node.Items.Add(new TreeViewItem { Header = "空文件夹", IsEnabled = false });
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception ex) when (ReadError(ex))
        {
            if (_workspace == workspace && !token.IsCancellationRequested)
            {
                node.Items.Clear();
                node.Items.Add(new TreeViewItem { Header = Message(ex), IsEnabled = false });
            }
        }
    }

    private void OpenFromKeyboard(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter || Files.SelectedItem is not TreeViewItem { Tag: WorkspaceDocuments.Entry { IsDirectory: false } entry })
            return;
        RequestPreview(entry);
        e.Handled = true;
    }

    private void RequestPreview(WorkspaceDocuments.Entry entry)
    {
        if (PreviewDocument.CanOpen(entry.RelativePath))
            PreviewRequested?.Invoke(entry.RelativePath);
    }

    private void ShowNotice(string text)
    {
        Notice.Text = text;
        Notice.Visibility = text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
    }

    private void CloseShelf(object sender, RoutedEventArgs e) => CloseRequested?.Invoke(sender, e);
    private static bool ReadError(Exception error) => error is ToolDeniedException or IOException or UnauthorizedAccessException or ArgumentException;
    private static string Message(Exception error) => error is ToolDeniedException denied ? denied.Message : "无法读取工作区文件。";
}
