using SciDev.Core;

namespace SciDev.Infrastructure;

internal interface IOperationReviewAudit
{
    void Requested(OperationReview request);
    void Decided(OperationReview request, string outcome);
}

internal static class OperationApproval
{
    public static async ValueTask<string> DecideAsync(IOperationReview reviewer, OperationReview request, CancellationToken token)
    {
        var audit = reviewer as IOperationReviewAudit;
        audit?.Requested(request);
        var outcome = await DecideCoreAsync(reviewer, request, token);
        audit?.Decided(request, outcome);
        return outcome;
    }

    private static async ValueTask<string> DecideCoreAsync(IOperationReview reviewer, OperationReview request, CancellationToken token)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromMinutes(2));
        try
        {
            deadline.Token.ThrowIfCancellationRequested();
            var approved = await reviewer.ApproveAsync(request, deadline.Token).AsTask().WaitAsync(deadline.Token);
            if (token.IsCancellationRequested)
                return "cancelled";
            if (deadline.IsCancellationRequested)
                return "timed_out";
            return approved ? "approved" : "denied";
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { return "cancelled"; }
        catch (OperationCanceledException) when (deadline.IsCancellationRequested) { return "timed_out"; }
        catch (Exception) { return "review_unavailable"; }
    }
}
