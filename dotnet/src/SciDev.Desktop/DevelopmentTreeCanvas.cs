using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace SciDev.Desktop;

internal sealed class DevelopmentTreeCanvas : Canvas
{
    private sealed record Placement(DevelopmentVersion Node, Point Dot, Rect Card);

    private const double CardHeight = 90;
    private readonly Dictionary<string, Placement> _positions = new(StringComparer.Ordinal);
    private readonly Dictionary<string, Point> _attemptOffsets = new(StringComparer.Ordinal);
    private DevelopmentVersion[] _nodes = [];
    private string _selected = DevelopmentVersion.MainRoot;
    private bool _paper = true;
    private bool _focus;
    private Size _graphViewport;
    private (string Id, Point Start, Point Offset)? _drag;

    internal event Action<DevelopmentVersion>? VersionSelected;

    public DevelopmentTreeCanvas()
    {
        ClipToBounds = true;
    }

    internal void SetVersions(DevelopmentVersion[] nodes, string selected)
    {
        _nodes = nodes;
        _selected = selected;
        var ids = nodes.Select(node => node.Id).ToHashSet(StringComparer.Ordinal);
        foreach (var id in _attemptOffsets.Keys.Where(id => !ids.Contains(id)).ToArray())
            _attemptOffsets.Remove(id);
        Rebuild();
    }

    internal void ResetOffsets() => _attemptOffsets.Clear();

    internal void SetProfile(string profile)
    {
        _paper = profile == "paper";
        _focus = profile == "focus";
        Rebuild();
    }

    internal void SetViewport(Size viewport)
    {
        if (viewport == _graphViewport)
            return;
        _graphViewport = viewport;
        Rebuild();
    }

    internal Brush Accent(DevelopmentVersion node) => BrushFor(node.State switch
    {
        DevelopmentState.Main => _paper ? "#13836B" : "#55E3C1",
        DevelopmentState.Failed => _paper ? "#B7473C" : "#FF8978",
        DevelopmentState.Active => _paper ? "#266DC0" : "#70B8FF",
        DevelopmentState.Retry => _paper ? "#91600C" : "#F2CA78",
        _ => _paper ? "#727E8A" : "#A7B1B8",
    });

    protected override void OnRender(DrawingContext drawing)
    {
        var width = RenderSize.Width;
        var height = RenderSize.Height;
        var bounds = new Rect(RenderSize);
        var backdrop = _paper ? Gradient((0, "#F9FBFD"), (0.18, "#F5F8FB"), (0.55, "#F1F5F9"), (1, "#EDF2F7"))
            : _focus ? Gradient((0, "#191725"), (0.18, "#151420"), (0.55, "#11121B"), (1, "#0E1018"))
            : Gradient((0, "#1B2732"), (0.18, "#17212A"), (0.55, "#121A21"), (1, "#0F151B"));
        drawing.DrawRectangle(backdrop, null, bounds);
        const double horizon = 54;
        var gridColor = BrushFor(_paper ? "#456387" : "#8CB3BD");
        var gridPen = new Pen(WithOpacity(gridColor, _paper ? 0.022 : 0.015), 1);
        var vanishingX = width * 0.52;
        for (var x = -width; x <= width * 2; x += 36)
            drawing.DrawLine(gridPen, new(x, horizon), new(vanishingX + (x - vanishingX) * 0.42, height));
        for (var row = 0; row < 13; row++)
        {
            var y = horizon + (height - horizon) * Math.Pow(row / 12.0, 1.7);
            drawing.DrawLine(new Pen(WithOpacity(gridColor, (_paper ? 0.010 : 0.007) + row * 0.001), 1), new(0, y), new(width, y));
        }
        drawing.DrawLine(new Pen(BrushFor(_paper ? "#381F828C" : "#385CDBC0"), 1), new(0, horizon), new(width, horizon));
        DrawTrunk(drawing);
        foreach (var item in _positions.Values)
        {
            if (!item.Node.IsMain && item.Node.ParentId is { } parentId && _positions.TryGetValue(parentId, out var parent))
                DrawBranch(drawing, parent.Dot, item.Dot, item.Node);
        }
        foreach (var item in _positions.Values)
        {
            var accent = Accent(item.Node);
            var dot = item.Dot;
            if (item.Node.Id == _selected)
            {
                var color = ((SolidColorBrush)accent).Color;
                var glow = new RadialGradientBrush(Color.FromArgb(112, color.R, color.G, color.B), Colors.Transparent);
                drawing.DrawEllipse(glow, null, dot, 25, 25);
            }
            drawing.DrawEllipse(BrushFor("#57000000"), null, new(dot.X + 4, dot.Y + 6), 12, 12);
            drawing.DrawEllipse(BrushFor(_paper ? "#F4F7FA" : _focus ? "#191824" : "#10171A"), new Pen(accent, 3), dot, 9, 9);
            drawing.DrawEllipse(accent, null, dot, 4, 4);
        }
    }

