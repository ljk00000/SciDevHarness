using SciDev.Core;

namespace SciDev.Infrastructure;

/// <summary>Bounds actual bytes read, including a chunked body without Content-Length.</summary>
internal sealed class McpHttpResponseStream(Stream input) : Stream
{
    public const int MaxBytes = 4_000_000;
    private int _remaining = MaxBytes;

    public override int Read(byte[] buffer, int offset, int count) => Read(buffer.AsSpan(offset, count));

    public override int Read(Span<byte> buffer) => Count(input.Read(buffer[..Math.Min(buffer.Length, _remaining + 1)]));

    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken token = default) =>
        Count(await input.ReadAsync(buffer[..Math.Min(buffer.Length, _remaining + 1)], token));

    public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken token) =>
        ReadAsync(buffer.AsMemory(offset, count), token).AsTask();

    private int Count(int count)
    {
        _remaining -= count;
        if (_remaining < 0)
            throw Limit();
        return count;
    }

    public static ToolDeniedException Limit() => new("mcp_response_limit", "MCP HTTP 响应超过 4 MB。");

    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() => throw new NotSupportedException();
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
}
