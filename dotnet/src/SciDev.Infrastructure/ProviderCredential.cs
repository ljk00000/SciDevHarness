using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed record SavedProvider(string Address, string Model, string Profile)
{
    public static SavedProvider Load(string workspace)
    {
        _ = new WindowsWorkspace(workspace);
        var state = Path.Combine(workspace, ".research");
        if (!Directory.Exists(state))
            throw new ToolDeniedException("provider_not_found", "当前工作区没有已保存的模型配置。");
        using var path = new WindowsWorkspace(state).OpenPath("provider.json");
        if (!WindowsWorkspace.FileExists(path.AbsolutePath))
            throw new ToolDeniedException("provider_not_found", "当前工作区没有已保存的模型配置。");
        using var file = WorkspaceFiles.OpenSnapshot(path.AbsolutePath, FileShare.Read, maxBytes: 128 * 1024);
        using var json = JsonDocument.Parse(file.Bytes.AsMemory(file.Bytes.AsSpan().StartsWith(Encoding.UTF8.Preamble) ? 3 : 0));
        var root = json.RootElement;
        if (root.ValueKind != JsonValueKind.Object)
            throw new ToolDeniedException("provider_invalid", "已保存的模型配置格式无效。");
        var address = Get("base_url")?.Trim();
        _ = WorkspaceRunner.EndpointFor(address);
        var model = Get("model")?.Trim();
        if (string.IsNullOrEmpty(model) || model.Length > 256 || model.Any(char.IsControl))
            throw new ToolDeniedException("provider_invalid", "已保存的模型 ID 无效。");
        return new(address!, model, ProviderCredential.NormalizeProfile(Get("credential_profile") ?? "default"));

        string? Get(string name) => root.TryGetProperty(name, out var item) && item.ValueKind == JsonValueKind.String
            ? item.GetString() : null;
    }
}

public static class ProviderCredential
{
    private const string TargetPrefix = "SciDevHarness/provider/";

    public static string? Read(string profile)
    {
        if (!OperatingSystem.IsWindows())
            throw new PlatformNotSupportedException();
        if (!CredReadW(TargetPrefix + NormalizeProfile(profile), 1, 0, out var pointer))
        {
            var error = Marshal.GetLastWin32Error();
            if (error == 1168)
                return null;
            throw new ToolDeniedException("credential_unavailable", $"Windows 凭据管理器不可用（Windows {error}）。");
        }

        try
        {
            var credential = Marshal.PtrToStructure<NativeCredential>(pointer);
            if (credential.BlobSize == 0 || credential.Blob == IntPtr.Zero)
                return null;
            if (credential.BlobSize > 2560)
                throw new ToolDeniedException("credential_invalid", "Windows 模型凭据长度无效。");
            var bytes = new byte[credential.BlobSize];
            Marshal.Copy(credential.Blob, bytes, 0, bytes.Length);
            try
            {
                try { return new UnicodeEncoding(false, false, true).GetString(bytes); }
                catch (DecoderFallbackException) { return Encoding.UTF8.GetString(bytes); }
            }
            finally { CryptographicOperations.ZeroMemory(bytes); }
        }
        finally { CredFree(pointer); }
    }

    public static string NormalizeProfile(string profile)
    {
        var cleaned = new StringBuilder();
        var count = 0;
        foreach (var symbol in profile.EnumerateRunes())
        {
            var category = Rune.GetUnicodeCategory(symbol);
            if (!Rune.IsLetterOrDigit(symbol) && category is not (UnicodeCategory.LetterNumber or UnicodeCategory.OtherNumber) &&
                symbol.Value is not ('-' or '_'))
                continue;
            cleaned.Append(symbol.ToString());
            if (++count == 60)
                break;
        }
        return count == 0 ? "default" : cleaned.ToString();
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct NativeCredential
    {
        public uint Flags, Type;
        public IntPtr Target, Comment;
        public uint LastWrittenLow, LastWrittenHigh, BlobSize;
        public IntPtr Blob;
        public uint Persist, AttributeCount;
        public IntPtr Attributes, TargetAlias, UserName;
    }

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredReadW(string target, uint type, uint flags, out IntPtr credential);

    [DllImport("advapi32.dll")]
    private static extern void CredFree(IntPtr credential);
}
