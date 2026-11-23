using System.Globalization;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public sealed class TerminalViewport : Control
{
    private static readonly int[] DarkAnsi =
        [0x161616, 0xa32130, 0x216443, 0x885c16, 0x284b9f, 0x85478c, 0x1f6a73, 0xb4b4b4,
         0x5c5c5c, 0xb3253a, 0x287653, 0x946413, 0x3558bd, 0x90449d, 0x166678, 0xffffff];
    private static readonly int[] LightAnsi =
        [0x111111, 0xcd585f, 0x79c093, 0xdcb26a, 0x87a7ef, 0xc990cd, 0x78c5cb, 0xc2c6cd,
         0x777b83, 0xfb7379, 0x93d8a7, 0xf6cd84, 0xa6bfff, 0xe2abe6, 0x95e0e2, 0xffffff];
    private readonly Dictionary<int, SolidColorBrush> _brushes = new();
    private TerminalScreen.Frame? _frame;
    private bool _active;
    private double _cellWidth = 8, _cellHeight = 18;
    private (int Column, int Row)? _selectionStart, _selectionEnd;

    internal TerminalScreen.Frame? Frame
    {
        get => _frame;
        set
        {
            if (_frame?.ScrollOffset != value?.ScrollOffset || _frame?.Alternate != value?.Alternate ||
                _frame?.Columns != value?.Columns || _frame?.Rows != value?.Rows)
                _selectionStart = _selectionEnd = null;
            _frame = value;
            InvalidateVisual();
        }
    }

    internal bool Active
    {
        get => _active;
        set { _active = value; InvalidateVisual(); }
    }

    internal (int Columns, int Rows) Capacity()
    {
        var dpi = VisualTreeHelper.GetDpi(this).PixelsPerDip;
        var probe = Format("M", Foreground ?? Brushes.Black, bold: false, dpi);
        _cellWidth = Math.Max(1, probe.WidthIncludingTrailingWhitespace);
        _cellHeight = Math.Max(1, Math.Ceiling(probe.Height * 1.13));
        if (ActualWidth <= 0 || ActualHeight <= 0)
            return (80, 24);
        return TerminalScreen.Dimensions(
            (int)((ActualWidth - Padding.Left - Padding.Right) / _cellWidth),
            (int)((ActualHeight - Padding.Top - Padding.Bottom) / _cellHeight));
    }

    internal (int Column, int Row)? Hit(Point point)
    {
        if (_frame is not { } frame || point.X < Padding.Left || point.Y < Padding.Top)
            return null;
        var column = (int)((point.X - Padding.Left) / _cellWidth);
        var row = (int)((point.Y - Padding.Top) / _cellHeight);
        return column < frame.Columns && row < frame.Rows ? (column + 1, row + 1) : null;
    }

    internal string CopyText()
    {
        if (_frame is not { } frame)
            return "";
        var lines = new List<string>(frame.Rows);
        var selection = Selection();
        for (var row = 0; row < frame.Rows; row++)
        {
            var line = frame.Lines[row].Span;
            if (selection is { } selected && (row + 1 < selected.Start.Row || row + 1 > selected.End.Row))
                continue;
            var first = selection is { } from && row + 1 == from.Start.Row ? from.Start.Column - 1 : 0;
            var last = selection is { } to && row + 1 == to.End.Row ? to.End.Column - 1 : line.Length - 1;
            if (first > 0 && first < line.Length && line[first].Width == 0)
                first--;
            var text = new StringBuilder();
            for (var index = first; index <= last && index < line.Length; index++)
            {
                var cell = line[index];
                if (cell.Width == 0)
                    continue;
                if (!cell.Style.Hidden && cell.Text is { } value)
                    text.Append(value);
                else
                    text.Append(' ', cell.Width);
            }
            lines.Add(text.ToString().TrimEnd());
        }
        while (lines.Count > 0 && lines[^1].Length == 0)
            lines.RemoveAt(lines.Count - 1);
        return string.Join(Environment.NewLine, lines);
    }

    internal bool BeginSelection(Point point)
    {
        _selectionStart = _selectionEnd = Hit(point);
        InvalidateVisual();
        return _selectionStart is not null;
    }

    internal void ExtendSelection(Point point)
    {
        if (Hit(point) is { } hit)
        {
            _selectionEnd = hit;
            InvalidateVisual();
        }
    }

    private ((int Column, int Row) Start, (int Column, int Row) End)? Selection()
    {
        if (_selectionStart is not { } start || _selectionEnd is not { } end || start == end)
            return null;
        return start.Row > end.Row || start.Row == end.Row && start.Column > end.Column ? (end, start) : (start, end);
    }

    protected override void OnPropertyChanged(DependencyPropertyChangedEventArgs e)
    {
        base.OnPropertyChanged(e);
        if (e.Property == BackgroundProperty || e.Property == ForegroundProperty || e.Property == FontFamilyProperty ||
            e.Property == FontSizeProperty || e.Property == PaddingProperty)
            InvalidateVisual();
    }

    protected override void OnDpiChanged(DpiScale oldDpi, DpiScale newDpi)
    {
        base.OnDpiChanged(oldDpi, newDpi);
        Capacity();
        MetricsChanged?.Invoke(this, EventArgs.Empty);
    }

    internal event EventHandler? MetricsChanged;

    protected override void OnRender(DrawingContext drawing)
    {
        base.OnRender(drawing);
        var bounds = new Rect(RenderSize);
        var defaultBackground = Background ?? Brushes.White;
        var defaultForeground = Foreground ?? Brushes.Black;
        drawing.DrawRectangle(defaultBackground, null, bounds);
        if (_frame is not { } frame)
            return;

        var dpi = VisualTreeHelper.GetDpi(this).PixelsPerDip;
        var darkSurface = defaultBackground is SolidColorBrush background && background.Color.R < 100;
        drawing.PushClip(new RectangleGeometry(bounds));
        for (var row = 0; row < frame.Rows && Padding.Top + row * _cellHeight < ActualHeight; row++)
        {
            var line = frame.Lines[row].Span;
            DrawLine(drawing, row, line, dpi, darkSurface, defaultForeground, defaultBackground);
        }
        if (Selection() is { } selected)
        {
            drawing.PushOpacity(0.34);
            var brush = TryFindResource(SystemColors.HighlightBrushKey) as Brush ?? Brushes.SteelBlue;
            for (var row = selected.Start.Row; row <= selected.End.Row; row++)
            {
                var from = row == selected.Start.Row ? selected.Start.Column : 1;
                var to = row == selected.End.Row ? selected.End.Column : frame.Columns;
                drawing.DrawRectangle(brush, null,
                    new Rect(Position(from - 1, row - 1), new Size((to - from + 1) * _cellWidth, _cellHeight)));
            }
            drawing.Pop();
        }
        if (_active && frame.CursorVisible)
        {
            var cursor = Position(frame.CursorColumn, frame.CursorRow);
            drawing.DrawRectangle(defaultForeground, null, new Rect(cursor.X, cursor.Y + _cellHeight - 2, _cellWidth, 2));
        }
        drawing.Pop();
    }

    private void DrawLine(DrawingContext drawing, int row, ReadOnlySpan<TerminalScreen.Cell> line, double dpi,
        bool darkSurface, Brush defaultForeground, Brush defaultBackground)
    {
        // Paint all background runs before glyphs so wide characters stay visible.
        for (var column = 0; column < line.Length;)
        {
            var style = line[column].Style;
            var (background, inverse) = (style.Inverse ? style.Foreground : style.Background, style.Inverse);
            var end = column + 1;
            while (end < line.Length && line[end].Style.Inverse == inverse &&
                   (inverse ? line[end].Style.Foreground : line[end].Style.Background) == background)
                end++;
            if (background is not null || inverse)
                drawing.DrawRectangle(ColorBrush(background, darkSurface, defaultForeground), null,
                    new Rect(Position(column, row), new Size((end - column) * _cellWidth, _cellHeight)));
            column = end;
        }
        var scratch = new StringBuilder();
        for (var column = 0; column < line.Length;)
        {
            var cell = line[column];
            var style = cell.Style;
            var end = column + 1;
            if (!style.Hidden && cell.Text is { } text)
            {
                if (Ascii(text))
                {
                    scratch.Clear().Append(text);
                    while (end < line.Length && line[end].Style == style && line[end].Text is { } next && Ascii(next))
                    {
                        scratch.Append(next);
                        end++;
                    }
                    text = scratch.ToString();
                }
                var brush = style.Inverse
                    ? ColorBrush(style.Background, darkSurface, defaultBackground)
                    : ColorBrush(style.Foreground, darkSurface, defaultForeground);
                var start = Position(column, row);
                var run = Format(text, brush, style.Bold, dpi);
                drawing.DrawText(run, new Point(start.X, start.Y + (_cellHeight - run.Height) / 2));
                if (style.Underline)
                    drawing.DrawRectangle(brush, null,
                        new Rect(start.X, start.Y + _cellHeight - 2, Math.Max(cell.Width, end - column) * _cellWidth, 1));
            }
            column = end;
        }
    }

    private Point Position(int column, int row) => new(Padding.Left + column * _cellWidth, Padding.Top + row * _cellHeight);

    private static bool Ascii(string text) => text.Length == 1 && text[0] is >= ' ' and <= '~';

    private FormattedText Format(string text, Brush ink, bool bold, double dpi) => new(text, CultureInfo.InvariantCulture,
        FlowDirection.LeftToRight, new Typeface(FontFamily, FontStyles.Normal, bold ? FontWeights.Bold : FontWeights.Normal,
            FontStretches.Normal), FontSize, ink, dpi);

    private Brush ColorBrush(int? color, bool darkSurface, Brush fallback)
    {
        if (color is not { } value)
            return fallback;
        if (value < 0)
        {
            var index = -value - 1;
            if (index < 16)
                value = (darkSurface ? LightAnsi : DarkAnsi)[index];
            else if (index < 232)
            {
                var cube = index - 16;
                value = (Cube(cube / 36) << 16) | (Cube(cube / 6 % 6) << 8) | Cube(cube % 6);
            }
            else
                value = (8 + (index - 232) * 10) * 0x010101;
        }
        if (_brushes.TryGetValue(value, out var hit))
            return hit;
        var brush = new SolidColorBrush(Color.FromRgb((byte)(value >> 16), (byte)(value >> 8), (byte)value));
        brush.Freeze();
        if (_brushes.Count >= 1024)
            _brushes.Clear();
        _brushes.Add(value, brush);
        return brush;
    }

    private static int Cube(int component) => component == 0 ? 0 : 55 + 40 * component;
}
