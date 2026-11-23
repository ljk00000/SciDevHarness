using System.Buffers.Binary;
using System.Security.Cryptography;
using SciDev.Core;
using static SciDev.Infrastructure.WorkspaceFiles;

namespace SciDev.Infrastructure;

/// <summary>Bounded read of the local index for Agent file discovery. Never resolves linked metadata.</summary>
internal sealed class WorkspaceGitIndex
{
    private const int MaxIndexBytes = 8 * 1024 * 1024;
    private const int MaxEntries = 60_000;
    private const int MaxNameBytes = 8192;
    private const int SkipWorktree = 0x4000;
    private const int IntentToAdd = 0x2000;
    private readonly Dictionary<string, Entry> _files = new(StringComparer.OrdinalIgnoreCase);
    private readonly HashSet<string> _directories = new(StringComparer.OrdinalIgnoreCase);
    private readonly HashSet<string> _submodules = new(StringComparer.OrdinalIgnoreCase);

    internal sealed record Entry(string Path, byte[] ObjectId, bool Conflict, bool Sparse, bool Intent);
    internal string? Branch { get; private init; }
    internal string? Unavailable { get; private init; }
    internal IEnumerable<Entry> Entries => _files.Values;
    internal bool Tracks(string path, bool directory) => directory ? _directories.Contains(path) : _files.ContainsKey(path);
    internal bool IsSubmodule(string path) => _submodules.Contains(path);

