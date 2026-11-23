namespace SciDev.Core;

public sealed class ImageAttachment
{
    public const int MaxImages = 4;
    public const int MaxImageBytes = 5 * 1024 * 1024;
    public const int MaxTotalBytes = 12 * 1024 * 1024;

    private ImageAttachment(string name, string format, int byteCount, string dataUrl) =>
        (Name, Format, ByteCount, DataUrl) = (name, format, byteCount, dataUrl);

    public string Name { get; }
    public string Format { get; }
    public int ByteCount { get; }
    public string DataUrl { get; }

    public static ImageAttachment FromBytes(ReadOnlySpan<byte> data, string name)
    {
        if (data.Length is 0 or > MaxImageBytes)
            throw new ArgumentException("图片大小必须在 1 字节到 5 MiB 之间。");
        var format = DetectFormat(data);
        var filename = Path.GetFileName(name).Trim();
        filename = string.Concat(filename.EnumerateRunes().Take(80));
        return new(filename.Length == 0 ? "image" : filename, format, data.Length,
            $"data:{format};base64,{Convert.ToBase64String(data)}");
    }

    public static void Validate(int count, long totalBytes)
    {
        if (count > MaxImages)
            throw new ArgumentException("最多附加 4 张图片。");
        if (totalBytes > MaxTotalBytes)
            throw new ArgumentException("图片总大小不能超过 12 MiB。");
    }

    private static string DetectFormat(ReadOnlySpan<byte> data)
    {
        if (data.StartsWith(new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A }))
            return "image/png";
        if (data.StartsWith(new byte[] { 0xFF, 0xD8, 0xFF }))
            return "image/jpeg";
        if (data.StartsWith("GIF87a"u8) || data.StartsWith("GIF89a"u8))
            return "image/gif";
        if (data.Length >= 12 && data.StartsWith("RIFF"u8) && data[8..12].SequenceEqual("WEBP"u8))
            return "image/webp";
        throw new ArgumentException("只支持 PNG、JPEG、GIF 和 WebP 图片。");
    }
}
