"""Radio effect post-processing — broadcast team-radio character.

Modelled on how F1/WEC pit radio sounds on a world feed rather than on
old analogue static. The character comes from four things, in order of
how much they matter:

1. A narrow band-pass (roughly 350 Hz – 3.2 kHz). This is most of it.
2. Heavy compression — comms radio runs aggressive AGC, so every word
   lands at the same level and sounds slightly breathless.
3. A presence lift around 2 kHz so speech cuts through engine noise.
4. Squelch: a short mic click as the transmission opens and closes.

Deliberately *not* a constant hiss bed. Modern race radio is digital;
steady white noise reads as tape hiss and gets annoying fast. The noise
control defaults low and is band-limited to the voice band so what
little there is sounds like radio rather than fizz.
"""

import numpy as np
from scipy.ndimage import maximum_filter1d
from scipy.signal import butter, lfilter, sosfilt

_EPS = 1e-9


# ── Building blocks ─────────────────────────────────────────────────


def bandpass_filter(audio: np.ndarray, low: float, high: float,
                    sr: int, order: int = 4) -> np.ndarray:
    """Band-pass filter audio (broadcast radio: ~350–3200 Hz)."""
    nyq = sr / 2
    low_n = max(1e-4, min(low / nyq, 0.99))
    high_n = max(low_n + 1e-3, min(high / nyq, 0.99))
    sos = butter(order, [low_n, high_n], btype="band", output="sos")
    return sosfilt(sos, audio)


def peaking_eq(audio: np.ndarray, freq: float, gain_db: float,
               q: float, sr: int) -> np.ndarray:
    """RBJ peaking EQ — the presence lift that makes speech cut through."""
    if abs(gain_db) < 0.01:
        return audio
    a_gain = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * min(freq, sr / 2 * 0.99) / sr
    alpha = np.sin(w0) / (2 * max(q, 0.1))
    cos_w0 = np.cos(w0)

    b = np.array([1 + alpha * a_gain, -2 * cos_w0, 1 - alpha * a_gain])
    a = np.array([1 + alpha / a_gain, -2 * cos_w0, 1 - alpha / a_gain])
    return lfilter(b / a[0], a / a[0], audio)


def compress(audio: np.ndarray, sr: int, threshold_db: float = -24.0,
             ratio: float = 6.0, attack_ms: float = 3.0,
             release_ms: float = 90.0) -> np.ndarray:
    """Feed-forward compressor with make-up gain.

    Attack is a peak-hold window and release a one-pole decay, so the
    whole detector stays vectorised — this runs per spoken message and
    latency on track matters.
    """
    if ratio <= 1.0 or audio.size == 0:
        return audio

    attack_samples = max(1, int(sr * attack_ms / 1000))
    envelope = maximum_filter1d(np.abs(audio), size=attack_samples, mode="nearest")

    release_coeff = np.exp(-1.0 / max(1.0, sr * release_ms / 1000))
    envelope = lfilter([1 - release_coeff], [1, -release_coeff], envelope)

    envelope_db = 20 * np.log10(envelope + _EPS)
    over_db = np.maximum(0.0, envelope_db - threshold_db)
    gain = 10 ** ((-over_db * (1 - 1 / ratio)) / 20)

    compressed = audio * gain

    # Make-up: bring the compressed signal back to the original peak.
    peak_in = float(np.abs(audio).max())
    peak_out = float(np.abs(compressed).max())
    if peak_out > _EPS and peak_in > _EPS:
        compressed *= min(peak_in / peak_out, 8.0)
    return compressed


def soft_clip(audio: np.ndarray, drive: float = 1.0) -> np.ndarray:
    """Soft saturation — the mild grit of an overdriven comms mic."""
    if drive <= 1.0:
        return np.tanh(audio)
    return np.tanh(audio * drive) / np.tanh(drive)


def _squelch_burst(sr: int, duration_ms: float, level: float,
                   low: float, high: float, decay: float = 6.0) -> np.ndarray:
    """Short band-limited noise burst: the mic keying on or off."""
    n = max(1, int(sr * duration_ms / 1000))
    burst = np.random.normal(0, 1.0, n)
    burst = bandpass_filter(burst, low, high, sr, order=4)
    burst *= np.exp(-decay * np.linspace(0, 1, n))
    peak = float(np.abs(burst).max())
    if peak > _EPS:
        burst = burst / peak * level
    return burst


