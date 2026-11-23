using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class AutomaticProjectVerification(WorkspaceFiles files, DockerWorkspaceTools container, bool automatic) : IAutomaticVerification
{
    public bool CanRunAutomatically => automatic;

    public VerificationPlan Discover(IReadOnlyList<FileChange> changes, CancellationToken token)
    {
        try { return files.FindVerificationPlan(changes, token); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException or DecoderFallbackException)
        {
            return new([], [], Complete: false);
        }
    }

    public ValueTask<ToolResult> RunAsync(string callId, VerificationCandidate candidate, CancellationToken token) =>
        container.RunVerificationAsync(callId, candidate, token);
}
