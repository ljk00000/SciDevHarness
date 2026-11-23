using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Channels;
using Microsoft.Win32.SafeHandles;
using static SciDev.Infrastructure.ConPtyNative;

namespace SciDev.Infrastructure;

/// <summary>Interactive Windows PowerShell launched by the desktop user.</summary>
public static class TerminalSession
{
    internal delegate Task<SafeFileHandle> LaunchProcess(IntPtr console, SafeFileHandle job, string directory, CancellationToken token);

    public abstract record Input
    {
        public sealed record Text(string Value) : Input;
        public sealed record Resize(int Columns, int Rows) : Input;
    }

    public sealed record Result(int ExitCode, bool Resized);

    public static Task<Result> RunAsync(string workspace, int columns, int rows, ChannelReader<Input> input,
        Action<string> output, CancellationToken token) => RunCoreAsync(workspace, columns, rows, input, output, token, script: null);

    public static Task<Result> RunScreenAsync(string workspace, int columns, int rows, ChannelReader<Input> input,
        Action<string> output, CancellationToken token) =>
        RunCoreAsync(workspace, columns, rows, input, output, token, script: null, rawOutput: true);

    internal static Task<Result> RunCoreAsync(string workspace, int columns, int rows, ChannelReader<Input> input,
        Action<string> output, CancellationToken token, string? script, bool rawOutput = false) =>
        Task.Run(() => RunWorkerAsync(workspace, columns, rows, input, output, token, script, rawOutput, launch: null), token);

    internal static Task<Result> RunDebuggerAsync(string workspace, int columns, int rows, ChannelReader<Input> input,
        Action<string> output, CancellationToken token, LaunchProcess launch) =>
        Task.Run(() => RunWorkerAsync(workspace, columns, rows, input, output, token, script: null, rawOutput: true, launch), token);

    private static async Task<Result> RunWorkerAsync(string workspace, int columns, int rows, ChannelReader<Input> input,
        Action<string> output, CancellationToken token, string? script, bool rawOutput, LaunchProcess? launch)
    {
        var directory = Path.GetFullPath(workspace);
        _ = new WindowsWorkspace(directory);
        using var pipes = new Pipes();
        using var job = NewJob();
        Marshal.ThrowExceptionForHR(CreatePseudoConsole(Size(columns, rows), pipes.Input, pipes.Output, 0, out var console));
        var read = Task.Run(() => Drain(pipes.Reader, output, rawOutput));
        using var stopInput = CancellationTokenSource.CreateLinkedTokenSource(token);
        var write = Task.FromResult(false);
        var consoleLock = new object();
        uint code;
        try
        {
            token.ThrowIfCancellationRequested();
            using var process = launch is null ? StartShell(console, job, directory, script)
                : await launch(console, job, directory, token);
            pipes.Input.Dispose();
            pipes.Output.Dispose();
            write = Task.Run(async () =>
            {
                using var destination = new FileStream(pipes.Writer, FileAccess.Write);
                var resized = false;
                try
                {
                    await foreach (var entry in input.ReadAllAsync(stopInput.Token))
                    {
                        switch (entry)
                        {
                            case Input.Resize size:
                                lock (consoleLock)
                                    if (console != IntPtr.Zero)
                                        resized |= ResizePseudoConsole(console, Size(size.Columns, size.Rows)) == 0;
                                break;
                            case Input.Text text:
                                destination.Write(Encoding.UTF8.GetBytes(text.Value));
                                destination.Flush();
                                break;
                        }
                    }
                }
                catch (OperationCanceledException) when (stopInput.IsCancellationRequested) { }
                catch (IOException) when (stopInput.IsCancellationRequested) { }
                return resized;
            });
            while (true)
            {
                token.ThrowIfCancellationRequested();
                var state = WaitForSingleObject(process, 0);
                if (state == 0)
                    break;
                if (state != 258)
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                if (write.IsFaulted)
                    await write;
                await Task.Delay(35, token);
            }
            if (!GetExitCodeProcess(process, out code))
                throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        finally
        {
            stopInput.Cancel();
            job.Dispose(); // Kill descendants before closing the pseudoconsole.
            pipes.Input.Dispose();
            pipes.Output.Dispose();
            lock (consoleLock)
            {
                ClosePseudoConsole(console);
                console = IntPtr.Zero;
            }
            await read;
            await write;
        }
        return new(unchecked((int)code), write.Result);
    }

    private static void Drain(SafeFileHandle handle, Action<string> output, bool rawOutput)
    {
        using var stream = new FileStream(handle, FileAccess.Read);
        using var reader = new StreamReader(stream, Encoding.UTF8);
        var filter = new TerminalText();
        var buffer = new char[4096];
        int count;
        while ((count = reader.Read(buffer, 0, buffer.Length)) != 0)
        {
            var text = rawOutput ? new string(buffer, 0, count) : filter.Read(buffer.AsSpan(0, count));
            if (text.Length > 0)
                output(text);
        }
    }

    private static Coord Size(int columns, int rows)
    {
        var size = TerminalScreen.Dimensions(columns, rows);
        return new((short)size.Columns, (short)size.Rows);
    }
}
