using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace SciDev.Infrastructure;

internal static partial class ConPtyNative
{
    internal static SafeFileHandle NewJob()
    {
        var job = CreateJobObjectW(IntPtr.Zero, IntPtr.Zero);
        var limits = new JobLimits { LimitFlags = 0x2000 }; // KILL_ON_JOB_CLOSE
        if (!job.IsInvalid && SetInformationJobObject(job, 9, ref limits, Marshal.SizeOf<JobLimits>()))
            return job;
        var error = new Win32Exception(Marshal.GetLastWin32Error());
        job.Dispose();
        throw error;
    }

    internal static SafeFileHandle StartShell(IntPtr console, SafeFileHandle job, string directory, string? script)
    {
        var executable = Path.Combine(Environment.SystemDirectory, "WindowsPowerShell", "v1.0", "powershell.exe");
        var command = new StringBuilder($"\"{executable}\" -NoLogo -NoProfile");
        if (script is not null)
            command.Append(" -EncodedCommand ").Append(Convert.ToBase64String(Encoding.Unicode.GetBytes(script)));
        return StartConsoleProcess(console, job, directory, executable, command);
    }

    internal static SafeFileHandle StartConsoleProcess(IntPtr console, SafeFileHandle job, string directory,
        string executable, StringBuilder command)
    {
        IntPtr size = IntPtr.Zero;
        _ = InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
        if (size == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error());
        var list = Marshal.AllocHGlobal(size);
        try
        {
            if (!InitializeProcThreadAttributeList(list, 1, 0, ref size))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                if (!UpdateProcThreadAttribute(list, 0, new IntPtr(0x00020016), console,
                    new IntPtr(IntPtr.Size), IntPtr.Zero, IntPtr.Zero))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                // Do not inherit standard handles from a redirected host.
                var startup = new StartupInfoEx
                {
                    Startup = new StartupInfo { Cb = Marshal.SizeOf<StartupInfoEx>(), Flags = 0x100 },
                    Attributes = list,
                };
                var environment = Marshal.StringToHGlobalUni(ShellEnvironment());
                try
                {
                    const uint extendedStartup = 0x00080000, unicodeEnvironment = 0x400, suspended = 0x4;
                    if (!CreateProcessW(executable, command, IntPtr.Zero, IntPtr.Zero, false,
                        extendedStartup | unicodeEnvironment | suspended, environment, directory, ref startup, out var info))
                        throw new Win32Exception(Marshal.GetLastWin32Error());

                    using var thread = new SafeFileHandle(info.Thread, ownsHandle: true);
                    var process = new SafeFileHandle(info.Process, ownsHandle: true);
                    if (AssignProcessToJobObject(job, process) && ResumeThread(thread) != uint.MaxValue)
                        return process;
                    var error = new Win32Exception(Marshal.GetLastWin32Error());
                    _ = TerminateProcess(process, 1);
                    process.Dispose();
                    throw error;
                }
                finally { Marshal.FreeHGlobal(environment); }
            }
            finally { DeleteProcThreadAttributeList(list); }
        }
        finally { Marshal.FreeHGlobal(list); }
    }

    private static string ShellEnvironment()
    {
        var system = Environment.SystemDirectory;
        var root = Path.GetDirectoryName(system)!;
        var programs = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        var variables = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["SystemRoot"] = root, ["WINDIR"] = root, ["ComSpec"] = Path.Combine(system, "cmd.exe"),
            ["PATH"] = string.Join(';', system, root, Path.Combine(system, "WindowsPowerShell", "v1.0"),
                Path.Combine(programs, "Git", "cmd"), Path.Combine(programs, "dotnet")),
            ["PATHEXT"] = ".COM;.EXE;.BAT;.CMD",
        };
        foreach (var key in new[] { "TEMP", "TMP", "USERNAME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramData" })
            if (Environment.GetEnvironmentVariable(key) is { } value)
                variables[key] = value;
        return string.Join('\0', variables.Select(entry => $"{entry.Key}={entry.Value}")) + "\0\0";
    }

    internal sealed class Pipes : IDisposable
    {
        public SafeFileHandle Input { get; }
        public SafeFileHandle Writer { get; }
        public SafeFileHandle Reader { get; }
        public SafeFileHandle Output { get; }

        public Pipes()
        {
            if (!CreatePipe(out var input, out var writer, IntPtr.Zero, 0))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            Input = input;
            Writer = writer;
            if (!CreatePipe(out var reader, out var output, IntPtr.Zero, 0))
            {
                var error = new Win32Exception(Marshal.GetLastWin32Error());
                input.Dispose();
                writer.Dispose();
                throw error;
            }
            Reader = reader;
            Output = output;
        }

        public void Dispose()
        {
            Input.Dispose();
            Writer.Dispose();
            Reader.Dispose();
            Output.Dispose();
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    internal readonly record struct Coord(short X, short Y);
    [StructLayout(LayoutKind.Sequential)]
    private struct StartupInfo
    {
        public int Cb;
        public IntPtr Reserved, Desktop, Title;
        public uint X, Y, XSize, YSize, XCount, YCount, Fill, Flags;
        public ushort Show, ReservedSize;
        public IntPtr ReservedData, StandardInput, StandardOutput, StandardError;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct StartupInfoEx
    {
        public StartupInfo Startup;
        public IntPtr Attributes;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInfo
    {
        public IntPtr Process, Thread;
        public uint ProcessId, ThreadId;
    }
    // JOBOBJECT_EXTENDED_LIMIT_INFORMATION, including the six IO_COUNTERS.
    [StructLayout(LayoutKind.Sequential)]
    private struct JobLimits
    {
        public long ProcessTime, JobTime;
        public uint LimitFlags;
        public nuint MinimumWorkingSet, MaximumWorkingSet;
        public uint ActiveProcessLimit;
        public nuint Affinity;
        public uint PriorityClass, SchedulingClass;
        public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
        public nuint ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CreatePipe(out SafeFileHandle read, out SafeFileHandle write, IntPtr attributes, uint size);
    [DllImport("kernel32.dll")]
    internal static extern int CreatePseudoConsole(Coord size, SafeFileHandle input, SafeFileHandle output, uint flags, out IntPtr console);
    [DllImport("kernel32.dll")]
    internal static extern int ResizePseudoConsole(IntPtr console, Coord size);
    [DllImport("kernel32.dll")]
    internal static extern void ClosePseudoConsole(IntPtr console);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, int flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr bytes,
        IntPtr previous, IntPtr previousBytes);
    [DllImport("kernel32.dll")]
    private static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern bool CreateProcessW(string program, StringBuilder command, IntPtr processAttributes, IntPtr threadAttributes,
        bool inheritHandles, uint flags, IntPtr environment, string directory, ref StartupInfoEx startup, out ProcessInfo info);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern uint WaitForSingleObject(SafeFileHandle process, uint timeout);
    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool GetExitCodeProcess(SafeFileHandle process, out uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(SafeFileHandle process, uint exitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern SafeFileHandle CreateJobObjectW(IntPtr attributes, IntPtr name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(SafeFileHandle job, int infoClass, ref JobLimits info, int length);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(SafeFileHandle job, SafeFileHandle child);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(SafeFileHandle thread);
}
