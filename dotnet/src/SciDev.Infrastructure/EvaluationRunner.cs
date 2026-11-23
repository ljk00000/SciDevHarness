using System.Diagnostics;
using System.Globalization;
using SciDev.Core;

namespace SciDev.Infrastructure;

public static class EvaluationRunner
{
    public sealed record Prices(decimal InputPerMillion, decimal OutputPerMillion);
    public sealed record TaskResult(string TaskId, bool Success, double ElapsedSeconds, int TurnCount, long Tokens,
        long PromptTokens, long CompletionTokens, int? ExitCode, int? CheckExit, string Error, string Output, string CheckOutput);
    public sealed record Report(string Suite, DateTimeOffset StartedAt, int Jobs, int Total, int Passed, int Failed, decimal PassRate,
        decimal AverageTurns, decimal TotalTokens, decimal TotalPromptTokens, decimal TotalCompletionTokens, decimal AverageTokens,
        decimal EstimatedCostUsd, Prices Pricing, double ElapsedSeconds, TaskResult[] Results,
        decimal? PreviousPassRate = null, decimal? Regression = null);

    public static async Task<Report> RunAsync(string workspace, string suitePath, WorkspaceRunOptions model,
        string image, string? credential, int jobs, CancellationToken token)
    {
        var suite = EvaluationSuite.Load(suitePath, credential);
        var settings = Configure(model, suite);
        var prices = new Prices(ReadPrice("SCIDEV_EVAL_INPUT_PRICE", suite.Prices.InputPerMillion),
            ReadPrice("SCIDEV_EVAL_OUTPUT_PRICE", suite.Prices.OutputPerMillion));
        jobs = Math.Clamp(jobs, 1, 8);
        var root = Path.GetFullPath(workspace);
        DockerWorkspaceTools.Validate(root, image);
        var state = new WindowsWorkspace(root).EnsureStateDirectory();
        var name = $"scidev-eval-{Guid.NewGuid():N}";
        using var scratch = state.OpenPath(name);
        Directory.CreateDirectory(scratch.AbsolutePath);
        var started = DateTimeOffset.UtcNow;
        var timer = Stopwatch.StartNew();
        try
        {
            var docker = DockerProbe.DockerExecutable();
            if (!File.Exists(docker))
                throw DockerProbe.Unavailable("docker_cli_missing");
            var config = Directory.CreateDirectory(Path.Combine(scratch.AbsolutePath, "docker-config")).FullName;
            var imageId = await DockerProbe.RequireLocalImageAsync(docker, config, image, token);
            using var stage = new WindowsWorkspace(scratch.AbsolutePath).OpenPath("evaluation-stage");
            var results = new TaskResult[suite.Tasks.Length];
            await Parallel.ForEachAsync(Enumerable.Range(0, results.Length), new ParallelOptions { MaxDegreeOfParallelism = jobs, CancellationToken = token },
                async (index, taskToken) => results[index] = await RunTaskAsync(scratch.AbsolutePath, index, suite.Template,
                    suite.Tasks[index], settings with { SandboxImage = imageId }, credential, taskToken));

            var total = results.Length;
            var passed = results.Count(result => result.Success);
            var input = results.Sum(result => (decimal)result.PromptTokens);
            var output = results.Sum(result => (decimal)result.CompletionTokens);
            var tokens = results.Sum(result => (decimal)result.Tokens);
            var cost = (input * prices.InputPerMillion + output * prices.OutputPerMillion) / 1_000_000;
            return new(suite.Name, started, jobs, total, passed, total - passed, Math.Round((decimal)passed / total, 4),
                Math.Round(results.Sum(result => (decimal)result.TurnCount) / total, 2), tokens, input, output, Math.Round(tokens / total, 1),
                Math.Round(cost, 6), prices, Math.Round(timer.Elapsed.TotalSeconds, 3), results);
        }
        finally
        {
            var folder = new DirectoryInfo(scratch.AbsolutePath);
            if (folder.Name == name && folder.Parent?.FullName.Equals(Path.Combine(root, ".research"), StringComparison.OrdinalIgnoreCase) == true &&
                (folder.Attributes & FileAttributes.ReparsePoint) == 0)
                folder.Delete(recursive: true);
        }
    }

