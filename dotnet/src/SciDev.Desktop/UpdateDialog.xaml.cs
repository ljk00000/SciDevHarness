using System.ComponentModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class UpdateDialog : Window
{
    private readonly string _repository;
    private readonly Func<IOperationReview, bool, PreviewUpdates> _open;
    private CancellationTokenSource? _operation;
    private PreviewUpdates.Release? _release;
    private VerifiedUpdate? _downloaded;
    private bool _closed;

    internal UpdateDialog(WorkbenchWindow owner, Func<IOperationReview, bool, PreviewUpdates>? open = null)
    {
        InitializeComponent();
        Owner = owner;
        Resources.MergedDictionaries.Add(WorkbenchAppearance.Create(owner.AppearanceProfile));
        Resources.MergedDictionaries.Add(new ResourceDictionary
        {
            Source = new Uri("pack://application:,,,/SciDev.Desktop;component/WorkbenchControls.xaml", UriKind.Absolute),
        });
        MaxWidth = Math.Max(MinWidth, SystemParameters.WorkArea.Width);
        MaxHeight = Math.Max(MinHeight, SystemParameters.WorkArea.Height);
        _repository = PreviewUpdates.Repository();
        _open = open ?? ((review, allow) => new PreviewUpdates(owner.ViewModel.Workspace, review, allow,
            owner.ViewModel.NetworkPolicy == AgentNetworkPolicy.Deny));
        VersionLabel.Text = $"当前版本 {PreviewUpdates.CurrentVersion}";
        ReleaseRepo.Text = $"更新源 {_repository}";
        Closed += (_, _) =>
        {
            _closed = true;
            _operation?.Cancel();
        };
        UpdateActions();
    }

    private PreviewUpdates Client() => _open(new WorkbenchReview(this), AllowNetwork.IsChecked == true);

    private async void CheckClicked(object sender, RoutedEventArgs args) => await RunAsync("正在检查更新", async token =>
    {
        _release = null;
        _downloaded = null;
        Notes.Clear();
        AssetLabel.Text = "";
        VersionLabel.Text = $"当前版本 {PreviewUpdates.CurrentVersion}";
        DigestField.Visibility = Visibility.Collapsed;
        Digest.Clear();
        var release = await Client().CheckAsync(_repository, token);
        if (_closed)
            return;
        _release = release;
        if (!release.Available)
        {
            Status.Text = "当前版本已是最新版本。";
            return;
        }
        VersionLabel.Text = $"可用更新 {release.Current} → {release.Latest}";
        AssetLabel.Text = $"{release.AssetName} · {release.AssetSize / (1024d * 1024):N1} MB";
        Notes.Text = release.Notes;
        DigestField.Visibility = PreviewUpdates.IsValidDigest(release.AssetDigest) ? Visibility.Collapsed : Visibility.Visible;
        Status.Text = DigestField.Visibility == Visibility.Visible ? "发布缺少 SHA-256，请输入你核对过的摘要。" : release.Url;
    });

    private async void DownloadClicked(object sender, RoutedEventArgs args)
    {
        if (_operation is not null || _release is not { Available: true } release)
            return;
        var picker = new SaveFileDialog
        {
            Title = "保存经过校验的安装包", FileName = release.AssetName,
            Filter = "Windows 安装程序 (*.exe)|*.exe", DefaultExt = ".exe", OverwritePrompt = false,
        };
        if (picker.ShowDialog(this) != true)
            return;
        await DownloadAsync(release, picker.FileName);
    }

    internal Task DownloadAsync(PreviewUpdates.Release release, string path) => RunAsync("等待批准下载", async token =>
    {
        Transfer.Value = 0;
        Transfer.Visibility = Visibility.Visible;
        var progress = new Progress<(long Done, long Total)>(value =>
        {
            if (!_closed && !token.IsCancellationRequested)
                Transfer.Value = value.Done * 100d / value.Total;
        });
        var package = await Client().DownloadAsync(release, path, Digest.Text, progress, token);
        if (!_closed)
        {
            _downloaded = package;
            Status.Text = $"已保存并校验 {package.Path}";
        }
    });

    private async void InstallClicked(object sender, RoutedEventArgs args)
    {
        if (_downloaded is not { } package)
            return;
        await RunAsync("等待批准启动安装器", async token =>
        {
            var pid = await Client().LaunchAsync(package, token);
            if (!_closed)
                Status.Text = $"已启动安装器（进程 {pid}），请在安装器窗口中继续。";
        });
    }

    private async Task RunAsync(string status, Func<CancellationToken, Task> operation)
    {
        if (_operation is not null)
            return;
        using var stop = new CancellationTokenSource();
        _operation = stop;
        Status.Text = status;
        UpdateActions();
        try { await operation(stop.Token); }
        catch (OperationCanceledException) { if (!_closed) Status.Text = "已取消。"; }
        catch (Exception error) when (error is ArgumentException or ToolDeniedException or IOException or
            UnauthorizedAccessException or Win32Exception or NotSupportedException)
        {
            if (!_closed)
                Status.Text = error.Message;
        }
        finally
        {
            _operation = null;
            if (!_closed)
            {
                Transfer.Visibility = Visibility.Collapsed;
                UpdateActions();
            }
        }
    }

    private void NetworkChanged(object sender, RoutedEventArgs args) => UpdateActions();
    private void DigestChanged(object sender, TextChangedEventArgs args) => UpdateActions();

    private void CloseClicked(object sender, RoutedEventArgs args)
    {
        if (_operation is { } running)
            running.Cancel();
        else
            Close();
    }

    private void UpdateActions()
    {
        if (CheckButton is null || CloseButton is null)
            return;
        var idle = _operation is null;
        CheckButton.IsEnabled = idle;
        AllowNetwork.IsEnabled = idle;
        Digest.IsEnabled = idle;
        DownloadButton.IsEnabled = idle && _release is { Available: true } available &&
            (PreviewUpdates.IsValidDigest(available.AssetDigest) || PreviewUpdates.IsValidDigest(Digest.Text));
        InstallButton.IsEnabled = idle && _downloaded is not null;
        CloseButton.Content = idle ? "关闭" : "取消";
    }
}
