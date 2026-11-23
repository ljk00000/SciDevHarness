using System.Diagnostics;
using SciDev.Core;

namespace SciDev.Infrastructure;

internal sealed class ModelRequestMetrics(int attempt, int requestBytes)
{
    private readonly Stopwatch _watch = Stopwatch.StartNew();
    public int Attempt { get; } = attempt;
    public int? StatusCode { get; set; }
    public bool Success { get; set; }
    public ModelUsage? Tokens { get; set; }
    public long ResponseBytes { get; private set; }
    public long ElapsedMs { get; private set; }

    public Stream WrapBody(Stream body) => new MeteredBody(body, count => ResponseBytes += count);

    internal ModelUsage Complete(ModelUsage? previous)
    {
        ElapsedMs = _watch.ElapsedMilliseconds;
        var transport = previous?.Transport ?? new ModelTransportUsage(0, 0, 0, 0, 0, 0, 0, 0);
        return new((previous?.PromptTokens ?? 0) + (Tokens?.PromptTokens ?? 0),
            (previous?.CompletionTokens ?? 0) + (Tokens?.CompletionTokens ?? 0),
            (previous?.TotalTokens ?? 0) + (Tokens?.TotalTokens ?? 0))
        {
            Transport = transport with
            {
                RequestAttempts = transport.RequestAttempts + 1,
                RetryAttempts = transport.RetryAttempts + (Attempt > 1 ? 1 : 0),
                FailedAttempts = transport.FailedAttempts + (Success ? 0 : 1),
                RequestBodyBytes = transport.RequestBodyBytes + requestBytes,
                ResponseBodyBytes = transport.ResponseBodyBytes + ResponseBytes,
                ElapsedMs = transport.ElapsedMs + ElapsedMs,
                TokenReports = transport.TokenReports + (Tokens is not null ? 1 : 0),
            },
        };
    }

    private sealed class MeteredBody(Stream body, Action<int> onRead) : Stream
    {
        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }

        public override int Read(byte[] buffer, int offset, int count)
        {
            var read = body.Read(buffer, offset, count);
            onRead(read);
            return read;
        }

        public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
        {
            var read = await body.ReadAsync(buffer, cancellationToken).ConfigureAwait(false);
            onRead(read);
            return read;
        }

        public override void Flush() => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    }
}
