using System.Diagnostics;
using System.Text;
using System.Text.RegularExpressions;
using SciDev.Core;

namespace SciDev.Infrastructure;

// A fixed operator-invoked probe. It is intentionally not a model tool.
public static partial class DockerProbe
{
    private const string Engine = "npipe:////./pipe/dockerDesktopLinuxEngine";
    private const string Script = """
        set -eu
        test "$(cat /workspace/inside.txt)" = scidev-probe && echo workspace-readable
        echo marker > /tmp/scidev-marker && echo temporary-writable
        if touch /workspace/write-attempt 2>/dev/null; then exit 33; fi
        echo workspace-read-only
        test ! -e /sys/class/net/eth0
        test -z "$(ip route show default)"
        echo network-isolated
        test -z "${SCIDEV_API_KEY:-}" && echo model-key-not-in-container
        mkdir /tmp/project
        cp /workspace/inside.txt /tmp/project/inside.txt
        cd /tmp/project
        printf 'build-cache\n' > cache.txt
        test "$(cat inside.txt)" = scidev-probe && test "$(cat cache.txt)" = build-cache
        echo project-copy-passed
        """;

    public sealed record Result(bool Available, string Status, bool Readable = false, bool ReadOnly = false,
        bool IsolatedNetwork = false, bool WritableScratch = false, bool ProjectCopyPassed = false, bool CancelledContainerRemoved = false,
        string? Detail = null);

