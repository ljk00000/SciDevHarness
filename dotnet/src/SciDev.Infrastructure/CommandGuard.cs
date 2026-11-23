using System.Text.RegularExpressions;

namespace SciDev.Infrastructure;

// Keep the legacy preflight for recognized command shapes. Process approvals and Docker remain the execution boundaries.
internal static class CommandGuard
{
    private static readonly Regex Blocked = new(
        @"\b(?:git(?:\.exe)?(?:\s+(?:-(?:c|C)\s+\S+|--(?:git-dir|work-tree|exec-path|config-env)=\S+|--(?:no-pager|no-optional-locks|literal-pathspecs|bare|paginate)))*\s+(?:add|commit|reset|restore|stash|revert|cherry-pick|rebase|merge|checkout|switch)\b|git(?:\.exe)?\s+clean\s+-[a-z]*f[a-z]*\b|rm\s+-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)\b|rmdir\s+/s\b|del\s+/[a-z]*[sq][a-z]*\b|format\s+[a-z]:)",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);

    internal static bool BlockedShell(string command) => Blocked.IsMatch(command);

    internal static bool BlockedContainer(string[] argv)
    {
        if (argv.Length == 0)
            return false;
        var executable = Path.GetFileName(argv[0]).ToLowerInvariant();
        return executable switch
        {
            "sh" or "bash" or "ash" or "dash" when argv is [_, "-c", var shell, ..] => BlockedShell(shell),
            "git" or "git.exe" or "rm" or "rmdir" or "del" or "format" => BlockedShell(string.Join(' ', [executable, .. argv.Skip(1)])),
            _ => false,
        };
    }
}
