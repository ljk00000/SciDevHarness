namespace SciDev.Infrastructure;

public sealed record DesktopAppearanceOptions
{
    public string AccentColor { get; init; } = "";
    public string Density { get; init; } = "comfortable";
    public int Zoom { get; init; }
    public string FontFamily { get; init; } = "";
    public int FontSize { get; init; } = 11;
    public int FontWeight { get; init; } = 400;
    public int ControlWeight { get; init; } = 600;
    public int RailWidth { get; init; } = 76;
    public int IconSize { get; init; } = 30;
    public string ExplorerMode { get; init; } = "auto";
    public string EditorFontFamily { get; init; } = "Consolas";
    public int EditorFontSize { get; init; } = 12;
    public int EditorTabSize { get; init; } = 4;
    public bool EditorWrap { get; init; }
    public bool LineNumbers { get; init; } = true;
    public bool HighlightLine { get; init; } = true;

    internal bool IsValid => AccentColor is not null && ValidAccent(AccentColor) &&
        Density is "comfortable" or "compact" && Zoom is 0 or 90 or 100 or 110 or 125 or 150 &&
        FontFamily is { Length: <= 160 } && EditorFontFamily is { Length: > 0 and <= 160 } &&
        FontSize is >= 10 and <= 16 && FontWeight is 400 or 500 or 600 && ControlWeight is 500 or 600 or 700 &&
        RailWidth is >= 64 and <= 104 && IconSize is >= 24 and <= 40 && ExplorerMode is "auto" or "show" or "hide" &&
        EditorFontSize is >= 10 and <= 28 && EditorTabSize is >= 2 and <= 8;

    public static bool ValidAccent(string color) =>
        color.Length == 0 || (color.Length == 7 && color[0] == '#' && color.AsSpan(1).IndexOfAnyExcept("0123456789abcdefABCDEF") < 0);
}
