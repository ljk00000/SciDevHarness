using System.Text.Json;
using Microsoft.Data.Sqlite;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Import explicitly selected legacy history into a new, independent snapshot directory.</summary>
public static class LegacyStateImport
{
    private const long MaxText = 16L * 1024 * 1024;
    private const long MaxTotal = LegacyCheckpoints.MaxTotalBytes;
    private static readonly string[] ProviderKeys =
    [
        "base_url", "model", "request_timeout", "temperature", "presence_penalty",
        "reasoning_effort", "fallback_model", "streaming", "text_tool_call_fallback", "credential_profile",
    ];

    public sealed record Result(string Status, string? Snapshot, LegacyStateAudit.Result? Counts,
        bool QtPreferencesImported = false);

    public static Result Import(string workspaceCopy, string snapshotPath, CancellationToken token, string? qtPreferences = null)
    {
        var sourceRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(workspaceCopy));
        var destination = Path.TrimEndingDirectorySeparator(Path.GetFullPath(snapshotPath));
        if (Within(sourceRoot, destination) || Within(destination, sourceRoot))
            throw new ArgumentException("旧工作区与新快照目录必须相互独立。");
        var parent = Path.GetDirectoryName(destination) ?? throw new ArgumentException("请选择现有父目录下的新快照路径。");
        _ = new WindowsWorkspace(sourceRoot);
        var output = new WindowsWorkspace(parent);
        var source = Path.Combine(sourceRoot, ".research");
        if (!Directory.Exists(source))
            return new("no_python_state", null, null);
        var legacy = new WindowsWorkspace(source);
        var folderName = Path.GetFileName(destination);
        using var target = output.OpenPath(folderName);
        if (Directory.Exists(target.AbsolutePath) || File.Exists(target.AbsolutePath))
            throw new ToolDeniedException("destination_exists", "导入只能发布到新的目录。");

        var before = LegacyStateAudit.Check(sourceRoot, token);
        if (before.Status != "ready_to_import")
            return new(before.Status, null, before);

