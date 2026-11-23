using System.Net;
using System.Reflection;
using System.Text.Json;
using System.Text.RegularExpressions;
using SciDev.Core;

namespace SciDev.Infrastructure;

public sealed partial class PreviewUpdates(string workspace, IOperationReview reviewer, bool allowNetwork = false,
    bool denyAdditionalNetwork = false)
{
    private const string DefaultRepo = "ljk00000/SciDevHarness";
    private const string InstallerPrefix = "SciDevHarness-CSharp-Preview-Setup-";
    private const int MetadataLimit = 2_000_000;
    internal const long DownloadLimit = 1_500_000_000;
    private Func<Uri, HttpClient> _open = ApprovedHttpsClient.CreateDirectClient;

    public static string CurrentVersion { get; } =
        typeof(PreviewUpdates).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?
            .InformationalVersion.Split('+', '-')[0] ?? "1.0.0";

    public static string Repository(string? selected = null) => !string.IsNullOrWhiteSpace(selected)
        ? selected.Trim() : Environment.GetEnvironmentVariable("SCIDEV_UPDATE_REPO")?.Trim() is { Length: > 0 } configured
            ? configured : DefaultRepo;

    public sealed record Release(string Current, string Latest, bool Available, string Url, string PublishedAt,
        string Notes, string AssetUrl, string AssetName, long AssetSize, string AssetDigest);

    internal PreviewUpdates(string workspace, IOperationReview reviewer, Func<Uri, HttpClient> open)
        : this(workspace, reviewer) => _open = open;

    public async Task<Release> CheckAsync(string? repository, CancellationToken token)
    {
        var repo = Repository(repository);
        if (!RepositoryPattern().IsMatch(repo) || repo.Split('/').Any(part => part is "." or ".." || part.EndsWith('.')))
            throw new ArgumentException("发布源必须是有效的 GitHub owner/repo。");
        var target = new Uri($"https://api.github.com/repos/{repo}/releases?per_page=30");
        RequireNetwork();
        await ApproveAsync(OperationKind.CheckForUpdate, target.AbsoluteUri,
            "读取最近 30 个发布版本及 C# 预览安装包信息。", target.IdnHost, token);

        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(20));
        try
        {
            using var client = _open(target);
            using var request = Get(target);
            request.Headers.Accept.ParseAdd("application/vnd.github+json");
            request.Headers.Add("X-GitHub-Api-Version", "2022-11-28");
            using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
            if (response.StatusCode == HttpStatusCode.NotFound)
                throw Error("未找到公开的 GitHub 发布源。");
            RequireResponse(response, MetadataLimit);
            await using var body = await response.Content.ReadAsStreamAsync(deadline.Token);
            var buffer = new byte[MetadataLimit + 1];
            var count = await body.ReadAtLeastAsync(buffer, buffer.Length, false, deadline.Token);
            if (count > MetadataLimit)
                throw Error("GitHub 发布数据超过大小限制。");
            using var document = JsonDocument.Parse(buffer.AsMemory(0, count), new() { MaxDepth = 32 });
            return SelectRelease(document.RootElement, repo);
        }
        catch (JsonException) { throw Error("GitHub 发布数据格式不正确。"); }
        catch (OperationCanceledException) when (!token.IsCancellationRequested) { throw Error("检查更新超时。"); }
        catch (HttpRequestException) { throw Error("无法直连 GitHub 发布源。"); }
    }

    private static Release SelectRelease(JsonElement releases, string repo)
    {
        if (releases.ValueKind != JsonValueKind.Array)
            throw Error("GitHub 发布数据格式不正确。");
        var current = Version.Parse(CurrentVersion);
        Version? latest = null;
        Release? selected = null;
        foreach (var release in releases.EnumerateArray().Take(30))
        {
            var tag = StringValue(release, "tag_name");
            var versionTag = ReleaseTag().Match(tag);
            if (!versionTag.Success || !Version.TryParse(versionTag.Groups[1].Value, out var version) ||
                tag.Length > 80 || latest is not null && version <= latest ||
                !release.TryGetProperty("draft", out var draft) || draft.ValueKind != JsonValueKind.False ||
                !release.TryGetProperty("assets", out var assets) || assets.ValueKind != JsonValueKind.Array)
                continue;
            var name = InstallerPrefix + version + ".exe";
            var path = $"/{repo}/releases/download/{tag}/{name}";
            foreach (var asset in assets.EnumerateArray())
            {
                if (!string.Equals(StringValue(asset, "name"), name, StringComparison.Ordinal) ||
                    StringValue(asset, "state") != "uploaded" ||
                    !Uri.TryCreate(StringValue(asset, "browser_download_url"), UriKind.Absolute, out var uri) ||
                    !SafeDownloadUri(uri) || uri.IdnHost != "github.com" || uri.Query.Length != 0 ||
                    !uri.AbsolutePath.Equals(path, StringComparison.OrdinalIgnoreCase) ||
                    !asset.TryGetProperty("size", out var sizeValue) || sizeValue.ValueKind != JsonValueKind.Number ||
                    !sizeValue.TryGetInt64(out var size) || size is <= 0 or > DownloadLimit)
                    continue;
                latest = version;
                selected = new(CurrentVersion, version.ToString(), version > current,
                    $"https://github.com/{repo}/releases/tag/{tag}", StringValue(release, "published_at", 128),
                    StringValue(release, "body", 12_000), uri.AbsoluteUri, name, size, StringValue(asset, "digest", 80));
                break;
            }
        }
        return selected ?? throw Error("最近 30 个发布中没有可用的 Windows C# 预览安装包。");
    }

    public async Task<VerifiedUpdate> DownloadAsync(Release release, string destination, string? expectedSha256,
        IProgress<(long Done, long Total)>? progress, CancellationToken token)
    {
        if (!release.Available)
            throw Error("当前版本已是最新版本。");
        var expected = NormalizeDigest(expectedSha256);
        var published = NormalizeDigest(release.AssetDigest);
        if ((!string.IsNullOrWhiteSpace(expectedSha256) && expected is null) ||
            (expected is not null && published is not null && expected != published))
            throw Error("指定的 SHA-256 格式无效或与发布摘要不一致。");
        expected ??= published ?? throw Error("发布未提供有效 SHA-256，请提供可信的 --sha256 摘要。");
        var source = ApprovedHttpsClient.ParseUrl(release.AssetUrl);
        if (source.IdnHost != "github.com" || !SafeDownloadUri(source) ||
            !source.AbsolutePath.EndsWith("/" + release.AssetName, StringComparison.Ordinal) ||
            !string.Equals(release.AssetName, InstallerPrefix + release.Latest + ".exe", StringComparison.Ordinal) ||
            !Version.TryParse(release.Latest, out _) || release.AssetSize is <= 0 or > DownloadLimit)
            throw Error("发布中的预览安装包地址或大小无效。");
        var path = WindowsWorkspace.ValidateAbsoluteFilePath(Path.TrimEndingDirectorySeparator(Path.GetFullPath(destination)));
        RequireNetwork();
        await ApproveAsync(OperationKind.DownloadUpdate, path,
            $"将下载 {release.AssetName}（{release.AssetSize} 字节）并校验 SHA-256。现有文件会保留。",
            source.IdnHost, token, expected);
        using var selected = WindowsWorkspace.OpenAbsoluteFilePath(path);
        using var inDirectory = IsDownloadDirectory(selected.AbsolutePath)
            ? WindowsWorkspace.OpenAbsoluteFilePath(Path.Combine(selected.AbsolutePath, release.AssetName))
            : null;
        var file = (inDirectory ?? selected).AbsolutePath;
        if (!Path.GetExtension(file).Equals(".exe", StringComparison.OrdinalIgnoreCase))
            throw Error("安装包目标需使用 .exe 扩展名。");
        if (WindowsWorkspace.FileExists(file))
            throw Error("目标文件已存在，请选择新路径。");

        for (var hop = 0; hop <= 3; hop++)
        {
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
            deadline.CancelAfter(TimeSpan.FromMinutes(5));
            try
            {
                using var client = _open(source);
                using var request = Get(source);
                using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
                if (!IsRedirect(response.StatusCode))
                {
                    RequireResponse(response, release.AssetSize);
                    if (response.Content.Headers.ContentLength is { } length && length != release.AssetSize)
                        throw Error("响应声明的大小与发布信息不一致。");
                    await using var body = await response.Content.ReadAsStreamAsync(deadline.Token);
                    return await UpdateAssetFiles.SaveAsync(file, body, release.AssetSize, expected, progress, deadline.Token);
                }

                if (hop == 3 || response.Headers.Location is not { } redirect ||
                    !Uri.TryCreate(source, redirect, out var next) || !SafeDownloadUri(next) || next.IdnHost == "github.com")
                    throw Error("下载重定向超限或目标不属于 GitHub 发布服务。");
                await ApproveAsync(OperationKind.DownloadUpdate, file,
                    "允许本次 GitHub 发布附件下载重定向。", next.IdnHost, token);
                source = next;
            }
            catch (OperationCanceledException) when (!token.IsCancellationRequested) { throw Error("下载更新超时。"); }
            catch (HttpRequestException) { throw Error("无法直连 GitHub 安装包地址。"); }
        }
        throw Error("安装包下载超过重定向上限。");
    }

    public async Task<int> LaunchAsync(VerifiedUpdate package, CancellationToken token)
    {
        await ApproveAsync(OperationKind.LaunchUpdate, package.Path, "再次校验并启动交互式安装器。", null, token, package.Sha256);
        return await UpdateAssetFiles.LaunchAsync(package, UpdateAssetFiles.StartInstaller, token);
    }

    private void RequireNetwork()
    {
        if (allowNetwork)
            return;
        if (denyAdditionalNetwork || WorkspaceNetworkPolicy.IsEnvironmentDenied() || IsWorkspaceNetworkDenied())
            throw new ToolDeniedException("network_policy_denied", "当前策略禁止额外联网。");
    }

    private bool IsWorkspaceNetworkDenied()
    {
        try
        {
            using var location = WindowsWorkspace.OpenAbsoluteFilePath(Path.Combine(workspace, ".research", "settings.json"));
            return LegacyExecutionPolicy.Read(workspace)?.EffectiveNetwork == AgentNetworkPolicy.Deny;
        }
        catch (DirectoryNotFoundException) { return false; }
    }

    private async Task ApproveAsync(OperationKind kind, string target, string detail, string? destination,
        CancellationToken token, string? digest = null)
    {
        if (allowNetwork && destination is not null)
            detail = $"申请覆盖工作区断网策略。{Environment.NewLine}{detail}";
        var review = new OperationReview(Guid.NewGuid().ToString("N"), kind, target, detail, destination, digest);
        var decision = await OperationApproval.DecideAsync(reviewer, review, token);
        token.ThrowIfCancellationRequested();
        if (decision != "approved")
            throw new ToolDeniedException(decision, "本次更新操作未获批准。");
    }

    private static HttpRequestMessage Get(Uri target)
    {
        var request = new HttpRequestMessage(HttpMethod.Get, target);
        request.Headers.UserAgent.ParseAdd($"SciDevHarness-CSharp/{CurrentVersion}");
        return request;
    }

    private static void RequireResponse(HttpResponseMessage response, long limit)
    {
        if (response.StatusCode != HttpStatusCode.OK || response.Content.Headers.ContentEncoding.Count > 0)
            throw Error($"GitHub 更新请求返回不支持的响应（HTTP {(int)response.StatusCode}）。");
        if (response.Content.Headers.ContentLength > limit)
            throw Error("下载声明大小超过限制。");
    }

    private static bool SafeDownloadUri(Uri uri) => uri.Scheme == "https" && uri.Port == 443 &&
        uri.UserInfo.Length == 0 && uri.Fragment.Length == 0 &&
        uri.IdnHost is "github.com" or "release-assets.githubusercontent.com" or "objects.githubusercontent.com" or "github-releases.githubusercontent.com";

    private static bool IsRedirect(HttpStatusCode code) =>
        code is HttpStatusCode.Moved or HttpStatusCode.Redirect or HttpStatusCode.RedirectMethod or
            HttpStatusCode.TemporaryRedirect or HttpStatusCode.PermanentRedirect;

    private static bool IsDownloadDirectory(string path)
    {
        FileAttributes attributes;
        try { attributes = File.GetAttributes(path); }
        catch (FileNotFoundException) { return false; }
        if ((attributes & (FileAttributes.ReparsePoint | FileAttributes.Device)) != 0)
            throw Error("下载目标必须是本机的普通路径。");
        return (attributes & FileAttributes.Directory) != 0;
    }

    internal static string? NormalizeDigest(string? digest)
    {
        var value = digest?.Trim().ToLowerInvariant();
        if (value?.StartsWith("sha256:", StringComparison.Ordinal) == true)
            value = value[7..];
        return value is { Length: 64 } && value.All(Uri.IsHexDigit) ? value : null;
    }

    public static bool IsValidDigest(string? digest) => NormalizeDigest(digest) is not null;

    private static string StringValue(JsonElement value, string key, int limit = 2048)
    {
        var text = value.ValueKind == JsonValueKind.Object && value.TryGetProperty(key, out var property) &&
            property.ValueKind == JsonValueKind.String ? property.GetString() ?? "" : "";
        return text.Length <= limit ? text : text[..limit];
    }

    internal static ToolDeniedException Error(string message) => new("update_failed", message);

    [GeneratedRegex(@"\A[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\z", RegexOptions.CultureInvariant)]
    private static partial Regex RepositoryPattern();

    [GeneratedRegex(@"\A(?:csharp-preview-)?[vV]?(\d{1,9}\.\d{1,9}\.\d{1,9})\z", RegexOptions.CultureInvariant)]
    private static partial Regex ReleaseTag();
}
