using System.Text.Json;
using SciDev.Core;

namespace SciDev.Cli;

internal sealed class ConsoleEditApproval(string workspace, Action<RunEvent> record, string? key) : IFileEditApproval
{
    public async ValueTask<bool> ApproveAsync(FileEditPreview preview, CancellationToken token)
    {
        var data = new { preview.Path, preview.Line, preview.Destination };
        record(new("edit_review_requested", 0, data));
        if (!string.IsNullOrEmpty(key) && (preview.Before.Contains(key, StringComparison.Ordinal) ||
            preview.After.Contains(key, StringComparison.Ordinal)))
        {
            record(new("edit_review_denied", 0, new { reason = "credential" }));
            return false;
        }

        // Each proposal needs a fresh answer; do not consume surplus keystrokes
        // from an earlier review as approval for the next one.
        ConsoleConfirmation.DiscardPendingInput(token);
        var source = JsonSerializer.Serialize(Path.Combine(workspace, preview.Path));
        if (preview.Destination is { } destination)
        {
            Console.Error.WriteLine("重命名审批");
            Console.Error.WriteLine($"- {source}");
            Console.Error.WriteLine($"+ {JsonSerializer.Serialize(Path.Combine(workspace, destination))}");
        }
        else
        {
            Console.Error.WriteLine($"补丁审批：{source} 第 {preview.Line} 行");
            Console.Error.WriteLine($"- {JsonSerializer.Serialize(preview.Before)}");
            Console.Error.WriteLine($"+ {JsonSerializer.Serialize(preview.After)}");
        }
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromMinutes(2));
        var approved = false;
        try { approved = await ConsoleConfirmation.ReadAsync(deadline.Token); }
        catch (OperationCanceledException) when (!token.IsCancellationRequested) { }

        token.ThrowIfCancellationRequested();
        record(new(approved ? "edit_review_approved" : "edit_review_denied", 0, data));
        return approved;
    }
}
