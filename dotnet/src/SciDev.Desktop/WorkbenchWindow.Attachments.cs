using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Media.Imaging;
using Microsoft.Win32;

namespace SciDev.Desktop;

public partial class WorkbenchWindow
{
    private void ChooseTaskImages(object sender, RoutedEventArgs e)
    {
        if (_switchingWorkspace)
            return;
        var picker = new OpenFileDialog
        {
            Title = "选择图片附件", Multiselect = true, InitialDirectory = _viewModel.Workspace,
            Filter = "图片|*.png;*.jpg;*.jpeg;*.gif;*.webp",
        };
        if (picker.ShowDialog(this) == true)
            AddTaskImageFiles(picker.FileNames);
    }

    private void ClearTaskImages(object sender, RoutedEventArgs e) => _viewModel.ClearImages();

    private void DragTaskImage(object sender, DragEventArgs e)
    {
        if (!_switchingWorkspace && e.Data.GetDataPresent(DataFormats.FileDrop, autoConvert: false))
        {
            e.Effects = DragDropEffects.Copy & e.AllowedEffects;
            e.Handled = true;
        }
    }

    private void DropTaskImage(object sender, DragEventArgs e)
    {
        if (_switchingWorkspace || e.Data.GetData(DataFormats.FileDrop, autoConvert: false) is not string[] paths)
            return;
        e.Handled = true;
        AddTaskImageFiles(paths);
    }

    private void PasteTaskImage(object sender, DataObjectPastingEventArgs e)
    {
        if (_switchingWorkspace)
            return;
        try
        {
            var data = e.SourceDataObject;
            if (data.GetData(DataFormats.FileDrop, autoConvert: false) is string[] files)
            {
                e.CancelCommand();
                AddTaskImageFiles(files);
            }
            else if (data.GetData(DataFormats.Bitmap, autoConvert: true) is BitmapSource image)
            {
                e.CancelCommand();
                if ((long)image.PixelWidth * image.PixelHeight > 16_777_216)
                    throw new ArgumentException("剪贴板图片像素过多，请压缩后重试。");
                using var buffer = new MemoryStream();
                var encoder = new PngBitmapEncoder();
                encoder.Frames.Add(BitmapFrame.Create(image));
                encoder.Save(buffer);
                _viewModel.AttachClipboardImage(buffer.ToArray());
            }
            else if (data.GetData(DataFormats.UnicodeText, autoConvert: false) is string text && ImagePathFromText(text) is { } path)
            {
                if (AddTaskImageFiles([path]))
                    e.CancelCommand();
            }
        }
        catch (Exception ex) when (ex is ArgumentException or ExternalException or InvalidOperationException or NotSupportedException)
        {
            e.CancelCommand();
            _viewModel.ShowAttachmentError(ex is ArgumentException ? ex.Message : "无法读取剪贴板图片。");
        }
    }

    private bool AddTaskImageFiles(string[] paths)
    {
        try { _viewModel.AttachFiles(paths); return true; }
        catch (ArgumentException ex) { _viewModel.ShowAttachmentError(ex.Message); return false; }
    }

    private static string? ImagePathFromText(string text)
    {
        var path = text.Trim().Trim('"');
        if (path.Length is < 5 or > 243 || !char.IsAsciiLetter(path[0]) || path[1] != ':' || path[2] is not ('\\' or '/') ||
            path.Any(char.IsControl) || Path.GetExtension(path).ToLowerInvariant() is not (".png" or ".jpg" or ".jpeg" or ".gif" or ".webp"))
            return null;
        return path;
    }
}
