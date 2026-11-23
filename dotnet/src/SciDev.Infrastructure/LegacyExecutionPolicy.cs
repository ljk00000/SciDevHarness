using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Read the Python profile without granting permissions or changing the source.</summary>
public sealed record LegacyExecutionPolicy(string Execution, string Network, string Backend, string? Image,
    string ContainerPrefixes, int IgnoredPrefixes)
{
    public sealed record Summary(string Status, string? Execution = null, string? Network = null, string? Backend = null,
        bool DockerImagePresent = false, int ContainerPrefixes = 0, int IgnoredPrefixes = 0, string? EffectiveNetwork = null);

    public ExecutionMode? Mode => Execution switch
    {
        "read-only" => ExecutionMode.ReadOnly,
        "manual" => ExecutionMode.Manual,
        "workspace-write" => ExecutionMode.WorkspaceWrite,
        "workspace-approval" => ExecutionMode.WorkspaceApproval,
        _ => null,
    };

    public AgentNetworkPolicy EffectiveNetwork => Network == "deny" ? AgentNetworkPolicy.Deny : AgentNetworkPolicy.Ask;

    public string? ImageFor(ExecutionMode mode) => mode == ExecutionMode.ReadOnly || Backend != "docker" ? null : Image;

    public string? PrefixesFor(ExecutionMode mode, string? image) =>
        mode == ExecutionMode.WorkspaceWrite && Mode == mode && image is not null && Backend == "docker" ? ContainerPrefixes : null;

    public static LegacyExecutionPolicy? Read(string workspace)
    {
        _ = new WindowsWorkspace(workspace);
        var folder = Path.Combine(workspace, ".research");
        return Directory.Exists(folder) ? ReadState(new WindowsWorkspace(folder)) : null;
    }

    internal static Summary? Inspect(WindowsWorkspace state)
    {
        try
        {
            var policy = ReadState(state);
            return policy is null ? null : new(policy.Mode.HasValue ? "available" : "mode_requires_selection", policy.Execution,
                policy.Network, policy.Backend, policy.Image is not null,
                policy.ContainerPrefixes.Length == 0 ? 0 : policy.ContainerPrefixes.Count(ch => ch == '\n') + 1,
                policy.IgnoredPrefixes, policy.EffectiveNetwork.ToString().ToLowerInvariant());
        }
        catch (ToolDeniedException ex) when (ex.Status == "legacy_policy_invalid")
        {
            return new("invalid");
        }
    }

    private static LegacyExecutionPolicy? ReadState(WindowsWorkspace state)
    {
        using var lease = state.OpenPath("settings.json");
        if (!WindowsWorkspace.FileExists(lease.AbsolutePath))
            return null;
        using var file = WindowsWorkspace.OpenForRead(lease.AbsolutePath, FileShare.Read);
        WindowsWorkspace.GetFileIdentity(file, lease.AbsolutePath);
        using var input = new FileStream(file, FileAccess.Read);
        if (input.Length > 1024 * 1024)
            throw Invalid();
        try
        {
            using var document = JsonDocument.Parse(input);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || root.TryGetProperty("schema_version", out var schema) &&
                (schema.ValueKind != JsonValueKind.Number || !schema.TryGetInt32(out var version) || version is < 0 or > 2))
                throw Invalid();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            if (root.EnumerateObject().Any(property => IsPolicyField(property.Name) && !seen.Add(property.Name)))
                throw Invalid();
            var execution = Setting(root, "execution_mode", "workspace-write").Replace('_', '-') switch
            {
                "read-only" or "readonly" or "read" => "read-only",
                "manual" or "ask" or "on-request" or "suggest" => "manual",
                "workspace-approval" => "workspace-approval",
                "workspace-write" or "workspacewrite" or "workspace" => "workspace-write",
                "full-access" or "fullaccess" or "full" or "danger-full-access" => "full-access",
                _ => throw Invalid(),
            };
            var network = Setting(root, "network_policy", "ask").Replace('_', '-') switch
            {
                "deny" or "denied" or "off" or "block" => "deny",
                "ask" or "manual" or "on-request" => "ask",
                "allow" or "allowed" or "on" => "allow",
                _ => throw Invalid(),
            };
            var backend = Setting(root, "sandbox_backend", "local");
            if (backend is not ("local" or "docker"))
                throw Invalid();
            string? image = null;
            if (backend == "docker" && root.TryGetProperty("sandbox_image", out var value))
            {
                if (value.ValueKind != JsonValueKind.String)
                    throw Invalid();
                image = value.GetString()?.Trim();
                if (image is { Length: > 0 })
                {
                    try { DockerProbe.ValidateImage(image); }
                    catch (ArgumentException) { throw Invalid(); }
                }
                else image = null;
            }
            var (prefixes, ignored) = ReadPrefixes(root);
            return new(execution, network, backend, image, prefixes, ignored);
        }
        catch (JsonException) { throw Invalid(); }
    }

    private static bool IsPolicyField(string name) => name is "schema_version" or "execution_mode" or "network_policy" or
        "sandbox_backend" or "sandbox_image" or "allowed_commands";

    private static string Setting(JsonElement root, string property, string fallback) => !root.TryGetProperty(property, out var value)
        ? fallback : value.ValueKind == JsonValueKind.String ? value.GetString()!.Trim().ToLowerInvariant() : throw Invalid();

    private static (string Prefixes, int Ignored) ReadPrefixes(JsonElement root)
    {
        if (!root.TryGetProperty("allowed_commands", out var value) || value.ValueKind == JsonValueKind.Null)
            return ("", 0);
        var lines = value.ValueKind switch
        {
            JsonValueKind.Array => value.EnumerateArray().Select(item => item.ValueKind == JsonValueKind.String ? item.GetString() : null),
            JsonValueKind.String => value.GetString()!.Split([';', '\r', '\n'], StringSplitOptions.RemoveEmptyEntries),
            _ => throw Invalid(),
        };
        var valid = new List<string>();
        var ignored = 0;
        foreach (var line in lines)
        {
            if (line is null || line.IndexOfAny([';', '\r', '\n']) >= 0 || valid.Count >= 50)
            {
                ignored++;
                continue;
            }
            var prefix = line.Trim();
            if (prefix.Length == 0 || valid.Contains(prefix, StringComparer.Ordinal))
                continue;
            try { _ = ContainerCommandPrefixes.Parse(prefix); }
            catch (ArgumentException) { ignored++; continue; }
            valid.Add(prefix);
        }
        return (string.Join('\n', valid), ignored);
    }

    private static ToolDeniedException Invalid() => new("legacy_policy_invalid", "旧版权限配置格式无效，应用前需要核对设置文件。");
}
