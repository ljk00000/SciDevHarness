using System.Globalization;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Cli;

internal static class PullRequestCommand
{
    internal const string Usage = "pr --workspace <directory> [--dry-run] [--metadata-workspace <approved-repository>] [--base <branch>]\n"
        + "    [--title <text>] [--body <text>] [--draft] [--allow-network] [--json]\n"
        + "pr-view | pr-diff | pr-review --workspace <directory> --number <n>\n"
        + "    [--metadata-workspace <approved-repository>] [--allow-network] [--json]\n"
        + "    pr-diff [--max-chars <n>]\n"
        + "    pr-review --action approve|comment|request-changes [--body <text>]";

    private sealed record Options(string Workspace, int Number, int MaxChars, string? Action, string? Body,
        string? Metadata, bool OverrideNetwork, bool DryRun, string? Base, string? Title, bool Draft);

    public static async Task<int> RunAsync(string[] args, JsonSerializerOptions json)
    {
        if (args.Length == 2 && args[1] is "-h" or "--help")
        {
            Console.WriteLine(Usage);
            return 0;
        }
        var asJson = args.Contains("--json", StringComparer.Ordinal);
        using var stop = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; stop.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            var options = Parse(args);
            if (args[0] == "pr" && options.DryRun)
            {
                var plan = await GitHubPullRequestPlan.BuildAsync(options.Workspace, options.Metadata,
                    options.Base, options.Title, options.Draft, stop.Token);
                var key = Environment.GetEnvironmentVariable("SCIDEV_API_KEY")?.Trim();
                Console.WriteLine(asJson ? RunJson.SerializeRedacted(plan, json, key) :
                    $"[{plan.Repo}] {plan.Branch} -> {plan.Base} (dry run)\n" +
                    (string.IsNullOrEmpty(key) ? plan.Title : plan.Title.Replace(key, "[redacted]", StringComparison.Ordinal)));
                return 0;
            }
            var reviewer = Console.IsInputRedirected || Console.IsErrorRedirected ? null : new ConsoleOperationReview();
            var github = await GitHubPullRequests.OpenAsync(options.Workspace, options.Metadata, reviewer, options.OverrideNetwork, stop.Token);
            switch (args[0])
            {
                case "pr":
                    var created = await github.PublishAsync(options.Metadata, options.Base, options.Title, options.Body, options.Draft, stop.Token);
                    Print(created, created.Url);
                    break;
                case "pr-view":
                    var view = await github.ViewAsync(options.Number, stop.Token);
                    Print(view, $"#{options.Number} {view.GetProperty("title").GetString()} [{view.GetProperty("state").GetString()}]\n" +
                        view.GetProperty("url").GetString());
                    break;
                case "pr-diff":
                    var diff = await github.DiffAsync(options.Number, options.MaxChars, stop.Token);
                    Print(new { diff.Number, diff = diff.Text, diff.Truncated, diff.Url }, diff.Text);
                    if (diff.Truncated && !asJson)
                        Console.Error.WriteLine("[diff truncated]");
                    break;
                case "pr-review":
                    var review = await github.ReviewAsync(options.Number, options.Action!, options.Body, stop.Token);
                    Print(review, $"#{review.Number} {review.Action}: {review.Url}");
                    break;
            }
            return 0;

            void Print(object data, string human) =>
                Console.WriteLine(asJson ? github.Serialize(data, json) : github.Display(human));
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested)
        { return Error("cancelled", "GitHub PR 操作已取消。", 130); }
        catch (ArgumentException) { return Error("invalid_input", Usage, 2); }
        catch (ToolDeniedException ex) { return Error(ex.Status, ex.Message, 1); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.ComponentModel.Win32Exception)
        { return Error("pr_workspace_error", "无法读取 PR 所用的仓库配置或启动本机凭据工具。", 1); }
        finally { Console.CancelKeyPress -= cancel; }

        int Error(string status, string message, int exit)
        {
            if (asJson)
                Console.WriteLine(JsonSerializer.Serialize(new { status, message }, json));
            else
                Console.Error.WriteLine($"{status} · {message}");
            return exit;
        }
    }

    private static Options Parse(string[] args)
    {
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var i = 1; i < args.Length; i++)
        {
            var flag = args[i];
            if (flag is "--allow-network" or "--json" || args[0] == "pr" && flag is "--dry-run" or "--draft")
            {
                if (!values.TryAdd(flag, "true"))
                    throw new ArgumentException();
                continue;
            }
            if (!Allowed(args[0], flag) || i + 1 >= args.Length || args[i + 1].StartsWith("--", StringComparison.Ordinal) ||
                !values.TryAdd(flag, args[++i]))
                throw new ArgumentException();
        }
        var number = 0;
        if (!values.TryGetValue("--workspace", out var workspace) || string.IsNullOrWhiteSpace(workspace) ||
            args[0] != "pr" && (!int.TryParse(values.GetValueOrDefault("--number")?.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out number) ||
                number is < 1 or > 1_000_000_000))
            throw new ArgumentException();
        var limit = 300_000;
        if (values.TryGetValue("--max-chars", out var size) && !int.TryParse(size, NumberStyles.Integer, CultureInfo.InvariantCulture, out limit))
            throw new ArgumentException();
        var action = values.GetValueOrDefault("--action")?.Trim().ToLowerInvariant().Replace('_', '-');
        var body = values.GetValueOrDefault("--body");
        if (args[0] == "pr" && body?.Trim().Length > 8000)
            throw new ArgumentException();
        if (args[0] == "pr-review" && (action is not ("approve" or "comment" or "request-changes") ||
            action != "approve" && string.IsNullOrWhiteSpace(body) || body?.Trim().Length > 20_000))
            throw new ArgumentException();
        return new(Path.GetFullPath(workspace), number, limit, action, body, values.GetValueOrDefault("--metadata-workspace"),
            values.ContainsKey("--allow-network"), values.ContainsKey("--dry-run"), values.GetValueOrDefault("--base"),
            values.GetValueOrDefault("--title"), values.ContainsKey("--draft"));
    }

    private static bool Allowed(string command, string flag) =>
        flag is "--workspace" or "--metadata-workspace" ||
        command == "pr" && flag is "--base" or "--title" or "--body" ||
        command != "pr" && flag == "--number" ||
        command == "pr-diff" && flag == "--max-chars" ||
        command == "pr-review" && flag is "--action" or "--body";
}
