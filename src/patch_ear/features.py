"""Audio -> measurements. Pure functions, no I/O besides loading.

Dimensions (grouped):
  time/dynamics   duration_s attack_s decay_s sustain_db rms_db crest_db
  spectrum        centroid_hz rolloff_hz low_ratio hi_ratio flatness zcr        (loudness-weighted over the note)
  pitch/harmonics pitch_clarity inharmonic_ratio harm_tilt_db                    (need a tracked pitch; see below)
  modulation      roughness (fast beating 20-150 Hz) lfo_rate_hz lfo_depth (slow pulsing 0.5-12 Hz)
  evolution       timbre_movement (how much the brightness wanders while the note sounds)
  stereo          side_mid_db
Pitch-dependent measures are only meaningful when pitch_clarity is high; for unpitched sounds (drums, noise)
inharmonic_ratio is 1.0 and harm_tilt_db 0.0 by convention.
"""

from __future__ import annotations

import librosa
import numpy as np
import soundfile as sf
from scipy.signal import hilbert

N_FFT = 4096
HOP = 1024
PITCHED_MIN_CLARITY = 0.3

FEATURES = [
    "duration_s", "attack_s", "decay_s", "sustain_db", "rms_db", "crest_db",
    "centroid_hz", "rolloff_hz", "low_ratio", "hi_ratio", "flatness", "zcr",
    "pitch_clarity", "inharmonic_ratio", "harm_tilt_db",
    "roughness", "lfo_rate_hz", "lfo_depth", "timbre_movement",
    "side_mid_db",
]
EXTRA = ["f0_hz"]  # stored and filterable, but not ranked or used for similarity (a pitch is not a character)


def _db(x: float) -> float:
    return float(20 * np.log10(max(x, 1e-9)))


def load(path, max_seconds: float) -> tuple[np.ndarray, int, float]:
    """Return (channels x samples, sr, full_duration_s), truncated to max_seconds."""
    with sf.SoundFile(str(path)) as f:
        sr = f.samplerate
        total = f.frames / sr
        y = f.read(frames=int(max_seconds * sr), dtype="float32", always_2d=True)
    return y.T, sr, total


def _sustain_segment(mono: np.ndarray, sr: int, onset: int) -> np.ndarray | None:
    seg = mono[onset + int(0.3 * sr): onset + int(1.5 * sr)]
    if len(seg) < 8192:  # short sample: take what there is after the onset
        seg = mono[onset: onset + int(1.0 * sr)]
    return seg if len(seg) >= 8192 else None


def _track_pitch(seg: np.ndarray, sr: int) -> tuple[float, float]:
    """(f0 in Hz, clarity 0..1). Clarity = normalised autocorrelation at the pitch period: ~1 periodic, ~0 noise."""
    f0 = float(np.median(librosa.yin(seg, fmin=24, fmax=2000, sr=sr, frame_length=8192)))
    x = seg - seg.mean()
    n = len(x)
    ac = np.fft.irfft(np.abs(np.fft.rfft(x, 2 * n)) ** 2)[:n]
    ac = ac / (ac[0] + 1e-20)
    lag = sr / f0
    lo, hi = max(1, int(lag * 0.97)), min(n - 1, int(lag * 1.03) + 1)
    clarity = float(np.clip(ac[lo:hi].max(), 0, 1)) if hi > lo else 0.0
    return f0, clarity


def _harmonic_split(seg: np.ndarray, sr: int, f0: float) -> tuple[float, float]:
    """(inharmonic_ratio, harm_tilt_db): energy NOT on harmonics of f0, and harmonics 4+ versus 1-3 (dB)."""
    n = 1 << int(np.ceil(np.log2(len(seg))) + 1)
    X = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n)) ** 2
    fr = np.fft.rfftfreq(n, 1 / sr)
    top = min(10000.0, sr * 0.45)
    total = X[(fr > 20) & (fr < top)].sum() + 1e-20
    ks = [k for k in range(1, 41) if k * f0 < top]
    h = []
    for k in ks:  # tolerance grows slowly with k to follow slight vibrato/drift, but stays well under f0/2
        tol = min(0.12 * f0 + 0.004 * k * f0, 0.3 * f0)
        h.append(X[(fr > k * f0 - tol) & (fr < k * f0 + tol)].sum())
    h = np.array(h)
    inharm = float(np.clip(1 - h.sum() / total, 0, 1))
    low, high = h[:3].sum(), h[3:20].sum()
    return inharm, float(10 * np.log10((high + 1e-20) / (low + 1e-20)))


