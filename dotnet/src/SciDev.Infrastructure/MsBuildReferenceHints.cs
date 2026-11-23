using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;
using static SciDev.Infrastructure.WorkspaceProjectContext;

namespace SciDev.Infrastructure;

/// <summary>Find possible local MSBuild edges without evaluating project code or deciding coverage.</summary>
internal sealed class MsBuildReferenceHints(string[] files, Func<string, string?> read, CancellationToken token)
{
    private const string Root = "|/";
    private const int MaxDocuments = 240;
    private const int MaxImports = 32;
    private const int MaxPath = 500;
    private const int MaxVariants = 8;
    private static readonly Regex Property = new(@"\$\(([A-Za-z_][A-Za-z0-9_.-]*)\)", RegexOptions.NonBacktracking);
    private readonly HashSet<string> _files = new(files, StringComparer.Ordinal);
    private readonly Dictionary<string, XElement?> _documents = new(StringComparer.Ordinal);

    internal XElement? Read(string path)
    {
        if (_documents.TryGetValue(path, out var root))
            return root;
        if (!_files.Contains(path) || _documents.Count >= MaxDocuments)
            return null;
        token.ThrowIfCancellationRequested();
        try { root = read(path) is { } text ? ProjectManifest.ReadDotnetRoot(text) : null; }
        catch (XmlException) { root = null; }
        _documents.Add(path, root);
        return root;
    }

    internal string[] FindReferences(string project)
    {
        var visited = new HashSet<string>(StringComparer.Ordinal);
        var properties = new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase);
        var references = new List<(string File, string Expression, bool Disabled)>();
        var mayUpdate = false;

        void Load(string file, bool conditional, int depth)
        {
            if (depth >= 8 || visited.Count >= MaxImports || !visited.Add(file) || Read(file) is not { } root)
                return;
            foreach (var node in root.Descendants())
            {
                token.ThrowIfCancellationRequested();
                var name = node.Name.LocalName;
                var parent = node.Parent?.Name.LocalName;
                if ((parent != "PropertyGroup" && name is not ("Import" or "ProjectReference")) ||
                    node.Ancestors().Any(ancestor => ancestor.Name.LocalName == "Target"))
                    continue;
                var guarded = conditional || node.AncestorsAndSelf().Any(ancestor => ancestor.Attribute("Condition") is not null ||
                    ancestor.Name.LocalName is "When" or "Otherwise");
                if (parent == "PropertyGroup" && !node.HasElements && properties.Count < 200)
                {
                    var values = Expand(node.Value.Trim(), project, file, properties);
                    properties[name] = guarded && properties.TryGetValue(name, out var previous)
                        ? previous.Concat(values).Distinct(StringComparer.Ordinal).Take(MaxVariants).ToArray() : values;
                }
                else if (name == "Import" && node.Attribute("Sdk") is null && node.Attribute("Project") is { } import)
                {
                    foreach (var path in Paths(file, Expand(import.Value, project, file, properties)))
                        Load(path, guarded, depth + 1);
                }
                else if (name == "ProjectReference" && parent == "ItemGroup")
                {
                    mayUpdate |= node.Attribute("Update") is not null;
                    if (node.Attribute("Include") is { } include && references.Count < 160)
                        references.Add((file, include.Value, Disabled(node, "ReferenceOutputAssembly") || Disabled(node, "BuildReference")));
                }
            }
        }

        void LoadNearest(string name)
        {
            var path = Ancestors(project, directory: false).Reverse().Select(dir => InDirectory(dir, name)).FirstOrDefault(_files.Contains);
            if (path is not null)
                Load(path, false, 0);
        }

