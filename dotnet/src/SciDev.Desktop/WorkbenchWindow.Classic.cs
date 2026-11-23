using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;

namespace SciDev.Desktop;

public partial class WorkbenchWindow
{
    private double? _resizedAgentFraction;
    private bool _showCompactExplorer;

    private void ClassicLayoutChanged(object sender, SizeChangedEventArgs e) => AdaptClassicLayout();

    private void ClassicPageChanged(object sender, SelectionChangedEventArgs e)
    {
        if (ReferenceEquals(e.Source, Views))
            AdaptClassicLayout();
    }

    private void AdaptClassicLayout()
    {
        if (WorkbenchGrid is null || Views is null || AgentPane is null || ComposerRegion is null ||
            _viewModel is null || WorkbenchGrid.ActualWidth <= 0)
            return;
        var width = WorkbenchGrid.ActualWidth;
        var compact = width < 1040;
        var options = _appearance.Appearance;
        var documents = Views.SelectedItem == DocumentsTab;
        var activity = documents && options.RailWidth == 76 ? 58 :
            options.RailWidth == 76 && width >= 1600 ? 80 : options.RailWidth;
        var preferences = _appearance.QtLayouts.GetValueOrDefault(_appearance.Profile);
        var versions = Views.SelectedItem == GitTab || Views.SelectedItem == GitChangesTab;
        var showAgent = Views.SelectedItem != SettingsTab && (!versions || _showVersionAgent);
        var chatFraction = _resizedAgentFraction ?? (preferences is { CanApply: true } ? preferences.Chat :
            _appearance.Profile == "focus" ? 0.30 : 0.50);
        var maxAgentWidth = Math.Max(0, (width - activity) * 0.66);
        var agentWidth = showAgent ? Math.Clamp((width - 10) * chatFraction, Math.Min(300, maxAgentWidth), maxAgentWidth) : 0;
        ActivityColumn.Width = new(activity);
        AgentHandleColumn.Width = new(showAgent ? 5 : 0);
        AgentColumn.Width = new(agentWidth);
        EditorAreaColumn.Width = new(1, GridUnitType.Star);
        AgentPane.Visibility = AgentSplitter.Visibility = showAgent ? Visibility.Visible : Visibility.Collapsed;
        agentWidth = ApplyDocumentsLayout(documents, width, activity, agentWidth);
        WorkspaceCaption.MaxWidth = width >= 1600 ? 250 : compact ? 60 : 137;
        WorkspaceButton.Width = compact ? 73 : Math.Clamp(133 + (width - 1280) * 0.20, 105, documents ? 190 : 380);
        CommandSearchRegion.MaxWidth = Math.Clamp(395 + (width - 1280) * 0.57, 125, documents ? 600 : 820);
        CommandSearchRegion.Width = CommandSearchRegion.MaxWidth;
        ComposerModelButton.MaxWidth = Math.Clamp(agentWidth * 0.27, 82, 170);
        RefreshWorkspaceButton.Visibility = compact || documents ? Visibility.Collapsed : Visibility.Visible;
        TitleVersionsButton.Visibility = TitleThemeButton.Visibility = documents ? Visibility.Collapsed : Visibility.Visible;
        var left = (width - 10) * (preferences is { CanApply: true } ? preferences.Left : 0.20);
        var showExplorer = options.ExplorerMode == "show" ||
            (options.ExplorerMode == "auto" && !compact) || _showCompactExplorer;
        FileDesk.SetExplorerWidth(!showExplorer ? 0 : compact ? 122 : Math.Clamp(left, 260, 440) - activity);
        FileDesk.SetEditorScale(options.EditorFontSize * 4d / 3d + (width >= 1600 && options.EditorFontSize == 12 ? 0.5 : 0));
        foreach (var button in new[] { ExplorerRailButton, SearchRailButton, GitRailButton, SettingsRailButton })
        {
            button.Width = activity - 16;
            button.Height = documents ? 44 : options.Density == "compact" ? 50 : 60;
            if (button.Content is System.Windows.Shapes.Path icon)
            {
                icon.Stretch = System.Windows.Media.Stretch.Uniform;
                icon.Width = icon.Height = options.IconSize * 0.8;
            }
        }
        MarkRail(ExplorerRailButton, documents ? _libraryOpen && !_readingFocus : Views.SelectedItem == FilesTab);
        MarkRail(GitRailButton, versions);
        MarkRail(SettingsRailButton, Views.SelectedItem == SettingsTab);
    }

    private void MarkRail(Button button, bool selected)
    {
        button.Tag = selected ? "True" : "False";
        if (selected)
        {
            button.SetResourceReference(BackgroundProperty, "WorkbenchTint");
            button.SetResourceReference(ForegroundProperty, "WorkbenchAccent");
        }
        else
        {
            button.ClearValue(BackgroundProperty);
            button.ClearValue(ForegroundProperty);
        }
    }

