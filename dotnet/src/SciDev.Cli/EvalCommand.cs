using System.Globalization;
using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Cli;

internal static class EvalCommand
{
    public static async Task<int> RunAsync(string[] args, JsonSerializerOptions json)
    {
        var asJson = args.Contains("--json");
        var key = Environment.GetEnvironmentVariable("SCIDEV_API_KEY")?.Trim();
        using var stop = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; stop.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            if (!OperatingSystem.IsWindows())
                throw new ArgumentException("当前评测入口只支持 Windows。");
            var options = Parse(args);
            var root = Path.GetFullPath(options.GetValueOrDefault("--workspace") ?? Directory.GetCurrentDirectory());
            var suite = Path.GetFullPath(options.GetValueOrDefault("--suite") ?? throw new ArgumentException("请指定 --suite。"));
            var image = options.GetValueOrDefault("--sandbox-image") ?? Environment.GetEnvironmentVariable("SCIDEV_SANDBOX_IMAGE");
            if (string.IsNullOrWhiteSpace(image))
                throw new ArgumentException("请用 --sandbox-image 指定本机工具链镜像。");
            var imageName = image.Trim();
            var output = options.GetValueOrDefault("--output") ?? $".research/evals/eval-{DateTimeOffset.UtcNow:yyyyMMddTHHmmssZ}-{Guid.NewGuid():N}.json";
            if (string.Equals(Path.GetFullPath(output, root), suite, StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("评测报告必须与套件文件分开保存。");
            WorkspaceResultFile.ValidatePath(root, output);
            var endpoint = WorkspaceRunner.EndpointFor(options.GetValueOrDefault("--api-base") ?? Environment.GetEnvironmentVariable("SCIDEV_API_BASE"));
            var model = options.GetValueOrDefault("--model") ?? Environment.GetEnvironmentVariable("SCIDEV_MODEL") ?? "";
            var jobs = Number(options, "--jobs", 1);
            var maxTokens = Number(options, "--max-tokens", 12_000);
            if (maxTokens is < 1 or > 32_768)
                throw new ArgumentException("--max-tokens 必须在 1 到 32768 之间。");
            var settings = new WorkspaceRunOptions(root, "evaluation", endpoint, model, MaxTokens: maxTokens, SandboxImage: imageName);
            WorkspaceRunner.ValidateOptions(settings, key, outsideReview: false, commandReview: false);
            var report = await EvaluationRunner.RunAsync(root, suite, settings, imageName, key, jobs, stop.Token);
            var serialized = WorkspaceResultFile.Publish(root, output, previous =>
            {
                if (PreviousRate(previous, report) is { } rate)
                    report = report with { PreviousPassRate = rate, Regression = Math.Round(report.PassRate - rate, 4) };
                return RunJson.SerializeRedacted(report, json, key);
            }, maxBytes: 8 * 1024 * 1024);
            if (asJson)
                Console.WriteLine(serialized);
            else
            {
                Console.WriteLine($"{Redact(report.Suite)} · {report.Passed}/{report.Total} 通过 · {report.TotalTokens} tokens");
                Console.WriteLine($"报告 {Redact(Path.GetFullPath(output, root))}");
                foreach (var item in report.Results.Where(result => !result.Success))
                    Console.WriteLine($"失败 {Redact(item.TaskId)} · {item.Error}");
            }
            return report.Failed == 0 ? 0 : 1;
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested) { return Failure("cancelled", "评测已停止。", 130); }
        catch (ArgumentException ex) { return Failure("invalid_input", ex.Message, 2); }
        catch (ToolDeniedException ex) { return Failure(ex.Status, ex.Message, 1); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or TimeoutException or System.ComponentModel.Win32Exception)
        { return Failure("evaluation_error", "无法完成评测或写入报告。", 1); }
        finally { Console.CancelKeyPress -= cancel; }

        string Redact(string text) => key is { Length: > 0 } ? text.Replace(key, "[redacted]", StringComparison.Ordinal) : text;

        int Failure(string status, string detail, int code)
        {
            if (asJson)
                Console.WriteLine(RunJson.SerializeRedacted(new { status, error = detail }, json, key));
            else
                Console.Error.WriteLine(Redact(detail));
            return code;
        }
    }

    private static Dictionary<string, string> Parse(string[] args)
    {
        var options = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var index = 1; index < args.Length; index++)
        {
            var flag = args[index];
            if (flag == "--json")
                continue;
            if (flag is not ("--workspace" or "--suite" or "--output" or "--jobs" or "--sandbox-image" or "--api-base" or "--model" or "--max-tokens") ||
                index + 1 == args.Length || args[index + 1].StartsWith("--", StringComparison.Ordinal) || !options.TryAdd(flag, args[++index]))
                throw new ArgumentException("评测参数未知、重复或缺值。");
        }
        return options;
    }

    private static int Number(Dictionary<string, string> options, string flag, int fallback) =>
        !options.TryGetValue(flag, out var value) ? fallback : int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var number)
            ? number : throw new ArgumentException($"{flag} 必须是整数。");

    private static decimal? PreviousRate(string? source, EvaluationRunner.Report current)
    {
        if (source is null)
            return null;
        try
        {
            using var file = JsonDocument.Parse(source);
            var report = file.RootElement;
            return report.ValueKind == JsonValueKind.Object && report.TryGetProperty("suite", out var old) && old.ValueKind == JsonValueKind.String &&
                old.GetString() == current.Suite && report.TryGetProperty("results", out var results) && results.ValueKind == JsonValueKind.Array &&
                results.EnumerateArray().Select(item => item.ValueKind == JsonValueKind.Object && item.TryGetProperty("task_id", out var id) &&
                    id.ValueKind == JsonValueKind.String ? id.GetString() : null).SequenceEqual(current.Results.Select(item => item.TaskId)) &&
                report.TryGetProperty("pass_rate", out var previous) && previous.ValueKind == JsonValueKind.Number &&
                previous.TryGetDecimal(out var rate) && rate is >= 0 and <= 1 ? rate : null;
        }
        catch (JsonException) { return null; }
    }
}
