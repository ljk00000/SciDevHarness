using Microsoft.Data.Sqlite;
using SciDev.Core;

namespace SciDev.Infrastructure;

// Work on a disposable copy so SQLite can open a WAL database without touching the source.
internal sealed class LegacyDatabaseCopy : IDisposable
{
    private readonly string _scratch;
    private readonly string _parent;

    public LegacyDatabaseCopy(WindowsWorkspace source, long remaining, CancellationToken token)
    {
        _parent = Path.GetFullPath(Path.GetTempPath());
        _scratch = Directory.CreateTempSubdirectory("scidev-legacy-db-").FullName;
        try
        {
            var sources = new List<(FileStream Input, string Name)>();
            try
            {
                foreach (var name in new[] { "runs.db", "runs.db-wal", "runs.db-journal" })
                {
                    using var lease = source.OpenPath(name);
                    if (!WindowsWorkspace.FileExists(lease.AbsolutePath))
                    {
                        if (name == "runs.db")
                            throw new ToolDeniedException("snapshot_mismatch", "源数据库在复制前消失。");
                        continue;
                    }

                    var input = File.Open(lease.AbsolutePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                    sources.Add((input, name));
                    WindowsWorkspace.GetFileIdentity(input.SafeFileHandle, lease.AbsolutePath);
                }
                var totalLength = sources.Sum(entry => entry.Input.Length);
                if (totalLength > remaining)
                    throw new ToolDeniedException("snapshot_limit", "旧 SQLite 历史超出导入大小限制。");
                foreach (var (input, name) in sources)
                {
                    token.ThrowIfCancellationRequested();
                    using var target = new FileStream(Path.Combine(_scratch, name), FileMode.CreateNew);
                    var length = input.Length;
                    input.CopyTo(target);
                    if (length != input.Length || length != target.Length)
                        throw new ToolDeniedException("snapshot_mismatch", "源数据库在复制期间发生变化。请先关闭旧程序。");
                }
            }
            finally
            {
                foreach (var (input, _) in sources)
                    input.Dispose();
            }
        }
        catch
        {
            Dispose();
            throw;
        }
    }

    public SqliteConnection Connection() => new(new SqliteConnectionStringBuilder
    {
        DataSource = Path.Combine(_scratch, "runs.db"), Mode = SqliteOpenMode.ReadWrite, Pooling = false, DefaultTimeout = 3,
    }.ToString());

    public void Dispose()
    {
        var folder = new DirectoryInfo(_scratch);
        if (folder.Exists && string.Equals(folder.Parent?.FullName, Path.TrimEndingDirectorySeparator(_parent),
            StringComparison.OrdinalIgnoreCase) && folder.Name.StartsWith("scidev-legacy-db-", StringComparison.Ordinal) &&
            (folder.Attributes & FileAttributes.ReparsePoint) == 0)
            folder.Delete(recursive: true);
    }
}
