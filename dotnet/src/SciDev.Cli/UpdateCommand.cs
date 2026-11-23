using System.ComponentModel;
using System.Text.Json;
using System.Text.Json.Nodes;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Cli;

internal static class UpdateCommand
{
    internal const string Usage = "update [--workspace <directory>] [--repo <owner/repo>] [--allow-network] [--json]\n" +
        "    [--download <path> [--sha256 <digest>] [--install]] [--fail-if-available]\n" +
        "    Network requests and installer launch each require interactive approval.";

    internal static async Task<int> RunAsync(string[] args, JsonSerializerOptions json)
    {
        if (args is ["update", "--help" or "-h"])
        {
            Console.WriteLine(Usage);
            return 0;
        }
        Dictionary<string, string> options;
        try { options = Parse(args); }
        catch (ArgumentException)
        {
            Console.Error.WriteLine(Usage);
            return 2;
        }

        if (Console.IsInputRedirected)
        {
            Console.Error.WriteLine("检查更新需要终端逐次审批。");
            return 1;
        }
        using var stop = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; stop.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            var workspace = Path.GetFullPath(options.GetValueOrDefault("--workspace", "."));
            var client = new PreviewUpdates(workspace, new ConsoleOperationReview(), options.ContainsKey("--allow-network"));
            var release = await client.CheckAsync(options.GetValueOrDefault("--repo"), stop.Token);
            VerifiedUpdate? downloaded = null;
            int? installer = null;
            if (options.TryGetValue("--download", out var path))
            {
                downloaded = await client.DownloadAsync(release, ExpandUser(path), options.GetValueOrDefault("--sha256"), null, stop.Token);
                if (options.ContainsKey("--install"))
                {
                    try { installer = await client.LaunchAsync(downloaded, stop.Token); }
                    catch
                    {
                        Console.Error.WriteLine($"已验证的安装包已保存至 {downloaded.Path}");
                        throw;
                    }
                }
            }
            WriteResult(options.ContainsKey("--json"), json, release, downloaded, installer);
            return options.ContainsKey("--fail-if-available") && release.Available ? 1 : 0;
        }
        catch (OperationCanceledException) { Console.Error.WriteLine("更新已取消。"); return 130; }
        catch (Exception error) when (error is ArgumentException or ToolDeniedException or IOException or
            UnauthorizedAccessException or Win32Exception or NotSupportedException)
        {
            Console.Error.WriteLine(error.Message);
            return 1;
        }
        finally { Console.CancelKeyPress -= cancel; }
    }

    private static void WriteResult(bool asJson, JsonSerializerOptions json, PreviewUpdates.Release release,
        VerifiedUpdate? downloaded, int? installer)
    {
        if (asJson)
        {
            var output = JsonSerializer.SerializeToNode(release, json)!.AsObject();
            if (downloaded is not null)
                output["downloaded"] = JsonSerializer.SerializeToNode(downloaded, json);
            if (installer is { } pid)
                output["installer_pid"] = JsonValue.Create(pid);
            Console.WriteLine(output.ToJsonString(json));
            return;
        }
        Console.WriteLine(release.Available ? $"可用更新 {release.Current} → {release.Latest}" : $"当前版本 {release.Current} 已是最新版本");
        if (release.Available)
        {
            Console.WriteLine(release.Url);
            Console.WriteLine(release.AssetUrl);
        }
        if (downloaded is not null)
            Console.WriteLine($"已下载并校验 {downloaded.Path}\nSHA-256 {downloaded.Sha256}");
        if (installer is { } processId)
            Console.WriteLine($"已启动安装器（进程 {processId}），请在安装器窗口中继续。");
    }

    private static Dictionary<string, string> Parse(string[] args)
    {
        var options = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var i = 1; i < args.Length; i++)
        {
            var key = args[i];
            var value = "true";
            if (key is not ("--allow-network" or "--json" or "--install" or "--fail-if-available"))
            {
                if (key is not ("--workspace" or "--repo" or "--download" or "--sha256") || i + 1 == args.Length ||
                    args[i + 1].StartsWith("--", StringComparison.Ordinal) || string.IsNullOrWhiteSpace(args[i + 1]))
                    throw new ArgumentException();
                value = args[++i];
            }
            if (!options.TryAdd(key, value))
                throw new ArgumentException();
        }
        if ((options.ContainsKey("--install") || options.ContainsKey("--sha256")) && !options.ContainsKey("--download"))
            throw new ArgumentException();
        return options;
    }

    private static string ExpandUser(string path) => path.StartsWith("~/", StringComparison.Ordinal) ||
        path.StartsWith("~\\", StringComparison.Ordinal)
        ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), path[2..]) : path;
}