    public static async Task<Result> CheckAsync(string workspace, string image, CancellationToken token)
    {
        _ = new WindowsWorkspace(workspace);
        ValidateImage(image);
        var docker = DockerExecutable();
        if (!File.Exists(docker))
            return Failed("docker_cli_missing");
        if (workspace.Contains(',') || workspace.Contains('"'))
            throw new ArgumentException("当前沙箱探针不支持含逗号或引号的目录。");
        var root = Path.GetFullPath(workspace);
        var name = $"scidev-probe-{Guid.NewGuid():N}";
        var basePath = Directory.CreateDirectory(Path.Combine(root, name)).FullName;
        var config = Directory.CreateDirectory(Path.Combine(basePath, "docker-config")).FullName;
        var fixture = Directory.CreateDirectory(Path.Combine(basePath, "fixture")).FullName;
        var inside = Path.Combine(fixture, "inside.txt");
        var marker = Path.Combine(fixture, "write-attempt");
        try
        {
            await File.WriteAllTextAsync(inside, "scidev-probe", token);
            var info = await StartAsync(docker, config, ["info", "--format", "{{.OSType}}"], TimeSpan.FromSeconds(8), token);
            if (info.ExitCode != 0 || info.Output.Trim() != "linux")
                return Failed(FailureStatus(info.Error, "docker_linux_unavailable"));

            var resolution = await InspectImageAsync(docker, config, image, token);
            if (resolution.Id is not { } id)
                return Failed(resolution.Status);

            var result = await RunContainerAsync(docker, config, id, fixture, name, Script, token);
            if (result.ExitCode == 125)
                return Failed(FailureStatus(result.Error, "sandbox_probe_failed"));
            var lines = result.Output.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries).ToHashSet(StringComparer.Ordinal);
            var readable = lines.Contains("workspace-readable");
            var readOnly = lines.Contains("workspace-read-only") && !File.Exists(marker);
            var offline = lines.Contains("network-isolated");
            var scratch = lines.Contains("temporary-writable");
            var projectCopy = lines.Contains("project-copy-passed") && !File.Exists(Path.Combine(fixture, "cache.txt"));
            var isolated = result.ExitCode == 0 && readable && readOnly && offline && scratch && projectCopy
                && lines.Contains("model-key-not-in-container");
            var removed = isolated && await CheckCancellationAsync(docker, config, id, fixture, name + "-cancel", token);
            return new(removed, removed ? "isolated_probe_passed" : "sandbox_probe_failed",
                readable, readOnly, offline, scratch, projectCopy, removed, removed ? null : Detail("sandbox_probe_failed"));
        }
        catch (TimeoutException)
        {
            return Failed("docker_timeout");
        }
        finally
        {
            File.Delete(marker);
            File.Delete(inside);
            Directory.Delete(fixture);
            Directory.Delete(config);
            Directory.Delete(basePath);
        }
    }

    private static async Task<bool> CheckCancellationAsync(string docker, string config, string id,
        string fixture, string name, CancellationToken token)
    {
        const string signal = "cancellation-probe-launched";
        using var cancel = CancellationTokenSource.CreateLinkedTokenSource(token);
        var started = false;
        try
        {
            await RunContainerAsync(docker, config, id, fixture, name, $"echo {signal}; sleep 25 & wait", cancel.Token,
                output =>
                {
                    if (!started && output.Contains(signal, StringComparison.Ordinal))
                    {
                        started = true;
                        cancel.Cancel();
                    }
                });
            return false;
        }
        catch (OperationCanceledException) when (started && !token.IsCancellationRequested)
        {
            return true; // RunContainerAsync confirmed removal before rethrowing cancellation.
        }
    }

    internal static string DockerExecutable() => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
        "Docker", "Docker", "resources", "bin", "docker.exe");

    public static bool IsInstalled => File.Exists(DockerExecutable());

    internal static void ValidateImage(string image)
    {
        if (!LocalImage().IsMatch(image))
            throw new ArgumentException("镜像必须是已在本机的标签或 sha256 镜像 ID。");
    }

    internal static async Task<string> RequireLocalImageAsync(string docker, string config, string image, CancellationToken token)
    {
        var resolution = await InspectImageAsync(docker, config, image, token);
        return resolution.Id ?? throw Unavailable(resolution.Status);
    }

    private static async Task<(string? Id, string Status)> InspectImageAsync(string docker, string config, string image, CancellationToken token)
    {
        ValidateImage(image);
        var local = await StartAsync(docker, config, ["image", "inspect", "--format", "{{.Id}}", image],
            TimeSpan.FromSeconds(8), token);
        var id = local.Output.Trim();
        return local.ExitCode == 0 && ImageId().IsMatch(id) ? (id, "image_available") :
            (null, FailureStatus(local.Error, "docker_image_probe_failed"));
    }

    internal static string FailureStatus(string error, string fallback)
    {
        const StringComparison comparison = StringComparison.OrdinalIgnoreCase;
        foreach (var line in error.Split('\n', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries))
        {
            if (line.Contains("dockerDesktopLinuxEngine", comparison))
            {
                if (line.Contains("permission denied", comparison) || line.Contains("access is denied", comparison) ||
                    line.Contains("access denied", comparison))
                    return "docker_engine_access_denied";
                if (line.Contains("error during connect", comparison) || line.Contains("cannot connect", comparison) ||
                    line.Contains("system cannot find the file", comparison))
                    return "docker_linux_unavailable";
            }
            if (line.StartsWith("Error response from daemon: No such image:", comparison))
                return "image_not_local";
        }
        return fallback;
    }

    internal static ToolDeniedException Unavailable(string status) => new("sandbox_unavailable", Detail(status));

    private static Result Failed(string status) => new(false, status, Detail: Detail(status));

    private static string Detail(string status) => status switch
    {
        "docker_cli_missing" => "找不到 Docker Desktop 客户端。",
        "docker_engine_access_denied" => "当前进程无权访问 Docker Desktop 的 Linux 管道。请核对启动该程序的用户和运行环境是否有 Docker 访问权限。",
        "docker_linux_unavailable" => "Docker Desktop 的 Linux 引擎不可达。请确认 Docker Desktop 正在运行并使用 Linux 容器。",
        "image_not_local" => "所选镜像不在本机。请选择已缓存的镜像。",
        "docker_image_probe_failed" => "无法确认所选镜像。请在相同运行环境中用 sandbox-check 检查 Docker 和本机镜像。",
        "docker_timeout" => "Docker 本机探测在时限内未完成。",
        _ => "Docker 隔离容器未能完成验证。",
    };

    internal static async Task<(int ExitCode, string Output, string Error)> RunContainerAsync(
        string docker, string config, string id, string fixture, string name, string script, CancellationToken token,
        Action<string>? observe = null, IReadOnlyList<string>? argv = null, int commandTimeoutSeconds = 90, int stdoutBeginning = 18_000)
    {
        if (fixture.Contains(',') || fixture.Contains('"'))
            throw new ArgumentException("沙箱工作区路径不能含逗号或引号。");
        var projectCommand = argv is not null;
        // Git may check the embedded shell script out with Windows line endings.
        var shellScript = script.Replace("\r\n", "\n", StringComparison.Ordinal);
        try
        {
            return await StartAsync(docker, config,
                ["run", "--pull", "never", "--rm", "--name", name, "--network", "none", "--read-only",
                 "--init", "--no-healthcheck", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--pids-limit", projectCommand ? "128" : "32", "--memory", projectCommand ? "512m" : "128m",
                 "--cpus", "1", "--tmpfs", projectCommand ? "/tmp:rw,nosuid,nodev,size=128m" : "/tmp:rw,noexec,nosuid,nodev,size=16m",
                 "--mount", $"type=bind,source={fixture},target=/workspace,readonly,bind-recursive=disabled",
                 "--entrypoint", "/bin/sh", id, "-c", shellScript, "scidevharness", ..(argv ?? [])],
                TimeSpan.FromSeconds(projectCommand ? commandTimeoutSeconds : 15), token, observe, stdoutBeginning);
        }
        catch (Exception failure) when (failure is OperationCanceledException or TimeoutException)
        {
            // Closing the attached CLI does not stop the daemon-side container.
            // This name is private to the current probe. Never remove other containers.
            await StartAsync(docker, config, ["container", "rm", "--force", name],
                TimeSpan.FromSeconds(8), CancellationToken.None);
            var found = await FindContainerAsync(docker, config, name);
            if (found.ExitCode != 0 || !string.IsNullOrWhiteSpace(found.Output))
                throw new IOException("无法核实沙箱容器已经清理。", failure);
            throw;
        }
    }

    private static Task<(int ExitCode, string Output, string Error)> FindContainerAsync(string docker, string config, string name) =>
        StartAsync(docker, config, ["container", "ls", "--all", "--quiet", "--filter", $"name=^/{name}$"],
            TimeSpan.FromSeconds(8), CancellationToken.None);

    private static async Task<(int ExitCode, string Output, string Error)> StartAsync(
        string docker, string config, IReadOnlyList<string> arguments, TimeSpan timeout, CancellationToken token,
        Action<string>? observe = null, int stdoutBeginning = 18_000)
    {
        var start = new ProcessStartInfo(docker)
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
        };
        start.ArgumentList.Add("--host");
        start.ArgumentList.Add(Engine);
        start.ArgumentList.Add("--config");
        start.ArgumentList.Add(config);
        foreach (var argument in arguments)
            start.ArgumentList.Add(argument);
        start.Environment.Clear();
        foreach (var key in new[] { "SystemRoot", "WINDIR", "TEMP", "TMP" })
            if (Environment.GetEnvironmentVariable(key) is { } value)
                start.Environment[key] = value;

        token.ThrowIfCancellationRequested();
        using var process = Process.Start(start) ?? throw new IOException("Docker CLI 无法启动。");
        var output = ReadBoundedAsync(process.StandardOutput, observe, stdoutBeginning);
        var errors = ReadBoundedAsync(process.StandardError);
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(token);
        budget.CancelAfter(timeout);
        try
        {
            await process.WaitForExitAsync(budget.Token);
            return (process.ExitCode, await output, await errors);
        }
        catch (OperationCanceledException)
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync(CancellationToken.None);
            }
            if (token.IsCancellationRequested)
                throw;
            throw new TimeoutException("Docker probe exceeded its time limit.");
        }
    }

    private static async Task<string> ReadBoundedAsync(StreamReader reader, Action<string>? observe = null, int beginning = 18_000)
    {
        const int ending = 7_000;
        var result = new StringBuilder();
        var tail = new StringBuilder();
        var truncated = false;
        var buffer = new char[1024];
        int count;
        while ((count = await reader.ReadAsync(buffer)) > 0)
        {
            var accepted = Math.Min(count, beginning - result.Length);
            if (accepted > 0)
            {
                result.Append(buffer, 0, accepted);
                observe?.Invoke(result.ToString());
            }
            if (accepted < count)
            {
                tail.Append(buffer, accepted, count - accepted);
                if (tail.Length > ending)
                {
                    tail.Remove(0, tail.Length - ending);
                    truncated = true;
                }
            }
        }
        if (truncated)
            result.Append("\n…\n");
        return result.Append(tail).ToString();
    }

    [GeneratedRegex("^[a-zA-Z0-9][a-zA-Z0-9./:_-]{0,191}$", RegexOptions.CultureInvariant)]
    private static partial Regex LocalImage();
    [GeneratedRegex("^sha256:[a-f0-9]{64}$", RegexOptions.CultureInvariant)]
    private static partial Regex ImageId();
}