def _roughness(seg: np.ndarray, sr: int, f0: float | None) -> float:
    """Fast amplitude modulation (20-150 Hz, the 'growl'/beating of detuned oscillators), excluding the note's own
    periodicity. = coefficient of variation of the envelope x sqrt(share of its variance in that band)."""
    env = np.abs(hilbert(seg))
    mean = env.mean()
    if mean < 1e-9:
        return 0.0
    x = env - mean
    P = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    fr = np.fft.rfftfreq(len(x), 1 / sr)
    band = (fr >= 20) & (fr <= 150)
    if f0:  # the envelope of any periodic sound has lines at multiples of f0: that's pitch, not roughness
        for k in range(1, int(150 / f0) + 2):
            band &= ~((fr > k * f0 * 0.94) & (fr < k * f0 * 1.06))
    var = P[1:].sum() + 1e-20
    return float((x.std() / mean) * np.sqrt(P[band].sum() / var))


def _slow_modulation(env: np.ndarray, fps: float) -> tuple[float, float]:
    """(rate Hz, depth) of slow periodic loudness modulation (tremolo / LFO / pulsing), or (0, 0).
    Needs >= 3 cycles in the window and a spectral peak clearly above the rest of the band, otherwise it is not a modulation."""
    if len(env) < 6 * fps:  # 3 cycles of the slowest rate (0.5 Hz)
        return 0.0, 0.0
    seg = env / (env.mean() + 1e-12)
    t = np.arange(len(seg))
    x = seg - np.polyval(np.polyfit(t, seg, 1), t)  # remove the overall decay/swell trend
    P = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    fr = np.fft.rfftfreq(len(x), 1 / fps)
    band = np.nonzero((fr >= 0.5) & (fr <= 12.0))[0]
    if len(band) < 4:
        return 0.0, 0.0
    p = band[P[band].argmax()]
    prominence = P[p] / (np.median(P[band]) + 1e-20)
    cycles = fr[p] * len(seg) / fps
    if prominence < 8 or cycles < 3:
        return 0.0, 0.0
    frac = P[max(p - 1, 1): p + 2].sum() / (P[1:].sum() + 1e-20)
    return float(fr[p]), float(x.std() * np.sqrt(frac))


