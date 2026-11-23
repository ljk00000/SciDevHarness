using System.Windows;
using System.Windows.Media;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal static class WorkbenchAppearance
{
    internal const string AutomaticFont = "Noto Sans SC, Microsoft YaHei UI, Segoe UI";
    internal static FontFamily[] InstalledFonts { get; } =
        Fonts.SystemFontFamilies.OrderBy(font => font.Source, StringComparer.CurrentCultureIgnoreCase).ToArray();

    internal static FontFamily Font(string source, string fallback) =>
        InstalledFonts.FirstOrDefault(font => font.Source.Equals(source, StringComparison.OrdinalIgnoreCase)) ?? new(fallback);

    public static ResourceDictionary Create(string profile, DesktopAppearanceOptions? options = null)
    {
        var (canvas, panel, sidebar, edge, text, muted, accent, accentText, selection, tint) = profile switch
        {
            "studio" => ("#151A23", "#202836", "#1B2330", "#364358", "#ECF1F8", "#AEBBCF", "#86B6FF", "#142139", "#33486A", "#283C5C"),
            "focus" => ("#191622", "#242030", "#201B2B", "#443952", "#F3EFFA", "#BCB0CE", "#C5A9FA", "#251B35", "#514266", "#392D4D"),
            _ => ("#F4F6F9", "#FFFFFF", "#F7F9FC", "#D4DDE8", "#263346", "#65758A", "#2D72D9", "#FFFFFF", "#DCE9FA", "#E8EFFA"),
        };
        options ??= new();
        if (options.AccentColor.Length != 0)
        {
            accent = options.AccentColor;
            var color = (Color)ColorConverter.ConvertFromString(accent);
            var luminance = 0.2126 * ScRgb(color.R) + 0.7152 * ScRgb(color.G) + 0.0722 * ScRgb(color.B);
            accentText = luminance > 0.179 ? "#000000" : "#FFFFFF";
        }
        var colors = new ResourceDictionary
        {
            ["WorkbenchCanvas"] = Brush(canvas),
            ["WorkbenchPanel"] = Brush(panel),
            ["WorkbenchSidebar"] = Brush(sidebar),
            ["WorkbenchEdge"] = Brush(edge),
            ["WorkbenchText"] = Brush(text),
            ["WorkbenchMuted"] = Brush(muted),
            ["WorkbenchAccent"] = Brush(accent),
            ["WorkbenchAccentText"] = Brush(accentText),
            ["WorkbenchTint"] = Brush(tint),
            ["WorkbenchAgentMark"] = Brush("#167F91"),
            ["WorkbenchGitAccent"] = Brush("#128575"),
            ["WorkbenchWarning"] = Brush(profile == "paper" ? "#C64D3D" : "#FFA094"),
            ["WorkbenchEditorGutter"] = Brush(profile == "paper" ? "#E9EDF2" : sidebar),
            ["WorkbenchEditorLine"] = Brush(profile == "paper" ? "#EAF0F5" : tint),
            ["WorkbenchScrollThumb"] = Brush(profile == "paper" ? "#C3CEDA" : edge),
            ["WorkbenchDisabled"] = Brush(profile == "paper" ? "#E4E8ED" : sidebar),
            ["WorkbenchDisabledText"] = Brush(profile == "paper" ? "#8592A2" : muted),
            ["WorkbenchTreeDetails"] = Brush(profile == "paper" ? "#F2F5F9" : sidebar),
            ["WorkbenchControlWeight"] = FontWeight.FromOpenTypeWeight(options.ControlWeight),
            ["WorkbenchButtonPadding"] = options.Density == "compact" ? new Thickness(10, 3, 10, 3) : new Thickness(12, 6, 12, 6),
            ["WorkbenchFileRowPadding"] = options.Density == "compact" ? new Thickness(2, 2, 2, 2) : new Thickness(2, 5, 2, 5),
        };
        colors[SystemColors.WindowBrushKey] = colors["WorkbenchPanel"];
        colors[SystemColors.WindowTextBrushKey] = colors["WorkbenchText"];
        colors[SystemColors.ControlBrushKey] = colors["WorkbenchSidebar"];
        colors[SystemColors.ControlTextBrushKey] = colors["WorkbenchText"];
        colors[SystemColors.GrayTextBrushKey] = colors["WorkbenchMuted"];
        colors[SystemColors.HighlightBrushKey] = Brush(selection);
        colors[SystemColors.HighlightTextBrushKey] = colors["WorkbenchText"];
        colors[SystemColors.InactiveSelectionHighlightBrushKey] = Brush(selection);
        colors[SystemColors.InactiveSelectionHighlightTextBrushKey] = colors["WorkbenchText"];
        return colors;
    }

    private static double ScRgb(byte value)
    {
        var component = value / 255d;
        return component <= 0.04045 ? component / 12.92 : Math.Pow((component + 0.055) / 1.055, 2.4);
    }

    private static SolidColorBrush Brush(string color)
    {
        var brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(color));
        brush.Freeze();
        return brush;
    }
}
