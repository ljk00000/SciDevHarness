using System.Text.Json;
using System.Text.RegularExpressions;
using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceProjectContext;

namespace SciDev.Infrastructure;

internal static class CargoWorkspaceTests
{
    private const string WorkingCopy = "/tmp/project";
    private const string ManifestName = "Cargo.toml";
    private static readonly Regex WorkspaceTable = new(
        """(?m)^[ \t]*\[[ \t]*(?:workspace|"workspace"|'workspace')[ \t]*(?:\]|\.)""",
        RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);

    internal static bool IsCargoSource(string path) => path.EndsWith(".rs", StringComparison.OrdinalIgnoreCase) ||
        Path.GetFileName(path) is ManifestName or "Cargo.lock" or "rust-toolchain.toml" ||
        path.EndsWith(".cargo/config.toml", StringComparison.Ordinal);

    internal static VerificationCandidate[] Find(IEnumerable<string> files, string[] affected, IEnumerable<string> testDirectories,
        Func<string, string?> read, CancellationToken token)
    {
        var tests = testDirectories.ToHashSet(StringComparer.Ordinal);
        if (tests.Count == 0)
            return [];
        var projects = files.Where(path => Path.GetFileName(path) == ManifestName).ToHashSet(StringComparer.Ordinal);
        var owners = new Dictionary<string, string?>(StringComparer.Ordinal);
        string? WorkspaceAt(string directory)
        {
            if (owners.TryGetValue(directory, out var owner))
                return owner;
            token.ThrowIfCancellationRequested();
            var manifest = directory.Length == 0 ? ManifestName : directory + "/" + ManifestName;
            owner = projects.Contains(manifest) && read(manifest) is { } text && WorkspaceTable.IsMatch(text) ? manifest : null;
            owners.Add(directory, owner);
            return owner;
        }

        // A declaration only supplies a candidate. Cargo confirms membership in the sealed copy before tests can count.
        var manifests = affected.Where(directory => projects.Contains(directory.Length == 0 ? ManifestName : directory + "/" + ManifestName))
            .Select(directory => (Directory: directory, Workspace: Ancestors(directory, directory: true).Reverse().Distinct()
                .Select(WorkspaceAt).OfType<string>().FirstOrDefault()));
        var candidates = new List<VerificationCandidate>();
        foreach (var group in manifests.Where(project => project.Workspace is not null).GroupBy(project => project.Workspace!))
        {
            var root = DirectoryPart(group.Key);
            var targets = group.Select(project => project.Directory).ToArray();
            if (!tests.Any(directory => directory == root || root.Length == 0 || directory.StartsWith(root + "/", StringComparison.Ordinal)))
                continue;
            if (targets.Length == 1 && targets[0] != root && tests.Contains(targets[0]))
                continue;
            candidates.Add(new("test", group.Key, Scope(root), ["cargo", "test", "--workspace", "--offline"],
                TargetDirectories: targets.Select(Scope).ToArray()));
        }
        return candidates.ToArray();
    }

    internal static string[] ReadMetadata(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var metadata = document.RootElement;
            if (metadata.GetProperty("version").GetInt32() != 1 ||
                metadata.GetProperty("workspace_root").GetString() is not { } root || ToScope(root) is not { } rootScope)
                return [];
            var members = metadata.GetProperty("workspace_members").EnumerateArray().Select(member => member.GetString())
                .OfType<string>().ToHashSet(StringComparer.Ordinal);
            if (members.Count == 0)
                return [];
            var directories = new HashSet<string>(StringComparer.Ordinal) { rootScope };
            foreach (var package in metadata.GetProperty("packages").EnumerateArray())
            {
                if (package.GetProperty("id").GetString() is not { } id || !members.Remove(id))
                    continue;
                const string suffix = "/" + ManifestName;
                if (package.GetProperty("manifest_path").GetString() is { } path && path.EndsWith(suffix, StringComparison.Ordinal) &&
                    ToScope(path[..^suffix.Length]) is { } directory)
                    directories.Add(directory);
            }
            return members.Count == 0 ? directories.Order(StringComparer.Ordinal).ToArray() : [];
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or InvalidOperationException or FormatException)
        {
            return [];
        }
    }

    private static string? ToScope(string path)
    {
        if (path == WorkingCopy)
            return ".";
        if (!path.StartsWith(WorkingCopy + "/", StringComparison.Ordinal))
            return null;
        var directory = path[(WorkingCopy.Length + 1)..];
        return directory.Length != 0 && !directory.Split('/').Any(part => part is "" or "." or "..") &&
            WorkspaceDocuments.IsVisible(directory) ? directory : null;
    }

    private static string Scope(string directory) => directory.Length == 0 ? "." : directory;
}
