using System.Drawing.Drawing2D;
using System.Runtime.Versioning;
using Svg;

namespace SciDev.Infrastructure;

[SupportedOSPlatform("windows")]
internal static class SvgRasterBudget
{
    private const double MaxSurfaceEdge = 4096;
    private const double MaxSurfacePixels = 8_000_000;
    private const double MaxTotalSurfacePixels = 24_000_000;

    internal static void Check(SvgDocument document, int width, int height, CancellationToken token)
    {
        var viewportScale = Math.Max(1, Math.Max(width / document.ViewBox.Width, height / document.ViewBox.Height));
        double totalSurfacePixels = 0;
        CheckElement(document, viewportScale, ref totalSurfacePixels, token);
    }

    private static void CheckElement(SvgElement element, double scale, ref double total, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        var localScale = LinearScale(element);
        if (element is SvgVisualElement visual && !float.IsFinite(visual.Opacity))
            throw new InvalidDataException("SVG opacity must be finite.");
        if (element is SvgVisualElement translucent && translucent.Opacity < 1)
        {
            // The renderer draws opacity into a temporary bitmap before compositing it on the fixed-size preview.
            // Check both local and transformed bounds, and account for cascaded transforms before invoking Draw.
            var local = translucent.Path(null)?.GetBounds() ?? translucent.Bounds;
            var transformed = translucent.Bounds;
            var width = Math.Ceiling(Math.Max(local.Width * localScale, transformed.Width) * scale);
            var height = Math.Ceiling(Math.Max(local.Height * localScale, transformed.Height) * scale);
            total += width * height;
            if (IsUnderDefinition(element) || !double.IsFinite(total) || width > MaxSurfaceEdge ||
                height > MaxSurfaceEdge || width * height > MaxSurfacePixels || total > MaxTotalSurfacePixels)
                throw new InvalidDataException("SVG opacity exceeds the supported compositing budget. Use fill-opacity or stroke-opacity on individual shapes.");
        }
        foreach (var child in element.Children)
            CheckElement(child, scale * localScale, ref total, token);
    }

    private static bool IsUnderDefinition(SvgElement element) =>
        element.Parents.Any(parent => parent is SvgDefinitionList);

    private static double LinearScale(SvgElement element)
    {
        if (element.Transforms is not { Count: > 0 } transforms)
            return 1;
        using var matrix = transforms.GetMatrix();
        var values = matrix.Elements;
        var max = Math.Max(Math.Abs(values[0]) + Math.Abs(values[2]), Math.Abs(values[1]) + Math.Abs(values[3]));
        if (!float.IsFinite(max) || max > 64)
            throw new InvalidDataException("SVG transform exceeds the supported scaling range.");
        return Math.Max(1, max);
    }
}
