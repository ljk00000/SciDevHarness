using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Data.Sqlite;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Cli;

internal static class Diagnostics
{
    public static async Task<int> RunAsync(string[] args, JsonSerializerOptions json)
    {
        using var stop = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; stop.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            var parameters = args.Skip(1).Where(value => value != "--json").ToArray();
            object result;
            int exit;
            if (!OperatingSystem.IsWindows())
                throw new ArgumentException("诊断原型只支持 Windows。");
            switch (args[0])
            {
                case "check-pelican" when parameters.Length is 2 or 4:
                    var nativeSvg = NativePelicanValidator.CheckFile(Option(parameters, "--svg"),
                        parameters.Length == 4 ? Option(parameters, "--preview") : null, stop.Token);
                    result = nativeSvg;
                    exit = nativeSvg.Status == "passed" ? 0 : 1;
                    break;
                case "terminal-check" when parameters.Length == 0:
                    var terminal = await TerminalProbe.CheckAsync(stop.Token);
                    result = terminal;
                    exit = terminal is { EchoedChinese: true, Resized: true, Cancelled: true, ExitCode: 7, ChildExited: true } ? 0 : 1;
                    break;
                case "sandbox-check" when parameters.Length == 4:
                    var workspace = Option(parameters, "--workspace");
                    var image = Option(parameters, "--sandbox-image");
                    var sandbox = await DockerProbe.CheckAsync(Path.GetFullPath(workspace), image, stop.Token);
                    result = sandbox;
                    exit = sandbox.Available ? 0 : 1;
                    break;
                case "migrate" when parameters.Length == 3 && parameters.Contains("--dry-run"):
                    var copy = parameters.Where(value => value != "--dry-run").ToArray();
                    if (copy[0] != "--workspace")
                        throw new ArgumentException("请用 --workspace 指定旧数据副本。");
                    var audit = LegacyStateAudit.Check(Path.GetFullPath(copy[1]), stop.Token);
                    result = audit;
                    exit = audit.Status is "ready_to_import" or "no_python_state" ? 0 : 1;
                    break;
                case "migrate" when parameters.Length is 4 or 6:
                    var imported = LegacyStateImport.Import(Option(parameters, "--workspace"),
                        Option(parameters, "--snapshot"), stop.Token,
                        parameters.Length == 6 ? Option(parameters, "--qt-preferences") : null);
                    result = imported;
                    exit = imported.Status == "imported" ? 0 : 1;
                    break;
                case "history" when parameters.Length is 2 or 4:
                    var limit = parameters.Length == 2 ? 20 : int.TryParse(Option(parameters, "--limit"), out var parsed) ? parsed :
                        throw new ArgumentException("历史记录条数无效。");
                    result = new { runs = RunHistory.Recent(Path.GetFullPath(Option(parameters, "--workspace")), limit, stop.Token) };
                    exit = 0;
                    break;
                case "sessions" when parameters.Length is 2 or 4:
                    var root = Path.GetFullPath(Option(parameters, "--workspace"));
                    if (!Directory.Exists(root))
                    {
                        Console.Error.WriteLine("工作区目录不存在。");
                        return 2;
                    }
                    var max = parameters.Length == 2 ? 20 : int.TryParse(Option(parameters, "--limit"), out var requested) ?
                        Math.Clamp(requested, 1, 100) : throw new ArgumentException("会话记录条数无效。");
                    var sessions = CliSession.Recent(root, max, stop.Token);
                    if (!args.Contains("--json"))
                    {
                        foreach (var session in sessions)
                        {
                            var prompt = string.Join(' ', session.Prompt.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
                            Console.WriteLine($"{session.SessionId}\t{session.Status}\t{session.UpdatedAt:O}\t{prompt[..Math.Min(80, prompt.Length)]}");
                        }
                        return 0;
                    }
                    result = sessions;
                    exit = 0;
                    break;
                case "git-status" when parameters.Length is 2 or 4:
                    result = await GitWorkspace.StatusAsync(Option(parameters, "--workspace"), stop.Token,
                        parameters.Length == 4 ? Option(parameters, "--metadata-workspace") : null);
                    exit = 0;
                    break;
                case "git-init" when parameters.Length is 2 or 4:
                    result = await GitInitialization.InitializeAsync(Option(parameters, "--workspace"),
                        parameters.Length == 4 ? Option(parameters, "--initial-branch") : GitInitialization.DefaultBranch, stop.Token);
                    exit = 0;
                    break;
                case "git-diff" when parameters.Length is 4 or 6 or 8:
                    result = await GitWorkspace.DiffAsync(Option(parameters, "--workspace"), Option(parameters, "--path"),
                        parameters.Contains("--original") ? Option(parameters, "--original") : null, stop.Token,
                        parameters.Contains("--metadata-workspace") ? Option(parameters, "--metadata-workspace") : null);
                    exit = 0;
                    break;
                case "git-stage" or "git-unstage" when parameters.Length is 4 or 6 or 8:
                    result = await GitIndexEditor.ChangeAsync(Option(parameters, "--workspace"), Option(parameters, "--path"),
                        parameters.Contains("--original") ? Option(parameters, "--original") : null,
                        args[0] == "git-stage", stop.Token,
                        parameters.Contains("--metadata-workspace") ? Option(parameters, "--metadata-workspace") : null);
                    exit = 0;
                    break;
                case "git-stage-all" when parameters.Length is 2 or 4:
                    result = await GitIndexEditor.StageAllAsync(Option(parameters, "--workspace"), stop.Token,
                        parameters.Length == 4 ? Option(parameters, "--metadata-workspace") : null);
                    exit = 0;
                    break;
                case "git-commit-preview" when parameters.Length is 2 or 4:
                    result = await GitCommit.PreviewAsync(Option(parameters, "--workspace"), stop.Token,
                        parameters.Length == 4 ? Option(parameters, "--metadata-workspace") : null);
                    exit = 0;
                    break;
                case "git-commit" when parameters.Length is 8 or 10:
                    var commitWorkspace = Option(parameters, "--workspace");
                    var message = Option(parameters, "--message");
                    var author = Option(parameters, "--name");
                    var email = Option(parameters, "--email");
                    var metadata = parameters.Length == 10 ? Option(parameters, "--metadata-workspace") : null;
                    var preview = await GitCommit.PreviewAsync(commitWorkspace, stop.Token, metadata);
                    result = await GitCommit.CommitAsync(commitWorkspace, preview, message, author, email, stop.Token, metadata);
                    exit = 0;
                    break;
                case "git-worktree-list" when parameters.Length is 2 or 4:
                    result = new { worktrees = await GitWorktrees.ListAsync(Option(parameters, "--workspace"), stop.Token,
                        parameters.Length == 4 ? Option(parameters, "--metadata-workspace") : null) };
                    exit = 0;
                    break;
                case "git-worktree-create" when parameters.Length is 4 or 6:
                    result = await GitWorktrees.CreateAsync(Option(parameters, "--workspace"),
                        Option(parameters, "--worktree-id"), stop.Token,
                        parameters.Length == 6 ? Option(parameters, "--metadata-workspace") : null);
                    exit = 0;
                    break;
                case "git-worktree-remove" when parameters.Length is 4 or 6:
                    result = await GitWorktrees.RemoveAsync(Option(parameters, "--workspace"),
                        Option(parameters, "--worktree-id"), stop.Token,
                        parameters.Length == 6 ? Option(parameters, "--metadata-workspace") : null);
                    exit = 0;
                    break;
                case "inspect-run" when parameters.Length is 4 or 6:
                    var inspectWorkspace = Path.GetFullPath(Option(parameters, "--workspace"));
                    var runId = Option(parameters, "--run-id");
                    switch (parameters.Length == 4 ? "csharp" : Option(parameters, "--source"))
                    {
                        case "csharp":
                            var inspection = RunInspection.Check(inspectWorkspace, runId);
                            result = inspection;
                            exit = inspection.Status == "finished" ? 0 : 1;
                            break;
                        case "python":
                            var legacyRun = RunHistory.InspectLegacy(inspectWorkspace, runId,
                                Environment.GetEnvironmentVariable("SCIDEV_API_KEY"), stop.Token);
                            result = legacyRun;
                            exit = legacyRun.Status == "legacy_session" ? 0 : 1;
                            break;
                        default: throw new ArgumentException("历史来源应为 csharp 或 python。");
                    }
                    break;
                case "restore-run" when parameters.Length == 4:
                    var restored = WorkspaceCheckpoint.RestoreInactive(
                        Option(parameters, "--workspace"), Option(parameters, "--run-id"), stop.Token);
                    result = restored;
                    exit = restored.Status is "restored" or "no_checkpoint" ? 0 : 1;
                    break;
                default:
                    throw new ArgumentException("诊断参数无效，请运行 --help。");
            }
            Console.WriteLine(JsonSerializer.Serialize(result, json));
            return exit;
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested)
        {
            Console.WriteLine(JsonSerializer.Serialize(new { status = "cancelled" }, json));
            return 130;
        }
        catch (ToolDeniedException ex)
        {
            Console.WriteLine(JsonSerializer.Serialize(new { status = ex.Status }, json));
            return 1;
        }
        catch (Exception ex) when (ex is ArgumentException or IOException or InvalidDataException or InvalidOperationException
            or UnauthorizedAccessException or ExternalException or TimeoutException or OperationCanceledException or SqliteException
            or JsonException or KeyNotFoundException)
        {
            // Diagnostics are limited to fixed scripts; no command output, request bodies or environment values are serialized.
            Console.WriteLine(JsonSerializer.Serialize(new { status = "diagnostic_failed", error = ex.GetType().Name }, json));
            return 1;
        }
        finally { Console.CancelKeyPress -= cancel; }
    }

    private static string Option(string[] args, string flag)
    {
        for (var i = 0; i + 1 < args.Length; i += 2)
            if (args[i] == flag && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
                return args[i + 1];
        throw new ArgumentException($"缺少 {flag}。");
    }
}
