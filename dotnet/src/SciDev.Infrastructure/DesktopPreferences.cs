using System.Text.Json;
using System.Text.Json.Serialization;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed record DesktopPreferences(string Profile, IReadOnlyDictionary<string, DesktopPreferences.QtLayout> QtLayouts)
{
    internal const string FileName = "desktop-preferences.json";
    private const int MaxBytes = 32_768;
    private static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
    public static IReadOnlyList<string> Profiles { get; } = Array.AsReadOnly(new[] { "studio", "paper", "focus" });
    public static DesktopPreferences Default => new("paper", new Dictionary<string, QtLayout>());
    public DesktopAppearanceOptions Appearance { get; init; } = new();

    public sealed record QtLayout(double Left, double Chat)
    {
        public bool CanApply => Left is >= 0.10 and <= 0.40 && Chat is >= 0.10 and <= 0.62 && Left + Chat < 0.85;
    }

    public static DesktopPreferences? Load(string workspace)
    {
        _ = new WindowsWorkspace(workspace);
        var state = Path.Combine(workspace, ".research");
        if (!Directory.Exists(state))
            return null;
        using var file = new WindowsWorkspace(state).OpenPath(FileName);
        if (!WindowsWorkspace.FileExists(file.AbsolutePath))
            return null;
        using var snapshot = WorkspaceFiles.OpenSnapshot(file.AbsolutePath, FileShare.Read, maxBytes: MaxBytes);
        return Parse(snapshot.Bytes);
    }

    public static DesktopPreferences ReadExport(string fileName)
    {
        var absolute = Path.GetFullPath(fileName);
        var parent = Path.GetDirectoryName(absolute) ?? throw new ArgumentException("请选择 Qt 偏好导出文件。");
        using var file = new WindowsWorkspace(parent).OpenOperatorPath(Path.GetFileName(absolute))!;
        using var snapshot = WorkspaceFiles.OpenSnapshot(file.AbsolutePath, FileShare.Read, maxBytes: MaxBytes);
        return Parse(snapshot.Bytes);
    }

    public void Save(string workspace, CancellationToken token) => Update(workspace, _ => this, token);

    public static DesktopPreferences SaveProfile(string workspace, string profile, CancellationToken token)
    {
        if (!Profiles.Contains(profile, StringComparer.Ordinal))
            throw Invalid();
        return Update(workspace, current => current with { Profile = profile }, token);
    }

    private static DesktopPreferences Update(string workspace, Func<DesktopPreferences, DesktopPreferences> change,
        CancellationToken token)
    {
        var state = new WindowsWorkspace(workspace).EnsureStateDirectory();
        using var file = state.OpenPath(FileName);
        WorkspaceFiles.FileStamp? expected = null;
        var previous = Default;
        if (WindowsWorkspace.FileExists(file.AbsolutePath))
        {
            using var current = WorkspaceFiles.OpenSnapshot(file.AbsolutePath, FileShare.Read, maxBytes: MaxBytes);
            previous = Parse(current.Bytes);
            expected = current.Stamp;
        }
        var next = change(previous);
        var serialized = next.Serialize();
        _ = Parse(serialized);
        WorkspaceFiles.Save(file.AbsolutePath, serialized, token, expected, maxCurrentBytes: MaxBytes);
        return next;
    }

    internal byte[] Serialize() => JsonSerializer.SerializeToUtf8Bytes(new
    {
        version = 1,
        Profile,
        qt_layouts = QtLayouts.ToDictionary(entry => entry.Key, entry => new[] { entry.Value.Left, entry.Value.Chat }),
        appearance = Appearance == new DesktopAppearanceOptions() ? null : Appearance,
    }, Json);

    private static DesktopPreferences Parse(byte[] bytes)
    {
        try
        {
            var offset = bytes.AsSpan().StartsWith(System.Text.Encoding.UTF8.Preamble) ? 3 : 0;
            using var json = JsonDocument.Parse(bytes.AsMemory(offset));
            var root = json.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("version", out var version) ||
                version.ValueKind != JsonValueKind.Number || !version.TryGetInt32(out var number))
                throw Invalid();
            if (number != 1)
                throw new ToolDeniedException("preferences_version", "桌面偏好版本不受支持，请保留原文件。");
            if (!root.TryGetProperty("profile", out var theme) || theme.ValueKind != JsonValueKind.String ||
                theme.GetString() is not { } profile || !Profiles.Contains(profile, StringComparer.Ordinal))
                throw Invalid();
            var layouts = new Dictionary<string, QtLayout>(StringComparer.Ordinal);
            if (root.TryGetProperty("qt_layouts", out var data))
            {
                if (data.ValueKind != JsonValueKind.Object)
                    throw Invalid();
                foreach (var item in data.EnumerateObject())
                {
                    if (!Profiles.Contains(item.Name, StringComparer.Ordinal))
                        continue;
                    if (item.Value.ValueKind != JsonValueKind.Array || item.Value.GetArrayLength() != 2 ||
                        !TryNumber(item.Value[0], out var left) || !TryNumber(item.Value[1], out var chat))
                        throw Invalid();
                    layouts[item.Name] = new(left, chat);
                }
            }
            var appearance = root.TryGetProperty("appearance", out var options)
                ? options.Deserialize<DesktopAppearanceOptions>(Json) : new();
            if (appearance is null || !appearance.IsValid)
                throw Invalid();
            return new(profile, layouts) { Appearance = appearance };
        }
        catch (JsonException) { throw Invalid(); }
    }

    private static bool TryNumber(JsonElement item, out double value)
    {
        value = 0;
        return item.ValueKind == JsonValueKind.Number && item.TryGetDouble(out value) && double.IsFinite(value);
    }

    private static ToolDeniedException Invalid() =>
        new("preferences_invalid", "桌面偏好文件格式无效。");
}
