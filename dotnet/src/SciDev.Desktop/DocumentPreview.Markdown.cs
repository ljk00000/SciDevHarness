using System.IO;
using System.Windows;
using System.Windows.Data;
using System.Windows.Documents;
using System.Windows.Media.Imaging;
using Markdig.Syntax;
using Markdig.Syntax.Inlines;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class DocumentPreview
{
    private static (MarkdownDocument Source, Dictionary<string, BitmapSource> Images) ReadMarkdown(
        string workspace, PreviewDocument document, CancellationToken token)
    {
        var source = MarkdownReader.Parse(document.ReadText());
        var images = new Dictionary<string, BitmapSource>(StringComparer.Ordinal);
        var references = source.Descendants<LinkInline>().Where(link => link.IsImage)
            .Select(MarkdownReader.LinkUrl).OfType<string>().Distinct(StringComparer.Ordinal)
            .Select(url => (Url: url, Path: ResolveLocalLink(workspace, document.Path, url)))
            .Where(link => link.Path is not null && PreviewDocument.KindOf(link.Path) == PreviewKind.Image).Take(8);
        var files = new WorkspaceDocuments(workspace);
        foreach (var reference in references)
        {
            token.ThrowIfCancellationRequested();
            try
            {
                var image = files.OpenPreview(reference.Path!);
                images.Add(reference.Url, ReadImage(image.Bytes, maxDimension: 1440));
            }
            catch (Exception ex) when (ExpectedPreviewError(ex))
            {
                // The document can still render the image's alternative text.
            }
        }
        return (source, images);
    }

    private void SetMarkdown(MarkdownDocument source, IReadOnlyDictionary<string, BitmapSource>? images = null)
    {
        var document = MarkdownReader.Render(source,
            raw => _workspace is { } workspace && ActivePath is { } path ? ResolveLocalLink(workspace, path, raw) : null,
            path => OpenRequested?.Invoke(path), images);
        BindingOperations.SetBinding(document, TextElement.FontFamilyProperty, new Binding(nameof(FontFamily)) { Source = this });
        MarkdownView.Document = document;
        TextSheet.Visibility = MarkdownView.Visibility = Visibility.Visible;
        ShowState("");
    }

    private static string? ResolveLocalLink(string workspace, string path, string raw)
    {
        if (Uri.TryCreate(raw, UriKind.Absolute, out _))
            return null;
        try
        {
            var link = Uri.UnescapeDataString(raw.Split(['#', '?'], 2)[0]);
            if (link.Length == 0 || Path.IsPathRooted(link))
                return null;
            var target = Path.GetFullPath(Path.Combine(workspace, Path.GetDirectoryName(path) ?? "", link));
            var relative = Path.GetRelativePath(workspace, target).Replace('\\', '/');
            return PreviewDocument.CanOpen(relative) ? relative : null;
        }
        catch (Exception ex) when (ex is ArgumentException or UriFormatException or IOException) { return null; }
    }

    private async void SwitchMarkdownMode(object sender, RoutedEventArgs e)
    {
        if (_workspace is { } workspace && ActivePath is { } path && PreviewDocument.KindOf(path) == PreviewKind.Markdown)
            await OpenAsync(workspace, path, asText: !_markdownSource);
    }
}
