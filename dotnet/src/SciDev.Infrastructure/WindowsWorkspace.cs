using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class WindowsWorkspace
{
    private readonly string _root;

    public WindowsWorkspace(string root)
    {
        if (!OperatingSystem.IsWindows())
            throw new PlatformNotSupportedException();
        _root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        var drive = Path.GetPathRoot(_root);
        if (drive is null || drive.Length != 3 || drive[1] != ':' || _root == drive || !Directory.Exists(_root))
            throw new ArgumentException("请指定本机磁盘上已存在的普通工作区。");

        for (DirectoryInfo? dir = new(_root); dir is not null; dir = dir.Parent)
            if ((dir.Attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != FileAttributes.Directory)
                throw new ToolDeniedException("path_denied", "工作区及其祖先目录不可指向链接。");
        using var validation = HoldDirectories([_root]);
    }

    public Lease OpenPath(string relative)
    {
        return OpenWithinRoot(_root, PathParts(relative));
    }

    internal static string NormalizeOperatorPath(string path) => string.Join('/', PathParts(path, allowHidden: true));

    internal Lease? OpenOperatorPath(string relative, bool allowMissingParents = false)
    {
        try { return OpenWithinRoot(_root, PathParts(relative, allowHidden: true)); }
        catch (DirectoryNotFoundException) when (allowMissingParents) { return null; }
    }

    internal static string ValidateAbsoluteFilePath(string path)
    {
        if (path.Length is < 4 or > 243 || !char.IsAsciiLetter(path[0]) ||
            path[1] != ':' || path[2] is not ('\\' or '/'))
            throw new ToolDeniedException("path_denied", "区外操作需要本机磁盘上的完整文件路径。");
        return char.ToUpperInvariant(path[0]) + @":\" + string.Join('\\', PathParts(path[3..], allowHidden: true));
    }

    internal static Lease OpenAbsoluteFilePath(string path)
    {
        var absolute = ValidateAbsoluteFilePath(path);
        var drive = absolute[..3];
        if (new DriveInfo(drive).DriveType != DriveType.Fixed)
            throw new ToolDeniedException("path_denied", "区外文件必须位于本机固定磁盘。");
        return OpenWithinRoot(drive, PathParts(absolute[3..], allowHidden: true));
    }

    private static string[] PathParts(string relative, bool allowHidden = false)
    {
        var parts = relative.Replace('\\', '/').Split('/');
        if (parts.Length > 32 || relative.Length > 240 || parts.Any(part =>
            part.Length == 0 || part is "." or ".." || (!allowHidden && part[0] == '.') || part != part.Trim() || part.EndsWith('.') ||
            part.Any(ch => char.IsControl(ch) || Path.GetInvalidFileNameChars().Contains(ch)) ||
            IsDeviceName(part)))
            throw new ToolDeniedException("path_denied", "只允许当前工作区内的普通相对路径。");
        return parts;
    }

    private static Lease OpenWithinRoot(string root, string[] parts)
    {
        var parents = new List<string> { root };
        var current = root;
        foreach (var part in parts[..^1])
        {
            current = Path.Combine(current, part);
            parents.Add(current);
        }

        var lease = HoldDirectories(parents);
        lease.AbsolutePath = Path.Combine(current, parts[^1]);
        lease.RelativePath = string.Join('/', parts);
        return lease;
    }

    internal WindowsWorkspace EnsureStateDirectory()
    {
        using var lease = HoldDirectories([_root]);
        var path = Path.Combine(_root, ".research");
        Directory.CreateDirectory(path);
        return new WindowsWorkspace(path);
    }

    internal static SafeFileHandle OpenForRecovery(string path, bool delete)
    {
        const uint read = 0x80000000, write = 0x40000000, deleteAccess = 0x00010000;
        return OpenFile(path, read | (delete ? deleteAccess : write), FileShare.None);
    }

    internal static SafeFileHandle OpenForRead(string path, FileShare share) => OpenFile(path, 0x80000000, share);

    private static SafeFileHandle OpenFile(string path, uint access, FileShare share)
    {
        const uint openExisting = 3, openReparsePoint = 0x00200000;
        var handle = CreateFileW(@"\\?\" + path, access, (uint)share,
            IntPtr.Zero, openExisting, openReparsePoint, IntPtr.Zero);
        if (!handle.IsInvalid)
            return handle;
        var error = Marshal.GetLastWin32Error();
        handle.Dispose();
        throw new IOException("无法以请求的权限打开已核对的文件。", new Win32Exception(error));
    }

    internal static void DeleteOnClose(SafeFileHandle handle)
    {
        const int fileDispositionInfo = 4;
        byte deleteFile = 1;
        if (!SetFileInformationByHandle(handle, fileDispositionInfo, ref deleteFile, 1))
            throw new IOException("无法删除已核对的任务文件。", new Win32Exception(Marshal.GetLastWin32Error()));
    }

    internal static void RenameWithoutReplace(SafeFileHandle handle, string destination)
    {
        const int fileRenameInfo = 3;
        var name = Encoding.Unicode.GetBytes(destination + "\0");
        var nameOffset = Marshal.OffsetOf<RenameInformation>(nameof(RenameInformation.FileName)).ToInt32();
        var lengthOffset = Marshal.OffsetOf<RenameInformation>(nameof(RenameInformation.FileNameLength)).ToInt32();
        var length = nameOffset + name.Length;
        var buffer = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.Copy(new byte[nameOffset], 0, buffer, nameOffset);
            Marshal.WriteInt32(buffer, lengthOffset, name.Length - sizeof(char));
            Marshal.Copy(name, 0, buffer + nameOffset, name.Length);
            if (SetFileInformationByHandle(handle, fileRenameInfo, buffer, (uint)length))
                return;
            var error = Marshal.GetLastWin32Error();
            if (error is 80 or 183)
                throw new ToolDeniedException("rename_conflict", "重命名目标已存在。");
            throw new IOException("无法重命名已核对的文件。", new Win32Exception(error));
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    public static bool FileExists(string path)
    {
        try
        {
            var attributes = File.GetAttributes(path);
            if ((attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint | FileAttributes.Device)) != 0)
                throw new ToolDeniedException("path_denied", "只允许普通文件；不跟随链接。");
            return true;
        }
        catch (FileNotFoundException) { return false; }
    }

    public static FileIdentity GetFileIdentity(SafeFileHandle handle, string path, bool requireSingleLink = true)
    {
        if (!GetFileInformationByHandle(handle, out var info) || (requireSingleLink && info.NumberOfLinks != 1) ||
            ((FileAttributes)info.Attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint | FileAttributes.Device)) != 0)
            throw new ToolDeniedException("path_denied", "无法确认文件是普通文件且只有一个链接，拒绝操作。");
        RequireCanonicalPath(handle, path);
        return new(info.VolumeSerial, info.IndexHigh, info.IndexLow);
    }

    private static Lease HoldDirectories(IEnumerable<string> paths)
    {
        var lease = new Lease();
        try
        {
            foreach (var path in paths)
            {
                // Keep the validated parent open without sharing delete while acting on its files.
                var handle = CreateFileW(@"\\?\" + path, 0x81, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
                lease.Parents.Add(handle);
                if (handle.IsInvalid && Marshal.GetLastWin32Error() is 2 or 3)
                    throw new DirectoryNotFoundException("文件的父目录已不存在。");
                if (handle.IsInvalid || !GetFileInformationByHandle(handle, out var info))
                    throw new ToolDeniedException("path_denied", $"无法安全打开父目录（Windows {Marshal.GetLastWin32Error()}）。");
                if (((FileAttributes)info.Attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != FileAttributes.Directory)
                    throw new ToolDeniedException("path_denied", "父目录必须可访问且不可指向链接。");
                RequireCanonicalPath(handle, path);
            }
            return lease;
        }
        catch
        {
            lease.Dispose();
            throw;
        }
    }

    private static void RequireCanonicalPath(SafeFileHandle handle, string expected)
    {
        var buffer = new StringBuilder(32768);
        var length = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
        if (length == 0 || length >= buffer.Capacity)
            throw new IOException("无法获取目标的最终路径。", new Win32Exception(Marshal.GetLastWin32Error()));
        if (!string.Equals(Path.TrimEndingDirectorySeparator(buffer.ToString()),
                           Path.TrimEndingDirectorySeparator(@"\\?\" + expected),
                           StringComparison.OrdinalIgnoreCase))
            throw new ToolDeniedException("path_denied", "目标的最终路径与请求不符。");
    }

    private static bool IsDeviceName(string part)
    {
        var stem = part.Split('.')[0].TrimEnd().ToUpperInvariant();
        return stem is "CON" or "PRN" or "AUX" or "NUL" or "CONIN$" or "CONOUT$" ||
            (stem.Length == 4 && (stem.StartsWith("COM", StringComparison.Ordinal) || stem.StartsWith("LPT", StringComparison.Ordinal)) &&
                "0123456789¹²³".Contains(stem[3]));
    }

    internal readonly record struct FileIdentity(uint Volume, uint IndexHigh, uint IndexLow);

    internal sealed class Lease : IDisposable
    {
        public List<SafeFileHandle> Parents { get; } = [];
        public string AbsolutePath { get; set; } = "";
        public string RelativePath { get; set; } = "";
        public void Dispose()
        {
            for (var i = Parents.Count - 1; i >= 0; i--)
                Parents[i].Dispose();
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileInformation
    {
        public uint Attributes, CreationLow, CreationHigh, AccessLow, AccessHigh, WriteLow, WriteHigh;
        public uint VolumeSerial, SizeHigh, SizeLow, NumberOfLinks, IndexHigh, IndexLow;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct RenameInformation
    {
        public uint Flags;
        public IntPtr RootDirectory;
        public uint FileNameLength;
        public char FileName;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    private static extern SafeFileHandle CreateFileW(string filename, uint access, uint share, IntPtr attributes,
        uint mode, uint flags, IntPtr template);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true, ExactSpelling = true)]
    private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder name, uint capacity, uint flags);

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInformation information);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle handle, int infoClass, ref byte value, uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle handle, int infoClass, IntPtr value, uint size);
}
