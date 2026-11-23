namespace SciDev.Core;

/// <summary>Recognize routine verification invocations in the offline project container.</summary>
public static class SandboxVerification
{
    public static bool IsCargoWorkspaceTest(string[] argv) => argv is
        ["cargo", "test", "--workspace"] or ["cargo", "test", "--workspace", "--offline"] or
        ["cargo", "test", "--offline", "--workspace"];

    public static string? EvidenceCategory(string[] argv)
    {
        var automatic = Classify(argv);
        if (automatic is not null || argv.Any(argument => !IsPlainArgument(argument)))
            return automatic;
        return argv switch
        {
            ["dotnet", "build", ..var args] when args.All(arg => IsTarget(arg) || arg == "--no-restore") => "build",
            ["cargo", "build", ..var args] when args.All(arg => IsTarget(arg) || arg is "--offline" or "--locked") => "build",
            ["npm" or "pnpm" or "yarn", "run", "build"] => "build",
            ["python" or "python3", "-m", "py_compile" or "compileall", ..var args] when args.Length > 0 &&
                args.All(arg => IsTarget(arg) || arg == "-q") => "compile",
            _ => null,
        };
    }

    public static string? Classify(string[] argv)
    {
        if (argv.Any(argument => !IsPlainArgument(argument)))
            return null;

        return argv switch
        {
            ["node", "--test", ..var files] when files.All(IsTarget) => "test",
            ["pytest", ..var args] when args.All(IsTestArgument) => "test",
            ["python" or "python3", "-m", "unittest", "discover", "-s", var directory, "-p", var pattern, "-v"]
                when IsTarget(directory) && IsTarget(pattern) && !pattern.Contains('/') && pattern.EndsWith(".py", StringComparison.OrdinalIgnoreCase) => "test",
            ["python" or "python3", "-m", "unittest", "discover", "-s", var directory, ..var flags]
                when IsTarget(directory) && flags.All(flag => flag is "-v" or "-q" or "--verbose" or "--quiet") => "test",
            ["python" or "python3", "-m", "pytest" or "unittest", ..var args] when args.All(IsTestArgument) => "test",
            ["npm" or "pnpm" or "yarn", "test"] => "test",
            ["npm" or "pnpm" or "yarn", "run", var script] => ScriptKind(script),
            ["dotnet", "test", ..var args] when args.All(arg => IsTarget(arg) || arg is "--no-restore" or "--no-build") => "test",
            ["cargo", "test", ..] when IsCargoWorkspaceTest(argv) => "test",
            ["go" or "cargo" or "mvn" or "gradle" or "./gradlew" or "./mvnw", "test", ..var targets]
                when targets.All(IsTarget) => "test",
            ["ruff" or "cargo", "check", ..var targets] when targets.All(IsTarget) => "lint",
            ["flake8" or "pylint" or "eslint", ..var targets] when targets.All(IsTarget) => "lint",
            ["go", "vet", ..var targets] when targets.All(IsTarget) => "lint",
            ["mypy" or "pyright", ..var targets] when targets.All(IsTarget) => "typecheck",
            ["tsc", "--noEmit", ..var targets] when targets.All(IsTarget) => "typecheck",
            _ => null,
        };
    }

    private static string? ScriptKind(string script)
    {
        if (script == "test" || (script.Length > 5 && script.StartsWith("test", StringComparison.Ordinal) &&
            script[4] is ':' or '-' or '_' && IsScriptSuffix(script[5..])))
            return "test";
        if (script == "lint" || (script.StartsWith("lint:", StringComparison.Ordinal) && IsScriptSuffix(script[5..])))
            return "lint";
        return script == "typecheck" ? "typecheck" : null;
    }

    private static bool IsScriptSuffix(string value) => value.Length > 0 &&
        value.All(ch => char.IsAsciiLetterOrDigit(ch) || ch is '_' or '-' or '.' or ':');

    private static bool IsTestArgument(string argument) =>
        IsTarget(argument) || argument is "-q" or "-v" or "-vv" or "-x" or "--quiet" or "--verbose";

    private static bool IsTarget(string value)
    {
        if (value.Length == 0 || value[0] == '-' || value.Any(char.IsWhiteSpace))
            return false;
        return value.Split("::", StringSplitOptions.None).All(part => part.Length > 0 &&
            part.All(ch => char.IsLetterOrDigit(ch) || ch is '_' or '-' or '.' or '/' or '[' or ']'));
    }

    private static bool IsPlainArgument(string value) =>
        value.Length > 0 && !value.StartsWith('/') && !value.StartsWith('~') &&
        !value.Contains("://", StringComparison.Ordinal) && !value.Split('/').Contains("..", StringComparer.Ordinal) &&
        !value.Any(ch => char.IsControl(ch) || ch is '\\' or ';' or '&' or '|' or '<' or '>' or '`' or '$');
}
