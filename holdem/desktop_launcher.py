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
    parser = argparse.ArgumentParser(description="Poker preview")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-visible", action="store_true",
                        help="Run the play test in a rendered game window")
    parser.add_argument("--presentation-self-test", action="store_true",
                        help="Check the normal desktop menu, deal animation and paced bots")
    parser.add_argument("--presentation-self-test-visible", action="store_true",
                        help="Render and capture the desktop deal presentation check")
    parser.add_argument("--lan-self-test-config", type=Path,
                        help="Run a paired LAN check using a local JSON configuration file")
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--godot", type=Path)
    parser.add_argument("--pck", type=Path)
    parser.add_argument("--libsodium", type=Path)
    args = parser.parse_args(argv)
    args.self_test = args.self_test or args.self_test_visible
    args.presentation_self_test = args.presentation_self_test or args.presentation_self_test_visible
    if sum((args.self_test, args.presentation_self_test,
            args.lan_self_test_config is not None)) > 1:
        parser.error("Choose one of the local, presentation, or paired LAN checks")
    if args.lan_self_test_config is not None:
        args.lan_self_test_config = args.lan_self_test_config.resolve()
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


def _lan_check_timeout(config: dict) -> int:
    """A bounded diagnostic budget, long enough for real 30-second waits."""
    seconds = config.get("deadline_seconds", 300)
    if type(seconds) is not int or not 30 <= seconds <= 900:
        raise ValueError("LAN check deadline must be between 30 and 900 seconds")
    return seconds + 15  # Give the driver time to publish its own timeout.


def _validate_lan_summary(config: dict, summary: dict) -> None:
    """Accept only the diagnostic outcome that this particular peer exercised."""
    network = config.get("network", "lan")
    if network not in ("lan", "internet"):
        raise ValueError("Choose LAN or Internet for the connection check")
    expected = config.get("expected_result", "hands")
    minimum = (config.get("target_hands", 3) if expected == "hands"
               else config.get("minimum_hands", 0))
    complete = (summary.get("hands_settled", 0) >= minimum
                and summary.get("ok") is True and not summary.get("error"))
    if network == "internet":
        complete = (complete and summary.get("network") == "internet"
                    and summary.get("network_checks", 0) > 0)
    if expected == "left_table":
        complete = complete and summary.get("left_table") is True
    elif expected == "host_lost":
        complete = complete and summary.get("host_loss_seen") is True
    elif expected != "hands":
        complete = False
    if "expected_players" in config:
        complete = complete and len(summary.get("roster_names", [])) == config["expected_players"]
    for field, measured in (("stall_action_hand", "action_timeout_elapsed_ms"),
                            ("auto_next_wait_hand", "auto_next_elapsed_ms")):
        if config.get(field, -1) >= 0:
            complete = complete and summary.get(measured, -1) >= 29000
    if config.get("departed_name"):
        complete = complete and summary.get("departure_seen") is True
    if not complete:
        raise RuntimeError("The paired LAN check did not complete its required outcome")


def _validate_presentation_summary(summary: dict) -> None:
    """Require the real desktop flow and visible motion, not a bare deal."""
    counts = ("menu_fresh_checks", "opening_preaction_checks", "shuffle_frames",
              "deal_frames", "shuffle_motion_checks", "deal_motion_checks",
              "moving_back_checks", "masked_control_checks", "private_card_checks",
              "next_hand_presentations", "bot_reaction_checks")
    passed = (
        summary.get("ok") is True and summary.get("error") == ""
        and all(type(summary.get(key)) is int and summary[key] > 0 for key in counts)
        and summary.get("presentations_completed", 0) >= 2
        and summary.get("all_in_clicks") == 1
        and summary.get("all_in_applied") is True
        and summary.get("cancelled_during_deal") is True
        and summary.get("returned_to_menu") is True
        and summary.get("menu_quiet_ms", 0) >= 3200
        and summary.get("refused") == []
    )
    if not passed:
        raise RuntimeError("The desktop presentation check did not complete its required outcome")


