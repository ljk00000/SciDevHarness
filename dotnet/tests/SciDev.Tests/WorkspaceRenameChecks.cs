using System.Text.Json;
using SciDev.Core;
using SciDev.Infrastructure;

internal static class WorkspaceRenameChecks
{
    internal static async Task RunAsync(DirectoryInfo fixture)
    {
        var root = fixture.FullName;
        var id = Guid.NewGuid().ToString("N");
        using var journal = new RunJournal(root, id, null);
        var checkpoint = new WorkspaceCheckpoint(root, id);
        const string source = "before.bin", title = "子目录", target = "子目录/after.bin";
        fixture.CreateSubdirectory(title);
        var original = new byte[] { 0xEF, 0xBB, 0xBF, 0x00, 0x0D, 0x0A, 0xFF };
        string Disk(string path) => Path.Combine(root, path);
        File.WriteAllBytes(Disk(source), original);

        var move = Rename(source, target);
        await Denied(new WorkspaceFiles(root, checkpoint: checkpoint), move, "approval_denied");
        await Denied(new WorkspaceFiles(root, new Review(_ => true), checkpoint, ExecutionMode.ReadOnly), move, "read_only");
        await Denied(new WorkspaceFiles(root, checkpoint: checkpoint), Rename(source, "../outside.bin"), "path_denied");
        await Denied(new WorkspaceFiles(root, checkpoint: checkpoint), Rename(source, "BEFORE.bin"), "rename_conflict");
        Assert(File.Exists(Disk(source)) && !File.Exists(Disk(target)) && checkpoint.CheckpointId is null,
            "denials leave both names and checkpoint intact");

        var sourceRace = new Review(_ =>
        {
            File.WriteAllText(Disk(source), "user source edit");
            return true;
        });
        await Denied(new WorkspaceFiles(root, sourceRace, checkpoint), move, "edit_conflict");
        Assert(File.ReadAllText(Disk(source)) == "user source edit" && !File.Exists(Disk(target)),
            "source changes during approval are preserved");
        File.WriteAllBytes(Disk(source), original);
        var targetRace = new Review(_ =>
        {
            File.WriteAllText(Disk(target), "user target edit");
            return true;
        });
        await Denied(new WorkspaceFiles(root, targetRace, checkpoint), move, "rename_conflict");
        Assert(File.ReadAllText(Disk(target)) == "user target edit" && File.ReadAllBytes(Disk(source)).SequenceEqual(original),
            "pre-existing and intervening targets are never overwritten");
        File.Delete(Disk(target));

        var approved = new Review(preview => preview.Path == source && preview.Destination == target);
        var tools = new WorkspaceFiles(root, approved, checkpoint);
        var result = await tools.ExecuteAsync(move, default);
        Assert(result.Change == new FileChange(source, "delete") &&
            result.RelatedChanges?.SequenceEqual([new FileChange(target, "write")]) == true &&
            !File.Exists(Disk(source)) && File.ReadAllBytes(Disk(target)).SequenceEqual(original),
            "one reviewed cross-directory move records both effects and preserves bytes");

        File.WriteAllText(Disk(source), "recreated source");
        var conflict = checkpoint.Restore(default);
        Assert(conflict.Status == "restore_conflict" && conflict.Failed.Count == 2 &&
            File.ReadAllText(Disk(source)) == "recreated source" && File.ReadAllBytes(Disk(target)).SequenceEqual(original),
            "a recreated source blocks recovery for the whole pair");
        File.Delete(Disk(source));
        var targetEdited = new byte[] { 6, 5, 4 };
        File.WriteAllBytes(Disk(target), targetEdited);
        Assert(checkpoint.Restore(default).Status == "restore_conflict" && !File.Exists(Disk(source)) &&
            File.ReadAllBytes(Disk(target)).SequenceEqual(targetEdited), "a changed destination blocks recovery for the whole pair");
        File.WriteAllBytes(Disk(target), original);
        var restored = checkpoint.Restore(default);
        Assert(restored.Status == "restored" && restored.Restored.SequenceEqual([source]) &&
            restored.Deleted.SequenceEqual([target]) && File.ReadAllBytes(Disk(source)).SequenceEqual(original) &&
            !File.Exists(Disk(target)) && checkpoint.Restore(default).Restored.Length == 0, "recovery is repeatable");

        var chain = fixture.CreateSubdirectory("chain").FullName;
        var chainId = Guid.NewGuid().ToString("N");
        using var chainJournal = new RunJournal(chain, chainId, null);
        var chainCheckpoint = new WorkspaceCheckpoint(chain, chainId);
        var chainTools = new WorkspaceFiles(chain, new Review(_ => true), chainCheckpoint);
        File.WriteAllText(Path.Combine(chain, "a.txt"), "original");
        await chainTools.ExecuteAsync(Call("apply_patch", new { path = "a.txt", old_text = "original", new_text = "edited" }), default);
        await chainTools.ExecuteAsync(Rename("a.txt", "b.txt"), default);
        await Denied(chainTools, Call("replace_in_file", new { path = "b.txt", old_text = "edited", new_text = "unapproved" }), "approval_required");
        await chainTools.ExecuteAsync(Rename("b.txt", "c.txt"), default);
        await chainTools.ExecuteAsync(Call("write_file", new { path = "draft.txt", content = "draft" }), default);
        await chainTools.ExecuteAsync(Rename("draft.txt", "final.txt"), default);
        await chainTools.ExecuteAsync(Call("replace_in_file", new { path = "final.txt", old_text = "draft", new_text = "final" }), default);
        Assert(chainCheckpoint.Restore(default).Status == "restored" &&
            File.ReadAllText(Path.Combine(chain, "a.txt")) == "original" &&
            Directory.EnumerateFiles(chain, "*.txt").Count() == 1,
            "mixed legacy edits, chained moves and task-owned files recover to the same starting state");
    }

    internal static ToolCall Rename(string path, string destination) => Call("rename_file", new { path, destination });

    private static ToolCall Call(string name, object arguments) => new(Guid.NewGuid().ToString("N"), name, JsonSerializer.Serialize(arguments));

    private static async Task Denied(WorkspaceFiles tools, ToolCall call, string expected)
    {
        try { await tools.ExecuteAsync(call, default); }
        catch (ToolDeniedException ex) when (ex.Status == expected) { return; }
        throw new InvalidOperationException($"Expected {call.Name} to fail with {expected}.");
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }

    private sealed class Review(Func<FileEditPreview, bool> decide) : IFileEditApproval
    {
        public ValueTask<bool> ApproveAsync(FileEditPreview preview, CancellationToken token) => new(decide(preview));
    }
}
