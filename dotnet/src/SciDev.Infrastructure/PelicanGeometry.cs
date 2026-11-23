using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.Versioning;
using Svg;
using Svg.Pathing;

namespace SciDev.Infrastructure;

[SupportedOSPlatform("windows")]
internal sealed class PelicanGeometry(StaticSvg source, SvgDocument document)
{
    private const string Body = "pelican-body";
    private const string Head = "pelican-head";
    private const string Eye = "pelican-eye";
    private const string Beak = "pelican-beak";
    private const string Wing = "pelican-wing";
    private const string Pouch = "pelican-pouch";
    private const string Reaching = "pelican-wing-reaching";
    private const string NearLeg = "pelican-leg-near";
    private const string FarLeg = "pelican-leg-far";
    private const string Left = "left-wheel";
    private const string Right = "right-wheel";
    private const string Frame = "bicycle-frame";
    private const string Fork = "bicycle-fork";
    private const string Spokes = "bicycle-spokes";
    private const string Saddle = "bicycle-saddle";
    private const string Handlebar = "bicycle-handlebar";
    private const string Pedals = "bicycle-pedals";

    private static readonly (string Id, Type[] Shapes)[] Required =
    [
        (Left, [typeof(SvgCircle)]), (Right, [typeof(SvgCircle)]),
        (Frame, [typeof(SvgPolyline)]), (Fork, [typeof(SvgPath), typeof(SvgPolyline), typeof(SvgLine)]),
        (Spokes, [typeof(SvgGroup)]), (Saddle, []), (Handlebar, []), (Pedals, [typeof(SvgGroup)]),
        (Body, [typeof(SvgPath), typeof(SvgEllipse)]), (Head, [typeof(SvgCircle), typeof(SvgEllipse)]),
        (Eye, [typeof(SvgCircle)]), (Wing, [typeof(SvgPath), typeof(SvgEllipse), typeof(SvgPolygon)]),
        (Reaching, [typeof(SvgPath)]), (Beak, [typeof(SvgPolygon)]), (Pouch, [typeof(SvgPath)]),
        (NearLeg, [typeof(SvgPath)]), (FarLeg, [typeof(SvgPath)])
    ];

    private readonly Dictionary<string, Part> parts = new(StringComparer.Ordinal);
    private readonly List<string> issues = [];

    internal string[] Check(CancellationToken token)
    {
        var view = RectangleF.Inflate(source.ViewBox, 0.5f, 0.5f);
        foreach (var (id, shapes) in Required)
        {
            token.ThrowIfCancellationRequested();
            if (!source.Ids.TryGetValue(id, out var element))
            {
                issues.Add($"The SVG is missing the part ID {id}. Assign it to the matching visible shape.");
                continue;
            }
            var original = document.GetElementById<SvgVisualElement>((string)element.Attribute("id")!);
            var shape = original is null ? null : Resolve(original, shapes);
            if (original is null || shape is null)
            {
                issues.Add($"{id} has the wrong shape type. Use {string.Join(" or ", shapes.Select(type => type.Name[3..].ToLowerInvariant()))}.");
                continue;
            }
            var bounds = Bounds(original);
            var visible = Usable(bounds) && HasVisiblePaint(shape);
            if (!visible)
                issues.Add($"{id} must be a visible non-empty shape.");
            else if (!view.Contains(bounds))
                issues.Add($"{id} is clipped by the viewBox.");
            if (visible)
                parts.Add(id, new(shape, bounds));
        }

        CheckBicycle();
        CheckBird();
        CheckRidingPose();
        return issues.ToArray();
    }

