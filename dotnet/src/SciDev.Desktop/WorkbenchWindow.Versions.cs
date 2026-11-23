using System.ComponentModel;
using System.IO;
using System.Windows;
using SciDev.Core;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class WorkbenchWindow
{
    private bool _showVersionAgent;
    private int _versionRefresh;

    private void WireDevelopmentTree()
    {
        GitDesk.InspectionChanged += VersionsPage.UpdateGit;
        VersionsPage.ReturnRequested += (_, _) => ReturnToDocuments();
        VersionsPage.RefreshRequested += async (_, _) =>
        {
            await RefreshVersionsAsync();
            if (GitDesk.IsEnabled && !_switchingWorkspace)
                await GitDesk.RefreshAsync();
        };
        VersionsPage.AgentRequested += (_, _) =>
        {
            _showVersionAgent = !_showVersionAgent;
            AdaptClassicLayout();
        };
        VersionsPage.InitializeRequested += async (_, _) =>
        {
            if (_viewModel.IsBusy || _switchingWorkspace)
                return;
            Views.SelectedItem = GitChangesTab;
            await GitDesk.InitializeAsync();
        };
        VersionsPage.GitRequested += async path =>
        {
            Views.SelectedItem = GitChangesTab;
            await GitDesk.RefreshAsync();
            if (path is not null && Views.SelectedItem == GitChangesTab)
                GitDesk.SelectPath(path);
        };
        VersionsPage.CommitRequested += async message =>
        {
            if (_viewModel.IsBusy || _switchingWorkspace)
                return;
            Views.SelectedItem = GitChangesTab;
            await GitDesk.CommitStagedAsync(message.Length == 0 ? null : message);
        };
        VersionsPage.RetryRequested += RetryVersionAsync;
        _viewModel.PropertyChanged += RefreshOpenTree;
    }

    private async void RetryVersionAsync(RunHistory.Entry run)
    {
        if (_viewModel.IsBusy || _switchingWorkspace)
            return;
        var workspace = _viewModel.Workspace;
        try
        {
            var prompt = await Task.Run(() => run.Source == "python"
                ? RunHistory.ReadLegacy(workspace, run.RunId).Task
                : RunInspection.Check(workspace, run.RunId).Task);
            if (string.IsNullOrWhiteSpace(prompt) || _viewModel.IsBusy || _switchingWorkspace ||
                _viewModel.Workspace != workspace || Views.SelectedItem != GitTab)
                return;
            if (!string.IsNullOrWhiteSpace(_viewModel.Prompt) && _viewModel.Prompt != prompt &&
                MessageBox.Show(this, "使用这条历史记录的提示词替换当前输入？", "重新尝试这个方向",
                    MessageBoxButton.YesNo, MessageBoxImage.Question, MessageBoxResult.No) != MessageBoxResult.Yes)
                return;
            _viewModel.BeginDraft();
            _viewModel.Prompt = prompt;
            ReturnToDocuments();
            PromptInput.Focus();
        }
        catch (Exception ex) when (HistoryReadError(ex))
        {
            if (_viewModel.Workspace == workspace && Views.SelectedItem == GitTab)
                MessageBox.Show(this, "无法读取历史记录中的提示词。", "重新尝试这个方向");
        }
    }

    private async void RefreshOpenTree(object? sender, PropertyChangedEventArgs e)
    {
        if (Views.SelectedItem == GitTab && e.PropertyName is nameof(WorkbenchViewModel.Queue) or nameof(WorkbenchViewModel.History))
            await RefreshVersionsAsync();
    }

    private async Task RefreshVersionsAsync()
    {
        var version = ++_versionRefresh;
        var workspace = _viewModel.Workspace;
        try
        {
            var history = await Task.Run(() => RunHistory.Recent(workspace, 100, CancellationToken.None));
            if (version == _versionRefresh && workspace == _viewModel.Workspace)
                VersionsPage.SetHistory(history, _viewModel.Queue);
        }
        catch (Exception ex) when (HistoryReadError(ex))
        {
            if (version == _versionRefresh && workspace == _viewModel.Workspace)
                VersionsPage.ShowHistoryError();
        }
    }

    private static bool HistoryReadError(Exception ex) => ex is ToolDeniedException or IOException or UnauthorizedAccessException
        or ArgumentException or System.Text.Json.JsonException or InvalidOperationException or KeyNotFoundException;
}
