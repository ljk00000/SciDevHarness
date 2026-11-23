using Microsoft.Win32.SafeHandles;
using static SciDev.Infrastructure.ConPtyNative;

namespace SciDev.Infrastructure;

internal sealed class LocalStdioProcess(SafeFileHandle process, SafeFileHandle job,
    FileStream input, FileStream output, FileStream stderr) : IAsyncDisposable
{
    private readonly Task _drainError = Task.Run(async () =>
    {
        // Protocol messages and progress go over stdout. Never copy child diagnostics to a log.
        try { await stderr.CopyToAsync(Stream.Null); }
        catch (IOException) { }
    });

    public Stream Input => input;
    public Stream Output => output;

    internal static LocalStdioProcess Start(string executable, IReadOnlyList<string> args, string cwd,
        IReadOnlyDictionary<string, string> env)
    {
        var job = NewJob();
        Pipes? pipes = null;
        Pipes? errors = null;
        SafeFileHandle? process = null;
        try
        {
            pipes = new Pipes();
            errors = new Pipes();
            process = StartApprovedProcess(executable, HostCommandProcess.BuildCommandLine(executable, args),
                cwd, HostCommandProcess.EnvironmentBlock(env), job, pipes, errors.Output);
            pipes.Input.Dispose();
            pipes.Output.Dispose();
            errors.Input.Dispose();
            errors.Writer.Dispose();
            errors.Output.Dispose();
            return new(process, job, new(pipes.Writer, FileAccess.Write, bufferSize: 1),
                new(pipes.Reader, FileAccess.Read), new(errors.Reader, FileAccess.Read));
        }
        catch
        {
            job.Dispose();
            process?.Dispose();
            pipes?.Dispose();
            errors?.Dispose();
            throw;
        }
    }

    // Closing the Windows job unblocks synchronous anonymous pipe I/O and kills descendants.
    public void Abort() => job.Dispose();

    public async ValueTask DisposeAsync()
    {
        try
        {
            input.Dispose();
            for (var attempt = 0; attempt < 10 && WaitForSingleObject(process, 0) == 258; attempt++)
                await Task.Delay(25);
        }
        finally
        {
            Abort();
            process.Dispose();
            output.Dispose();
            try { await _drainError.WaitAsync(TimeSpan.FromSeconds(5)); }
            finally { stderr.Dispose(); }
        }
    }
}
