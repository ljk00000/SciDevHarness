using System.IO;
using System.Windows.Media.Imaging;
using Windows.Data.Pdf;
using Windows.Storage.Streams;

namespace SciDev.Desktop;

internal sealed class PdfReader : IDisposable
{
    private readonly IRandomAccessStream _source;
    private readonly PdfDocument _document;
    private int _rendering;
    private bool _closed;

    private PdfReader(IRandomAccessStream source, PdfDocument document) => (_source, _document) = (source, document);

    internal uint PageCount => _document.PageCount;

    internal static async Task<PdfReader> OpenAsync(byte[] data, CancellationToken token)
    {
        var source = new MemoryStream(data, writable: false).AsRandomAccessStream();
        try
        {
            var document = await PdfDocument.LoadFromStreamAsync(source);
            token.ThrowIfCancellationRequested();
            return new(source, document);
        }
        catch
        {
            source.Dispose();
            throw;
        }
    }

    internal async Task<BitmapSource> RenderAsync(uint index, int desiredWidth, CancellationToken token)
    {
        ObjectDisposedException.ThrowIf(_closed, this);
        _rendering++;
        try
        {
            using var page = _document.GetPage(index);
            var size = page.Size;
            if (size.Width <= 0 || size.Height <= 0 || !float.IsFinite((float)(size.Height / size.Width)))
                throw new InvalidDataException("PDF 页面大小无效。");
            var scale = Math.Min(desiredWidth / size.Width, 3200 / Math.Max(size.Width, size.Height));
            var options = new PdfPageRenderOptions
            {
                DestinationWidth = (uint)Math.Max(1, Math.Round(size.Width * scale)),
                DestinationHeight = (uint)Math.Max(1, Math.Round(size.Height * scale)),
            };
            using var rendered = new InMemoryRandomAccessStream();
            await page.RenderToStreamAsync(rendered, options);
            token.ThrowIfCancellationRequested();
            rendered.Seek(0);
            using var stream = rendered.AsStreamForRead();
            return BitmapFrame.Create(stream, BitmapCreateOptions.PreservePixelFormat, BitmapCacheOption.OnLoad);
        }
        finally
        {
            if (--_rendering == 0 && _closed)
                _source.Dispose();
        }
    }

    public void Dispose()
    {
        if (_closed)
            return;
        _closed = true;
        if (_rendering == 0)
            _source.Dispose();
    }
}
