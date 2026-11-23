using System.Globalization;
using System.Text;
using System.Text.Json;

namespace SciDev.Infrastructure;

// LSP uses UTF-8 JSON with an ASCII Content-Length header, including over stdio.
internal sealed class LanguageServerFrames(Stream input, Stream output)
{
    private const int MaxFrameBytes = 4 * 1024 * 1024;
    private const int MaxHeaderBytes = 4096;
    private readonly byte[] _buffer = new byte[4096];
    private int _start;
    private int _end;

    public async ValueTask SendAsync(object packet, CancellationToken token)
    {
        var body = JsonSerializer.SerializeToUtf8Bytes(packet);
        if (body.Length > MaxFrameBytes)
            throw new InvalidDataException("语言服务请求过大。");
        var header = Encoding.ASCII.GetBytes(FormattableString.Invariant($"Content-Length: {body.Length}\r\n\r\n"));
        await input.WriteAsync(header, token).ConfigureAwait(false);
        await input.WriteAsync(body, token).ConfigureAwait(false);
        await input.FlushAsync(token).ConfigureAwait(false);
    }

    public async ValueTask<JsonElement> ReadAsync(CancellationToken token)
    {
        var header = new StringBuilder();
        while (header.Length < MaxHeaderBytes)
        {
            if (_start == _end)
            {
                _end = await output.ReadAsync(_buffer, token).ConfigureAwait(false);
                _start = 0;
                if (_end == 0)
                    throw new EndOfStreamException("语言服务已关闭连接。");
            }
            var next = _buffer[_start++];
            if (next is < 0x20 and not (9 or 10 or 13) || next > 0x7E)
                throw new InvalidDataException("语言服务响应头无效。");
            header.Append((char)next);
            if (header.Length >= 4 && header[^4] == '\r' && header[^3] == '\n' && header[^2] == '\r' && header[^1] == '\n')
                return await ReadJsonAsync(ContentLength(header.ToString()), token).ConfigureAwait(false);
        }
        throw new InvalidDataException("语言服务响应头过大。");
    }

    private async ValueTask<JsonElement> ReadJsonAsync(int length, CancellationToken token)
    {
        var payload = new byte[length];
        var count = Math.Min(length, _end - _start);
        _buffer.AsSpan(_start, count).CopyTo(payload);
        _start += count;
        await output.ReadExactlyAsync(payload.AsMemory(count), token).ConfigureAwait(false);
        using var parsed = JsonDocument.Parse(payload, new JsonDocumentOptions { MaxDepth = 48 });
        if (parsed.RootElement.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("语言服务返回了无效的 JSON-RPC 消息。");
        return parsed.RootElement.Clone();
    }

    private static int ContentLength(string header)
    {
        var length = 0;
        foreach (var line in header.Split("\r\n", StringSplitOptions.RemoveEmptyEntries))
        {
            var separator = line.IndexOf(':');
            if (separator <= 0)
                throw new InvalidDataException("语言服务响应头无效。");
            if (!line.AsSpan(0, separator).Equals("Content-Length", StringComparison.OrdinalIgnoreCase))
                continue;
            if (length != 0 || !int.TryParse(line.AsSpan(separator + 1).Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out length) ||
                length is < 1 or > MaxFrameBytes)
                throw new InvalidDataException("语言服务响应长度无效。");
        }
        return length > 0 ? length : throw new InvalidDataException("语言服务响应缺少 Content-Length。");
    }
}
