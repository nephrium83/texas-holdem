"""Portable Windows preview: one launcher owns the sidecar and Godot lifetime.

This is a host adapter. Rules and game sessions remain in the existing core.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from holdem.sidecar_launcher import run as run_sidecar

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimePaths:
    godot: Path
    pck: Path
    libsodium: Path


def resolve_paths(args) -> RuntimePaths:
    if getattr(sys, "frozen", False):
        root = Path(sys._MEIPASS)
        paths = RuntimePaths(root / "assets/PokerRuntime.exe",
                             root / "assets/poker.pck",
                             root / "native/libsodium.dll")
    else:
        if not all((args.godot, args.pck, args.libsodium)):
            raise RuntimeError("Source launch requires --godot, --pck and --libsodium.")
        paths = RuntimePaths(*(Path(p).resolve() for p in
                               (args.godot, args.pck, args.libsodium)))
    for path in (paths.godot, paths.pck, paths.libsodium):
        if not path.is_file():
            raise FileNotFoundError(f"A required game file is missing: {path}")
    return paths


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Poker local preview")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-visible", action="store_true",
                        help="Run the play test in a rendered game window")
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--godot", type=Path)
    parser.add_argument("--pck", type=Path)
    parser.add_argument("--libsodium", type=Path)
    args = parser.parse_args(argv)
    args.self_test = args.self_test or args.self_test_visible
    if args.log_dir is None:
        app_data = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        args.log_dir = app_data / "PokerPreview/logs" / f"{stamp}-{os.getpid()}"
    args.log_dir = args.log_dir.resolve()
    return args


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class _WindowsGame:
    """Create a child atomically inside its private Windows 10+ job.

    The job handle is not inherited. Windows closes it if this launcher
    crashes, terminating this game's process tree. No existing process is
    attached to, stopped by, or searched for by this launcher.
    """

    def __init__(self, command: list[str], log_path: Path):
        from ctypes import wintypes as w
        import msvcrt

        class StartupInfo(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR),
                        ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                        ("dwX", w.DWORD), ("dwY", w.DWORD),
                        ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                        ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD),
                        ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD),
                        ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                        ("lpReserved2", ctypes.POINTER(w.BYTE)),
                        ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                        ("hStdError", w.HANDLE)]

        class ProcessInfo(ctypes.Structure):
            _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                        ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]

        class StartupInfoEx(ctypes.Structure):
            _fields_ = [("StartupInfo", StartupInfo),
                        ("lpAttributeList", ctypes.c_void_p)]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", w.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits),
                        ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "InitializeProcThreadAttributeList": ([ctypes.c_void_p, w.DWORD, w.DWORD,
                                                    ctypes.POINTER(ctypes.c_size_t)], w.BOOL),
            "UpdateProcThreadAttribute": ([ctypes.c_void_p, w.DWORD, ctypes.c_size_t,
                                            ctypes.c_void_p, ctypes.c_size_t,
                                            ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
            "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
            "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, ctypes.c_void_p,
                                ctypes.c_void_p, w.BOOL, w.DWORD, ctypes.c_void_p,
                                w.LPCWSTR, ctypes.POINTER(StartupInfo),
                                ctypes.POINTER(ProcessInfo)], w.BOOL),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes = arguments
            function.restype = result
        self._kernel = kernel
        self._process = None
        self._code = None
        self._job = kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ProcessInfo()
        attributes = None
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(self._job, 9,
                                                  ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            size = ctypes.c_size_t()
            kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            storage = ctypes.create_string_buffer(size.value)
            if not kernel.InitializeProcThreadAttributeList(storage, 1, 0, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            attributes = storage
            jobs = (w.HANDLE * 1)(self._job)
            # PROC_THREAD_ATTRIBUTE_JOB_LIST assigns during CreateProcess,
            # leaving no suspended orphan if the launcher dies at startup.
            if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x2000D,
                                                      jobs, ctypes.sizeof(jobs), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            with open(log_path, "wb") as output, open(os.devnull, "rb") as input_file:
                extended = StartupInfoEx()
                extended.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
                startup = extended.StartupInfo
                startup.cb = ctypes.sizeof(extended)
                startup.dwFlags = 0x100  # STARTF_USESTDHANDLES
                startup.hStdInput = msvcrt.get_osfhandle(input_file.fileno())
                startup.hStdOutput = startup.hStdError = msvcrt.get_osfhandle(output.fileno())
                os.set_handle_inheritable(startup.hStdInput, True)
                os.set_handle_inheritable(startup.hStdOutput, True)
                try:
                    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
                    if not kernel.CreateProcessW(command[0], command_line, None, None,
                                                  True, 0x4 | 0x08000000 | 0x80000, None,
                                                  str(Path(command[0]).parent),
                                                  ctypes.cast(ctypes.byref(extended),
                                                              ctypes.POINTER(StartupInfo)),
                                                  ctypes.byref(info)):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    os.set_handle_inheritable(startup.hStdInput, False)
                    os.set_handle_inheritable(startup.hStdOutput, False)
            self._process = info.hProcess
            self.pid = info.dwProcessId
            if kernel.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            if info.hProcess:
                # Only the newly created child can be present here.
                kernel.TerminateProcess(info.hProcess, 1)
                kernel.WaitForSingleObject(info.hProcess, 5000)
                kernel.CloseHandle(info.hProcess)
                self._process = None
            kernel.CloseHandle(self._job)
            self._job = None
            raise
        finally:
            if attributes is not None:
                kernel.DeleteProcThreadAttributeList(attributes)
            if info.hThread:
                kernel.CloseHandle(info.hThread)

    def poll(self) -> int | None:
        if self._process is None:
            return self._code
        result = self._kernel.WaitForSingleObject(self._process, 0)
        if result == 0x102:  # WAIT_TIMEOUT
            return None
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        from ctypes import wintypes
        code = wintypes.DWORD()
        if not self._kernel.GetExitCodeProcess(self._process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._code = code.value
        return self._code

    def close(self) -> None:
        if self._job:
            self._kernel.CloseHandle(self._job)
            self._job = None
        if self._process:
            self._kernel.WaitForSingleObject(self._process, 5000)
            self.poll()
            self._kernel.CloseHandle(self._process)
            self._process = None


def _spawn_game(command: list[str], log_path: Path):
    if sys.platform != "win32":
        raise RuntimeError("The portable preview currently supports Windows only.")
    return _WindowsGame(command, log_path)


def _self_test_summary(log_path: Path) -> dict:
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    summaries = [line.split("E2E_SUMMARY ", 1)[1] for line in lines
                 if line.startswith("E2E_SUMMARY ")]
    if not summaries:
        raise RuntimeError("The game did not report its play test results.")
    summary = json.loads(summaries[-1])
    passed = (
        summary.get("error") == "" and summary.get("hands_settled", 0) >= 10
        and summary.get("hands_voided") == 0
        and summary.get("stack_carry_checks", 0) >= 9
        and summary.get("stack_carry_failures") == []
        and summary.get("refused") == []
        and summary.get("face_up_checks", 0) > 0
        and summary.get("face_up_failures") == []
        and all(summary.get("actions", {}).get(action, 0) > 0
                for action in ("check_call", "fold", "raise", "all_in"))
    )
    if not passed:
        raise RuntimeError(f"The game play test failed: {summary}")
    return summary


async def _run(args, paths: RuntimePaths) -> int:
    args.log_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HOLDEM_LIBSODIUM"] = str(paths.libsodium)
    ready = asyncio.get_running_loop().create_future()

    def announce(port):
        if not ready.done():
            ready.set_result(port)

    sidecar = asyncio.create_task(run_sidecar(
        seats=2, sb=25, bb=50, stack=100000 if args.self_test else 1000,
        structure="No-Limit", port=0, seed=7 if args.self_test else None,
        nickname="Player", ready=announce, desktop_lobby=not args.self_test))
    game = None
    session = {"game_pid": None, "sidecar_port": None, "game_exit_code": None}
    try:
        done, _ = await asyncio.wait((ready, sidecar), timeout=15,
                                     return_when=asyncio.FIRST_COMPLETED)
        if sidecar in done:
            await sidecar
            raise RuntimeError("The local game service stopped during startup.")
        if ready not in done:
            raise TimeoutError("The local game service did not start.")
        port = ready.result()
        session["sidecar_port"] = port
        command = [str(paths.godot), "--main-pack", str(paths.pck),
                   "--rendering-method", "gl_compatibility"]
        if args.self_test:
            if not args.self_test_visible:
                command.append("--headless")
            command.extend(["-s", "res://sidecar/main_e2e.gd"])
        command.extend(["--", f"--sidecar-port={port}"])
        if args.self_test:
            command.append("--hands=10")
        if args.self_test_visible:
            command.append(f"--screenshot-path={args.log_dir / 'table.png'}")
        game = _spawn_game(command, args.log_dir / "game.log")
        session["game_pid"] = game.pid
        _write_json(args.log_dir / "session.json", session)
        _log.info("Game started: pid=%s sidecar_port=%s", game.pid, port)
        deadline = time.monotonic() + 60 if args.self_test else None
        while game.poll() is None:
            if sidecar.done():
                await sidecar
                raise RuntimeError("The local game service stopped unexpectedly.")
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("The game play test exceeded 60 seconds.")
            await asyncio.sleep(0.05)
        session["game_exit_code"] = game.poll()
        if session["game_exit_code"] != 0:
            raise RuntimeError(f"The game closed with error {session['game_exit_code']}.")
        if args.self_test:
            summary = _self_test_summary(args.log_dir / "game.log")
            _write_json(args.log_dir / "self-test.json",
                        {"ok": True, "error": None, "summary": summary})
        return 0
    finally:
        try:
            if game is not None:
                game.close()
        finally:
            if not ready.done():
                ready.cancel()
            sidecar.cancel()
            await asyncio.gather(sidecar, return_exceptions=True)
            _write_json(args.log_dir / "session.json", session)
            _log.info("Game and local service stopped")


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        args.log_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=args.log_dir / "launcher.log", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            force=True)
        return asyncio.run(_run(args, resolve_paths(args)))
    except Exception as error:
        _log.exception("Could not run the game")
        if args.self_test:
            if args.log_dir.is_dir():
                _write_json(args.log_dir / "self-test.json",
                            {"ok": False, "error": str(error), "summary": None})
        elif sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                None, f"Poker could not start or closed unexpectedly.\n\n{error}\n\n"
                f"Details: {args.log_dir}", "Poker", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
