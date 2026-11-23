using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class ApprovedNetworkTools(IFileTools inside, IOperationReview? reviewer, Uri modelEndpoint,
    Action<RunEvent> record, string? credential = null, Func<Uri, CancellationToken, Task<string>>? fetch = null,
    bool denyExtraNetwork = false) : IFileTools
{
    public async ValueTask<ToolResult> ExecuteAsync(ToolCall call, CancellationToken token)
    {
        if (call.Name != "fetch_external_text")
            return await inside.ExecuteAsync(call, token);
        token.ThrowIfCancellationRequested();
        if (denyExtraNetwork)
            throw new ToolDeniedException("network_policy_denied", "断网策略禁止额外 HTTPS 请求。");
        if (reviewer is null)
            throw new ToolDeniedException("approval_denied", "额外 HTTPS 请求需要交互式审批。");

        var url = ApprovedHttpsClient.ParseUrl(ReadUrl(call.Arguments));
        ProtectCredential(url.AbsoluteUri);
        ProtectCredential(Uri.UnescapeDataString(url.AbsoluteUri));
        var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(url.AbsoluteUri)));
        await RequireAsync(new(call.Id, OperationKind.FetchExternalText, url.AbsoluteUri,
            "直连发送一次 HTTPS GET 请求并读取文本。读取后会另行确认发送给模型的内容。"), digest, token);
        var text = await (fetch ?? ApprovedHttpsClient.GetTextAsync)(url, token);
        ProtectCredential(text);
        var count = Math.Min(text.Length, ApprovedHttpsClient.MaxPreviewChars);
        if (count == 0)
            throw new ToolDeniedException("network_empty", "HTTPS 响应没有可预览的文本。");
        if (char.IsHighSurrogate(text[count - 1]))
            count--;
        var preview = text[..count];
        await RequireAsync(new(call.Id, OperationKind.ShareNetworkText, url.AbsoluteUri,
            "将以下只读预览发送给当前模型。模型回复可能引用这些内容。",
            modelEndpoint.GetLeftPart(UriPartial.Path), Before: preview), digest, token);
        return new(preview, ExternalDataShared: true);
    }

    private async ValueTask RequireAsync(OperationReview request, string digest, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        var audit = new { id = request.CallId, action = request.Kind.ToString(), urlSha256 = digest };
        record(new("network_review_requested", 0, audit));
        var decision = await OperationApproval.DecideAsync(reviewer!, request, token);
        record(new("network_review_decided", 0, new { review = audit, outcome = decision }));
        token.ThrowIfCancellationRequested();
        if (decision != "approved")
            throw new ToolDeniedException("approval_denied", "本次 HTTPS 读取或发送未获确认。");
    }

    private void ProtectCredential(string value)
    {
        if (!string.IsNullOrEmpty(credential) && value.Contains(credential, StringComparison.Ordinal))
            throw new ToolDeniedException("network_credential", "联网地址或文本含有当前模型凭据。");
    }

    private static string ReadUrl(string arguments)
    {
        try
        {
            using var document = JsonDocument.Parse(arguments);
            var value = document.RootElement;
            if (value.ValueKind == JsonValueKind.Object && value.EnumerateObject().Count() == 1 &&
                value.TryGetProperty("url", out var url) && url.ValueKind == JsonValueKind.String)
                return url.GetString()!;
        }
        catch (JsonException) { }
        catch (InvalidOperationException) { }
        throw new ToolDeniedException("invalid_tool", "fetch_external_text 只接受一个 url 字符串。");
    }
}
