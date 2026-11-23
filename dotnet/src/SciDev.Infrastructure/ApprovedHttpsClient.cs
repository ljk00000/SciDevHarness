using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal static class ApprovedHttpsClient
{
    internal const int MaxBytes = 64 * 1024;
    internal const int MaxPreviewChars = 8192;
    private static readonly string[] PrivateSuffixes = [".localhost", ".local", ".internal", ".lan", ".test",
        ".invalid", ".example", ".onion", ".home.arpa"];

    public static Uri ParseUrl(string url)
    {
        if (url.Length is < 1 or > 2048 || url.Any(ch => char.IsWhiteSpace(ch) || char.IsControl(ch) || ch == '\\') ||
            !Uri.TryCreate(url, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps || uri.Port != 443 ||
            uri.HostNameType != UriHostNameType.Dns || uri.IsLoopback || uri.UserInfo.Length > 0 || uri.Fragment.Length > 0)
            throw InvalidTarget();
        var host = uri.IdnHost.TrimEnd('.');
        if (!host.Contains('.') || PrivateSuffixes.Any(suffix => host.EndsWith(suffix, StringComparison.OrdinalIgnoreCase)))
            throw InvalidTarget();
        return uri;
    }

    public static async Task<string> GetTextAsync(Uri url, CancellationToken token)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(12));
        using var client = CreateDirectClient(url);
        using var request = new HttpRequestMessage(HttpMethod.Get, url)
        {
            Version = HttpVersion.Version11,
            VersionPolicy = HttpVersionPolicy.RequestVersionExact,
        };
        try
        {
            using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token);
            return await ReadTextAsync(response, deadline.Token);
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        {
            throw new ToolDeniedException("network_timeout", "HTTPS 请求达到时间上限。");
        }
        catch (Exception ex) when (ex is HttpRequestException or IOException or SocketException)
        {
            throw new ToolDeniedException("network_fetch_failed", "无法通过直连 HTTPS 读取已批准的公网地址。", ex);
        }
    }

    internal static HttpClient CreateDirectClient(Uri url)
    {
        var attemptedConnection = 0;
        var handler = new SocketsHttpHandler
        {
            AllowAutoRedirect = false,
            UseProxy = false,
            UseCookies = false,
            AutomaticDecompression = DecompressionMethods.None,
            MaxResponseHeadersLength = 16,
            MaxResponseDrainSize = 0,
            SslOptions = new() { CertificateChainPolicy = new X509ChainPolicy { DisableCertificateDownloads = true } },
            ConnectCallback = async (context, cancellation) =>
            {
                if (Interlocked.Exchange(ref attemptedConnection, 1) != 0 ||
                    context.DnsEndPoint.Port != url.Port ||
                    !context.DnsEndPoint.Host.Equals(url.IdnHost, StringComparison.OrdinalIgnoreCase))
                    throw new HttpRequestException("连接目标与已批准地址不符。");
                var addresses = await Dns.GetHostAddressesAsync(url.IdnHost.TrimEnd('.') + ".", cancellation);
                if (addresses.Length == 0 || addresses.Any(address => !IsPublicAddress(address)))
                    throw new HttpRequestException("目标未解析为公网地址。");
                var address = addresses.FirstOrDefault(item => item.AddressFamily == AddressFamily.InterNetwork) ?? addresses[0];
                var socket = new Socket(address.AddressFamily, SocketType.Stream, ProtocolType.Tcp);
                try
                {
                    await socket.ConnectAsync(new IPEndPoint(address, url.Port), cancellation);
                    return new NetworkStream(socket, ownsSocket: true);
                }
                catch
                {
                    socket.Dispose();
                    throw;
                }
            },
        };
        return new HttpClient(handler) { Timeout = Timeout.InfiniteTimeSpan };
    }

    internal static async Task<string> ReadTextAsync(HttpResponseMessage response, CancellationToken token)
    {
        if (!response.IsSuccessStatusCode)
            throw new ToolDeniedException("network_http_status", $"HTTPS 请求返回状态码 {(int)response.StatusCode}。");
        var type = response.Content.Headers.ContentType;
        var media = type?.MediaType?.ToLowerInvariant();
        var charset = type?.CharSet?.Trim('"')?.ToLowerInvariant();
        if (!(media?.StartsWith("text/", StringComparison.Ordinal) == true || media == "application/json" ||
            media?.EndsWith("+json", StringComparison.Ordinal) == true) ||
            charset is not (null or "utf-8" or "utf8" or "us-ascii") || response.Content.Headers.ContentEncoding.Count != 0)
            throw new ToolDeniedException("network_content_type", "此工具读取未压缩的 UTF-8 文本或 JSON。");
        if (response.Content.Headers.ContentLength > MaxBytes)
            throw Limit();
        await using var stream = await response.Content.ReadAsStreamAsync(token);
        var data = new byte[MaxBytes + 1];
        var length = 0;
        while (length < data.Length)
        {
            var count = await stream.ReadAsync(data.AsMemory(length), token);
            if (count == 0)
                break;
            length += count;
        }
        if (length > MaxBytes)
            throw Limit();

        string decoded;
        try { decoded = new UTF8Encoding(false, true).GetString(data, 0, length); }
        catch (DecoderFallbackException)
        {
            throw new ToolDeniedException("network_content_type", "响应内容不是有效的 UTF-8。");
        }
        var text = new string(decoded.Where(ch => (!char.IsControl(ch) || ch is '\n' or '\t' or '\r') &&
            char.GetUnicodeCategory(ch) != UnicodeCategory.Format).ToArray()).Trim();
        if (text.Length == 0)
            throw new ToolDeniedException("network_empty", "HTTPS 响应没有可预览的文本。");
        return text;
    }

    internal static bool IsPublicAddress(IPAddress address)
    {
        var bytes = address.GetAddressBytes();
        if (address.AddressFamily == AddressFamily.InterNetwork)
        {
            return bytes[0] switch
            {
                0 or 10 or 127 or >= 224 => false,
                100 => bytes[1] is < 64 or > 127,
                169 => bytes[1] != 254,
                172 => bytes[1] is < 16 or > 31,
                192 => bytes[1] != 168 && !(bytes[1] == 0 && bytes[2] is 0 or 2) &&
                    !(bytes[1] == 88 && bytes[2] == 99),
                198 => bytes[1] is not (18 or 19) && !(bytes[1] == 51 && bytes[2] == 100),
                203 => !(bytes[1] == 0 && bytes[2] == 113),
                _ => true,
            };
        }
        return address.AddressFamily == AddressFamily.InterNetworkV6 && (bytes[0] & 0xe0) == 0x20 &&
            !(bytes[0] == 0x20 && (bytes[1] == 0x02 ||
                bytes[1] == 0x01 && (bytes[2] < 2 || bytes[2] == 0x0d && bytes[3] == 0xb8))) &&
            !(bytes[0] == 0x3f && bytes[1] == 0xff && bytes[2] < 0x10);
    }

    private static ToolDeniedException InvalidTarget() =>
        new("network_target_denied", "请选择 443 端口的公网 HTTPS 域名，URL 使用完整地址且不含身份信息或片段。");
    private static ToolDeniedException Limit() => new("network_response_limit", "HTTPS 文本响应超出 64 KiB 上限。");
}
