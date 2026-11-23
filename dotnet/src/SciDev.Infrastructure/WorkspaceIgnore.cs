using System.Text;
using System.Text.RegularExpressions;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class WorkspaceIgnore
{
    internal sealed record Rule(string Prefix, Regex Pattern, bool FileNameOnly, bool DirectoryOnly, bool Include)
    {
        public bool Matches(WorkspaceDocuments.Entry entry) =>
            (!DirectoryOnly || entry.IsDirectory) && Pattern.IsMatch(FileNameOnly ? entry.Name : entry.RelativePath[Prefix.Length..]);
    }

    internal static Rule[] Read(WindowsWorkspace workspace, string? folder, Rule[] inherited)
    {
        var prefix = folder is null ? "" : folder + "/";
        using var target = workspace.OpenOperatorPath(prefix + ".gitignore")!;
        return ReadFile(target.AbsolutePath, prefix, inherited);
    }

    internal static Rule[] ReadRepositoryExcludes(WindowsWorkspace workspace)
    {
        using var repository = workspace.OpenOperatorPath(".git")!;
        FileAttributes attributes;
        try { attributes = File.GetAttributes(repository.AbsolutePath); }
        catch (FileNotFoundException) { return []; }
        if ((attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != FileAttributes.Directory)
            return [];
        using var target = workspace.OpenOperatorPath(".git/info/exclude", allowMissingParents: true);
        return target is null ? [] : ReadFile(target.AbsolutePath, "", []);
    }

    private static Rule[] ReadFile(string absolute, string prefix, Rule[] inherited)
    {
        if (!WindowsWorkspace.FileExists(absolute))
            return inherited;
        using var snapshot = WorkspaceFiles.OpenSnapshot(absolute, FileShare.Read, maxBytes: 32_768);
        using var source = new StringReader(WorkspaceFiles.Utf8.GetString(snapshot.Bytes).TrimStart('\uFEFF'));
        var rules = new List<Rule>(inherited);
        while (source.ReadLine() is { } line)
        {
            // Spaces at the end of a rule only count when escaped.
            var length = line.Length;
            while (length > 0 && line[length - 1] == ' ' && !Escaped(line, length - 1))
                length--;
            line = line[..length];
            if (line.Length == 0 || line[0] == '#')
                continue;
            var include = line[0] == '!';
            if (include)
                line = line[1..];
            var directoryOnly = line.EndsWith('/');
            if (directoryOnly)
                line = line[..^1];
            var nameOnly = !line.Contains('/');
            if (line.StartsWith('/'))
                line = line[1..];
            if (line.Length == 0)
                continue;
            if (line.Length > 256 || rules.Count == 256)
                throw new ToolDeniedException("context_ignore_limit", "当前目录的忽略规则超出处理上限。");
            rules.Add(new(prefix, Pattern(line), nameOnly, directoryOnly, include));
        }
        return rules.ToArray();
    }

    internal static bool Excludes(Rule[] rules, WorkspaceDocuments.Entry entry)
    {
        for (var index = rules.Length - 1; index >= 0; index--)
            if (rules[index].Matches(entry))
                return !rules[index].Include;
        return false;
    }

    internal static Regex Pattern(string glob)
    {
        var expression = new StringBuilder("\\A");
        for (var i = 0; i < glob.Length; i++)
        {
            switch (glob[i])
            {
                case '\\' when i + 1 < glob.Length:
                    expression.Append(Regex.Escape(glob[++i].ToString()));
                    break;
                case '*':
                    var start = i;
                    while (i + 1 < glob.Length && glob[i + 1] == '*')
                        i++;
                    if (i > start && (start == 0 || glob[start - 1] == '/') && i + 1 < glob.Length && glob[i + 1] == '/')
                    {
                        expression.Append("(?:[^/]+/)*");
                        i++;
                    }
                    else if (i > start && start > 0 && glob[start - 1] == '/' && i == glob.Length - 1)
                        expression.Append(".*");
                    else
                        expression.Append("[^/]*");
                    break;
                case '?':
                    expression.Append("[^/]");
                    break;
                case '[':
                    var end = glob.IndexOf(']', i + 1);
                    if (end < 0)
                        throw InvalidPattern();
                    var members = glob[(i + 1)..end];
                    if (members.Length == 0 || members.Any(ch => ch is '/' or '\\' or '['))
                        throw InvalidPattern();
                    var negated = members[0] is '!' or '^';
                    if (negated && members.Length == 1)
                        throw InvalidPattern();
                    if (negated)
                        expression.Append("[^/").Append(members[1..]).Append(']');
                    else
                        expression.Append('[').Append(members).Append("-[/]]");
                    i = end;
                    break;
                default:
                    expression.Append(Regex.Escape(glob[i].ToString()));
                    break;
            }
        }
        try
        {
            return new Regex(expression.Append("\\z").ToString(),
                RegexOptions.CultureInvariant | RegexOptions.IgnoreCase | RegexOptions.NonBacktracking);
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException) { throw InvalidPattern(); }
    }

    private static bool Escaped(string value, int index)
    {
        var offset = index;
        while (index > 0 && value[index - 1] == '\\')
            index--;
        return (offset - index) % 2 != 0;
    }

    private static ToolDeniedException InvalidPattern() =>
        new("context_ignore_pattern", "文件模式不受支持，请简化查找模式或缩小范围。");
}
