using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Durable session events shared by workspace clients.</summary>
public sealed class RunJournal : IDisposable
{
    private static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
    private readonly string _id;
    private readonly string? _credential;
    private readonly WindowsWorkspace.Lease _path;
    private readonly FileStream _stream;
    private readonly StreamWriter _writer;

    public RunJournal(string workspace, string id, string? credential)
    {
        RunInspection.ValidateId(id);
        _id = id;
        _credential = credential;
        var state = new WindowsWorkspace(workspace).EnsureStateDirectory();
        var filename = $"csharp-{id}.jsonl";
        RelativePath = $".research/{filename}";
        _path = state.OpenPath(filename);
        try
        {
            _stream = new FileStream(_path.AbsolutePath, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
            try
            {
                WindowsWorkspace.GetFileIdentity(_stream.SafeFileHandle, _path.AbsolutePath);
                _writer = new StreamWriter(_stream, new UTF8Encoding(false)) { AutoFlush = true };
            }
            catch
            {
                _stream.Dispose();
                throw;
            }
        }
        catch
        {
            _path.Dispose();
            throw;
        }
    }

    public string RelativePath { get; }

    public void Record(RunEvent entry)
    {
        var line = RunJson.SerializeRedacted(new { runId = _id, timestamp = DateTimeOffset.UtcNow, entry.Type, entry.Turn, entry.Data }, Json, _credential);
        _writer.WriteLine(line);
        if (entry.Type is "execution_policy" or "task_submitted" or "session_attempt" or "image_attachments" or "task_summary" or "model_retry_scheduled" or "tool_started" or "tool_denied" or
            "tool_completed" or "task_plan_updated" or "verification_reported" or "verification_auto_started" or
            "verification_auto_completed" or "verification_auto_failed" or "run_finished" or "checkpoint_restore" or
            "outside_review_requested" or "outside_review_decided" or
            "command_review_requested" or "command_review_decided" or "command_auto_approved" or
            "host_review_requested" or "host_review_decided" or "host_command_starting" or "host_command_exited" or
            "git_review_requested" or "git_review_decided" or "git_review_delivered" or
            "git_task_commit_review_requested" or "git_task_commit_review_decided" or
            "git_task_commit_publishing" or "git_task_commit_finished" or
            "network_review_requested" or "network_review_decided" or
            "mcp_review_requested" or "mcp_review_decided" or "mcp_tool_executed" or
            "edit_review_requested" or "edit_review_approved" or "edit_review_denied")
            _stream.Flush(flushToDisk: true);
    }

    public void Dispose()
    {
        try { _writer.Dispose(); }
        finally { _path.Dispose(); }
    }
}
