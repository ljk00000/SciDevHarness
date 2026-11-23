using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;
using System.Xml;
using SciDev.Core;
using Svg;

namespace SciDev.Infrastructure;

[SupportedOSPlatform("windows")]
public static class NativePelicanValidator
{
    internal static readonly Color Background = Color.FromArgb(244, 247, 251);
    private const int PreviewWidth = 1200;
    private const int PreviewHeight = 800;

    static NativePelicanValidator()
    {
        SvgDocument.DisableDtdProcessing = true;
        SvgDocument.ResolveExternalXmlEntites = ExternalType.None;
        SvgDocument.ResolveExternalImages = ExternalType.None;
        SvgDocument.ResolveExternalElements = ExternalType.None;
    }

    public static ArtifactAcceptance CheckFile(string path, string? previewPath, CancellationToken token)
    {
        path = Path.GetFullPath(path);
        if (!path.EndsWith(".svg", StringComparison.OrdinalIgnoreCase) ||
            previewPath is not null && !previewPath.EndsWith(".png", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Expected .svg input and .png preview paths.");
        using var input = File.OpenRead(path);
        if (input.Length is 0 or > StaticSvg.MaxBytes)
            return new("failed", path, "SVG is empty or exceeds the 512 KiB artifact limit.");
        var source = new byte[(int)input.Length];
        input.ReadExactly(source);

        using var encodedPreview = previewPath is not null ? new MemoryStream() : null;
        var result = Verify(source, token, encodedPreview);
        if (previewPath is not null && encodedPreview is { Length: > 0 })
        {
            token.ThrowIfCancellationRequested();
            using var output = new FileStream(previewPath, FileMode.CreateNew, FileAccess.Write, FileShare.None);
            encodedPreview.Position = 0;
            encodedPreview.CopyTo(output);
        }
        return new(result.Status, path, result.Detail);
    }

    internal static (string Status, string Detail, string[] Guidance) Verify(
        byte[] source, CancellationToken token, Stream? png = null)
    {
        StaticSvg svg;
        try
        {
            svg = StaticSvg.Read(source, token);
        }
        catch (InvalidDataException ex)
        {
            return ("failed", ex.Message, []);
        }
        catch (Exception ex) when (ex is XmlException or DecoderFallbackException)
        {
            return ("failed", "SVG must be well-formed UTF-8 XML without a DTD.", []);
        }

        try
        {
            var document = Open(svg);
            SvgRasterBudget.Check(document, PreviewWidth, PreviewHeight, token);
            using var preview = Render(document, token);
            var visibleSamples = CountVisibleSamples(preview, token);
            if (visibleSamples < 25)
                return ("failed", "SVG rendered almost no visible pixels.", []);
            if (png is not null)
                preview.Save(png, ImageFormat.Png);
            var checks = new PelicanGeometry(svg, document);
            var issues = checks.Check(token);
            return issues.Length > 0
                ? ("failed", string.Join(" ", issues.Take(18)), [])
                : ("passed", $"The SVG passed native rendering and geometry checks ({visibleSamples} visible samples).", []);
        }
        catch (InvalidDataException ex)
        {
            return ("failed", ex.Message, []);
        }
        catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or ExternalException or
            FormatException or OverflowException)
        {
            return ("failed", "The SVG could not render using the supported static shapes and dimensions.", []);
        }
    }

    private static SvgDocument Open(StaticSvg svg)
    {
        using var reader = svg.Root.CreateReader();
        return SvgDocument.Open<SvgDocument>(reader);
    }

    private static Bitmap Render(SvgDocument document, CancellationToken token)
    {
        var size = document.GetDimensions();
        if (size.Width is < 1 or > 8192 || size.Height is < 1 or > 8192 ||
            !float.IsFinite(size.Width) || !float.IsFinite(size.Height))
            throw new ArgumentException("SVG intrinsic dimensions must be between 1 and 8192 pixels.");

        token.ThrowIfCancellationRequested();
        using var raster = document.Draw(PreviewWidth, PreviewHeight);
        var preview = new Bitmap(PreviewWidth, PreviewHeight);
        try
        {
            using var graphics = Graphics.FromImage(preview);
            graphics.Clear(Background);
            graphics.DrawImageUnscaled(raster, 0, 0);
            return preview;
        }
        catch
        {
            preview.Dispose();
            throw;
        }
    }

    private static int CountVisibleSamples(Bitmap preview, CancellationToken token)
    {
        var background = Background.ToArgb();
        var samples = 0;
        for (var y = 0; y < preview.Height; y += 4)
        {
            token.ThrowIfCancellationRequested();
            for (var x = 0; x < preview.Width; x += 4)
                if (preview.GetPixel(x, y).ToArgb() != background)
                    samples++;
        }
        return samples;
    }
}
