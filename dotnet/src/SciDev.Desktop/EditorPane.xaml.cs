using System.Collections.ObjectModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using ICSharpCode.AvalonEdit.Document;
using ICSharpCode.AvalonEdit.Highlighting;
using ICSharpCode.AvalonEdit.Search;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class EditorPane : UserControl
{
    private record Position(int Caret, double Horizontal, double Vertical);
    private readonly ObservableCollection<EditorBuffer> _tabs = [];
    private readonly Dictionary<EditorBuffer, Position> _positions = [];
    private readonly TextDocument _empty = new();
    private readonly DarkSyntax _darkSyntax = new();
    private readonly SearchPanel _find;

    public EditorPane()
    {
        InitializeComponent();
        FileTabs.ItemsSource = _tabs;
        Code.Document = _empty;
        _find = SearchPanel.Install(Code.TextArea);
        Code.Options.HighlightCurrentLine = true;
        Code.TextArea.TextView.SetResourceReference(ICSharpCode.AvalonEdit.Rendering.TextView.CurrentLineBackgroundProperty, "WorkbenchEditorLine");
        Code.TextArea.LeftMargins.Insert(0, new Border { Width = 9 });
        Code.TextArea.Caret.PositionChanged += (_, _) => CaretMoved?.Invoke();
        Code.PreviewKeyDown += HandlePythonIntelShortcut;
    }

    internal event Action<EditorPane>? Activated;
    internal event Action? SelectionChanged;
    internal event Action? CaretMoved;
    internal event Action<EditorBuffer>? CloseRequested;
    private void CloseTab(object sender, RoutedEventArgs args)
    {
        if (sender is Button { DataContext: EditorBuffer buffer })
            CloseRequested?.Invoke(buffer);
    }
    internal void ShowFind()
    {
        _find.Open();
        _find.Reactivate();
    }
    internal EditorBuffer? Current { get; private set; }
    internal int CaretLine => Code.TextArea.Caret.Line;
    internal IReadOnlyList<EditorBuffer> Tabs => _tabs;
    internal int OpenVersion { get; set; }

    internal void Open(EditorBuffer buffer, bool select = true)
    {
        if (!_tabs.Contains(buffer))
            _tabs.Add(buffer);
        if (select)
        {
            FileTabs.SelectedItem = buffer;
            FileTabs.ScrollIntoView(buffer);
        }
    }

    internal void Close(EditorBuffer buffer)
    {
        var position = _tabs.IndexOf(buffer);
        if (position < 0)
            return;
        _tabs.RemoveAt(position);
        if (FileTabs.SelectedItem is null && _tabs.Count > 0)
            FileTabs.SelectedIndex = Math.Min(position, _tabs.Count - 1);
        _positions.Remove(buffer);
    }

    internal void Clear()
    {
        OpenVersion++;
        _tabs.Clear();
        _positions.Clear();
    }

    internal void SetActive(bool active) =>
        PaneFrame.SetResourceReference(Border.BorderBrushProperty, active ? "WorkbenchAccent" : "WorkbenchEdge");

    internal void SetReadOnly(bool editingBlocked) => Code.IsReadOnly = editingBlocked || Current?.Snapshot is null;
    internal void SetFontSize(double fontSize) => Code.FontSize = fontSize;

    internal void ApplyOptions(SciDev.Infrastructure.DesktopAppearanceOptions options)
    {
        Code.FontFamily = WorkbenchAppearance.Font(options.EditorFontFamily, "Consolas, Cascadia Mono");
        Code.WordWrap = options.EditorWrap;
        Code.ShowLineNumbers = options.LineNumbers;
        Code.Options.HighlightCurrentLine = options.HighlightLine;
        Code.Options.IndentationSize = options.EditorTabSize;
    }

    internal void Reveal(EditorDiagnostic diagnostic)
    {
        var document = Code.Document;
        var start = document.GetLineByNumber(Math.Clamp(diagnostic.Line, 1, document.LineCount));
        var end = document.GetLineByNumber(Math.Clamp(diagnostic.EndLine, 1, document.LineCount));
        var column = Math.Clamp(diagnostic.Column, 1, start.Length + 1);
        var first = start.Offset + column - 1;
        var last = end.Offset + Math.Clamp(diagnostic.EndColumn - 1, 0, end.Length);
        Code.Select(first, Math.Max(last - first, 0));
        Code.ScrollTo(start.LineNumber, column);
        Code.Focus();
    }

    internal void ApplyAppearance(string profile)
    {
        _appearanceProfile = profile;
        SetSyntax();
        var view = Code.TextArea.TextView;
        view.LineTransformers.Remove(_darkSyntax);
        if (profile != "paper")
            view.LineTransformers.Add(_darkSyntax);
        view.Redraw();
    }

    private void FocusPane(object sender, KeyboardFocusChangedEventArgs args) => Activated?.Invoke(this);
    private void ClickPane(object sender, MouseButtonEventArgs args) => Activated?.Invoke(this);

    private void SelectTab(object sender, SelectionChangedEventArgs args)
    {
        ClosePythonCompletions();
        if (Current is { } previous)
            _positions[previous] = new(Code.CaretOffset, Code.HorizontalOffset, Code.VerticalOffset);
        Current = FileTabs.SelectedItem as EditorBuffer;
        Code.Document = Current?.Document ?? _empty;
        SetSyntax();
        if (Current is { } next && _positions.TryGetValue(next, out var position))
        {
            Code.CaretOffset = Math.Min(position.Caret, Code.Document.TextLength);
            Code.ScrollToHorizontalOffset(position.Horizontal);
            Code.ScrollToVerticalOffset(position.Vertical);
        }
        SelectionChanged?.Invoke();
    }

    private string _appearanceProfile = "paper";
    private void SetSyntax()
    {
        if (Current is not { } buffer)
        {
            Code.SyntaxHighlighting = null;
            return;
        }
        var extension = Path.GetExtension(buffer.Path).ToLowerInvariant();
        Code.SyntaxHighlighting = _appearanceProfile == "paper" && extension is ".html" or ".htm"
            ? PaperHtmlSyntax.Definition : HighlightingManager.Instance.GetDefinitionByExtension(extension);
    }
}