def add_static(audio: np.ndarray, noise_level: float = 0.02,
               sr: int = 48000, low: float = 350.0,
               high: float = 3200.0) -> np.ndarray:
    """Add band-limited noise. Unlike white noise this reads as radio."""
    if noise_level <= 0:
        return audio
    noise = np.random.normal(0, noise_level, len(audio))
    # Steep, so the bed stays inside the voice band. Full-spectrum noise
    # is what makes a radio effect sound like tape hiss.
    return audio + bandpass_filter(noise, low, high, sr, order=6)


# ── The chain ───────────────────────────────────────────────────────


def apply_radio_effect(
    audio: np.ndarray,
    intensity: float = 0.7,
    sr: int = 48000,
    low_cut: float = 350.0,
    high_cut: float = 3200.0,
    noise: float = 0.12,
    compression: float = 0.6,
    squelch: bool = True,
) -> np.ndarray:
    """Broadcast team-radio chain: band-pass → presence → compress → clip → squelch.

    @param audio: float samples in [-1, 1]
    @param intensity: 0 (dry) → 1 (full narrow-band radio). Controls the
        band-pass width and saturation only — not the noise level.
    @param sr: sample rate
    @param low_cut: band-pass low cutoff in Hz at full intensity
    @param high_cut: band-pass high cutoff in Hz at full intensity
    @param noise: band-limited noise bed, 0 (clean, the F1 default) → 1 (loud)
    @param compression: 0 (none) → 1 (heavily squashed comms AGC)
    @param squelch: mic click as the transmission opens and closes
    @return: processed audio
    """
    audio = np.asarray(audio, dtype=np.float64).flatten()
    if audio.size == 0:
        return audio.astype(np.float32)

    intensity = float(np.clip(intensity, 0.0, 1.0))
    if intensity <= 0 and compression <= 0 and noise <= 0 and not squelch:
        return audio.astype(np.float32)

    # 1. Band-pass. Lower intensity keeps more of the original bandwidth.
    band_low = 80 + (low_cut - 80) * intensity
    band_high = min(high_cut + (1 - intensity) * 4000, sr / 2 * 0.98)
    out = bandpass_filter(audio, band_low, band_high, sr, order=4)

    # 2. Presence lift so the voice cuts through engine and tyre noise.
    out = peaking_eq(out, freq=2000.0, gain_db=5.0 * intensity, q=1.0, sr=sr)

    # 3. Compression — the sound of comms AGC.
    compression = float(np.clip(compression, 0.0, 1.0))
    if compression > 0:
        out = compress(
            out,
            sr,
            threshold_db=-18.0 - 12.0 * compression,
            ratio=1.0 + 7.0 * compression,
            attack_ms=3.0,
            release_ms=90.0,
        )

    # 4. Light saturation.
    peak = float(np.abs(out).max())
    if peak > _EPS:
        out = out / peak * 0.9
    out = soft_clip(out, drive=1.0 + 2.0 * intensity)

    # 5. Noise bed — band-limited, and off by default at low settings.
    if noise > 0:
        out = add_static(out, noise_level=0.02 * noise, sr=sr,
                         low=band_low, high=band_high)

    # 6. Squelch: fades plus the mic clicks that bracket a transmission.
    fade = min(int(0.008 * sr), out.size // 2)
    if fade > 0:
        out[:fade] *= np.linspace(0, 1, fade)
        out[-fade:] *= np.linspace(1, 0, fade)

    if squelch:
        click_level = 0.12 + 0.10 * intensity
        head = _squelch_burst(sr, 14, click_level, band_low, band_high, decay=7.0)
        tail = _squelch_burst(sr, 32, click_level * 1.1, band_low, band_high, decay=4.5)
        gap = np.zeros(int(sr * 0.012))
        out = np.concatenate([head, gap, out, gap, tail])

    return np.clip(out, -1.0, 1.0).astype(np.float32)
