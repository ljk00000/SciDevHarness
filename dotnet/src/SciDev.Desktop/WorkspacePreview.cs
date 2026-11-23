using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using ICSharpCode.AvalonEdit;
using ICSharpCode.AvalonEdit.Highlighting;
using Microsoft.Win32;

namespace SciDev.Desktop;

internal static class WorkspacePreview
{
    [STAThread]
    public static int Main(string[] args)
    {
        if (args is ["--capture", var folder])
            return Capture(Path.GetFullPath(folder));
        var app = new Application();
        if (args is ["--capture-workbench", var screenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(screenshots));
        if (args is ["--capture-home", var homeScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(homeScreenshots), sample: false);
        if (args is ["--capture-documents", var documentScreenshots, var documentFixture])
            return WorkbenchCapture.Render(Path.GetFullPath(documentScreenshots),
                view => view.LoadDocumentCapture(documentFixture == "-" ? null : Path.GetFullPath(documentFixture)),
                sample: false, workspace: @"C:\Projects\实验工作区", compareAt96Dpi: true);
        if (args is ["--capture-classic", var classicScreenshots, var kind, var sampleFile] && kind is "home" or "editor" or "settings" or "versions")
            return WorkbenchCapture.Render(Path.GetFullPath(classicScreenshots), view =>
            {
                if (kind == "settings")
                    view.ShowSettings();
                else if (kind == "versions")
                    view.LoadVersionCapture();
                else
                    view.LoadClassicCapture(kind == "editor" ? Path.GetFullPath(sampleFile) : null);
            },
                sample: false, workspace: @"C:\Projects\scidev-ui-workspace", compareAt96Dpi: true);
        if (args is ["--capture-settings", var settingsScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(settingsScreenshots), view => view.ShowSettings());
        if (args is ["--capture-rename-review", var renameScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(renameScreenshots), view => view.SetCaptureAppearance("paper"),
                WorkbenchReview.CreateRenameCapture);
        if (args is ["--capture-plan", var planScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(planScreenshots), view => view.ViewModel.LoadPlanCapture());
        if (args is ["--capture-queue", var queueScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(queueScreenshots), view => view.ViewModel.LoadQueueCapture());
        if (args is ["--capture-manual", var manualScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(manualScreenshots), view => view.ViewModel.Mode = Core.ExecutionMode.Manual);
        if (args is ["--capture-workspace-write", var writeScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(writeScreenshots), view => view.ViewModel.Mode = Core.ExecutionMode.WorkspaceWrite);
        if (args is ["--capture-full-access", var fullScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(fullScreenshots), view =>
            {
                view.SetCaptureAppearance("paper");
                view.ViewModel.Mode = Core.ExecutionMode.FullAccess;
            });
        if (args is ["--capture-files", var fileScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(fileScreenshots), view => view.LoadEditorCapture());
        if (args is ["--capture-diagnostics", var diagnosticScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(diagnosticScreenshots), view =>
            {
                view.SetCaptureAppearance("paper");
                view.LoadDiagnosticsCapture();
            });
        if (args is ["--capture-split", var splitScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(splitScreenshots), view => view.LoadEditorCapture(split: true));
        if (args is ["--capture-git", var gitScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(gitScreenshots), view => view.LoadGitCapture());
        if (args is ["--capture-terminal", var terminalScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(terminalScreenshots), view => view.LoadTerminalCapture());
        if (args is ["--capture-debugger", var debuggerScreenshots])
            return WorkbenchCapture.Render(Path.GetFullPath(debuggerScreenshots), view =>
            {
                view.SetCaptureAppearance("paper");
                view.LoadDebuggerCapture();
            });
        if (args is ["--capture-theme", "paper" or "studio" or "focus", var themeFolder])
            return WorkbenchCapture.Render(Path.GetFullPath(themeFolder), view =>
            {
                view.SetCaptureAppearance(args[1]);
                view.LoadEditorCapture();
            });
        var startup = WorkspaceStartup.ForUser();
        string workspace;
        switch (args)
        {
            case ["--workspace", var selected]:
                workspace = Path.GetFullPath(selected);
                if (!WorkspaceStartup.CanOpen(workspace))
                    return 2;
                startup.Remember(workspace);
                break;
            case []:
                workspace = startup.Resolve(Directory.GetCurrentDirectory(), AppContext.BaseDirectory);
                break;
            case ["--choose-workspace"]:
                var picker = new OpenFolderDialog { Title = "打开工作区" };
                if (picker.ShowDialog() != true)
                    return 0;
                workspace = picker.FolderName;
                if (!WorkspaceStartup.CanOpen(workspace))
                    return 2;
                startup.Remember(workspace);
                break;
            default:
                return 2;
        }
        app.Run(new WorkbenchWindow(workspace, startup.Remember));
        return 0;
    }

    private const string Example = """
        namespace Research;

        // 中文输入与代码定位示例
        public sealed record Observation(string Name, double Value);

        public static class Experiment
        {
            public static Observation Run() => new("实验一", 42.0);
        }
        """;
    private static readonly Brush Paper = Brush("#F7F7F5");
    private static readonly Brush Text = Brush("#273139");

    private static int Capture(string directory)
    {
        Directory.CreateDirectory(directory);
        var sw = Stopwatch.StartNew();
        var editor = CreateEditor(Example + "\n" + string.Concat(Enumerable.Repeat("// 基线 0123456789\n", 50_000)));
        var loaded = sw.ElapsedMilliseconds;
        if (editor.Document.LineCount < 50_000 || !editor.Document.GetText(editor.Document.GetLineByNumber(3)).Contains("中文"))
            throw new InvalidOperationException("Editor did not load the UTF-8 fixture.");
        var document = editor.Document;
        var line = document.GetLineByNumber(50001);
        var old = document.GetText(line);
        document.Replace(line.Offset, line.Length, "// exact edit");
        if (document.GetText(line.Offset, 13) != "// exact edit")
            throw new InvalidOperationException("Editor did not retain the exact edit.");
        editor.ScrollToLine(50001);
        editor.Text = Example;

        foreach (var (width, height, scale) in new[] { (1280, 720, 1d), (1920, 1080, 1.25), (2560, 1440, 1.5), (3840, 2160, 2d) })
        {
            var visual = Build(CreateEditor(Example));
            var size = new Size(width / scale, height / scale);
            visual.Measure(size);
            visual.Arrange(new Rect(size));
            visual.UpdateLayout();
            var image = new RenderTargetBitmap(width, height, 96 * scale, 96 * scale, PixelFormats.Pbgra32);
            image.Render(visual);
            var png = new PngBitmapEncoder();
            png.Frames.Add(BitmapFrame.Create(image));
            using var output = File.Create(Path.Combine(directory, $"{width}x{height}.png"));
            png.Save(output);
        }
        File.WriteAllText(Path.Combine(directory, "editor-metrics.txt"),
            $"Loaded 50000 lines in {loaded} ms; Chinese text and exact edit verified.");
        return 0;
    }

    private static TextEditor CreateEditor(string source) => new()
    {
        Text = source, ShowLineNumbers = true, FontFamily = new FontFamily("Consolas"), FontSize = 15,
        SyntaxHighlighting = HighlightingManager.Instance.GetDefinition("C#"),
        Background = Brushes.White, Foreground = Text,
        HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
        VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
    };

    private static Grid Build(TextEditor editor)
    {
        var grid = new Grid { Background = Paper };
        grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(54) });
        grid.RowDefinitions.Add(new RowDefinition());
        var heading = new TextBlock
        {
            Text = "SciDevHarness     /     工作区 · 编辑器原型",
            Margin = new Thickness(22, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center,
            Foreground = Text, FontSize = 19, FontWeight = FontWeights.SemiBold,
        };
        grid.Children.Add(heading);
        var body = new Grid { Margin = new Thickness(16, 0, 16, 16) };
        Grid.SetRow(body, 1);
        grid.Children.Add(body);
        body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(190), MinWidth = 125 });
        body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(7) });
        body.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star), MinWidth = 260 });
        var tabs = new ListBox
        {
            Background = Brushes.White,
            BorderThickness = new Thickness(0),
            ItemsSource = new[] { "研究项目", "    Experiment.cs", "    notes.md" },
            Padding = new Thickness(12),
            FontSize = 14,
        };
        body.Children.Add(tabs);
        var split = new GridSplitter { Width = 5, HorizontalAlignment = HorizontalAlignment.Center, Background = Brush("#DDDFDA") };
        Grid.SetColumn(split, 1);
        body.Children.Add(split);

        var code = new DockPanel { Background = Brushes.White };
        var label = new TextBlock
        {
            Text = "Experiment.cs", Foreground = Text, FontSize = 14,
            Padding = new Thickness(14, 10, 0, 10), Background = Brush("#ECEEE9"),
        };
        DockPanel.SetDock(label, Dock.Top);
        code.Children.Add(label);
        code.Children.Add(editor);
        Grid.SetColumn(code, 2);
        body.Children.Add(code);
        return grid;
    }

    private static SolidColorBrush Brush(string hex) => new((Color)ColorConverter.ConvertFromString(hex));
}