        var appearance = qtPreferences is null ? null : DesktopPreferences.ReadExport(qtPreferences);
        var stagePath = Path.Combine(parent, $"scidev-import-{Guid.NewGuid():N}");
        Directory.CreateDirectory(stagePath);
        try
        {
            var staging = Directory.CreateDirectory(Path.Combine(stagePath, ".research"));
            var remaining = MaxTotal;
            Copy(legacy, "settings.json", staging.FullName, ref remaining, token);
            Copy(legacy, "provider.json", staging.FullName, ref remaining, token);
            Copy(legacy, "events.jsonl", staging.FullName, ref remaining, token);
            var sessions = Path.Combine(source, "sessions");
            if (Directory.Exists(sessions))
            {
                var transcriptSource = new WindowsWorkspace(sessions);
                var transcript = Directory.CreateDirectory(Path.Combine(staging.FullName, "sessions"));
                var count = 0;
                foreach (var file in Directory.EnumerateFiles(sessions, "*.json"))
                {
                    token.ThrowIfCancellationRequested();
                    if (++count > before.SessionFiles)
                        throw new ToolDeniedException("snapshot_mismatch", "源会话目录已变化，快照未发布。");
                    Copy(transcriptSource, Path.GetFileName(file), transcript.FullName, ref remaining, token);
                }
            }
            var checkpoints = LegacyCheckpoints.Scan(legacy, staging.FullName, remaining, token);
            BackupDatabase(legacy, staging.FullName, remaining - checkpoints.Bytes, token);
            if (appearance is not null)
                File.WriteAllBytes(Path.Combine(staging.FullName, DesktopPreferences.FileName), appearance.Serialize());
            var after = LegacyStateAudit.Check(stagePath, token);
            if (after != before)
                throw new ToolDeniedException("snapshot_mismatch", "源历史在复制期间发生变化，快照未发布。");
            File.WriteAllText(Path.Combine(stagePath, "import-manifest.json"), JsonSerializer.Serialize(new
            {
                format_version = 1,
                imported_at_utc = DateTimeOffset.UtcNow,
                counts = after,
                qt_preferences_imported = appearance is not null,
            }));
            token.ThrowIfCancellationRequested();
            Directory.Move(stagePath, target.AbsolutePath);
            return new("imported", target.AbsolutePath, after, appearance is not null);
        }
        finally
        {
            // The stage remains private on failure. A failed import cannot publish a partially copied snapshot.
            if (Directory.Exists(stagePath))
                RemoveStage(stagePath, parent);
        }
    }

    private static bool Within(string parent, string path) =>
        path.Equals(parent, StringComparison.OrdinalIgnoreCase) ||
        path.StartsWith(Path.EndsInDirectorySeparator(parent) ? parent : parent + Path.DirectorySeparatorChar,
            StringComparison.OrdinalIgnoreCase);

    private static void Copy(WindowsWorkspace source, string name, string destination, ref long remaining, CancellationToken token)
    {
        using var lease = source.OpenPath(name);
        if (!WindowsWorkspace.FileExists(lease.AbsolutePath))
            return;
        using var input = File.Open(lease.AbsolutePath, FileMode.Open, FileAccess.Read, FileShare.Read);
        WindowsWorkspace.GetFileIdentity(input.SafeFileHandle, lease.AbsolutePath);
        var bytes = input.Length;
        if (bytes > MaxText || bytes > remaining)
            throw new ToolDeniedException("snapshot_limit", "旧历史超出导入大小限制。");
        remaining -= bytes;
        token.ThrowIfCancellationRequested();
        var path = Path.Combine(destination, name);
        using var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        if (name == "provider.json")
        {
            using var document = JsonDocument.Parse(input);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
                throw new ToolDeniedException("invalid_source", "旧版 provider.json 必须是 JSON 对象。");
            using var writer = new Utf8JsonWriter(output);
            writer.WriteStartObject();
            foreach (var key in ProviderKeys)
                if (document.RootElement.TryGetProperty(key, out var value) &&
                    (value.ValueKind is JsonValueKind.Number or JsonValueKind.True or JsonValueKind.False or JsonValueKind.Null ||
                     value.ValueKind == JsonValueKind.String && (key != "base_url" ||
                         (Uri.TryCreate(value.GetString(), UriKind.Absolute, out var uri) && uri.UserInfo.Length == 0 &&
                          uri.Query.Length == 0 && uri.Fragment.Length == 0))))
                {
                    writer.WritePropertyName(key);
                    value.WriteTo(writer);
                }
            writer.WriteEndObject();
        }
        else
        {
            input.CopyTo(output);
            output.Dispose();
            File.SetLastWriteTimeUtc(path, File.GetLastWriteTimeUtc(lease.AbsolutePath));
        }
    }

    private static void BackupDatabase(WindowsWorkspace source, string destination, long remaining, CancellationToken token)
    {
        using var database = source.OpenPath("runs.db");
        if (!WindowsWorkspace.FileExists(database.AbsolutePath))
            return;
        using var scratch = new LegacyDatabaseCopy(source, remaining, token);
        using var input = scratch.Connection();
        input.Open();
        using var output = OpenOutputDatabase(Path.Combine(destination, "runs.db"));
        input.BackupDatabase(output);
        using var query = output.CreateCommand();
        query.CommandText = "PRAGMA quick_check;";
        using var reader = query.ExecuteReader();
        if (!reader.Read() || reader.GetString(0) != "ok" || reader.Read())
            throw new ToolDeniedException("snapshot_invalid", "SQLite 快照未通过完整性检查。");
    }

    private static SqliteConnection OpenOutputDatabase(string path)
    {
        var database = new SqliteConnection(new SqliteConnectionStringBuilder
        {
            DataSource = path, Mode = SqliteOpenMode.ReadWriteCreate, Pooling = false, DefaultTimeout = 3,
        }.ToString());
        try
        {
            database.Open();
            using var query = database.CreateCommand();
            query.CommandText = "PRAGMA trusted_schema=OFF;";
            query.ExecuteNonQuery();
            return database;
        }
        catch
        {
            database.Dispose();
            throw;
        }
    }

    private static void RemoveStage(string stage, string expectedParent)
    {
        var folder = new DirectoryInfo(stage);
        if (string.Equals(folder.Parent?.FullName, expectedParent, StringComparison.OrdinalIgnoreCase) &&
            folder.Name.StartsWith("scidev-import-", StringComparison.Ordinal) &&
            (folder.Attributes & FileAttributes.ReparsePoint) == 0)
            folder.Delete(recursive: true);
    }
}
