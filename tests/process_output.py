"""Deadline-aware output and cleanup for children spawned by these tests only."""
from collections import deque
import subprocess
import threading
import time


class ProcessOutput:
    def __init__(self, proc):
        self.proc = proc
        self.lines = deque(maxlen=256)
        self.errors = deque(maxlen=16)  # last 64 KiB; stderr is always drained
        self.partial = ""
        self.eof = False
        self.condition = threading.Condition()
        self.threads = []
        self._start(self._stdout)
        if proc.stderr is not None:
            self._start(self._stderr)

    def _start(self, target):
        thread = threading.Thread(target=target, daemon=True)
        self.threads.append(thread)
        thread.start()

    def _stdout(self):
        try:
            while True:
                char = self.proc.stdout.read(1)
                if not char:
                    break
                self.partial += char
                if char == "\n" or len(self.partial) >= 8192:
                    with self.condition:
                        self.lines.append(self.partial)
                        self.partial = ""
                        self.condition.notify_all()
        finally:
            with self.condition:
                if self.partial:
                    self.lines.append(self.partial)
                    self.partial = ""
                self.eof = True
                self.condition.notify_all()

    def _stderr(self):
        while True:
            chunk = self.proc.stderr.read(4096)
            if not chunk:
                return
            self.errors.append(chunk)

    def readline(self, timeout):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not self.lines:
                if self.eof:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("child stdout deadline expired")
                self.condition.wait(remaining)
            return self.lines.popleft()

    def stderr_tail(self):
        return "".join(self.errors)


def capture(proc):
    output = getattr(proc, "_test_output", None)
    if output is None:
        output = proc._test_output = ProcessOutput(proc)
    return output


def read_port(proc, timeout):
    output = capture(proc)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            line = output.readline(remaining)
        except TimeoutError:
            break
        if line is None:
            break
        if line.strip().startswith("SIDECAR_PORT:"):
            port = int(line.strip().split(":", 1)[1])
            if not 1 <= port <= 65535:
                raise ValueError("sidecar port outside TCP range")
            return port
    raise TimeoutError(f"sidecar did not print SIDECAR_PORT within {timeout}s; "
                       f"partial stdout: {output.partial!r}; stderr: {output.stderr_tail()}")


def collect_stdout(proc, sentinel, timeout):
    output = capture(proc)
    deadline = time.monotonic() + timeout
    lines = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return lines
        try:
            line = output.readline(remaining)
        except TimeoutError:
            return lines
        if line is None:
            return lines
        line = line.strip()
        if line:
            lines.append(line)
        if line == sentinel:
            return lines


def stop_process(proc, *, timeout=5, force=False):
    """Reap a test-owned Popen; never search for or stop any other process."""
    if proc is None:
        return
    if proc.poll() is None:
        if force:
            proc.kill()
        else:
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)
    else:
        proc.wait(timeout=timeout)
    output = getattr(proc, "_test_output", None)
    if output is not None:
        for thread in output.threads:
            thread.join(timeout)
        if any(thread.is_alive() for thread in output.threads):
            raise RuntimeError("test child exited but its output pipe remains open")
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