async def _run(args, paths: RuntimePaths) -> int:
    args.log_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HOLDEM_LIBSODIUM"] = str(paths.libsodium)
    ready = asyncio.get_running_loop().create_future()

    def announce(port):
        if not ready.done():
            ready.set_result(port)

    sidecar = asyncio.create_task(run_sidecar(
        seats=2, sb=25, bb=50,
        stack=100000 if args.self_test or args.lan_self_test_config else 1000,
        structure="No-Limit", port=0,
        seed=7 if args.self_test or args.presentation_self_test else None,
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
        if args.lan_self_test_config is not None:
            command.extend(["--headless", "-s", "res://sidecar/lan_peer_e2e.gd"])
        elif args.presentation_self_test:
            if not args.presentation_self_test_visible:
                command.append("--headless")
            command.extend(["-s", "res://sidecar/presentation_e2e.gd"])
        elif args.self_test:
            if not args.self_test_visible:
                command.append("--headless")
            command.extend(["-s", "res://sidecar/main_e2e.gd"])
        command.extend(["--", f"--sidecar-port={port}"])
        if args.lan_self_test_config is not None:
            command.append(f"--lan-peer-config={args.lan_self_test_config}")
        if args.presentation_self_test:
            command.append(f"--presentation-report={args.log_dir / 'presentation.json'}")
            if args.presentation_self_test_visible:
                command.append(f"--presentation-capture-dir={args.log_dir / 'captures'}")
        if args.self_test:
            command.append("--hands=10")
        if args.self_test_visible:
            command.append(f"--screenshot-path={args.log_dir / 'table.png'}")
        game = _spawn_game(command, args.log_dir / "game.log")
        session["game_pid"] = game.pid
        _write_json(args.log_dir / "session.json", session)
        _log.info("Game started: pid=%s sidecar_port=%s", game.pid, port)
        lan_config = (json.loads(args.lan_self_test_config.read_text(encoding="utf-8"))
                      if args.lan_self_test_config is not None else None)
        check_timeout = (_lan_check_timeout(lan_config) if lan_config is not None
                         else 90 if args.presentation_self_test else 60)
        deadline = (time.monotonic() + check_timeout
                    if args.self_test or args.presentation_self_test or args.lan_self_test_config else None)
        while game.poll() is None:
            if sidecar.done():
                await sidecar
                raise RuntimeError("The local game service stopped unexpectedly.")
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"The game play test exceeded {check_timeout} seconds.")
            await asyncio.sleep(0.05)
        session["game_exit_code"] = game.poll()
        if session["game_exit_code"] != 0:
            raise RuntimeError(f"The game closed with error {session['game_exit_code']}.")
        if args.self_test:
            summary = _self_test_summary(args.log_dir / "game.log")
            _write_json(args.log_dir / "self-test.json",
                        {"ok": True, "error": None, "summary": summary})
        if args.presentation_self_test:
            summary = json.loads((args.log_dir / "presentation.json").read_text(encoding="utf-8"))
            _validate_presentation_summary(summary)
            _write_json(args.log_dir / "presentation-self-test.json",
                        {"ok": True, "error": None, "summary": summary})
        if args.lan_self_test_config is not None:
            config = lan_config
            summary = json.loads(Path(config["report_path"]).read_text(encoding="utf-8"))
            _validate_lan_summary(config, summary)
            _write_json(args.log_dir / "lan-self-test.json",
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
        elif args.presentation_self_test:
            if args.log_dir.is_dir():
                _write_json(args.log_dir / "presentation-self-test.json",
                            {"ok": False, "error": str(error), "summary": None})
        elif sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(
                None, f"Poker could not start or closed unexpectedly.\n\n{error}\n\n"
                f"Details: {args.log_dir}", "Poker", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
