using System.Globalization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class ClassicSettingsPage : UserControl
{
    private sealed record Row(string Category, string SearchText, Border Card,
        Action<DesktopPreferences>? Update = null, Func<DesktopPreferences, bool>? IsModified = null);
    private sealed record Choice<T>(string Label, T Value);

    private static readonly DesktopPreferences Defaults = DesktopPreferences.Default;
    private readonly List<Row> _rows = [];
    private DesktopPreferences _current = Defaults;
    private bool _syncing;

    public static readonly DependencyProperty AgentSettingsProperty = DependencyProperty.Register(
        nameof(AgentSettings), typeof(FrameworkElement), typeof(ClassicSettingsPage));
    public FrameworkElement? AgentSettings
    {
        get => (FrameworkElement?)GetValue(AgentSettingsProperty);
        set => SetValue(AgentSettingsProperty, value);
    }

    internal event Action<DesktopPreferences>? PreferencesChanged;
    internal event RoutedEventHandler? ReturnRequested;
    internal event RoutedEventHandler? ImportRequested;
    internal event RoutedEventHandler? UpdatesRequested;

    public ClassicSettingsPage()
    {
        InitializeComponent();
        BuildRows();
        string[] categories = ["全部", "外观", "文字", "布局", "编辑器", "Agent"];
        Categories.ItemsSource = CompactCategories.ItemsSource = categories;
        SetCategory("全部");
        Apply(Defaults);
    }

    internal void Apply(DesktopPreferences preferences)
    {
        _current = preferences;
        _syncing = true;
        foreach (var row in _rows)
            row.Update?.Invoke(preferences);
        _syncing = false;
        UpdateFilter();
    }

    internal void SetBusy(bool busy) => Rows.IsEnabled = !busy;

    internal void SetCategory(string category)
    {
        if (Equals(Categories.SelectedItem, category) && Equals(CompactCategories.SelectedItem, category))
            return;
        CompactCategories.SelectedItem = Categories.SelectedItem = category;
        RowsScroll.ScrollToTop();
    }

    internal void ShowAgentSettings()
    {
        Search.Clear();
        Modified.IsChecked = false;
        SetCategory("Agent");
        RowsScroll.ScrollToTop();
    }

    internal void ResetNavigation()
    {
        Search.Clear();
        Modified.IsChecked = false;
        SetCategory("全部");
        RowsScroll.ScrollToTop();
    }

    private void BuildRows()
    {
        AddChoice("外观", "颜色主题", "主题同时应用于工作台、编辑器和开发版本树。", "workbench color theme",
            p => p.Profile, (p, v) => p with { Profile = v },
            ("Studio · 深色工作台", "studio"), ("Paper · 明亮浅色", "paper"), ("Focus · 专注宽编辑器", "focus"));
        AddText("外观", "强调色", "输入 #RRGGBB；留空跟随主题。按钮文字自动匹配明暗对比度。",
            "workbench accent color 颜色", "跟随主题，例如 #3278df", a => a.AccentColor, (a, v) => a with { AccentColor = v });
        AddOption("外观", "界面密度", "舒适模式留出呼吸空间；紧凑模式缩小按钮与文件列表间距。",
            "workbench density spacing", a => a.Density, (a, v) => a with { Density = v }, ("舒适", "comfortable"), ("紧凑", "compact"));
        AddOption("外观", "界面缩放", "自动适配显示器，或使用固定缩放；Windows 的 DPI 缩放仍会生效。",
            "window zoom scale", a => a.Zoom, (a, v) => a with { Zoom = v },
            ("自动适配屏幕", 0), ("90%", 90), ("100%", 100), ("110%", 110), ("125%", 125), ("150%", 150));

        var fonts = WorkbenchAppearance.InstalledFonts;
        AddOption("文字", "界面字体", "自动优先中文无衬线字体；不会下载或安装新字体。", "workbench font family 字体",
            a => a.FontFamily, (a, v) => a with { FontFamily = v },
            fonts.Select(font => (font.Source, font.Source)).Prepend(("自动 · 系统字体", "")).ToArray());
        AddNumber("文字", "界面字号", "调整导航、对话和设置文字；代码字号可单独设置。", "workbench font size 字号",
            a => a.FontSize, (a, v) => a with { FontSize = v }, 10, 16, " pt");
        AddOption("文字", "正文粗细", "让文字更清晰；按钮和标题使用独立字重。", "workbench font weight 字重",
            a => a.FontWeight, (a, v) => a with { FontWeight = v }, ("常规 · 400", 400), ("中等 · 500", 500), ("较粗 · 600", 600));
        AddOption("文字", "按钮与标题粗细", "同时调整按钮、导航标题和标签文字。", "workbench control font weight 字重",
            a => a.ControlWeight, (a, v) => a with { ControlWeight = v }, ("中等 · 500", 500), ("半粗 · 600", 600), ("加粗 · 700", 700));

        AddNumber("布局", "左侧图标栏宽度", "点击区域随栏宽变化；关闭资源栏时也保留此宽度。", "activity bar sidebar width",
            a => a.RailWidth, (a, v) => a with { RailWidth = v }, 64, 104, " px");
        AddNumber("布局", "侧栏图标大小", "高 DPI 下使用高分辨率图标；尺寸不会超出点击区域。", "activity bar icon size",
            a => a.IconSize, (a, v) => a with { IconSize = v }, 24, 40, " px");
        AddOption("布局", "资源管理器显示", "自动模式在窄窗口收起；也可以始终显示或默认隐藏。", "explorer sidebar visibility",
            a => a.ExplorerMode, (a, v) => a with { ExplorerMode = v }, ("自动 · 响应式", "auto"), ("始终显示", "show"), ("默认隐藏", "hide"));

        AddOption("编辑器", "代码字体", "仅列出已安装的等宽字体，不影响界面和对话文字。", "editor font family 字体",
            a => a.EditorFontFamily, (a, v) => a with { EditorFontFamily = v },
            fonts.Where(IsMonospaced).Select(font => (font.Source, font.Source)).ToArray());
        AddNumber("编辑器", "代码字号", "应用于所有打开的文件及后续新文件，不改变文件内容。", "editor font size 字号",
            a => a.EditorFontSize, (a, v) => a with { EditorFontSize = v }, 10, 28, " pt");
        AddNumber("编辑器", "Tab 显示宽度", "只调整缩进的显示宽度，不替换文件中的空格或 Tab。", "editor tab size indentation",
            a => a.EditorTabSize, (a, v) => a with { EditorTabSize = v }, 2, 8, " 空格");
        AddOption("编辑器", "自动换行", "按编辑区宽度折行，不插入真实换行符。", "editor word wrap",
            a => a.EditorWrap, (a, v) => a with { EditorWrap = v }, ("关闭", false), ("开启", true));
        AddOption("编辑器", "显示行号", "关闭后释放左侧行号空间；欢迎页始终不显示行号。", "editor line numbers",
            a => a.LineNumbers, (a, v) => a with { LineNumbers = v }, ("显示", true), ("隐藏", false));
        AddOption("编辑器", "当前行高亮", "显示或隐藏光标所在行的背景，不影响语法颜色。", "editor render line highlight",
            a => a.HighlightLine, (a, v) => a with { HighlightLine = v }, ("开启", true), ("关闭", false));

        AddRow("布局", "恢复默认布局", "将工作区的分栏宽度恢复为主题默认值。", "reset layout",
            ActionButton("恢复布局", (_, _) => Request(_current with { QtLayouts = Defaults.QtLayouts })));
        AddRow("布局", "导入外观", "读取本地的桌面外观偏好导出文件。", "import appearance",
            ActionButton("导入外观", (sender, e) => ImportRequested?.Invoke(sender, e)));
        AddRow("Agent", "应用更新", "检查 SciDevHarness 的版本更新。", "check updates",
            ActionButton("检查更新", (sender, e) => UpdatesRequested?.Invoke(sender, e)));
    }

    private void AddOption<T>(string category, string title, string description, string keywords,
        Func<DesktopAppearanceOptions, T> value, Func<DesktopAppearanceOptions, T, DesktopAppearanceOptions> change,
        params (string Label, T Value)[] choices) =>
        AddChoice(category, title, description, keywords, p => value(p.Appearance),
            (p, v) => p with { Appearance = change(p.Appearance, v) }, choices);

    private void AddChoice<T>(string category, string title, string description, string keywords,
        Func<DesktopPreferences, T> value, Func<DesktopPreferences, T, DesktopPreferences> change,
        params (string Label, T Value)[] choices)
    {
        var items = choices.Select(choice => new Choice<T>(choice.Label, choice.Value)).ToList();
        var box = new ComboBox { Height = 36, DisplayMemberPath = nameof(Choice<T>.Label), ItemsSource = items };
        box.SetResourceReference(StyleProperty, "WorkbenchComboBoxStyle");
        box.SelectionChanged += (_, _) =>
        {
            if (box.SelectedItem is Choice<T> option && !Equals(option.Value, value(_current)))
                Request(change(_current, option.Value));
        };
        AddRow(category, title, description, keywords, box, () => Request(change(_current, value(Defaults))),
            p =>
            {
                var selected = value(p);
                var item = items.Find(choice => Equals(choice.Value, selected));
                if (item is null)
                {
                    item = new Choice<T>($"{Convert.ToString(selected, CultureInfo.CurrentCulture)}（使用回退字体）", selected);
                    items.Add(item);
                    box.Items.Refresh();
                }
                box.SelectedItem = item;
            }, p => !Equals(value(p), value(Defaults)));
    }

    private void AddText(string category, string title, string description, string keywords, string placeholder,
        Func<DesktopAppearanceOptions, string> value, Func<DesktopAppearanceOptions, string, DesktopAppearanceOptions> change)
    {
        var box = new TextBox { Height = 41, MaxLength = 7, Padding = new(12, 4, 12, 4) };
        box.SetResourceReference(StyleProperty, "SettingsTextInput");
        AutomationProperties.SetName(box, title);
        var hint = Caption(placeholder);
        hint.Margin = new(13, 0, 6, 0);
        hint.IsHitTestVisible = false;
        hint.Opacity = 0.75;
        var input = new Grid();
        input.Children.Add(box);
        input.Children.Add(hint);
        box.TextChanged += (_, _) => hint.Visibility = box.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
        void Commit()
        {
            var color = box.Text.Trim();
            var valid = DesktopAppearanceOptions.ValidAccent(color);
            box.SetResourceReference(BorderBrushProperty, valid ? "WorkbenchEdge" : "WorkbenchWarning");
            box.ToolTip = valid ? null : "请输入 #RRGGBB，或留空跟随主题。";
            if (valid && color != value(_current.Appearance))
                Request(_current with { Appearance = change(_current.Appearance, color) });
        }
        box.LostKeyboardFocus += (_, _) => Commit();
        box.KeyDown += (_, e) =>
        {
            if (e.Key == Key.Enter)
            {
                Commit();
                e.Handled = true;
            }
        };
        AddRow(category, title, description, keywords, input,
            () => Request(_current with { Appearance = change(_current.Appearance, value(Defaults.Appearance)) }),
            p => { box.Text = value(p.Appearance); Commit(); },
            p => value(p.Appearance) != value(Defaults.Appearance));
    }

    private void AddNumber(string category, string title, string description, string keywords,
        Func<DesktopAppearanceOptions, int> value, Func<DesktopAppearanceOptions, int, DesktopAppearanceOptions> change,
        int minimum, int maximum, string suffix)
    {
        var input = new TextBox { Height = 36, MaxLength = 14, Padding = new(8, 2, 48, 2) };
        input.SetResourceReference(StyleProperty, "SettingsTextInput");
        AutomationProperties.SetName(input, title);
        void SetNumber(int next)
        {
            next = Math.Clamp(next, minimum, maximum);
            if (next != value(_current.Appearance))
                Request(_current with { Appearance = change(_current.Appearance, next) });
            else
                input.Text = $"{next}{suffix}";
        }
        void Commit()
        {
            var number = input.Text.EndsWith(suffix, StringComparison.Ordinal) ? input.Text[..^suffix.Length] : input.Text;
            if (int.TryParse(number.Trim(), out var parsed))
                SetNumber(parsed);
            else
                input.Text = $"{value(_current.Appearance)}{suffix}";
        }
        input.LostKeyboardFocus += (_, _) => Commit();
        input.KeyDown += (_, e) =>
        {
            switch (e.Key)
            {
                case Key.Enter: Commit(); break;
                case Key.Up: SetNumber(value(_current.Appearance) + 1); break;
                case Key.Down: SetNumber(value(_current.Appearance) - 1); break;
                default: return;
            }
            e.Handled = true;
        };
        var stepper = new StackPanel { HorizontalAlignment = HorizontalAlignment.Right, Orientation = Orientation.Horizontal, Margin = new(0, 1, 1, 1) };
        foreach (var (text, step) in new[] { ("−", -1), ("+", 1) })
        {
            var button = ActionButton(text, (_, _) => SetNumber(value(_current.Appearance) + step));
            button.Focusable = false;
            button.Width = 23;
            button.Padding = new(0);
            AutomationProperties.SetName(button, step > 0 ? $"增加{title}" : $"减小{title}");
            stepper.Children.Add(button);
        }
        var panel = new Grid();
        panel.Children.Add(input);
        panel.Children.Add(stepper);
        AddRow(category, title, description, keywords, panel,
            () => SetNumber(value(Defaults.Appearance)),
            p => input.Text = $"{value(p.Appearance)}{suffix}",
            p => value(p.Appearance) != value(Defaults.Appearance));
    }

    private void AddRow(string category, string title, string description, string keywords, FrameworkElement input,
        Action? reset = null, Action<DesktopPreferences>? update = null, Func<DesktopPreferences, bool>? modified = null)
    {
        var heading = new DockPanel { Margin = new(0, 0, 0, 7) };
        if (reset is not null)
        {
            var button = ActionButton("重置", (_, _) => reset());
            DockPanel.SetDock(button, Dock.Right);
            heading.Children.Add(button);
            AutomationProperties.SetName(button, $"重置{title}");
        }
        heading.Children.Add(new TextBlock { Text = $"{category} › {title}", VerticalAlignment = VerticalAlignment.Center, FontWeight = FontWeights.Medium });
        var content = new StackPanel();
        content.Children.Add(heading);
        var detail = Caption(description);
        detail.Margin = new(0, 0, 0, 6);
        content.Children.Add(detail);
        content.Children.Add(input);
        if (input is Control control)
            AutomationProperties.SetName(control, title);
        var card = new Border
        {
            CornerRadius = new(8), BorderThickness = new(1), Padding = new(12, 8, 12, 8),
            Margin = new(0, 0, 0, 8), Child = content,
        };
        card.SetResourceReference(BackgroundProperty, "WorkbenchPanel");
        card.SetResourceReference(BorderBrushProperty, "WorkbenchEdge");
        Rows.Children.Add(card);
        _rows.Add(new(category, $"{title} {category} {description} {keywords}", card, update, modified));
    }

    private static Button ActionButton(string text, RoutedEventHandler click)
    {
        var button = new Button { Content = text, HorizontalAlignment = HorizontalAlignment.Left, MinHeight = 34 };
        button.SetResourceReference(StyleProperty, "ClassicQuiet");
        button.Click += click;
        return button;
    }

    private static TextBlock Caption(string text)
    {
        var caption = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
        caption.SetResourceReference(TextBlock.ForegroundProperty, "WorkbenchMuted");
        return caption;
    }

    private static bool IsMonospaced(FontFamily family)
    {
        var typeface = new Typeface(family, FontStyles.Normal, FontWeights.Normal, FontStretches.Normal);
        return typeface.TryGetGlyphTypeface(out var glyphs) &&
            glyphs.CharacterToGlyphMap.TryGetValue('i', out var narrow) &&
            glyphs.CharacterToGlyphMap.TryGetValue('W', out var wide) &&
            Math.Abs(glyphs.AdvanceWidths[narrow] - glyphs.AdvanceWidths[wide]) < 0.001;
    }

    private void ReturnToWorkspace(object sender, RoutedEventArgs e) => ReturnRequested?.Invoke(sender, e);
    private void RestoreDefaults(object sender, RoutedEventArgs e) => Request(Defaults);

    private void Request(DesktopPreferences preferences)
    {
        if (!_syncing && Rows.IsEnabled)
            PreferencesChanged?.Invoke(preferences);
    }

    private void SelectCategory(object sender, SelectionChangedEventArgs e)
    {
        if (!ReferenceEquals(sender, e.Source))
            return;
        var category = (sender as System.Windows.Controls.Primitives.Selector)?.SelectedItem as string;
        if (category is not null)
            SetCategory(category);
        UpdateFilter();
    }

    private void ResizeCategories(object sender, SizeChangedEventArgs e)
    {
        Categories.Visibility = e.NewSize.Width < 840 ? Visibility.Collapsed : Visibility.Visible;
        CompactCategories.Visibility = Categories.Visibility == Visibility.Visible ? Visibility.Collapsed : Visibility.Visible;
    }

    private void UpdateFilter(object sender, RoutedEventArgs e)
    {
        UpdateFilter();
        RowsScroll.ScrollToTop();
    }

    private void SearchChanged(object sender, TextChangedEventArgs e)
    {
        if (Search is { Text.Length: > 0 } && Categories is not null)
            SetCategory("全部");
        UpdateFilter();
        RowsScroll?.ScrollToTop();
    }

    private void UpdateFilter()
    {
        if (AgentRows is null || Empty is null)
            return;
        var words = Search.Text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        var category = Categories.SelectedItem as string ?? "全部";
        var modified = Modified.IsChecked == true;
        var count = 0;
        foreach (var row in _rows)
        {
            var visible = (category == "全部" || row.Category == category) &&
                          words.All(word => row.SearchText.Contains(word, StringComparison.OrdinalIgnoreCase)) &&
                          (!modified || row.IsModified?.Invoke(_current) == true);
            row.Card.Visibility = visible ? Visibility.Visible : Visibility.Collapsed;
            count += visible ? 1 : 0;
        }
        var agent = (category is "全部" or "Agent") && !modified &&
            words.All(word => "Agent 模型 API 地址 密钥 项目权限 容器 审批 联网 宿主 命令 白名单 MCP policy model"
                .Contains(word, StringComparison.OrdinalIgnoreCase));
        AgentRows.Visibility = agent ? Visibility.Visible : Visibility.Collapsed;
        Empty.Visibility = count > 0 || agent ? Visibility.Collapsed : Visibility.Visible;
    }
}
