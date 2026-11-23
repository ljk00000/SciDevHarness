using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal sealed partial class WorkbenchViewModel
{
    private ImageAttachment[] _images = [];
    public bool HasImages => _images.Length > 0;
    public string ImageNames => string.Join("、", _images.Select(image => image.Name));

    internal void AttachFiles(string[] paths) => SetImages(ImageAttachmentFiles.Load(paths, _images));

    internal void AttachClipboardImage(byte[] bytes)
    {
        ImageAttachment.Validate(_images.Length + 1, _images.Sum(image => (long)image.ByteCount) + bytes.Length);
        SetImages([.._images, ImageAttachment.FromBytes(bytes, "clipboard.png")]);
    }

    internal void ClearImages() => SetImages([]);

    internal void ShowAttachmentError(string text) => Status = text;

    private void SetImages(ImageAttachment[] updated)
    {
        _images = updated;
        Changed(nameof(HasImages));
        Changed(nameof(ImageNames));
    }
}
