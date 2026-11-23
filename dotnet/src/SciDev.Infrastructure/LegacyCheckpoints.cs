using System.Security.Cryptography;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

// Import only the files listed by Python's checkpoint manifest. Target paths are metadata.
internal static class LegacyCheckpoints
{
    internal const long MaxTotalBytes = 128L * 1024 * 1024;
    private const int MaxBackupBytes = 16 * 1024 * 1024;
    private const int MaxManifestBytes = 1024 * 1024;
    internal sealed record Counts(int Checkpoints, int Backups, long Bytes);

    public static Counts Scan(WindowsWorkspace state, string? destination, long budget, CancellationToken token)
    {
        using var basePath = state.OpenPath("checkpoints");
        if (!Directory.Exists(basePath.AbsolutePath))
            return new(0, 0, 0);
        var source = new WindowsWorkspace(basePath.AbsolutePath);
        var count = 0;
        var backups = 0;
        var remaining = budget;
        foreach (var path in Directory.EnumerateDirectories(basePath.AbsolutePath))
        {
            token.ThrowIfCancellationRequested();
            var id = Path.GetFileName(path);
            if (++count > 1000 || id.Length is < 1 or > 80 ||
                id.Any(ch => !char.IsLetterOrDigit(ch) && ch is not ('-' or '_')))
                throw InvalidCheckpoint();
            using var checkpointPath = source.OpenPath(id);
            var checkpoint = new WindowsWorkspace(checkpointPath.AbsolutePath);
            var manifest = Read(checkpoint, "manifest.json", MaxManifestBytes, ref remaining);
            using var document = JsonDocument.Parse(manifest);
            if (document.RootElement is not { ValueKind: JsonValueKind.Object } root ||
                !root.TryGetProperty("checkpoint_id", out var recordedId) ||
                recordedId.ValueKind != JsonValueKind.String || recordedId.GetString() != id ||
                !root.TryGetProperty("entries", out var entries) ||
                entries.ValueKind != JsonValueKind.Array || entries.GetArrayLength() > 1000)
                throw InvalidCheckpoint();

            var output = destination is null ? null : Path.Combine(destination, "checkpoints", id);
            if (output is not null)
            {
                Directory.CreateDirectory(Path.Combine(output, "files"));
                Write(output, "manifest.json", manifest);
            }
            var targets = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var entry in entries.EnumerateArray())
            {
                token.ThrowIfCancellationRequested();
                var relative = RequiredString(entry, "path").Replace('\\', '/');
                if (Path.IsPathRooted(relative) || relative.Split('/').Any(part => part is "" or "." or ".." ||
                    part.Any(ch => char.IsControl(ch) || Path.GetInvalidFileNameChars().Contains(ch))) || !targets.Add(relative))
                    throw InvalidCheckpoint();
                var name = RequiredString(entry, "backup");
                if (!entry.TryGetProperty("existed", out var existed) ||
                    !entry.TryGetProperty("size", out var size) || size.ValueKind != JsonValueKind.Number ||
                    !size.TryGetInt32(out var bytes))
                    throw InvalidCheckpoint();
                if (existed.ValueKind == JsonValueKind.False && name.Length == 0 && bytes == 0)
                    continue;
                if (existed.ValueKind != JsonValueKind.True || bytes is < 0 or > MaxBackupBytes || !IsBackupName(name) || !names.Add(name))
                    throw InvalidCheckpoint();

                using var filesPath = checkpoint.OpenPath("files");
                var files = new WindowsWorkspace(filesPath.AbsolutePath);
                var data = Read(files, name, MaxBackupBytes, ref remaining);
                if (data.Length != bytes ||
                    !Convert.ToHexString(SHA256.HashData(data))[..16].Equals(name[5..21], StringComparison.OrdinalIgnoreCase))
                    throw InvalidCheckpoint();
                if (output is not null)
                    Write(Path.Combine(output, "files"), name, data);
                backups++;
            }
        }
        return new(count, backups, budget - remaining);
    }

    private static string RequiredString(JsonElement entry, string field) => entry.ValueKind == JsonValueKind.Object &&
        entry.TryGetProperty(field, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString()! :
        throw InvalidCheckpoint();

    private static bool IsBackupName(string name) => name.Length == 25 && name[4] == '-' &&
        name.EndsWith(".bin", StringComparison.Ordinal) && name[..4].All(char.IsAsciiDigit) &&
        name[5..21].All(char.IsAsciiHexDigitLower);

    private static byte[] Read(WindowsWorkspace source, string name, int limit, ref long remaining)
    {
        using var file = source.OpenPath(name);
        if (!WindowsWorkspace.FileExists(file.AbsolutePath))
            throw InvalidCheckpoint();
        using var stream = File.Open(file.AbsolutePath, FileMode.Open, FileAccess.Read, FileShare.Read);
        WindowsWorkspace.GetFileIdentity(stream.SafeFileHandle, file.AbsolutePath);
        if (stream.Length > limit || stream.Length > remaining)
            throw new ToolDeniedException("snapshot_limit", "旧检查点超出导入大小限制。");
        var bytes = new byte[(int)stream.Length];
        stream.ReadExactly(bytes);
        remaining -= bytes.Length;
        return bytes;
    }

    private static void Write(string directory, string name, byte[] bytes)
    {
        using var stream = new FileStream(Path.Combine(directory, name), FileMode.CreateNew, FileAccess.Write);
        stream.Write(bytes);
    }

    private static ToolDeniedException InvalidCheckpoint() =>
        new("invalid_checkpoint", "旧检查点的清单、备份或校验值无效。");
}