    private void DrawTrunk(DrawingContext drawing)
    {
        var main = _positions.Values.Where(item => item.Node.IsMain).ToArray();
        if (main.Length == 0)
            return;
        var first = main[0].Dot;
        var last = main[^1].Dot;
        var offset = new Vector(3, 5);
        var core = BrushFor(_paper ? "#178774" : _focus ? "#B39AFF" : "#5BD9BD");
        drawing.DrawLine(new Pen(BrushFor(_paper ? "#1A000000" : "#33000000"), 13), first + offset, last + offset);
        drawing.DrawLine(new Pen(BrushFor(_paper ? "#D6E0E8" : _focus ? "#171523" : "#0B1115"), 11), first, last);
        drawing.DrawLine(new Pen(BrushFor(_paper ? "#73AD9F" : _focus ? "#3F3B5B" : "#284A45"), 7), first, last);
        drawing.DrawLine(new Pen(core, 2.4), first, last);
        drawing.DrawEllipse(WithOpacity(core, 0.75), null, new(first.X, first.Y + (last.Y - first.Y) * 0.21), 3, 3);
    }

    private void DrawBranch(DrawingContext drawing, Point parent, Point end, DevelopmentVersion node)
    {
        var side = end.X > parent.X ? 1 : -1;
        var geometry = new StreamGeometry();
        using (var path = geometry.Open())
        {
            path.BeginFigure(parent, false, false);
            path.BezierTo(new(parent.X + side * 46, parent.Y), new(end.X - side * 36, end.Y), end, true, true);
        }
        var accent = Accent(node);
        drawing.PushTransform(new TranslateTransform(3, 4));
        drawing.DrawGeometry(null, new Pen(BrushFor(_paper ? "#14000000" : "#2E000000"), 7), geometry);
        drawing.Pop();
        drawing.DrawGeometry(null, new Pen(BrushFor("#0AFFFFFF"), 10), geometry);
        drawing.DrawGeometry(null, new Pen(WithOpacity(accent, 0.72), 2.6), geometry);
    }

    private void Rebuild()
    {
        Children.Clear();
        _positions.Clear();
        if (_graphViewport.Width <= 0 || _nodes.Length == 0)
            return;
        var width = Math.Max(480, _graphViewport.Width);
        var trunk = Math.Floor(width / 2);
        var compact = width < 520 || _graphViewport.Height < 360;
        var cardWidth = Math.Clamp(width < 620 ? (width - 44) / 2 : width * 0.31, width < 620 ? 150 : 190, 300);
        var mainNodes = _nodes.Where(node => node.IsMain).ToArray();
        var attempts = _nodes.Where(node => !node.IsMain).ToLookup(node => node.ParentId);
        var y = compact ? 56d : 101d;
        foreach (var node in mainNodes)
        {
            var group = attempts[node.Id].ToArray();
            Place(node, trunk, y, Math.Max(12, trunk - cardWidth - 58), cardWidth);
            for (var slot = 0; slot < group.Length; slot++)
            {
                var child = group[slot];
                var offset = _attemptOffsets.GetValueOrDefault(child.Id);
                var x = Math.Clamp(Math.Min(width - cardWidth - 12, trunk + 102) + offset.X, 0, width - cardWidth);
                var branchY = Math.Max(94, y + (compact ? 52 : 62) + slot * 100 + offset.Y);
                Place(child, Math.Clamp(trunk + 78 + offset.X, 10, width - 10), branchY, x, cardWidth);
            }
            y += (compact ? 104 : 140) + Math.Max(0, group.Length - 1) * 100;
        }
        Width = width;
        var defaultHeight = 160 + mainNodes.Length * 140 + attempts.Sum(group => Math.Max(0, group.Count() - 1) * 100);
        var neededHeight = _positions.Values.Select(item => item.Card.Bottom + 24).DefaultIfEmpty(0).Max();
        Height = Math.Max(Math.Max(420, defaultHeight), Math.Max(_graphViewport.Height, neededHeight));
        InvalidateVisual();
    }

    private void Place(DevelopmentVersion node, double dotX, double dotY, double cardX, double width)
    {
        var cardRect = new Rect(cardX, dotY - CardHeight / 2, width, CardHeight);
        _positions[node.Id] = new(node, new(dotX, dotY), cardRect);
        var accent = Accent(node);
        var selected = node.Id == _selected;
        var card = CardFor(node, accent, selected);
        card.Width = width;
        card.Height = CardHeight;
        SetLeft(card, cardRect.X);
        SetTop(card, cardRect.Y);
        SetZIndex(card, selected ? 1 : 0);
        Children.Add(card);
    }

