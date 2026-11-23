using System.Text.Json;
using Microsoft.Data.Sqlite;
using SciDev.Core;

namespace SciDev.Infrastructure;

// Read an explicit copy of a Python workspace. No schema or transcript writes occur.
public static class LegacyStateAudit
{
    public sealed record Result(string Status, int? SettingsVersion, bool ProviderConfigPresent,
        int SessionFiles, bool EventsFilePresent, long IndexedSessions, long IndexedEvents, long ToolJournalEntries,
        int Checkpoints = 0, int CheckpointBackups = 0, LegacyExecutionPolicy.Summary? Policy = null);

    public static Result Check(string workspaceCopy, CancellationToken token = default)
    {
        _ = new WindowsWorkspace(workspaceCopy);
        var folder = Path.Combine(workspaceCopy, ".research");
        if (!Directory.Exists(folder))
            return new("no_python_state", null, false, 0, false, 0, 0, 0);
        var state = new WindowsWorkspace(folder);

        var settingsVersion = SchemaVersion(state);
        var hasProvider = CheckFile(state, "provider.json");
        var hasEvents = CheckFile(state, "events.jsonl");
        var sessionFiles = CountSessions(folder);
        if (settingsVersion is < 0 or > 2)
            return new("unsupported_schema", settingsVersion, hasProvider, sessionFiles, hasEvents, 0, 0, 0);

        var policy = LegacyExecutionPolicy.Inspect(state);
        var checkpoints = LegacyCheckpoints.Scan(state, null, LegacyCheckpoints.MaxTotalBytes, token);
        using var dbPath = state.OpenPath("runs.db");
        if (!WindowsWorkspace.FileExists(dbPath.AbsolutePath))
            return new("ready_to_import", settingsVersion, hasProvider, sessionFiles, hasEvents, 0, 0, 0,
                checkpoints.Checkpoints, checkpoints.Backups, policy);
        using var scratch = new LegacyDatabaseCopy(state, LegacyCheckpoints.MaxTotalBytes, token);
        using var database = scratch.Connection();
        database.Open();
        using var query = database.CreateCommand();
        query.CommandText = "PRAGMA query_only=ON; PRAGMA trusted_schema=OFF;";
        query.ExecuteNonQuery();
        query.CommandText = "PRAGMA user_version;";
        if (Convert.ToInt64(query.ExecuteScalar()) > 2)
            return new("unsupported_schema", settingsVersion, hasProvider, sessionFiles, hasEvents, 0, 0, 0);
        return new("ready_to_import", settingsVersion, hasProvider, sessionFiles, hasEvents,
            Count(database, "sessions"), Count(database, "events"), Count(database, "tool_call_journal"),
            checkpoints.Checkpoints, checkpoints.Backups, policy);
    }

    private static long Count(SqliteConnection database, string table)
    {
        using var query = database.CreateCommand();
        query.CommandText = "SELECT count(*) FROM sqlite_schema WHERE name=$name AND type='table';";
        query.Parameters.AddWithValue("$name", table);
        if (Convert.ToInt64(query.ExecuteScalar()) == 0)
            return 0;
        query.CommandText = table switch
        {
            "sessions" => "SELECT count(*) FROM sessions;",
            "events" => "SELECT count(*) FROM events;",
            "tool_call_journal" => "SELECT count(*) FROM tool_call_journal;",
            _ => throw new ArgumentOutOfRangeException(nameof(table)),
        };
        return Convert.ToInt64(query.ExecuteScalar());
    }

    private static int? SchemaVersion(WindowsWorkspace state)
    {
        using var settings = state.OpenPath("settings.json");
        if (!WindowsWorkspace.FileExists(settings.AbsolutePath))
            return null;
        using var stream = File.OpenRead(settings.AbsolutePath);
        WindowsWorkspace.GetFileIdentity(stream.SafeFileHandle, settings.AbsolutePath);
        if (stream.Length > 1024 * 1024)
            throw new ToolDeniedException("invalid_source", "旧版项目设置超出读取上限。");
        using var document = JsonDocument.Parse(stream);
        if (document.RootElement.ValueKind != JsonValueKind.Object)
            throw new ToolDeniedException("invalid_source", "旧版设置不是 JSON 对象。");
        return document.RootElement.TryGetProperty("schema_version", out var version)
            ? version.ValueKind == JsonValueKind.Number && version.TryGetInt32(out var number)
                ? number : throw new ToolDeniedException("invalid_source", "旧版设置版本无效。")
            : null;
    }

    private static bool CheckFile(WindowsWorkspace state, string file)
    {
        using var path = state.OpenPath(file);
        if (!WindowsWorkspace.FileExists(path.AbsolutePath))
            return false;
        using var stream = File.Open(path.AbsolutePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        WindowsWorkspace.GetFileIdentity(stream.SafeFileHandle, path.AbsolutePath);
        return true;
    }

    private static int CountSessions(string folder)
    {
        var sessions = Path.Combine(folder, "sessions");
        if (!Directory.Exists(sessions))
            return 0;
        _ = new WindowsWorkspace(sessions);
        var count = 0;
        foreach (var session in new DirectoryInfo(sessions).EnumerateFiles("*.json", SearchOption.TopDirectoryOnly))
        {
            if ((session.Attributes & System.IO.FileAttributes.ReparsePoint) != 0 || count >= 10000)
                throw new ToolDeniedException("invalid_source", "旧版会话目录包含链接或超出清单上限。");
            count++;
        }
        return count;
    }
}
