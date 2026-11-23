using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Unicode;
using SciDev.Core;

namespace SciDev.Cli;

internal sealed class ConsoleOperationReview : IOperationReview
{
    private static readonly JsonSerializerOptions Preview = new() { Encoder = JavaScriptEncoder.Create(UnicodeRanges.All) };

    public ValueTask<bool> ApproveAsync(OperationReview request, CancellationToken token)
    {
        ConsoleConfirmation.DiscardPendingInput(token);
        var title = request.Kind switch
        {
            OperationKind.RunSandboxCommand => "容器命令审批",
            OperationKind.FetchExternalText or OperationKind.ShareNetworkText => "额外联网审批",
            OperationKind.RunGitDiff => "宿主 Git 查询审批",
            OperationKind.ShareGitDiff => "Git 差异发送审批",
            OperationKind.PrepareGitCommit => "Git 提交检查审批",
            OperationKind.PublishGitCommit => "任务提交审批",
            OperationKind.InspectHostCommand => "宿主程序检查审批",
            OperationKind.RunHostCommand => "宿主命令审批",
            OperationKind.ShareHostCommandOutput => "宿主命令输出审批",
            OperationKind.RequestMcpHttp => "MCP 联网审批",
            OperationKind.ShareMcpCatalog => "MCP 工具定义发送审批",
            OperationKind.CallMcpTool => "MCP 工具调用审批",
            OperationKind.ShareMcpResult => "MCP 结果发送审批",
            OperationKind.ReadGitHubPullRequest => "GitHub PR 查询审批",
            OperationKind.SubmitGitHubReview => "GitHub PR 审阅发布审批",
            OperationKind.PushGitHubBranch => "GitHub 分支推送审批",
            OperationKind.CreateGitHubPullRequest => "GitHub PR 创建审批",
            OperationKind.CheckForUpdate => "检查更新审批",
            OperationKind.DownloadUpdate => "下载更新审批",
            OperationKind.LaunchUpdate => "启动安装器审批",
            _ => "区外审批",
        };
        Console.Error.WriteLine($"{title}  {JsonSerializer.Serialize(request.Target, Preview)}");
        Console.Error.WriteLine(request.Detail);
        if (request.Destination is not null)
            Console.Error.WriteLine($"发送至 {JsonSerializer.Serialize(request.Destination, Preview)}");
        if (request.Before is not null)
        {
            if (request.Kind is OperationKind.RunSandboxCommand or OperationKind.RunHostCommand)
            {
                Console.Error.WriteLine("命令参数");
                Console.Error.WriteLine(request.Before);
            }
            else if (request.Kind is OperationKind.PublishGitCommit or OperationKind.ShareHostCommandOutput or
                OperationKind.RequestMcpHttp or OperationKind.ShareMcpCatalog or OperationKind.CallMcpTool or OperationKind.ShareMcpResult or
                OperationKind.SubmitGitHubReview or OperationKind.PushGitHubBranch or OperationKind.CreateGitHubPullRequest)
            {
                Console.Error.WriteLine(request.Kind == OperationKind.PublishGitCommit ? "本轮差异" : "发送预览");
                using var lines = new StringReader(request.Before);
                while (lines.ReadLine() is { } line)
                    WritePreview(line);
            }
            else
            {
                Console.Error.WriteLine(request.Kind switch
                {
                    OperationKind.EditExternalText => "替换前",
                    OperationKind.DownloadUpdate or OperationKind.LaunchUpdate => "SHA-256",
                    _ => "发送预览",
                });
                WritePreview(request.Before);
            }
        }
        if (request.After is not null)
        {
            Console.Error.WriteLine("替换后");
            WritePreview(request.After);
        }
        return ConsoleConfirmation.ReadAsync(token);
    }

    private static void WritePreview(string value) =>
        Console.Error.WriteLine($"  {JsonSerializer.Serialize(value, Preview)}");
}

internal static class ConsoleConfirmation
{
    public static void DiscardPendingInput(CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        while (Console.KeyAvailable)
        {
            token.ThrowIfCancellationRequested();
            _ = Console.ReadKey(intercept: true);
        }
    }

    public static async ValueTask<bool> ReadAsync(CancellationToken token)
    {
        Console.Error.Write("仅批准这一次？[y/N] ");
        try
        {
            while (!Console.KeyAvailable)
                await Task.Delay(50, token);
            token.ThrowIfCancellationRequested();
            return Console.ReadKey(intercept: true).KeyChar is 'y' or 'Y';
        }
        finally { Console.Error.WriteLine(); }
    }
}
