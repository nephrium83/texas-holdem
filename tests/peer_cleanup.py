"""Bounded cleanup for explicitly test-owned peer children and their readers."""
import subprocess


def close_child(proc, *, quit=None, readers=(), timeout=5):
    if proc is None:
        return
    threads = list(readers) + list(getattr(proc, "_test_reader_threads", ()))
    streams = [stream for stream in (proc.stdin, proc.stdout, proc.stderr) if stream is not None]
    threads += [stream._test_reader_thread for stream in streams
                if hasattr(stream, "_test_reader_thread")]
    try:
        if proc.poll() is None and quit is not None:
            try:
                quit()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=timeout)
        else:
            proc.wait(timeout=timeout)  # reap even a previously exited child
    finally:
        for thread in threads:
            thread.join(timeout)
        alive = [thread.name for thread in threads if thread.is_alive()]
        if not alive:
            for stream in streams:
                try:
                    stream.close()
                except OSError:
                    pass  # a broken pipe can be reported when closing stdin
        if alive:
            raise RuntimeError(f"peer readers survived child cleanup: {alive}")


def close_all(children):
    """Attempt every cleanup, then report failures; never abandon later peers."""
    errors = []
    for label, cleanup in children:
        try:
            cleanup()
        except Exception as exc:
            errors.append(f"{label}: {exc!r}")
    if errors:
        raise RuntimeError("; ".join(errors))
