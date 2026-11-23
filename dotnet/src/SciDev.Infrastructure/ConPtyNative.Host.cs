using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace SciDev.Infrastructure;

internal static partial class ConPtyNative
{
    internal static SafeFileHandle StartApprovedProcess(string executable, StringBuilder arguments, string directory,
        string environment, SafeFileHandle job, Pipes pipes, SafeFileHandle? separateError = null)
    {
        const uint inherit = 1;
        if (!SetHandleInformation(pipes.Input, inherit, inherit) || !SetHandleInformation(pipes.Output, inherit, inherit) ||
            (separateError is not null && !SetHandleInformation(separateError, inherit, inherit)))
            throw new Win32Exception(Marshal.GetLastWin32Error());

        IntPtr size = IntPtr.Zero;
        _ = InitializeProcThreadAttributeList(IntPtr.Zero, 2, 0, ref size);
        if (size == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error());
        var childHandles = separateError is null ? 2 : 3;
        var list = Marshal.AllocHGlobal(size + (childHandles + 1) * IntPtr.Size);
        try
        {
            if (!InitializeProcThreadAttributeList(list, 2, 0, ref size))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            try
            {
                // Attribute values must remain allocated through CreateProcessW.
                var handles = list + (int)size;
                Marshal.WriteIntPtr(handles, pipes.Input.DangerousGetHandle());
                Marshal.WriteIntPtr(handles, IntPtr.Size, pipes.Output.DangerousGetHandle());
                if (separateError is not null)
                    Marshal.WriteIntPtr(handles, 2 * IntPtr.Size, separateError.DangerousGetHandle());
                var assignment = handles + childHandles * IntPtr.Size;
                Marshal.WriteIntPtr(assignment, job.DangerousGetHandle());
                var handleList = new IntPtr(0x00020002);
                var jobList = new IntPtr(0x0002000D);
                if (!UpdateProcThreadAttribute(list, 0, handleList, handles, new IntPtr(childHandles * IntPtr.Size), IntPtr.Zero, IntPtr.Zero) ||
                    !UpdateProcThreadAttribute(list, 0, jobList, assignment, new IntPtr(IntPtr.Size), IntPtr.Zero, IntPtr.Zero))
                    throw new Win32Exception(Marshal.GetLastWin32Error());

                var startup = new StartupInfoEx
                {
                    Startup = new StartupInfo
                    {
                        Cb = Marshal.SizeOf<StartupInfoEx>(), Flags = 0x101, // STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
                        StandardInput = pipes.Input.DangerousGetHandle(),
                        StandardOutput = pipes.Output.DangerousGetHandle(), StandardError = (separateError ?? pipes.Output).DangerousGetHandle(),
                    },
                    Attributes = list,
                };
                var env = Marshal.StringToHGlobalUni(environment);
                try
                {
                    const uint extendedStartup = 0x00080000, unicodeEnvironment = 0x400, noWindow = 0x08000000;
                    if (!CreateProcessW(executable, arguments, IntPtr.Zero, IntPtr.Zero, true,
                        extendedStartup | unicodeEnvironment | noWindow, env, directory, ref startup, out var info))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    using var thread = new SafeFileHandle(info.Thread, ownsHandle: true);
                    return new SafeFileHandle(info.Process, ownsHandle: true);
                }
                finally { Marshal.FreeHGlobal(env); }
            }
            finally { DeleteProcThreadAttributeList(list); }
        }
        finally { Marshal.FreeHGlobal(list); }
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetHandleInformation(SafeFileHandle handle, uint mask, uint flags);
}
