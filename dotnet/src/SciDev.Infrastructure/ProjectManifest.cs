using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class ProjectManifest
{
    internal sealed record Summary(string? Declaration, string[] Dependencies, Check[] Checks);
    internal sealed record Check(string Category, string[] Arguments);

    private const RegexOptions Safe = RegexOptions.CultureInvariant | RegexOptions.NonBacktracking;
    private static readonly Regex Requirement = new(
        """\A(?<name>[A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?:\[(?<extra>[A-Za-z0-9,_.-]{1,100})\])?(?:[ \t]*(?<version>[<>=!~]{1,3}[A-Za-z0-9*.+_,<>=!~ -]{1,120}))?\z""", Safe);
    private static readonly Regex Identifier = new("""\A(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\z""", Safe);
    private static readonly Regex Version = new("""\A[0-9v~^<>=!*][A-Za-z0-9.+*^<>=!~|,_ -]{0,99}\z""", Safe);
    private static readonly Regex Table = new("""(?m)^[ \t]*\[([A-Za-z0-9_.-]+)\][ \t]*(?:#[^\r\n]*)?\r?$""", Safe);
    private static readonly Regex TomlArrayStart = new("""(?m)^[ \t]*dependencies[ \t]*=[ \t]*\[""", Safe);
    private static readonly Regex TomlString = new("""(?m)^[ \t]*(?<key>[A-Za-z0-9_-]+)[ \t]*=[ \t]*(?:"(?<double>[^"\r\n]*)"|'(?<single>[^'\r\n]*)')[ \t]*(?:#[^\r\n]*)?\r?$""", Safe);
    private static readonly Regex TomlKey = new("""(?m)^\s*([A-Za-z0-9_.-]+)[ \t]*=""", Safe);
    private static readonly Regex GoVersion = new("""(?m)^go[ \t]+([0-9]+\.[0-9]+(?:\.[0-9]+)?)[ \t]*\r?$""", Safe);
    private static readonly Regex TestTarget = new("""(?m)^(test|lint|typecheck)[ \t]*:""", Safe);
    private static readonly Regex PytestSection = new(@"(?im)^[ \t]*\[(?:tool:pytest|pytest)\][ \t]*\r?$", Safe);
    private static readonly HashSet<string> ManifestNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "pyproject.toml", "package.json", "cargo.toml", "go.mod", "pom.xml",
        "build.gradle", "build.gradle.kts", "pytest.ini", "setup.cfg", "tox.ini", "noxfile.py", "makefile", "justfile",
    };
    private static readonly HashSet<string> LockNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "cargo.lock", "go.sum",
        "poetry.lock", "uv.lock", "pdm.lock", "pipfile.lock", "packages.lock.json", "gradle.lockfile",
    };
    private static readonly Dictionary<string, string> NodeLockManagers = new(StringComparer.OrdinalIgnoreCase)
    {
        ["pnpm-lock.yaml"] = "pnpm", ["yarn.lock"] = "yarn", ["package-lock.json"] = "npm", ["npm-shrinkwrap.json"] = "npm",
    };

    internal static bool IsManifest(string name) => ManifestNames.Contains(name) || IsRequirements(name) ||
        name.EndsWith(".csproj", StringComparison.OrdinalIgnoreCase);

    internal static bool IsLock(string name) => LockNames.Contains(name);
    internal static bool IsNodeLock(string name) => NodeLockManagers.ContainsKey(name);

    internal static bool UsesPythonTestDiscovery(string name) =>
        name.ToLowerInvariant() is "pyproject.toml" or "pytest.ini" or "setup.cfg" or "tox.ini" or "noxfile.py" || IsRequirements(name);
    internal static bool IsGoManifest(string name) => name.Equals("go.mod", StringComparison.OrdinalIgnoreCase);
    internal static bool UsesSourceTestDiscovery(string name) => UsesPythonTestDiscovery(name) ||
        name.ToLowerInvariant() is "go.mod" or "cargo.toml" or "pom.xml" or "build.gradle" or "build.gradle.kts";

    internal static Summary Describe(string name, string text, IReadOnlySet<string> siblings, ProjectTestEvidence tests, string directory)
    {
        var lower = name.ToLowerInvariant();
        return lower switch
        {
            "package.json" => Node(text, siblings),
            "pyproject.toml" => Python(text, tests, directory),
            "pytest.ini" or "setup.cfg" => new("Python", [], tests.PythonChecks(directory, PytestSection.IsMatch(text))),
            "cargo.toml" => Cargo(text, tests.HasTests(name, directory)),
            "go.mod" => new(VersionLabel("Go", GoVersion.Match(text).Groups[1].Value), [],
                tests.HasTests(name, directory) ? [new("test", ["go", "test", "./..."])] : []),
            "pom.xml" => new("Maven", [], tests.HasTests(name, directory) ? [new("test", [siblings.Contains("mvnw") ? "./mvnw" : "mvn", "test"])] : []),
            "build.gradle" or "build.gradle.kts" =>
                new("Gradle", [], tests.HasTests(name, directory) ? [new("test", [siblings.Contains("gradlew") ? "./gradlew" : "gradle", "test"])] : []),
            "tox.ini" => new("tox", [], [new("test", ["tox"])]),
            "noxfile.py" => new("nox", [], [new("test", ["nox"])]),
            "makefile" or "justfile" => new(null, [], TestTarget.Matches(text).Select(match => match.Groups[1].Value)
                .Distinct(StringComparer.Ordinal).Select(target => new Check(target, [lower == "makefile" ? "make" : "just", target])).ToArray()),
            _ when IsRequirements(lower) => Requirements(text, tests, directory),
            _ when lower.EndsWith(".csproj", StringComparison.Ordinal) => DescribeDotnet(ReadDotnetRoot(text), name),
            _ => new(null, [], []),
        };
    }

    private static Summary Node(string text, IReadOnlySet<string> siblings)
    {
        using var document = JsonDocument.Parse(text, new JsonDocumentOptions { MaxDepth = 24 });
        var root = document.RootElement;
        var manager = NodeManager(GetString(root, "packageManager"), siblings);
        var dependencies = new List<string>();
        foreach (var group in new[] { "dependencies", "devDependencies", "peerDependencies", "optionalDependencies" })
            if (TryObject(root, group, out var items))
                dependencies.AddRange(items.EnumerateObject().Where(item => Identifier.IsMatch(item.Name)).Take(32)
                    .Select(item => item.Name + SafeVersion(item.Value)));
        var candidates = new List<Check>();
        if (manager is not null && TryObject(root, "scripts", out var scripts))
            foreach (var script in scripts.EnumerateObject().Take(160)
                .OrderBy(script => script.Name != "test").ThenBy(script => script.Name, StringComparer.Ordinal))
            {
                if (script.Value.ValueKind != JsonValueKind.String || script.Value.GetString() is not { } body ||
                    string.IsNullOrWhiteSpace(body) || (script.Name == "test" && body.Contains("no test specified", StringComparison.OrdinalIgnoreCase)))
                    continue;
                string[] args = [manager, "run", script.Name];
                if (SandboxVerification.Classify(args) is { } category)
                    candidates.Add(new(category, args));
            }
        var version = TryObject(root, "engines", out var engines) ? GetString(engines, "node") : "";
        return new(VersionLabel("Node.js", version), dependencies.ToArray(), candidates.Take(12).ToArray());
    }

    private static string? NodeManager(string declaration, IReadOnlySet<string> locks)
    {
        if (!string.IsNullOrWhiteSpace(declaration))
        {
            var manager = declaration.TrimStart().Split('@', 2)[0].ToLowerInvariant();
            return manager is "npm" or "pnpm" or "yarn" ? manager : null;
        }
        var detected = NodeLockManagers.Where(entry => locks.Contains(entry.Key)).Select(entry => entry.Value)
            .Distinct(StringComparer.Ordinal).ToArray();
        return detected.Length switch { 0 => "npm", 1 => detected[0], _ => null };
    }

    private static bool TryObject(JsonElement root, string name, out JsonElement property)
    {
        property = default;
        return root.ValueKind == JsonValueKind.Object && root.TryGetProperty(name, out property) && property.ValueKind == JsonValueKind.Object;
    }

    private static string GetString(JsonElement root, string name) =>
        root.ValueKind == JsonValueKind.Object && root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()! : "";

    private static string SafeVersion(JsonElement value) =>
        value.ValueKind == JsonValueKind.String && Version.IsMatch(value.GetString()!) ? " " + value.GetString() : "";

    private static Summary Python(string text, ProjectTestEvidence tests, string directory)
    {
        var project = Section(text, "project");
        var dependencies = Declarations(PythonDependencies(project));
        var pytest = dependencies.Any(IsPytest) || Table.Matches(text).Any(match =>
            match.Groups[1].Value is "tool.pytest" or "tool.pytest.ini_options");
        return new(VersionLabel("Python", TomlValue(project, "requires-python")), dependencies, tests.PythonChecks(directory, pytest));
    }

    private static Summary Requirements(string text, ProjectTestEvidence tests, string directory)
    {
        var dependencies = Declarations(text.Split('\n'));
        return new("Python", dependencies, tests.PythonChecks(directory, dependencies.Any(IsPytest)));
    }

    private static bool IsPytest(string requirement) =>
        Requirement.Match(requirement).Groups["name"].Value.Equals("pytest", StringComparison.OrdinalIgnoreCase);

    private static IEnumerable<string> PythonDependencies(string project)
    {
        var array = TomlArrayStart.Match(project);
        if (!array.Success)
            yield break;
        for (var index = array.Index + array.Length; index < project.Length; index++)
        {
            var marker = project[index];
            if (char.IsWhiteSpace(marker) || marker == ',')
                continue;
            if (marker == '#')
            {
                index = project.IndexOf('\n', index);
                if (index < 0)
                    yield break;
                continue;
            }
            if (marker is not ('\'' or '"'))
                yield break;
            var start = index + 1;
            for (index = start; index < project.Length && project[index] != marker; index++)
            {
                if (project[index] is '\r' or '\n')
                    yield break;
                if (marker == '"' && project[index] == '\\')
                    index++;
            }
            if (index >= project.Length)
                yield break;
            yield return project[start..index];
        }
    }

    private static string[] Declarations(IEnumerable<string> values) => values
        .Select(value => value.Split(';', '#')[0].Split(" @ ", 2, StringSplitOptions.None)[0].Trim())
        .Where(value => value.Length is > 0 and <= 256 && Requirement.IsMatch(value))
        .Distinct(StringComparer.OrdinalIgnoreCase).Take(28).ToArray();

    private static Summary Cargo(string text, bool hasTests)
    {
        var deps = TomlKey.Matches(Section(text, "dependencies")).Select(match => match.Groups[1].Value)
            .Where(name => Identifier.IsMatch(name)).Take(28).ToArray();
        return new(VersionLabel("Rust", TomlValue(Section(text, "package"), "rust-version")), deps,
            hasTests ? [new("test", ["cargo", "test"])] : []);
    }

    internal static XElement? ReadDotnetRoot(string text)
    {
        using var input = new StringReader(text);
        using var reader = XmlReader.Create(input, new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null });
        var root = XDocument.Load(reader).Root;
        return root?.Name.LocalName == "Project" ? root : null;
    }

    internal static Summary DescribeDotnet(XElement? root, string name)
    {
        if (root is null)
            return new(null, [], []);
        var packages = root.Descendants().Where(node => node.Name.LocalName == "PackageReference")
            .Select(node => (Name: (string?)node.Attribute("Include"), Version: (string?)node.Attribute("Version")))
            .Where(item => item.Name is not null && Identifier.IsMatch(item.Name)).ToArray();
        var dependencies = packages
            .Take(28).Select(item => item.Name! + (item.Version is not null && Version.IsMatch(item.Version) ? " " + item.Version : "")).ToArray();
        var frameworks = root.Descendants().Where(node => node.Name.LocalName is "TargetFramework" or "TargetFrameworks")
            .SelectMany(node => node.Value.Split(';')).Select(value => value.Trim()).Where(value => Identifier.IsMatch(value))
            .Distinct(StringComparer.Ordinal).Take(8).ToArray();
        var declaredTest = root.Descendants().LastOrDefault(node => node.Name.LocalName == "IsTestProject" &&
            node.Attribute("Condition") is null && node.Parent?.Attribute("Condition") is null)?.Value.Trim();
        var sdk = ((string?)root.Attribute("Sdk"))?.Split(';').Select(item => item.Trim().Split('/')[0]) ?? [];
        var test = !string.Equals(declaredTest, "false", StringComparison.OrdinalIgnoreCase) &&
            (string.Equals(declaredTest, "true", StringComparison.OrdinalIgnoreCase) ||
                sdk.Contains("MSTest.Sdk", StringComparer.OrdinalIgnoreCase) ||
                packages.Any(item => string.Equals(item.Name, "Microsoft.NET.Test.Sdk", StringComparison.OrdinalIgnoreCase)));
        return new(frameworks.Length == 0 ? ".NET" : ".NET " + string.Join(", ", frameworks), dependencies,
            test ? [new("test", ["dotnet", "test", name, "--no-restore"])] : []);
    }

    private static string Section(string text, string name)
    {
        var sections = Table.Matches(text);
        for (var i = 0; i < sections.Count; i++)
            if (sections[i].Groups[1].Value == name)
                return text[(sections[i].Index + sections[i].Length)..(i + 1 == sections.Count ? text.Length : sections[i + 1].Index)];
        return "";
    }

    private static string TomlValue(string text, string key)
    {
        var match = TomlString.Matches(text).FirstOrDefault(item => item.Groups["key"].Value == key);
        return match is null ? "" : match.Groups["double"].Success ? match.Groups["double"].Value : match.Groups["single"].Value;
    }

    private static string VersionLabel(string runtime, string version) => Version.IsMatch(version) ? runtime + " " + version : runtime;

    private static bool IsRequirements(string name) =>
        (name.Equals("requirements.txt", StringComparison.OrdinalIgnoreCase) ||
            name.StartsWith("requirements-", StringComparison.OrdinalIgnoreCase) ||
            name.StartsWith("requirements.", StringComparison.OrdinalIgnoreCase) ||
            name.StartsWith("requirements_", StringComparison.OrdinalIgnoreCase)) && name.EndsWith(".txt", StringComparison.OrdinalIgnoreCase);
}
