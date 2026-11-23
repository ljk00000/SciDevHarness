using System.Drawing;
using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;

namespace SciDev.Infrastructure;

internal sealed partial class StaticSvg
{
    internal static readonly XNamespace Namespace = "http://www.w3.org/2000/svg";
    private static readonly XNamespace XLink = "http://www.w3.org/1999/xlink";
    private static readonly HashSet<string> Elements = new(StringComparer.Ordinal)
    {
        "svg", "g", "defs", "title", "desc", "style", "path", "circle", "ellipse", "rect", "line", "polyline",
        "polygon", "linearGradient", "radialGradient", "stop", "use", "text", "tspan"
    };
    private static readonly HashSet<string> Paints = new(StringComparer.Ordinal)
    {
        "fill", "stroke", "color", "stop-color"
    };
    private static readonly HashSet<string> Presentation = new(Paints, StringComparer.Ordinal)
    {
        "fill-opacity", "fill-rule", "stroke-width", "stroke-opacity", "stroke-linecap", "stroke-linejoin",
        "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "opacity", "display", "visibility",
        "stop-opacity", "font-family", "font-size", "font-weight", "text-anchor"
    };
    private static readonly HashSet<string> Attributes = new(Presentation, StringComparer.Ordinal)
    {
        "id", "class", "aria-label", "aria-labelledby", "role", "width", "height", "viewBox",
        "preserveAspectRatio", "d", "points", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry",
        "transform", "gradientTransform", "gradientUnits", "spreadMethod", "offset", "fx", "fy", "version",
        "dx", "dy", "rotate"
    };
    internal const int MaxBytes = 512 * 1024;
    private const int MaxElements = 2048;
    private const int MaxPathValues = 16_384;
    private const double MaxCoordinate = 100_000;

    private StaticSvg(XElement root, RectangleF viewBox, Dictionary<string, XElement> ids)
    {
        Root = root;
        ViewBox = viewBox;
        Ids = ids;
    }

    internal XElement Root { get; }
    internal RectangleF ViewBox { get; }
    internal IReadOnlyDictionary<string, XElement> Ids { get; }

