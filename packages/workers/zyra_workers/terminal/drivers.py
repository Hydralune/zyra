from __future__ import annotations

import os
import shlex
import signal
import struct
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from .models import TerminalError


@dataclass(frozen=True, slots=True)
class PtySpawnOptions:
    command: str
    cwd: Path
    shell: str
    rows: int
    cols: int
    environment: Mapping[str, str]


class PtyProcess(ABC):
    @property
    @abstractmethod
    def pid(self) -> int: ...

    @abstractmethod
    def read(self, maximum: int = 65_536) -> bytes: ...

    @abstractmethod
    def write(self, data: bytes) -> int: ...

    @abstractmethod
    def resize(self, rows: int, cols: int) -> None: ...

    @abstractmethod
    def poll(self) -> int | None: ...

    @abstractmethod
    def wait(self, timeout: float | None = None) -> int: ...

    @abstractmethod
    def terminate_tree(self, grace_seconds: float = 1.0) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


def command_argv(options: PtySpawnOptions) -> list[str]:
    shell = options.shell.strip()
    if not shell:
        if os.name == "nt":
            shell = os.environ.get("COMSPEC", "cmd.exe")
        else:
            shell = os.environ.get("SHELL", "/bin/sh")
    lower = Path(shell).name.casefold()
    if lower in {"cmd", "cmd.exe"}:
        return [shell, "/d", "/s", "/c", options.command]
    if lower in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return [
            shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            options.command,
        ]
    return [shell, "-lc", options.command]


def windows_command_line(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv)


def posix_command_line(argv: list[str]) -> str:
    return shlex.join(argv)


