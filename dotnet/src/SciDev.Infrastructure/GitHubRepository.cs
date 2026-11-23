using System.Text.RegularExpressions;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed partial record GitHubRepository(string Owner, string Name)
{
    public string Slug => $"{Owner}/{Name}";
    public string ApiRepository => $"/repos/{Slug}";
    public string PushUrl => $"https://github.com/{Slug}.git";
    public string ApiPath(int number) => $"{ApiRepository}/pulls/{number}";
    public string PullUrl(int number) => $"https://github.com/{Slug}/pull/{number}";

    public static async Task<GitHubRepository> ReadAsync(string workspace, string? approvedRepository, CancellationToken token)
    {
        var locations = GitLocations.Resolve(workspace, new WindowsWorkspace(workspace), approvedRepository);
        var metadata = new WindowsWorkspace(locations.CommonDirectory);
        using var config = metadata.OpenPath("config");
        using var file = WindowsWorkspace.OpenForRead(config.AbsolutePath, FileShare.Read);
        WindowsWorkspace.GetFileIdentity(file, config.AbsolutePath);
        using var size = new FileStream(file, FileAccess.Read);
        if (size.Length > 1_000_000)
            throw InvalidOrigin();

        var start = GitProcess.Prepare(GitProcess.RequireExecutable(), workspace, "NUL");
        var result = await GitProcess.RunAsync(start,
            ["config", "--file", config.AbsolutePath, "--no-includes", "--null", "--get-all", "remote.origin.url"],
            token, maxChars: 4096);
        var values = result.Text.Split('\0');
        if (result.Code != 0 || result.Truncated || values.Length != 2 || values[1] != "" || Parse(values[0]) is not { } repository)
            throw InvalidOrigin();
        return repository;
    }

    internal static GitHubRepository? Parse(string value)
    {
        if (value.Length is 0 or > 512 || value.Any(ch => char.IsControl(ch) || char.IsWhiteSpace(ch)))
            return null;
        var match = OriginPattern().Match(value);
        if (!match.Success)
            return null;
        var owner = match.Groups[1].Value;
        var repo = match.Groups[2].Value;
        return repo is "." or ".." || repo.EndsWith('.') ? null : new(owner, repo);
    }

    [GeneratedRegex(@"\A(?:https://github\.com(?::443)?/|git@github\.com:|ssh://git@github\.com(?::22)?/|ssh://git@ssh\.github\.com:443/)([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?\z",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex OriginPattern();

    private static ToolDeniedException InvalidOrigin() =>
        new("pr_origin_invalid", "仓库需要唯一的 GitHub origin，使用 HTTPS 或 GitHub SSH 地址。");
}