    private void AgentResized(object sender, DragCompletedEventArgs e)
    {
        if (!e.Canceled && WorkbenchGrid.ActualWidth > 10)
            _resizedAgentFraction = AgentColumn.ActualWidth / (WorkbenchGrid.ActualWidth - 10);
        AdaptClassicLayout();
    }

    private void SelectExplorer(object sender, RoutedEventArgs e)
    {
        _libraryOpen = Views.SelectedItem != DocumentsTab || _readingFocus || !_libraryOpen;
        _readingFocus = false;
        Views.SelectedItem = DocumentsTab;
        if (_libraryOpen)
            _ = Documents.RefreshAsync(CommandSearch.Text);
        AdaptClassicLayout();
    }
    private async void SelectGit(object sender, RoutedEventArgs e)
    {
        if (Views.SelectedItem != GitTab && Views.SelectedItem != GitChangesTab)
            _showVersionAgent = false;
        Views.SelectedItem = GitTab;
        await RefreshVersionsAsync();
        if (GitDesk.IsEnabled && !_switchingWorkspace)
            await GitDesk.RefreshAsync(approveLinkedRepository: false);
    }
    private void SelectTerminal(object sender, RoutedEventArgs e) => Views.SelectedItem = TerminalTab;
    private void SelectHistory(object sender, RoutedEventArgs e) => Views.SelectedItem = HistoryTab;

    private void FocusSearch(object sender, RoutedEventArgs e)
    {
        if (Views.SelectedItem != FilesTab)
        {
            Views.SelectedItem = DocumentsTab;
            _libraryOpen = true;
            _readingFocus = false;
            _documentFocused = false;
            AdaptClassicLayout();
        }
        CommandSearch.Focus();
        CommandSearch.SelectAll();
    }

    private void SearchWorkspace(object sender, TextChangedEventArgs e)
    {
        if (Views?.SelectedItem == DocumentsTab && Documents is not null)
        {
            if (!string.IsNullOrWhiteSpace(CommandSearch.Text))
            {
                _libraryOpen = true;
                _readingFocus = false;
            }
            _ = Documents.RefreshAsync(CommandSearch.Text);
            AdaptClassicLayout();
        }
        else if (FileDesk is not null)
            FileDesk.SetSearch(CommandSearch.Text);
    }

    private async void OpenSearchResult(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter || string.IsNullOrWhiteSpace(CommandSearch.Text))
            return;
        e.Handled = true;
        if (Views.SelectedItem == FilesTab)
            await FileDesk.OpenSearchResultAsync();
        else
        {
            Views.SelectedItem = DocumentsTab;
            await Documents.OpenFirstResultAsync(CommandSearch.Text);
        }
    }

    private async void RefreshWorkbench(object sender, RoutedEventArgs e)
    {
        if (!_switchingWorkspace)
        {
            await Task.WhenAll(FileDesk.RefreshAsync(), _viewModel.RefreshAsync(), Documents.RefreshAsync(CommandSearch.Text));
            if (Views.SelectedItem == GitTab)
                await RefreshVersionsAsync();
        }
    }

    private void ChooseTheme(object sender, RoutedEventArgs e)
    {
        if (sender is MenuItem { Tag: string profile } && profile != _appearance.Profile)
            ChangeAppearance(_appearance with { Profile = profile });
    }

    private void TogglePlan(object sender, RoutedEventArgs e)
    {
        var open = PlanBody.Visibility != Visibility.Visible;
        PlanBody.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
        PlanToggle.Content = open ? "⌄" : "›";
    }

    private void ShowThemeMenu(object sender, RoutedEventArgs e) => OpenAgentMenu(sender, e);

    private void OpenAgentMenu(object sender, RoutedEventArgs e)
    {
        if (sender is Button button && button.ContextMenu is { } menu)
        {
            menu.PlacementTarget = button;
            menu.IsOpen = true;
        }
    }

    private void MinimizeWindow(object sender, RoutedEventArgs e) => SystemCommands.MinimizeWindow(this);
    private void CloseWindow(object sender, RoutedEventArgs e) => Close();
    private void MaximizeWindow(object sender, RoutedEventArgs e) => ToggleMaximized();

    private void ToggleMaximized()
    {
        if (WindowState == WindowState.Maximized)
            SystemCommands.RestoreWindow(this);
        else
            SystemCommands.MaximizeWindow(this);
    }

    private void DragTitleBar(object sender, MouseButtonEventArgs e)
    {
        if (e.ChangedButton != MouseButton.Left)
            return;
        if (e.ClickCount == 2)
            ToggleMaximized();
        else if (WindowState == WindowState.Normal && e.LeftButton == MouseButtonState.Pressed)
            DragMove();
    }
}
