"""Synthesized sound effects — no external audio files needed."""
import threading, math, struct, wave, io
import queue

_enabled = True
_volume = 0.7
_mixer = None
_sounds = {}
_state_lock = threading.RLock()
_init_lock = threading.Lock()
_requests = queue.Queue(maxsize=32)
_worker = None

def _synth(freq_start, freq_end, duration_ms, wave_type="sine", envelope="adsr"):
    """Synthesize a sound as raw PCM bytes."""
    sample_rate = 22050
    n = int(sample_rate * duration_ms / 1000)
    data = []
    for i in range(n):
        t = i / sample_rate
        frac = i / max(n - 1, 1)
        freq = freq_start + (freq_end - freq_start) * frac
        if wave_type == "sine":
            s = math.sin(2 * math.pi * freq * t)
        elif wave_type == "noise":
            import random
            s = random.uniform(-1, 1)
        else:
            s = math.sin(2 * math.pi * freq * t)
        # simple ADSR-ish envelope
        attack = 0.05; decay = 0.1; sustain_level = 0.7; release = 0.2
        if frac < attack:
            amp = frac / attack
        elif frac < attack + decay:
            amp = 1.0 - (1.0 - sustain_level) * (frac - attack) / decay
        elif frac < 1.0 - release:
            amp = sustain_level
        else:
            amp = sustain_level * (1.0 - (frac - (1.0 - release)) / release)
        data.append(int(s * amp * 32767))
    return struct.pack(f"<{n}h", *data)

def _make_wav(pcm, sample_rate=22050):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    buf.seek(0)
    return buf.read()

def _init():
    with _init_lock:
        _init_locked()


def _init_locked():
    global _mixer, _sounds
    with _state_lock:
        if _mixer is not None:
            return
        volume = _volume
    try:
        import pygame.mixer as mx
        mx.init(frequency=22050, size=-16, channels=1, buffer=256)
        specs = {
            "deal":   (_synth(800, 400, 100), 22050),
            "check":  (_synth(1000, 1000, 80), 22050),
            "call":   (_synth(600, 700, 120), 22050),
            "raise_sound": (_synth(400, 900, 200), 22050),
            "fold":   (_synth(600, 200, 100, "noise"), 22050),
            "allin":  (_synth(200, 1200, 300), 22050),
            "win":    (_synth(400, 1000, 400), 22050),
            "bad_beat": (_synth(800, 150, 350), 22050),
        }
        sounds = {}
        for name, (pcm, sr) in specs.items():
            wav_bytes = _make_wav(pcm, sr)
            sound = mx.Sound(buffer=wav_bytes)
            sound.set_volume(volume)
            sounds[name] = sound
        with _state_lock:
            # A volume change during slow optional-backend initialization
            # applies to the complete table before it becomes visible.
            if volume != _volume:
                for sound in sounds.values():
                    sound.set_volume(_volume)
            _sounds = sounds
            _mixer = mx
    except Exception:
        with _state_lock:
            _mixer = False  # mark as unavailable

def play(name: str):
    global _worker
    if not isinstance(name, str):
        return
    with _state_lock:
        if not _enabled:
            return
        if _worker is None:
            _worker = threading.Thread(target=_play_worker, args=(_requests,),
                                       daemon=True, name="poker-audio")
            try:
                _worker.start()
            except (RuntimeError, OSError):
                _worker = None
                return
        try:
            _requests.put_nowait(name)
        except queue.Full:
            pass  # Sound effects may be dropped; gameplay must never wait.


def _play_worker(requests):
    while True:
        name = requests.get()
        try:
            if name is None:
                return
            _play_bg(name)
        finally:
            requests.task_done()

def _play_bg(name):
    _init()
    with _state_lock:
        sound = _sounds.get(name) if _enabled and _mixer else None
        if sound is None:
            return
    try:
        sound.play()
    except Exception:
        pass

def set_volume(vol: float):
    global _volume
    with _state_lock:
        _volume = max(0.0, min(1.0, float(vol)))
        for s in tuple(_sounds.values()):
            try:
                s.set_volume(_volume)
            except Exception:
                pass

def set_enabled(val: bool):
    global _enabled
    with _state_lock:
        _enabled = val
