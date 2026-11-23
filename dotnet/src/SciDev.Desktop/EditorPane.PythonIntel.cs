using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using ICSharpCode.AvalonEdit.CodeCompletion;
using ICSharpCode.AvalonEdit.Document;
using ICSharpCode.AvalonEdit.Editing;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal enum PythonIntelAction { Complete, Definition }

public partial class EditorPane
{
    internal readonly record struct PythonPosition(int Line, int Column, int Offset);
    internal event Action<EditorPane, PythonIntelAction>? PythonIntelRequested;
    private CompletionWindow? _pythonCompletions;

    internal PythonPosition CaretPosition =>
        new(CaretLine, Code.TextArea.Caret.Column - 1, Code.CaretOffset);

    private void HandlePythonIntelShortcut(object sender, KeyEventArgs args)
    {
        if (Code.IsReadOnly || Keyboard.Modifiers != ModifierKeys.Control ||
            Current?.Path.EndsWith(".py", StringComparison.OrdinalIgnoreCase) != true)
            return;
        PythonIntelAction? action = args.Key switch
        {
            Key.Space => PythonIntelAction.Complete,
            Key.F12 => PythonIntelAction.Definition,
            _ => null,
        };
        if (action is not { } request)
            return;
        args.Handled = true;
        PythonIntelRequested?.Invoke(this, request);
    }

    internal void ShowPythonCompletions(PythonCompletions completions)
    {
        ClosePythonCompletions();
        Code.Focus();
        var window = new CompletionWindow(Code.TextArea)
        {
            StartOffset = Code.CaretOffset - completions.Prefix,
            Width = 320,
            MaxHeight = 240,
        };
        foreach (var item in completions.Items)
            window.CompletionList.CompletionData.Add(new CompletionItem(item, this));
        _pythonCompletions = window;
        window.Closed += (_, _) =>
        {
            if (ReferenceEquals(_pythonCompletions, window))
                _pythonCompletions = null;
        };
        window.Show();
        window.CompletionList.SelectItem(Code.Document.GetText(window.StartOffset, completions.Prefix));
    }

    internal void ClosePythonCompletions() => _pythonCompletions?.Close();

    internal void Reveal(int line, int column)
    {
        var target = Code.Document.GetLineByNumber(Math.Clamp(line, 1, Code.Document.LineCount));
        column = Math.Clamp(column, 1, target.Length + 1);
        Code.Select(target.Offset + column - 1, 0);
        Code.CaretOffset = target.Offset + column - 1;
        Code.ScrollTo(target.LineNumber, column);
        Code.Focus();
    }

    private sealed class CompletionItem(PythonCompletion item, EditorPane pane) : ICompletionData
    {
        public ImageSource? Image => null;
        public string Text => item.Text;
        public object Content => item.Kind.Length == 0 ? item.Text : $"{item.Text}  ·  {item.Kind}";
        public object Description => item.Detail;
        public double Priority => 0;

        public void Complete(TextArea textArea, ISegment completionSegment, EventArgs insertionRequestEventArgs)
        {
            if (!pane.Code.IsReadOnly)
                textArea.Document.Replace(completionSegment, item.Text);
        }
    }
}