    private void CheckBicycle()
    {
        foreach (var name in new[] { Left, Right })
        {
            if (!parts.TryGetValue(name, out var tire))
                continue;
            var dimensions = tire.Bounds.Size;
            if (Math.Abs(dimensions.Width - dimensions.Height) > Math.Max(2, Math.Max(dimensions.Width, dimensions.Height) * 0.08f))
                issues.Add($"{name} must render as a circle.");
            if (!UnfilledTire(tire.Shape) || !HasStroke(tire.Shape))
                issues.Add($"{name} must be an unfilled, visibly outlined tire.");
        }
        if (parts.TryGetValue(Spokes, out var spokes))
        {
            var visibleSegments = Descendants(spokes.Shape).Where(HasStroke).Sum(shape => shape switch
            {
                SvgLine => 1,
                SvgPath path => path.PathData.Count(segment => segment is SvgMoveToSegment),
                SvgPolyline polyline => Math.Max(0, polyline.Points.Count / 2 - 1),
                _ => 0
            });
            if (visibleSegments < 8)
                issues.Add("bicycle-spokes must contain at least eight visible radial segments for both wheels.");
        }
        if (!parts.TryGetValue(Left, out var left) || !parts.TryGetValue(Right, out var right))
            return;

        var radius = Math.Min(left.Radius, right.Radius);
        var wheelSize = left.Radius + right.Radius;
        if (Distance(left.Center, right.Center) < wheelSize * 0.9f)
            issues.Add("The left-wheel and right-wheel overlap.");
        if (Math.Abs(left.Center.Y - right.Center.Y) > wheelSize * 0.35f)
            issues.Add("The left-wheel and right-wheel are not aligned at a readable height.");

        if (!parts.TryGetValue(Frame, out var frame) || frame.Shape is not SvgPolyline poly)
            return;
        var points = Points(poly, poly.Points);
        var hubTolerance = Math.Max(1, radius * 0.25f);
        if (!HasStroke(poly) || points.Length < 3)
            issues.Add("bicycle-frame needs a visibly stroked polyline with three or more points.");
        if (!points.Any(point => Distance(point, left.Center) <= hubTolerance))
            issues.Add("bicycle-frame must meet the rear wheel hub.");
        if (!points.Any(point => Math.Min(left.Center.X, right.Center.X) < point.X &&
            point.X < Math.Max(left.Center.X, right.Center.X) && Math.Abs(point.Y - (left.Center.Y + right.Center.Y) / 2) > radius / 2))
            issues.Add("bicycle-frame needs a triangular center vertex.");
        if (parts.TryGetValue(Pedals, out var pedals) && !points.Any(point => Distance(point, pedals.Center) <= hubTolerance))
            issues.Add("bicycle-frame must meet the visible crank/pedals.");
        if (parts.TryGetValue(Saddle, out var saddle) && !points.Any(point => Distance(point, saddle.Center) <= Math.Max(3, radius * 0.35f)))
            issues.Add("bicycle-frame must meet the seat joint directly beneath the saddle.");
        if (parts.TryGetValue(Fork, out var fork) &&
            (!RectangleF.Inflate(fork.Bounds, hubTolerance, hubTolerance).Contains(right.Center) ||
                !fork.Bounds.IntersectsWith(RectangleF.Inflate(frame.Bounds, hubTolerance, hubTolerance))))
            issues.Add("bicycle-fork must join the frame near the head tube and reach the front wheel hub.");
    }