class PosixPtyProcess(PtyProcess):
    def __init__(self, options: PtySpawnOptions) -> None:
        import fcntl
        import pty
        import termios

        master, slave = pty.openpty()
        self._master = master
        self._closed = False
        self._write_lock = threading.Lock()
        try:
            fcntl.ioctl(
                master,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", options.rows, options.cols, 0, 0),
            )
            environment = dict(options.environment)
            environment.setdefault("TERM", "xterm-256color")
            environment.setdefault("COLORTERM", "truecolor")
            self._process = subprocess.Popen(
                command_argv(options),
                cwd=str(options.cwd),
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        finally:
            try:
                os.close(slave)
            except OSError:
                pass

    @property
    def pid(self) -> int:
        return int(self._process.pid)

    def read(self, maximum: int = 65_536) -> bytes:
        if self._closed:
            return b""
        try:
            return os.read(self._master, maximum)
        except OSError as error:
            if self._process.poll() is not None:
                return b""
            raise TerminalError(
                "terminal_pty_read_failed",
                f"PTY read failed: {error}",
                retryable=True,
            ) from error

    def write(self, data: bytes) -> int:
        if self._closed:
            raise TerminalError("terminal_pty_closed", "PTY is closed.", status=410)
        with self._write_lock:
            try:
                return os.write(self._master, data)
            except OSError as error:
                raise TerminalError(
                    "terminal_pty_write_failed",
                    f"PTY write failed: {error}",
                    status=410,
                ) from error

    def resize(self, rows: int, cols: int) -> None:
        if self._closed:
            raise TerminalError("terminal_pty_closed", "PTY is closed.", status=410)
        import fcntl
        import termios

        try:
            fcntl.ioctl(
                self._master,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError as error:
            raise TerminalError(
                "terminal_pty_resize_failed",
                f"PTY resize failed: {error}",
                status=410,
            ) from error

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        try:
            return int(self._process.wait(timeout=timeout))
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("PTY process did not exit before the deadline.") from error

    def terminate_tree(self, grace_seconds: float = 1.0) -> None:
        if self._process.poll() is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self._process.wait(timeout=max(0.0, grace_seconds))
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            self._process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._master)
        except OSError:
            pass


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    HPCON = wintypes.HANDLE
    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    STILL_ACTIVE = 259
    INFINITE = 0xFFFFFFFF
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    ERROR_BROKEN_PIPE = 109
    ERROR_NO_DATA = 232
    HANDLE_FLAG_INHERIT = 0x00000001
    STD_INPUT_HANDLE = -10
    STD_OUTPUT_HANDLE = -11
    STD_ERROR_HANDLE = -12
    JobObjectExtendedLimitInformation = 9

    class COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", STARTUPINFOW),
            ("lpAttributeList", wintypes.LPVOID),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.CreatePipe.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.CreatePseudoConsole.argtypes = [
        COORD,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(HPCON),
    ]
    _kernel32.CreatePseudoConsole.restype = ctypes.c_long
    _kernel32.ResizePseudoConsole.argtypes = [HPCON, COORD]
    _kernel32.ResizePseudoConsole.restype = ctypes.c_long
    _kernel32.ClosePseudoConsole.argtypes = [HPCON]
    _kernel32.ClosePseudoConsole.restype = None
    _release_pseudo_console = getattr(
        _kernel32,
        "ReleasePseudoConsole",
        None,
    )
    if _release_pseudo_console is not None:
        _release_pseudo_console.argtypes = [HPCON]
        _release_pseudo_console.restype = ctypes.c_long
    _kernel32.InitializeProcThreadAttributeList.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    _kernel32.UpdateProcThreadAttribute.argtypes = [
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_size_t,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    _kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
    _kernel32.DeleteProcThreadAttributeList.restype = None
    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    _kernel32.CreateProcessW.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    _kernel32.GetStdHandle.restype = wintypes.HANDLE
    _kernel32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    _kernel32.SetStdHandle.restype = wintypes.BOOL
    _kernel32.GetHandleInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetHandleInformation.restype = wintypes.BOOL
    _kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _kernel32.SetHandleInformation.restype = wintypes.BOOL
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _process_creation_lock = threading.Lock()


    def _win_error(operation: str) -> OSError:
        return ctypes.WinError(ctypes.get_last_error(), operation)


    def _check_bool(value: int, operation: str) -> None:
        if not value:
            raise _win_error(operation)


    def _check_hresult(value: int, operation: str) -> None:
        if value < 0:
            unsigned = value & 0xFFFFFFFF
            raise OSError(unsigned, f"{operation} failed with HRESULT 0x{unsigned:08x}")


    def _environment_block(values: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
        entries = [
            f"{key}={value}"
            for key, value in sorted(values.items(), key=lambda item: item[0].casefold())
        ]
        return ctypes.create_unicode_buffer("\0".join(entries) + "\0\0")


    def _suppress_inheritable_standard_handles() -> list[tuple[int, int, int]]:
        """Keep redirected host stdio from bypassing the pseudoconsole.

        Windows may propagate process standard handles even when
        ``bInheritHandles`` is false. A supervised API process commonly has
        redirected stdout/stderr pipes; leaving them in the process parameters
        lets a console client write around ConPTY. Both the standard-handle
        slots and their inheritance flags are process-global, so callers hold
        ``_process_creation_lock`` across suppression, ``CreateProcessW`` and
        restoration.
        """

        changed: list[tuple[int, int, int]] = []
        try:
            for identifier in (
                STD_INPUT_HANDLE,
                STD_OUTPUT_HANDLE,
                STD_ERROR_HANDLE,
            ):
                handle = _kernel32.GetStdHandle(identifier & 0xFFFFFFFF)
                if not handle or handle == wintypes.HANDLE(-1).value:
                    continue
                flags = wintypes.DWORD()
                has_flags = _kernel32.GetHandleInformation(
                    handle,
                    ctypes.byref(flags),
                )
                changed.append((identifier, int(handle), int(flags.value)))
                if has_flags and flags.value & HANDLE_FLAG_INHERIT:
                    _check_bool(
                        _kernel32.SetHandleInformation(
                            handle,
                            HANDLE_FLAG_INHERIT,
                            0,
                        ),
                        "SetHandleInformation(clear standard handle inheritance)",
                    )
                _check_bool(
                    _kernel32.SetStdHandle(
                        identifier & 0xFFFFFFFF,
                        None,
                    ),
                    "SetStdHandle(clear redirected standard handle)",
                )
        except BaseException:
            _restore_standard_handle_flags(changed)
            raise
        return changed


    def _restore_standard_handle_flags(
        changed: list[tuple[int, int, int]],
    ) -> None:
        for identifier, value, flags in reversed(changed):
            handle = wintypes.HANDLE(value)
            _kernel32.SetStdHandle(
                identifier & 0xFFFFFFFF,
                handle,
            )
            if flags & HANDLE_FLAG_INHERIT:
                _kernel32.SetHandleInformation(
                    handle,
                    HANDLE_FLAG_INHERIT,
                    HANDLE_FLAG_INHERIT,
                )


    class WindowsConPtyProcess(PtyProcess):
        def __init__(self, options: PtySpawnOptions) -> None:
            self._closed = False
            self._write_lock = threading.Lock()
            self._console_lock = threading.RLock()
            self._console_released = False
            self._hpc = HPCON()
            self._process_handle = wintypes.HANDLE()
            self._job = wintypes.HANDLE()
            self._input_handle = wintypes.HANDLE()
            self._output_handle = wintypes.HANDLE()
            input_read = wintypes.HANDLE()
            input_write = wintypes.HANDLE()
            output_read = wintypes.HANDLE()
            output_write = wintypes.HANDLE()
            attribute_buffer: ctypes.Array[ctypes.c_char] | None = None
            attribute_list = wintypes.LPVOID()
            process = PROCESS_INFORMATION()
            try:
                _check_bool(
                    _kernel32.CreatePipe(
                        ctypes.byref(input_read),
                        ctypes.byref(input_write),
                        None,
                        0,
                    ),
                    "CreatePipe(input)",
                )
                _check_bool(
                    _kernel32.CreatePipe(
                        ctypes.byref(output_read),
                        ctypes.byref(output_write),
                        None,
                        0,
                    ),
                    "CreatePipe(output)",
                )
                _check_hresult(
                    _kernel32.CreatePseudoConsole(
                        COORD(options.cols, options.rows),
                        input_read,
                        output_write,
                        0,
                        ctypes.byref(self._hpc),
                    ),
                    "CreatePseudoConsole",
                )
                attribute_size = ctypes.c_size_t()
                _kernel32.InitializeProcThreadAttributeList(
                    None,
                    1,
                    0,
                    ctypes.byref(attribute_size),
                )
                attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
                attribute_list = ctypes.cast(attribute_buffer, wintypes.LPVOID)
                _check_bool(
                    _kernel32.InitializeProcThreadAttributeList(
                        attribute_list,
                        1,
                        0,
                        ctypes.byref(attribute_size),
                    ),
                    "InitializeProcThreadAttributeList",
                )
                _check_bool(
                    _kernel32.UpdateProcThreadAttribute(
                        attribute_list,
                        0,
                        PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                        self._hpc,
                        ctypes.sizeof(self._hpc),
                        None,
                        None,
                    ),
                    "UpdateProcThreadAttribute(ConPTY)",
                )
                startup = STARTUPINFOEXW()
                startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
                startup.lpAttributeList = attribute_list
                command_line = ctypes.create_unicode_buffer(
                    windows_command_line(command_argv(options))
                )
                environment = dict(options.environment)
                environment.setdefault("TERM", "xterm-256color")
                environment.setdefault("COLORTERM", "truecolor")
                environment_block = _environment_block(environment)
                with _process_creation_lock:
                    changed_standard_handles = (
                        _suppress_inheritable_standard_handles()
                    )
                    try:
                        _check_bool(
                            _kernel32.CreateProcessW(
                                None,
                                command_line,
                                None,
                                None,
                                False,
                                EXTENDED_STARTUPINFO_PRESENT
                                | CREATE_UNICODE_ENVIRONMENT,
                                ctypes.cast(environment_block, wintypes.LPVOID),
                                str(options.cwd),
                                ctypes.cast(
                                    ctypes.byref(startup),
                                    ctypes.POINTER(STARTUPINFOW),
                                ),
                                ctypes.byref(process),
                            ),
                            "CreateProcessW(ConPTY)",
                        )
                    finally:
                        _restore_standard_handle_flags(changed_standard_handles)
                self._process_handle = process.hProcess
                self._pid = int(process.dwProcessId)
                _kernel32.CloseHandle(process.hThread)
                process.hThread = wintypes.HANDLE()
                # The pseudoconsole-side pipe handles must remain open until
                # the client process has attached to the HPCON attribute.
                # Closing them immediately after CreatePseudoConsole races
                # ConHost startup and yields an empty/broken output channel.
                _kernel32.CloseHandle(input_read)
                input_read = wintypes.HANDLE()
                _kernel32.CloseHandle(output_write)
                output_write = wintypes.HANDLE()
                self._job = _kernel32.CreateJobObjectW(None, None)
                if self._job:
                    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                    limits.BasicLimitInformation.LimitFlags = (
                        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                    )
                    if _kernel32.SetInformationJobObject(
                        self._job,
                        JobObjectExtendedLimitInformation,
                        ctypes.byref(limits),
                        ctypes.sizeof(limits),
                    ) and _kernel32.AssignProcessToJobObject(
                            self._job,
                            self._process_handle,
                    ):
                        pass
                    else:
                        _kernel32.CloseHandle(self._job)
                        self._job = wintypes.HANDLE()
                self._input_handle = input_write
                input_write = wintypes.HANDLE()
                self._output_handle = output_read
                output_read = wintypes.HANDLE()
                self._exit_watcher = threading.Thread(
                    target=self._close_console_after_process_exit,
                    name=f"zyra-conpty-exit-{self._pid}",
                    daemon=True,
                )
                self._exit_watcher.start()
            except BaseException:
                if process.hThread:
                    _kernel32.CloseHandle(process.hThread)
                if process.hProcess:
                    _kernel32.TerminateProcess(process.hProcess, 1)
                    _kernel32.CloseHandle(process.hProcess)
                self._close_handle(input_read)
                self._close_handle(input_write)
                self._close_handle(output_read)
                self._close_handle(output_write)
                if self._hpc:
                    _kernel32.ClosePseudoConsole(self._hpc)
                    self._hpc = HPCON()
                raise
            finally:
                if attribute_list:
                    _kernel32.DeleteProcThreadAttributeList(attribute_list)

        @property
        def pid(self) -> int:
            return self._pid

        def read(self, maximum: int = 65_536) -> bytes:
            if self._closed or not self._output_handle:
                return b""
            buffer = ctypes.create_string_buffer(max(1, int(maximum)))
            read = wintypes.DWORD()
            if not _kernel32.ReadFile(
                self._output_handle,
                buffer,
                ctypes.sizeof(buffer),
                ctypes.byref(read),
                None,
            ):
                error_code = ctypes.get_last_error()
                if error_code in {ERROR_BROKEN_PIPE, ERROR_NO_DATA}:
                    if self._console_released:
                        self._finalize_released_console()
                    return b""
                if self.poll() is not None:
                    return b""
                raise TerminalError(
                    "terminal_pty_read_failed",
                    f"ConPTY ReadFile failed with Windows error {error_code}.",
                    retryable=True,
                )
            if read.value == 0:
                if self._console_released:
                    self._finalize_released_console()
                return b""
            return bytes(buffer.raw[: read.value])

        def write(self, data: bytes) -> int:
            if self._closed or not self._input_handle:
                raise TerminalError("terminal_pty_closed", "ConPTY is closed.", status=410)
            with self._write_lock:
                material = bytes(data)
                written = wintypes.DWORD()
                buffer = ctypes.create_string_buffer(material)
                if not _kernel32.WriteFile(
                    self._input_handle,
                    buffer,
                    len(material),
                    ctypes.byref(written),
                    None,
                ):
                    error_code = ctypes.get_last_error()
                    raise TerminalError(
                        "terminal_pty_write_failed",
                        f"ConPTY WriteFile failed with Windows error {error_code}.",
                        status=410,
                    )
                return int(written.value)

        def resize(self, rows: int, cols: int) -> None:
            with self._console_lock:
                if self._closed or not self._hpc:
                    raise TerminalError("terminal_pty_closed", "ConPTY is closed.", status=410)
                try:
                    _check_hresult(
                        _kernel32.ResizePseudoConsole(
                            self._hpc,
                            COORD(cols, rows),
                        ),
                        "ResizePseudoConsole",
                    )
                except OSError as error:
                    raise TerminalError(
                        "terminal_pty_resize_failed",
                        str(error),
                        status=410,
                    ) from error

        def poll(self) -> int | None:
            if not self._process_handle:
                return 0
            code = wintypes.DWORD()
            _check_bool(
                _kernel32.GetExitCodeProcess(
                    self._process_handle,
                    ctypes.byref(code),
                ),
                "GetExitCodeProcess",
            )
            return None if code.value == STILL_ACTIVE else int(code.value)

        def wait(self, timeout: float | None = None) -> int:
            milliseconds = INFINITE if timeout is None else max(0, int(timeout * 1000))
            result = _kernel32.WaitForSingleObject(self._process_handle, milliseconds)
            if result == WAIT_TIMEOUT:
                raise TimeoutError("ConPTY process did not exit before the deadline.")
            if result != WAIT_OBJECT_0:
                raise _win_error("WaitForSingleObject")
            code = self.poll()
            return 0 if code is None else code

        def terminate_tree(self, grace_seconds: float = 1.0) -> None:
            if self.poll() is not None:
                return
            if self._input_handle:
                try:
                    self.write(b"\x03")
                except TerminalError:
                    pass
            try:
                self.wait(timeout=max(0.0, grace_seconds))
                return
            except TimeoutError:
                pass
            if self._job:
                _kernel32.CloseHandle(self._job)
                self._job = wintypes.HANDLE()
            else:
                _kernel32.TerminateProcess(self._process_handle, 1)
            try:
                self.wait(timeout=2.0)
            except TimeoutError:
                _kernel32.TerminateProcess(self._process_handle, 1)

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            if self._input_handle:
                _kernel32.CloseHandle(self._input_handle)
                self._input_handle = wintypes.HANDLE()
            if self._output_handle:
                _kernel32.CloseHandle(self._output_handle)
                self._output_handle = wintypes.HANDLE()
            with self._console_lock:
                hpc = self._hpc
                self._hpc = HPCON()
            if hpc:
                done = threading.Event()

                def close_console() -> None:
                    _kernel32.ClosePseudoConsole(hpc)
                    done.set()

                threading.Thread(
                    target=close_console,
                    name=f"zyra-conpty-close-{self._pid}",
                    daemon=True,
                ).start()
                done.wait(timeout=2.0)
            if self._job:
                _kernel32.CloseHandle(self._job)
                self._job = wintypes.HANDLE()
            if self._process_handle:
                _kernel32.CloseHandle(self._process_handle)
                self._process_handle = wintypes.HANDLE()

        def _close_console_after_process_exit(self) -> None:
            process = self._process_handle
            if not process:
                return
            result = _kernel32.WaitForSingleObject(process, INFINITE)
            if result != WAIT_OBJECT_0:
                return
            # ConHost may still be serializing the final screen update after
            # the root process handle is signalled. Keep the output channel
            # live briefly so the dedicated reader can drain that frame.
            time.sleep(0.1)
            with self._console_lock:
                hpc = self._hpc
                if not hpc:
                    return
                if _release_pseudo_console is not None:
                    _check_hresult(
                        _release_pseudo_console(hpc),
                        "ReleasePseudoConsole",
                    )
                    self._console_released = True
                    return
                self._hpc = HPCON()
            _kernel32.ClosePseudoConsole(hpc)

        def _finalize_released_console(self) -> None:
            with self._console_lock:
                hpc = self._hpc
                self._hpc = HPCON()
                self._console_released = False
            if hpc:
                _kernel32.ClosePseudoConsole(hpc)

        @staticmethod
        def _close_handle(handle: wintypes.HANDLE) -> None:
            if handle:
                _kernel32.CloseHandle(handle)


def spawn_pty(options: PtySpawnOptions) -> PtyProcess:
    if not options.cwd.is_absolute():
        raise TerminalError(
            "terminal_workspace_not_absolute",
            "Terminal workspace root must be absolute.",
            status=500,
        )
    if not options.cwd.is_dir():
        raise TerminalError(
            "terminal_workspace_missing",
            "Terminal workspace does not exist.",
            status=409,
        )
    if not options.command or "\x00" in options.command:
        raise TerminalError(
            "terminal_command_invalid",
            "Terminal command is empty or contains NUL.",
            status=400,
        )
    if not 2 <= options.rows <= 500 or not 2 <= options.cols <= 1_000:
        raise TerminalError(
            "terminal_size_invalid",
            "Terminal size is outside the supported range.",
            status=400,
        )
    try:
        if os.name == "nt":
            return WindowsConPtyProcess(options)
        return PosixPtyProcess(options)
    except TerminalError:
        raise
    except OSError as error:
        raise TerminalError(
            "terminal_pty_spawn_failed",
            f"Could not open the platform PTY: {error}",
            status=503,
            retryable=True,
        ) from error


def pty_capabilities() -> dict[str, object]:
    return {
        "platform": sys.platform,
        "driver": "windows-conpty" if os.name == "nt" else "posix-pty",
        "native_owner": "zyra_workers.terminal.drivers",
        "external_package": False,
        "source_process": False,
        "stdin": True,
        "resize": True,
        "process_tree_kill": True,
    }