def extract(chans: np.ndarray, sr: int, full_duration_s: float) -> dict[str, float]:
    mono = chans.mean(axis=0)
    peak = float(np.abs(mono).max())
    if peak < 1e-6:
        raise ValueError("silent file")
    rms = float(np.sqrt(np.mean(mono**2)))

    # short samples (drum hits, single-cycle waves) get a smaller window instead of a padded one
    n_fft = int(min(N_FFT, max(256, 2 ** int(np.log2(max(len(mono), 2))))))
    hop = n_fft // 4
    S = np.abs(librosa.stft(mono, n_fft=n_fft, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    P = (S**2).mean(axis=1)
    total = float(P.sum()) + 1e-20

    env = librosa.feature.rms(S=S, frame_length=n_fft)[0]
    fps = sr / hop
    # Frame-level statistics must be loudness-weighted: an unweighted mean lets the near-silent tail (noise floor, high
    # centroid, high flatness) dominate a note that decays, e.g. a clean sub measured "bright" and "noisy".
    # (centroid/rolloff use the POWER spectrum: the magnitude one lets a low broadband noise floor outweigh strong low partials)
    w = env / (env.sum() + 1e-12)

    def wavg(frames: np.ndarray) -> float:
        n = min(len(frames), len(w))
        return float((frames[:n] * w[:n]).sum() / (w[:n].sum() + 1e-12))

    ipk = int(env.argmax())
    attack_s = float(np.nonzero(env >= 0.9 * env[ipk])[0][0] / fps)

    # decay / sustain: from the loudest SMOOTHED frame on (a 100 ms smoothing so an initial click is not mistaken for the peak
    # and everything after it for "20 dB down")
    k = max(1, int(0.1 * fps))
    env_s = np.convolve(env, np.ones(k) / k, mode="same") if k > 1 else env
    after = env_s[int(env_s.argmax()):]
    below = np.nonzero(after < 0.1 * after[0])[0]  # first time 20 dB under the peak
    decay_s = float((below[0] if len(below) else len(after)) / fps)  # no decay within the window -> the window length
    sustain_db = max(_db(after[min(len(after) - 1, int(1.0 * fps))] / (after[0] + 1e-12)), -60.0)

    lfo_rate, lfo_depth = _slow_modulation(after, fps)

    # evolution: how much the brightness wanders while the note is clearly sounding
    cent_frames = librosa.feature.spectral_centroid(S=S**2, sr=sr, n_fft=n_fft)[0]
    onset_f = int(np.argmax(env >= 0.2 * env.max()))
    loud = np.nonzero(env >= 0.3 * env.max())[0]
    loud = loud[(loud >= onset_f + int(0.1 * fps)) & (loud < len(cent_frames))]
    movement = float(np.std(np.log2(np.maximum(cent_frames[loud], 20.0)))) if len(loud) >= 6 else 0.0

    # pitch-dependent measures on a steady part of the note
    f0, clarity, inharm, tilt, rough = 0.0, 0.0, 1.0, 0.0, 0.0
    seg = _sustain_segment(mono, sr, onset_f * hop)
    if seg is not None:
        f0, clarity = _track_pitch(seg, sr)
        pitched = clarity >= PITCHED_MIN_CLARITY
        if pitched:
            inharm, tilt = _harmonic_split(seg, sr, f0)
        rough = _roughness(seg, sr, f0 if pitched else None)
    f0 = f0 if clarity >= PITCHED_MIN_CLARITY else 0.0

    side_mid = -120.0
    if chans.shape[0] >= 2:
        mid, side = (chans[0] + chans[1]) / 2, (chans[0] - chans[1]) / 2
        side_mid = float(10 * np.log10((np.sum(side**2) + 1e-20) / (np.sum(mid**2) + 1e-20)))

    return {
        "duration_s": float(full_duration_s), "attack_s": attack_s, "decay_s": decay_s, "sustain_db": float(sustain_db),
        "rms_db": _db(rms), "crest_db": _db(peak) - _db(rms),
        "centroid_hz": wavg(librosa.feature.spectral_centroid(S=S**2, sr=sr, n_fft=n_fft)[0]),
        "rolloff_hz": wavg(librosa.feature.spectral_rolloff(S=S**2, sr=sr, n_fft=n_fft)[0]),
        "low_ratio": float(P[freqs < 150].sum() / total),
        "hi_ratio": float(P[freqs > 2000].sum() / total),
        "flatness": wavg(librosa.feature.spectral_flatness(S=S)[0]),
        "zcr": wavg(librosa.feature.zero_crossing_rate(mono, frame_length=n_fft, hop_length=hop)[0]),
        "pitch_clarity": float(clarity), "inharmonic_ratio": float(inharm), "harm_tilt_db": float(tilt),
        "roughness": float(rough), "lfo_rate_hz": lfo_rate, "lfo_depth": lfo_depth, "timbre_movement": movement,
        "side_mid_db": side_mid,
        "f0_hz": float(f0),
    }


def measured_midi(chans: np.ndarray, sr: int) -> float | None:
    """Real pitch of a sustained note as a (fractional) MIDI number, from the audio, or None if it can't be tracked.

    Library note labels disagree with each other (a "C1" can be 33 Hz or 65 Hz), so presets are matched on measured pitch."""
    mono = chans.mean(axis=0)
    env = librosa.feature.rms(y=mono, frame_length=2048, hop_length=512)[0]
    if env.max() < 1e-6:
        return None
    seg = _sustain_segment(mono, sr, int(np.argmax(env > 0.2 * env.max())) * 512)
    if seg is None:
        return None
    f0 = float(np.median(librosa.yin(seg, fmin=24, fmax=2000, sr=sr, frame_length=8192)))
    return 69 + 12 * np.log2(f0 / 440.0) if f0 > 0 else None