    private void CheckBird()
    {
        var headSize = parts.GetValueOrDefault(Head)?.Radius ?? 0;
        if (parts.TryGetValue(Head, out var head) &&
            (headSize < 8 || !HasFilledSilhouette(head.Shape)))
            issues.Add("pelican-head needs a filled silhouette that contrasts with the canvas and has a radius of at least eight.");
        if (parts.TryGetValue(Eye, out var eye) &&
            (eye.Radius < 2 || !HasFill(eye.Shape) ||
                (eye.Shape.Fill is SvgColourServer color && color.Colour.ToArgb() == Color.White.ToArgb())))
            issues.Add("pelican-eye must be a visible small circle with a dark fill.");
        if (parts.TryGetValue(Beak, out var beak) && (beak.Shape is not SvgPolygon polygon ||
                Points(polygon, polygon.Points).Length < 3 || beak.Bounds.Width < Math.Max(60, headSize * 2.5f)))
            issues.Add("pelican-beak is too short relative to the head. Use a long tapered polygon.");
        if (parts.TryGetValue(Pouch, out var pouch) &&
            (!Curved(pouch.Shape, closed: true) || !HasFilledSilhouette(pouch.Shape) ||
                (head is not null && (pouch.Bounds.Width > headSize * 3 || pouch.Bounds.Top <= head.Center.Y))))
            issues.Add("pelican-pouch must be a compact, filled, closed curved throat shape below the head.");

        if (!parts.TryGetValue(Body, out var body))
            return;
        if (body.Shape is SvgPath && !Curved(body.Shape, closed: false))
            issues.Add("pelican-body must use a curved path or ellipse.");
        if (!HasContrast(body.Shape))
            issues.Add("pelican-body has too little contrast against the canvas.");
        if (parts.TryGetValue(Left, out var left) && parts.TryGetValue(Right, out var right))
        {
            var wheelDistance = Distance(left.Center, right.Center);
            if (body.Bounds.Height < wheelDistance * 0.19f)
                issues.Add("pelican-body is too flat to read as a bird torso.");
            if (body.Bounds.Width > wheelDistance * 0.58f)
                issues.Add("pelican-body is too wide relative to the bicycle.");
        }
        if (head is not null &&
            (body.Center.X >= head.Center.X + headSize * 0.25f ||
                Math.Max(body.Bounds.Top, head.Bounds.Top) - Math.Min(body.Bounds.Bottom, head.Bounds.Bottom) > headSize * 0.25f))
            issues.Add("pelican-body must sit behind and connect visually to the pelican-head.");
        if (parts.TryGetValue(Wing, out var wing))
        {
            var overlap = RectangleF.Intersect(body.Bounds, wing.Bounds);
            if (!HasFilledSilhouette(wing.Shape) || overlap.Width * overlap.Height < wing.Bounds.Width * wing.Bounds.Height * 0.5f)
                issues.Add("pelican-wing must be visibly filled and sit mostly inside the body silhouette.");
        }
    }

    private void CheckRidingPose()
    {
        parts.TryGetValue(Body, out var body);
        var radius = Math.Min(parts.GetValueOrDefault(Left)?.Radius ?? 0, parts.GetValueOrDefault(Right)?.Radius ?? 0);
        var tolerance = Math.Max(3, radius * 0.2f);
        var torso = body is null ? RectangleF.Empty : RectangleF.Inflate(body.Bounds, tolerance, tolerance);
        if (parts.TryGetValue(Saddle, out var saddle) && body is not null && !torso.IntersectsWith(saddle.Bounds))
            issues.Add("pelican-body must rest on or closely touch the bicycle-saddle.");
        if (parts.TryGetValue(Handlebar, out var handlebar))
        {
            if (saddle is not null && handlebar.Center.X <= saddle.Center.X)
                issues.Add("bicycle-handlebar must be ahead of the saddle.");
            if (parts.TryGetValue(Reaching, out var wing) &&
                (!Curved(wing.Shape, closed: true) || !HasFilledSilhouette(wing.Shape) ||
                    (body is not null && !wing.Bounds.IntersectsWith(body.Bounds)) ||
                    !TryPathEndpoints((SvgPath)wing.Shape, out _, out _, out var tip) ||
                    tip is null || !RectangleF.Inflate(handlebar.Bounds, 2, 2).Contains(tip.Value)))
                issues.Add("pelican-wing-reaching must be a closed filled curve from the shoulder. Put its first cubic endpoint on the handlebar grip.");
        }

        var pedals = new List<Part>();
        if (parts.TryGetValue(Pedals, out var group))
        {
            foreach (var circle in Descendants(group.Shape).OfType<SvgCircle>().Where(IsVisible))
            {
                var bounds = Bounds(circle);
                var pedal = new Part(circle, bounds);
                if (Usable(bounds) && (HasFill(circle) || HasStroke(circle)) &&
                    pedals.All(other => Distance(pedal.Center, other.Center) >= tolerance))
                    pedals.Add(pedal);
            }
            if (pedals.Count < 2)
                issues.Add("bicycle-pedals must contain two distinct visible pedal circles.");
        }
        var legEnds = new List<PointF>();
        foreach (var id in new[] { NearLeg, FarLeg })
        {
            if (!parts.TryGetValue(id, out var leg))
                continue;
            if (!TryPathEndpoints((SvgPath)leg.Shape, out var start, out var end, out _))
            {
                issues.Add($"{id} must have a clear body-to-pedal path.");
                continue;
            }
            legEnds.Add(end);
            if (body is not null && !torso.Contains(start))
                issues.Add($"{id} must visibly begin at the pelican body.");
            if (pedals.Count >= 2 && !pedals.Any(pedal => Distance(pedal.Center, end) <= tolerance + pedal.Radius))
                issues.Add($"{id} must visibly reach a pedal position.");
        }
        if (legEnds.Count == 2 && pedals.Count >= 2 &&
            (Distance(legEnds[0], legEnds[1]) < Math.Max(3, radius * 0.15f) ||
                !pedals.Any(near => Distance(near.Center, legEnds[0]) <= tolerance + near.Radius &&
                    pedals.Any(far => far != near && Distance(far.Center, legEnds[1]) <= tolerance + far.Radius))))
            issues.Add("pelican-leg-near and pelican-leg-far must reach separate pedal positions.");
    }

