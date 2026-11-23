using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal sealed class WorkbenchReview(Window owner) : IOperationReview
{
    private const string RenameTitle = "重命名审批";
    private const string RenameDetail = "移动此文件。目标必须是工作区内的空闲文件名。";

    public ValueTask<bool> ApproveAsync(OperationReview operation, CancellationToken token)
    {
        var (title, target, before, label) = operation.Kind switch
        {
            OperationKind.RunSandboxCommand => ("容器命令审批", operation.Target, operation.Before, "命令参数"),
            OperationKind.FetchExternalText => ("额外联网审批", "HTTPS GET", operation.Target, "请求 URL"),
            OperationKind.ShareNetworkText => ("额外联网审批", new Uri(operation.Target).IdnHost, operation.Before, "发送预览"),
            OperationKind.RunGitDiff => ("宿主 Git 查询审批", operation.Target, operation.Before, (string?)null),
            OperationKind.ShareGitDiff => ("Git 差异发送审批", operation.Target, operation.Before, "差异预览"),
            OperationKind.PrepareGitCommit => ("Git 提交检查审批", operation.Target, operation.Before, (string?)null),
            OperationKind.PublishGitCommit => ("任务提交审批", operation.Target, operation.Before, "本轮差异"),
            OperationKind.InspectHostCommand => ("宿主程序检查审批", operation.Target, (string?)null, (string?)null),
            OperationKind.RunHostCommand => ("宿主命令审批", operation.Target, operation.Before, "命令参数"),
            OperationKind.ShareHostCommandOutput => ("宿主命令输出审批", operation.Target, operation.Before, "发送预览"),
            OperationKind.RequestMcpHttp => ("MCP 联网审批", operation.Target, operation.Before, "请求内容"),
            OperationKind.ShareMcpCatalog => ("MCP 工具定义发送审批", operation.Target, operation.Before, "发送预览"),
            OperationKind.CallMcpTool => ("MCP 工具调用审批", operation.Target, operation.Before, "调用参数"),
            OperationKind.ShareMcpResult => ("MCP 结果发送审批", operation.Target, operation.Before, "发送预览"),
            OperationKind.ReadGitHubPullRequest => ("GitHub PR 查询审批", operation.Target, operation.Before, (string?)null),
            OperationKind.SubmitGitHubReview => ("GitHub PR 审阅发布审批", operation.Target, operation.Before, "审阅预览"),
            OperationKind.CheckForUpdate => ("检查更新审批", operation.Target, operation.Before, (string?)null),
            OperationKind.DownloadUpdate => ("下载更新审批", operation.Target, operation.Before, "SHA-256"),
            OperationKind.LaunchUpdate => ("启动安装器审批", operation.Target, operation.Before, "SHA-256"),
            _ => ("区外文件审批", operation.Target, operation.Before, (string?)null),
        };
        return new(ShowOnUiAsync(title, target, operation.Detail, operation.Destination, before, operation.After, token, beforeLabel: label));
    }

    public ValueTask<string?> SelectGitRepositoryAsync(CancellationToken token) => new(owner.Dispatcher.InvokeAsync(() =>
    {
        token.ThrowIfCancellationRequested();
        var picker = new OpenFolderDialog { Title = "选择链接工作树的主仓库" };
        return picker.ShowDialog(owner) == true ? picker.FolderName : null;
    }, DispatcherPriority.Normal, token).Task);

    public IFileEditApproval PatchReviewer(Action<RunEvent> record, string? credential) => new PatchReview(this, record, credential);

    internal Task<bool> ConfirmLegacyPolicyAsync(string workspace, string detail, string prefixes) =>
        ShowOnUiAsync("应用旧版权限", workspace, detail, null, prefixes, null, CancellationToken.None,
            beforeLabel: "容器命令白名单", allowLabel: "应用到当前工作台");

    internal Task<bool> ConfirmPythonResumeAsync(string id, Uri endpoint, RunHistory.LegacyContinuation continuation,
        string prompt, CancellationToken token)
    {
        var preview = new System.Text.StringBuilder();
        if (continuation.RequiresInspection)
            preview.AppendLine("旧会话中有未确认的操作。请先核对工作区和外部状态，再决定是否继续。");
        foreach (var tool in continuation.RecoveredTools.Take(15))
        {
            var status = tool.Status switch
            {
                "completed" => "已有结果",
                "pending" => "尚未执行",
                "started" => "已启动，结果未知",
                _ => "缺少执行记录",
            };
            preview.AppendLine($"{tool.Name} · {status}");
        }
        if (continuation.RecoveredTools.Length > 15 || continuation.UnmatchedJournalRows > 0)
            preview.AppendLine($"恢复记录 {continuation.RecoveredTools.Length} 项，未关联的日志 {continuation.UnmatchedJournalRows} 项。");
        preview.Append($"\n本次任务\n{prompt[..Math.Min(prompt.Length, 1200)]}");
        return ShowOnUiAsync("续跑 Python 会话", id,
            $"将把 {continuation.Messages} 条旧消息（含历史工具结果）及 {continuation.Attachments} 张旧图片发送给当前模型。" +
            "历史操作不会自动重放，本轮按当前审批设置执行。",
            endpoint.AbsoluteUri, preview.ToString(), null, token, beforeLabel: "旧会话和本次任务", allowLabel: "发送并续跑");
    }

    internal Task<bool> ConfirmInterruptedRunAsync(string runId, RunInspection.Result run, CancellationToken token) =>
        ShowOnUiAsync("续跑中断的 C# 任务", runId, "请核对上轮运行可能产生的副作用。本轮会新开一次运行。",
            null, string.Join('\n', run.UnconfirmedTools.Take(25).Select(tool => $"{tool.Name} · {tool.Id}")), null, token,
            beforeLabel: "未确认的工具", allowLabel: "已核对，继续");

    private Task<bool> ShowOnUiAsync(string title, string target, string detail, string? destination,
        string? before, string? after, CancellationToken token, string? beforeLabel = null, string? allowLabel = null) =>
        owner.Dispatcher.InvokeAsync(() => ShowAsync(title, target, detail, destination, before, after, token, beforeLabel, allowLabel),
            DispatcherPriority.Normal, token).Task.Unwrap();

    private async Task<bool> ShowAsync(string title, string target, string detail, string? destination,
        string? before, string? after, CancellationToken token, string? beforeLabel, string? allowLabel)
    {
        token.ThrowIfCancellationRequested();
        var dialog = new Window
        {
            Owner = owner, Title = title, Width = 625, Height = before is null ? 255 : 450,
            MinWidth = 390, MinHeight = 220, WindowStartupLocation = WindowStartupLocation.CenterOwner,
            FontSize = 13,
        };
        ApplyAppearance(dialog, (owner as WorkbenchWindow ?? owner.Owner as WorkbenchWindow)?.AppearanceProfile ?? "paper");
        var (layout, deny, allow) = BuildContent(target, detail, destination, before, after, beforeLabel, allowLabel);
        dialog.Content = layout;
        var result = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var approved = false;
        deny.Click += (_, _) => dialog.Close();
        allow.Click += (_, _) => { approved = !token.IsCancellationRequested; dialog.Close(); };
        dialog.PreviewKeyDown += (_, e) => { if (e.Key == Key.Escape) dialog.Close(); };
        dialog.Closed += (_, _) => result.TrySetResult(approved);
        using var registration = token.Register(() => dialog.Dispatcher.BeginInvoke(new Action(() =>
        {
            if (dialog.IsVisible)
                dialog.Close();
        })));
        dialog.Show();
        deny.Focus();
        return await result.Task;
    }

    private static void ApplyAppearance(Control element, string profile)
    {
        element.Resources.MergedDictionaries.Add(WorkbenchAppearance.Create(profile));
        element.Resources.MergedDictionaries.Add(new ResourceDictionary
        {
            Source = new Uri("pack://application:,,,/SciDev.Desktop;component/WorkbenchControls.xaml", UriKind.Absolute),
        });
        element.SetResourceReference(Control.BackgroundProperty, "WorkbenchPanel");
        element.SetResourceReference(Control.ForegroundProperty, "WorkbenchText");
    }

    internal static FrameworkElement CreateRenameCapture()
    {
        var (layout, _, _) = BuildContent("源文件\nexperiments/pelican-draft.svg", RenameDetail,
            "目标文件\nassets/pelican-on-bike.svg", null, null, null, null);
        var stack = new StackPanel();
        stack.Children.Add(new TextBlock { Text = RenameTitle, FontSize = 17, FontWeight = FontWeights.SemiBold,
            Margin = new(16, 16, 16, 0) });
        stack.Children.Add(layout);
        var content = new ContentControl { Content = stack, FontSize = 13, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        ApplyAppearance(content, "paper");
        var card = new Border { Child = content, Width = 625, MinHeight = 220, Margin = new Thickness(16),
            HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
        card.Background = (Brush)content.FindResource("WorkbenchPanel");
        return card;
    }

    private static (DockPanel Layout, Button Deny, Button Allow) BuildContent(string target, string detail, string? destination,
        string? before, string? after, string? beforeLabel, string? allowLabel)
    {
        var layout = new DockPanel { Margin = new Thickness(16), LastChildFill = before is not null };
        var heading = new StackPanel();
        heading.Children.Add(new TextBlock { Text = target, FontSize = 15, FontWeight = FontWeights.SemiBold,
            TextWrapping = TextWrapping.Wrap });
        heading.Children.Add(new TextBlock { Text = detail, TextWrapping = TextWrapping.Wrap, Margin = new(0, 6, 0, 0) });
        if (destination is not null)
            heading.Children.Add(new TextBlock { Text = destination, TextWrapping = TextWrapping.Wrap, Margin = new(0, 6, 0, 0) });
        DockPanel.SetDock(heading, Dock.Top);
        layout.Children.Add(heading);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right,
            Margin = new(0, 10, 0, 0) };
        var deny = new Button { Content = "拒绝", Padding = new(15, 7, 15, 7), IsDefault = true };
        var allow = new Button { Content = allowLabel ?? "仅批准本次", Padding = new(15, 7, 15, 7), Margin = new(8, 0, 0, 0) };
        buttons.Children.Add(deny);
        buttons.Children.Add(allow);
        DockPanel.SetDock(buttons, Dock.Bottom);
        layout.Children.Add(buttons);
        if (before is not null)
        {
            var text = new Grid { Margin = new(0, 8, 0, 0) };
            AddPreview(text, beforeLabel ?? (after is null ? "发送预览" : "替换前"), before);
            if (after is not null)
                AddPreview(text, "替换后", after);
            layout.Children.Add(text);
        }
        return (layout, deny, allow);
    }

    private static void AddPreview(Grid grid, string title, string code)
    {
        var row = grid.RowDefinitions.Count;
        grid.RowDefinitions.Add(new RowDefinition());
        var panel = new DockPanel();
        var label = new TextBlock { Text = title, FontWeight = FontWeights.SemiBold, Margin = new(0, 4, 0, 4) };
        DockPanel.SetDock(label, Dock.Top);
        panel.Children.Add(label);
        var preview = new TextBox { Text = code, IsReadOnly = true, FontFamily = new FontFamily("Consolas"),
            TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        AutomationProperties.SetName(preview, title);
        panel.Children.Add(preview);
        Grid.SetRow(panel, row);
        grid.Children.Add(panel);
    }

    private sealed class PatchReview(WorkbenchReview ui, Action<RunEvent> record, string? credential) : IFileEditApproval
    {
        public async ValueTask<bool> ApproveAsync(FileEditPreview preview, CancellationToken token)
        {
            var change = new { preview.Path, preview.Line, preview.Destination };
            record(new("edit_review_requested", 0, change));
            var sensitive = !string.IsNullOrEmpty(credential) &&
                (preview.Before.Contains(credential, StringComparison.Ordinal) || preview.After.Contains(credential, StringComparison.Ordinal));
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
            deadline.CancelAfter(TimeSpan.FromMinutes(2));
            var approved = false;
            try
            {
                if (!sensitive)
                    approved = preview.Destination is { } destination
                        ? await ui.ShowOnUiAsync(RenameTitle, "源文件\n" + preview.Path, RenameDetail,
                            "目标文件\n" + destination, null, null, deadline.Token)
                        : await ui.ShowOnUiAsync("补丁审批", preview.Path, $"在第 {preview.Line} 行应用下列精确替换。",
                            null, preview.Before, preview.After, deadline.Token);
                approved &= !deadline.IsCancellationRequested;
            }
            catch (OperationCanceledException) when (!token.IsCancellationRequested) { }
            record(new(approved ? "edit_review_approved" : "edit_review_denied", 0, change));
            token.ThrowIfCancellationRequested();
            return approved;
        }
    }
}
