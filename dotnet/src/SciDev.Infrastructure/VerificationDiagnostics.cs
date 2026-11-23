using System.Globalization;
using System.Text.RegularExpressions;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class VerificationDiagnostics
{
    private const RegexOptions Safe = RegexOptions.CultureInvariant | RegexOptions.NonBacktracking;
    private static readonly Regex Ansi = new(@"\x1B\[[0-?]*[ -/]*[@-~]", Safe);
    private static readonly Regex Parenthesized = new(
        """^(?<file>[^()]+)\((?<line>\d+),(?<column>\d+)(?:,\d+,\d+)?\):\s*(?<detail>.+)$""", Safe);
    private static readonly Regex Colon = new("""^(?<file>[^:]+):(?<line>\d+)(?::(?<column>\d+))?:\s*(?<detail>.+)$""", Safe);
    private static readonly Regex Pytest = new("""^(?:FAILED|ERROR)\s+(?<file>[^:\s]+)(?:::|\s|$)\s*(?<detail>.*)$""", Safe);
    private static readonly Regex Tap = new("""^location:\s*['"](?<file>[^'"]+?):(?<line>\d+):(?<column>\d+)['"]$""", Safe);
    private static readonly Regex Detail = new(
        """^(?:(?<severity>fatal error|error|warning|note)(?:\s+|:\s*))?(?:(?<code>[A-Z]+\d+)(?:\s+|:\s*))?(?<message>.*)$""", Safe);
    private static readonly Regex MypyCode = new("""\s+\[(?<code>[a-z][a-z-]*)\]$""", Safe);

    internal static VerificationDiagnostic[] Parse(string category, string output, string directory)
    {
        var results = new List<VerificationDiagnostic>();
        var seen = new HashSet<VerificationDiagnostic>();
        using var reader = new StringReader(output);
        while (results.Count < 20 && reader.ReadLine() is { } raw)
        {
            if (raw.Length is 0 or > 1200)
                continue;
            var row = Ansi.Replace(raw, "").Trim();
            if (row.Any(ch => char.IsControl(ch) && ch != '\t'))
                continue;
            var match = Parenthesized.Match(row);
            if (!match.Success)
                match = Colon.Match(row);
            if (!match.Success && category == "test")
                match = Pytest.Match(row);
            if (!match.Success && category == "test")
                match = Tap.Match(row);
            if (!match.Success || RelativePath(match.Groups["file"].Value, directory) is not { } path)
                continue;
            var detail = Detail.Match(match.Groups["detail"].Value.Trim());
            var message = detail.Groups["message"].Value;
            var code = detail.Groups["code"].Value;
            var bracket = MypyCode.Match(message);
            if (code.Length == 0 && bracket.Success)
            {
                code = bracket.Groups["code"].Value;
                message = message[..bracket.Index].TrimEnd();
            }
            if (message.Length == 0)
                message = "测试失败";
            if (message.Length > 240)
                message = message[..(char.IsHighSurrogate(message[239]) ? 239 : 240)];
            var severity = detail.Groups["severity"].Value switch
            {
                "warning" => "warning",
                "note" => "note",
                _ => "error",
            };
            var item = new VerificationDiagnostic(path, Number(match, "line"), Number(match, "column"), severity,
                code.Length == 0 ? null : code, message);
            if (seen.Add(item))
                results.Add(item);
        }
        return results.ToArray();
    }

    private static int? Number(Match match, string group) =>
        int.TryParse(match.Groups[group].Value, NumberStyles.None, CultureInfo.InvariantCulture, out var number) && number > 0
            ? number : null;

    private static string? RelativePath(string value, string directory)
    {
        var path = value.Trim().Trim('\'', '"').Replace('\\', '/');
        if (path.StartsWith("file://", StringComparison.Ordinal))
            path = path[7..];
        if (path.StartsWith("/tmp/project/", StringComparison.Ordinal))
            path = path[13..];
        else
        {
            while (path.StartsWith("./", StringComparison.Ordinal))
                path = path[2..];
            if (path.StartsWith('/') || path.Contains(':'))
                return null;
            if (directory != ".")
                path = directory + "/" + path;
        }
        // Tool diagnostics name source files, not progress counters or arbitrary labels.
        if (!Path.HasExtension(path))
            return null;
        try { return WorkspaceDocuments.VisiblePath(path, allowHidden: false); }
        catch (ToolDeniedException) { return null; }
    }
}