    private static SvgVisualElement? Resolve(SvgVisualElement element, Type[] shapes) =>
        shapes.Length == 0 || shapes.Contains(element.GetType()) ? element :
            element is SvgGroup ? Descendants(element).FirstOrDefault(child => shapes.Contains(child.GetType())) : null;

    private static IEnumerable<SvgVisualElement> Descendants(SvgElement element)
    {
        foreach (var child in element.Children)
        {
            if (child is SvgVisualElement visual)
                yield return visual;
            foreach (var nested in Descendants(child))
                yield return nested;
        }
    }

    private static bool Curved(SvgVisualElement shape, bool closed) =>
        shape is SvgPath path && path.PathData.Any(segment => segment is SvgCubicCurveSegment or SvgQuadraticCurveSegment) &&
        (!closed || path.PathData.LastOrDefault() is SvgClosePathSegment);

    private static bool TryPathEndpoints(SvgPath path, out PointF start, out PointF end, out PointF? firstCubic)
    {
        start = end = PointF.Empty;
        firstCubic = null;
        if (path.PathData is not { Count: >= 2 } segments || segments[0] is not SvgMoveToSegment)
            return false;
        var current = segments[0].End;
        var cubic = segments[1] is SvgCubicCurveSegment ? RelativeEnd(segments[1], current) : (PointF?)null;
        var finish = current;
        foreach (var segment in segments.Skip(1))
        {
            if (segment is SvgClosePathSegment or SvgMoveToSegment)
                break;
            finish = RelativeEnd(segment, finish);
        }
        var points = cubic is { } point ? new[] { current, finish, point } : [current, finish];
        using var transform = TransformToRoot(path);
        transform.TransformPoints(points);
        start = points[0];
        end = points[1];
        firstCubic = cubic.HasValue ? points[2] : null;
        return start != end || cubic.HasValue;
    }

    private static PointF RelativeEnd(SvgPathSegment segment, PointF current) =>
        segment.IsRelative ? new(current.X + segment.End.X, current.Y + segment.End.Y) : segment.End;

    private static PointF[] Points(SvgElement shape, SvgPointCollection values)
    {
        if (values.Count < 2 || values.Count % 2 != 0)
            return [];
        var points = new PointF[values.Count / 2];
        for (var i = 0; i < points.Length; i++)
            points[i] = new(values[i * 2].Value, values[i * 2 + 1].Value);
        using var transform = TransformToRoot(shape);
        transform.TransformPoints(points);
        return points;
    }

    private static RectangleF Bounds(SvgVisualElement element)
    {
        // SVG.NET includes the element's own transform in Bounds, but applies parent transforms at draw time.
        var box = element.Bounds;
        var corners = new PointF[] { box.Location, new(box.Right, box.Top), new(box.Left, box.Bottom), new(box.Right, box.Bottom) };
        using var transform = TransformToRoot(element.Parent);
        transform.TransformPoints(corners);
        return RectangleF.FromLTRB(corners.Min(point => point.X), corners.Min(point => point.Y),
            corners.Max(point => point.X), corners.Max(point => point.Y));
    }

