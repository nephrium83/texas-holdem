"""Optional audio remains bounded and publishes a complete sound table."""
import sys
import threading
from types import ModuleType

import pytest

from holdem import audio

RealThread = threading.Thread


@pytest.fixture
def backend(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    mixer = ModuleType("pygame.mixer")
    mixer.created = []
    mixer.block = False
    mixer.init_calls = 0
    mixer.played = threading.Event()
    def init(**kwargs):
        mixer.init_calls += 1
    class Sound:
        def __init__(self, **kwargs):
            self.volume = None
            self.plays = 0
            mixer.created.append(self)
            if mixer.block and len(mixer.created) == 1:
                entered.set()
                assert release.wait(timeout=3), "audio initialization was not released"
        def set_volume(self, volume):
            self.volume = volume
        def play(self):
            self.plays += 1
            mixer.played.set()
    mixer.init, mixer.Sound = init, Sound
    package = ModuleType("pygame")
    package.mixer = mixer
    monkeypatch.setitem(sys.modules, "pygame", package)
    monkeypatch.setitem(sys.modules, "pygame.mixer", mixer)
    monkeypatch.setattr(audio, "_mixer", None)
    monkeypatch.setattr(audio, "_sounds", {})
    monkeypatch.setattr(audio, "_enabled", True)
    monkeypatch.setattr(audio, "_volume", .7)
    monkeypatch.setattr(audio, "_synth", lambda *args: b"\0\0")
    yield mixer, entered, release
    release.set()
    worker = getattr(audio, "_worker", None)
    if isinstance(worker, RealThread) and worker.is_alive():
        audio._requests.put(None, timeout=3)
        worker.join(timeout=3)
        assert not worker.is_alive(), "test audio worker did not stop"
    if hasattr(audio, "_worker"):
        audio._worker = None


def test_initialization_does_not_publish_partial_audio_state(backend):
    mixer, entered, release = backend
    mixer.block = True
    thread = RealThread(target=audio._init)
    thread.start()
    try:
        assert entered.wait(timeout=2)
        assert audio._mixer is None and audio._sounds == {}
    finally:
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert len(audio._sounds) == 8 and mixer.init_calls == 1


def test_playback_burst_creates_one_bounded_worker(backend, monkeypatch):
    created = []
    class PendingThread:
        def __init__(self, *args, **kwargs):
            created.append(self)
        def start(self):
            pass
    monkeypatch.setattr(audio.threading, "Thread", PendingThread)
    for _ in range(1000):
        audio.play("deal")
    assert len(created) == 1
    assert audio._requests.qsize() <= audio._requests.maxsize <= 64
    # The fake thread deliberately consumes nothing.
    while not audio._requests.empty():
        audio._requests.get_nowait()
        audio._requests.task_done()


def test_concurrent_first_play_and_volume_changes_keep_one_complete_backend(backend):
    from concurrent.futures import ThreadPoolExecutor
    mixer, _, _ = backend
    def work(index):
        if index % 2:
            audio._play_bg("deal")
        else:
            audio.set_volume((index % 10) / 10)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, range(80)))
    assert mixer.init_calls == 1 and len(mixer.created) == 8
    assert all(sound.volume == audio._volume for sound in mixer.created)


def test_slow_backend_initialization_does_not_block_play_or_volume_commands(backend):
    mixer, entered, release = backend
    mixer.block = True
    audio.play("deal")
    assert entered.wait(timeout=2)
    def commands():
        audio.play("call")
        audio.set_volume(.2)
    caller = RealThread(target=commands)
    caller.start()
    try:
        caller.join(timeout=.3)
        assert not caller.is_alive(), "audio command waited for backend startup"
    finally:
        release.set()
        caller.join(timeout=3)
    assert mixer.played.wait(timeout=2)
    assert all(sound.volume == .2 for sound in mixer.created)