    internal static WorkspaceGitIndex? Read(WindowsWorkspace workspace, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        try
        {
            using var marker = workspace.OpenOperatorPath(".git")!;
            FileAttributes attributes;
            try { attributes = File.GetAttributes(marker.AbsolutePath); }
            catch (FileNotFoundException) { return null; }
            if ((attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != FileAttributes.Directory)
                return new() { Unavailable = "索引位于链接工作树或受保护的 Git 元数据中" };
            var branch = ReadBranch(workspace);
            using var index = workspace.OpenOperatorPath(".git/index")!;
            if (!WindowsWorkspace.FileExists(index.AbsolutePath))
                return new() { Branch = branch };
            using var handle = WindowsWorkspace.OpenForRead(index.AbsolutePath, FileShare.Read);
            WindowsWorkspace.GetFileIdentity(handle, index.AbsolutePath);
            using var stream = new FileStream(handle, FileAccess.Read);
            if (stream.Length is < 32 or > MaxIndexBytes)
                return new() { Branch = branch, Unavailable = "索引超出读取限制或格式不完整" };
            var bytes = new byte[(int)stream.Length];
            stream.ReadExactly(bytes);
            return Parse(bytes, branch, token);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException)
        {
            return new() { Unavailable = "本地索引不可用" };
        }
    }

    private static string? ReadBranch(WindowsWorkspace workspace)
    {
        using var head = workspace.OpenOperatorPath(".git/HEAD")!;
        if (!WindowsWorkspace.FileExists(head.AbsolutePath))
            return null;
        try
        {
            using var snapshot = OpenSnapshot(head.AbsolutePath, FileShare.Read, maxBytes: 1024);
            var value = Utf8.GetString(snapshot.Bytes).TrimEnd('\r', '\n');
            const string prefix = "ref: refs/heads/";
            if (value.StartsWith(prefix, StringComparison.Ordinal) && value.Length is > 16 and <= 160 &&
                !value.Any(char.IsControl))
                return value[prefix.Length..];
            return value.Length is 40 or 64 && value.All(char.IsAsciiHexDigit) ? "分离 HEAD" : null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException or System.Text.DecoderFallbackException)
        {
            return null;
        }
    }

    internal static WorkspaceGitIndex Parse(ReadOnlySpan<byte> bytes, string? branch, CancellationToken token)
    {
        var length = HashSize(bytes);
        if (length == 0 || !bytes[..4].SequenceEqual("DIRC"u8))
            return new() { Branch = branch, Unavailable = "索引校验值或签名无效" };
        var version = BinaryPrimitives.ReadUInt32BigEndian(bytes[4..]);
        var count = BinaryPrimitives.ReadUInt32BigEndian(bytes[8..]);
        if (version is < 2 or > 4 || count > MaxEntries)
            return new() { Branch = branch, Unavailable = "索引版本或条目数量超出支持范围" };
        var data = bytes[..^length];
        var index = new WorkspaceGitIndex { Branch = branch };
        var offset = 12;
        byte[] previous = [];
        for (var number = 0; number < count; number++)
        {
            if ((number & 127) == 0)
                token.ThrowIfCancellationRequested();
            if (!index.TryEntry(data, length, version, ref offset, ref previous))
                return new() { Branch = branch, Unavailable = "索引条目格式不受支持" };
        }
        while (offset < data.Length)
        {
            if (data.Length - offset < 8 || data[offset] is < (byte)'A' or > (byte)'Z')
                return new() { Branch = branch, Unavailable = "拆分、稀疏或未知的索引扩展" };
            var size = BinaryPrimitives.ReadUInt32BigEndian(data[(offset + 4)..]);
            offset += 8;
            if (size > data.Length - offset)
                return new() { Branch = branch, Unavailable = "索引扩展不完整" };
            offset += (int)size;
        }
        return index;
    }

    private bool TryEntry(ReadOnlySpan<byte> data, int hashSize, uint version, ref int offset, ref byte[] previous)
    {
        var start = offset;
        const int statSize = 40;
        if (data.Length - offset < statSize + hashSize + 2)
            return false;
        var mode = BinaryPrimitives.ReadUInt32BigEndian(data[(offset + 24)..]);
        var hash = data.Slice(offset + statSize, hashSize);
        offset += statSize + hashSize;
        var flags = BinaryPrimitives.ReadUInt16BigEndian(data[offset..]);
        offset += 2;
        ushort extended = 0;
        if ((flags & 0x4000) != 0)
        {
            if (version == 2 || data.Length - offset < 2)
                return false;
            extended = BinaryPrimitives.ReadUInt16BigEndian(data[offset..]);
            offset += 2;
        }

        var retained = 0;
        if (version == 4)
        {
            var removed = ReadOffset(data, ref offset);
            if (removed < 0 || removed > previous.Length)
                return false;
            retained = previous.Length - removed;
        }
        var terminator = data[offset..Math.Min(data.Length, offset + MaxNameBytes + 1)].IndexOf((byte)0);
        var nameLength = terminator + retained;
        if (terminator < 0 || nameLength is < 1 or > MaxNameBytes ||
            (flags & 0x0fff) != Math.Min(nameLength, 0x0fff))
            return false;
        var name = new byte[nameLength];
        previous.AsSpan(0, retained).CopyTo(name);
        data.Slice(offset, terminator).CopyTo(name.AsSpan(retained));
        previous = name;
        offset += terminator + 1;
        if (version != 4)
        {
            var padding = (8 - (offset - start) % 8) % 8;
            if (data.Length - offset < padding || data.Slice(offset, padding).ContainsAnyExcept((byte)0))
                return false;
            offset += padding;
        }
        if (mode == 0x4000) // Sparse directory entries need a tree walk to enumerate.
            return false;
        AddPath(name, mode, hash, flags, extended);
        return true;
    }

    private void AddPath(byte[] name, uint mode, ReadOnlySpan<byte> hash, ushort flags, ushort extended)
    {
        string path;
        try { path = WorkspaceDocuments.VisiblePath(Utf8.GetString(name), allowHidden: false); }
        catch (Exception ex) when (ex is ToolDeniedException or System.Text.DecoderFallbackException) { return; }
        if (name.AsSpan().Contains((byte)'\\'))
            return;
        if (mode == 0xe000)
        {
            _submodules.Add(path);
            return;
        }
        if ((mode & 0xf000) != 0x8000)
            return;
        var entry = new Entry(path, hash.ToArray(), (flags & 0x3000) != 0, (extended & SkipWorktree) != 0,
            (extended & IntentToAdd) != 0);
        if (!_files.TryAdd(path, entry))
            _files[path] = _files[path] with { Conflict = true };
        for (var slash = path.LastIndexOf('/'); slash > 0; slash = path[..slash].LastIndexOf('/'))
            if (!_directories.Add(path[..slash]))
                break;
    }

    private static int ReadOffset(ReadOnlySpan<byte> bytes, ref int offset)
    {
        var value = 0;
        while (offset < bytes.Length && value <= MaxNameBytes)
        {
            var digit = bytes[offset++];
            value = (value << 7) + (digit & 0x7f);
            if ((digit & 0x80) == 0)
                return value;
            value++;
        }
        return -1;
    }

    private static int HashSize(ReadOnlySpan<byte> bytes)
    {
        if (bytes.Length >= 32 && SHA1.HashData(bytes[..^20]).AsSpan().SequenceEqual(bytes[^20..]))
            return 20;
        return bytes.Length >= 44 && SHA256.HashData(bytes[..^32]).AsSpan().SequenceEqual(bytes[^32..]) ? 32 : 0;
    }
}
