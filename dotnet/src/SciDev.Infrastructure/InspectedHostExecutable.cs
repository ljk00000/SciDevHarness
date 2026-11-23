using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class InspectedHostExecutable : IDisposable
{
    private readonly WindowsWorkspace.Lease _path;
    private readonly FileStream _file;
    private readonly WindowsWorkspace.FileIdentity _identity;

    private InspectedHostExecutable(WindowsWorkspace.Lease path, FileStream file, WindowsWorkspace.FileIdentity identity, string hash)
    {
        _path = path;
        _file = file;
        _identity = identity;
        Sha256 = hash;
    }

    public string Path => _path.AbsolutePath;
    public string Sha256 { get; }

    public static async Task<InspectedHostExecutable> OpenAsync(string executable, CancellationToken token)
    {
        var path = WindowsWorkspace.OpenAbsoluteFilePath(executable);
        SafeFileHandle? handle = null;
        FileStream? file = null;
        try
        {
            handle = WindowsWorkspace.OpenForRead(path.AbsolutePath, FileShare.Read);
            var identity = WindowsWorkspace.GetFileIdentity(handle, path.AbsolutePath, requireSingleLink: false);
            // The stream takes ownership of the handle after inspection succeeds.
            file = new FileStream(handle, FileAccess.Read);
            if (file.Length is < 2 or > 128 * 1024 * 1024 || file.ReadByte() != 'M' || file.ReadByte() != 'Z')
                throw new ToolDeniedException("host_command_executable", "此工具需要普通的 Windows 可执行文件（EXE，最多 128 MiB）。");
            file.Position = 0;
            var hash = Convert.ToHexString(await SHA256.HashDataAsync(file, token));
            return new(path, file, identity, hash);
        }
        catch
        {
            file?.Dispose();
            handle?.Dispose();
            path.Dispose();
            throw;
        }
    }

    public void VerifyUnchanged()
    {
        if (_identity != WindowsWorkspace.GetFileIdentity(_file.SafeFileHandle, Path, requireSingleLink: false))
            throw new ToolDeniedException("host_command_changed", "待执行文件的身份在审批期间发生变化。");
    }

    public void Dispose()
    {
        _file.Dispose();
        _path.Dispose();
    }
}
