using System.Windows.Media;
using ICSharpCode.AvalonEdit.Document;
using ICSharpCode.AvalonEdit.Rendering;

namespace SciDev.Desktop;

internal sealed class DarkSyntax : DocumentColorizingTransformer
{
    private readonly Dictionary<Color, SolidColorBrush> _colors = [];

    protected override void ColorizeLine(DocumentLine line)
    {
        if (line.Length != 0)
            ChangeLinePart(line.Offset, line.EndOffset, Adapt);
    }

    private void Adapt(VisualLineElement element)
    {
        if (element.TextRunProperties.ForegroundBrush is not SolidColorBrush source)
            return;
        var color = source.Color;
        if (0.21 * color.R + 0.72 * color.G + 0.07 * color.B >= 143)
            return;
        if (!_colors.TryGetValue(color, out var adjusted))
        {
            adjusted = new SolidColorBrush(Color.FromRgb(Lighter(color.R), Lighter(color.G), Lighter(color.B)));
            adjusted.Freeze();
            _colors.Add(color, adjusted);
        }
        element.TextRunProperties.SetForegroundBrush(adjusted);
    }

    private static byte Lighter(byte value) => (byte)(value + (255 - value) * 0.53);
}
