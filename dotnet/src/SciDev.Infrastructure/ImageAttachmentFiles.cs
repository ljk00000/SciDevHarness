using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Read files explicitly selected by the operator for a model request.</summary>
public static class ImageAttachmentFiles
{
    public static ImageAttachment[] Load(IReadOnlyList<string> paths, IReadOnlyList<ImageAttachment>? current = null)
    {
        var images = current?.ToList() ?? [];
        long total = images.Sum(image => image.ByteCount);
        ImageAttachment.Validate(images.Count + paths.Count, total);
        foreach (var path in paths)
        {
            try
            {
                using var target = WindowsWorkspace.OpenAbsoluteFilePath(Path.GetFullPath(path));
                using var handle = WindowsWorkspace.OpenForRead(target.AbsolutePath, FileShare.Read);
                WindowsWorkspace.GetFileIdentity(handle, target.AbsolutePath);
                using var file = new FileStream(handle, FileAccess.Read);
                if (file.Length is <= 0 or > ImageAttachment.MaxImageBytes)
                    throw new ArgumentException("图片大小必须在 1 字节到 5 MiB 之间。");
                total += file.Length;
                ImageAttachment.Validate(images.Count + 1, total);
                var bytes = new byte[(int)file.Length];
                file.ReadExactly(bytes);
                images.Add(ImageAttachment.FromBytes(bytes, target.AbsolutePath));
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ToolDeniedException)
            {
                throw new ArgumentException("无法安全读取所选图片。请选择本机磁盘上的普通文件。", ex);
            }
        }
        return images.ToArray();
    }
}
