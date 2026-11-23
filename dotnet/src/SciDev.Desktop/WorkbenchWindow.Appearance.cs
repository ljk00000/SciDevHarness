using System.IO;
using System.Windows;
using System.Windows.Media;
using Microsoft.Win32;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class WorkbenchWindow
{
    private ResourceDictionary? _palette;
    private DesktopPreferences _appearance = DesktopPreferences.Default;
    private bool _appearanceBusy;
    private int _appearanceVersion;

    internal string AppearanceProfile => _appearance.Profile;

    private void ApplyAppearance(DesktopPreferences preferences)
    {
        if (_appearance.Profile != preferences.Profile || !ReferenceEquals(_appearance.QtLayouts, preferences.QtLayouts))
            _resizedAgentFraction = null;
        _appearance = preferences;
        var options = preferences.Appearance;
        if (_palette is not null)
            Resources.MergedDictionaries.Remove(_palette);
        _palette = WorkbenchAppearance.Create(preferences.Profile, options);
        Resources.MergedDictionaries.Add(_palette);
        FontFamily = WorkbenchAppearance.Font(options.FontFamily, WorkbenchAppearance.AutomaticFont);
        FontSize = options.FontSize * 4d / 3d;
        FontWeight = System.Windows.FontWeight.FromOpenTypeWeight(options.FontWeight);
        Surface.LayoutTransform = options.Zoom == 0 ? Transform.Identity : new ScaleTransform(options.Zoom / 100d, options.Zoom / 100d);
        SettingsPage.Apply(preferences);
        FileDesk.ApplyAppearance(preferences.Profile, preferences.QtLayouts.GetValueOrDefault(preferences.Profile));
        FileDesk.ApplyEditorOptions(options);
        VersionsPage.SetProfile(preferences.Profile);
        AdaptClassicLayout();
    }

    private async Task LoadAppearanceAsync(string workspace)
    {
        var version = ++_appearanceVersion;
        _appearanceBusy = true;
        SettingsPage.SetBusy(true);
        try
        {
            var preferences = await Task.Run(() => DesktopPreferences.Load(workspace));
            if (version == _appearanceVersion)
                ApplyAppearance(preferences ?? DesktopPreferences.Default);
        }
        catch (Exception ex) when (AppearanceError(ex))
        {
            if (version == _appearanceVersion)
            {
                ApplyAppearance(DesktopPreferences.Default);
                SettingsPage.ToolTip = AppearanceMessage(ex);
            }
        }
        finally
        {
            if (version == _appearanceVersion)
            {
                _appearanceBusy = false;
                SettingsPage.SetBusy(false);
            }
        }
    }

    private async void ChangeAppearance(DesktopPreferences preferences)
    {
        if (_appearanceBusy || _switchingWorkspace)
            return;
        var workspace = _viewModel.Workspace;
        await ChangeAppearanceAsync(workspace, () =>
        {
            preferences.Save(workspace, CancellationToken.None);
            return preferences;
        });
    }

    private async void ImportAppearance(object sender, RoutedEventArgs e)
    {
        if (_switchingWorkspace || _appearanceBusy)
            return;
        var dialog = new OpenFileDialog
        {
            Title = "导入 Qt 外观偏好", Filter = "偏好导出 (*.json)|*.json", CheckFileExists = true,
        };
        if (dialog.ShowDialog(this) != true)
            return;
        var workspace = _viewModel.Workspace;
        await ChangeAppearanceAsync(workspace, () =>
        {
            var preferences = DesktopPreferences.ReadExport(dialog.FileName);
            preferences.Save(workspace, CancellationToken.None);
            return preferences;
        });
    }

    private async Task ChangeAppearanceAsync(string workspace, Func<DesktopPreferences> save)
    {
        var version = ++_appearanceVersion;
        _appearanceBusy = true;
        SettingsPage.SetBusy(true);
        try
        {
            var preferences = await Task.Run(save);
            if (version == _appearanceVersion && workspace == _viewModel.Workspace)
            {
                ApplyAppearance(preferences);
                SettingsPage.ToolTip = null;
            }
        }
        catch (Exception ex) when (AppearanceError(ex))
        {
            if (version == _appearanceVersion && workspace == _viewModel.Workspace)
            {
                ApplyAppearance(_appearance);
                MessageBox.Show(this, AppearanceMessage(ex), "外观偏好");
            }
        }
        finally
        {
            if (version == _appearanceVersion)
            {
                _appearanceBusy = false;
                SettingsPage.SetBusy(false);
            }
        }
    }

    internal void SetCaptureAppearance(string profile) => ApplyAppearance(DesktopPreferences.Default with { Profile = profile });

    private static bool AppearanceError(Exception ex) => ex is ToolDeniedException or IOException or
        ArgumentException or UnauthorizedAccessException;

    private static string AppearanceMessage(Exception ex) => ex is ToolDeniedException denied ? denied.Message :
        "无法读取或保存工作台外观。";
}
