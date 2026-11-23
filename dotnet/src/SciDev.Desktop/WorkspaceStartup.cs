using System.IO;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal sealed class WorkspaceStartup(string stateDirectory)
{
    private const string Recent = "last-workspace.txt";

    public static WorkspaceStartup ForUser()
    {
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return new(Path.Combine(local.Length == 0 ? Path.GetTempPath() : local, "SciDevHarness", "Workbench"));
    }

    public string Resolve(string workingDirectory, string executableDirectory)
    {
        var remembered = ReadRecent();
        if (remembered is not null)
            return remembered;
        var checkout = FindCheckout(workingDirectory) ?? FindCheckout(executableDirectory);
        if (checkout is not null)
            return checkout;
        try
        {
            var workspace = Directory.CreateDirectory(Path.Combine(stateDirectory, "Workspace")).FullName;
            if (CanOpen(workspace))
                return workspace;
        }
        catch (Exception ex) when (Expected(ex)) { }
        return Directory.CreateTempSubdirectory("SciDevHarness-Workspace-").FullName;
    }

    public void Remember(string workspace)
    {
        if (!CanOpen(workspace))
            return;
        string? pending = null;
        try
        {
            Directory.CreateDirectory(stateDirectory);
            pending = Path.Combine(stateDirectory, $"{Guid.NewGuid():N}.tmp");
            File.WriteAllText(pending, Path.GetFullPath(workspace));
            File.Move(pending, Path.Combine(stateDirectory, Recent), overwrite: true);
            pending = null;
        }
        catch (Exception ex) when (Expected(ex)) { }
        finally
        {
            if (pending is not null)
                try { File.Delete(pending); }
                catch (Exception ex) when (Expected(ex)) { }
        }
    }

    private string? ReadRecent()
    {
        try
        {
            var file = new FileInfo(Path.Combine(stateDirectory, Recent));
            if (!file.Exists || file.Length > 4096 || file.Attributes.HasFlag(FileAttributes.ReparsePoint))
                return null;
            var path = File.ReadAllText(file.FullName);
            return CanOpen(path) ? Path.GetFullPath(path) : null;
        }
        catch (Exception ex) when (Expected(ex)) { return null; }
    }

    private static string? FindCheckout(string path)
    {
        var root = Path.GetPathRoot(path);
        if (root is not { Length: 3 } || root[1] != ':')
            return null;
        for (DirectoryInfo? directory = new(path); directory is not null; directory = directory.Parent)
        {
            if (File.Exists(Path.Combine(directory.FullName, "dotnet", "SciDevHarness.slnx")) &&
                File.Exists(Path.Combine(directory.FullName, "README.md")) && CanOpen(directory.FullName))
                return directory.FullName;
        }
        return null;
    }

    internal static bool CanOpen(string workspace)
    {
        try
        {
            if (!Path.IsPathFullyQualified(workspace))
                return false;
            _ = new WorkspaceDocuments(workspace);
            return true;
        }
        catch (Exception ex) when (Expected(ex)) { return false; }
    }

    private static bool Expected(Exception ex) => ex is ArgumentException or IOException or UnauthorizedAccessException or ToolDeniedException;
}
