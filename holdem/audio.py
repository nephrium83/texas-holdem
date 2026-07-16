"""Synthesized sound effects — no external audio files needed."""
import threading, math, struct, wave, io

_enabled = True
_volume = 0.7
_mixer = None
_sounds = {}

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
    global _mixer, _sounds
    if _mixer is not None:
        return
    try:
        import pygame.mixer as mx
        mx.init(frequency=22050, size=-16, channels=1, buffer=256)
        _mixer = mx
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
        for name, (pcm, sr) in specs.items():
            wav_bytes = _make_wav(pcm, sr)
            sound = mx.Sound(buffer=wav_bytes)
            sound.set_volume(_volume)
            _sounds[name] = sound
    except Exception:
        _mixer = False  # mark as unavailable

def play(name: str):
    if not _enabled:
        return
    threading.Thread(target=_play_bg, args=(name,), daemon=True).start()

def _play_bg(name):
    _init()
    if not _mixer or name not in _sounds:
        return
    try:
        _sounds[name].play()
    except Exception:
        pass

def set_volume(vol: float):
    global _volume
    _volume = max(0.0, min(1.0, float(vol)))
    for s in _sounds.values():
        try:
            s.set_volume(_volume)
        except Exception:
            pass

def set_enabled(val: bool):
    global _enabled
    _enabled = val