    internal static StaticSvg Read(byte[] source, CancellationToken token)
    {
        if (source.Length is 0 or > MaxBytes)
            throw new InvalidDataException("SVG is empty or exceeds the 512 KiB artifact limit.");

        var text = new UTF8Encoding(false, true).GetString(source);
        if (text.StartsWith('\uFEFF'))
            text = text[1..];
        using var input = new StringReader(text);
        using var reader = XmlReader.Create(input, new XmlReaderSettings
        {
            DtdProcessing = DtdProcessing.Prohibit,
            XmlResolver = null,
            MaxCharactersInDocument = MaxBytes,
            CheckCharacters = true
        });
        // Check nesting before constructing a tree or passing it to the renderer.
        var count = 0;
        using (var probeInput = new StringReader(text))
        using (var probe = XmlReader.Create(probeInput, reader.Settings!))
        {
            while (probe.Read())
            {
                token.ThrowIfCancellationRequested();
                if (probe.NodeType == XmlNodeType.ProcessingInstruction)
                    throw new InvalidDataException("SVG processing instructions are not allowed.");
                if (probe.Depth > 24 || (probe.NodeType == XmlNodeType.Element && ++count > MaxElements))
                    throw new InvalidDataException("SVG exceeds the supported element count or nesting depth.");
            }
        }
        var root = XDocument.Load(reader).Root!;
        if (root.Name != Namespace + "svg")
            throw new InvalidDataException("Expected an SVG document with the SVG namespace.");
        var box = NumberList((string?)root.Attribute("viewBox"));
        if (box.Length != 4)
            throw new InvalidDataException("SVG must define a four-number viewBox.");
        if (box[2] is < 300 or > 8192 || box[3] is < 180 or > 8192)
            throw new InvalidDataException("SVG viewBox must be at least 300x180 and at most 8192x8192.");

        var ids = new Dictionary<string, XElement>(StringComparer.OrdinalIgnoreCase);
        var references = new List<(string Id, bool IsUse)>();
        var uses = 0;
        var pathValues = 0;
        foreach (var element in root.DescendantsAndSelf())
        {
            token.ThrowIfCancellationRequested();
            var name = element.Name.LocalName;
            if (element.Name.Namespace != Namespace || !Elements.Contains(name) || (name == "svg" && element != root))
                throw new InvalidDataException("SVG contains an unsupported or active element. Use static SVG shapes.");
            if (name == "style")
                ValidateStylesheet(element, references);
            foreach (var attribute in element.Attributes().Where(attribute => !attribute.IsNamespaceDeclaration))
            {
                var key = attribute.Name.LocalName;
                var value = attribute.Value.Trim();
                if (key == "href" && name == "use" &&
                    (attribute.Name.Namespace == XNamespace.None || attribute.Name.Namespace == XLink))
                {
                    if (!Fragment().IsMatch(value) || ++uses > 32)
                        throw new InvalidDataException("SVG use must reference a local definition; at most 32 are supported.");
                    references.Add((value[1..], true));
                    continue;
                }
                if (attribute.Name == XNamespace.Xml + "space" && value is "preserve" or "default")
                    continue;
                if (attribute.Name == "type" && name == "style" && value == "text/css")
                    continue;
                if (attribute.Name.Namespace != XNamespace.None || (!Attributes.Contains(key) && key != "style"))
                    throw new InvalidDataException("SVG contains an unsupported attribute or external resource.");
                if (key == "style")
                    ValidateDeclarations(value, references);
                else if (key == "id")
                {
                    if (!Identifier().IsMatch(value) || value != attribute.Value)
                        throw new InvalidDataException("SVG IDs must be simple identifiers.");
                    if (!ids.TryAdd(value, element))
                        throw new InvalidDataException("SVG has duplicate IDs.");
                }
                else
                    ValidateValue(key, value, references);
                if (key is "d" or "points")
                {
                    pathValues += Number().Matches(value).Count;
                    if (pathValues > MaxPathValues)
                        throw new InvalidDataException("SVG has too many path coordinates.");
                }
            }
        }

        foreach (var (id, isUse) in references)
        {
            if (!ids.TryGetValue(id, out var target) || (string?)target.Attribute("id") != id)
                throw new InvalidDataException("SVG has an unresolved local resource reference.");
            if (!isUse && target.Name.LocalName is not ("linearGradient" or "radialGradient"))
                throw new InvalidDataException("SVG paint references must point to a local gradient.");
            if (isUse && (!target.Ancestors(Namespace + "defs").Any() || target.DescendantsAndSelf().Take(65).Count() > 64 ||
                target.DescendantsAndSelf().Any(element => element.Name == Namespace + "use")))
                throw new InvalidDataException("SVG use references must point to small, non-recursive local definitions.");
        }
        return new(root, new((float)box[0], (float)box[1], (float)box[2], (float)box[3]), ids);
    }

    private static void ValidateStylesheet(XElement element, List<(string Id, bool IsUse)> references)
    {
        if (element.HasElements || element.Value.Length > 8192)
            throw new InvalidDataException("SVG styles must contain bounded, static CSS.");
        var css = element.Value.Trim();
        var seen = 0;
        foreach (Match rule in CssRule().Matches(css))
        {
            if (rule.Index != seen || !Selector().IsMatch(rule.Groups[1].Value.Trim()))
                throw new InvalidDataException("SVG styles require simple local selectors.");
            ValidateDeclarations(rule.Groups[2].Value, references);
            seen += rule.Length;
        }
        if (seen != css.Length)
            throw new InvalidDataException("SVG contains an unsupported or external stylesheet.");
    }

    private static void ValidateDeclarations(string style, List<(string Id, bool IsUse)> references)
    {
        if (style.Length > 8192)
            throw new InvalidDataException("SVG style is too long.");
        foreach (var declaration in style.Split(';', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries))
        {
            var separator = declaration.IndexOf(':');
            if (separator < 1 || !Presentation.Contains(declaration[..separator].Trim()))
                throw new InvalidDataException("SVG style contains an unsupported property.");
            var value = declaration[(separator + 1)..].Trim();
            const string important = "!important";
            if (value.EndsWith(important, StringComparison.Ordinal))
                value = value[..^important.Length].TrimEnd();
            ValidateValue(declaration[..separator].Trim(), value, references);
        }
    }