    private static Matrix TransformToRoot(SvgElement? element)
    {
        var matrix = new Matrix();
        for (var current = element; current is not null; current = current.Parent)
        {
            if (current.Transforms is not { Count: > 0 } transforms)
                continue;
            using var local = transforms.GetMatrix();
            matrix.Multiply(local, MatrixOrder.Append);
        }
        return matrix;
    }

    private static bool IsVisible(SvgVisualElement shape) =>
        shape.Visible && shape.ParentsAndSelf.All(node => node.Display != "none" && node.Opacity > 0) &&
        !shape.Parents.Any(node => node is SvgDefinitionList);

    private static bool HasVisiblePaint(SvgVisualElement shape) => IsVisible(shape) &&
        (shape is SvgGroup ? Descendants(shape).Any(HasOwnPaint) : HasOwnPaint(shape));

    private static bool HasOwnPaint(SvgVisualElement shape) =>
        IsVisible(shape) && shape is not SvgGroup && (HasStroke(shape) ||
            shape is not (SvgLine or SvgPolyline) && HasFill(shape));

    private static bool HasStroke(SvgVisualElement shape) => IsVisible(shape) && shape.StrokeWidth.Value > 0 &&
        shape.StrokeOpacity > 0 && shape.Stroke != SvgPaintServer.NotSet && IsPainted(shape.Stroke);

    private static bool HasFill(SvgVisualElement shape) =>
        shape.FillOpacity > 0 && (shape.Fill == SvgPaintServer.NotSet || IsPainted(shape.Fill));

    private static bool HasFilledSilhouette(SvgVisualElement shape) =>
        shape.Fill != SvgPaintServer.NotSet && HasFill(shape) && HasContrast(shape);

    private static bool UnfilledTire(SvgVisualElement shape) => shape.Fill == SvgPaintServer.None ||
        (shape.Fill is SvgColourServer color && color.Colour.ToArgb() == Color.White.ToArgb());

    private static bool IsPainted(SvgPaintServer? paint) => paint is not null && paint != SvgPaintServer.None &&
        (paint is not SvgColourServer color || color.Colour.A > 0);

    private static bool HasContrast(SvgVisualElement shape) =>
        (HasFill(shape) && Contrasts(shape.Fill, shape.FillOpacity)) ||
        (HasStroke(shape) && Contrasts(shape.Stroke, shape.StrokeOpacity));

    private static bool Contrasts(SvgPaintServer? paint, float opacity)
    {
        // The original benchmark requires an explicit contrasting solid fill or outline for the bird silhouettes.
        if (paint is not SvgColourServer color || paint == SvgPaintServer.None ||
            paint == SvgPaintServer.NotSet || paint == SvgPaintServer.Inherit)
            return false;
        var background = NativePelicanValidator.Background;
        var alpha = color.Colour.A / 255f * Math.Clamp(opacity, 0, 1);
        var red = MathF.Round((color.Colour.R - background.R) * alpha);
        var green = MathF.Round((color.Colour.G - background.G) * alpha);
        var blue = MathF.Round((color.Colour.B - background.B) * alpha);
        return red * red + green * green + blue * blue >= 24 * 24;
    }

    private static bool Usable(RectangleF box) => box.Width > 0 && box.Height > 0 &&
        float.IsFinite(box.Left) && float.IsFinite(box.Top) && float.IsFinite(box.Right) && float.IsFinite(box.Bottom);

    private static float Distance(PointF left, PointF right) =>
        MathF.Sqrt(MathF.Pow(left.X - right.X, 2) + MathF.Pow(left.Y - right.Y, 2));

    private sealed record Part(SvgVisualElement Shape, RectangleF Bounds)
    {
        public PointF Center => new(Bounds.X + Bounds.Width / 2, Bounds.Y + Bounds.Height / 2);
        public float Radius => Math.Min(Bounds.Width, Bounds.Height) / 2;
    }
}
