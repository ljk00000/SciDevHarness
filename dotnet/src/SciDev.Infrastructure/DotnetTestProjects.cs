using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceProjectContext;

namespace SciDev.Infrastructure;

internal sealed class DotnetTestProjects(string[] files, Func<string, string?> read, CancellationToken token)
{
    private const int MaxProjects = 160;
    private static readonly StringComparer Paths = StringComparer.OrdinalIgnoreCase;
    private sealed record Project(bool Tests, string[] References);
    private readonly string[] _manifests = files.Where(IsProject).Order(Paths).Take(MaxProjects).ToArray();
    private readonly MsBuildReferenceHints _hints = new(files, read, token);
    private readonly Dictionary<string, Project?> _projects = new(StringComparer.Ordinal);

    internal static bool IsProject(string path) => path.EndsWith(".csproj", StringComparison.OrdinalIgnoreCase);

    internal IEnumerable<VerificationCandidate> Find(string project)
    {
        foreach (var path in _manifests)
        {
            token.ThrowIfCancellationRequested();
            string[] args = ["dotnet", "test", path, "--no-restore"];
            if (!Paths.Equals(project, path) && SandboxVerification.Classify(args) == "test" && HasReferenceHint(path, project))
                yield return new("test", path, DirectoryPart(project) is { Length: > 0 } directory ? directory : ".", args, project);
        }
    }

    private bool HasReferenceHint(string testProject, string subject)
    {
        if (Read(testProject) is not { Tests: true } test || Read(subject) is null)
            return false;
        var seen = new HashSet<string>(StringComparer.Ordinal) { testProject };
        var queue = new Queue<Project>();
        queue.Enqueue(test);
        while (queue.TryDequeue(out var item))
        {
            token.ThrowIfCancellationRequested();
            foreach (var path in item.References)
            {
                if (!seen.Add(path))
                    continue;
                if (string.Equals(path, subject, StringComparison.Ordinal))
                    return true;
                if (Read(path) is { } dependency)
                    queue.Enqueue(dependency);
            }
        }
        return false;
    }

    internal static bool SnapshotHasProjects(string workspace, string testProject, string subject)
    {
        var snapshot = new WindowsWorkspace(workspace);
        var sourceDir = Path.Combine(workspace, DirectoryPart(subject));
        if (!Directory.Exists(sourceDir) || new DirectoryInfo(sourceDir).EnumerateFiles().Where(file => IsProject(file.Name)).Take(2).Count() != 1)
            return false;
        bool ExistsAsCopied(string path)
        {
            try
            {
                using var lease = snapshot.OpenPath(path);
                var disk = new DirectoryInfo(workspace);
                foreach (var part in DirectoryPart(path).Split('/', StringSplitOptions.RemoveEmptyEntries))
                {
                    var direct = disk.EnumerateDirectories().FirstOrDefault(child => child.Name == part);
                    if (direct is null)
                        return false;
                    disk = direct;
                }
                return disk.EnumerateFiles().Any(item => item.Name == Path.GetFileName(path));
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException)
            {
                return false;
            }
        }
        return ExistsAsCopied(testProject) && ExistsAsCopied(subject);
    }

    private Project? Read(string path)
    {
        if (_projects.TryGetValue(path, out var value))
            return value;
        if (_projects.Count >= MaxProjects)
            return null;
        var root = _hints.Read(path);
        value = root is null ? null : new(ProjectManifest.DescribeDotnet(root, Path.GetFileName(path)).Checks.Length > 0,
            _hints.FindReferences(path));
        _projects[path] = value;
        return value;
    }

}
