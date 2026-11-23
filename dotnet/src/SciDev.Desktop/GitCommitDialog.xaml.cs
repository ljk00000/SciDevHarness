using System.Windows;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

public partial class GitCommitDialog : Window
{
    internal GitCommitDialog(Window owner, GitCommit.Preview preview, string message, string? name, string? email)
    {
        InitializeComponent();
        Owner = owner;
        Resources.MergedDictionaries.Add(WorkbenchAppearance.Create(
            owner is WorkbenchWindow workbench ? workbench.AppearanceProfile : "paper"));
        Resources.MergedDictionaries.Add(new ResourceDictionary
        {
            Source = new Uri("pack://application:,,,/SciDev.Desktop;component/WorkbenchControls.xaml", UriKind.Absolute),
        });
        TargetBranch.Text = $"{preview.Reference["refs/heads/".Length..]} · {preview.Paths.Length} 个暂存文件";
        StagedPaths.ItemsSource = preview.Paths;
        AuthorName.Text = name ?? preview.Name ?? "";
        AuthorEmail.Text = email ?? preview.Email ?? "";
        CommitMessage.Text = message;
        Loaded += (_, _) => CommitMessage.Focus();
    }

    internal string Message => CommitMessage.Text;
    internal string DraftName => AuthorName.Text;
    internal string DraftEmail => AuthorEmail.Text;

    private void Confirm(object sender, RoutedEventArgs args)
    {
        if (string.IsNullOrWhiteSpace(Message) || string.IsNullOrWhiteSpace(DraftName) ||
            !DraftEmail.Trim().Contains('@') || DraftEmail.Trim().Any(char.IsWhiteSpace))
        {
            Validation.Text = "请填写提交说明、提交者姓名和邮箱。";
            return;
        }
        DialogResult = true;
    }
}
