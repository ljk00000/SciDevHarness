using System.ComponentModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;

namespace SciDev.Desktop;

public partial class WorkbenchWindow
{
    private bool _libraryOpen;
    private bool _documentFocused;
    private bool _readingFocus;

    private void WireDocuments()
    {
        Documents.PreviewRequested += OpenDocument;
        Documents.TextRequested += async path => await OpenDocumentAsync(path, asText: true);
        Documents.CloseRequested += (_, _) =>
        {
            _libraryOpen = false;
            AdaptClassicLayout();
        };
        Reader.CloseRequested += (_, _) =>
        {
            Reader.Clear();
            ReturnToDocuments();
        };
        Reader.ChatRequested += (_, _) => ReturnToDocuments();
        Reader.FocusRequested += (_, _) => SetReadingFocus(!_readingFocus);
        Reader.OpenRequested += OpenDocument;
        _viewModel.PropertyChanged += DocumentStateChanged;
        UpdateAgentPresentation();
    }

    private void ReturnToDocuments()
    {
        _documentFocused = false;
        _readingFocus = false;
        Views.SelectedItem = DocumentsTab;
        AdaptClassicLayout();
    }

    private double ApplyDocumentsLayout(bool documents, double width, double activity, double previousAgentWidth)
    {
        if (Reader is null || AgentHeading is null || CodeStatus is null || _viewModel is null)
            return previousAgentWidth;
        var compactReader = width < 1120;
        var showReader = documents && Reader.IsOpen && (_readingFocus || !compactReader || _documentFocused);
        var exclusiveReader = showReader && (_readingFocus || compactReader);
        Reader.SetCompact(compactReader);
        Reader.SetFocused(_readingFocus);
        Reader.Visibility = showReader ? Visibility.Visible : Visibility.Collapsed;
        ReaderSplitter.Visibility = showReader && !exclusiveReader ? Visibility.Visible : Visibility.Collapsed;
        ReaderHandleColumn.Width = new(showReader && !exclusiveReader ? 5 : 0);
        ReaderColumn.Width = showReader ? new(exclusiveReader ? 1 : 0.95, GridUnitType.Star) : new(0);
        ResumePreviewButton.Visibility = documents && compactReader && Reader.IsOpen && !showReader
            ? Visibility.Visible : Visibility.Collapsed;
        AgentHeading.Text = documents ? "Agent 工作台" : "Coding Agent";
        CodeStatus.Visibility = Views.SelectedItem == FilesTab ? Visibility.Visible : Visibility.Collapsed;
        UpdateAgentPresentation();
        if (!documents)
        {
            Views.Visibility = Visibility.Visible;
            return previousAgentWidth;
        }

        var showShelf = _libraryOpen && !_readingFocus;
        var shelfWidth = showShelf ? width < 960 ? 188 : 218 : 0;
        var agentVisible = !exclusiveReader;
        Views.Visibility = showShelf ? Visibility.Visible : Visibility.Collapsed;
        EditorAreaColumn.Width = new(shelfWidth);
        AgentSplitter.Visibility = Visibility.Collapsed;
        AgentHandleColumn.Width = new(0);
        AgentPane.Visibility = agentVisible ? Visibility.Visible : Visibility.Collapsed;
        AgentColumn.Width = agentVisible ? new(1, GridUnitType.Star) : new(0);
        return agentVisible ? (width - activity - shelfWidth) / (showReader ? 1.95 : 1) : 0;
    }

    private void DocumentStateChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(WorkbenchViewModel.HasPlan) or nameof(WorkbenchViewModel.IsBusy) or nameof(WorkbenchViewModel.HasExchange))
        {
            UpdateAgentPresentation();
            if (e.PropertyName == nameof(WorkbenchViewModel.IsBusy) && !_viewModel.IsBusy && _libraryOpen)
                _ = Documents.RefreshAsync(CommandSearch.Text);
        }
    }

    private void UpdateAgentPresentation()
    {
        var home = !_viewModel.IsBusy && !_viewModel.HasExchange;
        AgentWelcome.Visibility = home ? Visibility.Visible : Visibility.Collapsed;
        AgentTopSpace.Height = home ? new(0.85, GridUnitType.Star) : new(0);
        AgentBottomSpace.Height = home ? new(1.15, GridUnitType.Star) : new(0);
        AgentResultRow.Height = home ? new(0) : new(1, GridUnitType.Star);
        Transcript.Visibility = home ? Visibility.Collapsed : Visibility.Visible;
        UserMessage.Visibility = _viewModel.SubmittedPrompt.Length != 0 ? Visibility.Visible : Visibility.Collapsed;
        AgentResult.Visibility = _viewModel.Output.Length != 0 ? Visibility.Visible : Visibility.Collapsed;
        AgentProgress.Visibility = _viewModel.Output.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
        PlanRegion.Visibility = _viewModel.HasPlan ? Visibility.Visible : Visibility.Collapsed;
    }

    private void SetReadingFocus(bool focused)
    {
        _readingFocus = focused && Reader.IsOpen;
        if (_readingFocus)
            _documentFocused = true;
        AdaptClassicLayout();
    }

    private async void OpenDocument(string path) => await OpenDocumentAsync(path, asText: false);

    private async Task OpenDocumentAsync(string path, bool asText)
    {
        if (_switchingWorkspace)
            return;
        _documentFocused = true;
        Views.SelectedItem = DocumentsTab;
        var pending = Reader.OpenAsync(_viewModel.Workspace, path, asText);
        AdaptClassicLayout();
        await pending;
    }

    private void OpenArtifact(object sender, RoutedEventArgs e)
    {
        if (sender is Button { Tag: string path })
            OpenDocument(path);
    }

    private void ShowDocumentReader(object sender, RoutedEventArgs e)
    {
        _documentFocused = true;
        AdaptClassicLayout();
    }

    private void OpenCodeEditor(object sender, RoutedEventArgs e)
    {
        _showCompactExplorer = true;
        Views.SelectedItem = FilesTab;
        FileDesk.SetSearch(CommandSearch.Text);
    }

    internal void LoadDocumentCapture(string? markdownFile)
    {
        Views.SelectedItem = DocumentsTab;
        if (markdownFile is null)
            return;
        Reader.CaptureMarkdown(Path.GetFileName(markdownFile), File.ReadAllText(markdownFile));
        _documentFocused = true;
        ViewModel.LoadDocumentCapture(Path.GetFileName(markdownFile));
        AdaptClassicLayout();
    }
}
