using System.Text.RegularExpressions;
using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceProjectContext;

namespace SciDev.Infrastructure;

/// <summary>Read bounded source samples and assign tests to their nearest project.</summary>
internal sealed class ProjectTestEvidence
{
    private const RegexOptions Lines = RegexOptions.Multiline | RegexOptions.CultureInvariant | RegexOptions.NonBacktracking;
    private static readonly Regex PythonFunction = new(@"^(?:async[ \t]+)?def[ \t]+test_\w+[ \t]*\(", Lines);
    private static readonly Regex PythonClass = new(@"^class[ \t]+Test\w*[ \t]*(?:\(([^\r\n)]*)\))?[ \t]*:", Lines);
    private static readonly Regex UnitClass = new(@"^class[ \t]+\w+[ \t]*\([^\r\n)]*\bTestCase\b[^\r\n)]*\)[ \t]*:", Lines);
    private static readonly Regex UnitMethod = new(@"^[ \t]+(?:async[ \t]+)?def[ \t]+test_\w+[ \t]*\(", Lines);
    private static readonly Regex RustTest = new(@"^[ \t]*#\[[ \t]*(?:cfg\([ \t]*test[ \t]*\)|(?:\w+::)?test)[ \t]*\]", Lines);
    private static readonly StringComparer Paths = StringComparer.OrdinalIgnoreCase;
    private const int SourceLimit = 160;
    internal sealed record PythonFile(string Path, bool Pytest, bool Unittest);

    private readonly ILookup<string, PythonFile> _python;
    private readonly HashSet<string> _go;
    private readonly HashSet<string> _rust;
    private readonly HashSet<string> _maven;
    private readonly HashSet<string> _gradle;

    internal ProjectTestEvidence(IEnumerable<string> paths, Func<string, string?> read, CancellationToken token)
    {
        var files = paths.Distinct(Paths).Order(Paths).ToArray();
        var pythonProjects = FindProjects(files, ProjectManifest.UsesPythonTestDiscovery);
        var rustProjects = FindProjects(files, name => name.Equals("Cargo.toml", StringComparison.OrdinalIgnoreCase));
        var python = new List<(string Directory, PythonFile File)>();
        foreach (var path in files.Where(IsPythonFile).Take(SourceLimit))
        {
            token.ThrowIfCancellationRequested();
            if (read(path) is not { } text)
                continue;
            var methods = UnitMethod.IsMatch(text);
            var unittest = methods && UnitClass.IsMatch(text);
            var pytest = PythonFunction.IsMatch(text) || methods && PythonClass.Matches(text).Any(match =>
                !match.Groups[1].Value.Contains("TestCase", StringComparison.Ordinal));
            if (unittest || pytest)
                python.Add((Nearest(path, pythonProjects) ?? "", new(path, pytest, unittest)));
        }
        _python = python.ToLookup(item => item.Directory, item => item.File, Paths);
        _go = Owners(files.Where(path => path.EndsWith("_test.go", StringComparison.OrdinalIgnoreCase)),
            FindProjects(files, ProjectManifest.IsGoManifest));
        _rust = new(Paths);
        foreach (var path in files.Where(path => path.EndsWith(".rs", StringComparison.OrdinalIgnoreCase)).Take(SourceLimit))
        {
            token.ThrowIfCancellationRequested();
            if (Nearest(path, rustProjects) is not { } owner || _rust.Contains(owner))
                continue;
            var relative = FromDirectory(path, owner);
            if ((relative.StartsWith("src/", StringComparison.OrdinalIgnoreCase) || relative.StartsWith("tests/", StringComparison.OrdinalIgnoreCase)) &&
                read(path) is { } text && RustTest.IsMatch(text))
                _rust.Add(owner);
        }
        var jvm = files.Where(path => FileNameHasTest(path) || path.Contains("/src/test/", StringComparison.OrdinalIgnoreCase) ||
            path.StartsWith("src/test/", StringComparison.OrdinalIgnoreCase)).ToArray();
        _maven = Owners(jvm.Where(path => path.EndsWith(".java", StringComparison.OrdinalIgnoreCase)),
            FindProjects(files, name => name.Equals("pom.xml", StringComparison.OrdinalIgnoreCase)));
        _gradle = Owners(jvm.Where(path => Path.GetExtension(path).ToLowerInvariant() is ".java" or ".kt" or ".scala"),
            FindProjects(files, name => name.Equals("build.gradle", StringComparison.OrdinalIgnoreCase) ||
                name.Equals("build.gradle.kts", StringComparison.OrdinalIgnoreCase)));
    }

    internal ProjectManifest.Check[] PythonChecks(string directory, bool declaredPytest)
    {
        var tests = _python[directory];
        var pytest = tests.Where(test => test.Pytest || declaredPytest && test.Unittest)
            .Select(test => FromDirectory(test.Path, directory))
            .Where(path => SandboxVerification.Classify(["python", "-m", "pytest", path]) == "test").Take(16).ToArray();
        if (pytest.Length > 0)
            return [new("test", ["python", "-m", "pytest", ..pytest])];
        return tests.Where(test => test.Unittest).Select(test =>
        {
            var folder = FromDirectory(DirectoryPart(test.Path), directory);
            return new ProjectManifest.Check("test", ["python", "-m", "unittest", "discover", "-s",
                folder.Length == 0 ? "." : folder, "-p", Path.GetFileName(test.Path), "-v"]);
        }).ToArray();
    }

    internal string? PythonSource(string directory) => _python[directory].FirstOrDefault()?.Path;
    internal IEnumerable<string> PythonDirectories => _python.Select(project => project.Key);
    internal IEnumerable<string> RustDirectories => _rust;

    internal bool HasTests(string name, string directory) => name.ToLowerInvariant() switch
    {
        "go.mod" => _go.Contains(directory),
        "cargo.toml" => _rust.Contains(directory),
        "pom.xml" => _maven.Contains(directory),
        "build.gradle" or "build.gradle.kts" => _gradle.Contains(directory),
        _ => false,
    };

    private static HashSet<string> FindProjects(string[] paths, Func<string, bool> matches) =>
        paths.Where(path => matches(Path.GetFileName(path))).Select(DirectoryPart).ToHashSet(Paths);

    private static HashSet<string> Owners(IEnumerable<string> files, HashSet<string> projects) =>
        files.Select(path => Nearest(path, projects)).OfType<string>().ToHashSet(Paths);

    private static string? Nearest(string path, HashSet<string> projects) =>
        Ancestors(path, directory: false).LastOrDefault(projects.Contains);

    private static string FromDirectory(string path, string directory) =>
        directory.Length == 0 ? path : path.Length == directory.Length ? "" : path[(directory.Length + 1)..];

    private static bool IsPythonFile(string path) => path.EndsWith(".py", StringComparison.OrdinalIgnoreCase) &&
        (FileNameHasTest(path) || path.Split('/').SkipLast(1).Contains("tests", Paths));

    private static bool FileNameHasTest(string path) => Path.GetFileNameWithoutExtension(path).Contains("test", StringComparison.OrdinalIgnoreCase);
}
