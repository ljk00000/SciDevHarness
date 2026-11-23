using System.Globalization;
using System.Text;

namespace SciDev.Infrastructure;

public sealed class McpStdioFrames(Stream input, int maxFrameBytes = 64 * 1024, bool? usesLengthHeaders = null)
{
    private readonly byte[] _buffer = new byte[8 * 1024];
    private int _offset;
    private int _length;
    private bool _negotiated = usesLengthHeaders.HasValue;

    public sealed record Frame(byte[] Payload, string? Error = null, bool Fatal = false);
    public bool UsesLengthHeaders { get; private set; } = usesLengthHeaders ?? false;

    public async ValueTask<Frame?> ReadAsync(CancellationToken token)
    {
        var line = await ReadLineAsync(token);
        if (line is null)
            return null;
        if (!_negotiated && (line.Error is not null || line.Payload.Any(value => value is not ((byte)' ' or (byte)'\r' or (byte)'\t'))))
        {
            // SciDev's original Python client used Content-Length frames. Standard MCP uses JSON lines.
            UsesLengthHeaders = line.Error is null && Header(line.Payload).StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase);
            _negotiated = true;
        }
        if (!UsesLengthHeaders)
            return line;

        var header = Header(line.Payload);
        if (line.Error is not null || !header.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase) ||
            !int.TryParse(header.AsSpan("Content-Length:".Length).Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out var size) ||
            size <= 0 || size > maxFrameBytes)
            return new([], "无效或超限的 Content-Length 请求帧。", Fatal: true);
        var boundary = await ReadLineAsync(token);
        if (boundary is null || boundary.Error is not null || boundary.Payload is not ([] or [(byte)'\r']))
            return new([], "Content-Length 请求帧缺少空行。", Fatal: true);

        var payload = new byte[size];
        for (var received = 0; received < size;)
        {
            if (!await BufferAsync(token))
                return new([], "Content-Length 请求帧提前结束。", Fatal: true);
            var count = Math.Min(size - received, _length - _offset);
            Buffer.BlockCopy(_buffer, _offset, payload, received, count);
            _offset += count;
            received += count;
        }
        return new(payload);
    }

    private async ValueTask<Frame?> ReadLineAsync(CancellationToken token)
    {
        using var data = new MemoryStream();
        var tooLong = false;
        while (true)
        {
            if (!await BufferAsync(token))
                return data.Length == 0 && !tooLong ? null : Build();
            var newline = Array.IndexOf(_buffer, (byte)'\n', _offset, _length - _offset);
            var end = newline >= 0 ? newline : _length;
            var count = end - _offset;
            if (!tooLong && data.Length + count > maxFrameBytes)
                tooLong = true;
            if (!tooLong)
                data.Write(_buffer, _offset, count);
            _offset = end + (newline >= 0 ? 1 : 0);
            if (newline >= 0)
                return Build();
        }

        Frame Build() => tooLong ? new([], $"报文超过 {maxFrameBytes / 1024} KiB。") : new(data.ToArray());
    }

    private async ValueTask<bool> BufferAsync(CancellationToken token)
    {
        if (_offset < _length)
            return true;
        _offset = 0;
        _length = await input.ReadAsync(_buffer.AsMemory(), token);
        return _length > 0;
    }

    private static string Header(byte[] line) => Encoding.ASCII.GetString(line).TrimEnd('\r');

    public static async ValueTask WriteAsync(Stream output, ReadOnlyMemory<byte> bytes, bool usesLengthHeaders, CancellationToken token)
    {
        if (usesLengthHeaders)
            await output.WriteAsync(Encoding.ASCII.GetBytes($"Content-Length: {bytes.Length}\r\n\r\n"), token);
        await output.WriteAsync(bytes, token);
        if (!usesLengthHeaders)
            await output.WriteAsync("\n"u8.ToArray(), token);
        await output.FlushAsync(token);
    }
}
