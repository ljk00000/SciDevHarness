using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class GitPullRequestDialog : Window
{
    private readonly WorkbenchReview _review;
    private readonly Func<string?, IOperationReview, bool, CancellationToken, Task<GitHubPullRequests>> _open;
    private CancellationTokenSource? _operation;
    private LoadedPullRequest? _loaded;
    private bool _closed;

    private sealed record LoadedPullRequest(GitHubPullRequests Client, GitHubPullRequests.ViewWithDiff View);

    internal GitPullRequestDialog(Window owner, string workspace, string? modelKey,
        Func<string?, IOperationReview, bool, CancellationToken, Task<GitHubPullRequests>>? open = null)
    {
        InitializeComponent();
        Owner = owner;
        Resources.MergedDictionaries.Add(WorkbenchAppearance.Create((owner as WorkbenchWindow)?.AppearanceProfile ?? "paper"));
        Resources.MergedDictionaries.Add(new ResourceDictionary
        {
            Source = new Uri("pack://application:,,,/SciDev.Desktop;component/WorkbenchControls.xaml", UriKind.Absolute),
        });
        MaxWidth = Math.Max(MinWidth, SystemParameters.WorkArea.Width);
        MaxHeight = Math.Max(MinHeight, SystemParameters.WorkArea.Height);
        _review = new(this);
        var denyNetwork = (owner as WorkbenchWindow)?.ViewModel.NetworkPolicy == AgentNetworkPolicy.Deny;
        _open = open ?? ((repository, review, allow, token) =>
            GitHubPullRequests.OpenAsync(workspace, repository, review, allow, token, modelKey, denyNetwork));
        Closed += (_, _) =>
        {
            _closed = true;
            _loaded = null;
            _operation?.Cancel();
        };
        UpdateActions();
    }

    private void QueryChanged(object sender, RoutedEventArgs args)
    {
        if (Notice is null)
            return;
        ClearPullRequest();
        Notice.Text = "";
        UpdateActions();
    }

    private async void LoadClicked(object sender, RoutedEventArgs args)
    {
        if (!TryNumber(out var number) || _operation is not null)
            return;
        var allowNetwork = OverrideNetwork.IsChecked == true;
        ClearPullRequest();
        await RunAsync("正在读取 PR…", async token =>
        {
            var client = await OpenClientAsync(allowNetwork, token);
            var view = await Task.Run(() => client.ViewWithDiffAsync(number, token), token);
            if (_closed)
                return;
            token.ThrowIfCancellationRequested();
            _loaded = new(client, view);
            var details = view.Details;
            PullTitle.Text = client.Display($"#{number} {details.GetProperty("title").GetString()}");
            PullDetails.Text = client.Display($"{details.GetProperty("state").GetString()} · " +
                $"{details.GetProperty("headRefName").GetString()} → {details.GetProperty("baseRefName").GetString()} · " +
                view.Revision.Head[..12]);
            Patch.Text = client.Display(view.Patch.Text);
            Notice.Text = view.Patch.Truncated ? "差异超过显示上限，已截断。" : "已核对差异对应的提交。";
        });
    }

    private async Task<GitHubPullRequests> OpenClientAsync(bool allowNetwork, CancellationToken token)
    {
        try { return await Task.Run(() => _open(null, _review, allowNetwork, token), token); }
        catch (ToolDeniedException ex) when (ex.Status == "git_linked_worktree")
        {
            if (await _review.SelectGitRepositoryAsync(token) is not { } repository)
                throw new ToolDeniedException("approval_denied", "未批准读取链接工作树的主仓库。");
            return await Task.Run(() => _open(repository, _review, allowNetwork, token), token);
        }
    }

    private async void ReviewClicked(object sender, RoutedEventArgs args)
    {
        if (_operation is not null || _loaded is not { } loaded || sender is not Button { Tag: string action })
            return;
        var body = ReviewBody.Text;
        await RunAsync("正在核对审阅目标…", async token =>
        {
            var result = await Task.Run(() => loaded.Client.ReviewAsync(loaded.View.Number, action, body, token, loaded.View.Revision), token);
            if (_closed)
                return;
            ReviewBody.Clear();
            var label = result.Action switch { "approve" => "批准", "comment" => "评论", _ => "请求修改" };
            Notice.Text = $"已提交{label} · {result.Url}";
        });
    }

    private async Task RunAsync(string pending, Func<CancellationToken, Task> operation)
    {
        using var source = new CancellationTokenSource();
        _operation = source;
        Notice.Text = pending;
        UpdateActions();
        try { await operation(source.Token); }
        catch (OperationCanceledException) when (source.IsCancellationRequested)
        {
            if (!_closed)
                Notice.Text = "操作已取消。";
        }
        catch (Exception ex) when (ex is ToolDeniedException or ArgumentException or IOException or UnauthorizedAccessException or Win32Exception)
        {
            if (!_closed)
            {
                if (ex is ToolDeniedException { Status: "pr_review_revision_changed" or "pr_review_unconfirmed" })
                    _loaded = null;
                Notice.Text = ex is ToolDeniedException or ArgumentException ? ex.Message : "无法完成 GitHub PR 操作。";
            }
        }
        finally
        {
            _operation = null;
            if (!_closed)
                UpdateActions();
        }
    }

    private void ClearPullRequest()
    {
        _loaded = null;
        PullTitle.Text = "输入 PR 编号";
        PullDetails.Text = "";
        Patch.Clear();
        ReviewBody.Clear();
    }

    private bool TryNumber(out int number) =>
        int.TryParse(NumberInput.Text.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out number) && number is >= 1 and <= 1_000_000_000;

    private void UpdateActions()
    {
        if (CancelButton is null)
            return;
        var idle = _operation is null;
        QueryFields.IsEnabled = idle;
        LoadButton.IsEnabled = idle && TryNumber(out _);
        ReviewBody.IsEnabled = ApproveButton.IsEnabled = idle && _loaded is not null;
        CommentButton.IsEnabled = ChangesButton.IsEnabled = ApproveButton.IsEnabled && !string.IsNullOrWhiteSpace(ReviewBody.Text);
        CancelButton.IsEnabled = !idle;
    }

    private void ReviewBodyChanged(object sender, TextChangedEventArgs args) => UpdateActions();
    private void CancelClicked(object sender, RoutedEventArgs args) => _operation?.Cancel();
    private void CloseClicked(object sender, RoutedEventArgs args) => Close();
}
