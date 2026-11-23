using System.Diagnostics;
using System.Globalization;
using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class GitPushCredentialScan
{
    internal static async Task<int> CheckAsync(GitReadContext git, string head, string remoteBase,
        string credential, string? modelKey, CancellationToken token)
    {
        var baseExists = await git.RunAsync(["--no-replace-objects", "cat-file", "-e", remoteBase + "^{commit}"], token, maxChars: 128);
        var revisions = baseExists.Code == 0 ? new[] { head, "^" + remoteBase } : [head];
        var listing = await git.RunAsync(["--no-replace-objects", "rev-list", "--objects", "--no-object-names", .. revisions],
            token, maxChars: 2_500_000);
        var ids = listing.Text.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (listing.Code != 0 || listing.Truncated || ids.Any(id => !GitHubPullRequestPlan.IsSha(id)))
            throw ScanFailed();
        if (ids.Length == 0)
            return 0;

        var keys = new[] { credential, modelKey }.Where(key => !string.IsNullOrEmpty(key)).Select(key => Encoding.UTF8.GetBytes(key!)).ToArray();
        await CheckObjectsAsync(git, ids, keys, token);
        return ids.Length;
    }

    private static async Task CheckObjectsAsync(GitReadContext git, string[] ids, byte[][] keys, CancellationToken token)
    {
        var start = git.PrepareSnapshotProcess();
        start.RedirectStandardInput = true;
        start.StandardInputEncoding = new UTF8Encoding(false);
        foreach (var arg in new[] { "--no-pager", "--no-replace-objects", "cat-file", "--batch" })
            start.ArgumentList.Add(arg);
        token.ThrowIfCancellationRequested();
        using var process = Process.Start(start) ?? throw ScanFailed();
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(token);
        budget.CancelAfter(TimeSpan.FromSeconds(90));
        var writing = WriteIdsAsync(process.StandardInput, ids, budget.Token);
        var draining = process.StandardError.BaseStream.CopyToAsync(Stream.Null, budget.Token);
        try
        {
            var reader = new BatchReader(process.StandardOutput.BaseStream);
            var overlap = keys.Max(key => key.Length) - 1;
            var data = new byte[16_384 + overlap];
            foreach (var id in ids)
            {
                var header = (await reader.HeaderAsync(budget.Token)).Split(' ');
                if (header.Length != 3 || header[0] != id ||
                    !long.TryParse(header[2], NumberStyles.None, CultureInfo.InvariantCulture, out var remaining))
                    throw ScanFailed();
                var retained = 0;
                while (remaining > 0)
                {
                    var length = (int)Math.Min(remaining, 16_384);
                    await reader.ReadExactlyAsync(data.AsMemory(retained, length), budget.Token);
                    var used = retained + length;
                    if (keys.Any(key => data.AsSpan(0, used).IndexOf(key) >= 0))
                        throw new ToolDeniedException("pr_credential", "待推送的 Git 对象包含当前 GitHub 或模型凭据，已停止发布。");
                    retained = Math.Min(overlap, used);
                    data.AsSpan(used - retained, retained).CopyTo(data);
                    remaining -= length;
                }
                if (await reader.HeaderAsync(budget.Token) != "")
                    throw ScanFailed();
            }
            await writing;
            await process.WaitForExitAsync(budget.Token);
            if (process.ExitCode != 0)
                throw ScanFailed();
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested) { throw ScanFailed(); }
        catch (IOException) { throw ScanFailed(); }
        finally
        {
            budget.Cancel();
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync(CancellationToken.None);
            }
            try { await Task.WhenAll(writing, draining); }
            catch (Exception ex) when (ex is IOException or OperationCanceledException) { }
        }
    }

    private static async Task WriteIdsAsync(StreamWriter writer, string[] ids, CancellationToken token)
    {
        await writer.WriteAsync(string.Join('\n', ids).AsMemory(), token);
        await writer.WriteAsync("\n".AsMemory(), token);
        writer.Close();
    }

    private static ToolDeniedException ScanFailed() =>
        new("pr_scan_incomplete", "无法完整检查待推送的 Git 对象。请核对本地对象和历史后重试。");

    private sealed class BatchReader(Stream input)
    {
        private readonly byte[] _buffer = new byte[8192];
        private readonly byte[] _header = new byte[128];
        private int _offset;
        private int _length;

        public async Task<string> HeaderAsync(CancellationToken token)
        {
            var used = 0;
            while (true)
            {
                await FillAsync(token);
                var newline = Array.IndexOf(_buffer, (byte)'\n', _offset, _length - _offset);
                var end = newline < 0 ? _length : newline;
                var count = end - _offset;
                if (used + count > _header.Length)
                    throw ScanFailed();
                _buffer.AsSpan(_offset, count).CopyTo(_header.AsSpan(used));
                used += count;
                _offset = end + (newline < 0 ? 0 : 1);
                if (newline >= 0)
                    return Encoding.ASCII.GetString(_header, 0, used);
            }
        }

        public async Task ReadExactlyAsync(Memory<byte> result, CancellationToken token)
        {
            while (result.Length > 0)
            {
                await FillAsync(token);
                var count = Math.Min(result.Length, _length - _offset);
                _buffer.AsMemory(_offset, count).CopyTo(result);
                _offset += count;
                result = result[count..];
            }
        }

        private async Task FillAsync(CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            if (_offset < _length)
                return;
            _offset = 0;
            _length = await input.ReadAsync(_buffer, token);
            if (_length == 0)
                throw ScanFailed();
        }
    }
}
