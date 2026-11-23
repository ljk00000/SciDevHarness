using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

public enum PreviewKind { Unsupported, Markdown, Pdf, Image, Text }

public sealed record PreviewDocument(string Path, PreviewKind Kind, byte[] Bytes)
{
    public static PreviewKind KindOf(string path) => System.IO.Path.GetExtension(path).ToLowerInvariant() switch
    {
        ".md" or ".markdown" => PreviewKind.Markdown,
        ".pdf" => PreviewKind.Pdf,
        ".png" or ".jpg" or ".jpeg" or ".gif" or ".bmp" or ".tif" or ".tiff" => PreviewKind.Image,
        ".txt" or ".log" or ".csv" or ".tsv" => PreviewKind.Text,
        _ => PreviewKind.Unsupported,
    };

    public static bool CanOpen(string path) => KindOf(path) != PreviewKind.Unsupported && WorkspaceDocuments.IsVisible(path);

    public string ReadText()
    {
        var bytes = Bytes.AsSpan();
        var text = bytes.StartsWith(Encoding.Unicode.Preamble) ? new UnicodeEncoding(false, true, true).GetString(bytes[2..])
            : bytes.StartsWith(Encoding.BigEndianUnicode.Preamble) ? new UnicodeEncoding(true, true, true).GetString(bytes[2..])
            : WorkspaceFiles.Utf8.GetString(bytes.StartsWith(Encoding.UTF8.Preamble) ? bytes[3..] : bytes);
        if (text.Contains('\0'))
            throw new ToolDeniedException("preview_text", "此文件包含无法显示的二进制内容。");
        return text;
    }
}
