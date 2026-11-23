using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class ProjectWorkspace
{
    private readonly Dictionary<string, EditorBuffer> _buffers = new(StringComparer.OrdinalIgnoreCase);
    private EditorPane _activePane = null!;
    private bool _split;
    private bool _reloading;
    private bool _saving;

    public bool HasUnsavedChanges => _buffers.Values.Any(buffer => buffer.IsModified);
    public bool IsSaving => _saving;
    internal event Action? PythonDebugRequested;

    internal (string Path, int Line, bool HasUnsavedChanges)? PythonDebugTarget =>
        !_saving && !_reloading && _activePane.Current is { Snapshot: not null } buffer &&
        buffer.Path.EndsWith(".py", StringComparison.OrdinalIgnoreCase)
            ? (buffer.Path, _activePane.CaretLine, buffer.IsModified) : null;

    internal void SetEditorNotice(string notice) => Notice.Text = notice;
    private void DebugFile(object sender, RoutedEventArgs args) => PythonDebugRequested?.Invoke();

    internal WorkspaceDrafts CaptureDrafts() => new(_buffers.Values.Where(buffer => buffer.IsModified)
        .OrderByDescending(buffer => ReferenceEquals(buffer, _activePane.Current))
        .Select((buffer, index) => (buffer.Path,
            index < WorkspaceDrafts.MaxContentFiles && buffer.Document.TextLength <= WorkspaceDrafts.MaxFileBytes
                ? buffer.Document.Text : null)));

    private void InitializeEditors()
    {
        PrimaryDocuments.Activated += ActivatePane;
        SecondaryDocuments.Activated += ActivatePane;
        PrimaryDocuments.SelectionChanged += UpdateEditorControls;
        SecondaryDocuments.SelectionChanged += UpdateEditorControls;
        PrimaryDocuments.CaretMoved += UpdateEditorStatus;
        SecondaryDocuments.CaretMoved += UpdateEditorStatus;
        PrimaryDocuments.CloseRequested += buffer => CloseBuffer(PrimaryDocuments, buffer);
        SecondaryDocuments.CloseRequested += buffer => CloseBuffer(SecondaryDocuments, buffer);
        PrimaryDocuments.PythonIntelRequested += RequestPythonIntel;
        SecondaryDocuments.PythonIntelRequested += RequestPythonIntel;
        ActivatePane(PrimaryDocuments);
    }

    public bool ConfirmDiscard()
    {
        var unsaved = _buffers.Values.Count(buffer => buffer.IsModified);
        return !_saving && (unsaved == 0 || Confirm($"有 {unsaved} 个文件含未保存的编辑。放弃这些编辑并继续？"));
    }

    private bool Confirm(string message) => MessageBox.Show(Window.GetWindow(this), message, "未保存的文件",
        MessageBoxButton.YesNo, MessageBoxImage.Question, MessageBoxResult.No) == MessageBoxResult.Yes;

    private void ActivatePane(EditorPane pane)
    {
        _activePane = pane;
        PrimaryDocuments.SetActive(pane == PrimaryDocuments);
        SecondaryDocuments.SetActive(pane == SecondaryDocuments);
        UpdateEditorControls();
    }

    private void ResetEditors()
    {
        _buffers.Clear();
        PrimaryDocuments.Clear();
        SecondaryDocuments.Clear();
        SetSplit(false);
        _reloading = false;
        ActivatePane(PrimaryDocuments);
    }

    private async Task OpenAsync(string relative, EditorPane pane, PythonDefinition? definition = null,
        Func<bool>? canReveal = null)
    {
        if (_workspace is not { } workspace || _reloading || _saving)
            return;
        var version = ++pane.OpenVersion;
        ActivatePane(pane);
        if (_buffers.TryGetValue(relative, out var existing))
        {
            pane.Open(existing);
            if (definition is not null)
                pane.Reveal(definition.Line, definition.Column);
            Notice.Text = "";
            DocumentOpened?.Invoke(existing.Path);
            return;
        }
        try
        {
            var snapshot = await Task.Run(() => workspace.Open(relative));
            if (version != pane.OpenVersion || !ReferenceEquals(_workspace, workspace) || canReveal?.Invoke() == false)
                return;
            if (!_buffers.TryGetValue(snapshot.Path, out var buffer))
            {
                buffer = new(snapshot);
                buffer.PropertyChanged += (_, _) => UpdateEditorControls();
                _buffers.Add(snapshot.Path, buffer);
            }
            pane.Open(buffer);
            if (definition is not null)
                pane.Reveal(definition.Line, definition.Column);
            Notice.Text = "";
            DocumentOpened?.Invoke(buffer.Path);
        }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (version == pane.OpenVersion && ReferenceEquals(_workspace, workspace))
                Notice.Text = Describe(ex, "无法打开文件。编辑器支持 UTF-8 文本。");
        }
    }

    private void ToggleSplit(object sender, RoutedEventArgs args)
    {
        if (_saving || _reloading)
            return;
        if (_split)
        {
            var selected = _activePane.Current ?? PrimaryDocuments.Current ?? SecondaryDocuments.Current;
            foreach (var buffer in SecondaryDocuments.Tabs)
                PrimaryDocuments.Open(buffer, select: false);
            SecondaryDocuments.Clear();
            SetSplit(false);
            if (selected is not null)
                PrimaryDocuments.Open(selected);
            ActivatePane(PrimaryDocuments);
        }
        else if (_activePane.Current is { } buffer)
        {
            OpenSplit();
            SecondaryDocuments.Open(buffer);
            ActivatePane(SecondaryDocuments);
        }
    }

    private void OpenSplit()
    {
        if (!_split)
            SetSplit(true);
    }

    private void SetSplit(bool split)
    {
        _split = split;
        PrimaryColumn.Width = new GridLength(1, GridUnitType.Star);
        SecondaryColumn.MinWidth = split ? 105 : 0;
        SecondaryColumn.Width = split ? new GridLength(1, GridUnitType.Star) : new GridLength(0);
        SplitColumn.Width = new GridLength(split ? 5 : 0);
        SecondaryDocuments.Visibility = SplitHandle.Visibility = split ? Visibility.Visible : Visibility.Collapsed;
        UpdateEditorControls();
    }

    private async void ReloadFile(object sender, RoutedEventArgs args)
    {
        if (_saving || _reloading || _activePane.Current is not { Snapshot: not null } buffer ||
            _workspace is not { } workspace ||
            buffer.IsModified && !Confirm($"重新读取 {buffer.Path} 并放弃草稿？两个窗格中的该文件都会更新。"))
            return;
        _reloading = true;
        UpdateEditorControls();
        try
        {
            var snapshot = await Task.Run(() => workspace.Open(buffer.Path));
            if (ReferenceEquals(_workspace, workspace))
            {
                buffer.Reload(snapshot);
                Notice.Text = "已重读";
            }
        }
        catch (Exception ex) when (ExpectedFailure(ex))
        {
            if (ReferenceEquals(_workspace, workspace))
                Notice.Text = Describe(ex, "文件重读失败，草稿仍在。");
        }
        finally
        {
            if (ReferenceEquals(_workspace, workspace))
            {
                _reloading = false;
                UpdateEditorControls();
            }
        }
    }

    private void CloseFile(object sender, RoutedEventArgs args) => CloseCurrent();

    private void CloseCurrent()
    {
        if (_activePane.Current is { } buffer)
            CloseBuffer(_activePane, buffer);
    }

    private void CloseBuffer(EditorPane pane, EditorBuffer buffer)
    {
        if (_saving || _reloading)
            return;
        var other = pane == PrimaryDocuments ? SecondaryDocuments : PrimaryDocuments;
        var lastView = !other.Tabs.Contains(buffer);
        if (lastView && buffer.IsModified && !Confirm($"放弃 {buffer.Path} 的未保存编辑？"))
            return;
        pane.Close(buffer);
        if (lastView)
            _buffers.Remove(buffer.Path);
        Notice.Text = "";
        UpdateEditorControls();
    }

    private async void SaveFile(object sender, RoutedEventArgs args) => await SaveCurrentAsync();
    private async void SaveAllFiles(object sender, RoutedEventArgs args) => await SaveAsync(_buffers.Values);

    private Task SaveCurrentAsync() => _activePane.Current is { } document ? SaveAsync([document]) : Task.CompletedTask;

    private async void EditorShortcut(object sender, KeyEventArgs args)
    {
        switch (args.Key, Keyboard.Modifiers)
        {
            case (Key.S, ModifierKeys.Control):
                args.Handled = true;
                await SaveCurrentAsync();
                break;
            case (Key.S, ModifierKeys.Control | ModifierKeys.Shift):
                args.Handled = true;
                await SaveAsync(_buffers.Values);
                break;
            case (Key.W, ModifierKeys.Control):
                args.Handled = true;
                CloseCurrent();
                break;
        }
    }

    private async Task SaveAsync(IEnumerable<EditorBuffer> buffers)
    {
        if (_saving || _reloading || _workspace is not { } workspace)
            return;
        var drafts = buffers.Where(item => item.IsModified && item.Snapshot is not null).ToArray();
        if (drafts.Length == 0)
            return;
        _saving = true;
        UpdateEditorControls();
        var saved = 0;
        var firstFailure = "";
        try
        {
            foreach (var buffer in drafts)
            {
                var snapshot = buffer.Snapshot!;
                var text = buffer.Document.Text;
                try
                {
                    buffer.MarkSaved(await Task.Run(() => workspace.Save(snapshot, text, CancellationToken.None)));
                    saved++;
                }
                catch (Exception ex) when (ExpectedFailure(ex))
                {
                    if (firstFailure.Length == 0)
                        firstFailure = $"{buffer.Path} · {Describe(ex, "文件保存失败，草稿仍在。")}";
                }
            }
            Notice.Text = saved == drafts.Length ? (saved == 1 ? "已保存" : $"已保存 {saved} 个文件")
                : $"已保存 {saved} 个，保留 {drafts.Length - saved} 个草稿 · {firstFailure}";
        }
        finally
        {
            _saving = false;
            UpdateEditorControls();
        }
    }

    private void UpdateEditorControls()
    {
        if (_activePane is null)
            return;
        var buffer = _activePane.Current;
        var blocked = _saving || _reloading;
        FileTitle.Text = buffer is null ? "" : "项目  /  " + System.IO.Path.GetFileName(buffer.Path) + (buffer.IsModified ? " *" : "");
        FileTitle.ToolTip = buffer?.Path;
        var welcome = PrimaryDocuments.Current is null && SecondaryDocuments.Current is null;
        WelcomePane.Visibility = welcome ? Visibility.Visible : Visibility.Collapsed;
        EditorDock.Visibility = welcome ? Visibility.Collapsed : Visibility.Visible;
        UpdateEditorStatus();
        SaveButton.IsEnabled = !blocked && buffer is { Snapshot: not null, IsModified: true };
        SaveAllButton.IsEnabled = !blocked && HasUnsavedChanges;
        ReloadButton.IsEnabled = !blocked && buffer?.Snapshot is not null;
        CloseButton.IsEnabled = !blocked && buffer is not null;
        SplitButton.IsEnabled = !blocked && (_split || buffer is not null);
        SplitButton.Content = _split ? "合并窗格" : "分屏";
        DebugFileButton.IsEnabled = PythonDebugTarget is not null;
        PythonCompleteButton.IsEnabled = PythonDefinitionButton.IsEnabled = PythonDebugTarget is not null;
        PrimaryDocuments.SetReadOnly(blocked);
        SecondaryDocuments.SetReadOnly(blocked);
        FileTree.IsEnabled = !blocked;
        ObserveDiagnosticBuffer(buffer);
    }

    private void UpdateEditorStatus()
    {
        var buffer = _activePane?.Current;
        SetValue(CaretStatusProperty, buffer is null ? "" : $"Ln {_activePane!.CaretLine}, Col {_activePane.CaretPosition.Column + 1}");
        var ext = buffer is null ? "" : System.IO.Path.GetExtension(buffer.Path).TrimStart('.');
        SetValue(EditorLanguageLabelProperty, ext.Length == 0 ? "Plain Text" : ext.ToUpperInvariant());
    }

    internal void LoadClassicCapture(string? source)
    {
        const string projectName = "scidev-ui-workspace";
        WelcomeProject.Text = projectName;
        WelcomeIndex.Visibility = Visibility.Visible;
        FileTree.Items.Clear();
        var project = BuildRoot(projectName);
        project.Items.Add(new TreeViewItem { Header = "src", Items = { new TreeViewItem { Header = "analysis.py" } } });
        project.Items.Add(new TreeViewItem { Header = "index.html" });
        project.Items.Add(new TreeViewItem { Header = "README.md" });
        FileTree.Items.Add(project);
        if (source is not null)
            PrimaryDocuments.Open(new("index.html", System.IO.File.ReadAllText(source)));
    }

    internal void LoadCaptureSample(bool split)
    {
        var folder = new TreeViewItem { Header = "experiment", IsExpanded = true };
        folder.Items.Add(new TreeViewItem { Header = "Analysis.cs", IsSelected = true });
        folder.Items.Add(new TreeViewItem { Header = "observations.csv" });
        FileTree.Items.Add(folder);
        FileTree.Items.Add(new TreeViewItem { Header = "README.md" });
        PrimaryDocuments.Open(new("experiment/Analysis.cs", """
            namespace Research;

            public static class Analysis
            {
                public static double Mean(double[] observations)
                {
                    return observations.Average();
                }
            }
            """));
        if (split)
        {
            OpenSplit();
            SecondaryDocuments.Open(new("experiment/observations.csv", "sample,value\nA,41.2\nB,42.0\nC,44.1\n"));
        }
    }
}
