using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class DevelopmentTreePage : UserControl
{
    private readonly DevelopmentTreeCanvas _tree = new();
    private DevelopmentVersion[] _nodes = [DevelopmentVersion.Root];
    private DevelopmentVersion _selected = DevelopmentVersion.Root;
    private GitWorkspace.Status? _git;
    private string? _gitNotice;
    private bool _canInitialize = true;
    private bool _detailsRequested = true;
    private bool _shortDetailsForced;
    private bool _fit;
    private double _zoom = 1;
    private readonly TranslateTransform _canvasOffset = new();
    private (Point Point, double Horizontal, double Vertical, MouseButton Button)? _pan;

    internal event RoutedEventHandler? ReturnRequested;
    internal event RoutedEventHandler? RefreshRequested;
    internal event RoutedEventHandler? AgentRequested;
    internal event Action<string?>? GitRequested;
    internal event RoutedEventHandler? InitializeRequested;
    internal event Action<RunHistory.Entry>? RetryRequested;
    internal event Action<string>? CommitRequested;

    public DevelopmentTreePage()
    {
        InitializeComponent();
        GraphHost.Content = _tree;
        _tree.RenderTransform = _canvasOffset;
        _tree.VersionSelected += SelectVersion;
        UpdateChanges();
        ApplyNodes();
    }

    internal void ChangeWorkspace()
    {
        _selected = DevelopmentVersion.Root;
        _nodes = [_selected];
        _tree.ResetOffsets();
        CommitInput.Clear();
        UpdateGit(null, canInitialize: false, null);
        SetZoom(1, fit: false);
        _canvasOffset.X = _canvasOffset.Y = 0;
        GraphScroll.ScrollToHome();
    }

    internal void SetProfile(string profile)
    {
        _tree.SetProfile(profile);
        DetailStatus.Foreground = _tree.Accent(_selected);
    }

    internal void SetHistory(RunHistory.Entry[] history, WorkbenchViewModel.QueueItem[] queue)
    {
        _nodes = DevelopmentVersion.FromHistory(history, queue);
        ApplyNodes();
    }

    internal void ShowHistoryError()
    {
        _nodes = [DevelopmentVersion.Root with { Description = "无法读取工作区的开发记录。" }];
        ApplyNodes();
    }

    internal void UpdateGit(GitWorkspace.Status? status, bool canInitialize, string? notice)
    {
        _git = status;
        _gitNotice = notice;
        _canInitialize = canInitialize;
        Head.Text = status?.Head is { Length: >= 8 } head ? head[..8] : "暂无";
        UpdateChanges();
        ApplyNodes();
    }

    private void UpdateChanges()
    {
        GitButton.Content = _canInitialize ? (ActualWidth < 900 ? "初始化" : "初始化 Git") : "Git 改动";
        Changes.ItemsSource = _git is { Changes.Length: > 0 } status
            ? status.Changes
            : new[] { _git is not null ? "工作区干净" : _gitNotice ?? "尚未读取 Git 状态" };
        Changes.DisplayMemberPath = _git is { Changes.Length: > 0 } ? nameof(GitWorkspace.Change.Display) : "";
    }

    private void ApplyNodes()
    {
        var root = _nodes[0];
        if (_git is { } git)
        {
            var head = git.Head is { Length: >= 10 } full ? full[..10] : "暂无";
            root = root with { Record = $"Git {git.Branch ?? "分离 HEAD"} · HEAD {head}" };
        }
        var nodes = new[] { root }.Concat(_nodes.Skip(1)).ToArray();
        MainCount.Text = (nodes.Count(node => node.IsMain) - 1).ToString();
        AttemptCount.Text = nodes.Count(node => !node.IsMain).ToString();
        FailedCount.Text = nodes.Count(node => node.State == DevelopmentState.Failed).ToString();
        _selected = nodes.FirstOrDefault(node => node.Id == _selected.Id) ?? root;
        _tree.SetVersions(nodes, _selected.Id);
        ShowDetails();
        Refit();
    }

    private void SelectVersion(DevelopmentVersion node)
    {
        _selected = node;
        ApplyNodes();
    }

    private void ShowDetails()
    {
        DetailTitle.Text = _selected.Title;
        DetailStatus.Text = _selected.Status;
        DetailStatus.Foreground = _tree.Accent(_selected);
        Description.Text = _selected.Description;
        Record.Text = _selected.Record;
        RetryButton.IsEnabled = (_selected.State is DevelopmentState.Failed or DevelopmentState.Retry) &&
            !string.IsNullOrWhiteSpace(_selected.Run?.Task);
    }

    private void AdaptLayout(object sender, SizeChangedEventArgs e) => PlaceDetails();

    private void PlaceDetails()
    {
        var compact = ActualWidth < 900;
        var shortLayout = ActualHeight < 492;
        var visible = _detailsRequested && (!shortLayout || _shortDetailsForced);
        PageHeader.Height = shortLayout ? 56 : 68;
        Metrics.Visibility = compact || shortLayout ? Visibility.Collapsed : Visibility.Visible;
        ReturnButton.Content = compact ? "返回" : "返回编码工作区";
        RefreshButton.Content = compact ? "刷新" : "刷新树";
        FitButton.Content = compact ? "全图" : "适应全图";
        Subtitle.Text = compact ? "主线与尝试方向" : "主线与尝试方向 · 非 Git 分支";
        GitButton.Content = _canInitialize ? compact ? "初始化" : "初始化 Git" : "Git 改动";
        // At very narrow widths the graph owns the primary area, with node details below.
        DetailsColumn.Width = visible && !compact ? new(305) : new(0);
        DetailsRow.Height = visible && compact ? new(0.62, GridUnitType.Star) : new(0);
        Grid.SetColumn(Details, compact ? 0 : 1);
        Grid.SetRow(Details, compact ? 1 : 0);
        Details.Visibility = visible ? Visibility.Visible : Visibility.Collapsed;
        ZoomHint.Visibility = ActualWidth >= 700 ? Visibility.Visible : Visibility.Collapsed;
    }

    private void ResizeGraph(object sender, SizeChangedEventArgs e)
    {
        _tree.SetViewport(new(Math.Max(0, e.NewSize.Width - 4), e.NewSize.Height));
        Refit();
    }

    private void SetZoom(double zoom, bool fit)
    {
        _zoom = Math.Clamp(zoom, 0.16, 2.2);
        _fit = fit;
        _tree.LayoutTransform = _zoom == 1 ? Transform.Identity : new ScaleTransform(_zoom, _zoom);
        ZoomHint.Text = $"{_zoom:P0} · Ctrl+滚轮缩放 · 拖动分支 / 平移画布";
    }

    private void Refit()
    {
        if (_fit && _tree.Width > 0 && _tree.Height > 0 && GraphArea.ActualWidth > 0)
            SetZoom(Math.Min(1, Math.Min(GraphArea.ActualWidth / _tree.Width, (GraphArea.ActualHeight - 53) / _tree.Height)), fit: true);
        GraphHost.Padding = _fit ? new(0, 53, 0, 0) : new(0);
    }

    private void ZoomTree(object sender, MouseWheelEventArgs e)
    {
        if (!Keyboard.Modifiers.HasFlag(ModifierKeys.Control))
            return;
        var factor = Math.Pow(1.12, e.Delta / 120d);
        SetZoom(_zoom * factor, fit: false);
        Refit();
        e.Handled = true;
    }

    private void BeginPan(object sender, MouseButtonEventArgs e)
    {
        bool canvasDrag = e.ChangedButton == MouseButton.Left && ReferenceEquals(e.OriginalSource, _tree);
        if ((!canvasDrag && e.ChangedButton != MouseButton.Middle) || !GraphScroll.CaptureMouse())
            return;
        _pan = (e.GetPosition(GraphScroll), _canvasOffset.X, _canvasOffset.Y, e.ChangedButton);
        e.Handled = true;
    }

    private void Pan(object sender, MouseEventArgs e)
    {
        if (_pan is not { } start || !GraphScroll.IsMouseCaptured)
            return;
        var delta = e.GetPosition(GraphScroll) - start.Point;
        _canvasOffset.X = Math.Clamp(start.Horizontal + delta.X, -_tree.Width * _zoom, GraphArea.ActualWidth);
        _canvasOffset.Y = Math.Clamp(start.Vertical + delta.Y, -_tree.Height * _zoom, GraphArea.ActualHeight);
        e.Handled = true;
    }

    private void EndPan(object sender, MouseButtonEventArgs e)
    {
        if (_pan is not { } start || e.ChangedButton != start.Button)
            return;
        _pan = null;
        GraphScroll.ReleaseMouseCapture();
        e.Handled = true;
    }

    private void ReturnToEditor(object sender, RoutedEventArgs e) => ReturnRequested?.Invoke(sender, e);
    private void ToggleAgent(object sender, RoutedEventArgs e) => AgentRequested?.Invoke(sender, e);
    private void RefreshTree(object sender, RoutedEventArgs e) => RefreshRequested?.Invoke(sender, e);

    private void FitTree(object sender, RoutedEventArgs e)
    {
        _fit = true;
        _canvasOffset.X = _canvasOffset.Y = 0;
        Refit();
        GraphScroll.ScrollToHome();
    }

    private void ToggleDetails(object sender, RoutedEventArgs e)
    {
        _detailsRequested = Details.Visibility != Visibility.Visible;
        _shortDetailsForced = _detailsRequested;
        PlaceDetails();
    }

    private void OpenGit(object sender, RoutedEventArgs e)
    {
        if (_canInitialize)
            InitializeRequested?.Invoke(sender, e);
        else
            OpenGitChanges(sender, e);
    }

    private void OpenChange(object sender, MouseButtonEventArgs e)
    {
        if (Changes.SelectedItem is GitWorkspace.Change item)
            GitRequested?.Invoke(item.Path);
    }

    private void OpenGitChanges(object sender, RoutedEventArgs e) => GitRequested?.Invoke(null);

    private void RetryVersion(object sender, RoutedEventArgs e)
    {
        if (_selected.Run is { } run)
            RetryRequested?.Invoke(run);
    }

    private void CommitStaged(object sender, RoutedEventArgs e) => CommitRequested?.Invoke(CommitInput.Text.Trim());
    private void SubmitMessage(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter)
            return;
        CommitStaged(sender, e);
        e.Handled = true;
    }

    internal void LoadCapture()
    {
        _nodes =
        [
            DevelopmentVersion.Root with { Description = "稳定的编码主线。", Record = "Git main" },
            new("editor-cleanup", DevelopmentVersion.MainRoot, DevelopmentState.Main, "整理指标计算", "抽离评价逻辑并补齐类型标注。", "成功 · 2026-10-01"),
            new("fast-warmup", "editor-cleanup", DevelopmentState.Failed, "缩短 warmup", "验证集波动明显，暂不保留。", "失败 · 3 次尝试"),
            new("typed-config", "editor-cleanup", DevelopmentState.Active, "类型化配置", "把配置默认值整理成 dataclass。", "进行中 · 1 次尝试"),
        ];
        UpdateGit(new("main", []), canInitialize: false, null);
        _nodes[0] = _nodes[0] with { Record = "Git main" };
        _git = null;
        ApplyNodes();
    }
}
