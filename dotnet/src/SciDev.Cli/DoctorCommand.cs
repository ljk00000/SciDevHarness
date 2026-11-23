using System.Runtime.InteropServices;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Cli;

internal static class DoctorCommand
{
    internal const string Usage = "doctor [--workspace <directory>] [--tests] [--online] [--json]";
    private sealed record Check(string Name, string Status, string Detail, string Fix = "");
    private sealed record Provider(Uri Endpoint, string Model, string? Key, bool Saved);

    internal static async Task<int> RunAsync(string[] args, JsonSerializerOptions json)
    {
        if (args is ["doctor", "--help" or "-h"])
        {
            Console.WriteLine(Usage);
            return 0;
        }
        var workspace = "";
        WorkspaceDoctor doctor;
        HashSet<string> flags;
        try
        {
            (workspace, flags) = Parse(args);
            doctor = new(workspace);
        }
        catch (Exception error) when (error is ArgumentException or ToolDeniedException or IOException or
            UnauthorizedAccessException or PlatformNotSupportedException)
        {
            Console.Error.WriteLine(Usage);
            Console.WriteLine(JsonSerializer.Serialize(new { ok = false, status = "invalid_workspace_or_arguments" }, json));
            return 2;
        }
        using var stop = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; stop.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            var runtime = RuntimeInformation.FrameworkDescription;
            var checks = new List<Check> { new("runtime", "ok", $"{runtime} · {RuntimeInformation.ProcessArchitecture}") };
            var provider = ReadProvider(workspace, checks);
            AddGitCheck(doctor, checks);
            var docker = DockerProbe.IsInstalled;
            AddSandboxCheck(workspace, docker, checks);
            var tools = doctor.FindLanguageTools();
            checks.Add(tools.Length > 0 ? new("lsp", "ok", string.Join(", ", tools)) :
                new("lsp", "warn", "未找到 Ruff、Pyright 或 TypeScript Language Server。"));

            VerificationPlan? tests = null;
            if (flags.Contains("--tests"))
            {
                try
                {
                    tests = doctor.ReadVerificationPlan(stop.Token);
                    var detail = tests.Candidates.Count > 0 ? $"发现 {tests.Candidates.Count} 个静态候选。" : "未发现静态测试或检查候选。";
                    if (!tests.Complete)
                        detail += "已达到扫描上限。";
                    checks.Add(new("tests", tests.Candidates.Count > 0 && tests.Complete ? "ok" : "warn", detail));
                }
                catch (Exception error) when (LocalFailure(error))
                {
                    checks.Add(new("tests", "warn", "无法读取测试声明，请检查工作区文件和权限。"));
                }
            }

            ModelCapabilityProbe.Result? probe = null;
            if (flags.Contains("--online"))
                probe = await ProbeAsync(provider, checks, stop.Token);
            var ok = checks.All(item => item.Status != "error");
            Console.WriteLine(RunJson.SerializeRedacted(new
            {
                ok, version = PreviewUpdates.CurrentVersion, workspace, checks, runtime,
                providerKeyConfigured = !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("SCIDEV_API_KEY")) ||
                    provider?.Key is { Length: > 0 },
                commandToolEnabled = false, dockerClientInstalled = docker, tests, probe,
            }, json, provider?.Key ?? Environment.GetEnvironmentVariable("SCIDEV_API_KEY")));
            return ok ? 0 : 1;
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested)
        {
            Console.WriteLine(JsonSerializer.Serialize(new { status = "cancelled" }, json));
            return 130;
        }
        finally { Console.CancelKeyPress -= cancel; }
    }

    private static (string Workspace, HashSet<string> Flags) Parse(string[] args)
    {
        var flags = new HashSet<string>(StringComparer.Ordinal);
        var workspace = ".";
        for (var i = 1; i < args.Length; i++)
        {
            var flag = args[i];
            if (!flags.Add(flag))
                throw new ArgumentException();
            if (flag == "--workspace" && i + 1 < args.Length && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
                workspace = args[++i];
            else if (flag is not ("--json" or "--tests" or "--online"))
                throw new ArgumentException();
        }
        return (Path.GetFullPath(workspace), flags);
    }

    private static Provider? ReadProvider(string workspace, List<Check> checks)
    {
        var address = Environment.GetEnvironmentVariable("SCIDEV_API_BASE")?.Trim();
        var model = Environment.GetEnvironmentVariable("SCIDEV_MODEL")?.Trim();
        var credential = Environment.GetEnvironmentVariable("SCIDEV_API_KEY")?.Trim();
        var saved = false;
        try
        {
            if (string.IsNullOrEmpty(address))
            {
                var config = SavedProvider.Load(workspace);
                (address, model, saved) = (config.Address, config.Model, true);
                // Never use an environment key at a destination controlled by project data.
                credential = null;
                try { credential = ProviderCredential.Read(config.Profile)?.Trim(); }
                catch (ToolDeniedException) { }
            }
            var endpoint = WorkspaceRunner.EndpointFor(address);
            if (string.IsNullOrWhiteSpace(model) || model.Length > 256 || model.Any(char.IsControl) ||
                credential?.Any(char.IsControl) == true)
                throw new ArgumentException();
            var ready = endpoint.IsLoopback || !string.IsNullOrEmpty(credential);
            checks.Add(new("provider", ready ? "ok" : "warn",
                $"{(saved ? "项目配置" : "环境配置")} · {endpoint.GetLeftPart(UriPartial.Authority)} · {(ready ? "可用于探测" : "缺少凭据")}",
                ready ? "" : "为选定端点配置模型凭据。"));
            return new(endpoint, model, credential, saved);
        }
        catch (ToolDeniedException error) when (error.Status == "provider_not_found")
        {
            checks.Add(new("provider", "warn", "未配置模型地址和模型 ID。"));
        }
        catch (Exception error) when (LocalFailure(error))
        {
            checks.Add(new("provider", "error", "模型配置格式无效。", "检查模型地址、模型 ID 和项目 provider.json。"));
        }
        return null;
    }

    private static void AddGitCheck(WorkspaceDoctor doctor, List<Check> checks)
    {
        try
        {
            var git = doctor.ReadGit();
            var check = git.Status switch
            {
                "ready" => new Check("git", "ok",
                    $"branch={git.Branch ?? "(detached)"}; head={(git.Head is { } head ? head[..12] : "(no commits)") }"),
                "missing" => new Check("git", "warn", "未找到 Git for Windows。"),
                "linked" => new Check("git", "warn", "当前目录是链接工作树。诊断没有读取主仓库。"),
                "not_repository" => new Check("git", "warn", "当前目录没有本地 Git 仓库。"),
                _ => new Check("git", "warn", "本地 Git HEAD 不可用。"),
            };
            checks.Add(check);
        }
        catch (Exception error) when (LocalFailure(error))
        {
            checks.Add(new("git", "warn", "无法安全读取本地 Git 元数据。"));
        }
    }

    private static void AddSandboxCheck(string workspace, bool docker, List<Check> checks)
    {
        try
        {
            var saved = LegacyExecutionPolicy.Read(workspace);
            var backend = Environment.GetEnvironmentVariable("SCIDEV_SANDBOX_BACKEND")?.Trim().ToLowerInvariant() ?? saved?.Backend;
            if (backend == "docker")
                checks.Add(new("sandbox", "warn", docker ? "发现 Docker 客户端。引擎、镜像与隔离待检查。" : "未找到 Docker 客户端。",
                    "用 sandbox-check 指定本机镜像验证实际隔离。"));
            else if (backend is null or "" or "local" or "none" or "off")
                checks.Add(new("sandbox", "ok", "工作区文件操作可直接使用。可配置镜像启用隔离命令。"));
            else
                checks.Add(new("sandbox", "error", "沙箱后端配置无效。"));
        }
        catch (Exception error) when (LocalFailure(error))
        {
            checks.Add(new("sandbox", "error", "无法核对工作区沙箱配置。"));
        }
    }

    private static async Task<ModelCapabilityProbe.Result?> ProbeAsync(Provider? provider, List<Check> checks, CancellationToken token)
    {
        if (provider is null || (!provider.Endpoint.IsLoopback && string.IsNullOrEmpty(provider.Key)))
        {
            checks.Add(new("provider-probe", "warn", "模型探测需要已配置的端点和相应凭据。"));
            return null;
        }
        if (provider.Saved)
        {
            if (Console.IsInputRedirected || Console.IsErrorRedirected)
            {
                checks.Add(new("provider-probe", "warn", "使用项目配置联网需要终端批准。"));
                return null;
            }
            ConsoleConfirmation.DiscardPendingInput(token);
            Console.Error.WriteLine("检查模型能力。会查询模型目录并发送一条固定测试消息。");
            var destination = string.IsNullOrEmpty(provider.Key) ? provider.Endpoint.AbsoluteUri :
                provider.Endpoint.AbsoluteUri.Replace(provider.Key, "[redacted]", StringComparison.Ordinal);
            Console.Error.WriteLine($"目的地 {destination}");
            if (!await ConsoleConfirmation.ReadAsync(token))
            {
                checks.Add(new("provider-probe", "warn", "本次探测未获批准。"));
                return null;
            }
        }
        var result = await ModelCapabilityProbe.CheckAsync(provider.Endpoint, provider.Model, provider.Key, token);
        var context = result.ContextLength?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "unknown";
        var detail = $"tools={(result.ToolsSupported ? "yes" : "no")}; context={context}; vision={(result.Vision == true ? "yes" : "unknown")}";
        checks.Add(new("provider-probe", result.ToolsSupported ? "ok" : "warn", $"{detail} {result.Detail}".TrimEnd()));
        return result;
    }

    private static bool LocalFailure(Exception error) => error is ArgumentException or ToolDeniedException or IOException or
        UnauthorizedAccessException or JsonException or InvalidDataException;
}
