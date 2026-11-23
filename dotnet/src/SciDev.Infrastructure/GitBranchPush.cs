using System.Diagnostics;
using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class GitBranchPush
{
    internal static string[] Arguments(string remote, string branch, string commit) =>
        ["--no-replace-objects", "push", "--porcelain", "--no-verify", "--recurse-submodules=no", "--", remote, $"{commit}:refs/heads/{branch}"];

    internal static ProcessStartInfo Prepare(GitReadContext git, string remote, string credential)
    {
        var start = git.PrepareSnapshotProcess();
        start.Environment["GIT_ALLOW_PROTOCOL"] = "https";
        var header = "Authorization: Basic " + Convert.ToBase64String(Encoding.UTF8.GetBytes("x-access-token:" + credential));
        (string Key, string Value)[] settings =
        [
            ("http.proxy", ""), ("http.followRedirects", "false"), ("http.sslVerify", "true"),
            ("credential.helper", ""), ("push.gpgSign", "false"), ("push.followTags", "false"),
            ("gc.auto", "0"), ("maintenance.auto", "false"), ($"http.{remote}.extraHeader", header),
        ];
        start.Environment["GIT_CONFIG_COUNT"] = settings.Length.ToString(System.Globalization.CultureInfo.InvariantCulture);
        for (var i = 0; i < settings.Length; i++)
        {
            start.Environment[$"GIT_CONFIG_KEY_{i}"] = settings[i].Key;
            start.Environment[$"GIT_CONFIG_VALUE_{i}"] = settings[i].Value;
        }
        return start;
    }

    internal static async Task PushAsync(GitReadContext git, GitHubRepository repository, GitHubPullRequestPlan plan,
        string credential, CancellationToken token)
    {
        try
        {
            var result = await GitProcess.RunAsync(Prepare(git, repository.PushUrl, credential),
                Arguments(repository.PushUrl, plan.Branch, plan.Commit), token, maxChars: 16_384, timeoutSeconds: 300);
            if (!Confirmed(result, plan.Branch))
                throw Unconfirmed();
        }
        catch (Exception ex) when (ex is OperationCanceledException or IOException or System.ComponentModel.Win32Exception ||
            ex is ToolDeniedException { Status: "git_timeout" })
        { throw Unconfirmed(); }
    }

    internal static bool Confirmed(GitReadContext.Output result, string branch)
    {
        if (result.Code != 0 || result.Truncated)
            return false;
        foreach (var line in result.Text.Split('\n'))
        {
            var fields = line.Split('\t');
            if (fields.Length == 3 && fields[0] is " " or "*" or "=" &&
                fields[1].EndsWith(":refs/heads/" + branch, StringComparison.Ordinal))
                return true;
        }
        return false;
    }

    private static ToolDeniedException Unconfirmed() =>
        new("pr_push_unconfirmed", "无法确认远端分支是否已更新。请核对 GitHub 分支和凭据权限后再试。");
}
