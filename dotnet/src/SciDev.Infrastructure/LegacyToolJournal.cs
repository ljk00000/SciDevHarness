using Microsoft.Data.Sqlite;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class LegacyToolJournal
{
    public sealed record Row(int MessageIndex, int CallIndex, string Id, string Name, string Status, string Result);

    public static Row[] Read(WindowsWorkspace workspace, string id, CancellationToken token)
    {
        using var path = workspace.OpenOperatorPath(".research/runs.db", allowMissingParents: true);
        if (path is null || !WindowsWorkspace.FileExists(path.AbsolutePath))
            return [];
        try
        {
            using var copy = new LegacyDatabaseCopy(new WindowsWorkspace(Path.GetDirectoryName(path.AbsolutePath)!),
                LegacyCheckpoints.MaxTotalBytes, token);
            using var db = copy.Connection();
            db.Open();
            using var query = db.CreateCommand();
            query.CommandText = "PRAGMA trusted_schema=OFF; PRAGMA query_only=ON;";
            query.ExecuteNonQuery();
            query.CommandText = "SELECT 1 FROM sqlite_schema WHERE name='tool_call_journal' AND type='table';";
            if (query.ExecuteScalar() is null)
                return [];
            query.CommandText = """
                SELECT assistant_message_index, call_index, substr(call_id,1,257), substr(name,1,121),
                    substr(status,1,32), substr(result,1,30001)
                FROM tool_call_journal WHERE session_id=$id
                ORDER BY assistant_message_index, call_index LIMIT 1025;
                """;
            query.Parameters.AddWithValue("$id", id);
            var rows = new List<Row>();
            var characters = 0;
            using var reader = query.ExecuteReader();
            while (reader.Read())
            {
                token.ThrowIfCancellationRequested();
                var row = new Row(reader.GetInt32(0), reader.GetInt32(1), reader.GetString(2), reader.GetString(3),
                    reader.GetString(4), reader.GetString(5));
                characters += row.Result.Length;
                if (rows.Count >= 1024 || row.Result.Length > 30_000 || characters > 2_000_000)
                    throw new ToolDeniedException("legacy_session_limit", "旧工具执行日志超出续跑上限。");
                rows.Add(row);
            }
            return rows.ToArray();
        }
        catch (Exception error) when (error is SqliteException or InvalidCastException or OverflowException)
        {
            throw new ToolDeniedException("legacy_session_invalid", "无法核对旧版 SQLite 工具执行记录。", error);
        }
    }
}
