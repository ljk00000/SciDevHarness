using System.ComponentModel;
using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Share the Python task's byte-range lock while continuing its work in C#.</summary>
internal sealed class LegacyCodingLease(WindowsWorkspace.Lease path, FileStream stream) : IDisposable
{
    public static LegacyCodingLease Acquire(WindowsWorkspace workspace)
    {
        if (!OperatingSystem.IsWindows())
            throw new PlatformNotSupportedException();
        using var git = workspace.OpenOperatorPath(".git")!;
        var gitDirectory = IsGitDirectory(git.AbsolutePath);
        WindowsWorkspace.Lease path;
        if (gitDirectory)
            path = workspace.OpenOperatorPath(".git/scidev-task.lock")!;
        else
        {
            var state = workspace.EnsureStateDirectory();
            using var directory = state.OpenPath("locks");
            Directory.CreateDirectory(directory.AbsolutePath);
            path = state.OpenPath("locks/task.lock");
        }

        FileStream? file = null;
        try
        {
            file = Open(path.AbsolutePath);
            WindowsWorkspace.GetFileIdentity(file.SafeFileHandle, path.AbsolutePath);
            file.Lock(0, 1);
            return new(path, file);
        }
        catch (IOException ex) when ((ex.HResult & 0xFFFF) is 32 or 33 ||
            ex.InnerException is Win32Exception { NativeErrorCode: 32 or 33 })
        {
            file?.Dispose();
            path.Dispose();
            throw new ToolDeniedException("legacy_session_busy", "Python 版正在运行此工作区的任务。等任务结束后再从旧会话续跑。");
        }
        catch
        {
            file?.Dispose();
            path.Dispose();
            throw;
        }
    }

    private static FileStream Open(string path)
    {
        const FileShare share = FileShare.ReadWrite;
        if (!WindowsWorkspace.FileExists(path))
        {
            try
            {
                var file = new FileStream(path, FileMode.CreateNew, FileAccess.ReadWrite, share);
                try
                {
                    file.WriteByte(0);
                    file.Flush();
                    return file;
                }
                catch { file.Dispose(); throw; }
            }
            catch (IOException error) when ((error.HResult & 0xFFFF) is 80 or 183) { }
        }
        return new FileStream(WindowsWorkspace.OpenForRead(path, share), FileAccess.Read);
    }

    private static bool IsGitDirectory(string path)
    {
        try
        {
            var attributes = File.GetAttributes(path);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
                throw new ToolDeniedException("path_denied", "旧任务锁的 Git 路径不能是链接。");
            return (attributes & FileAttributes.Directory) != 0;
        }
        catch (FileNotFoundException) { return false; }
    }

    public void Dispose()
    {
        stream.Dispose();
        path.Dispose();
    }
}