        LoadNearest("Directory.Build.props");
        Load(project, false, 0);
        LoadNearest("Directory.Build.targets");
        // Reference items use the project folder and the properties collected through imports.
        return references.Where(reference => mayUpdate || !reference.Disabled)
            .SelectMany(reference => Paths(project, Expand(reference.Expression, project, reference.File, properties)))
            .Where(DotnetTestProjects.IsProject).Distinct(StringComparer.Ordinal).Take(160).ToArray();
    }

    private static bool Disabled(XElement reference, string name)
    {
        var flag = (string?)reference.Attribute(name) ?? reference.Elements().FirstOrDefault(node =>
            node.Name.LocalName == name && node.Attribute("Condition") is null)?.Value;
        return flag?.Trim().Equals("false", StringComparison.OrdinalIgnoreCase) == true;
    }

    private static string[] Expand(string expression, string project, string file, Dictionary<string, string[]> properties)
    {
        if (expression.Length > MaxPath)
            return [];
        string[] values = [expression];
        for (var step = 0; step < 8; step++)
        {
            if (values.All(value => !Property.IsMatch(value)))
                return values.Where(value => value.Length <= MaxPath).ToArray();
            values = values.SelectMany(value =>
            {
                var match = Property.Match(value);
                return match.Success ? PropertyValues(match.Groups[1].Value, project, file, properties)
                    .Select(part => value[..match.Index] + part + value[(match.Index + match.Length)..]) : [value];
            }).Where(value => value.Length <= MaxPath).Distinct(StringComparer.Ordinal).Take(MaxVariants).ToArray();
        }
        return [];
    }

    private static string[] PropertyValues(string name, string project, string file, Dictionary<string, string[]> properties) =>
        name.ToLowerInvariant() switch
        {
            "msbuildprojectdirectory" => [Root + DirectoryPart(project)],
            "msbuildprojectfullpath" => [Root + project],
            "msbuildprojectfile" => [Path.GetFileName(project)],
            "msbuildprojectname" => [Path.GetFileNameWithoutExtension(project)],
            "msbuildthisfiledirectory" => [Root + InDirectory(DirectoryPart(file), "")],
            "msbuildthisfilefullpath" => [Root + file],
            "msbuildthisfile" => [Path.GetFileName(file)],
            _ => properties.GetValueOrDefault(name) ?? [],
        };

    private IEnumerable<string> Paths(string owner, IEnumerable<string> expressions) => expressions
        .SelectMany(expression => expression.Split(';', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries))
        .Select(expression => Resolve(owner, expression)).OfType<string>().SelectMany(Match).Take(32);

    private IEnumerable<string> Match(string path)
    {
        if (!path.Contains('*') && !path.Contains('?'))
            return _files.Contains(path) ? [path] : [];
        var parts = path.Split('/');
        var pattern = string.Concat(parts.Select((part, index) =>
        {
            if (part == "**" && index < parts.Length - 1)
                return "(?:[^/]+/)*";
            var segment = Regex.Escape(part).Replace(@"\*", "[^/]*", StringComparison.Ordinal)
                .Replace(@"\?", "[^/]", StringComparison.Ordinal);
            return index < parts.Length - 1 ? segment + "/" : segment;
        }));
        try
        {
            var regex = new Regex(@"\A" + pattern + @"\z", RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);
            return _files.Where(file => regex.IsMatch(file)).Order(StringComparer.Ordinal).Take(24);
        }
        catch (NotSupportedException) { return []; }
    }

    private static string? Resolve(string owner, string expression)
    {
        var rooted = expression.StartsWith(Root, StringComparison.Ordinal);
        var relative = rooted ? expression[Root.Length..].TrimStart('/', '\\') : expression;
        if (relative.Length is 0 or > MaxPath || Path.IsPathRooted(relative) || relative.Contains(':'))
            return null;
        List<string> parts = rooted ? [] : DirectoryPart(owner).Split('/', StringSplitOptions.RemoveEmptyEntries).ToList();
        foreach (var part in relative.Replace('\\', '/').Split('/', StringSplitOptions.RemoveEmptyEntries))
        {
            if (part == ".")
                continue;
            if (part == "..")
            {
                if (parts.Count == 0)
                    return null;
                parts.RemoveAt(parts.Count - 1);
            }
            else if (part.Any(ch => char.IsControl(ch) || ch is '<' or '>' or '"' or '|' or '$' or '%' or '@'))
                return null;
            else
                parts.Add(part);
        }
        return parts.Count > 0 ? string.Join('/', parts) : null;
    }

    private static string InDirectory(string directory, string name) => directory.Length == 0 ? name : directory + "/" + name;
}
