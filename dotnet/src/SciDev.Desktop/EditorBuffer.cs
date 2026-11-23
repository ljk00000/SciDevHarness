using System.ComponentModel;
using ICSharpCode.AvalonEdit.Document;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal sealed class EditorBuffer : INotifyPropertyChanged
{
    public EditorBuffer(WorkspaceDocuments.Document snapshot) : this(snapshot.Path, snapshot.Text) => Snapshot = snapshot;

    public EditorBuffer(string path, string text)
    {
        Path = path;
        Document = new(text) { FileName = path };
        Document.UndoStack.MarkAsOriginalFile();
        Document.UndoStack.PropertyChanged += (_, e) =>
        {
            if (e.PropertyName == nameof(UndoStack.IsOriginalFile))
                PropertyChanged?.Invoke(this, new(nameof(Caption)));
        };
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    public string Path { get; }
    public string Caption => System.IO.Path.GetFileName(Path) + (IsModified ? " *" : "");
    public bool IsModified => !Document.UndoStack.IsOriginalFile;
    public TextDocument Document { get; }
    public WorkspaceDocuments.Document? Snapshot { get; private set; }

    public void MarkSaved(WorkspaceDocuments.Document snapshot)
    {
        Snapshot = snapshot;
        Document.UndoStack.MarkAsOriginalFile();
    }

    public void Reload(WorkspaceDocuments.Document snapshot)
    {
        Document.Text = snapshot.Text;
        Document.UndoStack.ClearAll();
        MarkSaved(snapshot);
    }
}
