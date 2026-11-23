using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace SciDev.Desktop;

internal static class WorkbenchCapture
{
    public static int Render(string destination, Action<WorkbenchWindow>? selectPanel = null,
        Func<FrameworkElement>? createOverlay = null, bool sample = true, string? workspace = null, bool compareAt96Dpi = false)
    {
        Directory.CreateDirectory(destination);
        foreach (var (width, height, monitorScale) in new[] { (1280, 720, 1d), (1920, 1080, 1.25), (2560, 1440, 1.5), (3840, 2160, 2d) })
        {
            var scale = compareAt96Dpi ? 1 : monitorScale;
            var window = new WorkbenchWindow(workspace ?? @"C:\Projects\SciDevHarness");
            if (sample)
                window.ViewModel.LoadCaptureSample();
            else
                window.ViewModel.LoadEmptyCapture();
            selectPanel?.Invoke(window);
            var surface = window.CaptureSurface;
            var size = new Size(width / scale, height / scale);
            surface.Measure(size);
            surface.Arrange(new Rect(size));
            surface.UpdateLayout();
            if (createOverlay is not null)
            {
                var screen = new Grid { Background = new VisualBrush(surface) };
                screen.Children.Add(new Border { Background = new SolidColorBrush(Color.FromArgb(64, 0, 0, 0)) });
                screen.Children.Add(createOverlay());
                screen.Measure(size);
                screen.Arrange(new Rect(size));
                screen.UpdateLayout();
                surface = screen;
            }
            var bitmap = new RenderTargetBitmap(width, height, 96 * scale, 96 * scale, PixelFormats.Pbgra32);
            bitmap.Render(surface);
            var png = new PngBitmapEncoder();
            png.Frames.Add(BitmapFrame.Create(bitmap));
            using var output = File.Create(Path.Combine(destination, $"{width}x{height}.png"));
            png.Save(output);
            window.Close();
        }
        return 0;
    }
}