    private static void ValidateValue(string key, string value, List<(string Id, bool IsUse)> references)
    {
        if (Paints.Contains(key))
        {
            var paint = Gradient().Match(value);
            if (paint.Success && key is "fill" or "stroke")
                references.Add((paint.Groups[1].Value, false));
            else if (value is not ("none" or "currentColor" or "inherit") && !IsColor(value))
                throw new InvalidDataException($"SVG has an invalid {key} paint. Use a standard color or a local gradient.");
            return;
        }
        if (value.Length > 32_768 || !PlainValue().IsMatch(value))
            throw new InvalidDataException("SVG contains an unsupported value or external resource.");
        if (key is "id" or "class" or "aria-label" or "aria-labelledby" or "role" or "font-family")
            return;
        foreach (Match number in Number().Matches(value))
            if (!TryNumber(number.Value, out _))
                throw new InvalidDataException("SVG contains a coordinate outside the supported range.");
    }

    internal static bool IsColor(string value) =>
        HexColor().IsMatch(value) || ColorFunction().IsMatch(value) || Color.FromName(value).IsKnownColor;

    internal static double[] NumberList(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
            return [];
        var result = new List<double>();
        var index = 0;
        foreach (Match match in Number().Matches(value))
        {
            if (value.AsSpan(index, match.Index - index).Trim(" ,\r\n\t").Length != 0 ||
                !TryNumber(match.Value, out var number))
                return [];
            result.Add(number);
            index = match.Index + match.Length;
        }
        return value.AsSpan(index).Trim(" ,\r\n\t").Length == 0 ? result.ToArray() : [];
    }

    private static bool TryNumber(string value, out double number) =>
        double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out number) &&
        double.IsFinite(number) && Math.Abs(number) <= MaxCoordinate;

    [GeneratedRegex(@"[-+]?(?:[0-9]*\.[0-9]+|[0-9]+\.?)(?:[eE][-+]?[0-9]+)?", RegexOptions.CultureInvariant | RegexOptions.NonBacktracking)]
    private static partial Regex Number();

    [GeneratedRegex(@"\A[A-Za-z_][A-Za-z0-9_.-]{0,95}\z", RegexOptions.CultureInvariant)]
    private static partial Regex Identifier();

    [GeneratedRegex(@"\A#[A-Za-z_][A-Za-z0-9_.-]{0,95}\z", RegexOptions.CultureInvariant)]
    private static partial Regex Fragment();

    [GeneratedRegex(@"\Aurl\(\s*#([A-Za-z_][A-Za-z0-9_.-]{0,95})\s*\)\z", RegexOptions.CultureInvariant)]
    private static partial Regex Gradient();

    [GeneratedRegex(@"([^{}]+)\{([^{}]*)\}", RegexOptions.CultureInvariant | RegexOptions.NonBacktracking)]
    private static partial Regex CssRule();

    [GeneratedRegex(@"\A[#.A-Za-z_][A-Za-z0-9_ .,#>+~-]*\z", RegexOptions.CultureInvariant | RegexOptions.NonBacktracking)]
    private static partial Regex Selector();

    [GeneratedRegex(@"\A[\p{L}\p{N}_\-\s.,%+()#'""–]*\z", RegexOptions.CultureInvariant | RegexOptions.NonBacktracking)]
    private static partial Regex PlainValue();

    [GeneratedRegex(@"\A#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})\z", RegexOptions.CultureInvariant)]
    private static partial Regex HexColor();

    [GeneratedRegex(@"\Argb\(\s*[0-9]{1,3}%?\s*,\s*[0-9]{1,3}%?\s*,\s*[0-9]{1,3}%?\s*\)\z", RegexOptions.CultureInvariant)]
    private static partial Regex ColorFunction();
}