    private Button CardFor(DevelopmentVersion node, Brush accent, bool selected)
    {
        var body = new DockPanel();
        var text = new StackPanel { Margin = new(7, 6, 9, 5) };
        var heading = new DockPanel { Height = 22 };
        var badge = new Border
        {
            Padding = new(7, 0, 7, 1), MinHeight = 20, CornerRadius = new(9), BorderThickness = new(1),
            BorderBrush = WithOpacity(accent, 0.7), Background = WithOpacity(accent, 0.14),
            Child = new TextBlock { Text = node.Badge, Foreground = accent, FontSize = 12 },
        };
        DockPanel.SetDock(badge, Dock.Right);
        heading.Children.Add(badge);
        heading.Children.Add(new TextBlock
        {
            Text = node.Title, FontSize = 15, FontWeight = FontWeights.Bold, Margin = new(0, 0, 6, 0),
            TextTrimming = TextTrimming.CharacterEllipsis, VerticalAlignment = VerticalAlignment.Center,
        });
        text.Children.Add(heading);
        text.Children.Add(new TextBlock { Text = node.Status, FontSize = 13, Foreground = accent, Margin = new(0, 3, 0, 0) });
        var record = new TextBlock
        {
            Text = node.Record, FontSize = 12, TextTrimming = TextTrimming.CharacterEllipsis, Margin = new(0, 5, 0, 0),
        };
        record.SetResourceReference(TextBlock.ForegroundProperty, "WorkbenchMuted");
        text.Children.Add(record);
        var strip = new Border { Width = 4, CornerRadius = new(2), Background = accent };
        DockPanel.SetDock(strip, Dock.Left);
        body.Children.Add(strip);
        body.Children.Add(text);
        var card = new Button
        {
            Content = body, Padding = new(0), BorderThickness = new(selected ? 1.5 : 1),
            BorderBrush = selected ? accent : BrushFor(_paper ? "#CBD5DF" : "#53606A"),
            Background = CardBackground(selected),
        };
        card.SetResourceReference(StyleProperty, "DevelopmentVersionCard");
        AutomationProperties.SetName(card, node.Title);
        card.Click += (_, _) => VersionSelected?.Invoke(node);
        if (!node.IsMain)
            card.PreviewMouseLeftButtonDown += (_, e) =>
                _drag = (node.Id, e.GetPosition(this), _attemptOffsets.GetValueOrDefault(node.Id));
        return card;
    }

    private Brush CardBackground(bool selected)
    {
        if (_paper)
            return selected ? Gradient((0, "#E3F2EE"), (0.48, "#EFF7F4"), (1, "#E8F2EF"))
                : Gradient((0, "#FFFFFF"), (0.48, "#F8FAFC"), (1, "#F0F4F8"));
        if (_focus)
            return Gradient((0, selected ? "#302B46" : "#252334"), (0.48, "#211F30"), (1, "#191824"));
        return selected ? Gradient((0, "#243D42"), (0.48, "#20343A"), (1, "#18262D"))
            : Gradient((0, "#222E39"), (0.48, "#1E2933"), (1, "#151E26"));
    }

    protected override void OnPreviewMouseMove(MouseEventArgs e)
    {
        base.OnPreviewMouseMove(e);
        if (_drag is not { } drag || e.LeftButton != MouseButtonState.Pressed)
            return;
        var delta = e.GetPosition(this) - drag.Start;
        if (delta.Length < 6 && !IsMouseCaptured)
            return;
        if (!IsMouseCaptured)
            CaptureMouse();
        _attemptOffsets[drag.Id] = new(drag.Offset.X + delta.X, Math.Clamp(drag.Offset.Y + delta.Y, -5000, 5000));
        Rebuild();
        e.Handled = true;
    }

    protected override void OnPreviewMouseLeftButtonUp(MouseButtonEventArgs e)
    {
        base.OnPreviewMouseLeftButtonUp(e);
        _drag = null;
        if (IsMouseCaptured)
        {
            ReleaseMouseCapture();
            e.Handled = true;
        }
    }

    private static SolidColorBrush BrushFor(string hex) => new((Color)ColorConverter.ConvertFromString(hex));
    private static LinearGradientBrush Gradient(params (double Offset, string Color)[] stops) =>
        new(new GradientStopCollection(stops.Select(stop => new GradientStop(BrushFor(stop.Color).Color, stop.Offset))),
            new Point(0, 0), new Point(0, 1));
    private static Brush WithOpacity(Brush brush, double opacity)
    {
        var tone = brush.Clone();
        tone.Opacity = opacity;
        return tone;
    }
}
