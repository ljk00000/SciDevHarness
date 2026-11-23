using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class WorkspaceNetworkPolicy
{
    public static void RequireAllowed(string workspace, string deniedMessage)
    {
        if (IsDenied(workspace))
            throw new ToolDeniedException("network_policy_denied", deniedMessage);
    }

    public static bool IsDenied(string workspace) => IsEnvironmentDenied() ||
        LegacyExecutionPolicy.Read(workspace)?.EffectiveNetwork == AgentNetworkPolicy.Deny;

    internal static bool IsEnvironmentDenied()
    {
        var policy = Environment.GetEnvironmentVariable("SCIDEV_NETWORK_POLICY")?.Trim().ToLowerInvariant();
        return policy is "deny" or "denied" or "off" or "block";
    }
}