    private static WorkspaceRunOptions Configure(WorkspaceRunOptions model, EvaluationSuite suite)
    {
        var turns = model.MaxTurns;
        var deadline = model.RequestTimeoutSeconds;
        if (suite.Environment.TryGetValue("SCIDEV_MAX_AGENT_TURNS", out var budget) &&
            (!int.TryParse(budget, NumberStyles.Integer, CultureInfo.InvariantCulture, out turns) || turns is < 1 or > 16))
            throw new ArgumentException("评测模型轮次须在 1 到 16 之间。");
        if (suite.Environment.TryGetValue("SCIDEV_REQUEST_TIMEOUT_SECONDS", out var timeout) &&
            (!double.TryParse(timeout, NumberStyles.Float, CultureInfo.InvariantCulture, out deadline) || deadline is not (>= 5 and <= 600)))
            throw new ArgumentException("评测模型请求超时须在 5 到 600 秒之间。");
        var textFallback = suite.Environment.GetValueOrDefault("SCIDEV_TEXT_TOOL_CALL_FALLBACK")?.Trim().ToLowerInvariant() switch
        {
            "1" or "true" or "yes" or "on" => true,
            null or "" or "0" or "false" or "no" or "off" => false,
            _ => throw new ArgumentException("评测文本工具兼容选项须为 true 或 false。"),
        };
        return model with { MaxTurns = turns, RequestTimeoutSeconds = deadline, Mode = ExecutionMode.WorkspaceApproval,
            NetworkPolicy = AgentNetworkPolicy.Deny, RestoreOnFailure = false, AutoCommit = false, AllowedCommands = null,
            TextToolCallFallback = textFallback };
    }

    private static async Task<TaskResult> RunTaskAsync(string scratch, int index, string? template, EvaluationSuite.TaskSpec task,
        WorkspaceRunOptions settings, string? credential, CancellationToken token)
    {
        var timer = Stopwatch.StartNew();
        RunResult? outcome = null;
        int? checkExit = null;
        var checkOutput = "";
        var error = "";
        var passed = false;
        try
        {
            var workspace = Directory.CreateDirectory(Path.Combine(scratch, $"task-{index + 1}")).FullName;
            if (template is not null)
                _ = new WorkspaceSnapshot(template, credential).Capture(workspace, token);
            foreach (var (path, text) in task.Setup)
            {
                token.ThrowIfCancellationRequested();
                var destination = Path.Combine(workspace, path);
                Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
                File.WriteAllText(destination, text, WorkspaceFiles.Utf8);
            }
            var patch = new EvaluationPatchApproval(task);
            using (var budget = CancellationTokenSource.CreateLinkedTokenSource(token))
            {
                budget.CancelAfter(TimeSpan.FromSeconds(task.Timeout));
                try
                {
                    var result = await WorkspaceRunner.RunAsync(Guid.NewGuid().ToString("N"), settings with
                    {
                        Workspace = workspace, Task = task.Prompt, RequireVerification = task.Verify,
                    }, credential, budget.Token, patchReview: _ => patch);
                    outcome = result.Result;
                    if (outcome.Status == "cancelled" && budget.IsCancellationRequested && !token.IsCancellationRequested)
                        error = "evaluation_timeout";
                    else if (!outcome.Success)
                        error = outcome.Status;
                }
                catch (OperationCanceledException) when (budget.IsCancellationRequested && !token.IsCancellationRequested)
                { error = "evaluation_timeout"; }
            }

            token.ThrowIfCancellationRequested();
            var sandbox = new DockerWorkspaceTools(workspace, new WorkspaceFiles(workspace), settings.SandboxImage!, credential);
            var check = await sandbox.RunEvaluationCheckAsync(task.Id, task.Check, Math.Min(task.Timeout, 120), token);
            checkExit = check.CommandExitCode;
            checkOutput = check.Text;
            var testIssue = check.Verification?.TestIssue;
            if (error.Length == 0 && (checkExit != 0 || testIssue is not null))
                error = testIssue ?? "check_failed";
            passed = outcome?.Success == true && checkExit == 0 && testIssue is null;
        }
        catch (ToolDeniedException ex) { error = ex.Status; }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException)
        { error = "evaluation_workspace_error"; }
        var usage = outcome?.Usage;
        var code = outcome is null ? (int?)null : outcome.Success ? 0 : 1;
        return new(task.Id, passed, Math.Round(timer.Elapsed.TotalSeconds, 3), outcome?.TurnCount ?? 0,
            usage?.TotalTokens ?? 0, usage?.PromptTokens ?? 0, usage?.CompletionTokens ?? 0, code, checkExit, error,
            ReportText(outcome?.Text ?? "", credential), ReportText(checkOutput, credential));
    }

    private static string ReportText(string text, string? credential)
    {
        text = WorkspaceFiles.RedactCredential(text, credential);
        return text.Length <= 8_000 ? text : text[^8_000..];
    }

    private static decimal ReadPrice(string name, decimal fallback)
    {
        if (Environment.GetEnvironmentVariable(name) is not { Length: > 0 } configured)
            return fallback;
        return decimal.TryParse(configured, NumberStyles.Float, CultureInfo.InvariantCulture, out var price) && price is >= 0 and <= 1_000_000
            ? price : 0;
    }


    private sealed class EvaluationPatchApproval(EvaluationSuite.TaskSpec task) : IFileEditApproval
    {
        public ValueTask<bool> ApproveAsync(FileEditPreview preview, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            return new(task.CanEdit(preview.Path.Replace('\\', '/')) &&
                (preview.Destination is null || task.CanEdit(preview.Destination.Replace('\\', '/'))));
        }
    }
}
