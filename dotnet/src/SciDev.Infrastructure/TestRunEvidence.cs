using System.Globalization;
using System.Text.RegularExpressions;

namespace SciDev.Infrastructure;

internal static class TestRunEvidence
{
    private const RegexOptions Lines = RegexOptions.Multiline | RegexOptions.CultureInvariant | RegexOptions.IgnoreCase | RegexOptions.NonBacktracking;
    private static readonly Regex UnitCount = new(@"^[ \t]*Ran ([0-9]+) tests? in ", Lines);
    private static readonly Regex UnitSummary = new(@"^[ \t]*OK \(([^\r\n)]+)\)", Lines);
    private static readonly Regex UnitNotPassed = new(@"(?:skipped|expected failures)=([0-9]+)", Lines);
    private static readonly Regex DotnetPassed = new(@"\b(?:Passed|Succeeded|通过)[ \t]*[:：][ \t]*([0-9]+)", Lines);
    private static readonly Regex DotnetEmpty = new(@"\bno (?:test is available|tests (?:were )?(?:found|run|executed)|tests? match(?:es)?|test matches)", Lines);
    private static readonly Regex CargoCount = new(@"^[ \t]*test result: ok\.[ \t]+([0-9]+) passed;", Lines);
    private static readonly Regex GoSummary = new(@"^(?:ok|\?)[ \t]+[^\r\n]+", Lines);
    private static readonly Regex NodeTapPassed = new(@"^#[ \t]+pass[ \t]+([0-9]+)[ \t]*\r?$", Lines);
    private static readonly Regex NodeSpecPassed = new(
        @"^ℹ tests [0-9]+\r?\nℹ suites [0-9]+\r?\nℹ pass ([0-9]+)\r?\nℹ fail [0-9]+\r?$", Lines);
    private static readonly Regex PytestEmpty = new(@"\b(?:no tests ran|collected 0 items|[0-9]+ deselected, 0 selected|[0-9]+ (?:skipped|xfailed|deselected))\b", Lines);
    private static readonly Regex PytestPassed = new(@"\b[1-9][0-9]* passed\b", Lines);
    private static readonly Regex MavenCount = new(@"\bTests run:[ \t]*([0-9]+),[ \t]*Failures:[ \t]*[0-9]+,[ \t]*Errors:[ \t]*[0-9]+,[ \t]*Skipped:[ \t]*([0-9]+)", Lines);
    private static readonly Regex GradleTestTask = new(@"^> Task (?::[\w.\-]+)*:test(?:[ \t]+([^\r\n]+))?[ \t]*\r?$", Lines);

    // These rules only interpret summaries for known runners. Custom scripts may use their own output format.
    internal static string? UnsuccessfulTest(string[] argv, string output) => argv switch
    {
        ["python" or "python3", "-m", "unittest", ..] => Unittest(output),
        ["dotnet", "test", ..] => RequireCount(DotnetPassed, output, DotnetEmpty.IsMatch(output)),
        ["cargo", "test", ..] => RequireCount(CargoCount, output),
        ["go", "test", ..] => Go(output),
        ["node", "--test", ..] => Node(output),
        ["pytest", ..] or ["python" or "python3", "-m", "pytest", ..] =>
            PytestEmpty.IsMatch(output) && !PytestPassed.IsMatch(output) ? "no_tests" : null,
        ["mvn" or "./mvnw", "test", ..] => Maven(output),
        ["gradle" or "./gradlew", "test", ..] => Gradle(output),
        _ => null,
    };

    private static string? RequireCount(Regex pattern, string output, bool empty = false)
    {
        var counts = pattern.Matches(output);
        if (counts.Any(match => Count(match.Groups[1]) > 0))
            return null;
        return counts.Count > 0 || empty ? "no_tests" : "test_result_unconfirmed";
    }

    private static string? Unittest(string output)
    {
        var ran = UnitCount.Match(output);
        if (!ran.Success)
            return "test_result_unconfirmed";
        var notPassed = UnitNotPassed.Matches(UnitSummary.Match(output).Groups[1].Value)
            .Sum(match => (long)Count(match.Groups[1]));
        return Count(ran.Groups[1]) > notPassed ? null : "no_tests";
    }

    private static string? Node(string output)
    {
        var summary = NodeSpecPassed.Match(output);
        if (summary.Success)
            return Count(summary.Groups[1]) > 0 ? null : "no_tests";
        return RequireCount(NodeTapPassed, output);
    }

    private static string? Go(string output)
    {
        var runs = GoSummary.Matches(output);
        if (runs.Any(run => run.Value.StartsWith("ok", StringComparison.Ordinal) &&
            !run.Value.Contains("[no tests", StringComparison.OrdinalIgnoreCase)))
            return null;
        return runs.Count > 0 ? "no_tests" : "test_result_unconfirmed";
    }

    private static string? Gradle(string output)
    {
        var tasks = GradleTestTask.Matches(output);
        return tasks.Count > 0 && tasks.All(task => task.Groups[1].Value.Trim().ToUpperInvariant() is "NO-SOURCE" or "SKIPPED")
            ? "no_tests" : "test_result_unconfirmed";
    }

    private static string? Maven(string output)
    {
        var runs = MavenCount.Matches(output);
        if (runs.Any(run => Count(run.Groups[1]) > Count(run.Groups[2])))
            return null;
        return runs.Count > 0 || output.Contains("No tests to run.", StringComparison.OrdinalIgnoreCase)
            ? "no_tests" : "test_result_unconfirmed";
    }

    private static int Count(Group group) => int.TryParse(group.ValueSpan, NumberStyles.None, CultureInfo.InvariantCulture, out var count) ? count : 0;
}
