using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media.Imaging;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class DocumentPreview : UserControl
{
    private CancellationTokenSource? _opening;
    private PdfReader? _pdf;
    private string? _workspace;
    private uint _page;
    private bool _rendering;
    private double? _scale;
    private bool _markdownSource;

    private static readonly DependencyPropertyKey ActivePathPropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(ActivePath), typeof(string), typeof(DocumentPreview), new PropertyMetadata(null));
    public static readonly DependencyProperty ActivePathProperty = ActivePathPropertyKey.DependencyProperty;
    public string? ActivePath => (string?)GetValue(ActivePathProperty);

    internal bool IsOpen => ActivePath is not null;
    internal event RoutedEventHandler? CloseRequested;
    internal event RoutedEventHandler? ChatRequested;
    internal event RoutedEventHandler? FocusRequested;
    internal event Action<string>? OpenRequested;

    public DocumentPreview() => InitializeComponent();

    internal void SetCompact(bool compact) => BackToChat.Visibility = compact ? Visibility.Visible : Visibility.Collapsed;
    internal void SetFocused(bool focused)
    {
        FocusReaderButton.Content = focused ? "退出专注" : "专注阅读";
    }

    internal async Task OpenAsync(string workspace, string path, bool asText = false)
    {
        Clear();
        _workspace = workspace;
        SetTitle(path, asText);
        ShowState("正在读取文档…");
        using var opening = new CancellationTokenSource();
        _opening = opening;
        try
        {
            var document = await Task.Run(() => new WorkspaceDocuments(workspace).OpenPreview(path, asText), opening.Token);
            opening.Token.ThrowIfCancellationRequested();
            SetTitle(document.Path, asText);
            switch (document.Kind)
            {
                case PreviewKind.Markdown:
                    var markdown = await Task.Run(() => ReadMarkdown(workspace, document, opening.Token), opening.Token);
                    opening.Token.ThrowIfCancellationRequested();
                    SetMarkdown(markdown.Source, markdown.Images);
                    break;
                case PreviewKind.Pdf:
                    _pdf = await PdfReader.OpenAsync(document.Bytes, opening.Token);
                    ReaderTools.Visibility = _pdf.PageCount > 0 ? Visibility.Visible : Visibility.Collapsed;
                    await RenderPageAsync();
                    break;
                case PreviewKind.Image:
                    var bitmap = await Task.Run(() => ReadImage(document.Bytes), opening.Token);
                    opening.Token.ThrowIfCancellationRequested();
                    DisplayPicture(bitmap);
                    break;
                case PreviewKind.Text:
                    var text = await Task.Run(document.ReadText, opening.Token);
                    opening.Token.ThrowIfCancellationRequested();
                    PlainText.Text = text;
                    TextSheet.Visibility = PlainText.Visibility = Visibility.Visible;
                    PlainText.ScrollToHome();
                    ShowState("");
                    break;
            }
        }
        catch (OperationCanceledException) when (opening.IsCancellationRequested) { }
        catch (Exception ex) when (ExpectedPreviewError(ex))
        {
            if (_opening == opening)
                ShowState(ex is ToolDeniedException denied ? denied.Message : asText
                    ? "无法以纯文本打开此文件。请确认文件可访问，且为 UTF-8 或带 BOM 的 UTF-16 文本。"
                    : "无法预览此文件。请检查格式、访问权限或 PDF 是否加密。");
        }
        finally
        {
            if (_opening == opening)
                _opening = null;
        }
    }

    internal void Clear()
    {
        _opening?.Cancel();
        _opening = null;
        _pdf?.Dispose();
        _pdf = null;
        SetValue(ActivePathPropertyKey, null);
        MarkdownModeButton.Visibility = Visibility.Collapsed;
        _markdownSource = false;
        _workspace = null;
        _page = 0;
        _scale = null;
        _rendering = false;
        MarkdownView.Document = null;
        PlainText.Clear();
        Picture.Source = null;
        TextSheet.Visibility = MarkdownView.Visibility = PlainText.Visibility =
            ImageViewport.Visibility = ReaderTools.Visibility = Visibility.Collapsed;
        ShowState("");
    }

    private void SetTitle(string path, bool asText = false)
    {
        SetValue(ActivePathPropertyKey, path);
        var name = Path.GetFileName(path);
        FileTitle.Text = name;
        FileTitle.ToolTip = path;
        var folder = Path.GetDirectoryName(path)?.Replace('\\', '/');
        var isMarkdown = PreviewDocument.KindOf(path) == PreviewKind.Markdown;
        _markdownSource = isMarkdown && asText;
        MarkdownModeButton.Visibility = isMarkdown ? Visibility.Visible : Visibility.Collapsed;
        MarkdownModeButton.Content = _markdownSource ? "预览 Markdown" : "查看源码";
        var mode = isMarkdown ? asText ? "Markdown 源码 · 只读" : "Markdown 预览 · 只读" :
            asText ? "纯文本 · 只读" : "只读预览";
        FilePath.Text = $"{(string.IsNullOrEmpty(folder) ? "工作区" : folder)} · {mode}";
    }

    private async Task RenderPageAsync()
    {
        if (_pdf is not { } pdf || _rendering || pdf.PageCount == 0 || _page >= pdf.PageCount)
        {
            if (_pdf is { PageCount: 0 })
                ShowState("PDF 没有可显示的页面。");
            return;
        }
        _rendering = true;
        UpdateReaderTools();
        try
        {
            var frame = await pdf.RenderAsync(_page, 1800, _opening?.Token ?? CancellationToken.None);
            if (_pdf == pdf)
                DisplayPicture(frame);
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) when (ExpectedPreviewError(ex))
        {
            if (_pdf == pdf)
            {
                Picture.Source = null;
                ShowState("无法读取这一页。");
            }
        }
        finally
        {
            if (_pdf == pdf)
            {
                _rendering = false;
                UpdateReaderTools();
            }
        }
    }

    private void DisplayPicture(BitmapSource image)
    {
        Picture.Source = image;
        ImageViewport.Visibility = ReaderTools.Visibility = Visibility.Visible;
        UpdateReaderTools();
        SetPictureSize();
        ImageViewport.ScrollToHome();
        ShowState("");
    }

    private static BitmapSource ReadImage(byte[] bytes, int maxDimension = 2400)
    {
        using var stream = new MemoryStream(bytes, writable: false);
        var decoder = BitmapDecoder.Create(stream, BitmapCreateOptions.DelayCreation, BitmapCacheOption.None);
        if (decoder.Frames.FirstOrDefault() is not { } frame || (long)frame.PixelWidth * frame.PixelHeight > 40_000_000)
            throw new InvalidDataException("图片尺寸超出预览上限。");
        stream.Position = 0;
        var bitmap = new BitmapImage();
        bitmap.BeginInit();
        bitmap.CacheOption = BitmapCacheOption.OnLoad;
        bitmap.StreamSource = stream;
        if (frame.PixelWidth >= frame.PixelHeight)
            bitmap.DecodePixelWidth = Math.Min(frame.PixelWidth, maxDimension);
        else
            bitmap.DecodePixelHeight = Math.Min(frame.PixelHeight, maxDimension);
        bitmap.EndInit();
        bitmap.Freeze();
        return bitmap;
    }

    private void UpdateReaderTools()
    {
        var visibility = _pdf is null ? Visibility.Collapsed : Visibility.Visible;
        PreviousPage.Visibility = NextPage.Visibility = PageLabel.Visibility = visibility;
        PageLabel.Text = _pdf is { } pdf ? $"{_page + 1} / {pdf.PageCount}" : "";
        PreviousPage.IsEnabled = !_rendering && _page > 0;
        NextPage.IsEnabled = !_rendering && _pdf is { } current && _page + 1 < current.PageCount;
        ScaleButton.Content = _scale is { } scale ? $"{scale:P0}" : "适应宽度";
    }

    private async void GoPrevious(object sender, RoutedEventArgs e)
    {
        if (_page > 0 && !_rendering)
        {
            _page--;
            await RenderPageAsync();
        }
    }

    private async void GoNext(object sender, RoutedEventArgs e)
    {
        if (_pdf is { } pdf && _page + 1 < pdf.PageCount && !_rendering)
        {
            _page++;
            await RenderPageAsync();
        }
    }

    private void ResizeImage(object sender, SizeChangedEventArgs e) => SetPictureSize();
    private void ZoomIn(object sender, RoutedEventArgs e) => Zoom(1.25);
    private void ZoomOut(object sender, RoutedEventArgs e) => Zoom(0.8);
    private void Zoom(double factor)
    {
        _scale = Math.Clamp((_scale ?? 1) * factor, 0.5, 3);
        UpdateReaderTools();
        SetPictureSize();
    }

    private void FitWidth(object sender, RoutedEventArgs e)
    {
        _scale = null;
        UpdateReaderTools();
        SetPictureSize();
    }

    private void SetPictureSize()
    {
        if (Picture.Source is BitmapSource bitmap && ImageViewport.ActualWidth > 40)
        {
            var fit = Math.Min(980, ImageViewport.ActualWidth - 44);
            Picture.Width = (_pdf is null ? Math.Min(fit, bitmap.PixelWidth) : fit) * (_scale ?? 1);
        }
    }

    private void ShowState(string message)
    {
        State.Text = message;
        State.Visibility = message.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
    }

    private void CloseView(object sender, RoutedEventArgs e) => CloseRequested?.Invoke(sender, e);
    private void ReturnToChat(object sender, RoutedEventArgs e) => ChatRequested?.Invoke(sender, e);
    private void ToggleFocus(object sender, RoutedEventArgs e) => FocusRequested?.Invoke(sender, e);
    internal void CaptureMarkdown(string path, string markdown)
    {
        Clear();
        SetTitle(path);
        SetMarkdown(MarkdownReader.Parse(markdown));
    }

    private static bool ExpectedPreviewError(Exception error) => error is ToolDeniedException or IOException or ArgumentException
        or UnauthorizedAccessException or COMException or NotSupportedException or FormatException or InvalidDataException;
}
