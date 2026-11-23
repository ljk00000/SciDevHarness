using System.Diagnostics;
using System.Security.Cryptography;

namespace SciDev.Infrastructure;

public sealed record VerifiedUpdate(string Path, string Sha256, long Size);

internal static class UpdateAssetFiles
{
    internal static async Task<VerifiedUpdate> SaveAsync(string target, Stream body, long expectedSize, string expectedSha256,
        IProgress<(long Done, long Total)>? progress, CancellationToken token)
    {
        var temporary = Path.Combine(Path.GetDirectoryName(target)!, $".scidev-update-{Guid.NewGuid():N}.part");
        var ownsTemporary = false;
        try
        {
            var buffer = new byte[64 * 1024];
            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            long size = 0;
            await using (var output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                buffer.Length, FileOptions.Asynchronous | FileOptions.SequentialScan))
            {
                ownsTemporary = true;
                int count;
                while ((count = await body.ReadAsync(buffer, token)) != 0)
                {
                    size += count;
                    if (size > expectedSize || size > PreviewUpdates.DownloadLimit)
                        throw PreviewUpdates.Error("安装包超过发布声明的大小。");
                    hash.AppendData(buffer, 0, count);
                    await output.WriteAsync(buffer.AsMemory(0, count), token);
                    progress?.Report((size, expectedSize));
                }
                await output.FlushAsync(token);
                output.Flush(flushToDisk: true);
            }

            var digest = Convert.ToHexStringLower(hash.GetHashAndReset());
            if (size == 0 || size != expectedSize || digest != expectedSha256)
                throw PreviewUpdates.Error("安装包大小或 SHA-256 与发布信息不一致。");
            token.ThrowIfCancellationRequested();
            File.Move(temporary, target, overwrite: false);
            ownsTemporary = false;
            return new(target, digest, size);
        }
        finally
        {
            if (ownsTemporary)
                try { File.Delete(temporary); }
                catch (IOException) { }
        }
    }

    internal static async Task<int> LaunchAsync(VerifiedUpdate package, Func<string, int> start, CancellationToken token)
    {
        var digest = PreviewUpdates.NormalizeDigest(package.Sha256);
        if (digest is null || package.Size is <= 0 or > PreviewUpdates.DownloadLimit ||
            !Path.GetExtension(package.Path).Equals(".exe", StringComparison.OrdinalIgnoreCase))
            throw PreviewUpdates.Error("安装包校验信息无效。");
        using var target = WindowsWorkspace.OpenAbsoluteFilePath(package.Path);
        using var handle = WindowsWorkspace.OpenForRead(target.AbsolutePath, FileShare.Read);
        WindowsWorkspace.GetFileIdentity(handle, target.AbsolutePath);
        using var input = new FileStream(handle, FileAccess.Read);
        if (input.Length != package.Size || Convert.ToHexStringLower(await SHA256.HashDataAsync(input, token)) != digest)
            throw PreviewUpdates.Error("安装包在启动前的复核失败。");
        token.ThrowIfCancellationRequested();
        return start(target.AbsolutePath);
    }

    internal static int StartInstaller(string path)
    {
        var start = new ProcessStartInfo(path)
        {
            UseShellExecute = false,
            WorkingDirectory = Environment.SystemDirectory,
        };
        foreach (var name in start.Environment.Keys.Where(name => name.StartsWith("SCIDEV_", StringComparison.OrdinalIgnoreCase) ||
            name is "GH_TOKEN" or "GITHUB_TOKEN" || name.EndsWith("_API_KEY", StringComparison.OrdinalIgnoreCase)).ToArray())
            start.Environment.Remove(name);
        using var process = Process.Start(start) ?? throw PreviewUpdates.Error("无法启动安装器。");
        return process.Id;
    }
}
