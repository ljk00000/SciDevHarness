namespace SciDev.Infrastructure;

/// <summary>Operator-selected argument prefixes for the offline Docker backend.</summary>
internal sealed class ContainerCommandPrefixes(string[][] prefixes)
{
    internal static ContainerCommandPrefixes Parse(string? text)
    {
        if (string.IsNullOrWhiteSpace(text))
            return new([]);
        if (text.Length > 11_000)
            throw Invalid();
        var rows = text.Split(['\r', '\n', ';'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (rows.Length > 50)
            throw Invalid();
        var prefixes = new List<string[]>();
        foreach (var row in rows)
        {
            var parts = row.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
            if (row.Length is < 3 or > 200 || parts.Length == 0 ||
                !parts[0].Any(char.IsAsciiLetterOrDigit) || parts[0].StartsWith('-') || parts[0].Contains('=') || parts[0].Contains(':') ||
                parts.Any(part => part.StartsWith('/') || part.Split('/').Contains("..", StringComparer.Ordinal) ||
                    part.Any(ch => !char.IsAsciiLetterOrDigit(ch) && ch is not ('.' or '_' or '-' or '/' or ':' or '@' or '=' or '+'))))
                throw Invalid();
            prefixes.Add(parts);
        }
        return new(prefixes.ToArray());
    }

    internal bool Allows(string[] arguments) => prefixes.Any(prefix => arguments.AsSpan().StartsWith(prefix, StringComparer.Ordinal));

    private static ArgumentException Invalid() =>
        new("容器命令白名单只接受逐词匹配的参数前缀，以换行或分号分隔。最多 50 项，每项最多 200 字符。");
}
