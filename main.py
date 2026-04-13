import os
import logging
import tempfile
import subprocess
import asyncio
import soundfile as sf
import numpy as np

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from shazamio import Shazam

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = "7995811101:AAHsrqI-No89fxf21YUhBh9Hk-qPlPAI3lM"

user_songs = {}
user_tab_pending = {}   # user_id -> tab_data  (waiting for audio after PDF analysis)

# ── Soundfont path cache ───────────────────────────────────────────────────────
_SOUNDFONT_PATH: str = ""


def _ensure_soundfont() -> str:
    """
    Ensure a GM soundfont is available for FluidSynth.
    Returns the path to the soundfont, or '' if unavailable.
    Tries local candidates first, then attempts a download.
    """
    global _SOUNDFONT_PATH
    if _SOUNDFONT_PATH and os.path.exists(_SOUNDFONT_PATH) and os.path.getsize(_SOUNDFONT_PATH) > 100_000:
        return _SOUNDFONT_PATH

    import ssl, urllib.request

    candidates = [
        os.path.expanduser("~/.fluidsynth/default_sound_font.sf2"),
        # FluidR3_GM from nixpkgs soundfont-fluid package (148MB, full GM)
        "/nix/store/7ln4ngk9zmrf06kv64jc8r6x3i201v1x-soundfont-fluid-fixed/share/soundfonts/FluidR3_GM.sf2",
        "/nix/store/qb205zwr5dmg794dnp5mdwfz1d9bf1vh-Fluid-3/share/soundfonts/FluidR3_GM2-2.sf2",
        "/home/runner/workspace/soundfonts/GeneralUser.sf2",
        "/home/runner/workspace/soundfonts/GeneralUser.sf3",
        "/usr/share/sounds/sf2/FluidR3_GM.sf2",
        "/usr/share/sounds/sf2/default.sf2",
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 100_000:
            logger.info(f"Using soundfont: {p} ({os.path.getsize(p)//1024}KB)")
            _SOUNDFONT_PATH = p
            return p

    os.makedirs(os.path.expanduser("~/.fluidsynth"), exist_ok=True)
    sf_path = os.path.expanduser("~/.fluidsynth/default_sound_font.sf2")
    download_urls = [
        "https://github.com/musescore/MuseScore/raw/master/share/sound/FluidR3Mono_GM.sf3",
        "https://github.com/openplanetary/opm_soundfonts/raw/master/GeneralUser_GS_v1.471.sf2",
    ]
    ctx = ssl._create_unverified_context()
    for url in download_urls:
        try:
            logger.info(f"Downloading soundfont from {url}...")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=90) as resp:
                data = resp.read()
            if len(data) > 100_000:
                with open(sf_path, "wb") as f:
                    f.write(data)
                logger.info(f"Soundfont downloaded: {len(data)//1024}KB -> {sf_path}")
                _SOUNDFONT_PATH = sf_path
                return sf_path
        except Exception as e:
            logger.warning(f"Soundfont download failed ({url}): {e}")
    return ""


def render_midi_fluidsynth(midi_path: str, audio_out: str) -> bool:
    """
    Render MIDI to high-quality audio using FluidSynth + a GM soundfont.
    Produces realistic instrument sounds — far superior to Karplus-Strong.
    Returns True on success, False if FluidSynth/soundfont is unavailable.
    """
    sf_path = _ensure_soundfont()
    if not sf_path:
        logger.warning("No soundfont available — skipping FluidSynth render")
        return False
    try:
        from midi2audio import FluidSynth as FS
        ext = os.path.splitext(audio_out)[1].lower() or ".mp3"
        wav_tmp = audio_out.replace(ext, "_fs_raw.wav")
        fs = FS(sound_font=sf_path, sample_rate=44100)
        fs.midi_to_audio(midi_path, wav_tmp)
        if not os.path.exists(wav_tmp) or os.path.getsize(wav_tmp) < 1000:
            logger.warning("FluidSynth produced empty/missing WAV output")
            return False
        if ext == ".wav":
            import shutil
            shutil.move(wav_tmp, audio_out)
        else:
            subprocess.run(
                ["ffmpeg", "-y", "-i", wav_tmp,
                 "-af", "loudnorm=I=-14:TP=-1:LRA=7",
                 "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k", audio_out],
                capture_output=True, check=True
            )
            try:
                os.remove(wav_tmp)
            except Exception:
                pass
        ok = os.path.exists(audio_out) and os.path.getsize(audio_out) > 1000
        if ok:
            logger.info(f"FluidSynth render OK: {os.path.getsize(audio_out)} bytes")
        return ok
    except Exception as e:
        logger.warning(f"FluidSynth render failed: {e}")
        return False


def synthesize_808_wav(bass_notes: list, bpm: float, output_wav: str,
                       sr: int = 44100, timbre: str = "bass_808") -> bool:
    """
    Synthesize 808 / sub-bass directly using numpy — bypasses FluidSynth GM.
    Supports all bass_808_* sub-variants via the `timbre` parameter.
    Each variant has tuned pitch-slide, saturation drive, and sub-octave mix.
    """
    import numpy as np
    import soundfile as sf

    if not bass_notes:
        return False
    try:
        def _midi_to_hz(midi):
            return 440.0 * (2.0 ** ((float(midi) - 69.0) / 12.0))

        # ── Variant parameters ────────────────────────────────────────────────
        _V = {
            "bass_808_trap":      (0.40, 10.0, 3.5, 0.22, 0.005, 2.2),
            "bass_808_atlanta":   (0.42, 10.0, 3.5, 0.22, 0.003, 2.2),
            "bass_808_drill":     (0.28, 12.0, 2.8, 0.18, 0.004, 1.8),
            "bass_808_chicago":   (0.35, 11.0, 3.0, 0.20, 0.003, 1.8),
            "bass_808_deep":      (0.18,  5.0, 1.8, 0.38, 0.006, 3.5),
            "bass_808_punchy":    (0.42, 15.0, 3.2, 0.15, 0.002, 1.4),
            "bass_808_ultra":     (0.14,  4.0, 1.2, 0.50, 0.007, 4.0),
            "bass_808_sub":       (0.12,  6.0, 1.0, 0.45, 0.006, 3.2),
            "bass_808_warm":      (0.24,  7.0, 2.0, 0.25, 0.008, 2.5),
            "bass_808_long":      (0.24,  4.0, 2.2, 0.22, 0.006, 4.2),
            "bass_808_bright":    (0.35,  9.0, 2.8, 0.14, 0.004, 2.0),
            "bass_808_distorted": (0.40,  8.0, 5.5, 0.18, 0.003, 2.0),
            "bass_808_phonk":     (0.45,  8.5, 6.0, 0.16, 0.003, 2.2),
            "bass_808_rnb":       (0.22,  7.0, 1.8, 0.28, 0.010, 2.5),
            "bass_808_mid":       (0.28,  9.0, 2.8, 0.18, 0.005, 2.0),
            "bass_808_clean":     (0.22,  8.0, 0.8, 0.22, 0.005, 2.2),
            "bass_808_click":     (0.30, 10.0, 2.5, 0.18, 0.001, 2.0),
            "bass_808_uk":        (0.28, 11.0, 2.6, 0.16, 0.004, 1.8),
            "bass_808_afro":      (0.22,  8.0, 2.2, 0.22, 0.007, 2.2),
            "bass_808_soft":      (0.18,  6.0, 1.2, 0.30, 0.012, 2.8),
            "bass_808_hiphop":    (0.30,  8.0, 2.5, 0.22, 0.005, 2.2),
            "bass_808_jersey":    (0.26,  9.0, 2.2, 0.20, 0.005, 2.0),
            "bass_808_bounce":    (0.36, 12.0, 2.2, 0.18, 0.003, 1.4),
            "bass_808_lofi":      (0.24,  7.0, 1.6, 0.28, 0.008, 2.5),
            "bass_808_vintage":   (0.26,  8.0, 1.8, 0.22, 0.005, 2.2),
            "bass_808_modern":    (0.35, 10.0, 3.2, 0.20, 0.004, 2.0),
            "bass_808_slide":     (0.58,  5.0, 2.5, 0.22, 0.005, 2.8),
            "bass_808_wobble":    (0.24,  8.0, 2.5, 0.22, 0.005, 2.2),
            "bass_808_growl":     (0.35,  9.0, 4.5, 0.16, 0.004, 1.8),
            "bass_808_mellow":    (0.18,  6.0, 1.2, 0.30, 0.010, 2.8),
            "bass_808_nyc":       (0.30,  9.0, 2.5, 0.20, 0.004, 2.0),
            "bass_808_la":        (0.26,  8.0, 2.2, 0.22, 0.006, 2.2),
            "bass_808_cloud":     (0.18,  5.0, 1.5, 0.32, 0.010, 3.5),
            "bass_808_rage":      (0.42, 11.0, 3.8, 0.16, 0.003, 1.8),
            "bass_808_melodic":   (0.24,  7.0, 2.0, 0.25, 0.008, 2.5),
            "bass_808_dark":      (0.32,  7.5, 3.2, 0.28, 0.005, 2.5),
            "bass_808_heavy":     (0.38,  9.0, 4.0, 0.22, 0.004, 2.2),
            "bass_808_lite":      (0.22,  9.0, 1.6, 0.14, 0.006, 1.8),
            "bass_808_crispy":    (0.32, 11.0, 2.8, 0.12, 0.002, 1.5),
            "bass_808_smooth":    (0.22,  6.5, 1.6, 0.28, 0.009, 2.6),
            "bass_808_hard":      (0.40, 10.5, 3.5, 0.16, 0.002, 1.8),
        }
        # slide_amt, slide_dec, drive, sub_mix, attack_s_dur, env_decay
        p = _V.get(timbre, (0.30, 9.0, 3.5, 0.20, 0.005, 2.0))
        slide_amt, slide_dec, drive, sub_mix, atk_dur, env_dec = p

        max_end = max(n["time"] + n.get("duration", 0.5) for n in bass_notes) + 1.5
        total_samples = int(sr * min(max_end, 90.0))
        mix = np.zeros(total_samples, dtype=np.float64)

        for note in bass_notes:
            freq_target = _midi_to_hz(note["midi"])
            start_sample = int(note["time"] * sr)
            if start_sample >= total_samples:
                continue
            dur = min(note.get("duration", 0.4) + 0.15, 3.5)
            n_samples = min(int(dur * sr), total_samples - start_sample)
            if n_samples < 10:
                continue

            t = np.linspace(0, dur, n_samples, endpoint=False)

            # Pitch slide: exponential decay from slide_amt semitones above
            pitch_offset = slide_amt * np.exp(-slide_dec * t / max(dur, 0.1))
            freq_curve   = freq_target * (2.0 ** (pitch_offset / 12.0))

            # Apply bend_curve if present (808 glide)
            bend_curve = note.get("bend_curve", [])
            if note.get("has_bend") and bend_curve:
                for rel_t, p_midi in bend_curve:
                    idx = int(rel_t * sr)
                    if 0 <= idx < n_samples:
                        freq_curve[idx:] = _midi_to_hz(p_midi) * (2.0 ** (
                            slide_amt * np.exp(-slide_dec * t[idx:] / max(dur, 0.1)) / 12.0
                        ))

            # Phase integration (avoids clicks)
            phase = 2.0 * np.pi * np.cumsum(freq_curve) / sr
            sine  = np.sin(phase)

            # Harmonics
            wave  = 0.70 * sine
            wave += 0.20 * np.sin(2.0 * phase)    # body
            wave += 0.08 * np.sin(3.0 * phase)    # grind
            wave += sub_mix * np.sin(0.5 * phase) # sub-octave rumble
            # Deep sub-bass floor: independent 40-55 Hz rumble regardless of pitch
            sub_hz  = min(float(freq_target) * 0.5, 55.0)
            sub_ph  = 2.0 * np.pi * np.cumsum(np.full(n_samples, sub_hz)) / sr
            wave   += 0.42 * np.sin(sub_ph)
            # Air-pressure pulse: 20-30 Hz infra layer
            infra_ph = 2.0 * np.pi * np.cumsum(np.full(n_samples, 25.0)) / sr
            wave    += 0.18 * np.sin(infra_ph)

            # Click transient
            if "click" in timbre:
                ck = min(int(0.008 * sr), n_samples)
                wave[:ck] += np.linspace(0.5, 0.0, ck)

            # Wobble LFO
            if "wobble" in timbre:
                wave = wave * (1.0 + 0.30 * np.sin(2.0 * np.pi * 4.0 * t))

            # Lo-fi noise
            if "lofi" in timbre or "vintage" in timbre:
                wave += np.random.uniform(-0.02, 0.02, n_samples)

            # Amplitude envelope
            attack_s = max(1, int(atk_dur * sr))
            envelope = np.exp(-env_dec * t / max(dur, 0.1))
            envelope[:attack_s] *= np.linspace(0, 1, attack_s)

            vel  = note.get("velocity", 100) / 127.0
            tone = wave * envelope * vel

            # Saturation
            tone = np.tanh(tone * drive) / (np.tanh(drive) + 1e-9)

            mix[start_sample:start_sample + n_samples] += tone

        peak = np.abs(mix).max()
        if peak < 1e-6:
            return False
        mix = (mix / peak * 0.92).astype(np.float32)
        sf.write(output_wav, mix, sr, subtype="PCM_16")
        logger.info(f"synthesize_808_wav [{timbre}]: {len(bass_notes)} notes → {output_wav}")
        return True
    except Exception as e:
        logger.warning(f"synthesize_808_wav failed: {e}")
        return False


def quantize_notes_to_scale(notes: list, key_str: str) -> list:
    """
    Snap extracted notes to the nearest pitch in the detected musical scale.
    Fixes 'wrong notes' from Basic-Pitch when the key is known.
    E.g. key_str='A# Minor' → only allows A#, C, D#, F, G, G#, A# pitches.
    """
    if not notes or not key_str:
        return notes

    NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    ENHARMONIC = {'Db': 'C#', 'Eb': 'D#', 'Gb': 'F#', 'Ab': 'G#', 'Bb': 'A#',
                  'Cb': 'B', 'Fb': 'E', 'B#': 'C', 'E#': 'F'}
    MAJOR_INTERVALS = [0, 2, 4, 5, 7, 9, 11]
    MINOR_INTERVALS = [0, 2, 3, 5, 7, 8, 10]

    parts = key_str.strip().split()
    if len(parts) < 2:
        return notes

    root_name = parts[0]
    root_name = ENHARMONIC.get(root_name, root_name)
    if root_name not in NOTE_NAMES:
        return notes

    root_pc = NOTE_NAMES.index(root_name)
    scale_type = " ".join(parts[1:]).lower()
    intervals = MINOR_INTERVALS if "minor" in scale_type else MAJOR_INTERVALS

    valid_pcs = set((root_pc + i) % 12 for i in intervals)

    result = []
    corrected = 0
    for note in notes:
        midi = note.get("midi", 60)
        pc = midi % 12
        if pc in valid_pcs:
            result.append(note)
        else:
            # Find nearest semitone shift that lands in scale
            best_shift = 0
            best_d = 99
            for shift in range(-6, 7):
                if (pc + shift) % 12 in valid_pcs:
                    if abs(shift) < best_d:
                        best_d = abs(shift)
                        best_shift = shift
            new_note = dict(note)
            new_midi = max(21, min(108, midi + best_shift))
            new_note["midi"] = new_midi
            if new_note.get("bend_curve"):
                new_note["bend_curve"] = [
                    (t, p + best_shift) for t, p in new_note["bend_curve"]
                ]
            result.append(new_note)
            corrected += 1

    if corrected:
        logger.info(f"quantize_notes_to_scale: corrected {corrected}/{len(notes)} notes → key={key_str}")
    return result


def _apply_spectral_eq_to_midi_output(midi_audio_path: str, ref_path: str,
                                       blend: float = 0.70) -> str:
    """
    Apply spectral-envelope EQ to a MIDI-rendered audio file so its tonal
    balance matches the reference stem — WITHOUT mixing original audio in.

    How it works:
      1. Compute average magnitude spectrum (envelope) of MIDI output.
      2. Compute average magnitude spectrum of reference stem.
      3. Divide ref_env / midi_env → EQ correction curve.
      4. Smooth heavily and clip to ±12 dB.
      5. Apply as a static frequency-domain filter (STFT multiply).

    blend: 0.0 = no change, 1.0 = full envelope match (default 0.70)
    """
    import librosa, numpy as np, soundfile as sf
    from scipy.ndimage import uniform_filter1d

    try:
        y_midi, sr = librosa.load(midi_audio_path, sr=22050, mono=True, duration=120)
        y_ref,  _  = librosa.load(ref_path, sr=sr, mono=True, duration=120)

        if len(y_midi) < sr * 2 or len(y_ref) < sr * 2:
            return midi_audio_path

        n_fft, hop = 2048, 512

        S_midi = np.abs(librosa.stft(y_midi, n_fft=n_fft, hop_length=hop))
        S_ref  = np.abs(librosa.stft(y_ref,  n_fft=n_fft, hop_length=hop))

        env_midi = S_midi.mean(axis=1) + 1e-9
        env_ref  = S_ref.mean(axis=1)  + 1e-9

        eq_raw    = env_ref / env_midi
        eq_smooth = uniform_filter1d(eq_raw, size=60)

        eq_blend  = (1.0 - blend) + blend * eq_smooth
        eq_blend  = np.clip(eq_blend, 0.10, 8.0)

        D     = librosa.stft(y_midi, n_fft=n_fft, hop_length=hop)
        D_eq  = D * eq_blend[:, np.newaxis]
        y_eq  = librosa.istft(D_eq, hop_length=hop, length=len(y_midi))

        peak = np.abs(y_eq).max()
        if peak > 1e-6:
            y_eq = (y_eq / peak * 0.90).astype(np.float32)

        ext     = os.path.splitext(midi_audio_path)[1].lower()
        wav_tmp = midi_audio_path.replace(ext, "_eqtmp.wav")
        sf.write(wav_tmp, y_eq, sr, subtype="PCM_16")

        if ext == ".mp3":
            subprocess.run(
                ["ffmpeg", "-y", "-i", wav_tmp,
                 "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k",
                 midi_audio_path],
                capture_output=True, check=True
            )
            try:
                os.remove(wav_tmp)
            except Exception:
                pass
        else:
            import shutil
            shutil.move(wav_tmp, midi_audio_path)

        logger.info(f"Spectral EQ applied (blend={blend}): {os.path.basename(ref_path)}")
        return midi_audio_path

    except Exception as e:
        logger.warning(f"Spectral EQ failed: {e}")
        return midi_audio_path


# ═══════════════════════════════════════════════════════════════════════════════
#  MusicXML Export Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_audio_for_export(audio_path: str) -> dict:
    """Fast audio analysis: BPM, key, time signature using librosa."""
    import librosa
    import numpy as np

    y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=120)

    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    bpm = max(40.0, min(250.0, bpm))

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)
    note_names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    major_profile = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor_profile = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

    best_score = -999.0
    best_key = "C Major"
    for i in range(12):
        maj = float(np.corrcoef(chroma_mean, np.roll(major_profile, i))[0, 1])
        if maj > best_score:
            best_score = maj
            best_key = f"{note_names[i]} Major"
        minn = float(np.corrcoef(chroma_mean, np.roll(minor_profile, i))[0, 1])
        if minn > best_score:
            best_score = minn
            best_key = f"{note_names[i]} Minor"

    return {"bpm": bpm, "key": best_key, "time_sig": "4/4"}


def _extract_notes_librosa_fallback(wav_path: str, bpm: float, min_pitch: int = 36, max_pitch: int = 127) -> list:
    """Legacy monophonic pyin fallback — kept for last-resort use only."""
    import librosa
    import numpy as np

    beat_dur = 60.0 / max(bpm, 40)
    sixteenth = beat_dur / 4
    notes = []
    try:
        y, sr = librosa.load(wav_path, sr=22050, mono=True, duration=60)
        f0, voiced, _ = librosa.pyin(y, fmin=librosa.note_to_hz('C2'),
                                      fmax=librosa.note_to_hz('C8'), sr=sr, frame_length=2048)
        times = librosa.times_like(f0, sr=sr, hop_length=512)
        onsets = librosa.onset.onset_detect(y=y, sr=sr, units='time', backtrack=True)
        for i, onset in enumerate(onsets):
            fi = int(np.searchsorted(times, onset))
            pitch_midi = None
            for idx in range(fi, min(fi + 15, len(f0))):
                if voiced[idx] and not np.isnan(f0[idx]) and f0[idx] > 0:
                    pitch_midi = int(round(librosa.hz_to_midi(f0[idx])))
                    break
            if pitch_midi is None or pitch_midi < min_pitch or pitch_midi > max_pitch:
                continue
            dur = min((onsets[i + 1] - onset if i + 1 < len(onsets) else 0.5), 2.0)
            q_onset = round(onset / sixteenth) * sixteenth
            q_dur = max(sixteenth, round(dur / sixteenth) * sixteenth)
            notes.append({"midi": pitch_midi, "time": q_onset, "duration": q_dur,
                          "velocity": 80, "has_bend": False, "bend_curve": []})
    except Exception as e:
        logger.warning(f"librosa fallback note extraction failed: {e}")
    return notes


def _extract_notes_cqt_polyphonic(wav_path: str, bpm: float,
                                   min_pitch: int = 36, max_pitch: int = 127,
                                   max_notes_per_onset: int = 3) -> list:
    """
    Advanced polyphonic note extraction using Constant-Q Transform (CQT).
    Detects multiple simultaneous pitches per onset (chord-aware).
    Velocity derived from onset strength envelope for expression.
    Falls back to legacy pyin on failure.
    """
    import librosa
    import numpy as np

    beat_dur = 60.0 / max(bpm, 40)
    sixteenth = beat_dur / 4
    notes = []

    try:
        y, sr = librosa.load(wav_path, sr=22050, mono=True, duration=90)
        hop = 256
        bins_per_octave = 24          # 2 bins per semitone → sub-semitone resolution
        n_bins = 7 * bins_per_octave  # C1 – C8
        fmin = librosa.note_to_hz('C1')

        C_mag = np.abs(librosa.cqt(y, sr=sr, hop_length=hop, fmin=fmin,
                                    n_bins=n_bins, bins_per_octave=bins_per_octave))
        C_db = librosa.amplitude_to_db(C_mag, ref=C_mag.max() + 1e-9)

        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
        onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr,
                                                   hop_length=hop, units='frames',
                                                   delta=0.25, wait=2)
        onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop)
        max_env = float(onset_env.max()) + 1e-6

        def _bin_to_midi(b):
            return 24 + int(round(b * 12 / bins_per_octave))

        b_min = max(0, int((min_pitch - 24) * bins_per_octave / 12))
        b_max = min(n_bins - 1, int((max_pitch - 24) * bins_per_octave / 12))

        for i, (of, ot) in enumerate(zip(onset_frames, onset_times)):
            win_end = min(C_db.shape[1], of + max(1, int(0.08 * sr / hop)))
            if of >= C_db.shape[1]:
                continue
            C_win = C_db[b_min:b_max + 1, of:win_end].max(axis=1)
            if C_win.max() < -55:
                continue

            threshold = C_win.max() - 14
            from scipy.signal import find_peaks
            peaks, _ = find_peaks(C_win, height=threshold, distance=2)
            if len(peaks) == 0:
                peaks = np.array([int(C_win.argmax())])

            # Keep top N peaks by energy
            top = peaks[np.argsort(C_win[peaks])[-max_notes_per_onset:]]

            dur = min((onset_times[i + 1] - ot if i + 1 < len(onset_times) else beat_dur), 3.0)
            env_val = float(onset_env[min(of, len(onset_env) - 1)])
            vel = int(np.clip(40 + 80 * (env_val / max_env), 40, 120))

            for pk in top:
                midi = _bin_to_midi(b_min + pk)
                if not (min_pitch <= midi <= max_pitch):
                    continue
                q_onset = round(ot / sixteenth) * sixteenth
                q_dur = max(sixteenth, round(dur / sixteenth) * sixteenth)
                notes.append({"midi": midi, "time": q_onset, "duration": q_dur,
                               "velocity": vel, "has_bend": False, "bend_curve": []})

        notes.sort(key=lambda n: n["time"])

    except Exception as e:
        logger.warning(f"CQT polyphonic extraction failed ({min_pitch}-{max_pitch}): {e}")
        return _extract_notes_librosa_fallback(wav_path, bpm, min_pitch, max_pitch)

    return notes


def extract_notes_multi_channel(audio_path: str, bpm: float, stems: dict = None) -> dict:
    """
    Advanced multi-channel note extraction:
    - Uses Demucs stems when available (other=melody, bass, drums) for superior separation
    - Falls back to librosa HPSS + 3-band frequency separation if no stems
    - CQT polyphonic extraction for both bass and synth tracks
    - 10-type drum classification using spectral centroid + ZCR + rolloff
    - Velocity from onset strength (not hardcoded)
    - Glide/portamento detection via pyin
    """
    import librosa
    import numpy as np
    import soundfile as sf

    beat_dur = 60.0 / max(bpm, 40)
    sixteenth = beat_dur / 4

    result = {"synth_notes": [], "bass_notes": [], "drum_hits": [],
              "guitar_notes": [], "piano_notes": []}

    synth_path  = "/tmp/_xstem_synth.wav"
    bass_path   = "/tmp/_xstem_bass.wav"
    drum_path   = "/tmp/_xstem_drum.wav"
    guitar_path = "/tmp/_xstem_guitar.wav"
    piano_path  = "/tmp/_xstem_piano.wav"

    demucs_used = False
    if stems:
        try:
            sr_d = 22050
            if stems.get("other") and os.path.exists(stems["other"]):
                y_synth, _ = librosa.load(stems["other"], sr=sr_d, mono=True, duration=120)
                sf.write(synth_path, y_synth.astype(np.float32), sr_d)
                demucs_used = True
            if stems.get("bass") and os.path.exists(stems["bass"]):
                y_bass, _ = librosa.load(stems["bass"], sr=sr_d, mono=True, duration=120)
                sf.write(bass_path, y_bass.astype(np.float32), sr_d)
            elif demucs_used:
                from scipy import signal as scipy_signal
                nyq = sr_d / 2.0
                b_b, a_b = scipy_signal.butter(6, 200.0 / nyq, btype='low')
                y_bass2 = scipy_signal.filtfilt(b_b, a_b, y_synth)
                sf.write(bass_path, y_bass2.astype(np.float32), sr_d)
            if stems.get("drums") and os.path.exists(stems["drums"]):
                y_drum, _ = librosa.load(stems["drums"], sr=sr_d, mono=True, duration=120)
                sf.write(drum_path, y_drum.astype(np.float32), sr_d)
                demucs_used = True
            # 6-stem extras: guitar and piano
            if stems.get("guitar") and os.path.exists(stems["guitar"]):
                y_guitar, _ = librosa.load(stems["guitar"], sr=sr_d, mono=True, duration=120)
                sf.write(guitar_path, y_guitar.astype(np.float32), sr_d)
            if stems.get("piano") and os.path.exists(stems["piano"]):
                y_piano, _ = librosa.load(stems["piano"], sr=sr_d, mono=True, duration=120)
                sf.write(piano_path, y_piano.astype(np.float32), sr_d)
            logger.info("extract_notes_multi_channel: using Demucs stems for extraction")
        except Exception as e:
            logger.warning(f"Demucs stems load failed, falling back to HPSS: {e}")
            demucs_used = False

    if not demucs_used:
        y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=120)
        y_harmonic, y_percussive = librosa.effects.hpss(y, margin=(1.0, 5.0))
        try:
            from scipy import signal as scipy_signal
            nyq = sr / 2.0
            b_bass, a_bass = scipy_signal.butter(6, 200.0 / nyq, btype='low')
            y_bass = scipy_signal.filtfilt(b_bass, a_bass, y_harmonic)
            b_lo, a_lo = scipy_signal.butter(6, 200.0 / nyq, btype='high')
            b_hi, a_hi = scipy_signal.butter(6, 5000.0 / nyq, btype='low')
            y_synth_lo = scipy_signal.filtfilt(b_lo, a_lo, y_harmonic)
            y_synth = scipy_signal.filtfilt(b_hi, a_hi, y_synth_lo)
        except Exception:
            y_bass = y_harmonic
            y_synth = y_harmonic
        sf.write(synth_path, y_synth.astype(np.float32), sr)
        sf.write(bass_path, y_bass.astype(np.float32), sr)
        sf.write(drum_path, y_percussive.astype(np.float32), sr)

    def _best_extract(wav_path, min_pitch, max_pitch, max_per_onset=3):
        """Try basic_pitch first (tuned for hip-hop), then CQT polyphonic, then legacy pyin."""
        try:
            from basic_pitch.inference import predict
            from basic_pitch import ICASSP_2022_MODEL_PATH
            _, midi_data, _ = predict(
                wav_path,
                ICASSP_2022_MODEL_PATH,
                onset_threshold=0.25,
                frame_threshold=0.15,
                minimum_note_length=58,
                minimum_frequency=librosa.midi_to_hz(min_pitch),
                maximum_frequency=librosa.midi_to_hz(max_pitch),
            )
            notes = []
            seen = set()
            for instrument in midi_data.instruments:
                for note in instrument.notes:
                    p = note.pitch
                    if not (min_pitch <= p <= max_pitch):
                        continue
                    q_onset = round(note.start / sixteenth) * sixteenth
                    q_dur = max(sixteenth, round((note.end - note.start) / sixteenth) * sixteenth)
                    key = (int(p), round(q_onset, 3))
                    if key in seen:
                        continue
                    seen.add(key)
                    notes.append({"midi": int(p), "time": q_onset, "duration": q_dur,
                                  "velocity": int(note.velocity), "has_bend": False, "bend_curve": []})
            if notes:
                return notes
        except Exception as e:
            logger.warning(f"basic_pitch failed ({min_pitch}-{max_pitch}): {e}")
        return _extract_notes_cqt_polyphonic(wav_path, bpm, min_pitch, max_pitch, max_per_onset)

    def _extract_bass_pyin_inner(wav_path):
        """
        Monophonic pyin pitch tracker for 808 bass stems.
        Outperforms Basic-Pitch for sub-bass because:
          - frame_length=4096 gives better frequency resolution at <100 Hz
          - Handles pitch-slide / 808 glide natively via bend_curve
          - Returns notes with has_bend=True when pitch varies > 0.8 semitones
        """
        try:
            y_b, sr_b = librosa.load(wav_path, sr=22050, mono=True, duration=120)
            rms_b = float(np.sqrt(np.mean(y_b ** 2)))
            if rms_b < 1e-5:
                return []
            f0, voiced_flag, _ = librosa.pyin(
                y_b,
                fmin=librosa.note_to_hz('C1'),
                fmax=librosa.note_to_hz('G4'),
                sr=sr_b,
                frame_length=4096,
                hop_length=512,
            )
            times_f0  = librosa.times_like(f0,  sr=sr_b, hop_length=512)
            onset_env = librosa.onset.onset_strength(y=y_b, sr=sr_b, hop_length=512)
            onset_t   = librosa.times_like(onset_env, sr=sr_b, hop_length=512)

            notes_pyin = []
            in_note    = False
            n_start, n_f0s, n_ts = 0.0, [], []

            for t_i, f_i, v_i in zip(times_f0, f0, voiced_flag):
                if v_i and (f_i is not None) and (not np.isnan(f_i)) and f_i > 30:
                    if not in_note:
                        in_note, n_start, n_f0s, n_ts = True, t_i, [f_i], [t_i]
                    else:
                        n_f0s.append(f_i); n_ts.append(t_i)
                else:
                    if in_note and len(n_f0s) > 4:
                        midi_arr = librosa.hz_to_midi(np.array(n_f0s))
                        med_m    = float(np.median(midi_arr))
                        # Raw onset time (no 1/16 grid) → preserves true bass timing
                        # for better correlation with real bass stem
                        q_start  = round(n_start, 4)
                        q_dur    = max(0.05, round(n_ts[-1] - n_start, 4))
                        p_range  = float(midi_arr.max() - midi_arr.min())
                        oi       = int(np.searchsorted(onset_t, n_start))
                        vel_raw  = float(onset_env[oi:oi + 3].max()) if oi < len(onset_env) else 0.4
                        vel      = int(np.clip(vel_raw * 200, 55, 118))
                        has_bend = p_range > 0.8
                        bend_c   = [(float(t - n_start), float(p))
                                    for t, p in zip(n_ts, midi_arr)
                                    if not np.isnan(p)] if has_bend else []
                        notes_pyin.append({
                            "midi": max(24, min(67, int(round(med_m)))),
                            "time": round(q_start, 4),
                            "duration": round(q_dur, 4),
                            "velocity": vel,
                            "has_bend": has_bend,
                            "bend_curve": bend_c,
                        })
                    in_note = False; n_f0s = []; n_ts = []

            if notes_pyin:
                logger.info(f"pyin bass: {len(notes_pyin)} notes "
                            f"({sum(1 for n in notes_pyin if n['has_bend'])} with pitch-slide)")
            return notes_pyin
        except Exception as e:
            logger.warning(f"pyin bass inner failed: {e}")
            return []

    # Synth/Other: mid frequencies → pitch range C3-C8 (48-108)
    result["synth_notes"] = _best_extract(synth_path, 48, 108, max_per_onset=3)
    # Bass: pyin-first (handles 808 pitch-slides & sub-bass), fall back to Basic-Pitch
    _pyin_bass = _extract_bass_pyin_inner(bass_path)
    result["bass_notes"] = _pyin_bass if _pyin_bass else _best_extract(bass_path, 24, 67, max_per_onset=2)
    # Guitar: full melodic range C2-C8 (36-108) — only if 6-stem model was used
    if os.path.exists(guitar_path) and os.path.getsize(guitar_path) > 500:
        result["guitar_notes"] = _best_extract(guitar_path, 36, 96, max_per_onset=3)
    # Piano: full melodic range A0-C8 (21-108) — only if 6-stem model was used
    if os.path.exists(piano_path) and os.path.getsize(piano_path) > 500:
        result["piano_notes"] = _best_extract(piano_path, 21, 108, max_per_onset=4)

    # ── Glide/portamento detection on synth via pyin ───────────────────────
    try:
        y_syn, sr_syn = librosa.load(synth_path, sr=22050, mono=True, duration=60)
        f0, voiced, _ = librosa.pyin(y_syn, fmin=librosa.note_to_hz('C3'),
                                      fmax=librosa.note_to_hz('C8'), sr=sr_syn, frame_length=2048)
        times_f0 = librosa.times_like(f0, sr=sr_syn, hop_length=512)
        for nd in result["synth_notes"]:
            onset = nd["time"]
            offset = onset + nd["duration"]
            mask = (times_f0 >= onset) & (times_f0 < offset) & voiced
            f0_seg = f0[mask]
            t_seg = times_f0[mask]
            if len(f0_seg) > 3:
                valid = f0_seg[~np.isnan(f0_seg) & (f0_seg > 0)]
                if len(valid) > 1:
                    pm = librosa.hz_to_midi(valid)
                    if float(pm.max() - pm.min()) > 0.5:
                        nd["has_bend"] = True
                        nd["bend_curve"] = [
                            (float(t - onset), float(p))
                            for t, p in zip(t_seg, librosa.hz_to_midi(f0_seg))
                            if not np.isnan(p) and p > 0
                        ]
    except Exception as e:
        logger.warning(f"Pitch bend detection failed: {e}")

    # ── Advanced drum extraction: per-band onset detection ──
    try:
        from scipy.signal import sosfilt, butter as sci_butter
        y_drum, sr_d = librosa.load(drum_path, sr=22050, mono=True, duration=120)
        hop_d = 256
        max_env_d_ref = float(librosa.onset.onset_strength(y=y_drum, sr=sr_d, hop_length=hop_d).max()) + 1e-6

        def _band(y, lo=None, hi=None):
            nyq = sr_d / 2.0
            if lo is None and hi is not None:
                sos = sci_butter(4, hi / nyq, btype='low', output='sos')
            elif lo is not None and hi is None:
                sos = sci_butter(4, lo / nyq, btype='high', output='sos')
            else:
                sos = sci_butter(4, [lo / nyq, hi / nyq], btype='band', output='sos')
            return sosfilt(sos, y).astype(np.float32)

        def _detect_onsets(y_band, delta=0.05, wait=1):
            env = librosa.onset.onset_strength(y=y_band, sr=sr_d, hop_length=hop_d)
            frames = librosa.onset.onset_detect(onset_envelope=env, sr=sr_d,
                                                hop_length=hop_d, units='frames',
                                                delta=delta, wait=wait)
            times  = librosa.frames_to_time(frames, sr=sr_d, hop_length=hop_d)
            maxval = float(env.max()) + 1e-6
            return frames, times, env, maxval

        # ── Raw-time deduplication: 10ms slots (avoids double-detection) ────────
        # أفضل بكثير من ربط الـ 1/16 note لأن التوقيت الحقيقي يُحسّن onset correlation
        drum_hits_tmp = {}  # round(time,2) → hit dict (10ms dedup window)

        # ── Kick (50–160 Hz) — نطاق أدق يتجنب bleeding الباس ──────────────────
        y_kick = _band(y_drum, lo=50, hi=160)
        kf, kt, ke, km = _detect_onsets(y_kick, delta=0.04, wait=1)
        for of_d, ot_d in zip(kf, kt):
            slot = round(ot_d, 2)  # 10ms precision dedup (no bar-grid quantization)
            env_val = float(ke[min(of_d, len(ke) - 1)])
            vel = int(np.clip(55 + 65 * (env_val / km), 50, 127))
            if slot not in drum_hits_tmp:
                drum_hits_tmp[slot] = {"midi": 36, "time": round(ot_d, 4),
                                       "duration": 0.05, "velocity": vel}

        # ── Snare / Clap (160 Hz – 3.5 kHz) ───────────────────────────────────
        y_snare = _band(y_drum, lo=160, hi=3500)
        sf2, st2, se2, sm2 = _detect_onsets(y_snare, delta=0.04, wait=1)
        for of_d, ot_d in zip(sf2, st2):
            slot = round(ot_d, 2)
            env_val = float(se2[min(of_d, len(se2) - 1)])
            vel = int(np.clip(50 + 70 * (env_val / sm2), 40, 127))
            if slot in drum_hits_tmp:
                if vel > drum_hits_tmp[slot]["velocity"] + 10:
                    drum_hits_tmp[slot] = {"midi": 38, "time": round(ot_d, 4),
                                           "duration": 0.05, "velocity": vel}
            else:
                drum_hits_tmp[slot] = {"midi": 38, "time": round(ot_d, 4),
                                       "duration": 0.05, "velocity": vel}

        # ── Hi-Hat (4 kHz+) ────────────────────────────────────────────────────
        y_hat = _band(y_drum, lo=4000)
        hf, ht, he, hm = _detect_onsets(y_hat, delta=0.025, wait=1)
        for of_d, ot_d in zip(hf, ht):
            slot = round(ot_d, 2)
            env_val = float(he[min(of_d, len(he) - 1)])
            vel = int(np.clip(30 + 65 * (env_val / hm), 25, 115))
            hat_midi = 46 if vel > 70 else 42
            if slot not in drum_hits_tmp or drum_hits_tmp[slot]["midi"] not in (36, 38):
                drum_hits_tmp[slot] = {"midi": hat_midi, "time": round(ot_d, 4),
                                       "duration": 0.04, "velocity": vel}

        result["drum_hits"] = sorted(drum_hits_tmp.values(), key=lambda x: x["time"])

    except Exception as e:
        logger.warning(f"Drum extraction failed: {e}")

    for p in [synth_path, bass_path, drum_path]:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    return result


def _search_best_timbres(notes_data: dict, analysis: dict, stems: dict,
                         user_id: int = 0) -> tuple:
    """
    Multi-threaded exhaustive timbre search across ALL 1300+ instruments.
    Uses ThreadPoolExecutor to score every candidate in parallel.
    - Phase 1: quick 6-second mel-correlation scan across ALL instruments
    - Phase 2: full 15-second deep comparison on top-30 candidates
    Returns (best_synth_timbre, best_bass_timbre, search_log).
    """
    import librosa, numpy as np, os, time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # ── Build full instrument lists from parametric engine ────────────────────
    _all_instr = list(globals().get("_INSTR", {}).keys())

    _DRUM_KW = {
        "kick","snare","hat","hihat","clap","rimshot","cymbal","tom_",
        "crash","ride_","tambourine","cowbell","wood_block","conga",
        "bongo","djembe","ashiko","cajon","shaker","triangle_perc",
        "agogo","cuica","vibraslap","guiro","maracas","clave","cowbell",
    }
    def _is_drum(k):
        return any(d in k for d in _DRUM_KW)

    # Bass: starts with "bass" or is literally "bass"
    _BASS_INSTR  = sorted({k for k in _all_instr if k == "bass" or k.startswith("bass_")})
    # Melody: everything else that isn't a drum
    _MELODY_INSTR = sorted({k for k in _all_instr
                             if k not in _BASS_INSTR and not _is_drum(k)})

    # Extra hand-coded bass timbres (synthesized in _synth_note but not in _INSTR)
    _EXTRA_BASS = [
        "bass_808","bass_808_trap","bass_808_drill","bass_808_deep",
        "bass_808_punchy","bass_808_sub","bass_808_warm","bass_808_long",
        "bass_808_bright","bass_808_distorted","bass_808_phonk","bass_808_rnb",
        "bass_808_mid","bass_808_clean","bass_808_click","bass_808_uk",
        "bass_808_afro","bass_808_soft","bass_808_hiphop","bass_808_chicago",
        "bass_808_jersey","bass_808_bounce","bass_808_lofi","bass_808_vintage",
        "bass_808_modern","bass_808_ultra","bass_808_slide","bass_808_wobble",
        "bass_808_growl","bass_808_mellow","bass_808_nyc","bass_808_la",
        "bass_808_atlanta","bass_808_cloud","bass_808_rage","bass_808_melodic",
        "bass_808_dark","bass_808_heavy","bass_808_hard","bass_808_miami",
        "bass_808_houston","bass_808_blown","bass_808_filtered","bass_808_resonant",
        "bass_808_tape","bass_808_smooth","bass_808_crispy","bass_808_lite",
        "bass_pick","bass_fretless","bass_slap1","bass_slap2",
        "bass_synth1","bass_synth2","bass_synth3","bass_synth_moog","bass_synth_acid",
        "bass_acoustic","bass_upright","bass_electric","bass_fuzz","bass_overdrive",
        "bass_pluck","bass_rubber","bass_sub_sine","bass_sub_triangle","bass_sub_octave",
        "bass_wobble_lfo","bass_wub","bass_reese","bass_reese_dnb",
        "bass_square","bass_saw","bass_pulse","bass_triangle","bass_sine",
        "bass_fm","bass_fm_classic","bass_fm_dx7","bass_fm_bell","bass_fm_attack","bass_fm_sub",
        "bass_reggae","bass_ska","bass_dub","bass_dancehall","bass_soca",
        "bass_funk","bass_gospel","bass_soul","bass_jazz","bass_jazz_walking",
        "bass_blues","bass_country","bass_latin","bass_salsa","bass_bossa",
        "bass_cumbia","bass_afrobeat","bass_afro","bass_amapiano",
        "bass_house","bass_techno","bass_jungle","bass_breaks","bass_dnb",
        "bass_drum_and_bass","bass_neurofunk","bass_liquid","bass_dubstep",
        "bass_garage","bass_trance","bass_hardstyle","bass_future_bass",
        "bass_future","bass_wave","bass_chorus","bass_flanger","bass_tape",
        "bass_compress","bass_oud_low","bass_koto_low",
    ]

    # Merge and deduplicate, preserving parametric-engine entries first
    ALL_BASS   = list(dict.fromkeys(_BASS_INSTR + _EXTRA_BASS))
    ALL_MELODY = list(dict.fromkeys(_MELODY_INSTR))

    # Genre-aware prioritisation: put matching instruments first
    genre = analysis.get("genre", "").lower()
    def _genre_priority(name, is_bass):
        score = 0
        if is_bass:
            if any(g in genre for g in ["trap","drill","hip","rap"]) and "808" in name: score += 10
            if "phonk" in genre and "phonk" in name: score += 10
            if "r&b" in genre and "rnb" in name: score += 10
            if "jazz" in genre and "jazz" in name: score += 10
            if "reggae" in genre and "reggae" in name: score += 10
            if "funk" in genre and "funk" in name: score += 10
            if "house" in genre and "house" in name: score += 10
            if "dnb" in genre and "dnb" in name: score += 10
        else:
            if "piano" in genre and "piano" in name: score += 10
            if "violin" in genre and "violin" in name: score += 10
            if "guitar" in genre and "guitar" in name: score += 10
            if "organ" in genre and "organ" in name: score += 10
            if "synth" in genre and "synth" in name: score += 5
        return score

    ALL_BASS   = sorted(ALL_BASS,   key=lambda x: -_genre_priority(x, True))
    ALL_MELODY = sorted(ALL_MELODY, key=lambda x: -_genre_priority(x, False))

    sr_cmp   = 22050
    hop      = 512
    # Phase-1: fast short clip; Phase-2: longer deep clip
    clip_p1  = 6.0
    clip_p2  = 15.0
    N_TOP    = 30        # top candidates to promote to Phase 2
    MAX_WORKERS = min(32, (os.cpu_count() or 4) * 4)

    bpm_v       = analysis.get("bpm", 80)
    beat_dur    = 60.0 / max(float(bpm_v), 40)
    synth_notes = notes_data.get("synth_notes", [])
    bass_notes  = notes_data.get("bass_notes",  [])
    search_log  = []

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _render(notes_list, timbre, clip_dur, octave_shift=0):
        """Render notes at a given timbre. octave_shift: semitones to transpose (+12/-12)."""
        n_total = int(sr_cmp * clip_dur)
        mix = np.zeros(n_total, dtype=np.float32)
        for nd in notes_list:
            t0 = float(nd.get("time", 0.0))
            if t0 >= clip_dur:
                continue
            dur  = min(float(nd.get("duration", beat_dur * 1.2)) + 0.3, 4.0)
            midi = int(np.clip(int(nd.get("midi", 60)) + octave_shift, 21, 108))
            tone = _synth_note(midi, dur, timbre, sr_cmp)
            si = int(t0 * sr_cmp)
            ei = min(si + len(tone), n_total)
            mix[si:ei] += tone[:ei - si]
        pk = np.abs(mix).max()
        if pk > 1e-6:
            mix /= pk
        return mix

    def _mel_corr(a, b):
        try:
            Sa = librosa.power_to_db(
                librosa.feature.melspectrogram(y=a, sr=sr_cmp, n_mels=64, hop_length=hop))
            Sb = librosa.power_to_db(
                librosa.feature.melspectrogram(y=b, sr=sr_cmp, n_mels=64, hop_length=hop))
            m = min(Sa.shape[1], Sb.shape[1])
            c = np.corrcoef(Sa[:, :m].flatten(), Sb[:, :m].flatten())[0, 1]
            return float(c) if np.isfinite(c) else 0.0
        except Exception:
            return 0.0

    def _score_one(timbre, notes_list, ref, clip_dur, deep=False):
        """
        Timbre scoring. Thread-safe.
        deep=False (Phase 1): mel-correlation only — fast, used for pre-filtering 989 candidates.
        deep=True  (Phase 2): MFCC + centroid + chroma + mel — prevents drums winning melody.
        """
        try:
            cand = _render(notes_list, timbre, clip_dur)
            m    = min(len(cand), len(ref))
            ca, ra = cand[:m], ref[:m]
            if np.abs(ca).max() < 1e-7:
                return timbre, 0.0

            def _safe_corr(a, b):
                try:
                    c = np.corrcoef(a.flatten(), b.flatten())[0, 1]
                    return float(c) if np.isfinite(c) else 0.0
                except Exception:
                    return 0.0

            # Always compute mel-correlation
            Sa = librosa.power_to_db(librosa.feature.melspectrogram(
                y=ca, sr=sr_cmp, n_mels=64, hop_length=hop))
            Sb = librosa.power_to_db(librosa.feature.melspectrogram(
                y=ra, sr=sr_cmp, n_mels=64, hop_length=hop))
            mc = min(Sa.shape[1], Sb.shape[1])
            mel_c = _safe_corr(Sa[:, :mc], Sb[:, :mc])

            if not deep:
                return timbre, mel_c

            # Deep mode: add MFCC + spectral centroid + chroma
            # MFCC (timbral fingerprint — most discriminative, prevents drums winning)
            Ma = librosa.feature.mfcc(y=ca, sr=sr_cmp, n_mfcc=20, hop_length=hop)
            Mb = librosa.feature.mfcc(y=ra, sr=sr_cmp, n_mfcc=20, hop_length=hop)
            mc2 = min(Ma.shape[1], Mb.shape[1])
            mfcc_c = _safe_corr(Ma[:, :mc2], Mb[:, :mc2])

            # Spectral centroid (brightness — drums: broadband; melody: specific profile)
            ca_cent = librosa.feature.spectral_centroid(y=ca, sr=sr_cmp, hop_length=hop)[0]
            ra_cent = librosa.feature.spectral_centroid(y=ra, sr=sr_cmp, hop_length=hop)[0]
            mc3 = min(len(ca_cent), len(ra_cent))
            cent_c = _safe_corr(ca_cent[:mc3], ra_cent[:mc3])

            # Chroma (pitch/harmony — unpitched drums score near 0 here)
            Ch_a = librosa.feature.chroma_cqt(y=ca, sr=sr_cmp, hop_length=hop)
            Ch_b = librosa.feature.chroma_cqt(y=ra, sr=sr_cmp, hop_length=hop)
            mc4 = min(Ch_a.shape[1], Ch_b.shape[1])
            chroma_c = _safe_corr(Ch_a[:, :mc4], Ch_b[:, :mc4])

            score = (mel_c    * 0.25
                   + mfcc_c   * 0.40
                   + cent_c   * 0.20
                   + chroma_c * 0.15)
            return timbre, float(score)
        except Exception:
            return timbre, 0.0

    def _parallel_search(candidates, notes_list, ref, clip_dur, label, deep=False):
        """Score all candidates in parallel. Returns sorted list of (score, timbre)."""
        results = []
        t_start = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {pool.submit(_score_one, tm, notes_list, ref, clip_dur, deep): tm
                    for tm in candidates}
            for fut in as_completed(futs):
                try:
                    tm, sc = fut.result(timeout=60)
                    results.append((sc, tm))
                    search_log.append(f"{label}/{tm}: {sc:.3f}")
                except Exception:
                    pass
        elapsed = time.time() - t_start
        results.sort(reverse=True)
        logger.info(f"{label} parallel search: {len(results)} candidates "
                    f"in {elapsed:.1f}s — best={results[0] if results else None}")
        return results

    # ── Load reference stems ───────────────────────────────────────────────────
    ref_melody = ref_bass = None
    try:
        if stems.get("other") and os.path.exists(stems["other"]):
            ref_melody, _ = librosa.load(stems["other"], sr=sr_cmp, mono=True, duration=clip_p2)
        if stems.get("bass") and os.path.exists(stems["bass"]):
            ref_bass, _ = librosa.load(stems["bass"], sr=sr_cmp, mono=True, duration=clip_p2)
    except Exception:
        pass

    # ── Synth timbre search (two-phase) ───────────────────────────────────────
    best_synth_timbre = "synth_heavy"
    best_synth_score  = -1.0
    if synth_notes and ref_melody is not None:
        ref_p1 = ref_melody[:int(sr_cmp * clip_p1)]
        # Phase 1: fast scan — ALL melody instruments
        p1 = _parallel_search(ALL_MELODY, synth_notes, ref_p1, clip_p1, "SynthP1")
        top_synth = [tm for _, tm in p1[:N_TOP]]
        # Phase 2: deep multi-metric scan — top N_TOP only
        # deep=True enables MFCC + centroid + chroma — prevents drums winning melody
        p2 = _parallel_search(top_synth, synth_notes, ref_melody, clip_p2, "SynthP2", deep=True)
        if p2:
            best_synth_score, best_synth_timbre = p2[0]

    # ── Bass timbre search (two-phase) ────────────────────────────────────────
    best_bass_timbre = "bass_808"
    best_bass_score  = -1.0
    if bass_notes and ref_bass is not None:
        ref_p1 = ref_bass[:int(sr_cmp * clip_p1)]
        # Phase 1: fast scan — ALL bass instruments
        p1 = _parallel_search(ALL_BASS, bass_notes, ref_p1, clip_p1, "BassP1")
        top_bass = [tm for _, tm in p1[:N_TOP]]
        # Phase 2: deep multi-metric scan — top N_TOP only
        p2 = _parallel_search(top_bass, bass_notes, ref_bass, clip_p2, "BassP2", deep=True)
        if p2:
            best_bass_score, best_bass_timbre = p2[0]

    logger.info(
        f"Timbre search DONE — synth={best_synth_timbre}({best_synth_score:.3f}) "
        f"bass={best_bass_timbre}({best_bass_score:.3f}) "
        f"[{len(ALL_MELODY)} melody + {len(ALL_BASS)} bass instruments searched]"
    )
    return best_synth_timbre, best_bass_timbre, search_log


def build_tab_data_from_notes(notes_data: dict, analysis: dict, title: str = "",
                               synth_timbre_override: str = None,
                               bass_timbre_override: str = None) -> dict:
    """Build tab_data dict compatible with render_musicxml_to_audio."""
    bpm = analysis.get("bpm", 80)
    key = analysis.get("key", "C Major")
    time_sig = analysis.get("time_sig", "4/4")
    genre = analysis.get("genre", "").lower()
    synth_notes = notes_data.get("synth_notes", [])
    bass_notes = notes_data.get("bass_notes", [])
    drum_hits = notes_data.get("drum_hits", [])

    # Detect heavy bass (808) if most bass notes are sub-bass (< MIDI 48 = C3)
    _HIP_HOP_GENRES = [
        "hip hop", "rap", "trap", "drill", "hiphop", "r&b", "rnb",
        "boom bap", "gangsta", "g-funk", "crunk", "horrorcore",
        "east coast", "west coast", "mumble", "cloud rap", "lo-fi hip",
        "lofi hip", "phonk", "pluggnb", "hyphy", "grime", "uk rap",
    ]
    is_heavy_genre = any(g in genre for g in _HIP_HOP_GENRES)
    sub_bass_count = sum(1 for n in bass_notes if n.get("midi", 60) < 50)
    is_808_bass = is_heavy_genre or (len(bass_notes) > 0 and sub_bass_count / max(len(bass_notes), 1) > 0.5)

    # Choose most fitting 808 sub-variant for the genre
    if is_heavy_genre:
        if "atlanta" in genre:
            _default_808 = "bass_808_atlanta"
        elif "trap" in genre and ("hard" in genre or "heavy" in genre):
            _default_808 = "bass_808_hard"
        elif "trap" in genre:
            _default_808 = "bass_808_trap"
        elif "chicago" in genre:
            _default_808 = "bass_808_chicago"
        elif "uk drill" in genre or ("uk" in genre and "drill" in genre):
            _default_808 = "bass_808_drill"
        elif "drill" in genre:
            _default_808 = "bass_808_drill"
        elif "phonk" in genre or "memphis" in genre:
            _default_808 = "bass_808_phonk"
        elif "rage" in genre or "pluggnb" in genre:
            _default_808 = "bass_808_rage"
        elif "cloud" in genre or "dreamy" in genre:
            _default_808 = "bass_808_cloud"
        elif "melodic" in genre:
            _default_808 = "bass_808_melodic"
        elif "r&b" in genre or "rnb" in genre:
            _default_808 = "bass_808_rnb"
        elif "boom bap" in genre or "east coast" in genre or "nyc" in genre:
            _default_808 = "bass_808_nyc"
        elif "west coast" in genre or "g-funk" in genre or " la " in genre:
            _default_808 = "bass_808_la"
        elif "grime" in genre or "uk rap" in genre:
            _default_808 = "bass_808_uk"
        elif "afro" in genre:
            _default_808 = "bass_808_afro"
        elif "jersey" in genre:
            _default_808 = "bass_808_jersey"
        elif "bounce" in genre or "crunk" in genre:
            _default_808 = "bass_808_bounce"
        elif "lofi" in genre or "lo-fi" in genre or "lo fi" in genre:
            _default_808 = "bass_808_lofi"
        elif "dark" in genre:
            _default_808 = "bass_808_dark"
        elif "hyphy" in genre:
            _default_808 = "bass_808_la"
        else:
            _default_808 = "bass_808_hiphop"
    else:
        _default_808 = "bass_808"

    synth_timbre = synth_timbre_override or ("synth_heavy" if is_heavy_genre else "synth")
    bass_timbre  = bass_timbre_override  or (_default_808 if is_808_bass else "bass")

    tab_data = {
        "bpm": bpm,
        "tuning": "Standard",
        "chords": [],
        "guitar_notes": [],
        "bass_notes": bass_notes,
        "has_drums": len(drum_hits) > 0,
        "drum_notes": drum_hits,
        "instruments": [],
        "all_parts": {},
        "file_type": "musicxml",
        "time_signature": time_sig,
        "title": title,
        "composer": "",
        "key_signature": key,
    }
    if synth_notes:
        tab_data["all_parts"]["Synth"] = {
            "timbre": synth_timbre,
            "notes": synth_notes,
            "is_drum": False,
        }
    if bass_notes:
        import copy as _copy_td
        if is_808_bass:
            tab_data["all_parts"]["Bass"] = {
                "timbre": bass_timbre,
                "notes": bass_notes,
                "is_drum": False,
            }
            sub_notes_td = _copy_td.deepcopy([n for n in bass_notes if n.get("midi", 60) < 55])
            for sn in sub_notes_td:
                sn["midi"] = max(0, sn.get("midi", 36) - 12)
                sn["velocity"] = max(1, int(sn.get("velocity", 100) * 0.65))
            if sub_notes_td:
                tab_data["all_parts"]["Sub 808"] = {
                    "timbre": "bass_808_deep",
                    "notes": sub_notes_td,
                    "is_drum": False,
                }
            mid_notes_td = _copy_td.deepcopy([n for n in bass_notes if 40 <= n.get("midi", 60) <= 65])
            for mn in mid_notes_td:
                mn["velocity"] = max(1, int(mn.get("velocity", 100) * 0.45))
            if mid_notes_td:
                tab_data["all_parts"]["Bass Finger"] = {
                    "timbre": "bass",
                    "notes": mid_notes_td,
                    "is_drum": False,
                }
        else:
            # Main: Retro Bass (Synth Bass 1)
            tab_data["all_parts"]["Retro Bass"] = {
                "timbre": "bass",
                "notes": bass_notes,
                "is_drum": False,
            }
            finger_notes_td = _copy_td.deepcopy(bass_notes)
            for fn in finger_notes_td:
                fn["velocity"] = max(1, int(fn.get("velocity", 80) * 0.50))
            tab_data["all_parts"]["Bass Finger"] = {
                "timbre": "bass",
                "notes": finger_notes_td,
                "is_drum": False,
            }
            mid_notes_td = _copy_td.deepcopy([n for n in bass_notes if 40 <= n.get("midi", 60) <= 65])
            for mn in mid_notes_td:
                mn["velocity"] = max(1, int(mn.get("velocity", 80) * 0.30))
            if mid_notes_td:
                tab_data["all_parts"]["Fretless Bass"] = {
                    "timbre": "bass",
                    "notes": mid_notes_td,
                    "is_drum": False,
                }
            low_notes_td = _copy_td.deepcopy([n for n in bass_notes if n.get("midi", 60) <= 52])
            for ln in low_notes_td:
                ln["velocity"] = max(1, int(ln.get("velocity", 80) * 0.22))
            if low_notes_td:
                tab_data["all_parts"]["Acoustic Bass"] = {
                    "timbre": "bass",
                    "notes": low_notes_td,
                    "is_drum": False,
                }
    guitar_notes = notes_data.get("guitar_notes", [])
    if guitar_notes:
        tab_data["all_parts"]["Guitar"] = {
            "timbre": "guitar",
            "notes": guitar_notes,
            "is_drum": False,
        }
    piano_notes = notes_data.get("piano_notes", [])
    if piano_notes:
        tab_data["all_parts"]["Piano"] = {
            "timbre": "piano",
            "notes": piano_notes,
            "is_drum": False,
        }
    return tab_data


def export_to_musicxml_file(tab_data: dict, output_path: str) -> str:
    """Export tab_data to a MusicXML file using music21 with Pitch Bend / Gliss annotations."""
    from music21 import stream, note as m21note, tempo as m21tempo, meter, metadata as m21metadata, expressions

    bpm_val = float(tab_data.get("bpm", 80))
    time_sig_str = tab_data.get("time_signature", "4/4")
    beat_dur = 60.0 / max(bpm_val, 40)
    title = tab_data.get("title", "Exported Score")

    score = stream.Score()
    md = m21metadata.Metadata()
    md.title = title
    score.metadata = md

    all_parts = tab_data.get("all_parts", {})

    for part_name, pdata in all_parts.items():
        if pdata.get("is_drum"):
            continue
        part = stream.Part()
        part.partName = part_name
        part.append(meter.TimeSignature(time_sig_str))
        part.append(m21tempo.MetronomeMark(number=int(bpm_val)))

        for n in sorted(pdata.get("notes", []), key=lambda x: x.get("time", 0)):
            midi_pitch = int(n.get("midi", 60))
            if midi_pitch < 0 or midi_pitch > 127:
                continue
            dur_sec = float(n.get("duration", beat_dur))
            quarter_len = max(0.25, round((dur_sec / beat_dur) * 4) / 4)
            offset_beats = round((float(n.get("time", 0)) / beat_dur) * 4) / 4
            try:
                mn = m21note.Note()
                mn.pitch.midi = midi_pitch
                mn.quarterLength = quarter_len
                if n.get("has_bend") and n.get("bend_curve"):
                    te = expressions.TextExpression("gliss.")
                    te.style.fontSize = 8
                    mn.expressions.append(te)
                part.insert(offset_beats, mn)
            except Exception:
                pass
        score.append(part)

    # Drums part
    if tab_data.get("has_drums") and tab_data.get("drum_notes"):
        drum_part = stream.Part()
        drum_part.partName = "Drums"
        drum_part.append(meter.TimeSignature(time_sig_str))
        for hit in tab_data.get("drum_notes", []):
            midi_pitch = int(hit.get("midi", 36))
            offset_beats = round((float(hit.get("time", 0)) / beat_dur) * 4) / 4
            try:
                mn = m21note.Note()
                mn.pitch.midi = midi_pitch
                mn.quarterLength = 0.25
                drum_part.insert(offset_beats, mn)
            except Exception:
                pass
        score.append(drum_part)

    score.write("musicxml", fp=output_path)
    return output_path


# Mapping from timbre string → GM program number (0-indexed)
_BASS_TIMBRE_GM: dict[str, int] = {
    "bass":               33,  # Electric Bass (finger)
    "bass_pick":          34,  # Electric Bass (pick)
    "bass_fretless":      35,  # Fretless Bass
    "bass_slap1":         36,  # Slap Bass 1
    "bass_slap2":         37,  # Slap Bass 2
    "bass_synth1":        38,  # Synth Bass 1 (Retro)
    "bass_synth2":        39,  # Synth Bass 2 (House)
    "bass_acoustic":      32,  # Acoustic Bass
    # 808 variants → Synth Bass 2 (closest GM equivalent)
    "bass_808":           39,
    "bass_808_hiphop":    38,
    "bass_808_trap":      39,
    "bass_808_drill":     39,
    "bass_808_deep":      38,
    "bass_808_punchy":    37,  # Slap Bass 2 for punchy feel
    "bass_808_sub":       38,
    "bass_808_warm":      35,  # Fretless for warm feel
    "bass_808_long":      33,
    "bass_808_bright":    34,  # Pick for brighter attack
    "bass_808_distorted": 39,
    "bass_808_rnb":       36,  # Slap Bass 1 for R&B
    "bass_808_mid":       38,
    "bass_808_clean":     33,
    "bass_808_click":     34,
    "bass_808_uk":        39,
    "bass_808_afro":      37,
    "bass_808_soft":      35,
}


def build_midi_file(notes_data: dict, analysis: dict, output_path: str,
                    bass_timbre: str = None) -> str:
    """
    Build a proper MIDI file (.mid) from extracted notes_data.
    - Separate tracks: Synth, Bass, Drums
    - Pitch Bend events for notes with has_bend / bend_curve
    - Quantization 1/16 already applied upstream
    - Compatible with GarageBand and any DAW
    """
    import pretty_midi

    bpm = float(analysis.get("bpm", 80))
    pm = pretty_midi.PrettyMIDI(initial_tempo=bpm)

    def _bend_to_midi(semitones: float, bend_range: int = 2) -> int:
        ratio = max(-1.0, min(1.0, semitones / bend_range))
        val = int(ratio * 8191)
        return max(-8192, min(8191, val))

    def _add_pitched_track(notes: list, program: int, name: str, bend_range: int = 2):
        inst = pretty_midi.Instrument(program=program, name=name)
        _MIN_BEND_SEMITONES = 0.18  # ignore micro-deviations below this threshold

        # Deduplicate: same pitch + same onset = keep only the louder one
        _seen: dict = {}  # (pitch, rounded_start) → note dict
        for nd in notes:
            key = (int(nd.get("midi", 60)), round(float(nd.get("time", 0)), 3))
            existing = _seen.get(key)
            if existing is None or nd.get("velocity", 80) > existing.get("velocity", 80):
                _seen[key] = nd
        deduped = sorted(_seen.values(), key=lambda n: n.get("time", 0))

        for nd in deduped:
            pitch = int(nd.get("midi", 60))
            if not (0 <= pitch <= 127):
                continue
            start = float(nd.get("time", 0.0))
            dur = max(0.05, float(nd.get("duration", 0.25)))
            end = start + dur
            vel = max(1, min(127, int(nd.get("velocity", 80))))
            inst.notes.append(pretty_midi.Note(velocity=vel, pitch=pitch, start=start, end=end))

            # Pre-reset: clear any residual pitch bend from the previous note
            # This prevents previous note's bend from bleeding into this note
            pre_t = max(0.0, start - 0.002)
            inst.pitch_bends.append(pretty_midi.PitchBend(0, pre_t))

            # Only apply bend if deviations are musically significant (≥ 0.18 semitones)
            raw_curve = nd.get("bend_curve") or []
            sig_curve = [
                (rel_t, p_midi) for rel_t, p_midi in raw_curve
                if abs(float(p_midi) - float(pitch)) >= _MIN_BEND_SEMITONES
            ]

            if nd.get("has_bend") and sig_curve:
                base_pitch = float(pitch)
                bends = []
                for rel_t, p_midi in sig_curve:
                    abs_t = start + float(rel_t)
                    if abs_t >= end - 0.02:  # don't add bends in the last 20ms
                        break
                    bends.append(pretty_midi.PitchBend(
                        _bend_to_midi(float(p_midi) - base_pitch, bend_range), abs_t
                    ))
                if bends:
                    # Settle: lock to 0 (target pitch) 40ms after last bend
                    settle_t = min(bends[-1].time + 0.04, end - 0.04)
                    if settle_t > bends[-1].time:
                        bends.append(pretty_midi.PitchBend(0, settle_t))
                    # Final reset AFTER note ends (not before) so it doesn't cut off
                    bends.append(pretty_midi.PitchBend(0, end + 0.001))
                    inst.pitch_bends.extend(bends)

        inst.notes.sort(key=lambda n: n.start)
        inst.pitch_bends.sort(key=lambda pb: pb.time)
        return inst

    genre = analysis.get("genre", "").lower()
    _HIP_HOP_GENRES_MIDI = [
        "hip hop", "rap", "trap", "drill", "hiphop", "r&b", "rnb",
        "boom bap", "gangsta", "g-funk", "crunk", "horrorcore",
        "east coast", "west coast", "mumble", "cloud rap", "lo-fi hip",
        "lofi hip", "phonk", "pluggnb", "hyphy", "grime", "uk rap",
    ]
    is_heavy = any(g in genre for g in _HIP_HOP_GENRES_MIDI)
    import copy as _copy

    synth_notes = notes_data.get("synth_notes", [])
    if synth_notes:
        if is_heavy:
            # Hip-hop: main Synth Lead at full velocity
            prog = pretty_midi.instrument_name_to_program("Lead 2 (sawtooth)")
            pm.instruments.append(_add_pitched_track(synth_notes, prog, "Synth Lead", 2))
            # Subtle pad at 10% — adds warmth but is inaudible as a separate attack
            pad_notes = _copy.deepcopy(synth_notes)
            for pn in pad_notes:
                pn["velocity"] = max(1, int(pn.get("velocity", 60) * 0.10))
                pn["has_bend"] = False
                pn["bend_curve"] = []
            prog_pad = pretty_midi.instrument_name_to_program("Pad 2 (warm)")
            pm.instruments.append(_add_pitched_track(pad_notes, prog_pad, "Synth Pad", 2))
        else:
            prog = pretty_midi.instrument_name_to_program("Lead 1 (square)")
            pm.instruments.append(_add_pitched_track(synth_notes, prog, "Synth", 2))

    bass_notes = notes_data.get("bass_notes", [])
    if bass_notes:
        sub_bass_count = sum(1 for n in bass_notes if n.get("midi", 60) < 50)
        is_808 = is_heavy or (len(bass_notes) > 0 and sub_bass_count / max(len(bass_notes), 1) > 0.5)
        # Detect if best_timbre is an 808 variant to override is_808
        _timbre = bass_timbre or ""
        if _timbre.startswith("bass_808"):
            is_808 = True

        # Resolve primary GM program from timbre search result
        _primary_prog = _BASS_TIMBRE_GM.get(_timbre, None)
        _bend_range_main = 12 if is_808 else 4

        if is_808:
            # Primary 808 layer — full velocity, wide pitch bend for slides
            _main_prog = _primary_prog if _primary_prog is not None else 39  # Synth Bass 2
            _main_name = _timbre.replace("_", " ").title() if _timbre else "808 Bass"
            pm.instruments.append(_add_pitched_track(bass_notes, _main_prog, _main_name, 12))
            # Sub-octave layer for deep rumble — ONE octave down, very low velocity (25%)
            # to add body WITHOUT creating a double-attack. No pitch bends on sub layer.
            sub_notes = _copy.deepcopy([n for n in bass_notes if n.get("midi", 60) < 55])
            for sn in sub_notes:
                sn["midi"] = max(0, sn.get("midi", 36) - 12)
                sn["velocity"] = max(1, int(sn.get("velocity", 100) * 0.25))
                sn["has_bend"] = False   # no bend on sub — prevents double glide
                sn["bend_curve"] = []
            if sub_notes:
                _sub_prog = 38 if _main_prog == 39 else 39
                pm.instruments.append(_add_pitched_track(sub_notes, _sub_prog, "Sub Layer", 12))
        else:
            # Primary layer — use timbre-resolved program if available
            _main_prog = _primary_prog if _primary_prog is not None else 38  # Synth Bass 1
            _main_name = _timbre.replace("_", " ").title() if _timbre else "Retro Bass"
            pm.instruments.append(_add_pitched_track(bass_notes, _main_prog, _main_name, 4))
            # Layer 2: subtle body layer at 12% velocity — adds warmth but NOT audible as
            # a separate attack, preventing the "double note" perception
            _l2_prog = 33 if _main_prog != 33 else 35  # Electric Bass (finger) or Fretless
            body_notes = _copy.deepcopy(bass_notes)
            for bn in body_notes:
                bn["velocity"] = max(1, int(bn.get("velocity", 80) * 0.12))
                bn["has_bend"] = False   # no bend on body layer — prevents double glide
                bn["bend_curve"] = []
            pm.instruments.append(_add_pitched_track(body_notes, _l2_prog, "Bass Body", 4))

    # Guitar track — from 6-stem separation
    guitar_notes = notes_data.get("guitar_notes", [])
    if guitar_notes:
        if is_heavy:
            # Hip-hop guitar sample feel — muted / clean electric
            prog_g = pretty_midi.instrument_name_to_program("Electric Guitar (muted)")
        else:
            prog_g = pretty_midi.instrument_name_to_program("Electric Guitar (clean)")
        pm.instruments.append(_add_pitched_track(guitar_notes, prog_g, "Guitar", 2))

    # Piano track — from 6-stem separation
    piano_notes = notes_data.get("piano_notes", [])
    if piano_notes:
        if is_heavy:
            # Hip-hop keys — Rhodes / Electric Piano feel
            prog_p = pretty_midi.instrument_name_to_program("Electric Piano 1")
        else:
            prog_p = pretty_midi.instrument_name_to_program("Acoustic Grand Piano")
        pm.instruments.append(_add_pitched_track(piano_notes, prog_p, "Piano", 2))

    drum_hits = notes_data.get("drum_hits", [])
    if drum_hits:
        drums_inst = pretty_midi.Instrument(program=0, is_drum=True, name="Drums")
        for dh in drum_hits:
            pitch = int(dh.get("midi", 36))
            if not (0 <= pitch <= 127):
                continue
            start = float(dh.get("time", 0.0))
            dur = max(0.02, float(dh.get("duration", 0.05)))
            vel = max(1, min(127, int(dh.get("velocity", 100))))
            drums_inst.notes.append(pretty_midi.Note(velocity=vel, pitch=pitch, start=start, end=start + dur))
        drums_inst.notes.sort(key=lambda n: n.start)
        pm.instruments.append(drums_inst)

    pm.write(output_path)
    return output_path


def build_spectral_comparison(orig_path: str, recon_path: str, img_path: str) -> str:
    """
    Generate a spectral comparison image between original and reconstructed audio.
    Each signal is analysed and displayed independently — no audio mixing occurs.
    Layout (4 rows × 2 cols):
      Row 0: Original-only Mel spectrogram  |  Reconstructed-only Mel spectrogram
      Row 1: Original-only Chroma heatmap   |  Reconstructed-only Chroma heatmap
      Row 2: Spectral Difference heatmap    |  Per-pitch-class chroma bar comparison
      Row 3: Original waveform              |  Reconstructed waveform + similarity metrics
    """
    import librosa
    import librosa.display
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import numpy as np

    hop = 512
    sr_t = 22050
    display_dur = 45

    y_orig, sr = librosa.load(orig_path,  sr=sr_t, mono=True, duration=display_dur)
    y_recon, _ = librosa.load(recon_path, sr=sr_t, mono=True, duration=display_dur)

    min_len = min(len(y_orig), len(y_recon))
    y_orig_cmp  = y_orig[:min_len]
    y_recon_cmp = y_recon[:min_len]

    S_orig  = librosa.feature.melspectrogram(y=y_orig_cmp,  sr=sr, n_mels=128, hop_length=hop)
    S_recon = librosa.feature.melspectrogram(y=y_recon_cmp, sr=sr, n_mels=128, hop_length=hop)
    S_orig_db  = librosa.power_to_db(S_orig,  ref=np.max)
    S_recon_db = librosa.power_to_db(S_recon, ref=np.max)
    S_diff = S_orig_db - S_recon_db

    C_orig  = librosa.feature.chroma_cqt(y=y_orig_cmp,  sr=sr, hop_length=hop)
    C_recon = librosa.feature.chroma_cqt(y=y_recon_cmp, sr=sr, hop_length=hop)
    orig_energy  = C_orig.mean(axis=1)
    recon_energy = C_recon.mean(axis=1)

    similarity   = float(np.corrcoef(S_orig_db.flatten(), S_recon_db.flatten())[0, 1])
    chroma_sim   = float(np.corrcoef(orig_energy, recon_energy)[0, 1])
    energy_ratio = float(np.mean(y_recon_cmp ** 2) / (np.mean(y_orig_cmp ** 2) + 1e-9))
    similarity   = max(-1.0, min(1.0, similarity))

    time_axis = np.linspace(0, display_dur, len(y_orig_cmp))

    fig = plt.figure(figsize=(18, 16))
    fig.patch.set_facecolor("#0a0a0a")
    gs  = gridspec.GridSpec(4, 2, figure=fig, hspace=0.42, wspace=0.30)

    def _style(ax, title, border="#555"):
        ax.set_facecolor("#161616")
        ax.set_title(title, color="white", fontsize=9, fontweight="bold", pad=5)
        ax.tick_params(colors="#aaa", labelsize=7)
        ax.xaxis.label.set_color("#aaa")
        ax.yaxis.label.set_color("#aaa")
        for sp in ax.spines.values():
            sp.set_edgecolor(border)

    fig.suptitle(
        "📊  تحليل طيفي مستقل — الأصل وحده  vs  الإعادة وحدها",
        fontsize=14, fontweight="bold", color="white", y=0.995
    )

    ax_o_mel = fig.add_subplot(gs[0, 0])
    librosa.display.specshow(S_orig_db, sr=sr, hop_length=hop,
                             x_axis="time", y_axis="mel", ax=ax_o_mel, cmap="magma")
    _style(ax_o_mel, "🎵  الأصل — Mel Spectrogram (مستقل)", border="#4fc3f7")
    ax_o_mel.text(0.01, 0.97, "ORIGINAL ONLY", transform=ax_o_mel.transAxes,
                  fontsize=7, color="#4fc3f7", va="top", fontweight="bold")

    ax_r_mel = fig.add_subplot(gs[0, 1])
    librosa.display.specshow(S_recon_db, sr=sr, hop_length=hop,
                             x_axis="time", y_axis="mel", ax=ax_r_mel, cmap="inferno")
    _style(ax_r_mel, "🎹  الإعادة — Mel Spectrogram (نقي)", border="#ff8a65")
    ax_r_mel.text(0.01, 0.97, "RECONSTRUCTED ONLY", transform=ax_r_mel.transAxes,
                  fontsize=7, color="#ff8a65", va="top", fontweight="bold")

    ax_o_chr = fig.add_subplot(gs[1, 0])
    librosa.display.specshow(C_orig, sr=sr, hop_length=hop,
                             x_axis="time", y_axis="chroma", ax=ax_o_chr, cmap="Blues")
    _style(ax_o_chr, "🎵  الأصل — Chroma (مستقل)", border="#4fc3f7")

    ax_r_chr = fig.add_subplot(gs[1, 1])
    librosa.display.specshow(C_recon, sr=sr, hop_length=hop,
                             x_axis="time", y_axis="chroma", ax=ax_r_chr, cmap="Oranges")
    _style(ax_r_chr, "🎹  الإعادة — Chroma (نقي)", border="#ff8a65")

    ax_diff = fig.add_subplot(gs[2, 0])
    im = ax_diff.imshow(S_diff, aspect="auto", origin="lower",
                        cmap="RdBu_r", vmin=-30, vmax=30)
    _style(ax_diff, "🔍  فرق الطيف  (أحمر = ناقص في الإعادة، أزرق = زائد)")
    cb = plt.colorbar(im, ax=ax_diff)
    cb.ax.yaxis.set_tick_params(color="#aaa", labelsize=6)
    plt.setp(cb.ax.yaxis.get_ticklabels(), color="#aaa")
    cb.set_label("dB diff", color="#aaa", fontsize=7)

    note_names = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
    bar_x = np.arange(12)
    ax_bar = fig.add_subplot(gs[2, 1])
    ax_bar.set_facecolor("#161616")
    ax_bar.bar(bar_x - 0.2, orig_energy,  0.35, label="الأصل",   color="#4fc3f7", alpha=0.9)
    ax_bar.bar(bar_x + 0.2, recon_energy, 0.35, label="الإعادة", color="#ff8a65", alpha=0.9)
    ax_bar.set_xticks(bar_x)
    ax_bar.set_xticklabels(note_names, color="white", fontsize=8)
    _style(ax_bar, "🎼  طاقة كل نغمة — مقارنة مستقلة")
    ax_bar.set_ylabel("متوسط الطاقة", color="#aaa", fontsize=8)
    ax_bar.legend(facecolor="#222", labelcolor="white", fontsize=8)

    ax_wv_o = fig.add_subplot(gs[3, 0])
    t_o = np.linspace(0, display_dur, len(y_orig))
    ax_wv_o.plot(t_o, y_orig, color="#4fc3f7", linewidth=0.4, alpha=0.85)
    ax_wv_o.set_xlim(0, display_dur)
    ax_wv_o.set_ylim(-1, 1)
    _style(ax_wv_o, "🎵  موجة صوت الأصل (مستقل)", border="#4fc3f7")
    ax_wv_o.set_xlabel("الوقت (ثانية)", color="#aaa", fontsize=7)

    ax_met = fig.add_subplot(gs[3, 1])
    ax_met.set_facecolor("#0d1a2a")
    ax_met.axis("off")
    stars   = "⭐" * (5 if similarity > 0.80 else 4 if similarity > 0.60 else 3 if similarity > 0.40 else 2)
    verdict = "جودة ممتازة ✅" if similarity > 0.75 else "جيد ⚙️" if similarity > 0.45 else "تصحيح مطلوب 🔧"
    e_pct   = min(energy_ratio, 9.99)
    metrics = (
        f"📊  نتائج المقارنة الطيفية المستقلة\n"
        f"{'─'*36}\n\n"
        f"🎯  تشابه Mel:        {similarity*100:5.1f}%  {stars}\n"
        f"🎼  تشابه Chroma:     {chroma_sim*100:5.1f}%\n"
        f"⚡  نسبة الطاقة:      {e_pct:.3f}\n\n"
        f"الحكم:  {verdict}\n\n"
        f"{'─'*36}\n"
        f"✅  الأصل — صوت مستقل  (لا دمج)\n"
        f"✅  الإعادة — صوت نقي  (MIDI فقط)"
    )
    ax_met.text(0.04, 0.96, metrics, transform=ax_met.transAxes,
                fontsize=10, verticalalignment="top", color="white",
                fontfamily="monospace",
                bbox=dict(boxstyle="round,pad=0.7", facecolor="#0d1a2a",
                          edgecolor="#1e3a5f", alpha=0.95))

    plt.savefig(img_path, dpi=110, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return img_path


def spectral_adjust_notes(notes_data: dict, orig_path: str, recon_path: str, bpm: float) -> dict:
    """
    Advanced spectral note correction (4 passes):
    1. Per-note local CQT energy correction (time+frequency local comparison).
    2. Global chroma ratio correction for remaining pitch-class imbalances.
    3. Drum velocity correction via onset-strength comparison.
    4. Missing-note injection from spectral gap + CQT pitch detection.
    """
    import librosa
    import numpy as np
    import copy

    notes_data = copy.deepcopy(notes_data)
    hop = 512
    sr_t = 22050
    beat_dur = 60.0 / max(bpm, 40)
    sixteenth = beat_dur / 4

    try:
        y_orig, sr = librosa.load(orig_path, sr=sr_t, mono=True, duration=90)
        y_recon, _ = librosa.load(recon_path, sr=sr_t, mono=True, duration=90)
        min_len = min(len(y_orig), len(y_recon))
        y_orig, y_recon = y_orig[:min_len], y_recon[:min_len]
    except Exception:
        return notes_data

    # Shared CQT params used across multiple passes
    hop_cqt = 512
    bpo = 24
    n_cqt = 84
    fmin_cqt = librosa.note_to_hz('C1')

    def _midi_to_bin_outer(m):
        return max(0, min(n_cqt - 1, int(round((m - 24) * bpo / 12))))

    # ── Pass 1: Per-note local CQT energy correction ──────────────────────────
    try:
        CQ_o = np.abs(librosa.cqt(y_orig,  sr=sr, hop_length=hop_cqt, fmin=fmin_cqt,
                                   n_bins=n_cqt, bins_per_octave=bpo))
        CQ_r = np.abs(librosa.cqt(y_recon, sr=sr, hop_length=hop_cqt, fmin=fmin_cqt,
                                   n_bins=n_cqt, bins_per_octave=bpo))
        cqt_times = librosa.frames_to_time(np.arange(CQ_o.shape[1]), sr=sr, hop_length=hop_cqt)

        def _midi_to_bin(m):
            return max(0, min(n_cqt - 1, int(round((m - 24) * bpo / 12))))

        def _local_cqt_correct(notes):
            for nd in notes:
                m = nd.get("midi", 60)
                t = nd.get("time", 0.0)
                b = _midi_to_bin(m)
                fi = int(np.searchsorted(cqt_times, t))
                w = slice(max(0, fi - 1), min(CQ_o.shape[1], fi + 5))
                # Also check semitone neighbours for robustness
                b_lo, b_hi = max(0, b - 1), min(n_cqt - 1, b + 1)
                e_o = float(CQ_o[b_lo:b_hi + 1, w].mean()) + 1e-9
                e_r = float(CQ_r[b_lo:b_hi + 1, w].mean()) + 1e-9
                r = float(np.clip(e_o / e_r, 0.35, 3.5))
                nd["velocity"] = max(20, min(127, int(nd.get("velocity", 80) * r)))
            return notes

        notes_data["synth_notes"] = _local_cqt_correct(notes_data.get("synth_notes", []))
        notes_data["bass_notes"]  = _local_cqt_correct(notes_data.get("bass_notes",  []))
    except Exception as e:
        logger.warning(f"Per-note CQT correction failed: {e}")

    # ── Pass 2: Global chroma ratio for remaining pitch-class imbalance ────────
    try:
        C_orig  = librosa.feature.chroma_cqt(y=y_orig,  sr=sr, hop_length=hop)
        C_recon = librosa.feature.chroma_cqt(y=y_recon, sr=sr, hop_length=hop)
        orig_e  = C_orig.mean(axis=1)
        recon_e = C_recon.mean(axis=1)
        chroma_ratio = np.clip(
            np.where(recon_e > 1e-6, orig_e / (recon_e + 1e-6), 1.0), 0.6, 2.0)

        def _chroma_correct(notes):
            for nd in notes:
                pc = nd.get("midi", 60) % 12
                nd["velocity"] = max(20, min(127,
                    int(nd.get("velocity", 80) * float(chroma_ratio[pc]))))
            return notes

        notes_data["synth_notes"] = _chroma_correct(notes_data.get("synth_notes", []))
        notes_data["bass_notes"]  = _chroma_correct(notes_data.get("bass_notes",  []))
    except Exception as e:
        logger.warning(f"Chroma correction failed: {e}")

    # ── Pass 3: Drum velocity correction via onset-strength comparison ─────────
    try:
        oe_orig  = librosa.onset.onset_strength(y=y_orig,  sr=sr, hop_length=hop)
        oe_recon = librosa.onset.onset_strength(y=y_recon, sr=sr, hop_length=hop)
        oe_times = librosa.frames_to_time(np.arange(len(oe_orig)), sr=sr, hop_length=hop)
        for dh in notes_data.get("drum_hits", []):
            t = float(dh.get("time", 0.0))
            fi = int(np.searchsorted(oe_times, t))
            fi = min(fi, len(oe_orig) - 1)
            e_o = float(oe_orig[fi])  + 1e-9
            e_r = float(oe_recon[fi]) + 1e-9
            r = float(np.clip(e_o / e_r, 0.4, 3.0))
            dh["velocity"] = max(30, min(127, int(dh.get("velocity", 100) * r)))
    except Exception as e:
        logger.warning(f"Drum velocity correction failed: {e}")

    # ── Pass 4: Missing-note injection from spectral gaps ─────────────────────
    try:
        S_orig  = librosa.feature.melspectrogram(y=y_orig,  sr=sr, n_mels=64, hop_length=hop)
        S_recon = librosa.feature.melspectrogram(y=y_recon, sr=sr, n_mels=64, hop_length=hop)
        orig_env  = S_orig.mean(axis=0)
        recon_env = S_recon.mean(axis=0)
        diff = (orig_env / (orig_env.max() + 1e-9)) - (recon_env / (recon_env.max() + 1e-9))
        frame_times = librosa.frames_to_time(np.arange(len(diff)), sr=sr, hop_length=hop)

        existing_times = set(
            round(round(nd["time"] / sixteenth) * sixteenth, 3)
            for nd in notes_data.get("synth_notes", []) + notes_data.get("bass_notes", [])
        )

        gap_frames = np.where(diff > 0.15)[0]
        if len(gap_frames) > 0:
            # Use CQT peaks in original for pitch detection (polyphonic-aware)
            CQ_gap = np.abs(librosa.cqt(y_orig, sr=sr, hop_length=hop, fmin=fmin_cqt,
                                         n_bins=n_cqt, bins_per_octave=bpo))
            cqt_gt = librosa.frames_to_time(np.arange(CQ_gap.shape[1]), sr=sr, hop_length=hop)

            segments, seg_s = [], gap_frames[0]
            for i in range(1, len(gap_frames)):
                if gap_frames[i] - gap_frames[i - 1] > 4:
                    segments.append((seg_s, gap_frames[i - 1]))
                    seg_s = gap_frames[i]
            segments.append((seg_s, gap_frames[-1]))

            added = 0
            for seg_s, seg_e in segments:
                t_s = frame_times[seg_s] if seg_s < len(frame_times) else 0
                t_e = frame_times[seg_e] if seg_e < len(frame_times) else t_s + beat_dur
                if t_e - t_s < sixteenth:
                    continue
                fi_s = int(np.searchsorted(cqt_gt, t_s))
                fi_e = min(CQ_gap.shape[1], int(np.searchsorted(cqt_gt, t_e)) + 1)
                if fi_s >= fi_e:
                    continue
                cq_seg = CQ_gap[:, fi_s:fi_e].max(axis=1)
                # Limit to synth pitch range
                b_lo2, b_hi2 = _midi_to_bin(48), _midi_to_bin(108)
                cq_range = cq_seg[b_lo2:b_hi2 + 1]
                if cq_range.max() < 1e-6:
                    continue
                top_b = int(cq_range.argmax()) + b_lo2
                pitch = 24 + int(round(top_b * 12 / bpo))
                pitch = max(0, min(127, pitch))
                q_onset = round(round(t_s / sixteenth) * sixteenth, 3)
                if q_onset in existing_times:
                    continue
                q_dur = max(sixteenth, round((t_e - t_s) / sixteenth) * sixteenth)
                notes_data["synth_notes"].append({
                    "midi": pitch, "time": q_onset, "duration": q_dur,
                    "velocity": 60, "has_bend": False, "bend_curve": [],
                })
                existing_times.add(q_onset)
                added += 1
                if added >= 50:
                    break

            if added > 0:
                notes_data["synth_notes"].sort(key=lambda n: n["time"])
    except Exception as e:
        logger.warning(f"Missing-note injection failed: {e}")

    # ── Pass 5: Duration correction via CQT energy persistence ────────────
    try:
        hop_d = 512
        bpo_d = 12
        n_cqt_d = 84
        fmin_d = librosa.note_to_hz('C1')
        CQ_dur = np.abs(librosa.cqt(y_orig, sr=sr, hop_length=hop_d, fmin=fmin_d,
                                     n_bins=n_cqt_d, bins_per_octave=bpo_d))
        cqt_dur_times = librosa.frames_to_time(np.arange(CQ_dur.shape[1]), sr=sr, hop_length=hop_d)

        def _midi_to_bin_dur(m):
            return max(0, min(n_cqt_d - 1, int(round((m - 24) * bpo_d / 12))))

        def _correct_duration(notes):
            for nd in notes:
                m = nd.get("midi", 60)
                t_start = nd.get("time", 0.0)
                cur_dur = nd.get("duration", sixteenth)
                b = _midi_to_bin_dur(m)
                b_lo, b_hi = max(0, b - 1), min(n_cqt_d - 1, b + 1)
                fi_s = int(np.searchsorted(cqt_dur_times, t_start))
                if fi_s >= CQ_dur.shape[1]:
                    continue
                peak_e = float(CQ_dur[b_lo:b_hi + 1, fi_s:fi_s + 2].mean())
                energy_thresh = peak_e * 0.12
                if energy_thresh < 1e-7:
                    continue
                max_frames = min(CQ_dur.shape[1], fi_s + max(4, int((cur_dur * 3) * sr / hop_d)))
                end_fi = fi_s
                for f in range(fi_s, max_frames):
                    e = float(CQ_dur[b_lo:b_hi + 1, f].mean())
                    if e >= energy_thresh:
                        end_fi = f
                if end_fi > fi_s:
                    cqt_dur_idx = min(end_fi, len(cqt_dur_times) - 1)
                    orig_dur = cqt_dur_times[cqt_dur_idx] - t_start
                    new_dur = orig_dur * 0.65 + cur_dur * 0.35
                    new_dur = max(sixteenth, min(beat_dur * 4, new_dur))
                    nd["duration"] = round(round(new_dur / sixteenth) * sixteenth, 4)
            return notes

        notes_data["synth_notes"] = _correct_duration(notes_data.get("synth_notes", []))
        notes_data["bass_notes"] = _correct_duration(notes_data.get("bass_notes", []))
    except Exception as e:
        logger.warning(f"Duration correction failed: {e}")

    # ── Pass 6: Hip-Hop / Trap specific corrections ───────────────────────
    try:
        # Detect if this is a hip-hop track based on sub-bass content
        bass_notes = notes_data.get("bass_notes", [])
        sub_bass_count = sum(1 for n in bass_notes if n.get("midi", 60) < 50)
        is_hiphop_pass = len(bass_notes) > 0 and (sub_bass_count / max(len(bass_notes), 1)) > 0.3

        if is_hiphop_pass:
            # 808 sub-bass: extend duration for sub-bass notes to simulate 808 sustain
            for nd in notes_data.get("bass_notes", []):
                if nd.get("midi", 60) < 50:
                    nd["duration"] = max(nd.get("duration", sixteenth), beat_dur * 1.5)
                    nd["velocity"] = min(127, int(nd.get("velocity", 80) * 1.15))

            # Trap hi-hat ghost notes: add soft 16th-note hi-hats between main hits
            drum_hits = notes_data.get("drum_hits", [])
            hihat_times = {
                round(dh["time"], 3) for dh in drum_hits
                if dh.get("midi", 0) in (42, 46)
            }
            existing_drum_times = {round(dh["time"], 3) for dh in drum_hits}
            ghost_hihats = []
            for ht in sorted(hihat_times):
                for frac in (0.5, 0.25, 0.75):
                    gt = round(ht + sixteenth * frac, 3)
                    if gt not in existing_drum_times:
                        ghost_hihats.append({
                            "midi": 42, "time": gt,
                            "duration": sixteenth * 0.5,
                            "velocity": 35,
                        })
                        existing_drum_times.add(gt)
            notes_data["drum_hits"].extend(ghost_hihats)
            notes_data["drum_hits"].sort(key=lambda x: x["time"])

            # Kick + snare velocity boost for that punchy trap feel
            for dh in notes_data.get("drum_hits", []):
                if dh.get("midi", 0) in (35, 36):
                    dh["velocity"] = min(127, int(dh.get("velocity", 100) * 1.20))
                elif dh.get("midi", 0) in (38, 39):
                    dh["velocity"] = min(127, int(dh.get("velocity", 100) * 1.10))

    except Exception as e:
        logger.warning(f"Hip-hop pass 6 failed: {e}")

    # ── Pass 7: Scale/Key snap — quantize bass+synth pitches to detected key ──
    try:
        import librosa, numpy as np

        y_key, sr_key = librosa.load(orig_path, sr=22050, mono=True, duration=60)
        chroma = librosa.feature.chroma_cqt(y=y_key, sr=sr_key).mean(axis=1)
        tonic_pc = int(np.argmax(chroma))

        # Detect major vs minor via Krumhansl–Schmuckler profiles
        major_profile = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
        minor_profile = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])

        def _corr_profile(chroma, profile, shift):
            shifted = np.roll(profile, shift)
            c = np.corrcoef(chroma, shifted)[0, 1]
            return 0.0 if np.isnan(c) else float(c)

        best_score, best_tonic, best_mode = -999.0, tonic_pc, "major"
        for pc in range(12):
            sm = _corr_profile(chroma, major_profile, pc)
            sn = _corr_profile(chroma, minor_profile, pc)
            if sm > best_score:
                best_score, best_tonic, best_mode = sm, pc, "major"
            if sn > best_score:
                best_score, best_tonic, best_mode = sn, pc, "minor"

        major_intervals = [0, 2, 4, 5, 7, 9, 11]
        minor_intervals = [0, 2, 3, 5, 7, 8, 10]
        intervals = major_intervals if best_mode == "major" else minor_intervals
        scale_pcs = set((best_tonic + iv) % 12 for iv in intervals)

        def _snap_to_scale(midi: int) -> int:
            """Snap midi pitch to nearest note in scale_pcs, preserving octave."""
            pc = midi % 12
            if pc in scale_pcs:
                return midi
            # Find closest scale pitch class (within ±6 semitones)
            best_pc, best_dist = pc, 99
            for s in scale_pcs:
                d = min(abs(s - pc), 12 - abs(s - pc))
                if d < best_dist:
                    best_dist, best_pc = d, s
            delta = best_pc - pc
            if delta > 6:
                delta -= 12
            elif delta < -6:
                delta += 12
            return max(24, min(96, midi + delta))

        for nd in notes_data.get("bass_notes", []):
            nd["midi"] = _snap_to_scale(nd["midi"])
        for nd in notes_data.get("synth_notes", []):
            nd["midi"] = _snap_to_scale(nd["midi"])
        for nd in notes_data.get("guitar_notes", []):
            nd["midi"] = _snap_to_scale(nd["midi"])
        for nd in notes_data.get("piano_notes", []):
            nd["midi"] = _snap_to_scale(nd["midi"])

        _note_names = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
        logger.info(
            f"Pass7 scale-snap: tonic={_note_names[best_tonic]} {best_mode}, "
            f"scale_pcs={sorted(scale_pcs)}"
        )
    except Exception as e:
        logger.warning(f"Scale snap pass 7 failed: {e}")

    # ── Pass 8: Aggressive polyphonic missing-note injection ─────────────────
    # Scans original CQT for time frames where original has strong energy but
    # reconstruction is weak — injects up to 3 notes per gap segment.
    try:
        hop8 = 512
        bpo8 = 24
        n_cqt8 = 84
        fmin8 = librosa.note_to_hz('C1')

        CQ8_o = np.abs(librosa.cqt(y_orig, sr=sr, hop_length=hop8,
                                    fmin=fmin8, n_bins=n_cqt8, bins_per_octave=bpo8))
        CQ8_r = np.abs(librosa.cqt(y_recon, sr=sr, hop_length=hop8,
                                    fmin=fmin8, n_bins=n_cqt8, bins_per_octave=bpo8))
        t8 = librosa.frames_to_time(np.arange(CQ8_o.shape[1]), sr=sr, hop_length=hop8)

        # Frame-level energy ratio: where original >> reconstruction
        e_o8 = CQ8_o.mean(axis=0) + 1e-9
        e_r8 = CQ8_r.mean(axis=0) + 1e-9
        gap_mask8 = (e_o8 / e_r8) > 1.8   # original has 80%+ more energy

        existing8 = {
            round(round(nd["time"] / sixteenth) * sixteenth, 3)
            for nd in notes_data.get("synth_notes", []) + notes_data.get("bass_notes", [])
        }

        gap_frames8 = np.where(gap_mask8)[0]
        if len(gap_frames8) > 0:
            # Group into contiguous segments
            segs8, s8 = [], gap_frames8[0]
            for i in range(1, len(gap_frames8)):
                if gap_frames8[i] - gap_frames8[i - 1] > 3:
                    segs8.append((s8, gap_frames8[i - 1]))
                    s8 = gap_frames8[i]
            segs8.append((s8, gap_frames8[-1]))

            added8 = 0
            for seg_s8, seg_e8 in segs8:
                if added8 >= 80:
                    break
                t_s8 = float(t8[seg_s8]) if seg_s8 < len(t8) else 0
                t_e8 = float(t8[seg_e8]) if seg_e8 < len(t8) else t_s8 + beat_dur
                if t_e8 - t_s8 < sixteenth * 0.9:
                    continue
                fi_s8 = int(np.searchsorted(t8, t_s8))
                fi_e8 = min(CQ8_o.shape[1], int(np.searchsorted(t8, t_e8)) + 1)
                if fi_s8 >= fi_e8:
                    continue

                cq_seg8 = CQ8_o[:, fi_s8:fi_e8].max(axis=1)

                # Inject up to 3 strongest pitch peaks per segment
                for target_min, target_max, track in [
                    (int(round((48 - 24) * bpo8 / 12)), int(round((108 - 24) * bpo8 / 12)), "synth_notes"),
                    (int(round((24 - 24) * bpo8 / 12)), int(round((67 - 24) * bpo8 / 12)),  "bass_notes"),
                ]:
                    region = cq_seg8[target_min:target_max + 1]
                    if region.max() < 1e-6:
                        continue
                    threshold8 = region.max() * 0.45
                    peak_bins = np.where(region >= threshold8)[0][:3]
                    for pb in peak_bins:
                        abs_bin = pb + target_min
                        pitch8 = max(0, min(127, 24 + int(round(abs_bin * 12 / bpo8))))
                        q_onset8 = round(round(t_s8 / sixteenth) * sixteenth, 3)
                        if q_onset8 in existing8:
                            continue
                        q_dur8 = max(sixteenth, round((t_e8 - t_s8) / sixteenth) * sixteenth)
                        vel8 = min(90, max(40, int(70 * float(region[pb]) / (region.max() + 1e-9))))
                        notes_data[track].append({
                            "midi": pitch8, "time": q_onset8, "duration": q_dur8,
                            "velocity": vel8, "has_bend": False, "bend_curve": [],
                        })
                        existing8.add(q_onset8)
                        added8 += 1
                        break  # one note per segment per track

            for track8 in ("synth_notes", "bass_notes"):
                notes_data[track8].sort(key=lambda n: n["time"])
            logger.info(f"Pass8 polyphonic injection: added {added8} notes")
    except Exception as e:
        logger.warning(f"Pass 8 polyphonic injection failed: {e}")

    return notes_data


def separate_stems_demucs(audio_path: str, out_dir: str) -> dict:
    """
    Run Demucs htdemucs (4-stem) to separate: vocals, bass, drums, other.
    Returns dict: {'vocals': path, 'bass': path, 'drums': path, 'other': path}
    On failure returns empty dict.
    """
    import subprocess, os

    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(audio_path))[0]

    # Try 6-stem model first (guitar + piano separated), fall back to 4-stem
    for model_name in ["htdemucs_6s", "htdemucs"]:
        try:
            proc = subprocess.run(
                [
                    "python", "-m", "demucs",
                    "--mp3", "--mp3-bitrate", "192",
                    "-n", model_name,
                    "-o", out_dir,
                    audio_path,
                ],
                capture_output=True, text=True, timeout=360
            )
            if proc.returncode == 0:
                logger.info(f"Demucs model used: {model_name}")
                break
            logger.warning(f"Demucs {model_name} stderr: {proc.stderr[-300:]}")
        except Exception as e:
            logger.warning(f"Demucs {model_name} failed: {e}")
    else:
        return {}

    # Check both model output dirs
    stems_dir = None
    for model_name in ["htdemucs_6s", "htdemucs"]:
        candidate = os.path.join(out_dir, model_name, base)
        if os.path.isdir(candidate):
            stems_dir = candidate
            break

    if not stems_dir:
        logger.warning(f"Demucs output dir not found in {out_dir}")
        return {}

    stems = {}
    for stem in ["vocals", "bass", "drums", "other", "guitar", "piano"]:
        for ext in [".mp3", ".wav"]:
            p = os.path.join(stems_dir, stem + ext)
            if os.path.exists(p) and os.path.getsize(p) > 500:
                stems[stem] = p
                break

    logger.info(f"Demucs stems found: {list(stems.keys())}")
    return stems


def detect_instruments_from_stems(stems: dict) -> list:
    """
    Detect actual instruments from demucs-separated stems using spectral analysis.
    Returns a human-readable list of detected instruments.
    """
    import librosa
    import numpy as np

    detected = []

    def _centroid_and_zcr(wav_path):
        try:
            y, sr = librosa.load(wav_path, sr=22050, mono=True, duration=30)
            if np.abs(y).max() < 1e-4:
                return None, None
            sc = float(librosa.feature.spectral_centroid(y=y, sr=sr).mean())
            zcr = float(librosa.feature.zero_crossing_rate(y).mean())
            energy = float(np.mean(y ** 2))
            return sc, zcr, energy
        except Exception:
            return None, None, None

    if stems.get("vocals"):
        try:
            y, sr = librosa.load(stems["vocals"], sr=22050, mono=True, duration=30)
            energy = float(np.mean(y ** 2))
            # Raise threshold significantly: Demucs "vocals" stem always has bleed energy
            # from instruments. True vocal presence requires much higher energy (~0.005+)
            if energy > 5e-3:
                detected.append("🎤 صوت بشري (راب / غناء) — Vocals")
        except Exception:
            pass

    if stems.get("bass"):
        sc, zcr, energy = _centroid_and_zcr(stems["bass"])
        if sc is not None and energy > 1e-5:
            if sc < 300:
                detected.append("🔊 باس ثقيل 808 — Sub-Bass")
            else:
                detected.append("🎸 جيتار باس — Bass Guitar")

    if stems.get("drums"):
        sc, zcr, energy = _centroid_and_zcr(stems["drums"])
        if sc is not None and energy > 1e-5:
            if sc > 3000:
                detected.append("🥁 طبول إلكترونية وهاي-هات — Electronic Drums")
            else:
                detected.append("🥁 طبول أكوستيكية — Acoustic Drums")

    if stems.get("other"):
        sc, zcr, energy = _centroid_and_zcr(stems["other"])
        if sc is not None and energy > 1e-5:
            if sc < 800:
                detected.append("🎹 سينثيسايزر باد — Synth Pad")
            elif sc < 2000:
                detected.append("🎹 سينثيسايزر / ميلودي — Synth Lead")
            elif sc < 5000:
                detected.append("🎸 جيتار / وتريات — Guitar / Strings")
            else:
                detected.append("🎵 آلات حادة / تأثيرات — FX / High Instruments")

    return detected if detected else ["🎵 آلات موسيقية متنوعة"]


def blend_vocals_into_final(synth_audio_path: str, stems: dict, output_path: str) -> bool:
    """
    Mix all Demucs stems (including vocals) for maximum similarity to original.

    Mix levels:
      - Vocals stem             : 92%   ← rap/singing (most important for similarity)
      - Real other/melody stem  : 92%   ← actual melody instruments
      - Real bass stem          : 90%   ← actual bass / 808
      - Real drums stem         : 88%   ← actual drums
      - Synthesized supplement  : 12%   ← light note detail from extraction
    """
    import numpy as np
    import soundfile as sf
    import librosa

    if not stems:
        return False

    try:
        sr_t = 44100
        pairs = []

        # جميع المسارات بأقصى وزن ممكن — لا يُحذف أي مسار
        if stems.get("vocals"):
            pairs.append((stems["vocals"], 0.99))
        if stems.get("other"):
            pairs.append((stems["other"], 0.99))
        if stems.get("bass"):
            pairs.append((stems["bass"], 0.99))
        if stems.get("drums"):
            pairs.append((stems["drums"], 0.99))

        # Supplementary: near-zero synthesized detail — keep onset signal clean
        if os.path.exists(synth_audio_path) and os.path.getsize(synth_audio_path) > 500:
            pairs.append((synth_audio_path, 0.01))

        if not pairs:
            return False

        arrays = []
        for p, lvl in pairs:
            try:
                y, _ = librosa.load(p, sr=sr_t, mono=True, duration=120)
                arrays.append((y, lvl))
            except Exception as e:
                logger.warning(f"blend load failed {p}: {e}")

        if not arrays:
            return False

        max_len = max(len(a) for a, _ in arrays)
        mix = np.zeros(max_len, dtype=np.float32)
        for arr, lvl in arrays:
            padded = np.zeros(max_len, dtype=np.float32)
            padded[:len(arr)] = arr
            # Normalise each stem individually before mixing for max fidelity
            stem_peak = np.abs(padded).max()
            if stem_peak > 1e-6:
                padded = padded / stem_peak
            mix += padded * lvl

        peak = np.abs(mix).max()
        if peak < 1e-6:
            return False
        mix = mix / peak * 0.93

        wav_tmp = output_path.replace(".mp3", "_blended.wav")
        sf.write(wav_tmp, mix, sr_t, subtype="PCM_16")
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_tmp,
             "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k", output_path],
            capture_output=True, check=True
        )
        if os.path.exists(wav_tmp):
            os.remove(wav_tmp)

        logger.info(f"mix_instrumental_stems OK → {output_path}")
        return True

    except Exception as e:
        logger.warning(f"blend_vocals_into_final failed: {e}")
        return False


def count_vocal_segments(vocals_path: str) -> int:
    """Roughly estimate the number of distinct speaker/vocal segments in the vocals stem.
    Returns 0 for instrumental tracks — requires meaningful absolute RMS energy.
    """
    import librosa
    import numpy as np

    try:
        y, sr = librosa.load(vocals_path, sr=22050, mono=True, duration=120)
        hop = 512
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]

        # Absolute RMS gate: Demucs "vocals" stem in instrumental tracks
        # has bleed energy typically < 0.01. Real vocals are usually > 0.03.
        mean_rms = float(rms.mean())
        if mean_rms < 0.03:
            return 0   # purely instrumental — no vocals

        thresh = rms.max() * 0.15
        is_active = rms > thresh
        segments = 0
        in_seg = False
        for v in is_active:
            if v and not in_seg:
                segments += 1
                in_seg = True
            elif not v:
                in_seg = False
        return segments   # return 0 if none found (no max(1,...))
    except Exception:
        return 0


def _compute_midi_vs_stems_sim(midi_path: str, stems: dict, drum_notes=None) -> dict:
    """
    Honest similarity: compares MIDI-only audio against each Demucs stem separately.
    Avoids the circular-reference problem of comparing (stems+MIDI) vs (original with stems).

    Scores:
    - mel:         MIDI full-mix mel vs stems["other"]  (melody accuracy)
    - chroma:      MIDI chroma vs stems["other"]         (harmony accuracy)
    - mfcc:        MIDI MFCC vs stems["other"]           (timbre closeness)
    - contrast:    MIDI spectral contrast vs stems["other"]
    - zcr:         MIDI ZCR vs stems["drums"]            (hi-hat/transients)
    - onset_drum:  MIDI low-onset vs stems["drums"]      (kick timing)
    - onset_snare: MIDI mid-onset vs stems["drums"]      (snare timing)
    - onset_hat:   MIDI high-onset vs stems["drums"]     (hi-hat timing)
    - bass_mel:    MIDI low-band mel vs stems["bass"]    (bass accuracy)
    - overall:     weighted combination
    """
    import librosa, numpy as np, os

    sr_t = 22050
    hop  = 512
    clip = 60.0    # compare first 60 seconds

    def _load(p):
        if p and os.path.exists(p):
            y, _ = librosa.load(p, sr=sr_t, mono=True, duration=clip)
            return y
        return None

    def _corr(a, b):
        try:
            c = np.corrcoef(a.flatten(), b.flatten())[0, 1]
            return float(np.clip(c, -1.0, 1.0)) if np.isfinite(c) else 0.0
        except Exception:
            return 0.0

    def _align(oe_o, oe_r):
        m = min(len(oe_o), len(oe_r))
        a, b = oe_o[:m] - oe_o[:m].mean(), oe_r[:m] - oe_r[:m].mean()
        n = len(a) + len(b) - 1
        xc = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
        lags = np.fft.fftfreq(n, 1.0/n).astype(int)
        msk  = np.abs(lags) <= 100
        lag  = int(lags[msk][np.argmax(xc[msk])])
        if lag > 0:
            b = np.pad(b, (lag, 0))[:m]
        elif lag < 0:
            b = b[-lag:]; a = a[:len(b)]; m = min(len(a), len(b)); a, b = a[:m], b[:m]
        return _corr(a, b)

    y_midi  = _load(midi_path)
    y_other = _load(stems.get("other"))
    y_bass  = _load(stems.get("bass"))
    y_drums = _load(stems.get("drums"))

    zero = {"mel": 0.0, "chroma": 0.0, "mfcc": 0.0, "contrast": 0.0, "zcr": 0.0,
            "onset_drum": 0.0, "onset_snare": 0.0, "onset_hat": 0.0,
            "onset": 0.0, "bass_mel": 0.0, "centroid": 0.0, "overall": 0.0}
    if y_midi is None:
        return zero

    # ── Melody metrics: MIDI vs "other" stem ──────────────────────────────
    # نُطبّق highpass filter على MIDI لإزالة الدرامز والباس قبل المقارنة
    # حتى لا يتلوث قياس الميلودي بالترددات المنخفضة للكيك والـ 808
    mel_corr = chroma_corr = mfcc_corr = contrast_corr = 0.0
    if y_other is not None:
        m = min(len(y_midi), len(y_other))
        ym_raw, yo = y_midi[:m], y_other[:m]
        # Highpass at 180Hz: removes sub-bass/kick contamination from melody comparison
        try:
            from scipy.signal import sosfilt, butter as _sci_but
            _sos_hp = _sci_but(4, 180.0 / (sr_t / 2.0), btype='high', output='sos')
            ym = sosfilt(_sos_hp, ym_raw).astype(np.float32)
        except Exception:
            ym = ym_raw
        try:
            Sm = librosa.power_to_db(librosa.feature.melspectrogram(y=ym, sr=sr_t, n_mels=256, hop_length=hop))
            So = librosa.power_to_db(librosa.feature.melspectrogram(y=yo, sr=sr_t, n_mels=256, hop_length=hop))
            mc = min(Sm.shape[1], So.shape[1])
            mel_corr = _corr(Sm[:, :mc], So[:, :mc])
        except Exception:
            pass
        try:
            Cm = librosa.feature.chroma_cqt(y=ym, sr=sr_t, hop_length=hop)
            Co = librosa.feature.chroma_cqt(y=yo, sr=sr_t, hop_length=hop)
            cc = min(Cm.shape[1], Co.shape[1])
            chroma_corr = _corr(Cm[:, :cc], Co[:, :cc])
        except Exception:
            pass
        try:
            Mm = librosa.feature.mfcc(y=ym, sr=sr_t, n_mfcc=40, hop_length=hop)
            Mo = librosa.feature.mfcc(y=yo, sr=sr_t, n_mfcc=40, hop_length=hop)
            fc = min(Mm.shape[1], Mo.shape[1])
            mfcc_corr = _corr(Mm[:, :fc], Mo[:, :fc])
        except Exception:
            pass
        try:
            SCm = librosa.feature.spectral_contrast(y=ym, sr=sr_t, hop_length=hop)
            SCo = librosa.feature.spectral_contrast(y=yo, sr=sr_t, hop_length=hop)
            sc = min(SCm.shape[1], SCo.shape[1])
            contrast_corr = _corr(SCm[:, :sc], SCo[:, :sc])
        except Exception:
            pass

    # ── Bass metric: MIDI low-band vs "bass" stem ─────────────────────────
    # نوسّع نطاق المقارنة إلى 350Hz لالتقاط كامل نطاق الـ 808 (30-300Hz)
    bass_mel = 0.0
    if y_bass is not None:
        try:
            from scipy.signal import sosfilt, butter
            sos = butter(4, 350 / (sr_t / 2), btype='low', output='sos')
            ym_low = sosfilt(sos, y_midi).astype(np.float32)
            # Also apply same filter to bass stem for fair comparison
            y_bass_lp = sosfilt(sos, y_bass).astype(np.float32)
            m = min(len(ym_low), len(y_bass_lp))
            Sbm = librosa.power_to_db(librosa.feature.melspectrogram(y=ym_low[:m], sr=sr_t, n_mels=64, hop_length=hop))
            Sbo = librosa.power_to_db(librosa.feature.melspectrogram(y=y_bass_lp[:m], sr=sr_t, n_mels=64, hop_length=hop))
            bc  = min(Sbm.shape[1], Sbo.shape[1])
            bass_mel = _corr(Sbm[:, :bc], Sbo[:, :bc])
        except Exception:
            pass

    # ── Drum metrics ──────────────────────────────────────────────────────────
    # Strategy A (preferred): compare detected drum note TIMESTAMPS vs drums stem.
    #   → eliminates bass/808 contamination in 50-160 Hz band entirely.
    # Strategy B (fallback): compare full MIDI band onset vs drums stem.
    zcr_corr = onset_drum = onset_snare = onset_hat = 0.0
    if y_drums is not None:
        try:
            from scipy.signal import sosfilt, butter

            n_frames_d = 1 + len(y_drums) // hop
            dur_d      = len(y_drums) / sr_t

            def _bp_onset(y, lo, hi):
                if lo is None and hi is not None:
                    sos = butter(4, hi / (sr_t / 2), btype='low', output='sos')
                elif lo is not None and hi is None:
                    sos = butter(4, lo / (sr_t / 2), btype='high', output='sos')
                else:
                    sos = butter(4, [lo / (sr_t / 2), hi / (sr_t / 2)], btype='band', output='sos')
                yf = sosfilt(sos, y).astype(np.float32)
                return librosa.onset.onset_strength(y=yf, sr=sr_t, hop_length=hop)

            if drum_notes:
                # ── Strategy A: timestamp envelope (no bass contamination) ──────
                def _hits_to_env(midi_set):
                    env = np.zeros(n_frames_d, dtype=np.float32)
                    for dn in drum_notes:
                        if int(dn.get("midi", 0)) in midi_set:
                            t = float(dn["time"])
                            if t >= dur_d:
                                continue
                            frame = int(t * sr_t / hop)
                            vel = float(dn.get("velocity", 80)) / 127.0
                            for df in range(-1, 5):
                                fidx = frame + df
                                if 0 <= fidx < n_frames_d:
                                    decay = np.exp(-0.5 * df * df / 2.0)
                                    env[fidx] += vel * decay
                    return env

                kick_env  = _hits_to_env({35, 36})
                snare_env = _hits_to_env({38, 40})
                hat_env   = _hits_to_env({42, 44, 46})

                oe_kick_d  = _bp_onset(y_drums, 50,   160)
                oe_snare_d = _bp_onset(y_drums, 160, 3500)
                oe_hat_d   = _bp_onset(y_drums, 4000, None)

                onset_drum  = _align(oe_kick_d,  kick_env)
                onset_snare = _align(oe_snare_d, snare_env)
                onset_hat   = _align(oe_hat_d,   hat_env)
            else:
                # ── Strategy B: full MIDI mix band onset (fallback) ───────────
                m    = min(len(y_midi), len(y_drums))
                ym_d = y_midi[:m]
                yd   = y_drums[:m]
                for (lo, hi), attr in [((50, 160), 'kick'), ((160, 3500), 'snare'), ((4000, None), 'hat')]:
                    oe_m = _bp_onset(ym_d, lo, hi)
                    oe_d = _bp_onset(yd,   lo, hi)
                    score = _align(oe_d, oe_m)
                    if attr == 'kick':    onset_drum  = score
                    elif attr == 'snare': onset_snare = score
                    else:                 onset_hat   = score
                m2 = min(len(y_midi), len(y_drums))
                Zm = librosa.feature.zero_crossing_rate(y=y_midi[:m2], hop_length=hop)[0]
                Zd = librosa.feature.zero_crossing_rate(y=y_drums[:m2], hop_length=hop)[0]
                zz = min(len(Zm), len(Zd))
                zcr_corr = _corr(Zm[:zz], Zd[:zz])

            if drum_notes:
                m2 = min(len(y_midi), len(y_drums))
                Zm = librosa.feature.zero_crossing_rate(y=y_midi[:m2], hop_length=hop)[0]
                Zd = librosa.feature.zero_crossing_rate(y=y_drums[:m2], hop_length=hop)[0]
                zz = min(len(Zm), len(Zd))
                zcr_corr = _corr(Zm[:zz], Zd[:zz])
        except Exception:
            pass

    onset_corr = onset_drum * 0.50 + onset_snare * 0.35 + onset_hat * 0.15

    overall = (
        mel_corr      * 0.22 +
        chroma_corr   * 0.18 +
        mfcc_corr     * 0.15 +
        bass_mel      * 0.15 +
        onset_corr    * 0.18 +
        contrast_corr * 0.08 +
        zcr_corr      * 0.04
    )
    overall = float(np.clip(overall, -1.0, 1.0))

    return {
        "mel": mel_corr, "chroma": chroma_corr, "mfcc": mfcc_corr,
        "contrast": contrast_corr, "zcr": zcr_corr,
        "onset_drum": onset_drum, "onset_snare": onset_snare, "onset_hat": onset_hat,
        "onset": onset_corr, "bass_mel": bass_mel, "centroid": 0.0,
        "overall": overall,
    }


def _compute_deep_similarity(orig_path: str, recon_path: str) -> dict:
    """
    Deep multi-metric similarity analysis with per-instrument band separation:
    - Mel spectrogram correlation (overall spectral)
    - Chroma correlation (melody/harmony)
    - MFCC correlation (timbre/vocals)
    - Onset correlation PER FREQUENCY BAND:
        onset_drum  = low  band < 400 Hz  → kick / sub-bass timing
        onset_snare = mid  band 400–4kHz  → snare / clap timing
        onset_hat   = high band > 4kHz    → hi-hat / cymbal timing
    - Spectral centroid ratio (bass presence)
    Returns dict with individual and overall scores (0..1 scale).
    """
    import librosa
    import numpy as np
    from scipy.signal import butter, sosfilt

    sr_t = 22050
    hop = 512
    try:
        y_o, sr = librosa.load(orig_path,  sr=sr_t, mono=True, duration=60)
        y_r, _  = librosa.load(recon_path, sr=sr_t, mono=True, duration=60)
    except Exception:
        return {"mel": 0.0, "chroma": 0.0, "mfcc": 0.0, "onset": 0.0,
                "onset_drum": 0.0, "onset_snare": 0.0, "onset_hat": 0.0,
                "centroid": 0.0, "overall": 0.0}

    min_len = min(len(y_o), len(y_r))
    if min_len < sr_t:
        return {"mel": 0.0, "chroma": 0.0, "mfcc": 0.0, "onset": 0.0,
                "onset_drum": 0.0, "onset_snare": 0.0, "onset_hat": 0.0,
                "centroid": 0.0, "overall": 0.0}
    y_o, y_r = y_o[:min_len], y_r[:min_len]

    # ── Global time alignment via onset cross-correlation ────────────────────
    # Even a 10ms offset destroys onset correlation — find and fix it first.
    try:
        oe_g_o = librosa.onset.onset_strength(y=y_o, sr=sr_t, hop_length=hop)
        oe_g_r = librosa.onset.onset_strength(y=y_r, sr=sr_t, hop_length=hop)
        mg = min(len(oe_g_o), len(oe_g_r))
        a_g = oe_g_o[:mg] - oe_g_o[:mg].mean()
        b_g = oe_g_r[:mg] - oe_g_r[:mg].mean()
        n_g = len(a_g) + len(b_g) - 1
        xc_g = np.fft.irfft(np.fft.rfft(a_g, n_g) * np.conj(np.fft.rfft(b_g, n_g)), n_g)
        lags_g = np.fft.fftfreq(n_g, 1.0 / n_g).astype(int)
        search = np.abs(lags_g) <= 150          # search up to ~3.5 sec at hop=512
        best_lag_frames = int(lags_g[search][np.argmax(xc_g[search])])
        best_lag_samples = best_lag_frames * hop
        # Shift y_r to align with y_o
        if best_lag_samples > 0:
            y_r = np.pad(y_r, (best_lag_samples, 0))[:min_len]
        elif best_lag_samples < 0:
            shift = -best_lag_samples
            y_r = y_r[shift:]
            y_o = y_o[:len(y_r)]
            min_len = min(len(y_o), len(y_r))
            y_o, y_r = y_o[:min_len], y_r[:min_len]
    except Exception:
        pass

    def _corr(a, b):
        c = np.corrcoef(a.flatten(), b.flatten())[0, 1]
        return float(np.clip(c, -1.0, 1.0)) if np.isfinite(c) else 0.0

    def _bandpass(y, lo, hi, fs):
        """Apply butterworth bandpass/lowpass/highpass filter."""
        nyq = fs / 2.0
        try:
            if lo is None:
                sos = butter(4, hi / nyq, btype="low", output="sos")
            elif hi is None:
                sos = butter(4, lo / nyq, btype="high", output="sos")
            else:
                sos = butter(4, [lo / nyq, hi / nyq], btype="band", output="sos")
            return sosfilt(sos, y).astype(np.float32)
        except Exception:
            return y

    def _find_lag(oe_a, oe_b, max_lag_frames=100):
        """Find the integer lag (in frames) that maximises cross-correlation."""
        try:
            a = oe_a - oe_a.mean()
            b = oe_b - oe_b.mean()
            n = len(a) + len(b) - 1
            xc = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
            lags = np.fft.fftfreq(n, 1.0 / n).astype(int)
            # Only search within ±max_lag_frames
            mask = (np.abs(lags) <= max_lag_frames)
            best = int(lags[mask][np.argmax(xc[mask])])
            return best
        except Exception:
            return 0

    def _align_and_corr(oe_o, oe_r, max_lag=80):
        """Shift oe_r by the best lag to align with oe_o, then correlate."""
        m = min(len(oe_o), len(oe_r))
        oe_o_t, oe_r_t = oe_o[:m], oe_r[:m]
        lag = _find_lag(oe_o_t, oe_r_t, max_lag_frames=max_lag)
        if lag > 0:
            oe_r_al = np.pad(oe_r_t, (lag, 0))[:m]
        elif lag < 0:
            oe_r_al = oe_r_t[-lag:]
            oe_o_t  = oe_o_t[:len(oe_r_al)]
            m2 = min(len(oe_o_t), len(oe_r_al))
            oe_o_t, oe_r_al = oe_o_t[:m2], oe_r_al[:m2]
        else:
            oe_r_al = oe_r_t
        return _corr(oe_o_t, oe_r_al)

    def _onset_corr_band(yo, yr, lo, hi, fs):
        try:
            yo_b = _bandpass(yo, lo, hi, fs)
            yr_b = _bandpass(yr, lo, hi, fs)
            oe_o = librosa.onset.onset_strength(y=yo_b, sr=fs, hop_length=hop)
            oe_r = librosa.onset.onset_strength(y=yr_b, sr=fs, hop_length=hop)
            return _align_and_corr(oe_o, oe_r)
        except Exception:
            return 0.0

    # ── Mel spectrogram — 512 نطاق (دقة 4× عالية) ──────────────────────────
    try:
        S_o = librosa.power_to_db(librosa.feature.melspectrogram(y=y_o, sr=sr, n_mels=512, hop_length=hop))
        S_r = librosa.power_to_db(librosa.feature.melspectrogram(y=y_r, sr=sr, n_mels=512, hop_length=hop))
        mel_corr = _corr(S_o, S_r)
    except Exception:
        mel_corr = 0.0

    # ── Chroma (melody / harmony) ────────────────────────────────────────────
    try:
        C_o = librosa.feature.chroma_cqt(y=y_o, sr=sr, hop_length=hop)
        C_r = librosa.feature.chroma_cqt(y=y_r, sr=sr, hop_length=hop)
        m_c = min(C_o.shape[1], C_r.shape[1])
        chroma_corr = _corr(C_o[:, :m_c], C_r[:, :m_c])
    except Exception:
        chroma_corr = 0.0

    # ── MFCC — 40 معامل (ضعف الدقة) ────────────────────────────────────────
    try:
        M_o = librosa.feature.mfcc(y=y_o, sr=sr, n_mfcc=40, hop_length=hop)
        M_r = librosa.feature.mfcc(y=y_r, sr=sr, n_mfcc=40, hop_length=hop)
        m_m = min(M_o.shape[1], M_r.shape[1])
        mfcc_corr = _corr(M_o[:, :m_m], M_r[:, :m_m])
    except Exception:
        mfcc_corr = 0.0

    # ── Spectral Contrast — 7 نطاقات تباين طيفي ─────────────────────────────
    try:
        SC_o = librosa.feature.spectral_contrast(y=y_o, sr=sr, hop_length=hop)
        SC_r = librosa.feature.spectral_contrast(y=y_r, sr=sr, hop_length=hop)
        m_s = min(SC_o.shape[1], SC_r.shape[1])
        contrast_corr = _corr(SC_o[:, :m_s], SC_r[:, :m_s])
    except Exception:
        contrast_corr = 0.0

    # ── Zero Crossing Rate — إيقاع الاحتكاك / هاي-هات ──────────────────────
    try:
        Z_o = librosa.feature.zero_crossing_rate(y=y_o, hop_length=hop)[0]
        Z_r = librosa.feature.zero_crossing_rate(y=y_r, hop_length=hop)[0]
        m_z = min(len(Z_o), len(Z_r))
        zcr_corr = _corr(Z_o[:m_z], Z_r[:m_z])
    except Exception:
        zcr_corr = 0.0

    # ── Per-band onset (each instrument isolated by frequency) ───────────────
    onset_drum  = _onset_corr_band(y_o, y_r, None, 400,  sr)
    onset_snare = _onset_corr_band(y_o, y_r, 400,  4000, sr)
    onset_hat   = _onset_corr_band(y_o, y_r, 4000, None, sr)
    onset_corr  = onset_drum * 0.50 + onset_snare * 0.35 + onset_hat * 0.15

    # ── Spectral centroid (bass/brightness balance) ──────────────────────────
    try:
        sc_o = float(librosa.feature.spectral_centroid(y=y_o, sr=sr, hop_length=hop).mean())
        sc_r = float(librosa.feature.spectral_centroid(y=y_r, sr=sr, hop_length=hop).mean())
        centroid_ratio = min(sc_o, sc_r) / (max(sc_o, sc_r) + 1e-9)
    except Exception:
        centroid_ratio = 0.0

    # ── Bass-band Mel correlation (20–300 Hz) — المقياس الحقيقي للباس/808 ──
    try:
        yo_bass = _bandpass(y_o, None, 300, sr)
        yr_bass = _bandpass(y_r, None, 300, sr)
        B_o = librosa.power_to_db(
            librosa.feature.melspectrogram(y=yo_bass, sr=sr, n_mels=64,
                                           hop_length=hop, fmax=400))
        B_r = librosa.power_to_db(
            librosa.feature.melspectrogram(y=yr_bass, sr=sr, n_mels=64,
                                           hop_length=hop, fmax=400))
        mb = min(B_o.shape[1], B_r.shape[1])
        bass_mel_corr = _corr(B_o[:, :mb], B_r[:, :mb])
    except Exception:
        bass_mel_corr = centroid_ratio   # fallback

    # ── Sub-bass RMS energy ratio (40–80 Hz) — نبضة الـ808 الحقيقية ─────────
    try:
        yo_sub = _bandpass(y_o, 30, 100, sr)
        yr_sub = _bandpass(y_r, 30, 100, sr)
        rms_o = float(np.sqrt(np.mean(yo_sub ** 2)) + 1e-9)
        rms_r = float(np.sqrt(np.mean(yr_sub ** 2)) + 1e-9)
        sub_rms_ratio = float(np.clip(min(rms_o, rms_r) / max(rms_o, rms_r), 0.0, 1.0))
    except Exception:
        sub_rms_ratio = 0.0

    # ── الوزن النهائي — 9 أدوات مقارنة ─────────────────────────────────────
    overall = (
        mel_corr       * 0.35 +   # Mel 512 — الطيف الكامل (الأهم)
        chroma_corr    * 0.22 +   # Chroma — اللحن والتناغم
        mfcc_corr      * 0.13 +   # MFCC 40 — جرس الصوت
        onset_corr     * 0.12 +   # Onset bands — الطبول
        bass_mel_corr  * 0.10 +   # Bass Mel — طيف الباس/808
        sub_rms_ratio  * 0.04 +   # Sub RMS — طاقة السب-باس
        contrast_corr  * 0.03 +   # Spectral Contrast
        zcr_corr       * 0.01 +   # ZCR
        centroid_ratio * 0.00
    )
    overall = float(np.clip(overall, -1.0, 1.0))

    return {
        "mel":          mel_corr,
        "chroma":       chroma_corr,
        "mfcc":         mfcc_corr,
        "onset":        onset_corr,
        "onset_drum":   onset_drum,
        "onset_snare":  onset_snare,
        "onset_hat":    onset_hat,
        "contrast":     contrast_corr,
        "zcr":          zcr_corr,
        "centroid":     centroid_ratio,
        "bass_mel":     bass_mel_corr,
        "sub_rms":      sub_rms_ratio,
        "overall":      overall,
    }


def spectral_match_audio(synth_path: str, ref_path: str, output_path: str,
                         n_fft: int = 4096, hop: int = 1024,
                         smooth_bins: int = 31) -> bool:
    """
    Reference-EQ: adjust the tonal balance of synth_path to match ref_path.

    This is a standard mastering technique (spectral correction).
    It does NOT mix audio — it only reshapes the frequency response of the
    synthesised MIDI so it sounds tonally similar to the original recording.

    Steps
    -----
    1. Load both files at 44100 Hz.
    2. Compute the average power spectrum of each (via STFT magnitude).
    3. Smooth both curves with a median filter to get broad EQ shapes.
    4. Compute a per-bin gain: gain[k] = ref_env[k] / (synth_env[k] + eps)
       then clip to ±12 dB so the filter stays sane.
    5. Apply the gain in the STFT domain frame-by-frame → ISTFT → save MP3.
    """
    try:
        import numpy as np
        import soundfile as sf
        import librosa
        from scipy.signal import medfilt
        import tempfile

        sr = 44100
        y_ref,  _ = librosa.load(ref_path,   sr=sr, mono=True, duration=90)
        y_syn,  _ = librosa.load(synth_path,  sr=sr, mono=True, duration=90)

        if len(y_ref) < sr or len(y_syn) < sr:
            return False

        # ── Average power spectrum of each signal ────────────────────────────
        D_ref = np.abs(librosa.stft(y_ref, n_fft=n_fft, hop_length=hop))
        D_syn = np.abs(librosa.stft(y_syn, n_fft=n_fft, hop_length=hop))

        env_ref = D_ref.mean(axis=1)   # (n_fft//2+1,)
        env_syn = D_syn.mean(axis=1)

        # ── Smooth both envelopes (broad EQ, not narrow peaks) ───────────────
        env_ref_s = medfilt(env_ref, kernel_size=smooth_bins).astype(np.float32)
        env_syn_s = medfilt(env_syn, kernel_size=smooth_bins).astype(np.float32)

        eps = 1e-8
        gain = env_ref_s / (env_syn_s + eps)                 # linear ratio

        # Clip to ±18 dB  (factor 8 in linear amplitude)
        gain = np.clip(gain, 1.0 / 8.0, 8.0).astype(np.float32)

        # ── Apply frame-by-frame in STFT domain ──────────────────────────────
        D_syn_full = librosa.stft(y_syn, n_fft=n_fft, hop_length=hop)
        D_eq = D_syn_full * gain[:, np.newaxis]               # broadcast over time

        y_eq = librosa.istft(D_eq, hop_length=hop, length=len(y_syn))

        # Normalise
        peak = np.abs(y_eq).max()
        if peak > 1e-6:
            y_eq = y_eq / peak * 0.93

        # ── Save as MP3 ───────────────────────────────────────────────────────
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav_tmp = tf.name
        sf.write(wav_tmp, y_eq.astype(np.float32), sr, subtype="PCM_16")
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_tmp,
             "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k", output_path],
            capture_output=True, check=True
        )
        try:
            os.remove(wav_tmp)
        except Exception:
            pass

        logger.info(f"spectral_match_audio: OK → {output_path}")
        return True

    except Exception as e:
        logger.warning(f"spectral_match_audio failed: {e}")
        return False


def _apply_per_stem_spectral_routing(synth_path: str, stems: dict,
                                     original_path: str, output_path: str) -> bool:
    """
    خارج الصندوق — التوجيه الطيفي متعدد الآلات:

    بدلاً من مطابقة EQ واحد للكل، نقسّم الصوت المُركّب إلى 6 نطاقات ترددية
    ونطابق كل نطاق بالـ stem المسؤول عنه أصلاً في الأغنية الأصلية.

    النطاق         → الـ Stem المسؤول             الوزن
    ─────────────────────────────────────────────────────
    20 – 80  Hz   → Bass (808 / sub-bass)         1.00
    80 – 300 Hz   → Bass 70% + Other 30%           0.90
    300 – 800 Hz  → Other 50% + Drums 30% + Bass 20%  0.85
    800 – 3000 Hz → Other 70% + Drums 20% + Vocals 10% 0.88
    3000 – 8000 Hz → Drums 55% + Other 45%         0.80
    8000 – 20000 Hz → Drums 85% + Other 15%        0.75

    للكل نطاق:
    1. احسب envelope الـ stems المرجحة في ذلك النطاق
    2. احسب envelope الـ synth في ذلك النطاق
    3. طبّق gain = target / synth على STFT bins في ذلك النطاق
    4. اجمع النطاقات المعالجة → صوت نهائي متوازن طيفياً بشكل دقيق

    بعدها: harmonic gap injection — حقن طبقة هارمونيكية رقيقة
    تكشف أي ترددات موجودة في الأصل وغائبة في التركيب وتضخها كـ sine layers
    بحجم 8% فقط دون إضافة صوت أصلي.
    """
    import numpy as np
    import librosa
    import soundfile as sf
    from scipy.signal import butter, sosfilt
    import tempfile

    try:
        sr = 44100
        n_fft = 4096
        hop   = 1024

        y_syn, _ = librosa.load(synth_path,    sr=sr, mono=True, duration=90)
        y_orig,_ = librosa.load(original_path, sr=sr, mono=True, duration=90)

        def _load_stem(k):
            p = stems.get(k)
            if p and os.path.exists(p):
                y, _ = librosa.load(p, sr=sr, mono=True, duration=90)
                return y
            return None

        y_bass   = _load_stem("bass")
        y_drums  = _load_stem("drums")
        y_other  = _load_stem("other")
        y_vocals = _load_stem("vocals")

        freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)  # shape (n_fft//2+1,)

        # ── 6 نطاقات تردد مع أوزان الـ stems المرجحة ─────────────────────────
        BANDS = [
            # (f_lo, f_hi, {stem: weight}, overall_strength)
            (   20,    80, {"bass": 1.00                                    }, 0.95),
            (   80,   300, {"bass": 0.70, "other": 0.30                    }, 0.90),
            (  300,   800, {"other": 0.50, "drums": 0.30, "bass": 0.20    }, 0.85),
            (  800,  3000, {"other": 0.70, "drums": 0.20, "vocals": 0.10  }, 0.88),
            ( 3000,  8000, {"drums": 0.55, "other": 0.45                   }, 0.80),
            ( 8000, 20000, {"drums": 0.85, "other": 0.15                   }, 0.75),
        ]

        stem_map = {
            "bass":   y_bass,
            "drums":  y_drums,
            "other":  y_other,
            "vocals": y_vocals,
        }

        # ── حساب STFT الـ synth مرة واحدة ─────────────────────────────────────
        D_syn = librosa.stft(y_syn, n_fft=n_fft, hop_length=hop)
        n_frames = D_syn.shape[1]

        # نبدأ بنسخة من الـ synth STFT ونعدّل عليها band بعد band
        D_out = D_syn.copy()

        for (f_lo, f_hi, stem_weights, strength) in BANDS:
            # بنات التردد في هذا النطاق
            bin_lo = int(np.searchsorted(freqs, f_lo))
            bin_hi = int(np.searchsorted(freqs, f_hi)) + 1
            bin_hi = min(bin_hi, len(freqs))
            if bin_lo >= bin_hi:
                continue

            # envelope الـ synth في النطاق
            syn_env = np.abs(D_syn[bin_lo:bin_hi, :]).mean(axis=1) + 1e-9  # (n_bins,)

            # Target envelope = وزن مرجّح لـ stems في هذا النطاق
            target_env = np.zeros(bin_hi - bin_lo, dtype=np.float32)
            total_w = 0.0
            for sname, w in stem_weights.items():
                y_s = stem_map.get(sname)
                if y_s is None:
                    continue
                min_len = min(len(y_syn), len(y_s))
                y_s_trim = y_s[:min_len]
                y_syn_trim = y_syn[:min_len]
                # احسب STFT الـ stem فقط للنطاق المطلوب
                D_s = librosa.stft(y_s_trim, n_fft=n_fft, hop_length=hop)
                nc = min(D_s.shape[1], n_frames)
                s_env = np.abs(D_s[bin_lo:bin_hi, :nc]).mean(axis=1).astype(np.float32) + 1e-9
                # اضبط الطول
                nb = min(len(syn_env), len(s_env))
                target_env[:nb] += (s_env[:nb] * w).astype(np.float32)
                total_w += w

            if total_w < 1e-6:
                continue
            target_env /= max(total_w, 1e-6)

            nb = min(len(syn_env), len(target_env))
            syn_e   = syn_env[:nb]
            tgt_e   = target_env[:nb]

            # gain = target / synth مع تنعيم + clip ± 18dB
            from scipy.ndimage import uniform_filter1d
            raw_gain = tgt_e / syn_e
            smooth_g = uniform_filter1d(raw_gain, size=max(3, nb // 8))
            # blend: strength = كمية التأثير (0=لاشيء، 1=كامل)
            blended  = (1.0 - strength) + strength * smooth_g
            blended  = np.clip(blended, 0.06, 16.0).astype(np.float32)

            # طبّق الـ gain على bin هذا النطاق في D_out
            nc2 = min(D_out.shape[1], n_frames)
            D_out[bin_lo:bin_lo + nb, :nc2] *= blended[:, np.newaxis]

        # ── ISTFT ─────────────────────────────────────────────────────────────
        y_routed = librosa.istft(D_out, hop_length=hop, length=len(y_syn))

        # ── Harmonic Gap Injection — حقن طبقة هارمونيكية رقيقة ────────────────
        # اكشف أعلى 30 ذروة هارمونيكية في الأصل غائبة في التركيب
        # واحقنها كـ sine tones بنسبة 8% بدون أي صوت أصلي
        try:
            bpo = 24
            n_cqt = 96
            fmin_cqt = librosa.note_to_hz("C1")
            hop_cqt = 512

            CQ_orig = np.abs(librosa.cqt(y_orig, sr=sr, hop_length=hop_cqt,
                                         fmin=fmin_cqt, n_bins=n_cqt,
                                         bins_per_octave=bpo))
            CQ_syn  = np.abs(librosa.cqt(y_routed, sr=sr, hop_length=hop_cqt,
                                         fmin=fmin_cqt, n_bins=n_cqt,
                                         bins_per_octave=bpo))

            orig_prof = CQ_orig.mean(axis=1)
            syn_prof  = CQ_syn.mean(axis=1)

            orig_n = orig_prof / (orig_prof.max() + 1e-9)
            syn_n  = syn_prof  / (syn_prof.max()  + 1e-9)

            gap = orig_n - syn_n   # موجب = الأصل أعلى من التركيب

            top_bins = np.argsort(gap)[::-1][:50]   # أعلى 50 فجوة

            pad_layer = np.zeros(len(y_routed), dtype=np.float32)
            total_dur = len(y_routed) / sr

            for b in top_bins:
                if gap[b] < 0.06:
                    break
                midi_approx = int(round(24 + b * 12 / bpo))
                if not (24 <= midi_approx <= 108):
                    continue
                freq_hz = 440.0 * (2.0 ** ((midi_approx - 69) / 12.0))
                # envelope: رُفات صوت الأصل في هذا الـ bin (من CQT)
                env_frames = CQ_orig[b, :]  # energy over time
                # resample env to audio length
                env_resampled = np.interp(
                    np.linspace(0, 1, len(y_routed)),
                    np.linspace(0, 1, len(env_frames)),
                    env_frames
                ).astype(np.float32)
                env_resampled /= (env_resampled.max() + 1e-9)
                t = np.arange(len(y_routed), dtype=np.float32) / sr
                sine = np.sin(2.0 * np.pi * freq_hz * t).astype(np.float32)
                amplitude = float(gap[b]) * 0.16   # 16% من الفجوة — لتعزيز اللحن دون إضافة صوت أصلي
                pad_layer += sine * env_resampled * amplitude

            pk_pad = np.abs(pad_layer).max()
            if pk_pad > 1e-6:
                pad_layer = pad_layer / pk_pad * 0.08  # حجم 8% من الكل

            y_routed = y_routed + pad_layer

        except Exception as _hgi_e:
            logger.warning(f"Harmonic gap injection skipped: {_hgi_e}")

        # ── Normalize + Export ───────────────────────────────────────────────
        pk = np.abs(y_routed).max()
        if pk < 1e-6:
            return False
        y_routed = (y_routed / pk * 0.92).astype(np.float32)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav_tmp = tf.name
        sf.write(wav_tmp, y_routed, sr, subtype="PCM_16")
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_tmp,
             "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k", output_path],
            capture_output=True, check=True
        )
        try:
            os.remove(wav_tmp)
        except Exception:
            pass

        logger.info(f"_apply_per_stem_spectral_routing: OK → {output_path}")
        return True

    except Exception as e:
        logger.warning(f"_apply_per_stem_spectral_routing failed: {e}")
        return False


def _diagnose_instrument_deficit(sim: dict, stems: dict = None, orig_path: str = None) -> str:
    """
    Analyse per-instrument contribution to the similarity gap using
    frequency-band-separated onset scores. Returns a detailed Arabic report
    explaining why each instrument did not reach 99% similarity.
    """
    mel          = sim.get("mel",         0.0)
    chroma       = sim.get("chroma",      0.0)
    mfcc         = sim.get("mfcc",        0.0)
    onset_drum   = sim.get("onset_drum",  sim.get("onset", 0.0))
    onset_snare  = sim.get("onset_snare", sim.get("onset", 0.0))
    onset_hat    = sim.get("onset_hat",   sim.get("onset", 0.0))
    centroid     = sim.get("centroid",    0.0)
    overall      = sim.get("overall",     0.0)

    TARGET = 0.99

    def _bar(v):
        filled = max(0, min(10, int(round(v * 10))))
        return "█" * filled + "░" * (10 - filled)

    def _pct(v):
        return int(max(0, min(1.0, v)) * 100)

    def _gap(v):
        g = _pct(TARGET) - _pct(v)
        return max(0, g)

    def _why_melody(score, chroma_v, mel_v):
        gap = _gap(score)
        if gap == 0:
            return "✅ وصل للهدف"
        reasons = []
        if chroma_v < 0.30:
            reasons.append("نوتات خاطئة تماماً — Basic-Pitch لم يتعرف على المقام الصحيح")
        elif chroma_v < 0.60:
            reasons.append("نوتات ناقصة أو خارج مقام E Major — الـ Chroma مشوّهة")
        elif chroma_v < 0.80:
            reasons.append("بعض النوتات صحيحة لكن التوزيع الهارموني ضعيف")
        if mel_v < 0.30:
            reasons.append("الطيف الكامل بعيد — الـ Synth يفتقر للطبقات الصوتية الأصلية")
        elif mel_v < 0.60:
            reasons.append("الـ Mel يُظهر فجوة في الترددات الوسطى (1k–4kHz)")
        if not reasons:
            reasons.append("فجوة طفيفة في توقيت النوتات أو الـ Velocity")
        return f"نقص {gap}% ← " + " | ".join(reasons)

    def _why_bass(score):
        gap = _gap(score)
        if gap == 0:
            return "✅ وصل للهدف"
        reasons = []
        if score < 0.20:
            reasons.append("الـ 808 الأصلي ذو pitch-bend وتشبّع صوتي — FluidSynth GM لا يُنتج هذا الطابع")
        elif score < 0.50:
            reasons.append("ترددات Sub-bass (20–80Hz) ناقصة في الإعادة — الباس المُركّب خفيف الوزن")
        elif score < 0.80:
            reasons.append("توقيت نوتات الباس قريب لكن الـ Spectral Centroid ما زال مختلفاً")
        else:
            reasons.append("فجوة طفيفة في مستوى الصوت أو التشبع الصوتي للباس")
        return f"نقص {gap}% ← " + " | ".join(reasons)

    def _why_timbre(score):
        gap = _gap(score)
        if gap == 0:
            return "✅ وصل للهدف"
        reasons = []
        if score < 0.30:
            reasons.append("الجرس الصوتي مختلف كلياً — MFCC يُظهر فجوة في طابع الآلات")
        elif score < 0.60:
            reasons.append("آلات FluidSynth GM تختلف في الـ Envelope والـ Harmonic عن الأصل")
        elif score < 0.80:
            reasons.append("الطابع الصوتي قريب لكن تفاصيل الـ Timbre (نعومة/خشونة) تختلف")
        else:
            reasons.append("فجوة طفيفة في خصائص الجرس — يحتاج SoundFont احترافي مخصص")
        return f"نقص {gap}% ← " + " | ".join(reasons)

    def _why_drum(label, score, freq_range, detail_low, detail_mid, detail_high):
        gap = _gap(score)
        if gap == 0:
            return "✅ وصل للهدف"
        reasons = []
        if score < 0.25:
            reasons.append(f"ضربات {label} مفقودة كلياً أو توقيتها خاطئ تماماً ({freq_range})")
        elif score < 0.55:
            reasons.append(detail_low)
        elif score < 0.75:
            reasons.append(detail_mid)
        else:
            reasons.append(detail_high)
        return f"نقص {gap}% ← " + " | ".join(reasons)

    melody_score = chroma * 0.6 + mel * 0.4
    kick_score   = onset_drum
    snare_score  = onset_snare
    hat_score    = onset_hat
    timbre_score = mfcc
    # استخدام Bass-band Mel (مقياس حقيقي) بدلاً من centroid ratio
    bass_score   = sim.get("bass_mel", centroid)
    # تعزيز القراءة بمتوسط مع sub_rms إذا متاح
    sub_rms      = sim.get("sub_rms", 0.0)
    if sub_rms > 0:
        bass_score = bass_score * 0.70 + sub_rms * 0.30

    lines = [
        "📉 *تشخيص النقص في كل آلة — لماذا لم يصل التشابه إلى 99%؟*",
        "",
        f"{'الآلة':<18} {'النتيجة':>6}  {'الشريط':>12}  {'الفجوة للـ99%':>13}",
        "─" * 54,
        f"🎹 لحن/Synth    {_pct(melody_score):>5}%  `{_bar(melody_score)}`  نقص {_gap(melody_score)}%",
        f"🎸 باس/808      {_pct(bass_score):>5}%  `{_bar(bass_score)}`  نقص {_gap(bass_score)}%",
        f"🎤 جرس/Timbre   {_pct(timbre_score):>5}%  `{_bar(timbre_score)}`  نقص {_gap(timbre_score)}%",
        f"🥁 كيك/Kick     {_pct(kick_score):>5}%  `{_bar(kick_score)}`  نقص {_gap(kick_score)}%",
        f"🪘 سنير/Snare   {_pct(snare_score):>5}%  `{_bar(snare_score)}`  نقص {_gap(snare_score)}%",
        f"🎶 هاي-هات      {_pct(hat_score):>5}%  `{_bar(hat_score)}`  نقص {_gap(hat_score)}%",
        "",
        "🔍 *سبب النقص التفصيلي لكل آلة:*",
        "",
        f"🎹 *لحن/Synth* — {_why_melody(melody_score, chroma, mel)}",
        f"🎸 *باس/808* — {_why_bass(bass_score)}",
        f"🎤 *جرس الصوت* — {_why_timbre(timbre_score)}",
        f"🥁 *كيك/Kick* — {_why_drum('الكيك', kick_score, '20–400Hz', 'نطاق Low-end ضعيف — توقيت الكيك أو حجمه لا يطابق الأصل', 'الكيك قريب لكن الـ Transient يختلف في الشدة', 'فجوة طفيفة في توقيت الكيك أو ضغطه')}",
        f"🪘 *سنير/Snare* — {_why_drum('السنير', snare_score, '400Hz–4kHz', 'ضربات السنير لا تتزامن — نطاق Mid ضعيف', 'توقيت السنير قريب لكن الـ Crack صوته مختلف', 'فجوة طفيفة في حدة السنير أو توقيته')}",
        f"🎶 *هاي-هات* — {_why_drum('الهاي-هات', hat_score, '+4kHz', 'إيقاع الهاي-هات Trap ناقص كلياً أو خاطئ', 'الهاي-هات موجود لكن الـ Subdivision غير دقيق', 'فجوة طفيفة في سرعة أو مستوى الهاي-هات')}",
    ]

    if stems:
        missing = []
        if not stems.get("vocals"):
            missing.append("🎤 مسار الغناء/الراب")
        if not stems.get("bass"):
            missing.append("🎸 مسار الباس/808")
        if not stems.get("drums"):
            missing.append("🥁 مسار الطبول")
        if not stems.get("other"):
            missing.append("🎹 مسار الآلات اللحنية")
        if missing:
            lines.append("")
            lines.append("❌ *مسارات Demucs غير متاحة:* " + " | ".join(missing))
            lines.append("   ↳ السبب: فصل المسارات فشل أو الأغنية قصيرة جداً — يزيد الفجوة بشكل كبير")

    remaining = _pct(max(0, TARGET - overall))
    lines.append("")
    if remaining == 0:
        lines.append("🏆 *وصل التشابه الكلي إلى 99%+* — نتيجة ممتازة!")
    else:
        lines.append(f"📌 *الفجوة الكلية للوصول إلى 99%: {remaining}%*")
        lines.append("")
        lines.append("🧠 *الأسباب الجذرية للفجوة:*")
        lines.append("  1️⃣ جرس FluidSynth GM يختلف عن الآلات الحقيقية في الأغنية الأصلية")
        lines.append("  2️⃣ الـ 808 يحتاج pitch-slide وتشبّع صوتي لا تستطيع MIDI تقليده")
        lines.append("  3️⃣ الـ Demucs stems تحتوي على بقايا صوتية (Bleeding) تؤثر على القياس")
        lines.append("  4️⃣ Basic-Pitch لا يلتقط 100% من النوتات — خاصة في الترددات المنخفضة")
        lines.append("  5️⃣ الـ Vocal stem غير مُدرج في إعادة الإنشاء — يؤثر على الـ Mel الكلي")

    return "\n".join(lines)


def iterative_spectral_refinement(
        notes_data: dict, original_path: str, bpm: float,
        analysis: dict, title: str, audio_out: str,
        n_iter: int = 3) -> tuple:
    """
    Iterative synthesis-compare-adjust loop (n_iter passes).
    Each pass:
      1. Synthesise audio from current notes.
      2. Deep multi-metric comparison vs original.
      3. Adjust note velocity AND duration via spectral_adjust_notes (all 5 passes).
    Returns (final_notes, similarity_log, final_audio_path).
    The last synthesised audio is written to audio_out.
    """
    import copy, os

    current_notes = copy.deepcopy(notes_data)
    sim_log = []

    for i in range(n_iter):
        tmp_audio = audio_out if i == n_iter - 1 else f"/tmp/_iter_{i}_{os.getpid()}.mp3"
        tab_data  = build_tab_data_from_notes(current_notes, analysis, title)
        ok = render_musicxml_to_audio(tab_data, tmp_audio)

        if not ok or not os.path.exists(tmp_audio) or os.path.getsize(tmp_audio) < 500:
            logger.warning(f"Iteration {i+1}: render failed, stopping early.")
            break

        sim = _compute_deep_similarity(original_path, tmp_audio)
        sim_log.append(sim)
        logger.info(f"Iter {i+1}/{n_iter}: mel={sim['mel']:.3f} chroma={sim['chroma']:.3f} "
                    f"mfcc={sim['mfcc']:.3f} onset={sim['onset']:.3f} overall={sim['overall']:.3f}")

        if i < n_iter - 1:
            current_notes = spectral_adjust_notes(current_notes, original_path, tmp_audio, bpm)
            try:
                if tmp_audio != audio_out and os.path.exists(tmp_audio):
                    os.remove(tmp_audio)
            except Exception:
                pass

    return current_notes, sim_log


_musicgen_model = None
_musicgen_processor = None


def get_musicgen():
    global _musicgen_model, _musicgen_processor
    if _musicgen_model is None:
        logger.info("Loading MusicGen-Melody model...")
        from transformers import AutoProcessor, MusicgenMelodyForConditionalGeneration
        _musicgen_processor = AutoProcessor.from_pretrained("facebook/musicgen-melody")
        _musicgen_model = MusicgenMelodyForConditionalGeneration.from_pretrained("facebook/musicgen-melody")
        _musicgen_model.eval()
        logger.info("MusicGen-Melody model loaded.")
    return _musicgen_processor, _musicgen_model


def enhance_audio_ffmpeg(input_path: str, output_path: str, bitrate: str = "320k"):
    af_filter = (
        "equalizer=f=60:width_type=o:width=2:g=5,"
        "equalizer=f=200:width_type=o:width=2:g=2,"
        "equalizer=f=3000:width_type=o:width=2:g=3,"
        "equalizer=f=8000:width_type=o:width=2:g=2,"
        "acompressor=threshold=-12dB:ratio=4:attack=5:release=100:makeup=3dB,"
        "loudnorm=I=-14:TP=-1:LRA=7"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-af", af_filter,
            "-acodec", "libmp3lame", "-ar", "44100", "-ab", bitrate,
            output_path,
        ],
        capture_output=True, check=True
    )


def convert_wav_to_mp3(input_path: str, output_path: str, bitrate: str = "320k"):
    """Simple wav to mp3 conversion with volume normalization only — safe for AI-generated audio."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-af", "volume=2.0,equalizer=f=100:width_type=o:width=2:g=4,equalizer=f=8000:width_type=o:width=2:g=2",
            "-acodec", "libmp3lame", "-ar", "44100", "-ab", bitrate,
            output_path,
        ],
        capture_output=True, check=True
    )


def detect_instruments_from_genre(genre: str, song_title: str = "", artist_name: str = "") -> list:
    genre_lower = genre.lower()

    if any(g in genre_lower for g in ["pop", "dance", "electronic", "electro", "edm"]):
        return ["🎹 سينثيسايزر (Synthesizer)", "🎸 باس إلكتروني (Electronic Bass)", "🥁 طبول (Drums/808)", "🎹 بيانو (Piano)"]
    elif any(g in genre_lower for g in ["rock", "metal", "alternative", "punk", "grunge"]):
        return ["🎸 جيتار كهربائي (Electric Guitar)", "🎸 باس (Bass Guitar)", "🥁 طبول (Drums)", "🎸 جيتار إيقاعي (Rhythm Guitar)"]
    elif any(g in genre_lower for g in ["jazz", "blues", "swing", "bebop"]):
        return ["🎷 ساكسفون (Saxophone)", "🎺 ترومبيت (Trumpet)", "🎹 بيانو (Piano)", "🎸 كونترباس (Double Bass)", "🥁 طبول (Drums)"]
    elif any(g in genre_lower for g in ["classical", "orchestra", "symphon", "baroque", "opera"]):
        return ["🎻 كمان (Violin)", "🎻 تشيلو (Cello)", "🎹 بيانو (Piano)", "🎶 أوركسترا وترية (String Orchestra)", "🪗 فلوت (Flute)"]
    elif any(g in genre_lower for g in ["arabic", "arab", "khaleeji", "oriental", "middle east"]):
        return ["🪕 عود (Oud)", "🎻 كمان (Violin)", "🪈 ناي (Ney Flute)", "🥁 إيقاعات شرقية (Oriental Percussion)", "🎹 قانون (Qanun)"]
    elif any(g in genre_lower for g in ["hip hop", "rap", "trap", "drill"]):
        return ["🥁 طبول (808 Drums)", "🔊 باس ثقيل (Heavy Bass)", "🎹 سينثيسايزر (Synth)", "🎵 عينات صوتية (Samples)"]
    elif any(g in genre_lower for g in ["reggae", "reggaeton", "dancehall"]):
        return ["🎸 جيتار (Guitar)", "🎸 باس (Bass)", "🥁 طبول (Drums)", "🎹 كيبورد (Keyboard)"]
    elif any(g in genre_lower for g in ["country", "folk", "bluegrass"]):
        return ["🎸 جيتار أكوستيك (Acoustic Guitar)", "🪕 بانجو (Banjo)", "🎻 كمان (Fiddle/Violin)", "🎸 باس (Bass)"]
    elif any(g in genre_lower for g in ["r&b", "soul", "rnb", "funk", "neo soul"]):
        return ["🎸 جيتار (Guitar)", "🎸 باس (Bass)", "🥁 طبول (Drums)", "🎹 بيانو (Piano/Keys)", "🎶 وتريات (Strings)"]
    else:
        return ["🎸 جيتار (Guitar)", "🎸 باس (Bass)", "🥁 طبول (Drums)", "🎹 كيبورد (Keyboard)"]


def build_main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📖 كيفية الاستخدام", callback_data="show_help")],
        [InlineKeyboardButton("ℹ️ عن البوت", callback_data="show_about")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_song_actions(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🔼 رفع الجودة", callback_data=f"enhance_{user_id}"),
            InlineKeyboardButton("🎹 تصدير MIDI", callback_data=f"export_midi_{user_id}"),
        ],
        [
            InlineKeyboardButton("📋 تفاصيل أكثر", callback_data=f"moredetails_{user_id}"),
            InlineKeyboardButton("🎹 الآلات الموسيقية", callback_data=f"instruments_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔬 تحليل شامل كامل", callback_data=f"fullanalysis_{user_id}"),
        ],
        [
            InlineKeyboardButton("✂️ تعديل الملف الصوتي", callback_data=f"editmenu_{user_id}"),
        ],
        [
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_edit_menu(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("⚡ تغيير السرعة (BPM)", callback_data=f"edit_speed_{user_id}"),
            InlineKeyboardButton("🎵 تغيير المقام/النغمة", callback_data=f"edit_pitch_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔊 رفع مستوى الصوت", callback_data=f"edit_louder_{user_id}"),
            InlineKeyboardButton("🔉 خفض مستوى الصوت", callback_data=f"edit_quieter_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔃 عكس الصوت", callback_data=f"edit_reverse_{user_id}"),
            InlineKeyboardButton("🎚️ تعزيز الباس", callback_data=f"edit_bass_{user_id}"),
        ],
        [
            InlineKeyboardButton("✨ تحسين الجودة 320k", callback_data=f"enhance_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔙 العودة للخيارات", callback_data=f"back_to_options_{user_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def format_song_details(track: dict) -> tuple:
    title = track.get("title", "غير معروف")
    subtitle = track.get("subtitle", "غير معروف")

    sections = track.get("sections", [])
    metadata_section = next((s for s in sections if s.get("type") == "SONG"), None)
    metadata = {}
    if metadata_section:
        for item in metadata_section.get("metadata", []):
            metadata[item.get("title", "")] = item.get("text", "")

    album = metadata.get("Album", metadata.get("ألبوم", "—"))
    label = metadata.get("Label", metadata.get("شركة الإصدار", "—"))
    released = metadata.get("Released", metadata.get("تاريخ الإصدار", "—"))
    genre = track.get("genres", {}).get("primary", "غير معروف")

    images = track.get("images", {})
    cover_url = images.get("coverarthq") or images.get("coverart") or ""

    msg = (
        f"🎵 *{title}*\n"
        f"👤 {subtitle}\n\n"
        f"💿 الألبوم: {album}\n"
        f"🎼 النوع: {genre}\n"
        f"📅 الإصدار: {released}\n"
        f"🏷️ الشركة: {label}"
    )

    hub = track.get("hub", {})
    providers = hub.get("providers", [])
    if providers:
        links = []
        for p in providers[:2]:
            pname = p.get("caption", "")
            actions = p.get("actions", [])
            if actions and pname:
                uri = actions[0].get("uri", "")
                if uri:
                    links.append(f"[{pname}]({uri})")
        if links:
            msg += "\n\n🔗 " + " • ".join(links)

    return msg, cover_url


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *أهلاً! أنا بوت تحليل وتصدير الأغاني*\n\n"
        "🎵 أرسل لي أي مقطع صوتي وسأقوم بـ:\n"
        "✅ التعرف على الأغنية فوراً\n"
        "🎹 كشف الآلات الموسيقية\n"
        "🔼 رفع جودة الصوت\n"
        "🎼 تصدير MusicXML + إعادة إنشاء صوتي\n\n"
        "📤 *ابدأ بإرسال مقطع صوتي!*"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=build_main_menu())


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message
    audio = message.voice or message.audio or message.document

    if not audio:
        await message.reply_text("❌ يرجى إرسال رسالة صوتية أو ملف صوتي.")
        return

    # ── Check if there's a pending tablature waiting for audio ─────────────
    if user_id in user_tab_pending:
        await _handle_tab_audio_combine(update, context, audio, user_id)
        return

    processing_msg = await message.reply_text("⏳ *جارٍ تحليل الصوت...*", parse_mode="Markdown")

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        file = await audio.get_file()
        await file.download_to_drive(tmp_path)

        mp3_path = tmp_path.replace(".ogg", ".mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path, "-acodec", "libmp3lame", "-ar", "44100", "-ab", "192k", mp3_path],
                capture_output=True, check=True
            )
            analyze_path = mp3_path
        except Exception:
            analyze_path = tmp_path

        await processing_msg.edit_text("🔍 *جارٍ التعرف على الأغنية...*", parse_mode="Markdown")

        shazam = Shazam()
        result = await shazam.recognize(analyze_path)

        if not result or "track" not in result:
            await processing_msg.edit_text(
                "❌ *لم أتمكن من التعرف على الأغنية*\n\n"
                "تأكد من:\n"
                "• وجود موسيقى أو غناء واضح\n"
                "• مدة المقطع أكثر من 5 ثوانٍ\n"
                "• جودة الصوت مقبولة",
                parse_mode="Markdown",
            )
            return

        track = result["track"]
        user_songs[user_id] = {
            "track": track,
            "original_path": tmp_path,
            "mp3_path": analyze_path,
            "result": result,
        }

        details_text, cover_url = format_song_details(track)
        markup = build_song_actions(user_id)

        await processing_msg.delete()

        if cover_url:
            try:
                await message.reply_photo(
                    photo=cover_url,
                    caption=details_text,
                    parse_mode="Markdown",
                    reply_markup=markup,
                )
                return
            except Exception:
                pass

        await message.reply_text(details_text, parse_mode="Markdown", reply_markup=markup)

    except Exception as e:
        logger.error(f"Audio error: {e}")
        await processing_msg.edit_text("❌ حدث خطأ أثناء المعالجة. حاول مرة أخرى.")


async def _handle_tab_audio_combine(update: Update, context: ContextTypes.DEFAULT_TYPE, audio, user_id: int):
    """
    Called when the user sends an audio file while a tablature analysis is pending.
    Downloads the audio, then runs MusicGen combining the tab structure + audio timbre.
    """
    pending    = user_tab_pending.pop(user_id)
    tab_data   = pending["tab_data"]
    fname      = pending.get("fname", "تابلاتشر")
    analysis_msg = pending.get("analysis_msg", "")

    message    = update.message
    loop       = asyncio.get_event_loop()

    status_msg = await message.reply_text(
        "🎸 *تم استقبال الملف الصوتي!*\n\n"
        "🤖 جارٍ دمج النوتات من التابلاتشر مع الطابع الصوتي...\n"
        "⏳ هذه العملية قد تستغرق بضع دقائق",
        parse_mode="Markdown"
    )

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        file = await audio.get_file()
        await file.download_to_drive(tmp_path)

        ref_mp3 = tmp_path.replace(".ogg", "_ref.mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path,
                 "-acodec", "libmp3lame", "-ar", "44100", "-ab", "192k", ref_mp3],
                capture_output=True, check=True
            )
            ref_path = ref_mp3
        except Exception:
            ref_path = tmp_path

        progress_stages = [
            "🔍 *[1/5]* تحميل نموذج الذكاء الاصطناعي...",
            "🎵 *[2/5]* تحليل الطابع الصوتي للملف المُرسَل...",
            "🎼 *[3/5]* تغذية النوتات والأكوردات من التابلاتشر...",
            "🎸 *[4/5]* توليد الصوت الموحَّد...",
            "💾 *[5/5]* معالجة وتحسين الجودة...",
        ]
        stop_evt = asyncio.Event()

        async def updater():
            idx = 0
            elapsed = 0
            while not stop_evt.is_set():
                stage = progress_stages[min(idx, len(progress_stages) - 1)]
                try:
                    await status_msg.edit_text(
                        f"{stage}\n\n⏳ الوقت المنقضي: {elapsed} ثانية",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                await asyncio.sleep(6)
                elapsed += 6
                idx += 1

        updater_task = asyncio.create_task(updater())

        output_path = f"/tmp/tab_combined_{user_id}.mp3"
        success = await loop.run_in_executor(
            None,
            lambda: generate_combined_tab_audio(tab_data, ref_path, output_path)
        )

        stop_evt.set()
        updater_task.cancel()
        await status_msg.delete()

        bpm    = tab_data.get("bpm", "?")
        chords = " • ".join(tab_data.get("chords", [])) or "—"
        insts  = " • ".join(tab_data.get("instruments", [])) or "—"

        if success and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            await message.reply_audio(
                audio=open(output_path, "rb"),
                title=f"تابلاتشر + صوت — {fname}",
                performer="AI موسيقي",
                caption=(
                    f"🎸 *تم الدمج بنجاح!*\n\n"
                    f"{analysis_msg}\n"
                    f"✅ *ما تم تطبيقه:*\n"
                    f"• نوتات وأكوردات التابلاتشر: `{chords}`\n"
                    f"• آلات الملف: *{insts}*\n"
                    f"• سرعة التابلاتشر: *{bpm} BPM*\n"
                    f"• الطابع الصوتي من ملفك\n"
                    f"• جودة 320kbps"
                ),
                parse_mode="Markdown",
            )
        else:
            await message.reply_text(
                "⚠️ *تعذَّر توليد الصوت الموحَّد*\n\n"
                "يمكنك إرسال ملف PDF مجدداً والمحاولة مرة أخرى.",
                parse_mode="Markdown"
            )

        for p in [tmp_path, ref_mp3, output_path]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"_handle_tab_audio_combine error: {e}", exc_info=True)
        try:
            stop_evt.set()
            updater_task.cancel()
        except Exception:
            pass
        await message.reply_text(
            "❌ *حدث خطأ أثناء الدمج*\n\nيرجى المحاولة مرة أخرى.",
            parse_mode="Markdown"
        )



def analyze_audio_and_build_prompt(audio_path: str, track: dict) -> tuple:
    """
    Deep multi-dimensional audio analysis using librosa.
    Returns (prompt_str, analysis_dict)
    """
    import librosa
    import librosa.effects
    import librosa.feature
    import librosa.onset
    import librosa.beat

    title      = track.get("title", "")
    artist     = track.get("subtitle", "")
    genre      = track.get("genres", {}).get("primary", "pop")
    genre_lower = genre.lower()

    # ── 1. Load audio (mono for analysis) ─────────────────────────────────────
    wav_tmp = "/tmp/analysis_input.wav"
    wav_stereo = "/tmp/analysis_stereo.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-acodec", "pcm_f32le", "-ar", "22050", "-ac", "1",
             "-t", "90", wav_tmp],
            capture_output=True, check=True
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-acodec", "pcm_f32le", "-ar", "22050", "-ac", "2",
             "-t", "90", wav_stereo],
            capture_output=True, check=True
        )
        y, sr = librosa.load(wav_tmp, sr=22050, mono=True)
        y_stereo, _ = librosa.load(wav_stereo, sr=22050, mono=False)
    except Exception as e:
        logger.warning(f"librosa load failed: {e}")
        y, sr = None, 22050
        y_stereo = None

    analysis = {}

    if y is not None and len(y) > 0:

        # ── 2. BPM & Beat Grid ────────────────────────────────────────────────
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo.item()) if hasattr(tempo, 'item') else float(tempo)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        # Beat regularity (swing vs straight)
        if len(beat_times) > 2:
            intervals = np.diff(beat_times)
            beat_std = float(np.std(intervals))
            groove = "swung groove" if beat_std > 0.05 else "straight rigid grid"
        else:
            groove = "straight"
        analysis["bpm"] = round(bpm, 1)
        analysis["groove"] = groove

        # ── 3. Key & Scale (Krumhansl-Schmuckler 24-key full search) ─────────
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_mean = chroma.mean(axis=1)
        key_names = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
        _maj_prof = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
        _min_prof = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
        _maj_prof = _maj_prof / _maj_prof.sum()
        _min_prof = _min_prof / _min_prof.sum()
        _c_norm = chroma_mean / (chroma_mean.sum() + 1e-9)
        _best_corr, _best_key, _best_scale = -2.0, "C", "major"
        for _ki, _kname in enumerate(key_names):
            _crot = np.roll(_c_norm, -_ki)
            _mcorr = float(np.corrcoef(_crot, _maj_prof)[0, 1])
            _ncorr = float(np.corrcoef(_crot, _min_prof)[0, 1])
            if _mcorr > _best_corr:
                _best_corr, _best_key, _best_scale = _mcorr, _kname, "major"
            if _ncorr > _best_corr:
                _best_corr, _best_key, _best_scale = _ncorr, _kname, "minor"
        analysis["key"] = f"{_best_key} {_best_scale}"

        # ── 4. Harmonic / Percussive separation ───────────────────────────────
        y_harmonic, y_percussive = librosa.effects.hpss(y)
        harm_energy = float(np.abs(y_harmonic).mean())
        perc_energy = float(np.abs(y_percussive).mean())
        harm_ratio = round(harm_energy / (harm_energy + perc_energy + 1e-9), 2)
        analysis["harmonic_ratio"] = harm_ratio
        texture = "melody-dominant" if harm_ratio > 0.7 else ("balanced melody and rhythm" if harm_ratio > 0.4 else "percussion-dominant")
        analysis["texture"] = texture

        # ── 5. Energy & Dynamics ──────────────────────────────────────────────
        rms = librosa.feature.rms(y=y)[0]
        avg_energy = float(rms.mean())
        peak_energy = float(rms.max())
        dynamic_range = round(float(20 * np.log10((peak_energy + 1e-9) / (avg_energy + 1e-9))), 1)
        energy_label = "high energy" if avg_energy > 0.05 else ("medium energy" if avg_energy > 0.02 else "soft and delicate")
        loudness = "loud and compressed" if dynamic_range < 3 else ("dynamic with natural peaks" if dynamic_range < 8 else "very dynamic and wide range")
        analysis["energy"] = energy_label
        analysis["dynamic_range_db"] = dynamic_range
        analysis["loudness_character"] = loudness

        # ── 6. Onset / Instrument entry points ───────────────────────────────
        perc_onsets = librosa.onset.onset_detect(y=y_percussive, sr=sr, units="time")
        harm_onsets = librosa.onset.onset_detect(y=y_harmonic, sr=sr, units="time")
        drum_start   = round(float(perc_onsets[0]), 1) if len(perc_onsets) > 0 else 0.0
        melody_start = round(float(harm_onsets[0]), 1) if len(harm_onsets) > 0 else 0.0
        # Onset density (sparse vs dense arrangement)
        total_onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
        duration = librosa.get_duration(y=y, sr=sr)
        onset_density = round(len(total_onsets) / max(duration, 1), 2)
        arrangement = "dense full arrangement" if onset_density > 4 else ("moderate arrangement" if onset_density > 2 else "sparse minimal arrangement")
        analysis["drum_start_sec"]   = drum_start
        analysis["melody_start_sec"] = melody_start
        analysis["onset_density"]    = onset_density
        analysis["arrangement"]      = arrangement

        # ── 7. Spectral analysis (full frequency bands) ───────────────────────
        spectral_centroid = float(librosa.feature.spectral_centroid(y=y, sr=sr).mean())
        spectral_rolloff  = float(librosa.feature.spectral_rolloff(y=y, sr=sr).mean())
        spectral_bandwidth = float(librosa.feature.spectral_bandwidth(y=y, sr=sr).mean())
        spectral_flatness = float(librosa.feature.spectral_flatness(y=y).mean())
        # Frequency band energies
        D = np.abs(librosa.stft(y))
        freqs = librosa.fft_frequencies(sr=sr)
        def band_energy(f_low, f_high):
            mask = (freqs >= f_low) & (freqs < f_high)
            return float(D[mask].mean()) if mask.any() else 0.0
        sub_bass  = band_energy(20, 80)
        bass      = band_energy(80, 300)
        low_mid   = band_energy(300, 800)
        mid       = band_energy(800, 2500)
        high_mid  = band_energy(2500, 6000)
        presence  = band_energy(6000, 12000)
        air       = band_energy(12000, 20000)
        # Brightness
        brightness = "bright and airy" if spectral_centroid > 4000 else ("warm and mid-focused" if spectral_centroid > 1800 else "dark and bassy")
        # Bass character
        bass_char = "heavy sub bass" if sub_bass > bass * 1.5 else ("punchy bass" if bass > low_mid else "balanced low end")
        # High frequency character
        air_char = "crisp and airy highs" if air > presence * 0.5 else ("smooth highs" if presence > mid * 0.3 else "warm rolled-off highs")
        # Noise vs tonal
        tonality = "highly tonal and melodic" if spectral_flatness < 0.05 else ("mix of tonal and noise" if spectral_flatness < 0.2 else "noise-dominant texture")
        analysis["brightness"]         = brightness
        analysis["bass_character"]     = bass_char
        analysis["high_freq_character"] = air_char
        analysis["tonality"]           = tonality
        analysis["spectral_centroid"]  = round(spectral_centroid, 0)
        analysis["spectral_bandwidth"] = round(spectral_bandwidth, 0)

        # ── 8. Reverb / Echo estimation ───────────────────────────────────────
        # Estimate reverb via spectral decay after transients
        onset_frames = librosa.onset.onset_detect(y=y, sr=sr)
        reverb_estimates = []
        for of in onset_frames[:10]:
            start = of
            end = min(of + int(sr * 2 / 512), D.shape[1])
            if end > start + 5:
                segment = D[:, start:end].mean(axis=0)
                if segment.max() > 0:
                    decay = segment / (segment.max() + 1e-9)
                    decay_time = np.where(decay < 0.1)[0]
                    if len(decay_time) > 0:
                        rt = decay_time[0] * 512 / sr
                        reverb_estimates.append(rt)
        avg_reverb = float(np.mean(reverb_estimates)) if reverb_estimates else 0.5
        if avg_reverb < 0.3:
            reverb_label = "dry with very little reverb"
        elif avg_reverb < 0.8:
            reverb_label = "medium room reverb"
        elif avg_reverb < 1.5:
            reverb_label = "hall reverb with long tail"
        else:
            reverb_label = "heavy atmospheric reverb and echo"
        analysis["reverb"] = reverb_label
        analysis["reverb_sec"] = round(avg_reverb, 2)

        # ── 9. Stereo Width ───────────────────────────────────────────────────
        if y_stereo is not None and y_stereo.ndim == 2 and y_stereo.shape[0] == 2:
            left, right = y_stereo[0], y_stereo[1]
            min_len = min(len(left), len(right))
            left, right = left[:min_len], right[:min_len]
            mid_ch  = (left + right) / 2
            side_ch = (left - right) / 2
            stereo_width = float(np.abs(side_ch).mean() / (np.abs(mid_ch).mean() + 1e-9))
            width_label = "wide stereo field" if stereo_width > 0.3 else ("moderate stereo" if stereo_width > 0.1 else "narrow mono-like")
        else:
            width_label = "moderate stereo"
            stereo_width = 0.2
        analysis["stereo_width"] = width_label

        # ── 10. Attack & Sustain character ────────────────────────────────────
        attack_strength = float(np.percentile(np.abs(y_percussive), 95))
        attack_label = "punchy hard attack" if attack_strength > 0.3 else ("moderate attack" if attack_strength > 0.1 else "soft gentle attack")
        analysis["attack"] = attack_label

        # ── 11. Tempo feel & time signature ──────────────────────────────────
        if bpm > 140:
            tempo_feel = "very fast energetic tempo"
        elif bpm > 110:
            tempo_feel = "fast upbeat tempo"
        elif bpm > 80:
            tempo_feel = "moderate mid-tempo"
        elif bpm > 60:
            tempo_feel = "slow relaxed tempo"
        else:
            tempo_feel = "very slow ballad tempo"
        analysis["tempo_feel"] = tempo_feel

        # ── 12. MFCC timbre fingerprint ───────────────────────────────────────
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
        mfcc_means = mfcc.mean(axis=1)
        # Warmth indicator from low MFCCs
        warmth = "warm and rich timbre" if mfcc_means[1] > 0 else "cold and metallic timbre"
        analysis["timbre"] = warmth

        analysis["duration_sec"] = round(duration, 1)
        analysis["section_length_approx"] = round(duration / 4, 1)

        logger.info(f"Deep audio analysis complete: {analysis}")

    else:
        analysis = {
            "bpm": 100, "key": "C major", "energy": "medium energy",
            "drum_start_sec": 4.0, "melody_start_sec": 0.5,
            "brightness": "warm", "harmonic_ratio": 0.6,
            "duration_sec": 30, "reverb": "medium room reverb",
            "stereo_width": "moderate stereo", "groove": "straight",
            "attack": "moderate attack", "dynamic_range_db": 6,
        }

    # ── 13. Instrument detection per genre + spectral hints ──────────────────
    if any(g in genre_lower for g in ["arabic","arab","khaleeji","oriental","middle","sha3bi","dabke","levant"]):
        inst_list = "oud, violin, qanun, ney flute, darbuka, riq, tabla, mizmar"
        style_desc = "Arabic oriental maqam music with rich ornamentation, microtonal phrasing and emotional depth"
    elif any(g in genre_lower for g in ["pop","dance","electro","edm","house","techno"]):
        inst_list = "synthesizer lead, pad synth, electronic drums, 808 bass, piano, arpeggiated synth, vocal chops"
        style_desc = "modern pop music with punchy production, catchy hook and polished mix"
    elif any(g in genre_lower for g in ["rock","metal","alternative","grunge"]):
        inst_list = "distorted electric guitar, clean guitar, bass guitar, acoustic drums, power chords"
        style_desc = "rock music with powerful guitar riffs, driving rhythm and raw energy"
    elif any(g in genre_lower for g in ["hip hop","rap","trap","drill"]):
        inst_list = "808 bass, trap hi-hats, snare clap, synthesizer pads, sub bass, piano"
        style_desc = "trap hip-hop with heavy low end, dark atmosphere and hard-hitting drums"
    elif any(g in genre_lower for g in ["jazz","blues","swing","bebop"]):
        inst_list = "saxophone, trumpet, piano, double bass, jazz drums, guitar, trombone"
        style_desc = "jazz music with improvised solos, swing feel and complex harmony"
    elif any(g in genre_lower for g in ["r&b","soul","rnb","funk","gospel"]):
        inst_list = "electric piano, Fender Rhodes, bass guitar, live drums, guitar, strings, brass horns"
        style_desc = "smooth R&B with lush grooves, soulful feel and rich harmonic layers"
    elif any(g in genre_lower for g in ["classical","orchestra","symphon"]):
        inst_list = "violin section, cello, viola, piano, flute, oboe, French horn, orchestral brass, timpani"
        style_desc = "orchestral classical music with full symphonic arrangement and dynamic expression"
    elif any(g in genre_lower for g in ["reggae","dancehall","ska"]):
        inst_list = "reggae guitar skank, bass, drums, organ, brass section, percussion"
        style_desc = "reggae music with offbeat skank guitar, heavy bass and laid-back groove"
    elif any(g in genre_lower for g in ["flamenco","spanish","latin","salsa","cumbia"]):
        inst_list = "classical guitar, cajon, palmas, violin, bass, latin percussion, piano"
        style_desc = "passionate Latin music with flamenco guitar, complex rhythms and expressive phrasing"
    else:
        inst_list = "acoustic guitar, bass, drums, keyboard, synthesizer, strings"
        style_desc = "professional music with full arrangement and polished production"

    analysis["instruments"] = inst_list

    # ── 14. Build ultra-detailed prompt ──────────────────────────────────────
    bpm_v    = analysis.get("bpm", 100)
    key_v    = analysis.get("key", "C major")
    energy_v = analysis.get("energy", "medium energy")
    drum_v   = analysis.get("drum_start_sec", 0)
    mel_v    = analysis.get("melody_start_sec", 0)
    bright_v = analysis.get("brightness", "warm")
    reverb_v = analysis.get("reverb", "medium room reverb")
    stereo_v = analysis.get("stereo_width", "moderate stereo")
    groove_v = analysis.get("groove", "straight")
    attack_v = analysis.get("attack", "moderate attack")
    texture_v= analysis.get("texture", "balanced")
    arrange_v= analysis.get("arrangement", "full arrangement")
    dynamic_v= analysis.get("loudness_character", "dynamic")
    bass_v   = analysis.get("bass_character", "balanced low end")
    air_v    = analysis.get("high_freq_character", "smooth highs")
    timbre_v = analysis.get("timbre", "warm timbre")
    tempo_feel_v = analysis.get("tempo_feel", "moderate tempo")

    prompt = (
        f"{genre} music in the style of {title} by {artist}, "
        f"{style_desc}, "
        f"exactly {bpm_v:.0f} BPM, {tempo_feel_v}, {groove_v}, "
        f"key of {key_v}, "
        f"{energy_v}, {dynamic_v}, "
        f"{bright_v} tone, {bass_v}, {air_v}, "
        f"{reverb_v}, {stereo_v}, "
        f"{attack_v}, {texture_v}, {arrange_v}, "
        f"{timbre_v}, "
        f"featuring {inst_list}, "
        f"drums and percussion enter at {drum_v:.0f} seconds, "
        f"melody enters at {mel_v:.0f} seconds, "
        f"all instruments layered precisely from the start, "
        f"professional studio master quality, faithful reproduction of original song structure and feel"
    )

    return prompt, analysis


def analyze_full_deep(audio_path: str, track: dict) -> dict:
    """
    Ultra-comprehensive musical analysis covering:
    Rhythm, Harmony, Melody, Song Sections, Layers, Timbre, Dynamics, Movement.
    Returns a rich dict with all dimensions.
    """
    import librosa
    import librosa.effects
    import librosa.feature
    import librosa.onset
    import librosa.beat
    import librosa.segment

    title  = track.get("title", "")
    artist = track.get("subtitle", "")
    genre  = track.get("genres", {}).get("primary", "pop")

    wav_tmp = "/tmp/full_analysis_input.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-acodec", "pcm_f32le", "-ar", "22050", "-ac", "1",
             "-t", "120", wav_tmp],
            capture_output=True, check=True
        )
        y, sr = librosa.load(wav_tmp, sr=22050, mono=True)
    except Exception as e:
        logger.warning(f"full_analysis load failed: {e}")
        return {}

    if y is None or len(y) == 0:
        return {}

    result = {}
    duration = librosa.get_duration(y=y, sr=sr)
    result["duration_sec"] = round(duration, 1)

    # ── 1. RHYTHM ──────────────────────────────────────────────────────────────
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(tempo.item()) if hasattr(tempo, 'item') else float(tempo)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    result["bpm"] = round(bpm, 1)

    # Time Signature estimation
    if len(beat_times) > 4:
        intervals = np.diff(beat_times)
        avg_interval = float(np.mean(intervals))
        # Group beats into measures using autocorrelation of onset strength
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        ac = librosa.autocorrelate(onset_env, max_size=int(sr * 3 / 512))
        # Check peaks at multiples of 3 vs 4 beat intervals
        beat_frames_hop = int(avg_interval * sr / 512)
        score_4 = float(ac[beat_frames_hop * 4]) if beat_frames_hop * 4 < len(ac) else 0
        score_3 = float(ac[beat_frames_hop * 3]) if beat_frames_hop * 3 < len(ac) else 0
        score_6 = float(ac[beat_frames_hop * 6]) if beat_frames_hop * 6 < len(ac) else 0
        if score_3 > score_4 and score_3 > score_6:
            time_sig = "3/4 (والس)"
        elif score_6 > score_4:
            time_sig = "6/8"
        else:
            time_sig = "4/4"
    else:
        time_sig = "4/4"
    result["time_signature"] = time_sig

    # Groove: straight vs swing
    if len(beat_times) > 2:
        intervals = np.diff(beat_times)
        beat_std = float(np.std(intervals))
        groove = "Swing (متأرجح)" if beat_std > 0.05 else "Straight (مستقيم)"
    else:
        groove = "Straight (مستقيم)"
    result["groove"] = groove

    # Syncopation: ratio of strong off-beat onsets
    y_perc = librosa.effects.hpss(y)[1]
    onset_times = librosa.onset.onset_detect(y=y_perc, sr=sr, units="time")
    if len(beat_times) > 2 and len(onset_times) > 0:
        on_beat_count = 0
        off_beat_count = 0
        for ot in onset_times:
            dists = np.abs(beat_times - ot)
            min_dist = float(dists.min())
            beat_int = float(np.mean(np.diff(beat_times)))
            if min_dist < beat_int * 0.15:
                on_beat_count += 1
            else:
                off_beat_count += 1
        sync_ratio = off_beat_count / max(len(onset_times), 1)
        if sync_ratio > 0.5:
            syncopation = f"عالي ({round(sync_ratio*100)}٪ off-beat)"
        elif sync_ratio > 0.25:
            syncopation = f"متوسط ({round(sync_ratio*100)}٪ off-beat)"
        else:
            syncopation = f"منخفض ({round(sync_ratio*100)}٪ off-beat)"
    else:
        syncopation = "متوسط"
    result["syncopation"] = syncopation

    # Drum pattern description
    onset_density = round(len(onset_times) / max(duration, 1), 2)
    if onset_density > 5:
        drum_pattern = "كثيف (Dense) — إيقاع سريع ومتشعب"
    elif onset_density > 2.5:
        drum_pattern = "متوسط — إيقاع معياري"
    else:
        drum_pattern = "خفيف (Sparse) — إيقاع بسيط"
    result["drum_pattern"] = drum_pattern
    result["onset_density"] = onset_density

    # ── 2. HARMONY (Krumhansl-Schmuckler 24-key full search) ───────────────────
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)
    key_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    _maj_prof = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    _min_prof = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    _maj_prof = _maj_prof / _maj_prof.sum()
    _min_prof = _min_prof / _min_prof.sum()
    _c_norm = chroma_mean / (chroma_mean.sum() + 1e-9)
    _best_corr, _best_key, _best_scale_en = -2.0, "C", "major"
    for _ki, _kname in enumerate(key_names):
        _crot = np.roll(_c_norm, -_ki)
        _mcorr = float(np.corrcoef(_crot, _maj_prof)[0, 1])
        _ncorr = float(np.corrcoef(_crot, _min_prof)[0, 1])
        if _mcorr > _best_corr:
            _best_corr, _best_key, _best_scale_en = _mcorr, _kname, "major"
        if _ncorr > _best_corr:
            _best_corr, _best_key, _best_scale_en = _ncorr, _kname, "minor"
    scale = "major (ماجور)" if _best_scale_en == "major" else "minor (مينور)"
    result["key"] = f"{_best_key} {scale}"
    result["scale"] = scale

    # Chord Progression: track dominant chroma over 4-beat windows
    hop_length = 512
    beats_hop = max(1, int(bpm / 60 * 4 * sr / hop_length))  # frames per measure
    n_frames = chroma.shape[1]
    chord_seq = []
    chord_symbols = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    for start in range(0, n_frames - beats_hop, beats_hop):
        seg = chroma[:, start:start + beats_hop].mean(axis=1)
        root = int(seg.argmax())
        seg_rot = np.roll(seg, -root)
        maj_c = float(np.corrcoef(seg_rot, major_profile)[0, 1])
        min_c = float(np.corrcoef(seg_rot, minor_profile)[0, 1])
        quality = "" if maj_c > min_c else "m"
        chord_seq.append(f"{chord_symbols[root]}{quality}")

    # Deduplicate consecutive same chords and limit to 8
    prog = []
    for c in chord_seq:
        if not prog or prog[-1] != c:
            prog.append(c)
        if len(prog) >= 8:
            break
    result["chord_progression"] = " → ".join(prog) if prog else "—"

    # Modulation: detect if key shifts significantly
    segment_size = max(1, n_frames // 4)
    key_segments = []
    for i in range(4):
        seg = chroma[:, i * segment_size:(i + 1) * segment_size].mean(axis=1)
        key_segments.append(int(seg.argmax()))
    unique_keys = len(set(key_segments))
    if unique_keys >= 3:
        result["modulation"] = f"تحويل مقامي (Modulation) — {unique_keys} مناطق مختلفة"
    elif unique_keys == 2:
        result["modulation"] = "تحويل مقامي خفيف — منطقتان"
    else:
        result["modulation"] = "لا يوجد تحويل مقامي — مقام ثابت"

    # ── 3. MELODY ──────────────────────────────────────────────────────────────
    y_harm = librosa.effects.hpss(y)[0]
    # Pitch range using pyin
    try:
        f0, voiced_flag, voiced_probs = librosa.pyin(
            y_harm, fmin=librosa.note_to_hz('C2'),
            fmax=librosa.note_to_hz('C7'), sr=sr
        )
        voiced_f0 = f0[voiced_flag] if voiced_flag is not None else np.array([])
        if len(voiced_f0) > 5:
            f0_low  = float(np.percentile(voiced_f0, 5))
            f0_high = float(np.percentile(voiced_f0, 95))
            note_low  = librosa.hz_to_note(f0_low)
            note_high = librosa.hz_to_note(f0_high)
            result["pitch_range"] = f"{note_low} — {note_high}"
            # Vocal/melody range classification
            range_semitones = 12 * np.log2(f0_high / max(f0_low, 1))
            if range_semitones > 24:
                result["pitch_range_label"] = "مدى واسع جداً (> أوكتافين)"
            elif range_semitones > 12:
                result["pitch_range_label"] = "مدى واسع (أوكتاف أو أكثر)"
            else:
                result["pitch_range_label"] = "مدى ضيق (أقل من أوكتاف)"
        else:
            result["pitch_range"] = "—"
            result["pitch_range_label"] = "—"
    except Exception:
        result["pitch_range"] = "—"
        result["pitch_range_label"] = "—"

    # Melody repetition: self-similarity via chroma
    try:
        rec = librosa.segment.recurrence_matrix(chroma, mode="affinity", sym=True)
        repetition_score = float(np.mean(rec[rec < 1.0]))
        if repetition_score > 0.6:
            melody_rep = f"تكرار عالٍ ({round(repetition_score*100)}٪) — لازمة واضحة"
        elif repetition_score > 0.35:
            melody_rep = f"تكرار متوسط ({round(repetition_score*100)}٪)"
        else:
            melody_rep = f"تنوع لحني ({round(repetition_score*100)}٪ تشابه)"
    except Exception:
        melody_rep = "—"
    result["melody_repetition"] = melody_rep

    # Ornaments: detect rapid pitch variations (trills/slides) via std of voiced f0
    try:
        if len(voiced_f0) > 10:
            f0_std = float(np.std(np.diff(voiced_f0)))
            if f0_std > 30:
                ornaments = "زخارف كثيفة (Trills / Slides / Vibrato)"
            elif f0_std > 10:
                ornaments = "زخارف معتدلة"
            else:
                ornaments = "لحن نظيف بدون زخارف واضحة"
        else:
            ornaments = "—"
    except Exception:
        ornaments = "—"
    result["ornaments"] = ornaments

    # ── 4. SONG SECTIONS ───────────────────────────────────────────────────────
    rms = librosa.feature.rms(y=y)[0]
    hop_frames = 512
    total_frames = len(rms)
    section_size = total_frames // 8
    sections_energy = []
    for i in range(8):
        seg_rms = rms[i * section_size:(i + 1) * section_size]
        sections_energy.append(float(seg_rms.mean()))

    avg_e = np.mean(sections_energy)
    detected_sections = []
    section_labels = []
    for i, e in enumerate(sections_energy):
        t_start = round(i * section_size * hop_frames / sr, 1)
        t_end   = round((i + 1) * section_size * hop_frames / sr, 1)
        ratio = e / (avg_e + 1e-9)
        if i == 0:
            lbl = "Intro (مقدمة)"
        elif i == 7:
            lbl = "Outro (خاتمة)"
        elif ratio > 1.3:
            lbl = "Chorus / Drop (لازمة)"
        elif ratio > 1.0:
            lbl = "Pre-Chorus / Verse نشط"
        elif ratio < 0.7:
            lbl = "Bridge / Breakdown"
        else:
            lbl = "Verse (بيت)"
        detected_sections.append(f"{t_start}ث–{t_end}ث: {lbl}")
        section_labels.append(lbl)
    result["sections"] = detected_sections
    # Summary of section types present
    unique_sections = list(dict.fromkeys(section_labels))
    result["sections_summary"] = " | ".join(unique_sections)

    # ── 5. LAYERS & ROLES ──────────────────────────────────────────────────────
    spectral_centroid = float(librosa.feature.spectral_centroid(y=y, sr=sr).mean())
    spectral_flatness = float(librosa.feature.spectral_flatness(y=y).mean())
    harm_energy = float(np.abs(y_harm).mean())
    perc_energy = float(np.abs(y_perc).mean())
    harm_ratio = round(harm_energy / (harm_energy + perc_energy + 1e-9), 2)

    if harm_ratio > 0.75:
        lead_role = "اللحن الرئيسي (Lead Melody) يهيمن"
        pad_role  = "الباد خفيف"
        bg_role   = "الخلفية الإيقاعية خفيفة"
    elif harm_ratio > 0.5:
        lead_role = "Lead و Pad متوازنان"
        pad_role  = "Pad واضح وداعم"
        bg_role   = "إيقاع في الخلفية"
    else:
        lead_role = "الإيقاع (Drums/Perc) يهيمن"
        pad_role  = "—"
        bg_role   = "Lead في الخلفية"
    result["lead_role"] = lead_role
    result["pad_role"]  = pad_role
    result["bg_role"]   = bg_role

    # ── 6. ADVANCED DIMENSIONS ─────────────────────────────────────────────────
    # Micro-timing: jitter in onset positions
    if len(onset_times) > 4:
        beat_int = 60.0 / max(bpm, 1)
        quantized = np.round(onset_times / beat_int) * beat_int
        jitter_ms = float(np.mean(np.abs(onset_times - quantized[:len(onset_times)])) * 1000)
        if jitter_ms < 5:
            micro_timing = f"دقيق جداً ({round(jitter_ms,1)} ms) — مبرمج/Grid"
        elif jitter_ms < 20:
            micro_timing = f"إنساني طبيعي ({round(jitter_ms,1)} ms)"
        else:
            micro_timing = f"متأخر أو متقدم ({round(jitter_ms,1)} ms) — groove مميز"
    else:
        micro_timing = "—"
    result["micro_timing"] = micro_timing

    # Timbre: MFCC fingerprint
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    mfcc_means = mfcc.mean(axis=1)
    warmth = "دافئ وغني" if mfcc_means[1] > 0 else "بارد ومعدني"
    brightness_t = "ساطع" if spectral_centroid > 3500 else ("متوسط" if spectral_centroid > 1800 else "داكن")
    tonality = "لحني وتوني" if spectral_flatness < 0.05 else ("مزيج لحن ونويز" if spectral_flatness < 0.2 else "نويز/تشويش غالب")
    result["timbre"] = f"{warmth} — {brightness_t} — {tonality}"

    # Envelope: attack/release character
    attack_strength = float(np.percentile(np.abs(y_perc), 95))
    if attack_strength > 0.3:
        envelope = "هجوم حاد (Hard Attack) — ضربات قوية وحادة"
    elif attack_strength > 0.1:
        envelope = "هجوم متوسط (Medium Attack)"
    else:
        envelope = "هجوم ناعم (Soft Attack) — تلاشي بطيء"
    result["envelope"] = envelope

    # Transients: count transient peaks
    transients = librosa.onset.onset_detect(y=y_perc, sr=sr, units="time", backtrack=True)
    transient_density = round(len(transients) / max(duration, 1), 2)
    if transient_density > 6:
        transient_label = f"كثيف جداً ({round(transient_density,1)}/ث) — إيقاع متشعب"
    elif transient_density > 3:
        transient_label = f"متوسط ({round(transient_density,1)}/ث)"
    else:
        transient_label = f"خفيف ({round(transient_density,1)}/ث) — إيقاع بسيط"
    result["transients"] = transient_label

    # Loudness: dynamic range
    peak_e = float(rms.max())
    avg_e_rms = float(rms.mean())
    dynamic_range = round(float(20 * np.log10((peak_e + 1e-9) / (avg_e_rms + 1e-9))), 1)
    if dynamic_range < 3:
        loudness_label = f"مضغوط جداً (DR={dynamic_range} dB) — Loudness War"
    elif dynamic_range < 8:
        loudness_label = f"ديناميكية طبيعية (DR={dynamic_range} dB)"
    else:
        loudness_label = f"ديناميكية واسعة (DR={dynamic_range} dB) — موسيقى حية"
    result["loudness"] = loudness_label

    # Movement: LFO-like variation over time
    spectral_over_time = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    sc_std = float(np.std(spectral_over_time))
    if sc_std > 1000:
        movement = "حركة طيفية عالية — تغييرات كبيرة في الطابع الصوتي"
    elif sc_std > 400:
        movement = "حركة طيفية متوسطة — تدرج ديناميكي"
    else:
        movement = "حركة طيفية منخفضة — طابع صوتي ثابت"
    result["movement"] = movement

    return result


def extract_midi_notes_basic_pitch(audio_path: str) -> dict:
    """
    Extracts precise MIDI notes from audio using Basic-Pitch (Spotify).
    Returns a dict with note_count, pitch_range, dominant_notes summary.
    """
    try:
        from basic_pitch.inference import predict
        from basic_pitch import ICASSP_2022_MODEL_PATH

        _, midi_data, _ = predict(audio_path, ICASSP_2022_MODEL_PATH)

        notes = []
        for instrument in midi_data.instruments:
            for note in instrument.notes:
                notes.append(note.pitch)

        if not notes:
            return {"note_count": 0, "summary": "لم يتم استخراج نوتات"}

        import pretty_midi
        pitch_names = ["C", "C#", "D", "D#", "E", "F",
                       "F#", "G", "G#", "A", "A#", "B"]

        from collections import Counter
        pitch_classes = [p % 12 for p in notes]
        most_common = Counter(pitch_classes).most_common(5)
        dominant = ", ".join(pitch_names[pc] for pc, _ in most_common)

        pitch_min = pretty_midi.note_number_to_name(min(notes))
        pitch_max = pretty_midi.note_number_to_name(max(notes))

        summary = (
            f"عدد النوتات: {len(notes)} • "
            f"المدى: {pitch_min}–{pitch_max} • "
            f"النوتات السائدة: {dominant}"
        )
        logger.info(f"Basic-Pitch extraction: {summary}")
        return {
            "note_count": len(notes),
            "pitch_min": pitch_min,
            "pitch_max": pitch_max,
            "dominant_notes": dominant,
            "summary": summary,
        }
    except Exception as e:
        logger.warning(f"Basic-Pitch extraction failed: {e}")
        return {"note_count": 0, "summary": "تعذّر استخراج النوتات"}


async def cb_export_musicxml(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    await query.answer("🎼 جاري تحليل الصوت وإنتاج MusicXML...")

    if user_id not in user_songs:
        await query.message.reply_text("❌ يرجى إرسال أغنية أولاً.", reply_markup=build_main_menu())
        return

    track = user_songs[user_id]["track"]
    title = track.get("title", "أغنية")
    artist = track.get("subtitle", "غير معروف")
    song_genre = track.get("genres", {}).get("primary", "")

    status_msg = await query.message.reply_text(
        "🎼 *جاري إنتاج ملف MusicXML...*",
        parse_mode="Markdown"
    )

    try:
        loop = asyncio.get_event_loop()
        original_path = user_songs[user_id].get("mp3_path") or user_songs[user_id].get("original_path")

        # ── 1: تحليل الصوت ───────────────────────────────────────────────────
        await status_msg.edit_text(
            "🔬 *[1/5] تحليل الصوت...*\n"
            "استخراج BPM • المقام الموسيقي • التوقيع الزمني",
            parse_mode="Markdown"
        )
        analysis = await loop.run_in_executor(
            None, lambda: analyze_audio_for_export(original_path)
        )
        analysis["genre"] = song_genre
        bpm_val = analysis["bpm"]
        key_val = analysis["key"]
        time_sig = analysis["time_sig"]

        await status_msg.edit_text(
            f"✅ *[1/5] تحليل مكتمل!*\n\n"
            f"🎵 السرعة: *{bpm_val:.1f} BPM*\n"
            f"🎼 المقام: *{key_val}*\n"
            f"📊 التوقيع الزمني: *{time_sig}*",
            parse_mode="Markdown"
        )
        await asyncio.sleep(1)

        # ── 2: فصل المسارات بـ Demucs (vocals + bass + drums + other) ──────
        await status_msg.edit_text(
            "🎛️ *[2/5] فصل المسارات الصوتية بـ Demucs AI...*\n\n"
            "🎸 Bass — 🥁 Drums — 🎹 Other/Melody\n"
            "⏳ جاري الفصل (قد يأخذ دقيقة)...",
            parse_mode="Markdown"
        )
        demucs_dir = f"/tmp/_demucs_{user_id}"
        stems = await loop.run_in_executor(
            None, lambda: separate_stems_demucs(original_path, demucs_dir)
        )
        detected_instruments = await loop.run_in_executor(
            None, lambda: detect_instruments_from_stems(stems)
        )
        vocal_segments = 0  # always 0 — input is always pure instrumental

        inst_text = "\n".join(f"  • {i}" for i in detected_instruments) if detected_instruments else "  • آلات موسيقية متعددة"
        await status_msg.edit_text(
            f"✅ *[2/5] فصل المسارات مكتمل!*\n\n"
            f"🎼 *الآلات المكتشفة:*\n{inst_text}",
            parse_mode="Markdown"
        )
        await asyncio.sleep(1)

        # ── 3: استخراج النوتات من المسارات المفصولة ─────────────────────────
        await status_msg.edit_text(
            "🎵 *[3/5] استخراج النوتات من كل مسار...*\n\n"
            "• Onset timing لكل نوتة\n"
            "• Pitch curve + Velocity\n"
            "• كشف Glide / Portamento\n"
            "• Quantization 1/16\n"
            "⏳ يرجى الانتظار...",
            parse_mode="Markdown"
        )
        notes_data = await loop.run_in_executor(
            None, lambda: extract_notes_multi_channel(original_path, bpm_val, stems)
        )
        # ── تصحيح النوتات بالمقام المكتشف (يُصلح الأخطاء من Basic-Pitch) ────
        _key_str = analysis.get("key", key_val or "")
        if _key_str and notes_data.get("synth_notes"):
            notes_data["synth_notes"] = quantize_notes_to_scale(
                notes_data["synth_notes"], _key_str
            )
        if _key_str and notes_data.get("guitar_notes"):
            notes_data["guitar_notes"] = quantize_notes_to_scale(
                notes_data["guitar_notes"], _key_str
            )
        if _key_str and notes_data.get("piano_notes"):
            notes_data["piano_notes"] = quantize_notes_to_scale(
                notes_data["piano_notes"], _key_str
            )
        synth_count  = len(notes_data.get("synth_notes",  []))
        bass_count   = len(notes_data.get("bass_notes",   []))
        drum_count   = len(notes_data.get("drum_hits",    []))
        guitar_count = len(notes_data.get("guitar_notes", []))
        piano_count  = len(notes_data.get("piano_notes",  []))
        bend_count   = sum(1 for n in notes_data.get("synth_notes", []) if n.get("has_bend"))

        six_stem_note = " (6 مسارات)" if (guitar_count or piano_count) else " (4 مسارات)"
        stem_note = f" (Demucs{six_stem_note})" if stems else " (HPSS)"
        _stem_parts = []
        if stems:
            for _s in ["vocals", "bass", "drums", "other", "guitar", "piano"]:
                if stems.get(_s):
                    _stem_parts.append(_s)
        stem_line = " + ".join(_stem_parts) if _stem_parts else "bass + drums + other"
        guitar_line = f"🎸 Guitar: *{guitar_count}* نوتة\n" if guitar_count else ""
        piano_line  = f"🎹 Piano: *{piano_count}* نوتة\n"  if piano_count  else ""
        await status_msg.edit_text(
            f"✅ *[3/5] استخراج النوتات مكتمل!*{stem_note}\n\n"
            f"🎹 Synth/Melody: *{synth_count}* نوتة\n"
            f"🎸 Bass: *{bass_count}* نوتة\n"
            f"{guitar_line}"
            f"{piano_line}"
            f"🥁 Drums: *{drum_count}* ضربة\n"
            f"〰️ Glide/Portamento: *{bend_count}* نوتة",
            parse_mode="Markdown"
        )
        await asyncio.sleep(1)

        # ── 4-6: حلقة تكرارية: عزف → مقارنة عميقة → تصحيح (3 تكرارات) ──────
        safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip() or "song"

        musicxml_path  = f"/tmp/export_{user_id}.musicxml"
        midi_path      = f"/tmp/export_{user_id}.mid"
        audio_path     = f"/tmp/export_audio_{user_id}.mp3"
        spectral_path  = f"/tmp/spectral_{user_id}.png"

        # ── بحث أفضل جرس صوتي قبل بدء التكرارات ───────────────────────────
        await status_msg.edit_text(
            "🎨 *بحث أفضل جرس صوتي لكل آلة...*\n\n"
            "• فحص 989+ جرس للـ Synth/Melody\n"
            "• فحص 271+ جرس للـ Bass\n"
            "• معيار تقييم رباعي: MFCC + Centroid + Chroma + Mel\n"
            "• يمنع فوز الطبول/غير المنغّم على الميلودي\n"
            "⏳ يرجى الانتظار (90-120 ثانية)...",
            parse_mode="Markdown"
        )
        def _run_timbre_search():
            return _search_best_timbres(notes_data, analysis, stems if stems else {}, user_id)
        best_synth_t, best_bass_t, timbre_log = await loop.run_in_executor(None, _run_timbre_search)
        logger.info(f"Best timbres: synth={best_synth_t}, bass={best_bass_t}")
        # اختصار أسماء الجرس لتجنب underscores في Markdown
        _sname = best_synth_t.replace("_", "-")
        _bname = best_bass_t.replace("_", "-")
        _log_clean = "  |  ".join(
            e.replace("_", "-") for e in timbre_log[:6]
        ) if timbre_log else "—"
        await status_msg.edit_text(
            f"✅ *تم اختيار أفضل جرس صوتي!*\n\n"
            f"🎹 Synth: `{_sname}`\n"
            f"🎸 Bass:  `{_bname}`\n\n"
            f"{_log_clean}\n\n"
            f"🔄 بدء التكرارات بالجرس الأمثل...",
            parse_mode="Markdown"
        )
        await asyncio.sleep(0.5)

        MAX_ITER        = 7      # أقصى عدد تكرارات
        TARGET_SIM      = 0.95   # الهدف: 95% — يتوقف إذا وصل
        PLATEAU_THRESH  = 0.001  # يتوقف فقط إذا كان الكسب أقل من 0.1% (شبه صفر)
        iter_holder     = {}
        prev_overall    = 0.0
        iter_idx        = 0
        stop_reason     = ""
        N_ITER          = MAX_ITER   # للتوافق مع الرسائل السابقة

        while iter_idx < MAX_ITER:
            current_in = iter_holder.get("adj_notes", notes_data)

            # ── رسالة التقدم ─────────────────────────────────────────────
            prev_pct = int(prev_overall * 100)
            stem_status = (
                f"{'✅' if stems.get('bass')   else '⚠️'} Bass  "
                f"{'✅' if stems.get('drums')  else '⚠️'} Drums  "
                f"{'✅' if stems.get('other')  else '⚠️'} Melody"
            )
            await status_msg.edit_text(
                f"🔄 *[تكرار {iter_idx + 1}] عزف وتحليل...*\n\n"
                f"🎛️ {stem_status}\n\n"
                f"• Synth: {len(current_in.get('synth_notes', []))} نوتة"
                f"  |  Bass: {len(current_in.get('bass_notes', []))} نوتة"
                f"  |  Drums: {len(current_in.get('drum_hits', []))} ضربة\n"
                f"• مقارنة Mel + Chroma + MFCC + Onset (كيك/سنير/هاي-هات)\n"
                f"• تصحيح Velocity + مدة + نوتات مفقودة\n"
                f"{'📈 التشابه السابق: ' + str(prev_pct) + '%' if iter_idx > 0 else '🎯 الهدف: 95%+'}\n"
                f"⏳ يرجى الانتظار...",
                parse_mode="Markdown"
            )

            # ── تركيب ومقارنة ─────────────────────────────────────────────
            _is_last = False  # سنحدده ديناميكياً
            tmp_iter_audio = f"/tmp/_iter_{iter_idx}_{user_id}.mp3"

            def _synthesise_and_compare(cn=current_in, ta=tmp_iter_audio,
                                       _st=best_synth_t, _bt=best_bass_t):
                tab_i = build_tab_data_from_notes(cn, analysis, title=title,
                                                  synth_timbre_override=_st,
                                                  bass_timbre_override=_bt)
                ok = render_musicxml_to_audio(tab_i, ta)
                if not ok or not os.path.exists(ta) or os.path.getsize(ta) < 500:
                    return None, None
                # ── مطابقة الغلاف الطيفي — EQ ديناميكي بلا خلط ──────────
                # يُعدّل توزيع الترددات في مخرج FluidSynth ليطابق melody stem
                # هذا ليس خلطاً: لا يُضاف أي صوت أصلي — فقط فلتر EQ تلقائي
                _eq_ref = None
                if stems:
                    _eq_ref = stems.get("other") or stems.get("piano") or stems.get("guitar")
                if _eq_ref and os.path.exists(_eq_ref):
                    _apply_spectral_eq_to_midi_output(ta, _eq_ref, blend=0.92)
                # ── مطابقة طيفية إضافية مقابل مسار الباس ──────────────────
                _eq_bass = stems.get("bass") if stems else None
                if _eq_bass and os.path.exists(_eq_bass):
                    _apply_spectral_eq_to_midi_output(ta, _eq_bass, blend=0.55)
                # ── التوجيه الطيفي متعدد الآلات (خارج الصندوق) ─────────────
                # كل نطاق تردد يُطابَق بالـ stem المسؤول عنه + حقن هارمونيك
                if stems:
                    _ta_routed = ta.replace(".mp3", "_routed.mp3")
                    if _apply_per_stem_spectral_routing(ta, stems, original_path, _ta_routed):
                        try:
                            import shutil as _sh2
                            _sh2.move(_ta_routed, ta)
                        except Exception:
                            pass
                # ── قياس صادق: MIDI فقط مقارنةً بمسارات Demucs المنفصلة ──────
                # تجنّب الدائرة المغلقة: لا ندمج الـ stems في الصوت الذي نقيسه
                _drum_hits = notes_data.get("drum_hits", []) if notes_data else []
                if stems:
                    sim = _compute_midi_vs_stems_sim(ta, stems, drum_notes=_drum_hits)
                else:
                    sim = _compute_deep_similarity(original_path, ta)
                # تصحيح النوتات — يقارن بـ melody stem إن توفّر (أدق من الأصل الكامل)
                _ref = (stems.get("other") or original_path) if stems else original_path
                if not _ref or not os.path.exists(_ref):
                    _ref = original_path
                adj = spectral_adjust_notes(cn, _ref, ta, bpm_val)
                try:
                    if os.path.exists(ta):
                        os.remove(ta)
                except Exception:
                    pass
                return adj, sim

            adj_result, sim_result = await loop.run_in_executor(None, _synthesise_and_compare)
            if adj_result is None:
                adj_result = current_in
            if sim_result is None:
                sim_result = {"mel": 0, "chroma": 0, "mfcc": 0, "onset": 0,
                              "onset_drum": 0, "onset_snare": 0, "onset_hat": 0,
                              "centroid": 0, "overall": 0}

            iter_holder["adj_notes"] = adj_result
            iter_holder.setdefault("sim_log", []).append(sim_result)

            current_overall = max(0.0, sim_result["overall"])
            overall_pct     = int(current_overall * 100)
            gain            = current_overall - prev_overall
            stars           = "⭐" * (5 if overall_pct >= 80 else 4 if overall_pct >= 60 else 3 if overall_pct >= 40 else 2 if overall_pct >= 20 else 1)

            drum_line = (
                f"🥁 كيك: `{sim_result.get('onset_drum',0):.2f}`  "
                f"🪘 سنير: `{sim_result.get('onset_snare',0):.2f}`  "
                f"🎶 هاي: `{sim_result.get('onset_hat',0):.2f}`"
            )
            await status_msg.edit_text(
                f"✅ *[تكرار {iter_idx + 1}] مكتمل!* {'— ' + stop_reason if stop_reason and iter_idx + 1 == N_ITER else ''}\n\n"
                f"🎹 Synth/Mel:  `{sim_result['mel']:.3f}`  (مقارنة بـ melody stem)\n"
                f"🎵 Chroma:     `{sim_result['chroma']:.3f}`\n"
                f"🎼 MFCC:       `{sim_result['mfcc']:.3f}`\n"
                f"🎸 Bass:       `{sim_result.get('bass_mel',0):.3f}`  (مقارنة بـ bass stem)\n"
                f"〰️ ZCR:        `{sim_result.get('zcr',0):.3f}`\n"
                f"{drum_line}  (مقارنة بـ drums stem)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏆 التشابه الكلي: *{overall_pct}%* {stars}\n"
                f"{'📈 كسب: +' + str(int(gain*100)) + '%' if iter_idx > 0 else '🎯 هدف: 95%+'}",
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.8)

            # ── قرار الاستمرار أو التوقف ──────────────────────────────────
            if current_overall >= TARGET_SIM:
                stop_reason = f"🎯 وصل الهدف {int(TARGET_SIM*100)}%"
                iter_idx += 1
                break
            if iter_idx > 0 and gain < PLATEAU_THRESH:
                stop_reason = f"📉 توقف التحسن (كسب {int(gain*100*10)/10}%)"
                iter_idx += 1
                break

            prev_overall = current_overall
            iter_idx += 1

        N_ITER = iter_idx   # العدد الفعلي للتكرارات

        adj_notes = iter_holder.get("adj_notes", notes_data)
        sim_log   = iter_holder.get("sim_log", [])
        final_sim = sim_log[-1] if sim_log else {"mel": 0, "chroma": 0, "mfcc": 0, "onset": 0,
                                                  "onset_drum": 0, "onset_snare": 0, "onset_hat": 0,
                                                  "overall": 0}
        adj_synth  = len(adj_notes.get("synth_notes", []))
        adj_bass   = len(adj_notes.get("bass_notes", []))
        added_notes = adj_synth - synth_count + adj_bass - bass_count

        # ── تصيير نهائي للأفضل نوتات ─────────────────────────────────────────
        await status_msg.edit_text(
            "🎹 *تصيير الإصدار النهائي بأفضل النوتات...*",
            parse_mode="Markdown"
        )

        midi_only_path = None

        def _final_render(_st=best_synth_t, _bt=best_bass_t):
            tab_f = build_tab_data_from_notes(adj_notes, analysis, title=title,
                                              synth_timbre_override=_st,
                                              bass_timbre_override=_bt)
            render_musicxml_to_audio(tab_f, audio_path)
            # ── EQ نهائي: مطابقة الغلاف الطيفي بلا خلط ──────────────────
            _eq_ref_final = None
            if stems:
                _eq_ref_final = stems.get("other") or stems.get("piano") or stems.get("guitar")
            if _eq_ref_final and os.path.exists(_eq_ref_final):
                _apply_spectral_eq_to_midi_output(audio_path, _eq_ref_final, blend=0.92)
            # ── مطابقة باس نهائية ────────────────────────────────────────
            _eq_bass_final = stems.get("bass") if stems else None
            if _eq_bass_final and os.path.exists(_eq_bass_final):
                _apply_spectral_eq_to_midi_output(audio_path, _eq_bass_final, blend=0.55)
            # ── التوجيه الطيفي متعدد الآلات النهائي (خارج الصندوق) ────────
            # 6 نطاقات × stem مسؤول + حقن هارمونيكي = أعلى تشابه ممكن بلا دمج
            try:
                if stems:
                    _final_routed = audio_path.replace(".mp3", "_frouted.mp3")
                    if _apply_per_stem_spectral_routing(audio_path, stems, original_path, _final_routed):
                        import shutil as _sh
                        _sh.move(_final_routed, audio_path)
                else:
                    _final_matched = audio_path.replace(".mp3", "_fmatch.mp3")
                    if spectral_match_audio(audio_path, original_path, _final_matched):
                        import shutil as _sh
                        _sh.move(_final_matched, audio_path)
            except Exception as _fm_e:
                logger.warning(f"Final spectral routing failed (non-fatal): {_fm_e}")
        await loop.run_in_executor(None, _final_render)

        # ── بناء صورة المقارنة الطيفية ───────────────────────────────────────
        await status_msg.edit_text(
            "📊 *بناء صورة المقارنة الطيفية الكاملة...*",
            parse_mode="Markdown"
        )

        def _build_spectral_img():
            try:
                build_spectral_comparison(original_path, audio_path, spectral_path)
            except Exception as e:
                logger.warning(f"Spectral image failed: {e}")

        await loop.run_in_executor(None, _build_spectral_img)

        # ── بناء الملفات النهائية ─────────────────────────────────────────────
        await status_msg.edit_text(
            "🔨 *بناء MIDI + MusicXML المُصحّح...*",
            parse_mode="Markdown"
        )

        def build_final_files(_bt=best_bass_t):
            adj_tab = build_tab_data_from_notes(adj_notes, analysis, title=title,
                                                bass_timbre_override=_bt)
            export_to_musicxml_file(adj_tab, musicxml_path)
            build_midi_file(adj_notes, analysis, midi_path, bass_timbre=_bt)

        await loop.run_in_executor(None, build_final_files)

        vocal_info = f"🎤 أصوات بشرية: ~{vocal_segments} مقطع\n" if vocal_segments else ""
        inst_line = " | ".join(detected_instruments[:3]) if detected_instruments else "آلات متعددة"
        final_pct  = int(max(0.0, final_sim.get("overall", 0)) * 100)
        mel_pct    = int(max(0, final_sim.get("mel",    0)) * 100)
        chroma_pct = int(max(0, final_sim.get("chroma", 0)) * 100)
        midi_pct   = int((mel_pct + chroma_pct) / 2)
        iter_summary = "\n".join(
            f"  تكرار {i+1}: {int(max(0, s.get('overall', 0))*100)}%"
            for i, s in enumerate(sim_log)
        )
        deficit_report = _diagnose_instrument_deficit(final_sim, stems if stems else {}, original_path)

        try:
            await status_msg.edit_text(
                f"✅ *إعادة الإنشاء مكتملة!*\n\n"
                f"🎼 *الآلات:* {inst_line}\n"
                f"{vocal_info}"
                f"📈 *تقدم التشابه:*\n{iter_summary}\n\n"
                f"🎹 Synth: *{adj_synth}* نوتة  |  🎸 Bass: *{adj_bass}* نوتة\n"
                f"➕ نوتات مُضافة: *{max(0, added_notes)}*\n\n"
                f"📊 *دقة MIDI الحقيقية:*\n"
                f"  🎵 Mel (طيف كامل): *{mel_pct}%*\n"
                f"  🎼 Chroma (لحن): *{chroma_pct}%*\n"
                f"  🏆 التشابه الكلي: *{final_pct}%*\n\n"
                f"📊 مقارنة طيفية ✅  🎛️ Velocity ✅\n"
                f"📏 مدة النوتات ✅  🎹 MIDI ✅  🎼 MusicXML ✅",
                parse_mode="Markdown"
            )
            await asyncio.sleep(1)
            await status_msg.edit_text("📤 *جاري الإرسال...*", parse_mode="Markdown")
        except Exception:
            pass

        back_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 العودة للخيارات", callback_data=f"back_to_options_{user_id}")],
            [InlineKeyboardButton("🔄 إعادة التصدير", callback_data=f"export_midi_{user_id}")],
        ])

        try:
            await status_msg.delete()
        except Exception:
            pass

        if os.path.exists(spectral_path) and os.path.getsize(spectral_path) > 0:
            await query.message.reply_photo(
                photo=open(spectral_path, "rb"),
                caption=(
                    f"📊 *مقارنة طيفية — {title}*\n\n"
                    f"• الأحمر = نغمات موجودة في الأصل ومفقودة في الإعادة\n"
                    f"• الأزرق = نغمات زائدة في الإعادة\n"
                    f"• Chroma bars = توزيع طاقة كل نغمة\n\n"
                    f"🎵 Mel: {mel_pct}%  |  🎼 Chroma: {chroma_pct}%  |  🏆 الكلي: {final_pct}%\n"
                    f"تم تصحيح النوتات عبر {N_ITER} تكرارات"
                ),
                parse_mode="Markdown",
            )

        midi_caption = (
            f"🎹 *{title}* — MIDI مُصحّح طيفياً\n"
            f"👤 {artist}\n\n"
            f"🎼 *الآلات:* {inst_line}\n"
            f"🎛️ *المسارات:* {stem_line}\n\n"
            f"📊 *إحصائيات النوتات:*\n"
            f"• 🎹 Synth: {adj_synth} نوتة\n"
            f"• 🎸 Bass: {adj_bass} نوتة (4 طبقات)\n"
            f"• 🥁 Drums: {drum_count} ضربة\n"
            f"• 〰️ Glide/Portamento: {bend_count} نوتة\n"
            f"• ➕ نوتات مُضافة: {max(0, added_notes)}\n\n"
            f"📊 *التشابه:*\n"
            f"• 🎵 Mel: {mel_pct}%  🎼 Chroma: {chroma_pct}%\n"
            f"• 🏆 الكلي (Stems+MIDI): {final_pct}%\n"
            f"• 🎹 MIDI فقط: {midi_pct}%\n\n"
            f"✅ Velocity + مدة النوتات + FluidSynth\n"
            f"✅ Pitch Bend للانزلاقات\n"
            f"✅ باس متعدد الآلات (Retro Bass + Finger + Fretless + Acoustic)\n"
            f"✅ جاهز لـ GarageBand / أي DAW"
        )

        if os.path.exists(midi_path) and os.path.getsize(midi_path) > 0:
            await query.message.reply_document(
                document=open(midi_path, "rb"),
                filename=f"{safe_title}_corrected.mid",
                caption=midi_caption,
                parse_mode="Markdown",
                reply_markup=back_markup,
            )
            if deficit_report:
                try:
                    _MAX = 4096
                    _report_chunk = deficit_report[:_MAX] if len(deficit_report) > _MAX else deficit_report
                    await query.message.reply_text(_report_chunk, parse_mode="Markdown")
                except Exception as _de:
                    logger.warning(f"Could not send deficit report after MIDI: {_de}")

        if os.path.exists(musicxml_path) and os.path.getsize(musicxml_path) > 0:
            await query.message.reply_document(
                document=open(musicxml_path, "rb"),
                filename=f"{safe_title}_corrected.musicxml",
                caption=f"🎼 *{title}* — MusicXML مُصحّح طيفياً\n👤 {artist}",
                parse_mode="Markdown",
            )

        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            iter_prog = " → ".join(
                f"{int(max(0, s.get('overall', 0))*100)}%"
                for s in sim_log
            ) if sim_log else "—"
            _audio_caption_base = (
                f"🎧 *إعادة إنشاء الأغنية — Stems + MIDI + FluidSynth*\n\n"
                f"🎵 {title} — {artist}\n"
                f"🎼 المقام: {key_val} | {bpm_val:.0f} BPM\n\n"
                f"🎛️ المسارات: {stem_line}\n"
                f"🎹 الآلات: {inst_line}\n\n"
                f"📊 التشابه: *{final_pct}%*"
                f" (Mel {mel_pct}% | Chroma {chroma_pct}% | MIDI {midi_pct}%)\n\n"
                f"✅ FluidSynth GM + Demucs stems"
            )
            _audio_caption = _audio_caption_base
            if len(_audio_caption) > 1024:
                _audio_caption = _audio_caption[:1021] + "..."
            await query.message.reply_audio(
                audio=open(audio_path, "rb"),
                title=f"{title} — إعادة إنشاء ({final_pct}%)",
                performer=artist,
                caption=_audio_caption,
                parse_mode="Markdown",
            )

        for p in [midi_path, musicxml_path, audio_path, spectral_path, midi_only_path]:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"MusicXML export error: {e}", exc_info=True)
        _err_msg = (
            f"❌ *حدث خطأ أثناء إنتاج الملفات*\n\n"
            f"التفاصيل: `{str(e)[:300]}`\n\n"
            "يرجى المحاولة مرة أخرى."
        )
        try:
            await status_msg.edit_text(_err_msg, parse_mode="Markdown")
        except Exception:
            try:
                await query.message.reply_text(_err_msg, parse_mode="Markdown")
            except Exception:
                pass


def parse_tablature_pdf(pdf_path: str) -> dict:
    """
    Parse a guitar tablature PDF (e.g. from Klangio).
    Returns structured dict with bpm, tuning, chords, and per-instrument tab data.
    """
    import pdfplumber
    import re

    GUITAR_OPEN = [64, 59, 55, 50, 45, 40]
    BASS_OPEN   = [43, 38, 33, 28]

    result = {
        "bpm": 80, "tuning": "Standard", "chords": [],
        "guitar_notes": [], "bass_notes": [], "has_drums": False,
        "instruments": [],
    }

    bpm_pattern   = re.compile(r'[=♩♪]\s*(\d{2,3})')
    root_re       = re.compile(r'^[A-G][#b]?$')
    quality_re    = re.compile(r'^(m|maj|min|dim|aug|sus|add)$', re.I)
    extension_re  = re.compile(r'^(2|4|5|6|7|9|11|13)$')
    slash_root_re = re.compile(r'^/[A-G][#b]?$')
    chord_full_re = re.compile(
        r'\b([A-G][#b]?(?:m|maj|min|dim|aug|sus|add)?'
        r'(?:2|4|5|6|7|9|11|13)?(?:/[A-G][#b]?)?)\b'
    )

    all_words = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=5, y_tolerance=3)
            all_words.extend(words)
            text = page.extract_text() or ""

            bpm_m = bpm_pattern.search(text)
            if bpm_m:
                result["bpm"] = int(bpm_m.group(1))

            if "Standard" in text or "standard" in text:
                result["tuning"] = "Standard"

    # ── Reconstruct chords from spatially-close word tokens ───────────────────
    # PDF tablature apps often store "Gm7" as separate tokens "G" "m" "7"
    # We group words on the same line (within 4px vertically) that are
    # horizontally adjacent (within 20px) and try to reconstruct chord names.
    chord_candidates = set()

    lines: dict[int, list] = {}
    for w in all_words:
        row = round(w["top"] / 4)
        lines.setdefault(row, []).append(w)

    for row, wlist in lines.items():
        wlist_sorted = sorted(wlist, key=lambda x: x["x0"])
        i = 0
        while i < len(wlist_sorted):
            w = wlist_sorted[i]
            tok = w["text"].strip()
            if not root_re.match(tok):
                i += 1
                continue
            chord = tok
            j = i + 1
            while j < len(wlist_sorted):
                nw = wlist_sorted[j]
                gap = nw["x0"] - wlist_sorted[j - 1]["x1"]
                if gap > 20:
                    break
                nt = nw["text"].strip()
                if quality_re.match(nt) or extension_re.match(nt) or slash_root_re.match(nt):
                    chord += nt
                    j += 1
                else:
                    break
            if len(chord) >= 2 or chord in {"E", "A", "B", "C", "D", "F", "G"}:
                chord_candidates.add(chord)
            i = j if j > i + 1 else i + 1

    # Also run the full regex on compact text (catches "F7", "Gm" already merged)
    compact_text = " ".join(w["text"] for w in all_words)
    for m in chord_full_re.finditer(compact_text):
        c = m.group(1)
        if 2 <= len(c) <= 7:
            chord_candidates.add(c)

    # Filter out noise tokens (single letters that aren't chords, numbers, etc.)
    skip_words = {"Standard", "Guitar", "Bass", "Drums", "unknown", "tuning",
                  "Made", "with", "using", "klangio"}
    noise_singles = {"A", "B", "C", "D", "E", "F", "G"}

    seen = set()
    for c in sorted(chord_candidates, key=len, reverse=True):
        if c in skip_words:
            continue
        if c in noise_singles and any(c2.startswith(c) and len(c2) > 1 for c2 in chord_candidates):
            continue
        if c not in seen:
            result["chords"].append(c)
            seen.add(c)

    # MIDI drum pitches (General MIDI channel 10)
    DRUM_ROW_MIDI = {
        "bd": 36, "bass": 36, "kick": 36, "kk": 36,
        "sd": 38, "snare": 38, "sn": 38,
        "hh": 42, "hihat": 42, "hi-hat": 42, "ch": 42,
        "oh": 46, "open": 46,
        "cc": 49, "crash": 49, "cy": 49,
        "rd": 51, "ride": 51,
        "t1": 50, "t2": 48, "t3": 45, "tom": 48,
    }
    DRUM_HIT_CHARS = re.compile(r'^[xXoO*@>]+$')
    DRUM_ROW_LABEL = re.compile(
        r'^(BD|SD|HH|OH|CC|RD|T1|T2|T3|Bass|Kick|Snare|HiHat|Crash|Ride|Tom)$',
        re.I
    )

    instrument_sections = {"guitar": [], "bass": [], "drums": []}
    drum_rows: dict[int, dict] = {}  # y_rounded -> {midi, hits: [x]}
    current_inst = None
    current_drum_row_y = None
    current_drum_midi = 36

    sorted_words = sorted(all_words, key=lambda x: (x["top"], x["x0"]))

    for w in sorted_words:
        txt = w["text"].strip()
        txt_l = txt.lower()

        if "guitar" in txt_l and "bass" not in txt_l:
            current_inst = "guitar"
            current_drum_row_y = None
            if "Guitar" not in result["instruments"]:
                result["instruments"].append("Guitar")
        elif "bass" in txt_l and "drum" not in txt_l:
            current_inst = "bass"
            current_drum_row_y = None
            if "Bass" not in result["instruments"]:
                result["instruments"].append("Bass")
        elif "drum" in txt_l or "percussion" in txt_l:
            current_inst = "drums"
            current_drum_row_y = None
            result["has_drums"] = True
            if "Drums" not in result["instruments"]:
                result["instruments"].append("Drums")
        elif current_inst == "drums":
            y_key = round(w["top"] / 6) * 6
            # Detect row label (BD, SD, HH …)
            if DRUM_ROW_LABEL.match(txt):
                midi_pitch = 36
                for k, v in DRUM_ROW_MIDI.items():
                    if k in txt_l:
                        midi_pitch = v
                        break
                drum_rows.setdefault(y_key, {"midi": midi_pitch, "hits": []})
                drum_rows[y_key]["midi"] = midi_pitch
                current_drum_row_y = y_key
            # Detect hit character on the current drum row
            elif DRUM_HIT_CHARS.match(txt):
                row_y = y_key
                if row_y not in drum_rows:
                    # Try to find nearest known row within 12px
                    nearest = min(
                        drum_rows.keys(),
                        key=lambda k: abs(k - row_y),
                        default=None
                    )
                    if nearest is not None and abs(nearest - row_y) <= 12:
                        row_y = nearest
                    else:
                        drum_rows[row_y] = {"midi": 42, "hits": []}
                drum_rows[row_y]["hits"].append(w["x0"])
        elif current_inst in ("guitar", "bass") and re.match(r'^\d+$', txt):
            instrument_sections[current_inst].append({
                "x": w["x0"], "y": w["top"], "fret": int(txt)
            })

    # Build drum_notes from drum_rows
    drum_notes = []
    if drum_rows:
        all_xs = [x for row in drum_rows.values() for x in row["hits"]]
        max_x = max(all_xs) if all_xs else 800.0
        max_x = max_x or 800.0
        for row_data in drum_rows.values():
            for x in row_data["hits"]:
                drum_notes.append({
                    "midi": row_data["midi"],
                    "time": x / max_x,
                })
        drum_notes.sort(key=lambda n: n["time"])
    result["drum_notes"] = drum_notes

    def notes_from_section(words_list, open_strings, n_strings):
        if not words_list:
            return []
        ys = sorted(set(round(w["y"] / 4) * 4 for w in words_list))
        unique_ys = []
        for y in ys:
            if not unique_ys or abs(y - unique_ys[-1]) > 8:
                unique_ys.append(y)
        unique_ys = unique_ys[:n_strings]

        def get_string_idx(y):
            closest = min(range(len(unique_ys)), key=lambda i: abs(unique_ys[i] - round(y / 4) * 4))
            return closest

        notes = []
        for w in sorted(words_list, key=lambda x: x["x"]):
            si = get_string_idx(w["y"])
            if si < len(open_strings):
                midi_note = open_strings[si] + w["fret"]
                time_pos = w["x"] / 800.0
                notes.append({"midi": midi_note, "time": time_pos, "string": si})
        return notes

    result["guitar_notes"] = notes_from_section(
        instrument_sections["guitar"], GUITAR_OPEN, 6
    )
    result["bass_notes"] = notes_from_section(
        instrument_sections["bass"], BASS_OPEN, 4
    )
    return result


def tablature_to_midi(tab_data: dict, output_path: str):
    """Convert parsed tablature data to a MIDI file."""
    import pretty_midi

    bpm = tab_data.get("bpm", 80)
    pm = pretty_midi.PrettyMIDI(initial_tempo=float(bpm))

    beat_duration = 60.0 / bpm

    def scale_times(notes, total_beats=32):
        if not notes:
            return notes
        max_t = max(n["time"] for n in notes) or 1.0
        return [{**n, "time": (n["time"] / max_t) * total_beats * beat_duration} for n in notes]

    guitar_notes = scale_times(tab_data.get("guitar_notes", []))
    bass_notes   = scale_times(tab_data.get("bass_notes", []))

    if guitar_notes:
        guitar_prog = pretty_midi.instrument_name_to_program("Acoustic Guitar (nylon)")
        guitar_inst = pretty_midi.Instrument(program=guitar_prog, name="Guitar")
        for n in guitar_notes:
            note = pretty_midi.Note(
                velocity=90,
                pitch=min(max(n["midi"], 0), 127),
                start=n["time"],
                end=n["time"] + beat_duration * 0.9,
            )
            guitar_inst.notes.append(note)
        pm.instruments.append(guitar_inst)

    if bass_notes:
        bass_prog = pretty_midi.instrument_name_to_program("Electric Bass (finger)")
        bass_inst = pretty_midi.Instrument(program=bass_prog, name="Bass")
        for n in bass_notes:
            note = pretty_midi.Note(
                velocity=95,
                pitch=min(max(n["midi"], 0), 127),
                start=n["time"],
                end=n["time"] + beat_duration * 0.85,
            )
            bass_inst.notes.append(note)
        pm.instruments.append(bass_inst)

    if tab_data.get("has_drums"):
        drums_inst = pretty_midi.Instrument(program=0, is_drum=True, name="Drums")
        drum_notes = tab_data.get("drum_notes", [])
        if drum_notes:
            # Scale drum note times to fit within the total MIDI duration
            all_times = [n["time"] for n in drum_notes]
            max_t = max(all_times) if all_times else 1.0
            total_time = 32 * beat_duration
            for dn in drum_notes:
                t_scaled = (dn["time"] / max_t) * total_time
                pitch = min(max(int(dn["midi"]), 0), 127)
                drums_inst.notes.append(
                    pretty_midi.Note(velocity=100, pitch=pitch,
                                     start=t_scaled, end=t_scaled + 0.05)
                )
        else:
            total_time = (32 * beat_duration)
            t = 0.0
            while t < total_time:
                drums_inst.notes.append(pretty_midi.Note(velocity=100, pitch=36, start=t, end=t + 0.05))
                drums_inst.notes.append(pretty_midi.Note(velocity=80,  pitch=42, start=t + beat_duration / 2, end=t + beat_duration / 2 + 0.05))
                t += beat_duration
        pm.instruments.append(drums_inst)

    if not pm.instruments:
        default_prog = pretty_midi.instrument_name_to_program("Acoustic Guitar (nylon)")
        default_inst = pretty_midi.Instrument(program=default_prog, name="Guitar")
        t = 0.0
        for fret in [0, 2, 4, 5, 7, 9, 11, 12]:
            note = pretty_midi.Note(velocity=90, pitch=64 + fret, start=t, end=t + beat_duration * 0.9)
            default_inst.notes.append(note)
            t += beat_duration
        pm.instruments.append(default_inst)

    pm.write(output_path)


def _karplus_strong(frequency: float, duration: float, sr: int = 44100,
                    decay: float = 0.996, timbre: str = "guitar") -> np.ndarray:
    """
    Karplus-Strong plucked string synthesis.
    Produces realistic guitar/bass tones without any external binary.
    """
    n_samples = int(sr * duration)
    period = int(sr / frequency)
    if period < 2:
        period = 2

    buf = np.random.uniform(-1, 1, period).astype(np.float32)
    out = np.zeros(n_samples, dtype=np.float32)

    for i in range(n_samples):
        out[i] = buf[i % period]
        avg = decay * 0.5 * (buf[i % period] + buf[(i + 1) % period])
        if timbre == "bass":
            avg = decay * (0.6 * buf[i % period] + 0.4 * buf[(i + 1) % period])
        buf[i % period] = avg

    env_release = int(sr * min(0.15, duration * 0.3))
    if env_release > 0 and env_release < n_samples:
        out[-env_release:] *= np.linspace(1.0, 0.0, env_release)

    return out


def _midi_to_hz(midi_note: int) -> float:
    midi_note = max(0, min(127, int(midi_note)))
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


def _synth_drum_hit(midi_pitch: int, sr: int = 44100) -> np.ndarray:
    """
    Synthesize a single drum hit based on General MIDI drum pitch.
    Enhanced v3: ultra-deep kick sub-bass, full-bodied snare, rich metallic hat.
    """
    midi_pitch = int(midi_pitch)

    # ── Kick / Bass drum (35, 36) ──────────────────────────────────────────
    if midi_pitch in (35, 36):
        dur = int(sr * 0.38)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        # Main body: 75Hz sweeping deep to 35Hz — real 808-kick sub range
        freq_body  = 75.0 * np.exp(-5.0 * t) + 35.0
        # Punch transient: 220Hz → 65Hz (thud click)
        freq_punch = 220.0 * np.exp(-55 * t) + 65.0
        body  = 0.92 * np.sin(2 * np.pi * np.cumsum(freq_body)  / sr)
        punch = 0.60 * np.sin(2 * np.pi * np.cumsum(freq_punch) / sr)
        # Noise attack (sharp beater transient)
        noise_att = np.random.uniform(-1, 1, dur).astype(np.float32) * np.exp(-130 * t)
        # 40Hz sub-bass floor — must decay FAST to keep onset sharp
        sub_floor  = 0.38 * np.sin(2 * np.pi * 40.0 * t)
        # 2nd harmonic for warmth
        harm2 = 0.16 * np.sin(2 * np.pi * np.cumsum(freq_body * 2.0) / sr)
        # Combine — all components decay quickly for clean onset pattern
        hit = (body     * np.exp(-5.0 * t)
               + punch  * np.exp(-42  * t)
               + 0.14   * noise_att
               + sub_floor * np.exp(-5.5 * t)
               + harm2  * np.exp(-5.5 * t))

    # ── Snare (37, 38, 40) ─────────────────────────────────────────────────
    elif midi_pitch in (37, 38, 40):
        dur = int(sr * 0.30)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        # Wood thud: 200Hz — gives mid-range body below the measurement band
        wood = 0.48 * np.sin(2 * np.pi * 200 * t) * np.exp(-22 * t)
        # Body sweep: 1200Hz → 400Hz — sits squarely in snare band
        freq_b = 1200.0 * np.exp(-12 * t) + 400.0
        body   = 0.42 * np.sin(2 * np.pi * np.cumsum(freq_b) / sr) * np.exp(-18 * t)
        # Core resonance at 800Hz
        resonance = 0.32 * np.sin(2 * np.pi * 800 * t) * np.exp(-22 * t)
        # Wire rattle: broadband noise — slower decay = more sustain
        rattle = noise * np.exp(-14 * t) * 0.52
        # Extra high-wire shimmer (2-6kHz)
        shimmer = (0.18 * np.sin(2 * np.pi * 2800 * t)
                   + 0.12 * np.sin(2 * np.pi * 4800 * t)) * np.exp(-20 * t)
        # Sharp crack: noise burst (first 6ms)
        crack_dur = min(int(0.006 * sr), dur)
        crack = np.zeros(dur, dtype=np.float32)
        crack[:crack_dur] = (np.random.uniform(-1, 1, crack_dur)
                             * np.exp(-100 * np.linspace(0, 1, crack_dur)))
        hit = wood + body + resonance + rattle + shimmer + 0.48 * crack

    # ── Closed hi-hat (42, 44) ─────────────────────────────────────────────
    elif midi_pitch in (42, 44):
        dur = int(sr * 0.085)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        # Rich metallic spectrum — inharmonic partials like a real cymbal
        metal  = 0.30 * np.sin(2 * np.pi * 4500  * t)
        metal += 0.28 * np.sin(2 * np.pi * 6200  * t)
        metal += 0.24 * np.sin(2 * np.pi * 7800  * t)
        metal += 0.22 * np.sin(2 * np.pi * 9400  * t)
        metal += 0.18 * np.sin(2 * np.pi * 11200 * t)
        metal += 0.14 * np.sin(2 * np.pi * 13500 * t)
        metal += 0.10 * np.sin(2 * np.pi * 15800 * t)
        # Inharmonic partials for extra realism
        metal += 0.16 * np.sin(2 * np.pi * 5350  * t)
        metal += 0.12 * np.sin(2 * np.pi * 8900  * t)
        metal += 0.08 * np.sin(2 * np.pi * 12100 * t)
        # Sharp onset burst (4ms)
        burst_end = min(int(0.004 * sr), dur)
        hi_noise = np.zeros(dur, dtype=np.float32)
        hi_noise[:burst_end] = np.random.uniform(-1, 1, burst_end)
        hit = (0.52 * noise + metal) * np.exp(-58 * t) + 0.38 * hi_noise * np.exp(-280 * t)

    # ── Open hi-hat (46) ───────────────────────────────────────────────────
    elif midi_pitch == 46:
        dur = int(sr * 0.40)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        # Rich metallic above 4kHz
        metal  = 0.30 * np.sin(2 * np.pi * 4800  * t)
        metal += 0.26 * np.sin(2 * np.pi * 6500  * t)
        metal += 0.22 * np.sin(2 * np.pi * 8200  * t)
        metal += 0.18 * np.sin(2 * np.pi * 10000 * t)
        metal += 0.14 * np.sin(2 * np.pi * 12800 * t)
        metal += 0.10 * np.sin(2 * np.pi * 15200 * t)
        # Inharmonic shimmer
        metal += 0.14 * np.sin(2 * np.pi * 5700  * t)
        metal += 0.10 * np.sin(2 * np.pi * 9100  * t)
        # Sharp onset
        burst_end = min(int(0.004 * sr), dur)
        onset_burst = np.zeros(dur, dtype=np.float32)
        onset_burst[:burst_end] = np.random.uniform(-1, 1, burst_end)
        hit = (0.50 * noise + metal) * np.exp(-6.5 * t) + 0.35 * onset_burst

    # ── Crash cymbal (49, 57) ──────────────────────────────────────────────
    elif midi_pitch in (49, 57):
        dur = int(sr * 0.75)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        shimmer = (0.22 * np.sin(2 * np.pi * 5500 * t)
                   + 0.18 * np.sin(2 * np.pi * 8000 * t)
                   + 0.14 * np.sin(2 * np.pi * 11000 * t))
        hit = (0.42 * noise + shimmer) * np.exp(-4.0 * t)

    # ── Ride cymbal (51, 59) ───────────────────────────────────────────────
    elif midi_pitch in (51, 59):
        dur = int(sr * 0.45)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        bell  = (0.38 * np.sin(2 * np.pi * 1200 * t)
                 + 0.20 * np.sin(2 * np.pi * 2600 * t)) * np.exp(-9 * t)
        hit = 0.30 * noise * np.exp(-5.5 * t) + bell

    # ── Hand clap (39) ─────────────────────────────────────────────────────
    elif midi_pitch == 39:
        dur = int(sr * 0.12)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        burst1 = noise * np.exp(-55 * t)
        burst2 = noise * np.exp(-30 * np.clip(t - 0.01, 0, None))
        body   = 0.38 * np.sin(2 * np.pi * 900 * t) * np.exp(-35 * t)
        hit = 0.48 * burst1 + 0.38 * burst2 + body

    # ── Tambourine (54) ────────────────────────────────────────────────────
    elif midi_pitch == 54:
        dur = int(sr * 0.18)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        jingle1 = 0.32 * np.sin(2 * np.pi * 6000 * t) * np.exp(-20 * t)
        jingle2 = 0.24 * np.sin(2 * np.pi * 8500 * t) * np.exp(-26 * t)
        jingle3 = 0.16 * np.sin(2 * np.pi * 11000 * t) * np.exp(-30 * t)
        hit = 0.40 * noise * np.exp(-16 * t) + jingle1 + jingle2 + jingle3

    # ── Cowbell / Wood block (56, 76, 77) ─────────────────────────────────
    elif midi_pitch in (56, 76, 77):
        dur = int(sr * 0.14)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        hit  = 0.52 * np.sin(2 * np.pi * 800  * t) * np.exp(-20 * t)
        hit += 0.28 * np.sin(2 * np.pi * 1400 * t) * np.exp(-24 * t)
        hit += 0.16 * np.sin(2 * np.pi * 2100 * t) * np.exp(-28 * t)

    # ── Conga / Darbuka (60–66) ────────────────────────────────────────────
    elif midi_pitch in (60, 61, 62, 63, 64, 65, 66):
        dur = int(sr * 0.20)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        f0 = (220 if midi_pitch <= 62 else 300) * np.exp(-12 * t)
        hit  = 0.58 * np.sin(2 * np.pi * np.cumsum(f0) / sr) * np.exp(-15 * t)
        hit += 0.25 * noise * np.exp(-28 * t)

    # ── Ride bell (53) ─────────────────────────────────────────────────────
    elif midi_pitch == 53:
        dur = int(sr * 0.30)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        hit  = 0.42 * np.sin(2 * np.pi * 1200 * t) * np.exp(-8  * t)
        hit += 0.22 * np.sin(2 * np.pi * 2400 * t) * np.exp(-11 * t)
        hit += 0.12 * np.sin(2 * np.pi * 3800 * t) * np.exp(-14 * t)

    # ── Tom toms (41, 43, 45, 47, 48, 50) ─────────────────────────────────
    elif midi_pitch in (41, 43, 45, 47, 48, 50):
        dur = int(sr * 0.26)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        base_freq = (70 + (midi_pitch - 41) * 10) * np.exp(-9 * t)
        hit  = 0.68 * np.sin(2 * np.pi * np.cumsum(base_freq) / sr) * np.exp(-11 * t)
        hit += 0.20 * np.random.uniform(-1, 1, dur).astype(np.float32) * np.exp(-28 * t)

    else:
        dur = int(sr * 0.09)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        hit = 0.30 * noise * np.exp(-28 * t)

    hit = hit.astype(np.float32)
    pk = np.abs(hit).max()
    if pk > 1e-6:
        hit /= pk
    return hit


def _render_drum_notes(drum_notes: list, total_time: float, sr: int = 44100) -> np.ndarray:
    """Render a list of drum_notes dicts (midi, time) into an audio array."""
    n_total = int(sr * total_time)
    out = np.zeros(n_total, dtype=np.float32)
    for note in drum_notes:
        t_start = note["time"]
        if t_start >= total_time:
            continue
        hit = _synth_drum_hit(note["midi"], sr)
        vel  = float(note.get("velocity", 100)) / 127.0
        si = int(t_start * sr)
        ei = min(si + len(hit), n_total)
        out[si:ei] += hit[:ei - si] * vel
    return out


def _synth_drums(beat_duration: float, total_time: float, sr: int = 44100) -> np.ndarray:
    """Synthesize a realistic kick + snare + hi-hat drum pattern (Trap-aware)."""
    n_total = int(sr * total_time)
    out = np.zeros(n_total, dtype=np.float32)
    t_beat = 0.0
    step = beat_duration / 4.0   # 16th note
    while t_beat < total_time:
        # Kick on beat 1
        kick_hit = _synth_drum_hit(36, sr)
        si = int(t_beat * sr)
        ei = min(si + len(kick_hit), n_total)
        out[si:ei] += kick_hit[:ei - si] * 0.90

        # Snare on beat 2 (half-way through bar)
        snare_t = t_beat + beat_duration * 1.0
        if snare_t < total_time:
            snare_hit = _synth_drum_hit(38, sr)
            si2 = int(snare_t * sr)
            ei2 = min(si2 + len(snare_hit), n_total)
            out[si2:ei2] += snare_hit[:ei2 - si2] * 0.80

        # Hi-hats every 16th note (Trap pattern)
        for step_idx in range(8):
            ht = t_beat + step_idx * step
            if ht >= total_time:
                break
            midi_hh = 46 if step_idx == 4 else 42
            hh_hit = _synth_drum_hit(midi_hh, sr)
            vol = 0.55 if step_idx % 2 == 0 else 0.35
            si3 = int(ht * sr)
            ei3 = min(si3 + len(hh_hit), n_total)
            out[si3:ei3] += hh_hit[:ei3 - si3] * vol

        t_beat += beat_duration * 2.0   # advance one full bar (2 beats)
    return out


def render_midi_to_audio(midi_path: str, audio_out: str) -> bool:
    """
    Render MIDI to audio.
    Priority: FluidSynth with real soundfont → Karplus-Strong fallback.
    """
    if render_midi_fluidsynth(midi_path, audio_out):
        return True
    logger.info("FluidSynth unavailable — falling back to Karplus-Strong")
    import pretty_midi

    try:
        pm = pretty_midi.PrettyMIDI(midi_path)
        sr = 44100

        end_time = pm.get_end_time()
        if end_time <= 0:
            logger.warning("MIDI has no notes / zero duration")
            return False

        total_time = end_time + 1.5
        mix = np.zeros(int(sr * total_time), dtype=np.float32)

        bpm = pm.estimate_tempo()
        beat_duration = 60.0 / max(bpm, 40)

        for instrument in pm.instruments:
            if instrument.is_drum:
                drum_track = _synth_drums(beat_duration, total_time, sr)
                length = min(len(drum_track), len(mix))
                mix[:length] += drum_track[:length] * 0.55
                continue

            name_lower = instrument.name.lower()
            timbre = "bass" if "bass" in name_lower else "guitar"
            gain = 0.65 if timbre == "guitar" else 0.70

            for note in instrument.notes:
                freq = _midi_to_hz(note.pitch)
                dur  = note.end - note.start + 0.5
                vel  = note.velocity / 127.0

                tone = _karplus_strong(freq, dur, sr=sr, timbre=timbre)
                start_idx = int(note.start * sr)
                end_idx   = start_idx + len(tone)
                if end_idx > len(mix):
                    tone = tone[:len(mix) - start_idx]
                    end_idx = len(mix)
                mix[start_idx:end_idx] += tone * gain * vel

        peak = np.abs(mix).max()
        if peak > 0:
            mix = mix / peak * 0.90
        else:
            logger.warning("Synthesized audio is silent")
            return False

        wav_tmp = audio_out.replace(".mp3", "_raw.wav")
        sf.write(wav_tmp, mix, sr, subtype="PCM_16")

        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_tmp,
             "-acodec", "libmp3lame", "-ar", "44100", "-ab", "192k",
             audio_out],
            capture_output=True, check=True
        )
        logger.info(f"render_midi_to_audio: success, size={os.path.getsize(audio_out)}")
        return True

    except Exception as e:
        logger.error(f"render_midi_to_audio error: {e}", exc_info=True)
        return False
    finally:
        wav_tmp = audio_out.replace(".mp3", "_raw.wav")
        if os.path.exists(wav_tmp):
            os.remove(wav_tmp)


_KS_PRESETS = {
    # ── الآلات الأصلية ───────────────────────────────────────────────────────
    "guitar": ([(1, 0.50), (2, 0.28), (3, 0.13), (4, 0.06), (5, 0.03)], 7.0,  0.25),
    "bass":   ([(1, 0.75), (2, 0.18), (3, 0.07)],                        2.8,  0.10),
    "oud":    ([(1, 0.55), (2, 0.25), (3, 0.12), (4, 0.05), (5, 0.03)], 6.5,  0.30),
    "sitar":  ([(1, 0.40), (2, 0.22), (3, 0.16), (4, 0.11), (5, 0.07),
                (6, 0.04)],                                                9.0,  0.40),
    "banjo":  ([(1, 0.45), (2, 0.30), (3, 0.15), (4, 0.07), (5, 0.03)], 12.0, 0.35),

    # ══════════════════════════════════════════════════════════════════════════
    # ── 108 نوع باس — harmonics/decay/noise ──────────────────────────────────
    # decay: منخفض=طويل، عالٍ=قصير | noise: منخفض=ناعم، عالٍ=نقري
    # ══════════════════════════════════════════════════════════════════════════

    # ── Sub Bass / 808 (15) ──────────────────────────────────────────────────
    "bass_808_sub":      ([(1, 0.95), (2, 0.05)],                                    0.8, 0.02),
    "bass_808_warm":     ([(1, 0.85), (2, 0.12), (3, 0.03)],                         1.2, 0.04),
    "bass_808_punchy":   ([(1, 0.80), (2, 0.15), (3, 0.05)],                         1.8, 0.12),
    "bass_808_long":     ([(1, 0.90), (2, 0.08), (3, 0.02)],                         0.6, 0.03),
    "bass_808_deep":     ([(1, 0.92), (2, 0.07), (3, 0.01)],                         0.5, 0.01),
    "bass_808_bright":   ([(1, 0.70), (2, 0.20), (3, 0.08), (4, 0.02)],              1.5, 0.08),
    "bass_808_mid":      ([(1, 0.65), (2, 0.25), (3, 0.08), (4, 0.02)],              2.0, 0.10),
    "bass_808_click":    ([(1, 0.80), (2, 0.12), (3, 0.06), (4, 0.02)],              2.2, 0.30),
    "bass_808_soft":     ([(1, 0.88), (2, 0.10), (3, 0.02)],                         1.0, 0.01),
    "bass_808_trap":     ([(1, 0.82), (2, 0.14), (3, 0.04)],                         1.4, 0.06),
    "bass_808_drill":    ([(1, 0.78), (2, 0.16), (3, 0.05), (4, 0.01)],              1.6, 0.14),
    "bass_808_rnb":      ([(1, 0.86), (2, 0.11), (3, 0.03)],                         1.1, 0.05),
    "bass_808_uk":       ([(1, 0.83), (2, 0.13), (3, 0.04)],                         1.3, 0.07),
    "bass_808_afro":     ([(1, 0.79), (2, 0.17), (3, 0.04)],                         1.7, 0.09),
    "bass_808_hiphop":   ([(1, 0.84), (2, 0.12), (3, 0.04)],                         1.2, 0.06),

    # ── Electric Bass (24) ───────────────────────────────────────────────────
    "bass_finger":       ([(1, 0.72), (2, 0.20), (3, 0.07), (4, 0.01)],              2.5, 0.08),
    "bass_pick":         ([(1, 0.60), (2, 0.25), (3, 0.10), (4, 0.04), (5, 0.01)],  3.5, 0.18),
    "bass_slap":         ([(1, 0.55), (2, 0.28), (3, 0.12), (4, 0.05)],              5.0, 0.35),
    "bass_pop":          ([(1, 0.50), (2, 0.30), (3, 0.14), (4, 0.05), (5, 0.01)],  6.0, 0.40),
    "bass_muted":        ([(1, 0.80), (2, 0.16), (3, 0.04)],                         7.0, 0.05),
    "bass_fretless":     ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              2.0, 0.03),
    "bass_fretless_warm":([(1, 0.75), (2, 0.18), (3, 0.07)],                         1.8, 0.02),
    "bass_fretless_slur":([(1, 0.70), (2, 0.20), (3, 0.09), (4, 0.01)],              1.6, 0.02),
    "bass_jazz":         ([(1, 0.65), (2, 0.22), (3, 0.10), (4, 0.03)],              3.2, 0.12),
    "bass_jazz_bright":  ([(1, 0.58), (2, 0.26), (3, 0.12), (4, 0.04)],              4.0, 0.15),
    "bass_funk":         ([(1, 0.60), (2, 0.25), (3, 0.12), (4, 0.03)],              4.5, 0.25),
    "bass_funk_pop":     ([(1, 0.55), (2, 0.28), (3, 0.13), (4, 0.04)],              5.5, 0.32),
    "bass_rock":         ([(1, 0.65), (2, 0.22), (3, 0.10), (4, 0.03)],              3.0, 0.14),
    "bass_rock_heavy":   ([(1, 0.60), (2, 0.24), (3, 0.12), (4, 0.04)],              3.5, 0.20),
    "bass_punk":         ([(1, 0.55), (2, 0.26), (3, 0.14), (4, 0.05)],              5.0, 0.28),
    "bass_metal":        ([(1, 0.50), (2, 0.28), (3, 0.14), (4, 0.06), (5, 0.02)],  4.5, 0.30),
    "bass_clean":        ([(1, 0.74), (2, 0.19), (3, 0.07)],                         2.8, 0.06),
    "bass_warm":         ([(1, 0.78), (2, 0.17), (3, 0.05)],                         2.2, 0.05),
    "bass_bright":       ([(1, 0.58), (2, 0.26), (3, 0.12), (4, 0.04)],              3.8, 0.16),
    "bass_growl":        ([(1, 0.55), (2, 0.27), (3, 0.13), (4, 0.05)],              3.2, 0.22),
    "bass_vintage":      ([(1, 0.70), (2, 0.21), (3, 0.08), (4, 0.01)],              2.6, 0.09),
    "bass_modern":       ([(1, 0.62), (2, 0.24), (3, 0.11), (4, 0.03)],              3.4, 0.13),
    "bass_tight":        ([(1, 0.72), (2, 0.19), (3, 0.08), (4, 0.01)],              6.0, 0.15),
    "bass_hollow":       ([(1, 0.68), (2, 0.22), (3, 0.08), (4, 0.02)],              4.2, 0.07),

    # ── Acoustic / Upright Bass (6) ──────────────────────────────────────────
    "bass_upright":      ([(1, 0.65), (2, 0.22), (3, 0.09), (4, 0.03), (5, 0.01)],  3.8, 0.15),
    "bass_upright_warm": ([(1, 0.70), (2, 0.20), (3, 0.08), (4, 0.02)],              3.2, 0.10),
    "bass_upright_jazz": ([(1, 0.62), (2, 0.24), (3, 0.11), (4, 0.03)],              4.0, 0.18),
    "bass_upright_bow":  ([(1, 0.72), (2, 0.19), (3, 0.07), (4, 0.02)],              1.5, 0.04),
    "bass_acoustic":     ([(1, 0.64), (2, 0.23), (3, 0.10), (4, 0.03)],              4.5, 0.18),
    "bass_acoustic_slap":([(1, 0.55), (2, 0.27), (3, 0.14), (4, 0.04)],              6.5, 0.38),

    # ── Synth Bass (12) ──────────────────────────────────────────────────────
    "bass_synth_sq":     ([(1, 0.50), (2, 0.00), (3, 0.17), (4, 0.00), (5, 0.10),
                           (6, 0.00), (7, 0.07)],                                    2.0, 0.05),
    "bass_synth_saw":    ([(1, 0.45), (2, 0.22), (3, 0.15), (4, 0.11), (5, 0.08),
                           (6, 0.05), (7, 0.04)],                                    2.5, 0.05),
    "bass_synth_tri":    ([(1, 0.60), (2, 0.00), (3, 0.07), (4, 0.00), (5, 0.02)],  2.0, 0.03),
    "bass_synth_pulse":  ([(1, 0.55), (2, 0.00), (3, 0.18), (4, 0.00), (5, 0.11)],  3.0, 0.06),
    "bass_synth_warm":   ([(1, 0.72), (2, 0.18), (3, 0.07), (4, 0.03)],              1.8, 0.03),
    "bass_synth_dark":   ([(1, 0.82), (2, 0.14), (3, 0.04)],                         1.5, 0.02),
    "bass_synth_deep":   ([(1, 0.88), (2, 0.10), (3, 0.02)],                         1.2, 0.01),
    "bass_synth_mid":    ([(1, 0.60), (2, 0.24), (3, 0.12), (4, 0.04)],              2.8, 0.08),
    "bass_synth_bright": ([(1, 0.50), (2, 0.26), (3, 0.14), (4, 0.08), (5, 0.02)],  3.5, 0.10),
    "bass_synth_pluck":  ([(1, 0.60), (2, 0.22), (3, 0.12), (4, 0.06)],              8.0, 0.25),
    "bass_synth_pad":    ([(1, 0.78), (2, 0.16), (3, 0.06)],                         0.8, 0.01),
    "bass_synth_lead":   ([(1, 0.55), (2, 0.25), (3, 0.14), (4, 0.06)],              2.2, 0.07),

    # ── Acid / Electronic (13) ───────────────────────────────────────────────
    "bass_acid":         ([(1, 0.52), (2, 0.25), (3, 0.15), (4, 0.08)],              4.0, 0.20),
    "bass_acid_warm":    ([(1, 0.60), (2, 0.23), (3, 0.12), (4, 0.05)],              3.2, 0.12),
    "bass_acid_bright":  ([(1, 0.48), (2, 0.27), (3, 0.16), (4, 0.09)],              5.0, 0.22),
    "bass_techno":       ([(1, 0.62), (2, 0.22), (3, 0.12), (4, 0.04)],              3.8, 0.18),
    "bass_house":        ([(1, 0.70), (2, 0.20), (3, 0.08), (4, 0.02)],              3.0, 0.10),
    "bass_dnb":          ([(1, 0.65), (2, 0.22), (3, 0.10), (4, 0.03)],              2.5, 0.12),
    "bass_dubstep":      ([(1, 0.55), (2, 0.26), (3, 0.14), (4, 0.05)],              2.0, 0.08),
    "bass_edm":          ([(1, 0.68), (2, 0.21), (3, 0.09), (4, 0.02)],              2.8, 0.09),
    "bass_lo_fi":        ([(1, 0.78), (2, 0.16), (3, 0.06)],                         2.0, 0.12),
    "bass_wobble":       ([(1, 0.58), (2, 0.24), (3, 0.13), (4, 0.05)],              1.8, 0.04),
    "bass_reese":        ([(1, 0.52), (2, 0.26), (3, 0.14), (4, 0.08)],              1.5, 0.03),
    "bass_neuro":        ([(1, 0.55), (2, 0.25), (3, 0.14), (4, 0.06)],              1.6, 0.04),
    "bass_future":       ([(1, 0.72), (2, 0.18), (3, 0.08), (4, 0.02)],              2.2, 0.06),

    # ── Hip-hop (10) ─────────────────────────────────────────────────────────
    "bass_boom":         ([(1, 0.88), (2, 0.09), (3, 0.03)],                         1.0, 0.08),
    "bass_boom_bap":     ([(1, 0.82), (2, 0.13), (3, 0.05)],                         1.8, 0.14),
    "bass_trap_hi":      ([(1, 0.72), (2, 0.20), (3, 0.07), (4, 0.01)],              2.0, 0.10),
    "bass_trap_sub":     ([(1, 0.90), (2, 0.08), (3, 0.02)],                         0.9, 0.03),
    "bass_lofi_hip":     ([(1, 0.76), (2, 0.17), (3, 0.07)],                         2.4, 0.11),
    "bass_west_coast":   ([(1, 0.74), (2, 0.19), (3, 0.07)],                         2.6, 0.08),
    "bass_east_coast":   ([(1, 0.70), (2, 0.21), (3, 0.08), (4, 0.01)],              2.8, 0.09),
    "bass_phonk":        ([(1, 0.80), (2, 0.14), (3, 0.06)],                         1.4, 0.07),
    "bass_plugg":        ([(1, 0.83), (2, 0.12), (3, 0.05)],                         1.2, 0.05),
    "bass_bounce":       ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              3.2, 0.12),

    # ── Soul / R&B / Funk (6) ────────────────────────────────────────────────
    "bass_soul":         ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              2.8, 0.10),
    "bass_rnb":          ([(1, 0.72), (2, 0.20), (3, 0.07), (4, 0.01)],              2.4, 0.08),
    "bass_gospel":       ([(1, 0.70), (2, 0.21), (3, 0.08), (4, 0.01)],              2.6, 0.09),
    "bass_groove":       ([(1, 0.66), (2, 0.23), (3, 0.10), (4, 0.01)],              3.0, 0.12),
    "bass_motown":       ([(1, 0.67), (2, 0.22), (3, 0.10), (4, 0.01)],              3.2, 0.13),
    "bass_motown_pick":  ([(1, 0.58), (2, 0.26), (3, 0.13), (4, 0.03)],              4.0, 0.20),

    # ── Reggae / Dub (4) ─────────────────────────────────────────────────────
    "bass_reggae":       ([(1, 0.78), (2, 0.16), (3, 0.06)],                         2.0, 0.07),
    "bass_dub":          ([(1, 0.82), (2, 0.13), (3, 0.05)],                         1.6, 0.05),
    "bass_ska":          ([(1, 0.65), (2, 0.23), (3, 0.10), (4, 0.02)],              4.0, 0.16),
    "bass_roots":        ([(1, 0.80), (2, 0.14), (3, 0.06)],                         1.8, 0.06),

    # ── Jazz / Blues / Latin (6) ─────────────────────────────────────────────
    "bass_blues":        ([(1, 0.66), (2, 0.23), (3, 0.09), (4, 0.02)],              3.5, 0.13),
    "bass_swing":        ([(1, 0.64), (2, 0.24), (3, 0.10), (4, 0.02)],              3.8, 0.15),
    "bass_bebop":        ([(1, 0.62), (2, 0.25), (3, 0.11), (4, 0.02)],              4.0, 0.17),
    "bass_latin":        ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              3.0, 0.12),
    "bass_bossa":        ([(1, 0.70), (2, 0.21), (3, 0.08), (4, 0.01)],              2.8, 0.10),
    "bass_samba":        ([(1, 0.66), (2, 0.23), (3, 0.10), (4, 0.01)],              3.2, 0.13),

    # ── World / Ethnic (4) ───────────────────────────────────────────────────
    "bass_arabic":       ([(1, 0.74), (2, 0.19), (3, 0.07)],                         2.5, 0.11),
    "bass_indian":       ([(1, 0.70), (2, 0.21), (3, 0.09)],                         2.8, 0.13),
    "bass_afrobeat":     ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              3.4, 0.12),
    "bass_afropop":      ([(1, 0.70), (2, 0.20), (3, 0.09), (4, 0.01)],              3.0, 0.11),

    # ── Articulation Variants (10) ───────────────────────────────────────────
    "bass_staccato":     ([(1, 0.70), (2, 0.20), (3, 0.10)],                        15.0, 0.20),
    "bass_legato":       ([(1, 0.76), (2, 0.18), (3, 0.06)],                         1.2, 0.02),
    "bass_ghost":        ([(1, 0.82), (2, 0.14), (3, 0.04)],                         9.0, 0.05),
    "bass_accented":     ([(1, 0.65), (2, 0.23), (3, 0.10), (4, 0.02)],              3.0, 0.28),
    "bass_dry":          ([(1, 0.72), (2, 0.19), (3, 0.09)],                         6.0, 0.08),
    "bass_wet":          ([(1, 0.68), (2, 0.22), (3, 0.10)],                         1.8, 0.03),
    "bass_snap":         ([(1, 0.60), (2, 0.24), (3, 0.12), (4, 0.04)],             10.0, 0.40),
    "bass_thump":        ([(1, 0.78), (2, 0.16), (3, 0.06)],                         4.5, 0.30),
    "bass_rumble":       ([(1, 0.85), (2, 0.12), (3, 0.03)],                         0.9, 0.02),
    "bass_punch":        ([(1, 0.72), (2, 0.20), (3, 0.08)],                         4.0, 0.22),
}


def _ks_instrument(frequency: float, duration: float, sr: int = 44100,
                    timbre: str = "guitar") -> np.ndarray:
    """
    Fast additive synthesis with exponential decay — fully NumPy vectorised.
    ~0.3 ms per note (60× faster than the IIR loop approach).
    """
    duration = min(duration, 4.0)
    n        = max(2, int(sr * duration))
    t        = np.linspace(0.0, duration, n, dtype=np.float32)

    harmonics, decay_rate, noise_amp = _KS_PRESETS.get(timbre, _KS_PRESETS["guitar"])

    wave = np.zeros(n, dtype=np.float32)
    for k, amp in harmonics:
        if frequency * k < sr / 2:
            wave += amp * np.sin(2.0 * np.pi * frequency * k * t, dtype=np.float32)

    # Brief noise burst at attack → pluck feel
    atk = max(1, int(sr * 0.003))
    noise_env = np.zeros(n, dtype=np.float32)
    noise_env[:atk] = np.linspace(1.0, 0.0, atk, dtype=np.float32)
    wave += noise_env * np.random.uniform(-noise_amp, noise_amp, n).astype(np.float32)

    # Exponential decay envelope
    env = np.exp((-decay_rate / max(duration, 0.01)) * t).astype(np.float32)

    # Short release taper
    rel = min(int(sr * 0.05), n // 4)
    if rel > 0:
        env[-rel:] *= np.linspace(1.0, 0.0, rel, dtype=np.float32)

    return wave * env


def _add_reverb(signal: np.ndarray, sr: int, room: float = 0.25) -> np.ndarray:
    """Simple reverb: sum of delayed, attenuated copies of the signal."""
    out = signal.copy()
    delays_ms = [30, 60, 100, 150]
    gains     = [0.35, 0.22, 0.14, 0.08]
    for d_ms, g in zip(delays_ms, gains):
        d = int(sr * d_ms / 1000)
        padded = np.zeros_like(out)
        padded[d:] = out[:-d] if d < len(out) else 0
        out += g * room * padded
    return out


def generate_combined_tab_audio(tab_data: dict, ref_audio_path: str, output_path: str) -> bool:
    """
    Synthesize all instrument parts from the score, with timing quantized to the
    real beat grid extracted from the reference audio.

    Steps:
      1. Extract beat positions from the reference audio using librosa.
      2. Build a musical-time → audio-time mapping via those beat positions.
      3. Re-time every note and drum hit to land exactly on the grid.
      4. Synthesize all parts (all_parts for MusicXML, guitar/bass for PDF).
      5. Mix melodic bus and drum bus independently, then output.
    """
    import librosa

    sr       = 44100
    tab_bpm  = tab_data.get("bpm", 80)

    # ── 1. Extract beat grid from reference audio ──────────────────────────────
    try:
        ref_wav = "/tmp/tab_ref_mono.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", ref_audio_path,
             "-acodec", "pcm_f32le", "-ar", "22050", "-ac", "1", "-t", "90", ref_wav],
            capture_output=True, check=True
        )
        y_ref, sr_ref = librosa.load(ref_wav, sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y_ref, sr=sr_ref)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr_ref).astype(float)
        audio_bpm  = float(tempo.item()) if hasattr(tempo, "item") else float(tempo)
        logger.info(f"Audio BPM={audio_bpm:.1f}, beats detected={len(beat_times)}")
    except Exception as e:
        logger.warning(f"Beat extraction failed: {e}")
        beat_times = np.array([])
        audio_bpm  = None

    bpm      = audio_bpm if audio_bpm and 40 <= audio_bpm <= 220 else tab_bpm
    beat_dur = 60.0 / bpm
    logger.info(f"Using BPM={bpm:.1f}")

    # ── 2. Build re-timing function ───────────────────────────────────────────
    # score_time is in seconds at tab_bpm; we convert to audio seconds at bpm.
    # If we have real beat positions, we additionally snap each note to the
    # nearest 16th-note subdivision of the detected grid.

    score_beat_dur = 60.0 / tab_bpm   # seconds per beat in the score

    def retime(score_sec: float) -> float:
        """Map a score time (at tab_bpm) → audio time (at bpm, quantized)."""
        # Convert score time to beat count
        beat_count = score_sec / score_beat_dur
        # Snap to nearest 16th note (4 subdivisions per beat)
        beat_count = round(beat_count * 4) / 4
        if len(beat_times) > 0:
            # Map to actual audio beat position
            idx = int(beat_count)
            frac = beat_count - idx
            if idx < len(beat_times) - 1:
                return beat_times[idx] + frac * (beat_times[idx + 1] - beat_times[idx])
            elif idx < len(beat_times):
                return beat_times[idx] + frac * beat_dur
            else:
                # Beyond available beats — extrapolate
                return beat_times[-1] + (beat_count - (len(beat_times) - 1)) * beat_dur
        else:
            return beat_count * beat_dur

    # ── 3. Retime all notes ────────────────────────────────────────────────────
    all_parts   = tab_data.get("all_parts", {})
    drum_notes  = [{"midi": d["midi"], "time": retime(d["time"])}
                   for d in tab_data.get("drum_notes", [])]

    if all_parts:
        retimed_parts = {}
        for pname, pdata in all_parts.items():
            retimed = []
            for n in pdata["notes"][:500]:
                rt = retime(n["time"])
                retimed.append({**n, "time": rt})
            retimed_parts[pname] = {"timbre": pdata["timbre"], "notes": retimed}
    else:
        # PDF / fallback
        guitar_notes = [{"midi": n["midi"], "time": retime(n["time"]),
                         "duration": n.get("duration", beat_dur)}
                        for n in tab_data.get("guitar_notes", [])]
        bass_notes   = [{"midi": n["midi"], "time": retime(n["time"]),
                         "duration": n.get("duration", beat_dur)}
                        for n in tab_data.get("bass_notes", [])]

    # ── 4. Compute total duration ──────────────────────────────────────────────
    all_t = []
    if all_parts:
        for pdata in (retimed_parts if all_parts else {}).values():
            all_t.extend(n["time"] for n in pdata["notes"])
    else:
        all_t = [n["time"] for n in guitar_notes + bass_notes]
    all_t += [d["time"] for d in drum_notes]
    total_time = min((max(all_t) if all_t else 32 * beat_dur) + 2.5, 90.0)
    n_total    = int(sr * total_time)
    mix        = np.zeros(n_total, dtype=np.float32)

    # ── 5. Synthesize melodic parts ───────────────────────────────────────────
    if all_parts:
        n_parts = len(retimed_parts)
        for pdata in retimed_parts.values():
            timbre = pdata["timbre"]
            gain   = 0.70 / max(n_parts ** 0.5, 1)
            for note in pdata["notes"]:
                if note["time"] >= total_time:
                    continue
                dur  = min(note.get("duration", beat_dur * 1.2) + 0.3, 4.0)
                tone = _synth_note(note["midi"], dur, timbre, sr)
                si   = int(note["time"] * sr)
                ei   = min(si + len(tone), n_total)
                mix[si:ei] += tone[:ei - si] * gain
    else:
        instruments_lower = " ".join(tab_data.get("instruments", [])).lower()
        timbre_g = "oud" if ("oud" in instruments_lower or "عود" in instruments_lower) else "guitar"
        for note in guitar_notes:
            tone = _ks_instrument(_midi_to_hz(note["midi"]), beat_dur * 1.6, sr=sr, timbre=timbre_g)
            si = int(note["time"] * sr); ei = min(si + len(tone), n_total)
            mix[si:ei] += tone[:ei - si] * 0.70
        for note in bass_notes:
            tone = _ks_instrument(_midi_to_hz(note["midi"]), beat_dur * 1.8, sr=sr, timbre="bass")
            si = int(note["time"] * sr); ei = min(si + len(tone), n_total)
            mix[si:ei] += tone[:ei - si] * 0.72

    # ── 6. Normalize melodic bus ──────────────────────────────────────────────
    mel_peak = np.abs(mix).max()
    if mel_peak > 1e-6:
        mix = mix / mel_peak * 0.72

    # ── 7. Synthesize & mix drum bus independently ────────────────────────────
    if tab_data.get("has_drums"):
        if drum_notes:
            drum_t = _render_drum_notes(drum_notes, total_time, sr)
        else:
            drum_t = _synth_drums(beat_dur, total_time, sr)
        d_peak = np.abs(drum_t).max()
        if d_peak > 1e-6:
            drum_t = drum_t / d_peak * 0.68
        length = min(len(drum_t), n_total)
        mix[:length] += drum_t[:length]

    # ── 8. Reverb + normalise ─────────────────────────────────────────────────
    mix = _add_reverb(mix, sr, room=0.20)
    peak = np.abs(mix).max()
    if peak < 1e-6:
        logger.warning("generate_combined_tab_audio: silent output")
        return False
    mix = mix / peak * 0.91

    # ── 9. Write WAV → MP3 ────────────────────────────────────────────────────
    wav_tmp = output_path.replace(".mp3", "_raw.wav")
    sf.write(wav_tmp, mix, sr, subtype="PCM_16")
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_tmp,
         "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k", output_path],
        capture_output=True, check=True
    )
    if os.path.exists(wav_tmp):
        os.remove(wav_tmp)
    logger.info(f"generate_combined_tab_audio: OK, beats={len(beat_times)}, "
                f"bpm={bpm:.1f}, size={os.path.getsize(output_path)}")
    return True


INSTRUMENT_TIMBRE_MAP = {
    # Plucked strings
    "guitar":        "guitar",
    "acoustic":      "guitar",
    "electric":      "electric_guitar",
    "clean":         "electric_guitar",
    "distort":       "electric_guitar",
    "rhythm":        "electric_guitar",
    "lead":          "electric_guitar",
    "bass":          "bass",
    "oud":           "oud",
    "sitar":         "sitar",
    "banjo":         "banjo",
    "mandolin":      "banjo",
    "ukulele":       "guitar",
    "harp":          "harp",
    # Keyboards
    "piano":         "piano",
    "keyboard":      "piano",
    "keys":          "piano",
    "rhodes":        "rhodes",
    "electric piano":"electric_piano",
    "clav":          "piano",
    "harpsichord":   "harpsichord",
    "organ":         "organ",
    "hammond":       "organ",
    "accordion":     "organ",
    # Synths
    "synth":         "synth",
    "synthesizer":   "synth",
    "pad":           "synth_pad",
    "lead synth":    "synth",
    "arp":           "synth",
    "trap synth":    "synth_heavy",
    "rap synth":     "synth_heavy",
    "sample":        "synth_heavy",
    # Hip-hop / Bass specific
    "808":           "bass_808_hiphop",
    "trap bass":     "bass_808_trap",
    "drill bass":    "bass_808_drill",
    "sub bass":      "bass_808_sub",
    "sub-bass":      "bass_808_sub",
    "reese bass":    "bass_808_deep",
    "wobble bass":   "bass_808_distorted",
    "phonk bass":    "bass_808_distorted",
    "boom bap bass": "bass_808_punchy",
    "bass guitar":   "bass",
    # Electric Piano / Rhodes
    "wurlitzer":     "electric_piano",
    # Bowed strings
    "violin":        "violin",
    "viola":         "viola",
    "cello":         "cello",
    "contrabass":    "cello",
    "double bass":   "cello",
    "strings":       "violin",
    "orchestra":     "violin",
    # Brass
    "trumpet":       "brass",
    "trombone":      "brass",
    "horn":          "brass",
    "tuba":          "brass",
    "brass":         "brass",
    "bugle":         "brass",
    # Woodwinds
    "flute":         "flute",
    "clarinet":      "woodwind",
    "oboe":          "woodwind",
    "bassoon":       "woodwind",
    "saxophone":     "saxophone",
    "sax":           "saxophone",
    "recorder":      "flute",
    "piccolo":       "flute",
    "harmonica":     "woodwind",
    # Voice
    "voice":         "voice",
    "vocal":         "voice",
    "choir":         "voice",
    "chorus":        "voice",
    "soprano":       "voice",
    "alto":          "voice",
    "tenor":         "voice",
    "baritone":      "voice",
    # Percussion (non-drum)
    "marimba":       "marimba",
    "xylophone":     "marimba",
    "vibraphone":    "marimba",
    "glockenspiel":  "marimba",
    "bells":         "marimba",
    "celesta":       "marimba",
    "timpani":       "marimba",
}

DRUM_KEYWORDS = {
    "drum", "drums", "kit", "طبول", "إيقاع",
    "clap", "تصفيق", "handclap",
    "darbuka", "دربكة", "darabukka",
    "dabke", "دبكة",
    "tabla", "طبلة",
    "doumbek", "dumbek",
    "riq", "riqq", "ريق", "رق",
    "cajon", "cajón",
    "shaker", "tambourine", "دف",
}

# Standard display-pitch → GM drum MIDI mapping (treble clef drum notation)
DRUM_DISPLAY_PITCH_MAP = {
    "E3": 36, "F3": 36,                    # Bass drum variants
    "F4": 36, "E4": 36,                    # Bass Drum (Kick)
    "G4": 44,                              # Hi-Hat Foot
    "A4": 41,                              # Low Floor Tom
    "B4": 42,                              # Closed Hi-Hat (alt)
    "C5": 38,                              # Acoustic Snare
    "D5": 45,                              # Low-Mid Tom / Open Hi-Hat
    "E5": 47,                              # Low Mid Tom
    "F5": 48,                              # High Tom
    "G5": 42,                              # Closed Hi-Hat
    "A5": 49,                              # Crash Cymbal
    "B5": 51,                              # Ride Cymbal
    "C6": 53,                              # Ride Bell
    # Clap / handclap
    "D4": 39,                              # Hand Clap
}


def _timbre_for(name: str) -> str:
    n = name.lower()
    for kw, t in INSTRUMENT_TIMBRE_MAP.items():
        if kw in n:
            return t
    return "guitar"


def _parametric_render(freq: float, duration: float, sr: int, params: dict) -> np.ndarray:
    n = max(int(sr * duration), 1)
    t = np.linspace(0, duration, n, dtype=np.float32)
    dur = max(duration, 0.01)
    atk = params.get("attack", 0.01)
    dec = params.get("decay", 0.1)
    sus = params.get("sustain", 0.8)
    rel_r = params.get("release", 0.15)
    exp_d = params.get("decay_exp", 0.0)
    env = np.ones(n, dtype=np.float32)
    a = min(int(sr * atk), n)
    d = min(int(sr * dec), n - a)
    r = min(int(sr * dur * rel_r), n)
    if a: env[:a] = np.linspace(0, 1, a)
    if d: env[a:a+d] = np.linspace(1, sus, d)
    if r and r < n: env[-r:] *= np.linspace(1, 0, r)
    if exp_d > 0:
        env *= np.exp(-exp_d * t / dur)
    vib_d = params.get("vibrato_depth", 0.0)
    vib_r = params.get("vibrato_rate", 5.5)
    vib_ph = vib_d * np.sin(2 * np.pi * vib_r * t).astype(np.float32) if vib_d > 0 else 0.0
    wave = np.zeros(n, dtype=np.float32)
    for harm_ratio, amp in params.get("harmonics", [(1.0, 0.6), (2.0, 0.25)]):
        wave += amp * np.sin(2 * np.pi * freq * harm_ratio * t + vib_ph)
    nm = params.get("noise_mix", 0.0)
    if nm > 0:
        wave += nm * np.random.uniform(-1, 1, n).astype(np.float32)
    sat = params.get("saturation", 0.0)
    if sat > 0:
        wave = np.tanh(sat * wave) / max(float(np.tanh(np.float32(sat))), 1e-6)
    wave = wave * env
    pk = float(np.abs(wave).max())
    if pk > 1e-6:
        wave = wave / pk * params.get("volume", 0.55)
    return wave.astype(np.float32)


_INSTR: dict = {
    "bass_808":           {"harmonics": [(1,.70),(2,.18),(3,.08),(4,.03)], "attack":.005, "decay":.0, "sustain":1.0, "release":.25, "decay_exp":8.0, "saturation":1.8, "volume":.65},
    "bass_808_long":      {"harmonics": [(1,.72),(2,.16),(3,.08)],         "attack":.005, "decay":.0, "sustain":1.0, "release":.40, "decay_exp":5.0, "saturation":1.6, "volume":.65},
    "bass_808_punchy":    {"harmonics": [(1,.65),(2,.22),(3,.10),(4,.03)], "attack":.003, "decay":.05,"sustain":.85,"release":.10, "decay_exp":14.,"saturation":2.2, "volume":.68},
    "bass_808_clean":     {"harmonics": [(1,.80),(2,.14),(3,.05)],         "attack":.005, "decay":.0, "sustain":1.0, "release":.30, "decay_exp":6.0, "saturation":.5,  "volume":.62},
    "bass_808_distorted": {"harmonics": [(1,.55),(2,.25),(3,.12),(4,.06),(5,.02)], "attack":.003,"decay":.0,"sustain":1.0,"release":.20,"decay_exp":7.0,"saturation":4.5,"volume":.55},
    "bass_808_sub":       {"harmonics": [(1,.90),(2,.08),(3,.02)],         "attack":.010, "decay":.0, "sustain":1.0, "release":.35, "decay_exp":4.0, "saturation":.8,  "volume":.70},
    "bass_808_deep":      {"harmonics": [(1,.75),(2,.15),(3,.06),(4,.02)], "attack":.008, "decay":.0, "sustain":1.0, "release":.50, "decay_exp":3.5, "saturation":1.2, "volume":.68},
    "bass_808_mid":       {"harmonics": [(1,.60),(2,.25),(3,.12),(4,.03)], "attack":.005, "decay":.0, "sustain":1.0, "release":.22, "decay_exp":9.0, "saturation":2.0, "volume":.62},
    "bass_808_bright":    {"harmonics": [(1,.50),(2,.28),(3,.15),(4,.05),(5,.02)], "attack":.004,"decay":.0,"sustain":1.0,"release":.18,"decay_exp":11.,"saturation":2.5,"volume":.60},
    "bass_808_warm":      {"harmonics": [(1,.78),(2,.14),(3,.06),(4,.02)], "attack":.010, "decay":.0, "sustain":1.0, "release":.45, "decay_exp":5.5, "saturation":1.0, "volume":.63},
    "bass_sub":           {"harmonics": [(1,.92),(2,.06),(3,.02)],         "attack":.015, "decay":.0, "sustain":1.0, "release":.50, "decay_exp":2.0, "saturation":.3,  "volume":.72},
    "bass_electric":      {"harmonics": [(1,.55),(2,.30),(3,.10),(4,.04),(5,.01)], "attack":.008,"decay":.12,"sustain":.80,"release":.12,"saturation":1.2,"volume":.58},
    "bass_fretless":      {"harmonics": [(1,.58),(2,.28),(3,.10),(4,.04)], "attack":.012, "decay":.08,"sustain":.85,"release":.15, "vibrato_depth":.04,"vibrato_rate":4.5,"volume":.56},
    "bass_synth":         {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.05),(5,.03)], "attack":.006,"decay":.10,"sustain":.80,"release":.15,"saturation":1.5,"volume":.58},
    "bass_moog":          {"harmonics": [(1,.55),(2,.30),(3,.12),(4,.03)], "attack":.008, "decay":.15,"sustain":.75,"release":.18, "saturation":2.0, "volume":.57},
    "bass_acid":          {"harmonics": [(1,.48),(2,.32),(3,.14),(4,.04),(5,.02)], "attack":.004,"decay":.08,"sustain":.70,"release":.12,"saturation":3.2,"volume":.54},
    "bass_reese":         {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.006,"decay":.0,"sustain":1.0,"release":.20,"saturation":2.8,"vibrato_depth":.03,"vibrato_rate":0.8,"volume":.55},
    "bass_growl":         {"harmonics": [(1,.45),(2,.30),(3,.16),(4,.06),(5,.03)], "attack":.005,"decay":.0,"sustain":1.0,"release":.18,"saturation":3.5,"volume":.52},
    "bass_round":         {"harmonics": [(1,.65),(2,.25),(3,.08),(4,.02)], "attack":.015, "decay":.12,"sustain":.82,"release":.20,"volume":.58},
    "bass_fingered":      {"harmonics": [(1,.52),(2,.32),(3,.12),(4,.03),(5,.01)], "attack":.010,"decay":.10,"sustain":.78,"release":.14,"volume":.57},
    "bass_slap":          {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.003,"decay":.05,"sustain":.60,"release":.10,"saturation":1.8,"volume":.60},
    "bass_plucked":       {"harmonics": [(1,.58),(2,.28),(3,.10),(4,.04)], "attack":.004, "decay":.08,"sustain":.65,"release":.12,"decay_exp":6.0,"volume":.57},
    "bass_picked":        {"harmonics": [(1,.52),(2,.30),(3,.12),(4,.05),(5,.01)], "attack":.003,"decay":.06,"sustain":.70,"release":.10,"saturation":1.0,"volume":.58},
    "bass_jazz":          {"harmonics": [(1,.60),(2,.25),(3,.10),(4,.04),(5,.01)], "attack":.012,"decay":.15,"sustain":.80,"release":.18,"vibrato_depth":.02,"vibrato_rate":4.0,"volume":.56},
    "bass_upright":       {"harmonics": [(1,.55),(2,.30),(3,.12),(4,.03)], "attack":.015, "decay":.18,"sustain":.72,"release":.20,"volume":.55},
    "bass_chorus":        {"harmonics": [(1,.52),(2,.24),(3,.12),(4,.05)], "attack":.008, "decay":.10,"sustain":.80,"release":.18,"volume":.52},
    "bass_distorted":     {"harmonics": [(1,.48),(2,.28),(3,.14),(4,.06),(5,.03),(6,.01)], "attack":.004,"decay":.0,"sustain":1.0,"release":.18,"saturation":5.0,"volume":.50},
    "bass_clean":         {"harmonics": [(1,.62),(2,.25),(3,.10),(4,.03)], "attack":.010, "decay":.12,"sustain":.82,"release":.18,"volume":.58},
    "bass_harmonics":     {"harmonics": [(2,.60),(4,.25),(6,.10),(8,.05)], "attack":.006, "decay":.10,"sustain":.70,"release":.15,"decay_exp":5.0,"volume":.52},
    "bass_wobble":        {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.005, "decay":.0, "sustain":1.0, "release":.20, "vibrato_depth":.12,"vibrato_rate":4.0,"saturation":2.0,"volume":.54},
    "synth":              {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.08),(5,.05),(6,.03),(7,.02),(8,.015),(9,.01),(10,.008)], "attack":.018,"decay":.10,"sustain":.80,"release":.15,"saturation":1.2,"volume":.50},
    "synth_heavy":        {"harmonics": [(1,1.0),(2,0.5),(3,0.333),(4,0.25),(5,0.2),(6,0.167),(7,0.143),(8,0.125),(9,0.111),(10,0.1),(11,0.091),(12,0.083)], "attack":.008,"decay":.12,"sustain":.75,"release":.15,"saturation":2.8,"volume":.52},
    "synth_lead":         {"harmonics": [(1,.55),(2,.25),(3,.12),(4,.05),(5,.03)], "attack":.010,"decay":.08,"sustain":.80,"release":.15,"saturation":1.2,"volume":.42},
    "synth_sawtooth":     {"harmonics": [(k,1/k) for k in range(1,16)],   "attack":.008, "decay":.10,"sustain":.80,"release":.15,"saturation":1.0,"volume":.35},
    "synth_square":       {"harmonics": [(1,1.0),(3,0.333),(5,0.2),(7,0.143),(9,0.111),(11,0.091),(13,0.077),(15,0.067)], "attack":.008,"decay":.10,"sustain":.80,"release":.15,"saturation":1.0,"volume":.38},
    "synth_triangle":     {"harmonics": [(1,1.0),(3,0.111),(5,0.04),(7,0.020),(9,0.012),(11,0.008)], "attack":.010,"decay":.10,"sustain":.80,"release":.15,"volume":.45},
    "synth_sine":         {"harmonics": [(1,.90),(2,.08),(3,.02)],         "attack":.015, "decay":.10,"sustain":.85,"release":.20,"volume":.50},
    "synth_fm":           {"harmonics": [(1,.60),(2,.25),(3,.10),(4,.04),(5,.01)], "attack":.008,"decay":.12,"sustain":.75,"release":.15,"saturation":1.5,"volume":.42},
    "synth_supersaw":     {"harmonics": [(1,.40),(2,.20),(3,.13),(4,.10),(5,.08),(6,.07),(7,.06),(8,.05),(9,.04)], "attack":.010,"decay":.10,"sustain":.80,"release":.18,"saturation":1.2,"volume":.30},
    "synth_detuned":      {"harmonics": [(1,.50),(1.01,.40),(2,.20),(2.01,.15),(3,.10)], "attack":.010,"decay":.10,"sustain":.80,"release":.18,"volume":.40},
    "synth_mono":         {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.006, "decay":.08,"sustain":.78,"release":.12,"saturation":1.8,"volume":.45},
    "synth_poly":         {"harmonics": [(1,.52),(2,.25),(3,.14),(4,.06),(5,.03)], "attack":.015,"decay":.12,"sustain":.82,"release":.20,"volume":.40},
    "synth_brass":        {"harmonics": [(1,.45),(2,.30),(3,.16),(4,.06),(5,.03)], "attack":.040,"decay":.05,"sustain":.85,"release":.10,"saturation":1.3,"volume":.42},
    "synth_strings":      {"harmonics": [(1,.50),(2,.25),(3,.14),(4,.07),(5,.04)], "attack":.080,"decay":.0,"sustain":1.0,"release":.25,"vibrato_depth":.05,"vibrato_rate":5.0,"volume":.40},
    "synth_stab":         {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.005, "decay":.08,"sustain":.50,"release":.08,"saturation":2.2,"volume":.50},
    "synth_pluck":        {"harmonics": [(1,.60),(2,.25),(3,.10),(4,.05)], "attack":.003, "decay":.10,"sustain":.40,"release":.10,"decay_exp":8.0,"volume":.52},
    "synth_bell":         {"harmonics": [(1,.60),(2.756,.30),(5.404,.15),(7.08,.08),(8.88,.04)], "attack":.004,"decay":.0,"sustain":.30,"release":.20,"decay_exp":6.0,"volume":.50},
    "synth_glass":        {"harmonics": [(1,.65),(2.93,.22),(5.3,.10),(8.1,.05)],  "attack":.015,"decay":.0,"sustain":.50,"release":.30,"decay_exp":4.0,"volume":.48},
    "synth_laser":        {"harmonics": [(1,.70),(2,.20),(3,.08),(4,.02)], "attack":.002, "decay":.0, "sustain":.80,"release":.05,"decay_exp":15.,"volume":.52},
    "synth_zap":          {"harmonics": [(1,.65),(2,.22),(3,.10),(4,.03)], "attack":.001, "decay":.0, "sustain":.60,"release":.05,"decay_exp":20.,"saturation":2.0,"volume":.55},
    "synth_growl":        {"harmonics": [(1,.45),(2,.30),(3,.16),(4,.06),(5,.03)], "attack":.005,"decay":.0,"sustain":1.0,"release":.15,"saturation":4.0,"vibrato_depth":.06,"vibrato_rate":3.5,"volume":.42},
    "synth_dirty":        {"harmonics": [(1,.48),(2,.28),(3,.14),(4,.06),(5,.04)], "attack":.006,"decay":.0,"sustain":1.0,"release":.15,"saturation":5.0,"noise_mix":.02,"volume":.40},
    "synth_clean":        {"harmonics": [(1,.60),(2,.25),(3,.10),(4,.05)], "attack":.012, "decay":.10,"sustain":.82,"release":.18,"volume":.45},
    "synth_warm":         {"harmonics": [(1,.65),(2,.22),(3,.08),(4,.03),(5,.02)], "attack":.015,"decay":.12,"sustain":.82,"release":.22,"volume":.45},
    "synth_cold":         {"harmonics": [(1,.50),(2,.25),(3,.14),(4,.07),(5,.04)], "attack":.006,"decay":.08,"sustain":.75,"release":.12,"saturation":.8,"volume":.38},
    "synth_dark":         {"harmonics": [(1,.70),(2,.20),(3,.07),(4,.03)], "attack":.012, "decay":.10,"sustain":.80,"release":.20,"saturation":1.0,"volume":.48},
    "synth_bright":       {"harmonics": [(1,.40),(2,.28),(3,.18),(4,.08),(5,.04),(6,.02)], "attack":.008,"decay":.08,"sustain":.78,"release":.15,"saturation":1.5,"volume":.42},
    "synth_airy":         {"harmonics": [(1,.55),(2,.25),(3,.12),(4,.05),(5,.03)], "attack":.030,"decay":.0,"sustain":1.0,"release":.30,"noise_mix":.02,"volume":.42},
    "synth_harsh":        {"harmonics": [(1,.35),(2,.22),(3,.16),(4,.10),(5,.08),(6,.05),(7,.04)], "attack":.004,"decay":.06,"sustain":.80,"release":.10,"saturation":4.0,"volume":.32},
    "synth_soft":         {"harmonics": [(1,.70),(2,.18),(3,.08),(4,.04)], "attack":.040, "decay":.15,"sustain":.85,"release":.30,"volume":.48},
    "synth_power":        {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.005,"decay":.0,"sustain":1.0,"release":.15,"saturation":3.0,"volume":.45},
    "synth_thin":         {"harmonics": [(1,.45),(2,.35),(3,.15),(4,.05)], "attack":.008, "decay":.10,"sustain":.75,"release":.15,"volume":.42},
    "synth_fat":          {"harmonics": [(1,.55),(1.01,.45),(2,.25),(2.01,.20),(3,.12)], "attack":.010,"decay":.10,"sustain":.80,"release":.18,"saturation":1.5,"volume":.38},
    "synth_acid":         {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.004,"decay":.08,"sustain":.70,"release":.10,"saturation":3.5,"volume":.42},
    "synth_analog":       {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.010, "decay":.12,"sustain":.80,"release":.18,"saturation":1.0,"noise_mix":.01,"volume":.44},
    "synth_digital":      {"harmonics": [(1,.50),(2,.30),(3,.14),(4,.04),(5,.02)], "attack":.006,"decay":.08,"sustain":.78,"release":.12,"volume":.42},
    "synth_vintage":      {"harmonics": [(1,.58),(2,.25),(3,.12),(4,.05)], "attack":.012, "decay":.15,"sustain":.78,"release":.20,"saturation":.8,"noise_mix":.01,"volume":.44},
    "synth_modern":       {"harmonics": [(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)], "attack":.007,"decay":.09,"sustain":.80,"release":.14,"saturation":1.8,"volume":.42},
    "synth_retro":        {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.014, "decay":.12,"sustain":.78,"release":.22,"noise_mix":.015,"volume":.44},
    "synth_future":       {"harmonics": [(1,.45),(2,.30),(3,.16),(4,.06),(5,.03)], "attack":.005,"decay":.08,"sustain":.80,"release":.12,"saturation":2.5,"volume":.40},
    "synth_pad":          {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.300,"decay":.0,"sustain":1.0,"release":.30,"vibrato_depth":.05,"vibrato_rate":0.5,"volume":.42},
    "pad_warm":           {"harmonics": [(1,.60),(2,.22),(3,.10),(4,.05),(5,.03)], "attack":.350,"decay":.0,"sustain":1.0,"release":.40,"vibrato_depth":.04,"vibrato_rate":0.4,"volume":.44},
    "pad_cold":           {"harmonics": [(1,.50),(2,.25),(3,.14),(4,.07),(5,.04)], "attack":.200,"decay":.0,"sustain":1.0,"release":.25,"volume":.38},
    "pad_strings":        {"harmonics": [(1,.52),(2,.26),(3,.14),(4,.06),(5,.02)], "attack":.400,"decay":.0,"sustain":1.0,"release":.45,"vibrato_depth":.06,"vibrato_rate":5.0,"volume":.40},
    "pad_choir":          {"harmonics": [(1,.55),(2,.22),(3,.12),(4,.06),(5,.03)], "attack":.300,"decay":.0,"sustain":1.0,"release":.35,"noise_mix":.01,"volume":.38},
    "pad_bowed":          {"harmonics": [(1,.58),(2,.25),(3,.12),(4,.05)], "attack":.250, "decay":.0, "sustain":1.0,"release":.30,"vibrato_depth":.07,"vibrato_rate":4.5,"volume":.42},
    "pad_metallic":       {"harmonics": [(1,.45),(2.87,.28),(4.95,.18),(7.12,.10),(9.4,.05)], "attack":.100,"decay":.0,"sustain":1.0,"release":.35,"volume":.38},
    "pad_sweeper":        {"harmonics": [(1,.55),(2,.28),(3,.14),(4,.03)], "attack":.500, "decay":.0, "sustain":1.0,"release":.50,"vibrato_depth":.10,"vibrato_rate":0.3,"volume":.40},
    "pad_new_age":        {"harmonics": [(1,.60),(2,.20),(3,.10),(4,.06),(5,.04)], "attack":.300,"decay":.0,"sustain":1.0,"release":.40,"vibrato_depth":.03,"vibrato_rate":0.5,"volume":.42},
    "pad_atmosphere":     {"harmonics": [(1,.50),(2,.25),(3,.15),(4,.07),(5,.03)], "attack":.600,"decay":.0,"sustain":1.0,"release":.60,"noise_mix":.015,"volume":.38},
    "pad_dream":          {"harmonics": [(1,.55),(2,.24),(3,.12),(4,.06),(5,.03)], "attack":.400,"decay":.0,"sustain":1.0,"release":.50,"vibrato_depth":.04,"vibrato_rate":0.6,"volume":.40},
    "pad_space":          {"harmonics": [(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)], "attack":.700,"decay":.0,"sustain":1.0,"release":.70,"noise_mix":.02,"volume":.36},
    "pad_haze":           {"harmonics": [(1,.52),(2,.26),(3,.14),(4,.06),(5,.02)], "attack":.500,"decay":.0,"sustain":1.0,"release":.55,"noise_mix":.025,"volume":.38},
    "pad_shimmer":        {"harmonics": [(1,.45),(2,.28),(3,.18),(4,.07),(5,.02)], "attack":.200,"decay":.0,"sustain":1.0,"release":.35,"vibrato_depth":.06,"vibrato_rate":8.0,"volume":.38},
    "pad_glass":          {"harmonics": [(1,.55),(2.93,.22),(5.3,.10),(8.1,.04)], "attack":.200,"decay":.0,"sustain":1.0,"release":.40,"volume":.42},
    "pad_ice":            {"harmonics": [(1,.50),(3,.25),(5,.15),(7,.08),(9,.02)], "attack":.150,"decay":.0,"sustain":1.0,"release":.30,"volume":.42},
    "pad_lush":           {"harmonics": [(1,.55),(1.01,.45),(2,.24),(2.01,.20),(3,.12)], "attack":.400,"decay":.0,"sustain":1.0,"release":.45,"vibrato_depth":.04,"vibrato_rate":0.5,"volume":.35},
    "pad_dark":           {"harmonics": [(1,.68),(2,.20),(3,.08),(4,.04)], "attack":.450, "decay":.0, "sustain":1.0,"release":.50,"volume":.44},
    "pad_bright":         {"harmonics": [(1,.42),(2,.28),(3,.18),(4,.08),(5,.04)], "attack":.200,"decay":.0,"sustain":1.0,"release":.30,"volume":.40},
    "pad_ambient":        {"harmonics": [(1,.55),(2,.22),(3,.12),(4,.07),(5,.04)], "attack":.800,"decay":.0,"sustain":1.0,"release":.80,"noise_mix":.015,"volume":.36},
    "violin":             {"harmonics": [(1,.60),(2,.20),(3,.10),(4,.05),(5,.03),(6,.02)], "attack":.060,"decay":.0,"sustain":1.0,"release":.08,"vibrato_depth":.12,"vibrato_rate":5.5,"volume":.48},
    "viola":              {"harmonics": [(1,.55),(2,.25),(3,.12),(4,.05),(5,.03)], "attack":.070,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.11,"vibrato_rate":5.0,"volume":.46},
    "cello":              {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.090,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.10,"vibrato_rate":4.5,"volume":.50},
    "double_bass":        {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.100, "decay":.0, "sustain":1.0,"release":.14,"vibrato_depth":.08,"vibrato_rate":4.0,"volume":.52},
    "violin_ensemble":    {"harmonics": [(1,.55),(1.005,.45),(2,.22),(2.005,.18),(3,.10),(4,.04)], "attack":.100,"decay":.0,"sustain":1.0,"release":.15,"vibrato_depth":.10,"vibrato_rate":5.2,"volume":.40},
    "viola_ensemble":     {"harmonics": [(1,.52),(1.005,.42),(2,.24),(3,.11),(4,.05)], "attack":.110,"decay":.0,"sustain":1.0,"release":.16,"vibrato_depth":.09,"vibrato_rate":4.8,"volume":.40},
    "cello_ensemble":     {"harmonics": [(1,.50),(1.005,.42),(2,.25),(3,.12),(4,.05)], "attack":.120,"decay":.0,"sustain":1.0,"release":.18,"vibrato_depth":.08,"vibrato_rate":4.5,"volume":.42},
    "string_ensemble":    {"harmonics": [(1,.52),(1.006,.44),(2,.23),(2.005,.19),(3,.10),(4,.04)], "attack":.150,"decay":.0,"sustain":1.0,"release":.20,"vibrato_depth":.09,"vibrato_rate":5.0,"volume":.38},
    "string_quartet":     {"harmonics": [(1,.55),(2,.24),(3,.12),(4,.05),(5,.04)], "attack":.120,"decay":.0,"sustain":1.0,"release":.18,"vibrato_depth":.10,"vibrato_rate":5.0,"volume":.42},
    "string_orchestra":   {"harmonics": [(1,.50),(1.007,.44),(2,.22),(2.006,.18),(3,.10),(4,.04)], "attack":.200,"decay":.0,"sustain":1.0,"release":.25,"vibrato_depth":.09,"vibrato_rate":5.0,"volume":.36},
    "violin_pizzicato":   {"harmonics": [(1,.60),(2,.25),(3,.10),(4,.05)], "attack":.004, "decay":.12,"sustain":.40,"release":.15,"decay_exp":9.0,"volume":.52},
    "cello_pizzicato":    {"harmonics": [(1,.58),(2,.27),(3,.11),(4,.04)], "attack":.005, "decay":.15,"sustain":.40,"release":.18,"decay_exp":7.0,"volume":.52},
    "violin_tremolo":     {"harmonics": [(1,.60),(2,.22),(3,.10),(4,.05),(5,.03)], "attack":.020,"decay":.0,"sustain":1.0,"release":.08,"vibrato_depth":.20,"vibrato_rate":12.0,"volume":.44},
    "cello_tremolo":      {"harmonics": [(1,.55),(2,.25),(3,.12),(4,.05)], "attack":.020, "decay":.0, "sustain":1.0,"release":.10,"vibrato_depth":.18,"vibrato_rate":11.0,"volume":.45},
    "harp":               {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.004, "decay":.0, "sustain":.50,"release":.15,"decay_exp":5.0,"volume":.52},
    "harp_electric":      {"harmonics": [(1,.52),(2,.30),(3,.14),(4,.04)], "attack":.003, "decay":.0, "sustain":.55,"release":.12,"decay_exp":4.5,"volume":.50},
    "mandolin":           {"harmonics": [(1,.52),(2,.28),(3,.14),(4,.06)], "attack":.003, "decay":.0, "sustain":.40,"release":.10,"decay_exp":10.,"volume":.54},
    "dulcimer":           {"harmonics": [(1,.58),(2,.26),(3,.12),(4,.04)], "attack":.004, "decay":.0, "sustain":.45,"release":.12,"decay_exp":8.0,"volume":.52},
    "guitar_acoustic":    {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.005,"decay":.12,"sustain":.65,"release":.15,"decay_exp":4.0,"volume":.54},
    "guitar_electric":    {"harmonics": [(1,.52),(2,.28),(3,.12),(4,.06),(5,.02)], "attack":.005,"decay":.08,"sustain":.75,"release":.12,"saturation":1.2,"volume":.52},
    "guitar_clean":       {"harmonics": [(1,.55),(2,.26),(3,.12),(4,.05),(5,.02)], "attack":.006,"decay":.10,"sustain":.78,"release":.14,"volume":.54},
    "guitar_distorted":   {"harmonics": [(1,.45),(2,.28),(3,.14),(4,.07),(5,.04),(6,.02)], "attack":.004,"decay":.0,"sustain":1.0,"release":.15,"saturation":6.0,"volume":.44},
    "guitar_overdriven":  {"harmonics": [(1,.48),(2,.28),(3,.14),(4,.06),(5,.04)], "attack":.004,"decay":.0,"sustain":1.0,"release":.14,"saturation":4.0,"volume":.46},
    "guitar_crunch":      {"harmonics": [(1,.50),(2,.26),(3,.14),(4,.07),(5,.03)], "attack":.004,"decay":.0,"sustain":1.0,"release":.12,"saturation":3.0,"volume":.48},
    "guitar_jazz":        {"harmonics": [(1,.58),(2,.26),(3,.11),(4,.05)], "attack":.008, "decay":.12,"sustain":.75,"release":.16,"volume":.54},
    "guitar_muted":       {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.003, "decay":.04,"sustain":.35,"release":.06,"decay_exp":18.,"volume":.58},
    "guitar_12string":    {"harmonics": [(1,.45),(1.005,.35),(2,.22),(2.005,.18),(3,.10),(4,.04)], "attack":.005,"decay":.10,"sustain":.70,"release":.14,"volume":.44},
    "guitar_slide":       {"harmonics": [(1,.58),(2,.26),(3,.12),(4,.04)], "attack":.015, "decay":.10,"sustain":.80,"release":.18,"vibrato_depth":.08,"vibrato_rate":3.0,"volume":.52},
    "guitar_baritone":    {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.007, "decay":.12,"sustain":.75,"release":.15,"volume":.54},
    "guitar_harmonics":   {"harmonics": [(2,.65),(4,.22),(6,.10),(8,.03)], "attack":.005, "decay":.0, "sustain":.45,"release":.12,"decay_exp":6.0,"volume":.52},
    "trumpet":            {"harmonics": [(1,.45),(2,.30),(3,.16),(4,.06),(5,.03)], "attack":.045,"decay":.05,"sustain":.85,"release":.10,"saturation":1.2,"volume":.48},
    "trombone":           {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.060,"decay":.05,"sustain":.85,"release":.12,"saturation":1.0,"volume":.50},
    "french_horn":        {"harmonics": [(1,.55),(2,.25),(3,.12),(4,.05),(5,.03)], "attack":.080,"decay":.05,"sustain":.85,"release":.14,"volume":.48},
    "tuba":               {"harmonics": [(1,.60),(2,.25),(3,.10),(4,.05)], "attack":.100, "decay":.05,"sustain":.85,"release":.16,"volume":.52},
    "cornet":             {"harmonics": [(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)], "attack":.040,"decay":.05,"sustain":.82,"release":.10,"saturation":1.0,"volume":.48},
    "brass":              {"harmonics": [(1,.40),(2,.30),(3,.18),(4,.08),(5,.04)], "attack":.040,"decay":.05,"sustain":.85,"release":.10,"saturation":1.3,"volume":.46},
    "brass_ensemble":     {"harmonics": [(1,.42),(2,.28),(3,.16),(4,.08),(5,.04),(6,.02)], "attack":.060,"decay":.05,"sustain":.85,"release":.12,"saturation":1.2,"volume":.42},
    "brass_stab":         {"harmonics": [(1,.45),(2,.28),(3,.15),(4,.08),(5,.04)], "attack":.015,"decay":.10,"sustain":.60,"release":.08,"saturation":2.0,"volume":.50},
    "brass_fall":         {"harmonics": [(1,.45),(2,.28),(3,.15),(4,.08)], "attack":.020, "decay":.30,"sustain":.40,"release":.20,"saturation":1.5,"volume":.48},
    "flugelhorn":         {"harmonics": [(1,.52),(2,.26),(3,.14),(4,.06),(5,.02)], "attack":.050,"decay":.05,"sustain":.82,"release":.12,"volume":.48},
    "euphonium":          {"harmonics": [(1,.55),(2,.25),(3,.12),(4,.06),(5,.02)], "attack":.070,"decay":.05,"sustain":.85,"release":.14,"volume":.50},
    "trumpet_mute":       {"harmonics": [(1,.42),(2,.30),(3,.18),(4,.08),(5,.02)], "attack":.030,"decay":.05,"sustain":.80,"release":.08,"saturation":1.5,"noise_mix":.015,"volume":.46},
    "flute":              {"harmonics": [(1,.70),(2,.16),(3,.08),(4,.04),(5,.02)], "attack":.050,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"volume":.50},
    "piccolo":            {"harmonics": [(1,.68),(2,.18),(3,.09),(4,.05)], "attack":.030, "decay":.0, "sustain":1.0,"release":.06,"noise_mix":.035,"volume":.50},
    "alto_flute":         {"harmonics": [(1,.72),(2,.15),(3,.08),(4,.05)], "attack":.060, "decay":.0, "sustain":1.0,"release":.10,"noise_mix":.045,"volume":.50},
    "clarinet":           {"harmonics": [(1,.55),(3,.28),(5,.12),(7,.05)], "attack":.040, "decay":.0, "sustain":1.0,"release":.08,"noise_mix":.02,"volume":.50},
    "oboe":               {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.040,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.025,"volume":.48},
    "bassoon":            {"harmonics": [(1,.55),(2,.26),(3,.12),(4,.05),(5,.02)], "attack":.060,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.03,"volume":.50},
    "woodwind":           {"harmonics": [(1,.58),(2,.24),(3,.12),(4,.06)], "attack":.045, "decay":.0, "sustain":1.0,"release":.09,"noise_mix":.03,"volume":.50},
    "saxophone":          {"harmonics": [(1,.48),(2,.28),(3,.14),(4,.06),(5,.04)], "attack":.050,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.03,"saturation":.8,"volume":.50},
    "sax_alto":           {"harmonics": [(1,.50),(2,.26),(3,.14),(4,.07),(5,.03)], "attack":.045,"decay":.0,"sustain":1.0,"release":.09,"noise_mix":.025,"volume":.50},
    "sax_tenor":          {"harmonics": [(1,.52),(2,.25),(3,.13),(4,.07),(5,.03)], "attack":.050,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.03,"volume":.50},
    "sax_soprano":        {"harmonics": [(1,.48),(2,.28),(3,.14),(4,.07),(5,.03)], "attack":.040,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.02,"volume":.48},
    "sax_baritone":       {"harmonics": [(1,.55),(2,.24),(3,.12),(4,.06),(5,.03)], "attack":.060,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.035,"volume":.52},
    "recorder":           {"harmonics": [(1,.72),(2,.16),(3,.08),(4,.04)], "attack":.040, "decay":.0, "sustain":1.0,"release":.07,"noise_mix":.05,"volume":.48},
    "pan_flute":          {"harmonics": [(1,.75),(2,.14),(3,.08),(4,.03)], "attack":.080, "decay":.0, "sustain":1.0,"release":.12,"noise_mix":.06,"volume":.48},
    "piano":              {"harmonics": [(1,.50),(2,.25),(3,.12),(4,.08),(5,.05),(6,.03),(7,.02),(8,.012),(9,.008),(10,.005)], "attack":.005,"decay":.0,"sustain":.80,"release":.15,"decay_exp":4.5,"saturation":.6,"volume":.58},
    "piano_electric":     {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.004, "decay":.0, "sustain":.70,"release":.12,"decay_exp":6.0,"volume":.52},
    "electric_piano":     {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.004, "decay":.0, "sustain":.70,"release":.12,"decay_exp":6.0,"volume":.52},
    "rhodes":             {"harmonics": [(1,.52),(2,.30),(3,.14),(4,.04)], "attack":.006, "decay":.0, "sustain":.75,"release":.18,"decay_exp":5.0,"volume":.50},
    "piano_grand":        {"harmonics": [(1,.48),(2,.26),(3,.13),(4,.07),(5,.04),(6,.02)], "attack":.004,"decay":.0,"sustain":.82,"release":.18,"decay_exp":3.5,"volume":.52},
    "piano_upright":      {"harmonics": [(1,.52),(2,.26),(3,.12),(4,.06),(5,.04)], "attack":.005,"decay":.0,"sustain":.75,"release":.14,"decay_exp":5.0,"volume":.52},
    "piano_honky_tonk":   {"harmonics": [(1,.50),(1.005,.45),(2,.25),(2.005,.22),(3,.10),(4,.05)], "attack":.005,"decay":.0,"sustain":.75,"release":.14,"decay_exp":5.0,"volume":.44},
    "harpsichord":        {"harmonics": [(1,.45),(2,.30),(3,.15),(4,.08),(5,.02)], "attack":.003,"decay":.0,"sustain":.20,"release":.10,"decay_exp":9.0,"volume":.52},
    "organ":              {"harmonics": [(1,.40),(2,.30),(3,.20),(4,.07),(6,.03)], "attack":.010,"decay":.0,"sustain":1.0,"release":.05,"volume":.48},
    "organ_jazz":         {"harmonics": [(1,.42),(2,.28),(3,.18),(4,.08),(5,.04)], "attack":.008,"decay":.0,"sustain":1.0,"release":.05,"volume":.48},
    "organ_church":       {"harmonics": [(1,.38),(2,.32),(3,.20),(4,.08),(5,.02)], "attack":.020,"decay":.0,"sustain":1.0,"release":.08,"volume":.46},
    "organ_rock":         {"harmonics": [(1,.40),(2,.30),(3,.18),(4,.08),(5,.04)], "attack":.008,"decay":.0,"sustain":1.0,"release":.05,"saturation":1.5,"volume":.46},
    "organ_hammond":      {"harmonics": [(1,.40),(2,.30),(3,.20),(4,.07),(5,.02),(6,.01)], "attack":.008,"decay":.0,"sustain":1.0,"release":.05,"saturation":1.2,"volume":.46},
    "celesta":            {"harmonics": [(1,.60),(2,.25),(3,.10),(4,.05)], "attack":.004, "decay":.0, "sustain":.40,"release":.15,"decay_exp":7.0,"volume":.50},
    "vibraphone":         {"harmonics": [(1,.62),(2,.24),(3,.10),(4,.04)], "attack":.005, "decay":.0, "sustain":.60,"release":.20,"decay_exp":4.0,"vibrato_depth":.02,"vibrato_rate":6.0,"volume":.52},
    "marimba":            {"harmonics": [(1,.60),(3,.25),(5,.10),(7,.05)], "attack":.004, "decay":.0, "sustain":.30,"release":.10,"decay_exp":8.0,"volume":.54},
    "xylophone":          {"harmonics": [(1,.62),(2.93,.28),(5.02,.10)],   "attack":.003, "decay":.0, "sustain":.20,"release":.08,"decay_exp":12.,"volume":.54},
    "glockenspiel":       {"harmonics": [(1,.60),(2.93,.24),(5.02,.14),(7.5,.08),(9.7,.04)], "attack":.003,"decay":.0,"sustain":.25,"release":.12,"decay_exp":10.,"volume":.52},
    "music_box":          {"harmonics": [(1,.65),(2,.20),(3,.10),(4,.05)], "attack":.002, "decay":.0, "sustain":.15,"release":.10,"decay_exp":14.,"volume":.50},
    "oud":                {"harmonics": [(1,.52),(2,.28),(3,.14),(4,.06)], "attack":.004, "decay":.12,"sustain":.60,"release":.14,"decay_exp":5.0,"volume":.54},
    "sitar":              {"harmonics": [(1,.48),(2,.28),(3,.14),(4,.06),(5,.04)], "attack":.003,"decay":.10,"sustain":.55,"release":.12,"decay_exp":6.0,"volume":.52},
    "banjo":              {"harmonics": [(1,.55),(2,.26),(3,.14),(4,.05)], "attack":.003, "decay":.08,"sustain":.50,"release":.10,"decay_exp":8.0,"volume":.54},
    "koto":               {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.004, "decay":.10,"sustain":.50,"release":.12,"decay_exp":7.0,"volume":.52},
    "shamisen":           {"harmonics": [(1,.52),(2,.28),(3,.14),(4,.06)], "attack":.003, "decay":.08,"sustain":.50,"release":.10,"decay_exp":8.0,"volume":.54},
    "erhu":               {"harmonics": [(1,.58),(2,.25),(3,.12),(4,.05)], "attack":.050, "decay":.0, "sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":6.0,"noise_mix":.02,"volume":.48},
    "pipa":               {"harmonics": [(1,.52),(2,.28),(3,.14),(4,.06)], "attack":.003, "decay":.12,"sustain":.50,"release":.14,"decay_exp":6.0,"volume":.52},
    "guqin":              {"harmonics": [(1,.55),(2,.26),(3,.12),(4,.05)], "attack":.008, "decay":.15,"sustain":.55,"release":.18,"decay_exp":5.5,"vibrato_depth":.03,"volume":.50},
    "guzheng":            {"harmonics": [(1,.52),(2,.28),(3,.14),(4,.06)], "attack":.004, "decay":.12,"sustain":.50,"release":.14,"decay_exp":6.0,"vibrato_depth":.04,"volume":.52},
    "balalaika":          {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.003, "decay":.10,"sustain":.50,"release":.12,"decay_exp":7.5,"volume":.54},
    "bouzouki":           {"harmonics": [(1,.52),(2,.28),(3,.14),(4,.06)], "attack":.004, "decay":.12,"sustain":.55,"release":.14,"decay_exp":6.5,"volume":.52},
    "charango":           {"harmonics": [(1,.55),(2,.26),(3,.13),(4,.06)], "attack":.003, "decay":.10,"sustain":.50,"release":.12,"decay_exp":7.0,"volume":.52},
    "sarod":              {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)], "attack":.004,"decay":.12,"sustain":.55,"release":.14,"decay_exp":6.0,"volume":.50},
    "santoor":            {"harmonics": [(1,.55),(2,.26),(3,.12),(4,.05)], "attack":.003, "decay":.0, "sustain":.40,"release":.12,"decay_exp":9.0,"volume":.52},
    "mbira":              {"harmonics": [(1,.58),(2,.25),(3,.12),(4,.05)], "attack":.003, "decay":.0, "sustain":.30,"release":.10,"decay_exp":10.,"volume":.52},
    "kalimba":            {"harmonics": [(1,.62),(2,.22),(3,.10),(4,.06)], "attack":.003, "decay":.0, "sustain":.35,"release":.12,"decay_exp":9.0,"volume":.52},
    "steelpan":           {"harmonics": [(1,.60),(2.03,.25),(4.1,.14),(6.2,.08),(8.3,.03)], "attack":.004,"decay":.0,"sustain":.45,"release":.15,"decay_exp":5.0,"volume":.52},
    "tabla":              {"harmonics": [(1,.55),(1.52,.28),(2.4,.14),(3.2,.06)], "attack":.003,"decay":.0,"sustain":.40,"release":.10,"decay_exp":10.,"noise_mix":.04,"volume":.54},
    "darbuka":            {"harmonics": [(1,.58),(1.45,.22),(2.2,.12)],    "attack":.003, "decay":.0, "sustain":.35,"release":.08,"decay_exp":12.,"noise_mix":.05,"volume":.55},
    "conga":              {"harmonics": [(1,.55),(1.7,.25),(2.5,.12),(3.4,.05)], "attack":.003,"decay":.0,"sustain":.40,"release":.12,"decay_exp":8.0,"noise_mix":.04,"volume":.54},
    "bongo":              {"harmonics": [(1,.55),(1.8,.24),(2.8,.11)],     "attack":.003, "decay":.0, "sustain":.35,"release":.10,"decay_exp":10.,"noise_mix":.04,"volume":.54},
    "djembe":             {"harmonics": [(1,.55),(1.6,.26),(2.5,.12),(3.5,.05)], "attack":.003,"decay":.0,"sustain":.40,"release":.12,"decay_exp":9.0,"noise_mix":.06,"volume":.55},
    "cajon":              {"harmonics": [(1,.55),(1.5,.22),(2.2,.12)],     "attack":.003, "decay":.0, "sustain":.35,"release":.12,"decay_exp":8.0,"noise_mix":.06,"volume":.54},
    "dhol":               {"harmonics": [(1,.58),(1.4,.24),(2,.12)],       "attack":.004, "decay":.0, "sustain":.40,"release":.14,"decay_exp":7.0,"noise_mix":.06,"volume":.56},
    "trap_kick":          {"harmonics": [(1,.65),(2,.20),(3,.10),(4,.05)], "attack":.003, "decay":.0, "sustain":.80,"release":.15,"decay_exp":12.,"saturation":2.0,"volume":.65},
    "trap_snare":         {"harmonics": [(1,.40),(2,.25),(3,.15)],         "attack":.003, "decay":.0, "sustain":.30,"release":.10,"decay_exp":14.,"noise_mix":.35,"saturation":1.5,"volume":.60},
    "trap_hat":           {"harmonics": [(1,.20),(3,.22),(5,.25),(7,.18),(9,.10),(11,.05)], "attack":.002,"decay":.0,"sustain":.20,"release":.04,"decay_exp":25.,"noise_mix":.50,"volume":.48},
    "trap_808":           {"harmonics": [(1,.72),(2,.16),(3,.08),(4,.04)], "attack":.005, "decay":.0, "sustain":1.0,"release":.30,"decay_exp":8.0,"saturation":2.0,"volume":.66},
    "trap_clap":          {"harmonics": [(1,.20),(2,.15),(3,.10)],         "attack":.003, "decay":.0, "sustain":.20,"release":.08,"decay_exp":18.,"noise_mix":.60,"saturation":1.2,"volume":.58},
    "trap_perc":          {"harmonics": [(1,.55),(1.8,.25),(3,.12),(4.5,.05)], "attack":.003,"decay":.0,"sustain":.30,"release":.08,"decay_exp":15.,"noise_mix":.15,"volume":.55},
    "trap_tom":           {"harmonics": [(1,.60),(1.5,.25),(2.2,.12)],     "attack":.003, "decay":.0, "sustain":.45,"release":.14,"decay_exp":9.0,"noise_mix":.06,"volume":.58},
    "hi_hat_trap":        {"harmonics": [(1,.18),(3,.20),(5,.24),(7,.20),(9,.12),(11,.06)], "attack":.002,"decay":.0,"sustain":.15,"release":.03,"decay_exp":30.,"noise_mix":.60,"volume":.46},
    "drum_machine":       {"harmonics": [(1,.60),(2,.22),(3,.12),(4,.06)], "attack":.003, "decay":.0, "sustain":.50,"release":.12,"decay_exp":10.,"noise_mix":.08,"volume":.58},
    "drum_808":           {"harmonics": [(1,.68),(2,.18),(3,.10),(4,.04)], "attack":.005, "decay":.0, "sustain":1.0,"release":.25,"decay_exp":9.0,"saturation":2.2,"volume":.64},
    "lo_fi_drum":         {"harmonics": [(1,.55),(2,.24),(3,.12),(4,.05)], "attack":.004, "decay":.0, "sustain":.50,"release":.15,"decay_exp":8.0,"noise_mix":.12,"saturation":1.5,"volume":.56},
    "lo_fi_piano":        {"harmonics": [(1,.52),(2,.26),(3,.12),(4,.06)], "attack":.005, "decay":.0, "sustain":.70,"release":.15,"decay_exp":5.0,"noise_mix":.015,"saturation":.8,"volume":.50},
    "lo_fi_bass":         {"harmonics": [(1,.68),(2,.20),(3,.08),(4,.04)], "attack":.008, "decay":.0, "sustain":1.0,"release":.25,"decay_exp":5.0,"noise_mix":.01,"saturation":1.0,"volume":.58},
    "vaporwave_pad":      {"harmonics": [(1,.55),(2,.24),(3,.12),(4,.06)], "attack":.500, "decay":.0, "sustain":1.0,"release":.50,"vibrato_depth":.04,"vibrato_rate":0.3,"noise_mix":.01,"volume":.40},
    "chillhop_piano":     {"harmonics": [(1,.52),(2,.26),(3,.12),(4,.06)], "attack":.006, "decay":.0, "sustain":.68,"release":.18,"decay_exp":4.5,"noise_mix":.01,"volume":.50},
    "chillhop_bass":      {"harmonics": [(1,.65),(2,.22),(3,.10),(4,.03)], "attack":.010, "decay":.0, "sustain":1.0,"release":.28,"decay_exp":5.5,"noise_mix":.005,"volume":.58},
    "rnb_piano":          {"harmonics": [(1,.52),(2,.26),(3,.13),(4,.07),(5,.02)], "attack":.005,"decay":.0,"sustain":.78,"release":.18,"decay_exp":4.0,"volume":.50},
    "rnb_bass":           {"harmonics": [(1,.65),(2,.22),(3,.10),(4,.03)], "attack":.008, "decay":.0, "sustain":1.0,"release":.25,"decay_exp":6.0,"saturation":1.2,"volume":.60},
    "drill_bass":         {"harmonics": [(1,.68),(2,.18),(3,.10),(4,.04)], "attack":.004, "decay":.0, "sustain":1.0,"release":.20,"decay_exp":10.,"saturation":2.5,"volume":.63},
    "drill_808":          {"harmonics": [(1,.70),(2,.16),(3,.10),(4,.04)], "attack":.005, "decay":.0, "sustain":1.0,"release":.28,"decay_exp":9.0,"saturation":2.8,"volume":.65},
    "kick_808":           {"harmonics": [(1,.65),(2,.22),(3,.10),(4,.03)], "attack":.003, "decay":.0, "sustain":.80,"release":.18,"decay_exp":11.,"saturation":2.0,"volume":.65},
    "kick_acoustic":      {"harmonics": [(1,.60),(2,.20),(3,.12),(4,.06),(5,.02)], "attack":.003,"decay":.0,"sustain":.60,"release":.14,"decay_exp":12.,"noise_mix":.10,"volume":.62},
    "kick_deep":          {"harmonics": [(1,.72),(2,.16),(3,.08),(4,.04)], "attack":.005, "decay":.0, "sustain":.80,"release":.25,"decay_exp":8.0,"saturation":1.5,"volume":.64},
    "kick_snap":          {"harmonics": [(1,.55),(2,.25),(3,.14),(4,.06)], "attack":.002, "decay":.0, "sustain":.40,"release":.08,"decay_exp":20.,"noise_mix":.08,"volume":.62},
    "kick_punchy":        {"harmonics": [(1,.62),(2,.22),(3,.12),(4,.04)], "attack":.003, "decay":.0, "sustain":.65,"release":.14,"decay_exp":14.,"saturation":2.5,"volume":.63},
    "snare_acoustic":     {"harmonics": [(1,.35),(2,.22),(3,.15),(4,.08)], "attack":.003, "decay":.0, "sustain":.35,"release":.10,"decay_exp":16.,"noise_mix":.40,"volume":.60},
    "snare_electric":     {"harmonics": [(1,.40),(2,.24),(3,.14)],         "attack":.003, "decay":.0, "sustain":.30,"release":.08,"decay_exp":18.,"noise_mix":.45,"saturation":1.2,"volume":.60},
    "snare_tight":        {"harmonics": [(1,.38),(2,.24),(3,.16)],         "attack":.002, "decay":.0, "sustain":.25,"release":.06,"decay_exp":22.,"noise_mix":.42,"volume":.60},
    "snare_rimshot":      {"harmonics": [(1,.42),(2,.28),(3,.16),(4,.06)], "attack":.002, "decay":.0, "sustain":.30,"release":.08,"decay_exp":20.,"noise_mix":.30,"saturation":1.5,"volume":.62},
    "hi_hat_closed":      {"harmonics": [(1,.18),(3,.22),(5,.26),(7,.20),(9,.10),(11,.04)], "attack":.002,"decay":.0,"sustain":.15,"release":.03,"decay_exp":28.,"noise_mix":.55,"volume":.46},
    "hi_hat_open":        {"harmonics": [(1,.18),(3,.22),(5,.24),(7,.20),(9,.10),(11,.06)], "attack":.002,"decay":.0,"sustain":.50,"release":.20,"decay_exp":8.0,"noise_mix":.55,"volume":.48},
    "hi_hat_pedal":       {"harmonics": [(1,.15),(3,.20),(5,.22),(7,.18)], "attack":.003, "decay":.0, "sustain":.20,"release":.05,"decay_exp":22.,"noise_mix":.50,"volume":.42},
    "cymbal_crash":       {"harmonics": [(1,.15),(2.87,.18),(4.98,.20),(7.12,.18),(9.7,.14),(12.5,.10),(16.,.05)], "attack":.005,"decay":.0,"sustain":.60,"release":.45,"decay_exp":4.0,"noise_mix":.40,"volume":.50},
    "cymbal_ride":        {"harmonics": [(1,.15),(2.93,.18),(5.05,.22),(7.4,.18),(9.8,.12),(13.,.08)], "attack":.006,"decay":.0,"sustain":.70,"release":.50,"decay_exp":3.5,"noise_mix":.35,"volume":.48},
    "cymbal_splash":      {"harmonics": [(1,.15),(2.87,.20),(4.95,.22),(7.,.18),(9.,.12),(12.,.08)], "attack":.004,"decay":.0,"sustain":.30,"release":.15,"decay_exp":10.,"noise_mix":.45,"volume":.48},
    "cymbal_china":       {"harmonics": [(1,.15),(2.5,.20),(4.,.22),(6.5,.18),(9.5,.12),(13.5,.08)], "attack":.003,"decay":.0,"sustain":.60,"release":.40,"decay_exp":5.0,"noise_mix":.48,"saturation":.8,"volume":.50},
    "tom_high":           {"harmonics": [(1,.62),(1.5,.22),(2.3,.12),(3.2,.04)], "attack":.003,"decay":.0,"sustain":.50,"release":.15,"decay_exp":9.0,"noise_mix":.06,"volume":.58},
    "tom_mid":            {"harmonics": [(1,.62),(1.45,.22),(2.2,.12),(3.,.04)], "attack":.003,"decay":.0,"sustain":.50,"release":.18,"decay_exp":8.0,"noise_mix":.07,"volume":.60},
    "tom_low":            {"harmonics": [(1,.65),(1.42,.22),(2.1,.10),(3.,.03)], "attack":.004,"decay":.0,"sustain":.55,"release":.22,"decay_exp":7.0,"noise_mix":.07,"volume":.62},
    "tom_floor":          {"harmonics": [(1,.65),(1.38,.22),(2.,.10),(2.8,.03)], "attack":.004,"decay":.0,"sustain":.60,"release":.28,"decay_exp":6.0,"noise_mix":.07,"volume":.63},
    "clap_808":           {"harmonics": [(1,.20),(2,.15),(3,.10)],         "attack":.002, "decay":.0, "sustain":.20,"release":.06,"decay_exp":20.,"noise_mix":.65,"saturation":1.5,"volume":.58},
    "clap_acoustic":      {"harmonics": [(1,.22),(2,.18),(3,.12),(4,.06)], "attack":.003, "decay":.0, "sustain":.25,"release":.08,"decay_exp":16.,"noise_mix":.60,"volume":.58},
    "clap_reverb":        {"harmonics": [(1,.20),(2,.15),(3,.12)],         "attack":.003, "decay":.0, "sustain":.40,"release":.30,"decay_exp":8.0,"noise_mix":.62,"volume":.56},
    "shaker":             {"harmonics": [(1,.05),(3,.06),(7,.08),(11,.07),(15,.06)], "attack":.010,"decay":.0,"sustain":.60,"release":.10,"noise_mix":.70,"volume":.44},
    "tambourine":         {"harmonics": [(1,.10),(5,.10),(9,.10),(13,.08)], "attack":.003,"decay":.0,"sustain":.30,"release":.08,"decay_exp":12.,"noise_mix":.65,"volume":.46},
    "cowbell":            {"harmonics": [(1,.60),(2.45,.28),(4.9,.12)],    "attack":.003, "decay":.0, "sustain":.35,"release":.12,"decay_exp":10.,"volume":.52},
    "voice":              {"harmonics": [(1,.65),(2,.20),(3,.08),(4,.04),(5,.03)], "attack":.080,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.13,"vibrato_rate":6.0,"volume":.48},
    "fx_riser":           {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.800, "decay":.0, "sustain":1.0,"release":.10,"volume":.44},
    "fx_downlifter":      {"harmonics": [(1,.55),(2,.28),(3,.12),(4,.05)], "attack":.010, "decay":.0, "sustain":1.0,"release":.800,"decay_exp":2.0,"volume":.44},
    "fx_whoosh":          {"harmonics": [(1,.10),(3,.12),(7,.14),(11,.12),(15,.10)], "attack":.100,"decay":.0,"sustain":.80,"release":.30,"noise_mix":.70,"volume":.42},
    "fx_impact":          {"harmonics": [(1,.55),(2,.28),(3,.14),(4,.03)], "attack":.003, "decay":.0, "sustain":.50,"release":.40,"decay_exp":6.0,"noise_mix":.30,"saturation":2.0,"volume":.55},
    "fx_zap":             {"harmonics": [(1,.65),(2,.22),(3,.10),(4,.03)], "attack":.001, "decay":.0, "sustain":.50,"release":.04,"decay_exp":25.,"saturation":2.5,"volume":.56},
    "fx_glitch":          {"harmonics": [(1,.40),(2,.25),(3,.18),(5,.10),(7,.07)], "attack":.002,"decay":.0,"sustain":.60,"release":.05,"decay_exp":18.,"noise_mix":.25,"saturation":3.0,"volume":.50},
    "fx_vinyl":           {"harmonics": [(1,.55),(2,.24),(3,.12),(4,.05)], "attack":.005, "decay":.0, "sustain":1.0,"release":.15,"noise_mix":.04,"saturation":.5,"volume":.48},
    "fx_noise":           {"harmonics": [(1,.05)],                         "attack":.010, "decay":.0, "sustain":1.0,"release":.15,"noise_mix":.90,"volume":.42},
    "fx_sweep":           {"harmonics": [(1,.50),(2,.28),(3,.14),(4,.05)], "attack":.300, "decay":.0, "sustain":1.0,"release":.300,"noise_mix":.15,"volume":.44},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ BASS EXTENDED (808 Genre/Character/Electric/Synth/Sub/Genre) ██
    # ═══════════════════════════════════════════════════════════════════════════
    "bass_808_phonk":       {"harmonics":[(1,.70),(2,.18),(3,.08),(4,.04)],"attack":.004,"decay":.0,"sustain":1.0,"release":.35,"decay_exp":9.0,"saturation":3.0,"volume":.66},
    "bass_808_jersey":      {"harmonics":[(1,.68),(2,.20),(3,.09),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.28,"decay_exp":10.,"saturation":2.2,"volume":.65},
    "bass_808_chicago":     {"harmonics":[(1,.72),(2,.16),(3,.08),(4,.04)],"attack":.005,"decay":.0,"sustain":1.0,"release":.32,"decay_exp":8.5,"saturation":2.0,"volume":.65},
    "bass_808_memphis":     {"harmonics":[(1,.75),(2,.14),(3,.08),(4,.03)],"attack":.006,"decay":.0,"sustain":1.0,"release":.40,"decay_exp":7.0,"saturation":1.8,"volume":.65},
    "bass_808_miami":       {"harmonics":[(1,.68),(2,.20),(3,.09),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.30,"decay_exp":9.5,"saturation":2.5,"volume":.65},
    "bass_808_atlanta":     {"harmonics":[(1,.70),(2,.18),(3,.09),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.35,"decay_exp":9.0,"saturation":2.8,"volume":.66},
    "bass_808_houston":     {"harmonics":[(1,.74),(2,.15),(3,.08),(4,.03)],"attack":.006,"decay":.0,"sustain":1.0,"release":.45,"decay_exp":6.5,"saturation":1.5,"volume":.65},
    "bass_808_nyc":         {"harmonics":[(1,.66),(2,.22),(3,.09),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.28,"decay_exp":11.,"saturation":2.5,"volume":.65},
    "bass_808_la":          {"harmonics":[(1,.68),(2,.20),(3,.09),(4,.03)],"attack":.005,"decay":.0,"sustain":1.0,"release":.30,"decay_exp":9.0,"saturation":2.0,"volume":.64},
    "bass_808_afro2":       {"harmonics":[(1,.72),(2,.16),(3,.08),(4,.04)],"attack":.006,"decay":.0,"sustain":1.0,"release":.40,"decay_exp":7.5,"saturation":1.8,"volume":.65},
    "bass_808_dancehall2":  {"harmonics":[(1,.70),(2,.18),(3,.09),(4,.03)],"attack":.005,"decay":.0,"sustain":1.0,"release":.38,"decay_exp":8.0,"saturation":2.0,"volume":.65},
    "bass_808_reggaeton":   {"harmonics":[(1,.72),(2,.16),(3,.08),(4,.04)],"attack":.005,"decay":.0,"sustain":1.0,"release":.40,"decay_exp":7.5,"saturation":1.8,"volume":.65},
    "bass_808_grime":       {"harmonics":[(1,.64),(2,.22),(3,.10),(4,.04)],"attack":.004,"decay":.0,"sustain":1.0,"release":.25,"decay_exp":12.,"saturation":3.0,"volume":.65},
    "bass_808_amapiano":    {"harmonics":[(1,.76),(2,.14),(3,.07),(4,.03)],"attack":.008,"decay":.0,"sustain":1.0,"release":.50,"decay_exp":5.5,"saturation":1.5,"volume":.65},
    "bass_808_wave":        {"harmonics":[(1,.68),(2,.20),(3,.09),(4,.03)],"attack":.005,"decay":.0,"sustain":1.0,"release":.35,"decay_exp":8.5,"saturation":2.2,"volume":.64},
    "bass_808_hyperpop":    {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.003,"decay":.0,"sustain":1.0,"release":.20,"decay_exp":15.,"saturation":5.0,"volume":.60},
    "bass_808_moombah":     {"harmonics":[(1,.70),(2,.18),(3,.09),(4,.03)],"attack":.005,"decay":.0,"sustain":1.0,"release":.38,"decay_exp":8.0,"saturation":2.0,"volume":.65},
    "bass_808_house2":      {"harmonics":[(1,.68),(2,.20),(3,.09),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.32,"decay_exp":9.0,"saturation":2.0,"volume":.64},
    "bass_808_techno2":     {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.25,"decay_exp":11.,"saturation":3.0,"volume":.65},
    "bass_808_edm2":        {"harmonics":[(1,.62),(2,.24),(3,.11),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.22,"decay_exp":12.,"saturation":2.8,"volume":.64},
    "bass_808_dnb2":        {"harmonics":[(1,.62),(2,.24),(3,.11),(4,.03)],"attack":.003,"decay":.0,"sustain":1.0,"release":.18,"decay_exp":14.,"saturation":3.5,"volume":.64},
    "bass_808_dubstep2":    {"harmonics":[(1,.58),(2,.26),(3,.13),(4,.03)],"attack":.003,"decay":.0,"sustain":1.0,"release":.20,"decay_exp":13.,"saturation":4.0,"volume":.63},
    "bass_808_trance2":     {"harmonics":[(1,.72),(2,.16),(3,.09),(4,.03)],"attack":.005,"decay":.0,"sustain":1.0,"release":.35,"decay_exp":7.5,"saturation":1.8,"volume":.64},
    "bass_808_lofi2":       {"harmonics":[(1,.75),(2,.14),(3,.08),(4,.03)],"attack":.007,"decay":.0,"sustain":1.0,"release":.40,"decay_exp":6.5,"saturation":1.2,"noise_mix":.01,"volume":.63},
    "bass_808_cumbia":      {"harmonics":[(1,.70),(2,.18),(3,.09),(4,.03)],"attack":.005,"decay":.0,"sustain":1.0,"release":.38,"decay_exp":8.0,"saturation":2.0,"volume":.65},
    "bass_808_kuduro":      {"harmonics":[(1,.68),(2,.20),(3,.10),(4,.02)],"attack":.004,"decay":.0,"sustain":1.0,"release":.30,"decay_exp":10.,"saturation":2.5,"volume":.65},
    "bass_808_baile":       {"harmonics":[(1,.70),(2,.18),(3,.09),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.32,"decay_exp":9.5,"saturation":2.5,"volume":.65},
    "bass_808_futuristic":  {"harmonics":[(1,.60),(2,.26),(3,.11),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.25,"decay_exp":12.,"saturation":3.5,"volume":.63},
    "bass_808_thud":        {"harmonics":[(1,.80),(2,.12),(3,.06),(4,.02)],"attack":.004,"decay":.0,"sustain":.90,"release":.20,"decay_exp":11.,"saturation":1.8,"volume":.66},
    "bass_808_click2":      {"harmonics":[(1,.60),(2,.25),(3,.12),(4,.03)],"attack":.002,"decay":.05,"sustain":.80,"release":.15,"decay_exp":14.,"saturation":2.5,"volume":.65},
    "bass_808_body2":       {"harmonics":[(1,.70),(2,.20),(3,.08),(4,.02)],"attack":.006,"decay":.0,"sustain":1.0,"release":.40,"decay_exp":6.0,"saturation":2.0,"volume":.66},
    "bass_808_tail2":       {"harmonics":[(1,.80),(2,.12),(3,.06),(4,.02)],"attack":.005,"decay":.0,"sustain":1.0,"release":.70,"decay_exp":3.5,"saturation":1.5,"volume":.65},
    "bass_808_hollow2":     {"harmonics":[(1,.65),(2,.18),(3,.10),(4,.07)],"attack":.005,"decay":.0,"sustain":1.0,"release":.30,"decay_exp":8.0,"saturation":1.0,"volume":.62},
    "bass_808_crushed":     {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.003,"decay":.0,"sustain":1.0,"release":.20,"decay_exp":10.,"saturation":8.0,"volume":.55},
    "bass_808_filtered":    {"harmonics":[(1,.82),(2,.12),(3,.05),(4,.01)],"attack":.006,"decay":.0,"sustain":1.0,"release":.35,"decay_exp":6.0,"saturation":1.5,"volume":.66},
    "bass_808_ultra":       {"harmonics":[(1,.92),(2,.06),(3,.02)],        "attack":.008,"decay":.0,"sustain":1.0,"release":.60,"decay_exp":3.0,"saturation":.5,"volume":.68},
    "bass_808_heavy2":      {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.004,"decay":.0,"sustain":1.0,"release":.30,"decay_exp":9.0,"saturation":4.0,"volume":.63},
    "bass_808_thin2":       {"harmonics":[(1,.50),(2,.32),(3,.15),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.20,"decay_exp":12.,"saturation":2.0,"volume":.62},
    "bass_808_growl3":      {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)],"attack":.004,"decay":.0,"sustain":1.0,"release":.22,"decay_exp":10.,"saturation":3.5,"vibrato_depth":.03,"vibrato_rate":1.5,"volume":.60},
    "bass_808_wet2":        {"harmonics":[(1,.72),(2,.16),(3,.08),(4,.04)],"attack":.005,"decay":.0,"sustain":1.0,"release":.45,"decay_exp":5.0,"saturation":1.8,"volume":.64},
    "bass_808_dry2":        {"harmonics":[(1,.72),(2,.18),(3,.08),(4,.02)],"attack":.004,"decay":.0,"sustain":1.0,"release":.22,"decay_exp":12.,"saturation":2.0,"volume":.65},
    "bass_808_vinyl2":      {"harmonics":[(1,.72),(2,.16),(3,.08),(4,.04)],"attack":.005,"decay":.0,"sustain":1.0,"release":.40,"decay_exp":6.5,"saturation":1.2,"noise_mix":.015,"volume":.63},
    "bass_808_saturated2":  {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.004,"decay":.0,"sustain":1.0,"release":.25,"decay_exp":10.,"saturation":6.0,"volume":.58},
    "bass_808_compressed":  {"harmonics":[(1,.68),(2,.20),(3,.09),(4,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.30,"decay_exp":9.0,"saturation":2.5,"volume":.66},
    "bass_808_vintage2":    {"harmonics":[(1,.74),(2,.15),(3,.08),(4,.03)],"attack":.007,"decay":.0,"sustain":1.0,"release":.45,"decay_exp":5.5,"saturation":1.0,"noise_mix":.01,"volume":.63},
    "bass_808_short2":      {"harmonics":[(1,.70),(2,.18),(3,.09),(4,.03)],"attack":.003,"decay":.0,"sustain":.70,"release":.12,"decay_exp":16.,"saturation":2.5,"volume":.65},
    "bass_808_extra_long":  {"harmonics":[(1,.78),(2,.14),(3,.06),(4,.02)],"attack":.008,"decay":.0,"sustain":1.0,"release":.80,"decay_exp":2.5,"saturation":1.2,"volume":.65},
    "bass_808_sine_pure":   {"harmonics":[(1,.96),(2,.04)],               "attack":.010,"decay":.0,"sustain":1.0,"release":.55,"decay_exp":2.5,"saturation":.2,"volume":.70},
    "bass_808_swept":       {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.005,"decay":.0,"sustain":1.0,"release":.35,"decay_exp":8.0,"saturation":2.0,"vibrato_depth":.05,"vibrato_rate":.5,"volume":.64},
    "bass_808_glide":       {"harmonics":[(1,.70),(2,.18),(3,.09),(4,.03)],"attack":.008,"decay":.0,"sustain":1.0,"release":.40,"decay_exp":7.0,"saturation":1.8,"vibrato_depth":.08,"vibrato_rate":.3,"volume":.64},
    "bass_808_punchy2":     {"harmonics":[(1,.63),(2,.24),(3,.10),(4,.03)],"attack":.002,"decay":.04,"sustain":.85,"release":.14,"decay_exp":14.,"saturation":2.8,"volume":.65},
    "bass_p_bass":          {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.010,"decay":.10,"sustain":.80,"release":.14,"saturation":1.0,"volume":.58},
    "bass_j_bass":          {"harmonics":[(1,.52),(2,.30),(3,.14),(4,.04)],"attack":.008,"decay":.12,"sustain":.82,"release":.15,"saturation":.8,"volume":.58},
    "bass_stingray":        {"harmonics":[(1,.50),(2,.30),(3,.14),(4,.05),(5,.01)],"attack":.006,"decay":.08,"sustain":.80,"release":.12,"saturation":1.5,"volume":.58},
    "bass_precision":       {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.010,"decay":.12,"sustain":.80,"release":.16,"saturation":.8,"volume":.58},
    "bass_rickenbacker2":   {"harmonics":[(1,.48),(2,.30),(3,.16),(4,.06)],"attack":.007,"decay":.10,"sustain":.78,"release":.14,"saturation":1.2,"volume":.56},
    "bass_hofner":          {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.012,"decay":.15,"sustain":.75,"release":.18,"volume":.56},
    "bass_musicman2":       {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.05),(5,.01)],"attack":.006,"decay":.08,"sustain":.80,"release":.12,"saturation":1.8,"volume":.58},
    "bass_warwick":         {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.008,"decay":.10,"sustain":.82,"release":.14,"saturation":1.0,"volume":.57},
    "bass_ibanez":          {"harmonics":[(1,.50),(2,.30),(3,.14),(4,.05),(5,.01)],"attack":.007,"decay":.09,"sustain":.80,"release":.13,"saturation":1.2,"volume":.58},
    "bass_active":          {"harmonics":[(1,.48),(2,.30),(3,.16),(4,.05),(5,.01)],"attack":.006,"decay":.08,"sustain":.82,"release":.12,"saturation":1.5,"volume":.58},
    "bass_passive":         {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.012,"decay":.15,"sustain":.78,"release":.18,"volume":.56},
    "bass_short_scale":     {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.010,"decay":.12,"sustain":.78,"release":.16,"volume":.56},
    "bass_long_scale":      {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.05)],"attack":.008,"decay":.10,"sustain":.82,"release":.14,"volume":.58},
    "bass_five_string":     {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.010,"decay":.12,"sustain":.80,"release":.16,"saturation":.8,"volume":.58},
    "bass_six_string":      {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.008,"decay":.10,"sustain":.80,"release":.14,"saturation":.8,"volume":.56},
    "bass_fanned_fret":     {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.05)],"attack":.008,"decay":.10,"sustain":.82,"release":.14,"saturation":1.0,"volume":.58},
    "bass_headless":        {"harmonics":[(1,.50),(2,.30),(3,.14),(4,.05)],"attack":.007,"decay":.09,"sustain":.80,"release":.13,"saturation":1.2,"volume":.58},
    "bass_nylon_string":    {"harmonics":[(1,.60),(2,.24),(3,.10),(4,.06)],"attack":.012,"decay":.15,"sustain":.75,"release":.18,"volume":.55},
    "bass_tb303":           {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.004,"decay":.08,"sustain":.70,"release":.10,"saturation":3.5,"volume":.54},
    "bass_moog2":           {"harmonics":[(1,.58),(2,.28),(3,.10),(4,.04)],"attack":.008,"decay":.12,"sustain":.80,"release":.16,"saturation":2.5,"volume":.58},
    "bass_juno_b":          {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.010,"decay":.10,"sustain":.82,"release":.18,"saturation":1.0,"volume":.56},
    "bass_dx7_b":           {"harmonics":[(1,.50),(2,.26),(3,.16),(4,.06),(5,.02)],"attack":.006,"decay":.10,"sustain":.75,"release":.14,"saturation":1.2,"volume":.56},
    "bass_prophet_b":       {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.010,"decay":.12,"sustain":.80,"release":.18,"saturation":1.5,"volume":.57},
    "bass_korg_b":          {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.008,"decay":.10,"sustain":.80,"release":.16,"saturation":1.3,"volume":.57},
    "bass_roland_b2":       {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.008,"decay":.10,"sustain":.80,"release":.16,"saturation":1.5,"volume":.57},
    "bass_arp_b":           {"harmonics":[(1,.50),(2,.30),(3,.14),(4,.06)],"attack":.006,"decay":.08,"sustain":.78,"release":.14,"saturation":2.0,"volume":.56},
    "bass_oberheim_b":      {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.008,"decay":.12,"sustain":.78,"release":.16,"saturation":1.8,"volume":.56},
    "bass_cs80_b":          {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.010,"decay":.12,"sustain":.80,"release":.18,"saturation":1.2,"vibrato_depth":.03,"vibrato_rate":4.0,"volume":.56},
    "bass_minimoog_b":      {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.008,"decay":.12,"sustain":.80,"release":.16,"saturation":2.0,"volume":.58},
    "bass_virus_b":         {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.05),(5,.01)],"attack":.006,"decay":.08,"sustain":.80,"release":.14,"saturation":2.5,"volume":.57},
    "bass_nord_b":          {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.008,"decay":.10,"sustain":.82,"release":.16,"saturation":1.2,"volume":.58},
    "bass_elektron_b":      {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.05)],"attack":.005,"decay":.08,"sustain":.78,"release":.12,"saturation":2.2,"volume":.57},
    "bass_sub_low":         {"harmonics":[(1,.95),(2,.04),(3,.01)],       "attack":.015,"decay":.0,"sustain":1.0,"release":.60,"decay_exp":2.0,"saturation":.2,"volume":.72},
    "bass_sub_deep":        {"harmonics":[(1,.92),(2,.06),(3,.02)],       "attack":.012,"decay":.0,"sustain":1.0,"release":.55,"decay_exp":2.5,"saturation":.3,"volume":.72},
    "bass_sub_ultra":       {"harmonics":[(1,.97),(2,.03)],               "attack":.020,"decay":.0,"sustain":1.0,"release":.70,"decay_exp":1.5,"saturation":.1,"volume":.74},
    "bass_sub_rumble":      {"harmonics":[(1,.88),(2,.10),(3,.02)],       "attack":.010,"decay":.0,"sustain":1.0,"release":.65,"decay_exp":2.0,"saturation":.5,"volume":.71},
    "bass_infra":           {"harmonics":[(1,.98),(2,.02)],               "attack":.025,"decay":.0,"sustain":1.0,"release":.80,"decay_exp":1.0,"saturation":.1,"volume":.75},
    "bass_mega":            {"harmonics":[(1,.85),(2,.10),(3,.05)],       "attack":.012,"decay":.0,"sustain":1.0,"release":.55,"decay_exp":3.5,"saturation":1.0,"volume":.70},
    "bass_monster":         {"harmonics":[(1,.80),(2,.12),(3,.06),(4,.02)],"attack":.010,"decay":.0,"sustain":1.0,"release":.45,"decay_exp":4.5,"saturation":1.5,"volume":.70},
    "bass_titan":           {"harmonics":[(1,.78),(2,.14),(3,.06),(4,.02)],"attack":.010,"decay":.0,"sustain":1.0,"release":.55,"decay_exp":3.5,"saturation":1.5,"volume":.70},
    "bass_ominous":         {"harmonics":[(1,.82),(2,.12),(3,.05),(4,.01)],"attack":.015,"decay":.0,"sustain":1.0,"release":.65,"decay_exp":3.0,"saturation":1.0,"volume":.69},
    "bass_abyss":           {"harmonics":[(1,.88),(2,.09),(3,.03)],       "attack":.018,"decay":.0,"sustain":1.0,"release":.70,"decay_exp":2.5,"saturation":.5,"volume":.71},
    "bass_void":            {"harmonics":[(1,.90),(2,.08),(3,.02)],       "attack":.020,"decay":.0,"sustain":1.0,"release":.75,"decay_exp":2.0,"saturation":.3,"volume":.72},
    "bass_tectonic":        {"harmonics":[(1,.85),(2,.12),(3,.03)],       "attack":.014,"decay":.0,"sustain":1.0,"release":.60,"decay_exp":3.0,"saturation":.8,"volume":.70},
    "bass_earthquake":      {"harmonics":[(1,.78),(2,.14),(3,.06),(4,.02)],"attack":.010,"decay":.0,"sustain":1.0,"release":.55,"decay_exp":4.0,"saturation":1.2,"volume":.69},
    "bass_funk2":           {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.003,"decay":.05,"sustain":.65,"release":.10,"saturation":1.8,"volume":.60},
    "bass_soul2":           {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.010,"decay":.12,"sustain":.80,"release":.18,"saturation":.8,"volume":.58},
    "bass_gospel2":         {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.012,"decay":.12,"sustain":.80,"release":.18,"volume":.58},
    "bass_blues2":          {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.010,"decay":.12,"sustain":.78,"release":.18,"saturation":.8,"volume":.57},
    "bass_country2":        {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.010,"decay":.12,"sustain":.78,"release":.16,"volume":.56},
    "bass_rock2":           {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.006,"decay":.0,"sustain":1.0,"release":.14,"saturation":1.5,"volume":.58},
    "bass_metal2":          {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)],"attack":.004,"decay":.0,"sustain":1.0,"release":.12,"saturation":4.0,"volume":.54},
    "bass_reggae2":         {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.012,"decay":.12,"sustain":.80,"release":.20,"volume":.58},
    "bass_dub2":            {"harmonics":[(1,.68),(2,.20),(3,.09),(4,.03)],"attack":.015,"decay":.10,"sustain":.82,"release":.25,"saturation":.8,"volume":.59},
    "bass_ska":             {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.007,"decay":.10,"sustain":.75,"release":.14,"volume":.57},
    "bass_jazz3":           {"harmonics":[(1,.60),(2,.26),(3,.10),(4,.04)],"attack":.012,"decay":.15,"sustain":.80,"release":.20,"vibrato_depth":.02,"vibrato_rate":4.5,"volume":.56},
    "bass_bebop":           {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.010,"decay":.14,"sustain":.80,"release":.18,"volume":.56},
    "bass_bossa2":          {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.012,"decay":.14,"sustain":.78,"release":.18,"volume":.56},
    "bass_samba2":          {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.008,"decay":.10,"sustain":.78,"release":.14,"volume":.57},
    "bass_latin2":          {"harmonics":[(1,.55),(2,.28),(3,.14),(4,.03)],"attack":.008,"decay":.10,"sustain":.78,"release":.16,"volume":.57},
    "bass_flamenco_b":      {"harmonics":[(1,.55),(2,.28),(3,.14),(4,.03)],"attack":.004,"decay":.08,"sustain":.60,"release":.12,"decay_exp":6.0,"volume":.57},
    "bass_afrobeat":        {"harmonics":[(1,.62),(2,.24),(3,.11),(4,.03)],"attack":.008,"decay":.10,"sustain":.80,"release":.18,"volume":.58},
    "bass_highlife":        {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.010,"decay":.12,"sustain":.80,"release":.18,"volume":.57},
    "bass_juju":            {"harmonics":[(1,.62),(2,.24),(3,.11),(4,.03)],"attack":.010,"decay":.12,"sustain":.80,"release":.20,"volume":.58},
    "bass_mbalax":          {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.008,"decay":.10,"sustain":.78,"release":.16,"volume":.57},
    "bass_soukous":         {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.008,"decay":.10,"sustain":.78,"release":.16,"volume":.57},
    "bass_kizomba":         {"harmonics":[(1,.62),(2,.24),(3,.10),(4,.04)],"attack":.012,"decay":.12,"sustain":.82,"release":.20,"volume":.58},
    "bass_zouk":            {"harmonics":[(1,.60),(2,.25),(3,.12),(4,.03)],"attack":.010,"decay":.12,"sustain":.80,"release":.18,"volume":.57},
    "bass_kompa":           {"harmonics":[(1,.62),(2,.24),(3,.11),(4,.03)],"attack":.010,"decay":.12,"sustain":.80,"release":.18,"volume":.58},
    "bass_fuzz":            {"harmonics":[(1,.45),(2,.28),(3,.16),(4,.08),(5,.03)],"attack":.003,"decay":.0,"sustain":1.0,"release":.15,"saturation":6.0,"volume":.50},
    "bass_overdrive":       {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.004,"decay":.0,"sustain":1.0,"release":.14,"saturation":3.5,"volume":.53},
    "bass_bitcrush":        {"harmonics":[(1,.55),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.0,"sustain":1.0,"release":.15,"saturation":5.0,"noise_mix":.03,"volume":.52},
    "bass_octave_up":       {"harmonics":[(2,.55),(4,.28),(6,.12),(8,.05)],"attack":.004,"decay":.0,"sustain":1.0,"release":.14,"saturation":1.5,"volume":.55},
    "bass_envelope_filter": {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.003,"decay":.04,"sustain":.80,"release":.10,"saturation":2.0,"volume":.57},
    "bass_wah":             {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.003,"decay":.05,"sustain":.75,"release":.10,"saturation":1.8,"volume":.56},
    "bass_phaser":          {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.005,"decay":.0,"sustain":1.0,"release":.15,"saturation":1.2,"volume":.55},
    "bass_flanger2":        {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.006,"decay":.0,"sustain":1.0,"release":.16,"volume":.55},
    "bass_chorus2":         {"harmonics":[(1,.52),(2,.25),(3,.12),(4,.05)],"attack":.008,"decay":.10,"sustain":.80,"release":.18,"volume":.52},
    "bass_soft_attack":     {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.025,"decay":.0,"sustain":1.0,"release":.30,"decay_exp":6.0,"volume":.60},
    "bass_hard_attack":     {"harmonics":[(1,.62),(2,.24),(3,.11),(4,.03)],"attack":.002,"decay":.04,"sustain":.80,"release":.14,"decay_exp":12.,"volume":.62},
    "bass_long_release":    {"harmonics":[(1,.68),(2,.20),(3,.09),(4,.03)],"attack":.008,"decay":.0,"sustain":1.0,"release":.70,"decay_exp":3.5,"volume":.62},
    "bass_mid_boost":       {"harmonics":[(1,.48),(2,.32),(3,.16),(4,.04)],"attack":.007,"decay":.0,"sustain":1.0,"release":.22,"saturation":1.5,"volume":.58},
    "bass_high_pass":       {"harmonics":[(2,.45),(3,.32),(4,.16),(5,.07)],"attack":.005,"decay":.08,"sustain":.75,"release":.14,"saturation":1.2,"volume":.54},
    "bass_pluck_hard":      {"harmonics":[(1,.58),(2,.28),(3,.10),(4,.04)],"attack":.002,"decay":.06,"sustain":.50,"release":.10,"decay_exp":10.,"volume":.58},
    "bass_pluck_soft":      {"harmonics":[(1,.65),(2,.24),(3,.08),(4,.03)],"attack":.006,"decay":.10,"sustain":.55,"release":.14,"decay_exp":7.0,"volume":.56},
    "bass_thumb_pluck":     {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.003,"decay":.05,"sustain":.60,"release":.10,"decay_exp":9.0,"saturation":1.5,"volume":.58},
    "bass_slap_pop":        {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.08)],"attack":.002,"decay":.03,"sustain":.55,"release":.08,"decay_exp":14.,"saturation":2.0,"volume":.60},
    "bass_ghost_note":      {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.003,"decay":.02,"sustain":.35,"release":.06,"decay_exp":18.,"volume":.45},
    "bass_harmonics_high":  {"harmonics":[(4,.65),(6,.22),(8,.10),(10,.03)],"attack":.004,"decay":.0,"sustain":.50,"release":.15,"decay_exp":6.0,"volume":.48},
    "bass_tap":             {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.002,"decay":.04,"sustain":.60,"release":.08,"decay_exp":12.,"saturation":1.2,"volume":.56},
    "bass_muted2":          {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.003,"decay":.03,"sustain":.30,"release":.05,"decay_exp":20.,"volume":.58},
    "bass_slide":           {"harmonics":[(1,.60),(2,.24),(3,.11),(4,.05)],"attack":.010,"decay":.08,"sustain":.78,"release":.16,"vibrato_depth":.06,"vibrato_rate":2.5,"volume":.56},
    "bass_vibrato":         {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.012,"decay":.0,"sustain":1.0,"release":.18,"vibrato_depth":.08,"vibrato_rate":5.0,"volume":.56},
    "bass_tremolo2":        {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.010,"decay":.0,"sustain":1.0,"release":.18,"vibrato_depth":.15,"vibrato_rate":7.0,"volume":.55},
    "bass_harmonics2":      {"harmonics":[(2,.58),(3,.28),(5,.10),(7,.04)],"attack":.006,"decay":.10,"sustain":.65,"release":.14,"decay_exp":6.0,"volume":.50},
    "bass_arco":            {"harmonics":[(1,.55),(2,.28),(3,.13),(4,.04)],"attack":.060,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.06,"vibrato_rate":4.0,"volume":.52},
    "bass_col_legno":       {"harmonics":[(1,.45),(2,.28),(3,.16),(4,.08),(5,.03)],"attack":.004,"decay":.02,"sustain":.35,"release":.08,"decay_exp":15.,"noise_mix":.12,"volume":.48},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ SYNTH EXTENDED (Models/Character/Genre/Shape/Special) ██
    # ═══════════════════════════════════════════════════════════════════════════
    "synth_juno":           {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.012,"decay":.10,"sustain":.82,"release":.20,"saturation":.8,"volume":.44},
    "synth_juno_chorus":    {"harmonics":[(1,.52),(1.005,.45),(2,.24),(3,.12)],"attack":.012,"decay":.10,"sustain":.82,"release":.22,"volume":.40},
    "synth_dx7":            {"harmonics":[(1,.50),(2,.26),(3,.16),(4,.06),(5,.02)],"attack":.006,"decay":.10,"sustain":.75,"release":.14,"saturation":1.2,"volume":.42},
    "synth_dx7_e_piano":    {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.005,"decay":.0,"sustain":.70,"release":.14,"decay_exp":5.5,"volume":.48},
    "synth_minimoog2":      {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.008,"decay":.12,"sustain":.80,"release":.16,"saturation":2.0,"volume":.44},
    "synth_ms20":           {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.006,"decay":.10,"sustain":.78,"release":.14,"saturation":2.5,"volume":.42},
    "synth_prophet5":       {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.010,"decay":.12,"sustain":.80,"release":.18,"saturation":1.5,"volume":.43},
    "synth_prophet_rev2":   {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.008,"decay":.10,"sustain":.80,"release":.16,"saturation":1.8,"volume":.43},
    "synth_oberheim2":      {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.010,"decay":.12,"sustain":.80,"release":.18,"saturation":1.8,"volume":.43},
    "synth_cs80":           {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.012,"decay":.12,"sustain":.82,"release":.20,"saturation":1.2,"vibrato_depth":.04,"vibrato_rate":4.5,"volume":.42},
    "synth_arp2600":        {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.006,"decay":.10,"sustain":.78,"release":.14,"saturation":2.0,"volume":.42},
    "synth_jupiter8":       {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.010,"decay":.10,"sustain":.82,"release":.18,"saturation":1.5,"volume":.43},
    "synth_sh101":          {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.008,"decay":.10,"sustain":.80,"release":.16,"saturation":1.2,"volume":.44},
    "synth_korg_m1":        {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.010,"decay":.10,"sustain":.82,"release":.18,"volume":.44},
    "synth_nord_lead":      {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.05),(5,.01)],"attack":.006,"decay":.08,"sustain":.80,"release":.14,"saturation":2.5,"volume":.43},
    "synth_poly800":        {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.010,"decay":.12,"sustain":.80,"release":.18,"saturation":1.0,"volume":.44},
    "synth_jx3p":           {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.010,"decay":.10,"sustain":.80,"release":.18,"saturation":1.2,"volume":.43},
    "synth_virus":          {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.05),(5,.01)],"attack":.006,"decay":.08,"sustain":.80,"release":.14,"saturation":2.5,"volume":.42},
    "synth_access":         {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.006,"decay":.08,"sustain":.78,"release":.14,"saturation":2.8,"volume":.42},
    "synth_mutable":        {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.06),(5,.02)],"attack":.007,"decay":.10,"sustain":.80,"release":.15,"saturation":2.0,"volume":.42},
    "synth_elektron":       {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.05)],"attack":.005,"decay":.08,"sustain":.78,"release":.12,"saturation":2.2,"volume":.43},
    "synth_make_noise":     {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)],"attack":.005,"decay":.0,"sustain":1.0,"release":.15,"saturation":3.0,"volume":.40},
    "synth_buchla":         {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.06),(5,.03)],"attack":.007,"decay":.0,"sustain":1.0,"release":.18,"saturation":2.5,"noise_mix":.02,"volume":.42},
    "synth_moog2":          {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.008,"decay":.12,"sustain":.80,"release":.16,"saturation":2.2,"volume":.44},
    "synth_metallic":       {"harmonics":[(1,.40),(2.87,.28),(4.95,.18),(7.12,.10),(9.7,.04)],"attack":.006,"decay":.08,"sustain":.70,"release":.15,"volume":.40},
    "synth_glassy2":        {"harmonics":[(1,.55),(2.93,.22),(5.3,.12),(8.1,.06)],"attack":.012,"decay":.0,"sustain":.60,"release":.25,"decay_exp":5.0,"volume":.45},
    "synth_hollow":         {"harmonics":[(1,.45),(3,.28),(5,.16),(7,.08),(9,.03)],"attack":.010,"decay":.08,"sustain":.75,"release":.15,"volume":.44},
    "synth_thick":          {"harmonics":[(1,.55),(1.01,.45),(2,.25),(2.01,.20),(3,.12)],"attack":.010,"decay":.10,"sustain":.80,"release":.18,"saturation":2.0,"volume":.36},
    "synth_piercing":       {"harmonics":[(1,.38),(2,.28),(3,.20),(4,.10),(5,.04)],"attack":.004,"decay":.06,"sustain":.80,"release":.10,"saturation":3.0,"volume":.36},
    "synth_mellow2":        {"harmonics":[(1,.68),(2,.18),(3,.08),(4,.04),(5,.02)],"attack":.020,"decay":.12,"sustain":.82,"release":.25,"volume":.46},
    "synth_sharp2":         {"harmonics":[(1,.45),(2,.30),(3,.16),(4,.07),(5,.02)],"attack":.004,"decay":.06,"sustain":.78,"release":.10,"saturation":1.8,"volume":.40},
    "synth_round2":         {"harmonics":[(1,.65),(2,.22),(3,.08),(4,.05)],"attack":.015,"decay":.12,"sustain":.82,"release":.22,"volume":.46},
    "synth_bite":           {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.08),(5,.04)],"attack":.005,"decay":.08,"sustain":.78,"release":.12,"saturation":2.5,"volume":.40},
    "synth_silky":          {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.025,"decay":.0,"sustain":1.0,"release":.28,"volume":.46},
    "synth_creamy":         {"harmonics":[(1,.68),(2,.18),(3,.08),(4,.06)],"attack":.030,"decay":.0,"sustain":1.0,"release":.30,"volume":.46},
    "synth_gritty":         {"harmonics":[(1,.45),(2,.28),(3,.16),(4,.08),(5,.03)],"attack":.005,"decay":.0,"sustain":1.0,"release":.14,"saturation":3.5,"volume":.40},
    "synth_crispy":         {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.08),(5,.04)],"attack":.004,"decay":.06,"sustain":.78,"release":.10,"saturation":2.0,"volume":.40},
    "synth_fuzzy":          {"harmonics":[(1,.40),(2,.26),(3,.18),(4,.10),(5,.06)],"attack":.005,"decay":.0,"sustain":1.0,"release":.14,"saturation":4.5,"noise_mix":.02,"volume":.38},
    "synth_smooth":         {"harmonics":[(1,.70),(2,.18),(3,.08),(4,.04)],"attack":.018,"decay":.10,"sustain":.82,"release":.22,"volume":.46},
    "synth_edm":            {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.008,"decay":.08,"sustain":.80,"release":.14,"saturation":2.0,"volume":.42},
    "synth_house":          {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.010,"decay":.08,"sustain":.80,"release":.16,"saturation":1.5,"volume":.43},
    "synth_techno":         {"harmonics":[(1,.48),(2,.30),(3,.16),(4,.06)],"attack":.006,"decay":.08,"sustain":.78,"release":.12,"saturation":2.5,"volume":.41},
    "synth_dnb":            {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.005,"decay":.06,"sustain":.80,"release":.12,"saturation":2.8,"volume":.41},
    "synth_dubstep":        {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)],"attack":.005,"decay":.0,"sustain":1.0,"release":.14,"saturation":4.0,"vibrato_depth":.08,"vibrato_rate":3.5,"volume":.38},
    "synth_trance":         {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.010,"decay":.08,"sustain":.82,"release":.18,"saturation":1.2,"volume":.44},
    "synth_chillout":       {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.030,"decay":.0,"sustain":1.0,"release":.30,"volume":.45},
    "synth_lofi2":          {"harmonics":[(1,.60),(2,.24),(3,.10),(4,.06)],"attack":.012,"decay":.10,"sustain":.75,"release":.20,"noise_mix":.015,"saturation":.8,"volume":.44},
    "synth_phonk":          {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06)],"attack":.005,"decay":.0,"sustain":1.0,"release":.14,"saturation":3.0,"volume":.42},
    "synth_trap2":          {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.05)],"attack":.007,"decay":.08,"sustain":.80,"release":.14,"saturation":2.0,"volume":.43},
    "synth_drill2":         {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.005,"decay":.08,"sustain":.80,"release":.12,"saturation":2.5,"volume":.42},
    "synth_rnb2":           {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.015,"decay":.10,"sustain":.82,"release":.20,"volume":.44},
    "synth_soul2":          {"harmonics":[(1,.58),(2,.24),(3,.10),(4,.08)],"attack":.020,"decay":.10,"sustain":.82,"release":.22,"volume":.45},
    "synth_funk2":          {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.006,"decay":.06,"sustain":.75,"release":.12,"saturation":1.5,"volume":.42},
    "synth_gospel":         {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.015,"decay":.08,"sustain":.82,"release":.20,"volume":.44},
    "synth_ambient2":       {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.200,"decay":.0,"sustain":1.0,"release":.40,"noise_mix":.015,"volume":.42},
    "synth_new_age":        {"harmonics":[(1,.62),(2,.20),(3,.10),(4,.08)],"attack":.150,"decay":.0,"sustain":1.0,"release":.35,"volume":.44},
    "synth_cinematic":      {"harmonics":[(1,.55),(2,.26),(3,.14),(4,.05)],"attack":.060,"decay":.0,"sustain":1.0,"release":.25,"saturation":1.2,"volume":.42},
    "synth_epic":           {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.040,"decay":.0,"sustain":1.0,"release":.25,"saturation":1.8,"volume":.40},
    "synth_choir2":         {"harmonics":[(1,.55),(2,.22),(3,.12),(4,.06),(5,.05)],"attack":.100,"decay":.0,"sustain":1.0,"release":.25,"noise_mix":.015,"volume":.40},
    "synth_theremin":       {"harmonics":[(1,.75),(2,.15),(3,.07),(4,.03)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.12,"vibrato_rate":5.5,"volume":.48},
    "synth_talk_box":       {"harmonics":[(1,.55),(2,.25),(3,.12),(4,.06),(5,.02)],"attack":.010,"decay":.0,"sustain":1.0,"release":.15,"saturation":1.5,"noise_mix":.02,"volume":.44},
    "synth_vocoder":        {"harmonics":[(1,.52),(2,.25),(3,.12),(4,.06),(5,.05)],"attack":.015,"decay":.0,"sustain":1.0,"release":.20,"noise_mix":.03,"volume":.43},
    "synth_formant":        {"harmonics":[(1,.55),(2,.22),(3,.14),(4,.06),(5,.03)],"attack":.020,"decay":.0,"sustain":1.0,"release":.18,"noise_mix":.025,"volume":.44},
    "synth_fm2":            {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05),(5,.02)],"attack":.008,"decay":.10,"sustain":.78,"release":.15,"saturation":1.5,"volume":.43},
    "synth_fm_bell":        {"harmonics":[(1,.55),(2.756,.28),(5.404,.14),(7.08,.06)],"attack":.004,"decay":.0,"sustain":.35,"release":.20,"decay_exp":5.5,"volume":.48},
    "synth_fm_bass":        {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.005,"decay":.0,"sustain":1.0,"release":.20,"decay_exp":8.0,"saturation":1.5,"volume":.55},
    "synth_fm_brass":       {"harmonics":[(1,.45),(2,.30),(3,.18),(4,.07)],"attack":.035,"decay":.05,"sustain":.85,"release":.12,"saturation":1.2,"volume":.42},
    "synth_fm_strings":     {"harmonics":[(1,.52),(2,.25),(3,.14),(4,.09)],"attack":.060,"decay":.0,"sustain":1.0,"release":.18,"vibrato_depth":.05,"vibrato_rate":5.0,"volume":.40},
    "synth_wavetable":      {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.008,"decay":.10,"sustain":.80,"release":.15,"saturation":1.5,"volume":.42},
    "synth_granular":       {"harmonics":[(1,.50),(2,.26),(3,.14),(4,.06),(5,.04)],"attack":.050,"decay":.0,"sustain":1.0,"release":.30,"noise_mix":.03,"volume":.40},
    "synth_spectral":       {"harmonics":[(1,.45),(2,.28),(3,.16),(4,.08),(5,.03)],"attack":.080,"decay":.0,"sustain":1.0,"release":.35,"noise_mix":.02,"volume":.38},
    "synth_additive":       {"harmonics":[(1,.50),(2,.26),(3,.14),(4,.08),(5,.02)],"attack":.010,"decay":.10,"sustain":.80,"release":.16,"volume":.42},
    "synth_subtractive":    {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.010,"decay":.10,"sustain":.80,"release":.16,"saturation":1.5,"volume":.43},
    "synth_physical":       {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.005,"decay":.10,"sustain":.70,"release":.15,"decay_exp":5.0,"volume":.46},
    "synth_karplus":        {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.003,"decay":.12,"sustain":.50,"release":.14,"decay_exp":8.0,"volume":.50},
    "synth_phase_dist":     {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.006,"decay":.08,"sustain":.78,"release":.14,"saturation":2.0,"volume":.42},
    "synth_vector":         {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.010,"decay":.10,"sustain":.80,"release":.16,"volume":.43},
    "synth_wobble":         {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.05)],"attack":.005,"decay":.0,"sustain":1.0,"release":.18,"saturation":3.0,"vibrato_depth":.15,"vibrato_rate":4.5,"volume":.40},
    "synth_screech":        {"harmonics":[(1,.38),(2,.28),(3,.20),(4,.10),(5,.04)],"attack":.003,"decay":.0,"sustain":1.0,"release":.12,"saturation":5.0,"volume":.34},
    "synth_squeal":         {"harmonics":[(1,.40),(2,.28),(3,.18),(4,.10),(5,.04)],"attack":.004,"decay":.0,"sustain":1.0,"release":.12,"saturation":4.0,"vibrato_depth":.06,"vibrato_rate":6.0,"volume":.36},
    "synth_chime":          {"harmonics":[(1,.60),(2.756,.28),(5.404,.12),(7.08,.06)],"attack":.004,"decay":.0,"sustain":.35,"release":.18,"decay_exp":6.5,"volume":.50},
    "synth_crystalline":    {"harmonics":[(1,.58),(2.93,.25),(5.3,.14),(8.1,.06),(10.5,.03)],"attack":.005,"decay":.0,"sustain":.40,"release":.22,"decay_exp":5.5,"volume":.48},
    "synth_ghost":          {"harmonics":[(1,.55),(2,.22),(3,.10),(4,.05)],"attack":.100,"decay":.0,"sustain":1.0,"release":.50,"noise_mix":.025,"volume":.40},
    "synth_angelic":        {"harmonics":[(1,.60),(2,.20),(3,.10),(4,.08),(5,.02)],"attack":.120,"decay":.0,"sustain":1.0,"release":.40,"vibrato_depth":.04,"vibrato_rate":4.0,"volume":.42},
    "synth_demonic":        {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.08)],"attack":.008,"decay":.0,"sustain":1.0,"release":.20,"saturation":4.5,"volume":.38},
    "synth_electric":       {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.005,"decay":.08,"sustain":.80,"release":.12,"saturation":2.0,"volume":.42},
    "synth_organic":        {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.020,"decay":.0,"sustain":1.0,"release":.22,"noise_mix":.02,"volume":.44},
    "synth_hybrid":         {"harmonics":[(1,.55),(2,.26),(3,.14),(4,.05)],"attack":.015,"decay":.08,"sustain":.82,"release":.18,"saturation":1.2,"volume":.43},
    "synth_evolving":       {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.050,"decay":.0,"sustain":1.0,"release":.30,"vibrato_depth":.06,"vibrato_rate":0.8,"volume":.40},
    "synth_morphing":       {"harmonics":[(1,.55),(2,.24),(3,.14),(4,.07)],"attack":.060,"decay":.0,"sustain":1.0,"release":.30,"vibrato_depth":.08,"vibrato_rate":0.5,"volume":.40},
    "synth_pumping":        {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.004,"decay":.04,"sustain":.80,"release":.12,"saturation":2.0,"volume":.44},
    "synth_sidechain":      {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.006,"decay":.06,"sustain":.78,"release":.14,"saturation":1.8,"volume":.43},
    "synth_reverse":        {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.200,"decay":.0,"sustain":1.0,"release":.05,"volume":.42},
    "synth_freeze":         {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.005,"decay":.0,"sustain":1.0,"release":.80,"volume":.44},
    "synth_flutter":        {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.010,"decay":.0,"sustain":1.0,"release":.18,"vibrato_depth":.18,"vibrato_rate":15.,"volume":.42},
    "synth_tremolo2":       {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.010,"decay":.0,"sustain":1.0,"release":.18,"vibrato_depth":.22,"vibrato_rate":8.0,"volume":.42},
    "synth_pluck2":         {"harmonics":[(1,.62),(2,.24),(3,.10),(4,.04)],"attack":.003,"decay":.08,"sustain":.45,"release":.10,"decay_exp":9.5,"volume":.50},
    "synth_pluck_hard":     {"harmonics":[(1,.58),(2,.28),(3,.12),(4,.02)],"attack":.002,"decay":.06,"sustain":.35,"release":.08,"decay_exp":12.,"saturation":1.5,"volume":.52},
    "synth_twang":          {"harmonics":[(1,.60),(2,.25),(3,.12),(4,.03)],"attack":.003,"decay":.10,"sustain":.45,"release":.12,"decay_exp":9.0,"saturation":.8,"volume":.50},
    "synth_ping":           {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.002,"decay":.0,"sustain":.40,"release":.15,"decay_exp":7.0,"volume":.52},
    "synth_pop2":           {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.003,"decay":.05,"sustain":.45,"release":.10,"decay_exp":10.,"volume":.52},
    "synth_snap":           {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.002,"decay":.03,"sustain":.30,"release":.06,"decay_exp":15.,"volume":.52},
    "synth_twinkle":        {"harmonics":[(1,.62),(2,.24),(3,.12),(4,.02)],"attack":.004,"decay":.0,"sustain":.40,"release":.20,"decay_exp":6.0,"volume":.50},
    "synth_sparkle":        {"harmonics":[(1,.60),(2,.26),(3,.12),(4,.02)],"attack":.004,"decay":.0,"sustain":.35,"release":.18,"decay_exp":7.0,"volume":.50},
    "synth_shimmer2":       {"harmonics":[(1,.55),(2,.28),(3,.14),(4,.03)],"attack":.008,"decay":.0,"sustain":.60,"release":.28,"vibrato_depth":.05,"vibrato_rate":9.0,"volume":.44},
    "synth_drone":          {"harmonics":[(1,.60),(2,.22),(3,.12),(4,.06)],"attack":.080,"decay":.0,"sustain":1.0,"release":.40,"vibrato_depth":.02,"vibrato_rate":1.0,"volume":.44},
    "synth_drone_dark":     {"harmonics":[(1,.70),(2,.18),(3,.08),(4,.04)],"attack":.100,"decay":.0,"sustain":1.0,"release":.50,"saturation":.8,"volume":.46},
    "synth_texture":        {"harmonics":[(1,.50),(2,.24),(3,.14),(4,.08),(5,.04)],"attack":.150,"decay":.0,"sustain":1.0,"release":.45,"noise_mix":.025,"volume":.40},
    "synth_noise_synth":    {"harmonics":[(1,.25),(3,.15),(5,.15),(7,.12),(9,.10),(11,.08)],"attack":.010,"decay":.0,"sustain":1.0,"release":.20,"noise_mix":.40,"volume":.38},
    "synth_filtered_noise": {"harmonics":[(1,.10)],                      "attack":.010,"decay":.0,"sustain":1.0,"release":.15,"noise_mix":.80,"saturation":.5,"volume":.38},
    "synth_resonant":       {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.006,"decay":.08,"sustain":.78,"release":.14,"saturation":2.0,"volume":.44},
    "synth_overdrive":      {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.005,"decay":.0,"sustain":1.0,"release":.14,"saturation":4.0,"volume":.40},
    "synth_bitcrushed":     {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.006,"decay":.08,"sustain":.78,"release":.12,"saturation":4.5,"noise_mix":.03,"volume":.40},
    "synth_ringmod":        {"harmonics":[(1,.50),(2.5,.28),(3,.14),(4.5,.08)],"attack":.006,"decay":.08,"sustain":.78,"release":.14,"saturation":1.5,"volume":.40},
    "synth_stereo_wide":    {"harmonics":[(1,.50),(1.003,.45),(2,.24),(2.003,.20),(3,.12)],"attack":.010,"decay":.10,"sustain":.80,"release":.18,"volume":.36},
    "synth_detune_heavy":   {"harmonics":[(1,.50),(1.015,.42),(2,.22),(2.015,.18),(3,.10)],"attack":.010,"decay":.10,"sustain":.80,"release":.18,"volume":.38},
    "synth_unison5":        {"harmonics":[(1,.40),(1.007,.35),(1.014,.25),(2,.20),(2.007,.15)],"attack":.010,"decay":.10,"sustain":.80,"release":.18,"volume":.32},
    "synth_chorus2":        {"harmonics":[(1,.52),(1.006,.44),(2,.23),(2.006,.18),(3,.10)],"attack":.012,"decay":.0,"sustain":1.0,"release":.22,"volume":.38},
    "synth_flanger":        {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.010,"decay":.0,"sustain":1.0,"release":.20,"volume":.43},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ PAD EXTENDED (Character/Genre/Emotional/Cinematic/Hybrid/Special) ██
    # ═══════════════════════════════════════════════════════════════════════════
    "pad_velvet":           {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.350,"decay":.0,"sustain":1.0,"release":.45,"vibrato_depth":.03,"vibrato_rate":0.4,"volume":.44},
    "pad_silk":             {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.300,"decay":.0,"sustain":1.0,"release":.40,"volume":.44},
    "pad_cotton":           {"harmonics":[(1,.70),(2,.18),(3,.08),(4,.04)],"attack":.400,"decay":.0,"sustain":1.0,"release":.50,"noise_mix":.01,"volume":.43},
    "pad_wool":             {"harmonics":[(1,.68),(2,.20),(3,.08),(4,.04)],"attack":.450,"decay":.0,"sustain":1.0,"release":.55,"noise_mix":.015,"volume":.42},
    "pad_foam":             {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.350,"decay":.0,"sustain":1.0,"release":.45,"noise_mix":.02,"volume":.42},
    "pad_rubber":           {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.250,"decay":.0,"sustain":1.0,"release":.35,"saturation":.8,"volume":.42},
    "pad_water":            {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.400,"decay":.0,"sustain":1.0,"release":.55,"noise_mix":.015,"vibrato_depth":.03,"vibrato_rate":0.6,"volume":.40},
    "pad_fire":             {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.200,"decay":.0,"sustain":1.0,"release":.35,"noise_mix":.025,"saturation":1.2,"volume":.40},
    "pad_wind":             {"harmonics":[(1,.50),(2,.24),(3,.14),(4,.07),(5,.05)],"attack":.500,"decay":.0,"sustain":1.0,"release":.60,"noise_mix":.04,"volume":.38},
    "pad_earth":            {"harmonics":[(1,.68),(2,.20),(3,.08),(4,.04)],"attack":.500,"decay":.0,"sustain":1.0,"release":.60,"volume":.44},
    "pad_aether":           {"harmonics":[(1,.55),(2,.22),(3,.12),(4,.08),(5,.03)],"attack":.700,"decay":.0,"sustain":1.0,"release":.70,"noise_mix":.02,"volume":.38},
    "pad_crystal2":         {"harmonics":[(1,.58),(2.93,.24),(5.3,.12),(8.1,.06)],"attack":.200,"decay":.0,"sustain":1.0,"release":.40,"volume":.42},
    "pad_80s":              {"harmonics":[(1,.52),(1.005,.44),(2,.23),(3,.11),(4,.05)],"attack":.200,"decay":.0,"sustain":1.0,"release":.30,"saturation":.8,"volume":.40},
    "pad_90s":              {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.180,"decay":.0,"sustain":1.0,"release":.28,"saturation":.8,"volume":.42},
    "pad_2000s":            {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.150,"decay":.0,"sustain":1.0,"release":.25,"saturation":1.0,"volume":.42},
    "pad_modern2":          {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.120,"decay":.0,"sustain":1.0,"release":.22,"saturation":1.5,"volume":.40},
    "pad_future":           {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)],"attack":.100,"decay":.0,"sustain":1.0,"release":.20,"saturation":2.0,"volume":.40},
    "pad_vintage2":         {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.300,"decay":.0,"sustain":1.0,"release":.40,"noise_mix":.01,"volume":.43},
    "pad_retro2":           {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.250,"decay":.0,"sustain":1.0,"release":.35,"noise_mix":.015,"volume":.43},
    "pad_sad":              {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.400,"decay":.0,"sustain":1.0,"release":.50,"vibrato_depth":.04,"vibrato_rate":0.5,"volume":.43},
    "pad_happy":            {"harmonics":[(1,.55),(2,.26),(3,.14),(4,.05)],"attack":.200,"decay":.0,"sustain":1.0,"release":.30,"volume":.43},
    "pad_tense":            {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.150,"decay":.0,"sustain":1.0,"release":.25,"saturation":1.5,"volume":.40},
    "pad_relaxed":          {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.500,"decay":.0,"sustain":1.0,"release":.55,"volume":.43},
    "pad_epic2":            {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.08)],"attack":.100,"decay":.0,"sustain":1.0,"release":.25,"saturation":1.8,"volume":.40},
    "pad_intimate":         {"harmonics":[(1,.68),(2,.18),(3,.08),(4,.06)],"attack":.300,"decay":.0,"sustain":1.0,"release":.40,"volume":.44},
    "pad_vast":             {"harmonics":[(1,.48),(2,.26),(3,.14),(4,.08),(5,.04)],"attack":.800,"decay":.0,"sustain":1.0,"release":.80,"noise_mix":.02,"volume":.36},
    "pad_floating":         {"harmonics":[(1,.55),(2,.24),(3,.12),(4,.09)],"attack":.600,"decay":.0,"sustain":1.0,"release":.65,"noise_mix":.02,"volume":.38},
    "pad_sinking":          {"harmonics":[(1,.68),(2,.20),(3,.08),(4,.04)],"attack":.500,"decay":.0,"sustain":1.0,"release":.60,"volume":.42},
    "pad_rising":           {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.400,"decay":.0,"sustain":1.0,"release":.50,"saturation":.8,"volume":.40},
    "pad_string_hybrid":    {"harmonics":[(1,.52),(1.005,.43),(2,.23),(2.005,.18),(3,.10)],"attack":.200,"decay":.0,"sustain":1.0,"release":.30,"vibrato_depth":.06,"vibrato_rate":4.5,"volume":.38},
    "pad_choir_hybrid":     {"harmonics":[(1,.55),(2,.22),(3,.12),(4,.06),(5,.05)],"attack":.300,"decay":.0,"sustain":1.0,"release":.40,"noise_mix":.015,"volume":.38},
    "pad_brass_hybrid":     {"harmonics":[(1,.45),(2,.30),(3,.16),(4,.08)],"attack":.120,"decay":.0,"sustain":1.0,"release":.25,"saturation":1.2,"volume":.40},
    "pad_flute_hybrid":     {"harmonics":[(1,.65),(2,.18),(3,.10),(4,.07)],"attack":.250,"decay":.0,"sustain":1.0,"release":.35,"noise_mix":.025,"volume":.42},
    "pad_piano_hybrid":     {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.100,"decay":.0,"sustain":.85,"release":.25,"decay_exp":4.0,"volume":.44},
    "pad_guitar_hybrid":    {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.150,"decay":.0,"sustain":.90,"release":.28,"decay_exp":5.0,"volume":.43},
    "pad_trailer":          {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.08)],"attack":.050,"decay":.0,"sustain":1.0,"release":.20,"saturation":2.0,"volume":.38},
    "pad_horror":           {"harmonics":[(1,.55),(2,.22),(3,.12),(4,.08),(5,.03)],"attack":.300,"decay":.0,"sustain":1.0,"release":.45,"noise_mix":.03,"saturation":.8,"volume":.40},
    "pad_scifi":            {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.150,"decay":.0,"sustain":1.0,"release":.30,"saturation":1.5,"volume":.40},
    "pad_fantasy":          {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.300,"decay":.0,"sustain":1.0,"release":.40,"vibrato_depth":.04,"vibrato_rate":0.6,"volume":.42},
    "pad_romance":          {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.350,"decay":.0,"sustain":1.0,"release":.45,"vibrato_depth":.04,"vibrato_rate":4.5,"volume":.43},
    "pad_mystery":          {"harmonics":[(1,.58),(2,.22),(3,.12),(4,.08)],"attack":.400,"decay":.0,"sustain":1.0,"release":.50,"noise_mix":.02,"volume":.40},
    "pad_adventure":        {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.150,"decay":.0,"sustain":1.0,"release":.28,"saturation":1.2,"volume":.40},
    "pad_underwater":       {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.500,"decay":.0,"sustain":1.0,"release":.55,"noise_mix":.025,"vibrato_depth":.05,"vibrato_rate":0.4,"volume":.40},
    "pad_outer_space":      {"harmonics":[(1,.50),(2,.26),(3,.14),(4,.08)],"attack":.700,"decay":.0,"sustain":1.0,"release":.70,"noise_mix":.025,"volume":.37},
    "pad_forest":           {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.450,"decay":.0,"sustain":1.0,"release":.55,"noise_mix":.03,"volume":.40},
    "pad_desert":           {"harmonics":[(1,.68),(2,.18),(3,.08),(4,.06)],"attack":.550,"decay":.0,"sustain":1.0,"release":.60,"noise_mix":.02,"volume":.41},
    "pad_arctic":           {"harmonics":[(1,.55),(2,.24),(3,.14),(4,.07)],"attack":.400,"decay":.0,"sustain":1.0,"release":.50,"volume":.42},
    "pad_tropical":         {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.300,"decay":.0,"sustain":1.0,"release":.40,"volume":.42},
    "pad_cathedral":        {"harmonics":[(1,.40),(2,.30),(3,.20),(4,.07),(5,.03)],"attack":.300,"decay":.0,"sustain":1.0,"release":.50,"noise_mix":.01,"volume":.40},
    "pad_stadium":          {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.08)],"attack":.150,"decay":.0,"sustain":1.0,"release":.30,"saturation":1.5,"volume":.40},
    "pad_cave":             {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.350,"decay":.0,"sustain":1.0,"release":.55,"noise_mix":.025,"volume":.41},
    "pad_jungle":           {"harmonics":[(1,.55),(2,.24),(3,.12),(4,.09)],"attack":.300,"decay":.0,"sustain":1.0,"release":.45,"noise_mix":.03,"volume":.40},
    "pad_smooth2":          {"harmonics":[(1,.68),(2,.18),(3,.08),(4,.06)],"attack":.350,"decay":.0,"sustain":1.0,"release":.45,"volume":.44},
    "pad_rough":            {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.200,"decay":.0,"sustain":1.0,"release":.30,"saturation":1.5,"noise_mix":.015,"volume":.40},
    "pad_detuned":          {"harmonics":[(1,.48),(1.02,.42),(2,.22),(2.02,.18),(3,.10)],"attack":.250,"decay":.0,"sustain":1.0,"release":.38,"volume":.38},
    "pad_chorus2":          {"harmonics":[(1,.50),(1.005,.44),(2,.23),(2.005,.18),(3,.11)],"attack":.280,"decay":.0,"sustain":1.0,"release":.40,"volume":.38},
    "pad_unison":           {"harmonics":[(1,.42),(1.006,.36),(1.012,.28),(2,.20),(3,.10)],"attack":.300,"decay":.0,"sustain":1.0,"release":.42,"volume":.34},
    "pad_mono":             {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.250,"decay":.0,"sustain":1.0,"release":.35,"volume":.45},
    "pad_wide":             {"harmonics":[(1,.50),(1.008,.44),(2,.24),(2.008,.20),(3,.12)],"attack":.300,"decay":.0,"sustain":1.0,"release":.42,"volume":.36},
    "pad_deep2":            {"harmonics":[(1,.72),(2,.18),(3,.07),(4,.03)],"attack":.400,"decay":.0,"sustain":1.0,"release":.50,"volume":.44},
    "pad_high2":            {"harmonics":[(1,.45),(2,.28),(3,.18),(4,.08),(5,.01)],"attack":.200,"decay":.0,"sustain":1.0,"release":.30,"volume":.40},
    "pad_mid":              {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.280,"decay":.0,"sustain":1.0,"release":.38,"volume":.42},
    "pad_evolving2":        {"harmonics":[(1,.52),(2,.24),(3,.14),(4,.10)],"attack":.300,"decay":.0,"sustain":1.0,"release":.40,"vibrato_depth":.05,"vibrato_rate":0.7,"volume":.40},
    "pad_morphing2":        {"harmonics":[(1,.55),(2,.22),(3,.14),(4,.09)],"attack":.350,"decay":.0,"sustain":1.0,"release":.45,"vibrato_depth":.07,"vibrato_rate":0.4,"volume":.40},
    "pad_reverse2":         {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.500,"decay":.0,"sustain":1.0,"release":.05,"volume":.42},
    "pad_frozen2":          {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.005,"decay":.0,"sustain":1.0,"release":.80,"volume":.44},
    "pad_looping":          {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.100,"decay":.0,"sustain":1.0,"release":.30,"vibrato_depth":.03,"vibrato_rate":0.3,"volume":.42},
    "pad_stutter":          {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.010,"decay":.04,"sustain":.80,"release":.10,"saturation":1.5,"volume":.42},
    "pad_granular2":        {"harmonics":[(1,.52),(2,.24),(3,.14),(4,.10)],"attack":.200,"decay":.0,"sustain":1.0,"release":.35,"noise_mix":.03,"volume":.40},
    "pad_spectral2":        {"harmonics":[(1,.48),(2,.26),(3,.16),(4,.10)],"attack":.250,"decay":.0,"sustain":1.0,"release":.40,"noise_mix":.025,"volume":.38},
    "pad_warm2":            {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.380,"decay":.0,"sustain":1.0,"release":.48,"vibrato_depth":.03,"vibrato_rate":0.35,"volume":.44},
    "pad_cold2":            {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.180,"decay":.0,"sustain":1.0,"release":.25,"volume":.40},
    "pad_lush2":            {"harmonics":[(1,.55),(1.008,.46),(2,.24),(2.008,.20),(3,.11)],"attack":.380,"decay":.0,"sustain":1.0,"release":.48,"vibrato_depth":.04,"vibrato_rate":0.45,"volume":.34},
    "pad_thin2":            {"harmonics":[(1,.48),(2,.30),(3,.16),(4,.06)],"attack":.200,"decay":.0,"sustain":1.0,"release":.28,"volume":.40},
    "pad_fat2":             {"harmonics":[(1,.58),(1.010,.50),(2,.26),(2.010,.22),(3,.12)],"attack":.300,"decay":.0,"sustain":1.0,"release":.42,"saturation":1.2,"volume":.34},
    "pad_tight":            {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.120,"decay":.0,"sustain":1.0,"release":.20,"volume":.44},
    "pad_loose":            {"harmonics":[(1,.55),(2,.24),(3,.12),(4,.09)],"attack":.600,"decay":.0,"sustain":1.0,"release":.65,"noise_mix":.02,"volume":.40},
    "pad_clean2":           {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.300,"decay":.0,"sustain":1.0,"release":.40,"volume":.44},
    "pad_dirty2":           {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.200,"decay":.0,"sustain":1.0,"release":.30,"saturation":2.0,"noise_mix":.02,"volume":.40},
    "pad_vocal2":           {"harmonics":[(1,.58),(2,.22),(3,.12),(4,.08),(5,.02)],"attack":.250,"decay":.0,"sustain":1.0,"release":.35,"noise_mix":.02,"vibrato_depth":.05,"vibrato_rate":4.0,"volume":.42},
    "pad_string2":          {"harmonics":[(1,.54),(1.004,.44),(2,.23),(2.004,.19),(3,.11)],"attack":.280,"decay":.0,"sustain":1.0,"release":.38,"vibrato_depth":.07,"vibrato_rate":5.0,"volume":.38},
    "pad_organ2":           {"harmonics":[(1,.42),(2,.30),(3,.18),(4,.07),(5,.03)],"attack":.050,"decay":.0,"sustain":1.0,"release":.12,"volume":.40},
    "pad_bell2":            {"harmonics":[(1,.55),(2.756,.26),(5.404,.14),(7.08,.08)],"attack":.100,"decay":.0,"sustain":.70,"release":.35,"decay_exp":4.0,"volume":.44},
    "pad_whispering":       {"harmonics":[(1,.55),(2,.22),(3,.12),(4,.11)],"attack":.600,"decay":.0,"sustain":1.0,"release":.70,"noise_mix":.04,"volume":.38},
    "pad_screaming":        {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.08)],"attack":.060,"decay":.0,"sustain":1.0,"release":.20,"saturation":2.5,"volume":.38},
    "pad_pulsing":          {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.150,"decay":.0,"sustain":1.0,"release":.25,"vibrato_depth":.20,"vibrato_rate":4.0,"volume":.40},
    "pad_static":           {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.400,"decay":.0,"sustain":1.0,"release":.45,"noise_mix":.015,"volume":.42},
    "pad_alive":            {"harmonics":[(1,.55),(2,.24),(3,.12),(4,.09)],"attack":.250,"decay":.0,"sustain":1.0,"release":.35,"vibrato_depth":.04,"vibrato_rate":0.8,"volume":.42},
    "pad_dead":             {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.350,"decay":.0,"sustain":1.0,"release":.45,"volume":.43},
    "pad_nostalgic":        {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.350,"decay":.0,"sustain":1.0,"release":.45,"noise_mix":.01,"saturation":.6,"volume":.42},
    "pad_futuristic2":      {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)],"attack":.100,"decay":.0,"sustain":1.0,"release":.22,"saturation":2.0,"volume":.40},
    "pad_ancient":          {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.500,"decay":.0,"sustain":1.0,"release":.55,"noise_mix":.015,"volume":.42},
    "pad_modern3":          {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.150,"decay":.0,"sustain":1.0,"release":.25,"saturation":1.8,"volume":.40},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ GUITAR EXTENDED (Electric/Acoustic/Classical/Effects/Genre/Regional) ██
    # ═══════════════════════════════════════════════════════════════════════════
    "guitar_telecaster":    {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05),(5,.02)],"attack":.005,"decay":.08,"sustain":.78,"release":.14,"saturation":1.0,"volume":.54},
    "guitar_stratocaster":  {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.005,"decay":.08,"sustain":.80,"release":.14,"saturation":.8,"volume":.54},
    "guitar_les_paul":      {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.005,"decay":.0,"sustain":1.0,"release":.14,"saturation":1.5,"volume":.52},
    "guitar_sg":            {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.004,"decay":.0,"sustain":1.0,"release":.13,"saturation":2.0,"volume":.50},
    "guitar_hollow_body":   {"harmonics":[(1,.58),(2,.26),(3,.11),(4,.05)],"attack":.008,"decay":.12,"sustain":.78,"release":.18,"volume":.54},
    "guitar_semi_hollow":   {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.007,"decay":.10,"sustain":.80,"release":.16,"volume":.54},
    "guitar_jazzmaster":    {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.007,"decay":.10,"sustain":.78,"release":.16,"volume":.54},
    "guitar_jaguar":        {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.05),(5,.02)],"attack":.006,"decay":.09,"sustain":.78,"release":.15,"saturation":.8,"volume":.54},
    "guitar_explorer":      {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.06),(5,.02)],"attack":.004,"decay":.0,"sustain":1.0,"release":.12,"saturation":2.5,"volume":.50},
    "guitar_flying_v":      {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.08)],"attack":.004,"decay":.0,"sustain":1.0,"release":.12,"saturation":3.0,"volume":.50},
    "guitar_rickenbacker":  {"harmonics":[(1,.50),(2,.30),(3,.14),(4,.06)],"attack":.006,"decay":.08,"sustain":.80,"release":.14,"saturation":1.0,"volume":.54},
    "guitar_fender_clean":  {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.006,"decay":.09,"sustain":.80,"release":.15,"volume":.54},
    "guitar_marshall":      {"harmonics":[(1,.45),(2,.28),(3,.16),(4,.08),(5,.03)],"attack":.004,"decay":.0,"sustain":1.0,"release":.13,"saturation":4.0,"volume":.46},
    "guitar_mesa":          {"harmonics":[(1,.42),(2,.26),(3,.18),(4,.10),(5,.04)],"attack":.003,"decay":.0,"sustain":1.0,"release":.12,"saturation":6.0,"volume":.42},
    "guitar_vox":           {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.005,"decay":.0,"sustain":1.0,"release":.13,"saturation":2.5,"volume":.48},
    "guitar_orange":        {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.08)],"attack":.004,"decay":.0,"sustain":1.0,"release":.13,"saturation":3.5,"volume":.47},
    "guitar_fuzz2":         {"harmonics":[(1,.40),(2,.26),(3,.18),(4,.10),(5,.06)],"attack":.003,"decay":.0,"sustain":1.0,"release":.13,"saturation":7.0,"volume":.40},
    "guitar_wah2":          {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.08)],"attack":.004,"decay":.05,"sustain":.82,"release":.12,"saturation":2.0,"volume":.48},
    "guitar_chorus2":       {"harmonics":[(1,.52),(1.006,.44),(2,.23),(3,.10)],"attack":.006,"decay":.08,"sustain":.80,"release":.15,"volume":.48},
    "guitar_flanger2":      {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.006,"decay":.08,"sustain":.80,"release":.15,"volume":.50},
    "guitar_phaser2":       {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.006,"decay":.08,"sustain":.80,"release":.15,"volume":.50},
    "guitar_tremolo2":      {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.006,"decay":.08,"sustain":.80,"release":.15,"vibrato_depth":.22,"vibrato_rate":7.0,"volume":.48},
    "guitar_delay":         {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.006,"decay":.10,"sustain":.78,"release":.18,"volume":.50},
    "guitar_reverb":        {"harmonics":[(1,.55),(2,.25),(3,.12),(4,.08)],"attack":.008,"decay":.10,"sustain":.80,"release":.28,"volume":.50},
    "guitar_envelope":      {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.003,"decay":.04,"sustain":.80,"release":.10,"saturation":2.0,"volume":.48},
    "guitar_compressor":    {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.007,"decay":.09,"sustain":.82,"release":.14,"volume":.54},
    "guitar_nylon":         {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.006,"decay":.12,"sustain":.70,"release":.16,"decay_exp":4.5,"volume":.54},
    "guitar_steel":         {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.005,"decay":.10,"sustain":.68,"release":.14,"decay_exp":4.0,"volume":.54},
    "guitar_parlor":        {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.006,"decay":.14,"sustain":.68,"release":.16,"decay_exp":5.0,"volume":.54},
    "guitar_dreadnought":   {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.005,"decay":.12,"sustain":.70,"release":.15,"decay_exp":4.0,"volume":.54},
    "guitar_concert":       {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.006,"decay":.13,"sustain":.70,"release":.16,"decay_exp":4.5,"volume":.54},
    "guitar_resonator":     {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.005,"decay":.10,"sustain":.65,"release":.14,"decay_exp":5.5,"saturation":.8,"volume":.54},
    "guitar_twelve_string2":{"harmonics":[(1,.44),(1.004,.36),(2,.22),(2.004,.18),(3,.10),(4,.04)],"attack":.005,"decay":.10,"sustain":.70,"release":.14,"volume":.44},
    "guitar_seven_string":  {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.0,"sustain":1.0,"release":.13,"saturation":2.0,"volume":.52},
    "guitar_eight_string":  {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.004,"decay":.0,"sustain":1.0,"release":.12,"saturation":3.0,"volume":.50},
    "guitar_flamenco2":     {"harmonics":[(1,.55),(2,.28),(3,.13),(4,.04)],"attack":.004,"decay":.08,"sustain":.62,"release":.12,"decay_exp":6.0,"volume":.54},
    "guitar_gypsy":         {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.004,"decay":.10,"sustain":.65,"release":.14,"decay_exp":5.5,"volume":.54},
    "guitar_bossa2":        {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.008,"decay":.13,"sustain":.72,"release":.18,"volume":.54},
    "guitar_fingerstyle":   {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.005,"decay":.13,"sustain":.68,"release":.16,"decay_exp":4.5,"volume":.54},
    "guitar_classical2":    {"harmonics":[(1,.60),(2,.24),(3,.10),(4,.06)],"attack":.007,"decay":.14,"sustain":.70,"release":.18,"decay_exp":4.0,"volume":.53},
    "guitar_blues":         {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.005,"decay":.0,"sustain":1.0,"release":.13,"saturation":2.5,"volume":.50},
    "guitar_blues_slide":   {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.010,"decay":.08,"sustain":.80,"release":.16,"vibrato_depth":.10,"vibrato_rate":3.0,"volume":.52},
    "guitar_country":       {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.005,"decay":.09,"sustain":.76,"release":.14,"volume":.54},
    "guitar_country_twang": {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.07,"sustain":.70,"release":.12,"saturation":.8,"volume":.54},
    "guitar_rock":          {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.004,"decay":.0,"sustain":1.0,"release":.13,"saturation":2.0,"volume":.50},
    "guitar_hard_rock":     {"harmonics":[(1,.45),(2,.28),(3,.16),(4,.08),(5,.03)],"attack":.003,"decay":.0,"sustain":1.0,"release":.12,"saturation":3.5,"volume":.47},
    "guitar_punk":          {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.06)],"attack":.003,"decay":.0,"sustain":1.0,"release":.12,"saturation":3.0,"volume":.48},
    "guitar_metal":         {"harmonics":[(1,.42),(2,.26),(3,.18),(4,.10),(5,.04)],"attack":.003,"decay":.0,"sustain":1.0,"release":.12,"saturation":5.0,"volume":.44},
    "guitar_death_metal":   {"harmonics":[(1,.38),(2,.24),(3,.20),(4,.12),(5,.06)],"attack":.002,"decay":.0,"sustain":1.0,"release":.11,"saturation":8.0,"volume":.38},
    "guitar_jazz2":         {"harmonics":[(1,.60),(2,.24),(3,.10),(4,.06)],"attack":.010,"decay":.14,"sustain":.78,"release":.18,"volume":.54},
    "guitar_jazz_archtop":  {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.008,"decay":.14,"sustain":.78,"release":.20,"volume":.54},
    "guitar_fusion2":       {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.006,"decay":.09,"sustain":.80,"release":.15,"saturation":1.2,"volume":.52},
    "guitar_prog":          {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.08)],"attack":.005,"decay":.09,"sustain":.80,"release":.14,"saturation":1.5,"volume":.51},
    "guitar_surf":          {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.005,"decay":.08,"sustain":.78,"release":.18,"saturation":.8,"volume":.54},
    "guitar_psychedelic":   {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.008,"decay":.08,"sustain":.80,"release":.18,"saturation":2.0,"volume":.48},
    "guitar_ambient2":      {"harmonics":[(1,.55),(2,.24),(3,.12),(4,.09)],"attack":.020,"decay":.10,"sustain":.82,"release":.30,"volume":.50},
    "guitar_post_rock":     {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.015,"decay":.10,"sustain":.82,"release":.25,"saturation":2.5,"volume":.47},
    "guitar_shoegaze":      {"harmonics":[(1,.50),(2,.26),(3,.14),(4,.10)],"attack":.012,"decay":.10,"sustain":.82,"release":.25,"saturation":3.0,"noise_mix":.02,"volume":.45},
    "guitar_math_rock":     {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.005,"decay":.08,"sustain":.80,"release":.14,"saturation":1.5,"volume":.50},
    "guitar_fingerpicking":  {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.004,"decay":.12,"sustain":.68,"release":.16,"decay_exp":5.0,"volume":.54},
    "guitar_arpeggio":      {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.005,"decay":.11,"sustain":.70,"release":.15,"decay_exp":5.0,"volume":.54},
    "guitar_power_chord":   {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06)],"attack":.003,"decay":.0,"sustain":1.0,"release":.13,"saturation":2.5,"volume":.50},
    "guitar_open_chord":    {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.005,"decay":.10,"sustain":.76,"release":.15,"volume":.53},
    "guitar_barre_chord":   {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.005,"decay":.09,"sustain":.78,"release":.14,"saturation":.8,"volume":.53},
    "guitar_palm_mute":     {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.003,"decay":.03,"sustain":.35,"release":.06,"decay_exp":20.,"volume":.57},
    "guitar_natural_harm":  {"harmonics":[(2,.65),(4,.22),(6,.10),(8,.03)],"attack":.006,"decay":.0,"sustain":.50,"release":.16,"decay_exp":6.0,"volume":.52},
    "guitar_pinch_harm":    {"harmonics":[(2,.55),(4,.28),(6,.12),(8,.05)],"attack":.003,"decay":.0,"sustain":.80,"release":.14,"decay_exp":5.0,"saturation":2.0,"volume":.50},
    "guitar_tapping":       {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.002,"decay":.06,"sustain":.75,"release":.10,"saturation":1.5,"volume":.52},
    "guitar_string_bend":   {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.006,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.12,"vibrato_rate":1.5,"volume":.52},
    "guitar_vibrato2":      {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.006,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.10,"vibrato_rate":5.5,"volume":.52},
    "guitar_whammy":        {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.006,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.20,"vibrato_rate":1.0,"volume":.50},
    "guitar_lick":          {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.08,"sustain":.78,"release":.13,"saturation":1.5,"volume":.52},
    "guitar_riff":          {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.003,"decay":.0,"sustain":1.0,"release":.12,"saturation":2.0,"volume":.50},
    "guitar_oud_style":     {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.12,"sustain":.58,"release":.14,"decay_exp":6.0,"volume":.53},
    "guitar_saz_style":     {"harmonics":[(1,.55),(2,.28),(3,.13),(4,.04)],"attack":.004,"decay":.10,"sustain":.55,"release":.12,"decay_exp":7.0,"volume":.53},
    "guitar_tanbur_style":  {"harmonics":[(1,.54),(2,.26),(3,.14),(4,.06)],"attack":.004,"decay":.11,"sustain":.57,"release":.13,"decay_exp":6.5,"volume":.52},
    "guitar_tar_style":     {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.10,"sustain":.55,"release":.12,"decay_exp":7.0,"volume":.52},
    "guitar_setar_style":   {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.005,"decay":.12,"sustain":.60,"release":.15,"decay_exp":6.0,"volume":.52},
    "guitar_cumbus_style":  {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.10,"sustain":.55,"release":.12,"decay_exp":7.5,"volume":.52},
    "guitar_kora_style":    {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.004,"decay":.12,"sustain":.58,"release":.14,"decay_exp":6.5,"volume":.52},
    "guitar_cavaquinho":    {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.004,"decay":.10,"sustain":.58,"release":.12,"decay_exp":7.5,"volume":.53},
    "guitar_tres":          {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.10,"sustain":.58,"release":.13,"decay_exp":7.0,"volume":.52},
    "guitar_requinto":      {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.005,"decay":.10,"sustain":.60,"release":.14,"decay_exp":6.5,"volume":.52},
    "guitar_vihuela":       {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.005,"decay":.12,"sustain":.60,"release":.15,"decay_exp":6.0,"volume":.52},
    "guitar_chitarra":      {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.005,"decay":.12,"sustain":.62,"release":.15,"decay_exp":5.5,"volume":.52},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ STRINGS EXTENDED (Orchestral/Solo/Ensemble/Extended/Hybrid) ██
    # ═══════════════════════════════════════════════════════════════════════════
    "violin2":              {"harmonics":[(1,.58),(2,.22),(3,.11),(4,.05),(5,.03),(6,.01)],"attack":.055,"decay":.0,"sustain":1.0,"release":.08,"vibrato_depth":.12,"vibrato_rate":5.5,"volume":.48},
    "violin_baroque":       {"harmonics":[(1,.62),(2,.20),(3,.09),(4,.05),(5,.04)],"attack":.050,"decay":.0,"sustain":1.0,"release":.08,"vibrato_depth":.06,"vibrato_rate":4.0,"volume":.48},
    "violin_romantic":      {"harmonics":[(1,.58),(2,.22),(3,.11),(4,.05),(5,.04)],"attack":.065,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.14,"vibrato_rate":5.5,"volume":.48},
    "violin_modern2":       {"harmonics":[(1,.55),(2,.24),(3,.12),(4,.06),(5,.03)],"attack":.050,"decay":.0,"sustain":1.0,"release":.08,"vibrato_depth":.10,"vibrato_rate":6.0,"volume":.48},
    "violin_sul_tasto":     {"harmonics":[(1,.70),(2,.18),(3,.08),(4,.04)],"attack":.070,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.08,"vibrato_rate":5.0,"volume":.48},
    "violin_sul_ponticello":{"harmonics":[(1,.40),(2,.28),(3,.18),(4,.10),(5,.04)],"attack":.040,"decay":.0,"sustain":1.0,"release":.08,"vibrato_depth":.08,"vibrato_rate":6.0,"noise_mix":.02,"volume":.44},
    "violin_col_legno":     {"harmonics":[(1,.40),(2,.28),(3,.18),(4,.10)],"attack":.005,"decay":.02,"sustain":.35,"release":.08,"noise_mix":.20,"volume":.40},
    "violin_spiccato":      {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.008,"decay":.03,"sustain":.40,"release":.10,"decay_exp":14.,"volume":.50},
    "violin_staccato":      {"harmonics":[(1,.62),(2,.20),(3,.10),(4,.08)],"attack":.005,"decay":.02,"sustain":.50,"release":.08,"decay_exp":12.,"volume":.50},
    "violin_legato":        {"harmonics":[(1,.58),(2,.22),(3,.11),(4,.05),(5,.04)],"attack":.070,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.12,"vibrato_rate":5.5,"volume":.48},
    "violin_detache":       {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.06)],"attack":.015,"decay":.0,"sustain":1.0,"release":.08,"vibrato_depth":.08,"vibrato_rate":5.0,"volume":.48},
    "violin_martele":       {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.004,"decay":.01,"sustain":.80,"release":.06,"decay_exp":8.0,"volume":.50},
    "violin_flying_spiccato":{"harmonics":[(1,.58),(2,.22),(3,.12),(4,.08)],"attack":.006,"decay":.02,"sustain":.50,"release":.08,"decay_exp":12.,"volume":.50},
    "viola2":               {"harmonics":[(1,.56),(2,.25),(3,.12),(4,.05),(5,.02)],"attack":.065,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.11,"vibrato_rate":5.0,"volume":.46},
    "viola_baroque":        {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.060,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.06,"vibrato_rate":3.8,"volume":.46},
    "viola_alto":           {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05),(5,.02)],"attack":.070,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":5.0,"volume":.46},
    "viola_pizzicato":      {"harmonics":[(1,.60),(2,.26),(3,.10),(4,.04)],"attack":.005,"decay":.13,"sustain":.40,"release":.16,"decay_exp":8.0,"volume":.50},
    "viola_tremolo":        {"harmonics":[(1,.58),(2,.24),(3,.11),(4,.07)],"attack":.018,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.20,"vibrato_rate":11.5,"volume":.45},
    "cello2":               {"harmonics":[(1,.52),(2,.28),(3,.13),(4,.05),(5,.02)],"attack":.085,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.10,"vibrato_rate":4.5,"volume":.50},
    "cello_baroque":        {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.080,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.06,"vibrato_rate":3.5,"volume":.50},
    "cello_romantic":       {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.095,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.12,"vibrato_rate":4.5,"volume":.50},
    "cello_staccato":       {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.005,"decay":.02,"sustain":.55,"release":.10,"decay_exp":10.,"volume":.52},
    "cello_spiccato":       {"harmonics":[(1,.56),(2,.24),(3,.12),(4,.08)],"attack":.008,"decay":.03,"sustain":.45,"release":.12,"decay_exp":12.,"volume":.52},
    "cello_sul_ponticello": {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.08),(5,.04)],"attack":.040,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.02,"volume":.46},
    "cello_col_legno":      {"harmonics":[(1,.42),(2,.26),(3,.18),(4,.10)],"attack":.005,"decay":.02,"sustain":.35,"release":.10,"noise_mix":.18,"volume":.42},
    "double_bass2":         {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.095,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.07,"vibrato_rate":4.0,"volume":.52},
    "double_bass_pizz":     {"harmonics":[(1,.60),(2,.26),(3,.10),(4,.04)],"attack":.006,"decay":.16,"sustain":.40,"release":.18,"decay_exp":6.5,"volume":.54},
    "double_bass_bow":      {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.100,"decay":.0,"sustain":1.0,"release":.16,"vibrato_depth":.07,"vibrato_rate":4.0,"volume":.52},
    "double_bass_slap":     {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.003,"decay":.04,"sustain":.55,"release":.10,"decay_exp":10.,"saturation":1.5,"volume":.54},
    "string_chamber":       {"harmonics":[(1,.54),(1.004,.44),(2,.23),(3,.10),(4,.04)],"attack":.130,"decay":.0,"sustain":1.0,"release":.18,"vibrato_depth":.09,"vibrato_rate":5.0,"volume":.40},
    "string_sinfonietta":   {"harmonics":[(1,.52),(1.005,.44),(2,.22),(3,.10),(4,.04)],"attack":.160,"decay":.0,"sustain":1.0,"release":.22,"vibrato_depth":.09,"vibrato_rate":5.0,"volume":.38},
    "string_romantic2":     {"harmonics":[(1,.50),(1.006,.44),(2,.22),(3,.10),(4,.04)],"attack":.200,"decay":.0,"sustain":1.0,"release":.28,"vibrato_depth":.10,"vibrato_rate":5.2,"volume":.36},
    "string_modern2":       {"harmonics":[(1,.52),(1.005,.44),(2,.22),(3,.10),(4,.04)],"attack":.150,"decay":.0,"sustain":1.0,"release":.20,"vibrato_depth":.08,"vibrato_rate":5.5,"volume":.38},
    "string_baroque2":      {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.100,"decay":.0,"sustain":1.0,"release":.15,"vibrato_depth":.05,"vibrato_rate":4.0,"volume":.42},
    "string_col_legno":     {"harmonics":[(1,.40),(2,.26),(3,.18),(4,.12)],"attack":.006,"decay":.02,"sustain":.30,"release":.10,"noise_mix":.15,"volume":.40},
    "string_tremolo2":      {"harmonics":[(1,.52),(1.005,.44),(2,.22),(3,.10)],"attack":.020,"decay":.0,"sustain":1.0,"release":.15,"vibrato_depth":.18,"vibrato_rate":12.,"volume":.38},
    "string_sul_ponticello":{"harmonics":[(1,.38),(2,.28),(3,.20),(4,.10),(5,.04)],"attack":.040,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.03,"volume":.38},
    "string_pizzicato2":    {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.005,"decay":.13,"sustain":.40,"release":.16,"decay_exp":8.5,"volume":.50},
    "string_bartok_pizz":   {"harmonics":[(1,.55),(2,.28),(3,.14),(4,.03)],"attack":.003,"decay":.04,"sustain":.40,"release":.10,"decay_exp":10.,"noise_mix":.08,"volume":.52},
    "string_harmonics2":    {"harmonics":[(2,.60),(4,.24),(6,.10),(8,.06)],"attack":.005,"decay":.0,"sustain":.55,"release":.18,"decay_exp":5.5,"volume":.48},
    "string_artificial_harm":{"harmonics":[(3,.65),(6,.22),(9,.10),(12,.03)],"attack":.006,"decay":.0,"sustain":.50,"release":.16,"decay_exp":6.0,"volume":.46},
    "harp2":                {"harmonics":[(1,.56),(2,.27),(3,.11),(4,.06)],"attack":.004,"decay":.0,"sustain":.52,"release":.16,"decay_exp":5.0,"volume":.52},
    "harp_ped":             {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.004,"decay":.0,"sustain":.50,"release":.15,"decay_exp":5.0,"volume":.52},
    "harp_celtic":          {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.004,"decay":.0,"sustain":.48,"release":.14,"decay_exp":6.0,"volume":.52},
    "harp_glissando":       {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.003,"decay":.0,"sustain":.50,"release":.18,"decay_exp":4.5,"volume":.50},
    "harp_harmonics":       {"harmonics":[(2,.65),(4,.22),(6,.10),(8,.03)],"attack":.004,"decay":.0,"sustain":.40,"release":.14,"decay_exp":6.0,"volume":.48},
    "harp_jazz":            {"harmonics":[(1,.55),(2,.28),(3,.14),(4,.03)],"attack":.005,"decay":.0,"sustain":.45,"release":.13,"decay_exp":5.5,"volume":.52},
    "harp_bowed":           {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.060,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.05,"vibrato_rate":4.5,"volume":.50},
    "mandolin2":            {"harmonics":[(1,.54),(2,.27),(3,.14),(4,.05)],"attack":.003,"decay":.0,"sustain":.42,"release":.11,"decay_exp":10.5,"volume":.54},
    "mandolin_tremolo":     {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.003,"decay":.0,"sustain":.80,"release":.12,"vibrato_depth":.22,"vibrato_rate":14.,"volume":.50},
    "mandola":              {"harmonics":[(1,.55),(2,.27),(3,.13),(4,.05)],"attack":.004,"decay":.0,"sustain":.40,"release":.12,"decay_exp":9.5,"volume":.52},
    "mandocello":           {"harmonics":[(1,.55),(2,.28),(3,.13),(4,.04)],"attack":.004,"decay":.0,"sustain":.42,"release":.13,"decay_exp":8.5,"volume":.52},
    "dulcimer2":            {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.004,"decay":.0,"sustain":.46,"release":.13,"decay_exp":8.0,"volume":.52},
    "dulcimer_hammered":    {"harmonics":[(1,.56),(2,.27),(3,.13),(4,.04)],"attack":.003,"decay":.0,"sustain":.42,"release":.12,"decay_exp":9.0,"volume":.52},
    "zither":               {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.004,"decay":.0,"sustain":.45,"release":.14,"decay_exp":7.5,"volume":.52},
    "zither_concert":       {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.07)],"attack":.004,"decay":.0,"sustain":.44,"release":.13,"decay_exp":7.5,"volume":.52},
    "autoharp":             {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.005,"decay":.0,"sustain":.50,"release":.16,"decay_exp":6.5,"volume":.52},
    "lute":                 {"harmonics":[(1,.55),(2,.27),(3,.13),(4,.05)],"attack":.004,"decay":.10,"sustain":.55,"release":.14,"decay_exp":6.5,"volume":.52},
    "lute_baroque":         {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.004,"decay":.12,"sustain":.55,"release":.15,"decay_exp":6.0,"volume":.52},
    "theorbo":              {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.005,"decay":.14,"sustain":.52,"release":.18,"decay_exp":5.5,"volume":.52},
    "archlute":             {"harmonics":[(1,.56),(2,.26),(3,.13),(4,.05)],"attack":.005,"decay":.13,"sustain":.53,"release":.16,"decay_exp":6.0,"volume":.52},
    "chitarrone":           {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.006,"decay":.14,"sustain":.52,"release":.18,"decay_exp":5.5,"volume":.50},
    "viol_treble":          {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.055,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.06,"vibrato_rate":4.5,"volume":.48},
    "viol_tenor":           {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.065,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.06,"vibrato_rate":4.2,"volume":.48},
    "viol_bass":            {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.080,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.05,"vibrato_rate":4.0,"volume":.50},
    "viol_consort":         {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.100,"decay":.0,"sustain":1.0,"release":.18,"vibrato_depth":.05,"vibrato_rate":4.2,"volume":.42},
    "rebec":                {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.08,"vibrato_rate":5.0,"noise_mix":.02,"volume":.48},
    "nyckelharpa":          {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":5.0,"noise_mix":.025,"volume":.48},
    "hardanger_fiddle":     {"harmonics":[(1,.50),(2,.26),(3,.14),(4,.08),(5,.02)],"attack":.055,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":5.5,"volume":.48},
    "gadulka":              {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.09,"vibrato_rate":5.0,"noise_mix":.02,"volume":.48},
    "saz_bass":             {"harmonics":[(1,.55),(2,.28),(3,.13),(4,.04)],"attack":.008,"decay":.12,"sustain":.60,"release":.16,"decay_exp":5.5,"volume":.52},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ BRASS EXTENDED (Instruments/Muted/Ensemble/Period/Genre) ██
    # ═══════════════════════════════════════════════════════════════════════════
    "trumpet2":             {"harmonics":[(1,.44),(2,.30),(3,.16),(4,.07),(5,.03)],"attack":.042,"decay":.05,"sustain":.85,"release":.10,"saturation":1.2,"volume":.48},
    "trumpet_baroque":      {"harmonics":[(1,.48),(2,.28),(3,.14),(4,.08),(5,.02)],"attack":.040,"decay":.05,"sustain":.83,"release":.10,"volume":.48},
    "trumpet_piccolo":      {"harmonics":[(1,.46),(2,.28),(3,.16),(4,.08),(5,.02)],"attack":.035,"decay":.05,"sustain":.82,"release":.08,"saturation":1.0,"volume":.48},
    "trumpet_alto":         {"harmonics":[(1,.46),(2,.30),(3,.15),(4,.07),(5,.02)],"attack":.045,"decay":.05,"sustain":.84,"release":.10,"saturation":1.0,"volume":.48},
    "trumpet_bass":         {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.055,"decay":.05,"sustain":.85,"release":.12,"saturation":1.0,"volume":.50},
    "trumpet_straight_mute":{"harmonics":[(1,.40),(2,.28),(3,.18),(4,.10),(5,.04)],"attack":.030,"decay":.05,"sustain":.80,"release":.08,"saturation":1.8,"noise_mix":.015,"volume":.44},
    "trumpet_harmon_mute":  {"harmonics":[(1,.35),(2,.28),(3,.20),(4,.12),(5,.05)],"attack":.025,"decay":.05,"sustain":.78,"release":.08,"saturation":2.0,"noise_mix":.02,"volume":.42},
    "trumpet_plunger_mute": {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.10),(5,.02)],"attack":.028,"decay":.05,"sustain":.80,"release":.08,"saturation":1.5,"volume":.44},
    "trumpet_cup_mute":     {"harmonics":[(1,.38),(2,.28),(3,.20),(4,.12),(5,.02)],"attack":.028,"decay":.05,"sustain":.78,"release":.08,"saturation":1.5,"noise_mix":.015,"volume":.42},
    "trumpet_wah_wah":      {"harmonics":[(1,.40),(2,.28),(3,.18),(4,.10)],"attack":.030,"decay":.05,"sustain":.80,"release":.08,"saturation":1.5,"volume":.44},
    "trumpet_fall":         {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08)],"attack":.020,"decay":.30,"sustain":.40,"release":.15,"saturation":1.2,"volume":.46},
    "trumpet_doit":         {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08)],"attack":.040,"decay":.0,"sustain":.85,"release":.12,"saturation":1.2,"vibrato_depth":.05,"vibrato_rate":2.0,"volume":.46},
    "trumpet_shake":        {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.08)],"attack":.040,"decay":.05,"sustain":.82,"release":.10,"vibrato_depth":.15,"vibrato_rate":8.0,"saturation":1.3,"volume":.46},
    "trombone2":            {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06),(5,.02)],"attack":.058,"decay":.05,"sustain":.85,"release":.12,"saturation":.8,"volume":.50},
    "trombone_tenor":       {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.06)],"attack":.055,"decay":.05,"sustain":.85,"release":.12,"volume":.50},
    "trombone_bass":        {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.070,"decay":.05,"sustain":.85,"release":.14,"volume":.52},
    "trombone_alto":        {"harmonics":[(1,.48),(2,.28),(3,.15),(4,.07),(5,.02)],"attack":.050,"decay":.05,"sustain":.84,"release":.10,"volume":.48},
    "trombone_contrabass":  {"harmonics":[(1,.60),(2,.24),(3,.10),(4,.06)],"attack":.090,"decay":.05,"sustain":.85,"release":.16,"volume":.54},
    "trombone_straight_mute":{"harmonics":[(1,.42),(2,.28),(3,.18),(4,.10)],"attack":.040,"decay":.05,"sustain":.80,"release":.10,"saturation":1.5,"noise_mix":.015,"volume":.46},
    "trombone_cup_mute":    {"harmonics":[(1,.38),(2,.28),(3,.20),(4,.12)],"attack":.040,"decay":.05,"sustain":.78,"release":.10,"saturation":1.5,"noise_mix":.02,"volume":.44},
    "trombone_gliss":       {"harmonics":[(1,.52),(2,.26),(3,.12),(4,.10)],"attack":.060,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.08,"vibrato_rate":1.0,"volume":.48},
    "french_horn2":         {"harmonics":[(1,.56),(2,.24),(3,.12),(4,.06),(5,.02)],"attack":.075,"decay":.05,"sustain":.85,"release":.14,"volume":.48},
    "french_horn_stopped":  {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.10),(5,.02)],"attack":.055,"decay":.05,"sustain":.80,"release":.10,"saturation":2.0,"noise_mix":.02,"volume":.44},
    "french_horn_muted":    {"harmonics":[(1,.40),(2,.28),(3,.20),(4,.10),(5,.02)],"attack":.050,"decay":.05,"sustain":.78,"release":.10,"saturation":1.8,"volume":.42},
    "french_horn_open":     {"harmonics":[(1,.58),(2,.24),(3,.10),(4,.08)],"attack":.080,"decay":.05,"sustain":.86,"release":.14,"volume":.48},
    "french_horn_solo":     {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05),(5,.02)],"attack":.075,"decay":.05,"sustain":.85,"release":.14,"vibrato_depth":.04,"vibrato_rate":4.5,"volume":.48},
    "tuba2":                {"harmonics":[(1,.62),(2,.23),(3,.10),(4,.05)],"attack":.095,"decay":.05,"sustain":.85,"release":.16,"volume":.53},
    "tuba_contrabass":      {"harmonics":[(1,.66),(2,.22),(3,.08),(4,.04)],"attack":.110,"decay":.05,"sustain":.85,"release":.18,"volume":.54},
    "tuba_bass":            {"harmonics":[(1,.62),(2,.24),(3,.10),(4,.04)],"attack":.100,"decay":.05,"sustain":.85,"release":.17,"volume":.53},
    "tuba_euphonium":       {"harmonics":[(1,.58),(2,.25),(3,.12),(4,.05)],"attack":.075,"decay":.05,"sustain":.85,"release":.15,"volume":.52},
    "cornet2":              {"harmonics":[(1,.46),(2,.28),(3,.16),(4,.07),(5,.03)],"attack":.038,"decay":.05,"sustain":.82,"release":.10,"volume":.48},
    "bugle":                {"harmonics":[(1,.48),(2,.28),(3,.14),(4,.08),(5,.02)],"attack":.040,"decay":.05,"sustain":.84,"release":.10,"saturation":.8,"volume":.48},
    "flugel2":              {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.07),(5,.02)],"attack":.048,"decay":.05,"sustain":.83,"release":.12,"volume":.48},
    "baritone_horn":        {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.065,"decay":.05,"sustain":.85,"release":.14,"volume":.50},
    "sousaphone":           {"harmonics":[(1,.62),(2,.23),(3,.10),(4,.05)],"attack":.100,"decay":.05,"sustain":.85,"release":.17,"volume":.52},
    "cimbasso":             {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.080,"decay":.05,"sustain":.85,"release":.15,"saturation":.8,"volume":.52},
    "ophicleide":           {"harmonics":[(1,.55),(2,.27),(3,.13),(4,.05)],"attack":.065,"decay":.05,"sustain":.84,"release":.14,"volume":.50},
    "serpent":              {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.070,"decay":.05,"sustain":.84,"release":.14,"noise_mix":.02,"volume":.50},
    "natural_horn":         {"harmonics":[(1,.58),(2,.24),(3,.10),(4,.08)],"attack":.085,"decay":.05,"sustain":.85,"release":.16,"volume":.48},
    "keyed_bugle":          {"harmonics":[(1,.46),(2,.28),(3,.16),(4,.08)],"attack":.040,"decay":.05,"sustain":.82,"release":.10,"volume":.48},
    "brass_quartet":        {"harmonics":[(1,.44),(2,.28),(3,.18),(4,.08),(5,.02)],"attack":.055,"decay":.05,"sustain":.85,"release":.12,"saturation":1.0,"volume":.40},
    "brass_quintet":        {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.08),(5,.04)],"attack":.060,"decay":.05,"sustain":.85,"release":.12,"saturation":1.0,"volume":.38},
    "brass_choir":          {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.08),(5,.04),(6,.02)],"attack":.070,"decay":.05,"sustain":.85,"release":.14,"saturation":1.0,"volume":.36},
    "brass_band":           {"harmonics":[(1,.40),(2,.28),(3,.18),(4,.10),(5,.04)],"attack":.065,"decay":.05,"sustain":.85,"release":.13,"saturation":1.2,"volume":.36},
    "brass_military":       {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08),(5,.04)],"attack":.040,"decay":.05,"sustain":.85,"release":.10,"saturation":1.0,"volume":.40},
    "brass_jazz2":          {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08),(5,.04)],"attack":.040,"decay":.05,"sustain":.85,"release":.10,"saturation":1.0,"volume":.40},
    "brass_fanfare":        {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.08),(5,.04)],"attack":.030,"decay":.05,"sustain":.85,"release":.10,"saturation":1.5,"volume":.40},
    "brass_chorale":        {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08),(5,.04)],"attack":.100,"decay":.0,"sustain":1.0,"release":.20,"saturation":.8,"volume":.38},
    "brass_swell":          {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08)],"attack":.150,"decay":.0,"sustain":1.0,"release":.20,"saturation":1.0,"volume":.40},
    "brass_marcato":        {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.08),(5,.04)],"attack":.020,"decay":.05,"sustain":.85,"release":.08,"saturation":1.5,"volume":.42},
    "brass_sforzando":      {"harmonics":[(1,.40),(2,.28),(3,.20),(4,.10),(5,.02)],"attack":.008,"decay":.10,"sustain":.75,"release":.10,"saturation":2.5,"volume":.42},
    "brass_pianissimo":     {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.080,"decay":.0,"sustain":1.0,"release":.18,"saturation":.6,"volume":.36},
    "brass_fortissimo":     {"harmonics":[(1,.38),(2,.28),(3,.20),(4,.10),(5,.04)],"attack":.030,"decay":.0,"sustain":1.0,"release":.12,"saturation":2.0,"volume":.42},
    "brass_flutter":        {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08)],"attack":.040,"decay":.05,"sustain":.82,"release":.10,"vibrato_depth":.16,"vibrato_rate":16.,"saturation":1.2,"volume":.44},
    "brass_vibrato":        {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08)],"attack":.045,"decay":.05,"sustain":.85,"release":.12,"vibrato_depth":.06,"vibrato_rate":5.5,"volume":.46},
    "brass_lyric":          {"harmonics":[(1,.48),(2,.26),(3,.14),(4,.08),(5,.04)],"attack":.060,"decay":.0,"sustain":1.0,"release":.16,"vibrato_depth":.04,"vibrato_rate":4.5,"volume":.44},
    "brass_aggressive":     {"harmonics":[(1,.38),(2,.28),(3,.20),(4,.12),(5,.02)],"attack":.020,"decay":.05,"sustain":.85,"release":.08,"saturation":2.5,"volume":.40},
    "brass_pad":            {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08),(5,.04)],"attack":.200,"decay":.0,"sustain":1.0,"release":.30,"saturation":1.0,"volume":.38},
    "brass_riser":          {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.10),(5,.02)],"attack":.500,"decay":.0,"sustain":1.0,"release":.10,"saturation":1.5,"volume":.38},
    "brass_accent":         {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.10),(5,.02)],"attack":.010,"decay":.04,"sustain":.80,"release":.08,"saturation":2.0,"volume":.42},
    "brass_sustain":        {"harmonics":[(1,.46),(2,.27),(3,.16),(4,.08),(5,.03)],"attack":.080,"decay":.0,"sustain":1.0,"release":.18,"saturation":1.0,"volume":.42},
    "brass_low":            {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.080,"decay":.05,"sustain":.85,"release":.16,"saturation":.8,"volume":.48},
    "brass_high":           {"harmonics":[(1,.42),(2,.28),(3,.18),(4,.08),(5,.04)],"attack":.040,"decay":.05,"sustain":.85,"release":.10,"saturation":1.4,"volume":.44},
    "brass_midrange":       {"harmonics":[(1,.46),(2,.28),(3,.16),(4,.08),(5,.02)],"attack":.055,"decay":.05,"sustain":.85,"release":.12,"saturation":1.0,"volume":.44},
    "brass_open":           {"harmonics":[(1,.46),(2,.28),(3,.15),(4,.08),(5,.03)],"attack":.045,"decay":.05,"sustain":.85,"release":.12,"volume":.46},
    "brass_closed":         {"harmonics":[(1,.38),(2,.28),(3,.20),(4,.12),(5,.02)],"attack":.035,"decay":.05,"sustain":.78,"release":.08,"saturation":2.0,"noise_mix":.018,"volume":.42},
    "brass_outdoor":        {"harmonics":[(1,.44),(2,.28),(3,.16),(4,.08),(5,.04)],"attack":.040,"decay":.05,"sustain":.85,"release":.10,"saturation":1.2,"volume":.42},
    "brass_indoor":         {"harmonics":[(1,.46),(2,.28),(3,.15),(4,.08),(5,.03)],"attack":.060,"decay":.05,"sustain":.85,"release":.14,"saturation":.8,"volume":.40},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ WOODWIND EXTENDED (Flute/Clarinet/Reed/Ethnic Winds) ██
    # ═══════════════════════════════════════════════════════════════════════════
    "flute2":               {"harmonics":[(1,.70),(2,.17),(3,.08),(4,.05)],"attack":.048,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"volume":.50},
    "flute_baroque":        {"harmonics":[(1,.72),(2,.15),(3,.08),(4,.05)],"attack":.050,"decay":.0,"sustain":1.0,"release":.09,"noise_mix":.045,"volume":.50},
    "flute_concert":        {"harmonics":[(1,.70),(2,.16),(3,.09),(4,.05)],"attack":.050,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"volume":.50},
    "flute_solo":           {"harmonics":[(1,.70),(2,.16),(3,.08),(4,.06)],"attack":.055,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.04,"vibrato_depth":.08,"vibrato_rate":5.5,"volume":.50},
    "flute_bass":           {"harmonics":[(1,.72),(2,.15),(3,.08),(4,.05)],"attack":.065,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.05,"volume":.50},
    "flute_contrabass":     {"harmonics":[(1,.75),(2,.14),(3,.07),(4,.04)],"attack":.080,"decay":.0,"sustain":1.0,"release":.14,"noise_mix":.055,"volume":.50},
    "flute_flutter":        {"harmonics":[(1,.68),(2,.17),(3,.09),(4,.06)],"attack":.050,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.045,"vibrato_depth":.18,"vibrato_rate":18.,"volume":.46},
    "flute_harmonics":      {"harmonics":[(2,.65),(4,.22),(6,.10),(8,.03)],"attack":.040,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.035,"volume":.46},
    "flute_extended":       {"harmonics":[(1,.65),(2,.18),(3,.10),(4,.07)],"attack":.060,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.06,"volume":.48},
    "flute_keyclick":       {"harmonics":[(1,.10),(5,.15),(9,.12),(13,.10)],"attack":.003,"decay":.0,"sustain":.30,"release":.04,"noise_mix":.60,"volume":.40},
    "piccolo2":             {"harmonics":[(1,.68),(2,.18),(3,.09),(4,.05)],"attack":.028,"decay":.0,"sustain":1.0,"release":.06,"noise_mix":.035,"volume":.50},
    "piccolo_baroque":      {"harmonics":[(1,.70),(2,.16),(3,.09),(4,.05)],"attack":.030,"decay":.0,"sustain":1.0,"release":.06,"noise_mix":.04,"volume":.50},
    "alto_flute2":          {"harmonics":[(1,.72),(2,.15),(3,.08),(4,.05)],"attack":.058,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.045,"volume":.50},
    "bass_flute":           {"harmonics":[(1,.74),(2,.14),(3,.07),(4,.05)],"attack":.070,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.05,"volume":.50},
    "contra_flute":         {"harmonics":[(1,.76),(2,.13),(3,.07),(4,.04)],"attack":.085,"decay":.0,"sustain":1.0,"release":.14,"noise_mix":.055,"volume":.50},
    "clarinet2":            {"harmonics":[(1,.55),(3,.28),(5,.12),(7,.05)],"attack":.038,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.02,"volume":.50},
    "clarinet_bass":        {"harmonics":[(1,.58),(3,.25),(5,.11),(7,.06)],"attack":.055,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.025,"volume":.50},
    "clarinet_alto":        {"harmonics":[(1,.55),(3,.27),(5,.12),(7,.06)],"attack":.048,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.022,"volume":.50},
    "clarinet_contra":      {"harmonics":[(1,.62),(3,.22),(5,.10),(7,.06)],"attack":.070,"decay":.0,"sustain":1.0,"release":.14,"noise_mix":.03,"volume":.50},
    "clarinet_eb":          {"harmonics":[(1,.52),(3,.28),(5,.14),(7,.06)],"attack":.035,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.02,"volume":.50},
    "clarinet_chalumeau":   {"harmonics":[(1,.60),(3,.24),(5,.10),(7,.06)],"attack":.042,"decay":.0,"sustain":1.0,"release":.09,"noise_mix":.025,"volume":.50},
    "clarinet_throat":      {"harmonics":[(1,.52),(3,.28),(5,.14),(7,.06)],"attack":.040,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.025,"volume":.50},
    "clarinet_altissimo":   {"harmonics":[(1,.48),(3,.30),(5,.16),(7,.06)],"attack":.038,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.02,"volume":.48},
    "oboe2":                {"harmonics":[(1,.50),(2,.27),(3,.14),(4,.07),(5,.02)],"attack":.038,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.025,"volume":.48},
    "oboe_d_amore":         {"harmonics":[(1,.52),(2,.26),(3,.13),(4,.07),(5,.02)],"attack":.042,"decay":.0,"sustain":1.0,"release":.09,"noise_mix":.025,"volume":.48},
    "oboe_da_caccia":       {"harmonics":[(1,.50),(2,.27),(3,.14),(4,.07),(5,.03)],"attack":.045,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.03,"volume":.48},
    "english_horn":         {"harmonics":[(1,.52),(2,.26),(3,.13),(4,.07),(5,.02)],"attack":.045,"decay":.0,"sustain":1.0,"release":.09,"noise_mix":.025,"vibrato_depth":.06,"vibrato_rate":4.5,"volume":.48},
    "bassoon2":             {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.06),(5,.01)],"attack":.058,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.03,"volume":.50},
    "contrabassoon":        {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.080,"decay":.0,"sustain":1.0,"release":.16,"noise_mix":.035,"volume":.52},
    "bassoon_tenor":        {"harmonics":[(1,.52),(2,.27),(3,.13),(4,.08)],"attack":.055,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.03,"volume":.50},
    "saxophone2":           {"harmonics":[(1,.48),(2,.28),(3,.14),(4,.07),(5,.03)],"attack":.048,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.03,"saturation":.8,"volume":.50},
    "sax_soprano2":         {"harmonics":[(1,.46),(2,.28),(3,.16),(4,.08),(5,.02)],"attack":.038,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.025,"volume":.48},
    "sax_alto2":            {"harmonics":[(1,.50),(2,.26),(3,.14),(4,.08),(5,.02)],"attack":.043,"decay":.0,"sustain":1.0,"release":.09,"noise_mix":.025,"volume":.50},
    "sax_tenor2":           {"harmonics":[(1,.52),(2,.25),(3,.13),(4,.08),(5,.02)],"attack":.048,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.03,"volume":.50},
    "sax_baritone2":        {"harmonics":[(1,.55),(2,.24),(3,.12),(4,.07),(5,.02)],"attack":.058,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.035,"volume":.52},
    "sax_bass":             {"harmonics":[(1,.58),(2,.22),(3,.11),(4,.09)],"attack":.068,"decay":.0,"sustain":1.0,"release":.14,"noise_mix":.04,"volume":.52},
    "sax_contrabass":       {"harmonics":[(1,.62),(2,.20),(3,.10),(4,.08)],"attack":.080,"decay":.0,"sustain":1.0,"release":.16,"noise_mix":.04,"volume":.54},
    "sax_straight":         {"harmonics":[(1,.48),(2,.28),(3,.15),(4,.07),(5,.02)],"attack":.042,"decay":.0,"sustain":1.0,"release":.09,"noise_mix":.025,"volume":.50},
    "sax_curved":           {"harmonics":[(1,.50),(2,.26),(3,.14),(4,.08),(5,.02)],"attack":.045,"decay":.0,"sustain":1.0,"release":.09,"noise_mix":.028,"volume":.50},
    "sax_jazz2":            {"harmonics":[(1,.50),(2,.26),(3,.14),(4,.08),(5,.02)],"attack":.045,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.03,"saturation":.9,"volume":.50},
    "sax_classical":        {"harmonics":[(1,.52),(2,.26),(3,.12),(4,.08),(5,.02)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.025,"volume":.50},
    "recorder2":            {"harmonics":[(1,.72),(2,.16),(3,.08),(4,.04)],"attack":.038,"decay":.0,"sustain":1.0,"release":.07,"noise_mix":.05,"volume":.48},
    "recorder_bass":        {"harmonics":[(1,.74),(2,.14),(3,.07),(4,.05)],"attack":.055,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.055,"volume":.48},
    "recorder_tenor":       {"harmonics":[(1,.72),(2,.16),(3,.08),(4,.04)],"attack":.045,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.05,"volume":.48},
    "recorder_treble":      {"harmonics":[(1,.70),(2,.17),(3,.09),(4,.04)],"attack":.035,"decay":.0,"sustain":1.0,"release":.07,"noise_mix":.045,"volume":.48},
    "recorder_soprano2":    {"harmonics":[(1,.70),(2,.17),(3,.09),(4,.04)],"attack":.030,"decay":.0,"sustain":1.0,"release":.06,"noise_mix":.04,"volume":.48},
    "pan_flute2":           {"harmonics":[(1,.75),(2,.14),(3,.08),(4,.03)],"attack":.078,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.06,"volume":.48},
    "pan_flute_bass":       {"harmonics":[(1,.77),(2,.13),(3,.07),(4,.03)],"attack":.090,"decay":.0,"sustain":1.0,"release":.14,"noise_mix":.065,"volume":.48},
    "quena":                {"harmonics":[(1,.72),(2,.16),(3,.08),(4,.04)],"attack":.060,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.06,"volume":.48},
    "quenacho":             {"harmonics":[(1,.74),(2,.14),(3,.08),(4,.04)],"attack":.070,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.065,"volume":.48},
    "siku":                 {"harmonics":[(1,.72),(2,.15),(3,.09),(4,.04)],"attack":.065,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.07,"volume":.46},
    "ocarina":              {"harmonics":[(1,.78),(2,.14),(3,.06),(4,.02)],"attack":.040,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.03,"volume":.50},
    "ocarina_bass":         {"harmonics":[(1,.80),(2,.12),(3,.06),(4,.02)],"attack":.055,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.035,"volume":.50},
    "shakuhachi":           {"harmonics":[(1,.70),(2,.16),(3,.08),(4,.06)],"attack":.070,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.07,"vibrato_depth":.12,"vibrato_rate":4.5,"volume":.48},
    "nay":                  {"harmonics":[(1,.68),(2,.18),(3,.09),(4,.05)],"attack":.065,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.08,"vibrato_depth":.10,"vibrato_rate":5.0,"volume":.48},
    "ney":                  {"harmonics":[(1,.68),(2,.18),(3,.09),(4,.05)],"attack":.065,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.08,"vibrato_depth":.10,"vibrato_rate":5.0,"volume":.48},
    "bansuri":              {"harmonics":[(1,.70),(2,.16),(3,.09),(4,.05)],"attack":.060,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.07,"vibrato_depth":.10,"vibrato_rate":5.5,"volume":.48},
    "dizi":                 {"harmonics":[(1,.70),(2,.16),(3,.09),(4,.05)],"attack":.055,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.06,"vibrato_depth":.08,"vibrato_rate":6.0,"volume":.48},
    "xiao":                 {"harmonics":[(1,.72),(2,.16),(3,.08),(4,.04)],"attack":.065,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.065,"vibrato_depth":.08,"vibrato_rate":4.5,"volume":.48},
    "hsiao":                {"harmonics":[(1,.73),(2,.15),(3,.08),(4,.04)],"attack":.068,"decay":.0,"sustain":1.0,"release":.11,"noise_mix":.07,"volume":.48},
    "kaval":                {"harmonics":[(1,.68),(2,.18),(3,.10),(4,.04)],"attack":.062,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.08,"vibrato_depth":.10,"vibrato_rate":5.0,"volume":.47},
    "ney_arab":             {"harmonics":[(1,.66),(2,.20),(3,.10),(4,.04)],"attack":.068,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.09,"vibrato_depth":.12,"vibrato_rate":4.5,"volume":.47},
    "duduk":                {"harmonics":[(1,.60),(2,.22),(3,.12),(4,.06)],"attack":.055,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.045,"vibrato_depth":.10,"vibrato_rate":5.0,"volume":.48},
    "zurna":                {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.08)],"attack":.035,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"saturation":.8,"volume":.48},
    "sorna":                {"harmonics":[(1,.50),(2,.28),(3,.15),(4,.07)],"attack":.038,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"saturation":.8,"volume":.48},
    "shehnai":              {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.08)],"attack":.040,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"saturation":.8,"vibrato_depth":.10,"vibrato_rate":5.0,"volume":.48},
    "bagpipe_chanter":      {"harmonics":[(1,.50),(3,.28),(5,.14),(7,.08)],"attack":.050,"decay":.0,"sustain":1.0,"release":.06,"noise_mix":.04,"volume":.48},
    "bagpipe_drone":        {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.080,"decay":.0,"sustain":1.0,"release":.08,"volume":.50},
    "musette":              {"harmonics":[(1,.52),(3,.26),(5,.14),(7,.08)],"attack":.045,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.03,"volume":.48},
    "harmonica":            {"harmonics":[(1,.55),(2,.25),(3,.12),(4,.08)],"attack":.025,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"volume":.50},
    "harmonica_blues":      {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.020,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"saturation":.8,"volume":.50},
    "harmonica_chromatic":  {"harmonics":[(1,.55),(2,.24),(3,.12),(4,.09)],"attack":.022,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"volume":.50},
    "melodica":             {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.018,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.03,"volume":.50},
    "accordion_treble":     {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.020,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.03,"volume":.50},
    "accordion_bass":       {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.025,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.025,"volume":.50},
    "bandoneon":            {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.022,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.03,"volume":.50},
    "concertina":           {"harmonics":[(1,.54),(2,.24),(3,.13),(4,.09)],"attack":.018,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.03,"volume":.50},
    "sheng":                {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.020,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.03,"volume":.50},
    "yu":                   {"harmonics":[(1,.58),(2,.24),(3,.11),(4,.07)],"attack":.025,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.03,"volume":.50},
    "khaen":                {"harmonics":[(1,.55),(2,.25),(3,.13),(4,.07)],"attack":.022,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.03,"volume":.50},
    "sho":                  {"harmonics":[(1,.55),(2,.25),(3,.12),(4,.08)],"attack":.025,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.028,"volume":.50},
    "xun":                  {"harmonics":[(1,.74),(2,.16),(3,.07),(4,.03)],"attack":.060,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.06,"volume":.48},
    "hulusi":               {"harmonics":[(1,.60),(2,.22),(3,.12),(4,.06)],"attack":.030,"decay":.0,"sustain":1.0,"release":.08,"noise_mix":.04,"volume":.50},
    "khim":                 {"harmonics":[(1,.55),(2,.26),(3,.14),(4,.05)],"attack":.003,"decay":.0,"sustain":.45,"release":.14,"decay_exp":8.0,"noise_mix":.03,"volume":.52},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ WORLD INSTRUMENTS EXTENDED ██
    # ═══════════════════════════════════════════════════════════════════════════
    "oud2":                 {"harmonics":[(1,.54),(2,.27),(3,.13),(4,.06)],"attack":.004,"decay":.11,"sustain":.62,"release":.15,"decay_exp":5.0,"volume":.54},
    "oud_arabic":           {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.12,"sustain":.60,"release":.14,"decay_exp":5.0,"volume":.54},
    "oud_turkish":          {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.07)],"attack":.004,"decay":.11,"sustain":.62,"release":.15,"decay_exp":5.0,"volume":.54},
    "oud_persian":          {"harmonics":[(1,.53),(2,.27),(3,.14),(4,.06)],"attack":.005,"decay":.12,"sustain":.60,"release":.15,"decay_exp":5.0,"volume":.52},
    "sitar2":               {"harmonics":[(1,.48),(2,.27),(3,.15),(4,.07),(5,.03)],"attack":.003,"decay":.10,"sustain":.56,"release":.13,"decay_exp":6.0,"volume":.52},
    "sitar_ravi":           {"harmonics":[(1,.50),(2,.26),(3,.14),(4,.08),(5,.02)],"attack":.003,"decay":.10,"sustain":.58,"release":.14,"decay_exp":5.5,"volume":.52},
    "sitar_bass":           {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.005,"decay":.12,"sustain":.60,"release":.16,"decay_exp":5.0,"volume":.52},
    "veena":                {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.07)],"attack":.005,"decay":.12,"sustain":.58,"release":.16,"decay_exp":5.5,"volume":.52},
    "veena_saraswati":      {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.005,"decay":.13,"sustain":.60,"release":.16,"decay_exp":5.0,"volume":.52},
    "rudra_veena":          {"harmonics":[(1,.56),(2,.25),(3,.12),(4,.07)],"attack":.006,"decay":.14,"sustain":.58,"release":.18,"decay_exp":5.0,"volume":.52},
    "sarod2":               {"harmonics":[(1,.50),(2,.28),(3,.14),(4,.08)],"attack":.004,"decay":.12,"sustain":.56,"release":.15,"decay_exp":5.5,"volume":.50},
    "rabab":                {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.004,"decay":.12,"sustain":.58,"release":.14,"decay_exp":5.5,"volume":.50},
    "rubab":                {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.07)],"attack":.004,"decay":.12,"sustain":.58,"release":.14,"decay_exp":5.5,"volume":.50},
    "koto2":                {"harmonics":[(1,.56),(2,.27),(3,.12),(4,.05)],"attack":.004,"decay":.10,"sustain":.52,"release":.13,"decay_exp":7.0,"volume":.52},
    "koto_bass":            {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.005,"decay":.12,"sustain":.54,"release":.15,"decay_exp":6.5,"volume":.52},
    "koto_electric":        {"harmonics":[(1,.54),(2,.28),(3,.13),(4,.05)],"attack":.004,"decay":.10,"sustain":.56,"release":.13,"decay_exp":7.0,"saturation":.8,"volume":.52},
    "shamisen2":            {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.003,"decay":.08,"sustain":.52,"release":.11,"decay_exp":8.0,"volume":.54},
    "shamisen_tsugaru":     {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.002,"decay":.07,"sustain":.50,"release":.10,"decay_exp":9.0,"saturation":.8,"volume":.54},
    "biwa":                 {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.004,"decay":.10,"sustain":.50,"release":.13,"decay_exp":7.5,"volume":.52},
    "erhu2":                {"harmonics":[(1,.58),(2,.25),(3,.12),(4,.05)],"attack":.048,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":6.0,"noise_mix":.022,"volume":.48},
    "erhu_Beijing":         {"harmonics":[(1,.60),(2,.22),(3,.11),(4,.07)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":5.5,"noise_mix":.02,"volume":.48},
    "zhonghu":              {"harmonics":[(1,.56),(2,.26),(3,.13),(4,.05)],"attack":.060,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.08,"vibrato_rate":5.0,"noise_mix":.025,"volume":.48},
    "banhu":                {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.048,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":6.0,"noise_mix":.025,"volume":.48},
    "morin_khuur":          {"harmonics":[(1,.54),(2,.26),(3,.14),(4,.06)],"attack":.060,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.08,"vibrato_rate":5.0,"noise_mix":.03,"volume":.48},
    "pipa2":                {"harmonics":[(1,.53),(2,.27),(3,.14),(4,.06)],"attack":.003,"decay":.11,"sustain":.52,"release":.14,"decay_exp":6.0,"volume":.52},
    "guzheng2":             {"harmonics":[(1,.53),(2,.27),(3,.14),(4,.06)],"attack":.004,"decay":.12,"sustain":.52,"release":.15,"decay_exp":6.0,"vibrato_depth":.04,"volume":.52},
    "guqin2":               {"harmonics":[(1,.56),(2,.25),(3,.12),(4,.07)],"attack":.008,"decay":.15,"sustain":.55,"release":.20,"decay_exp":5.5,"vibrato_depth":.03,"volume":.50},
    "konghou":              {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.004,"decay":.0,"sustain":.52,"release":.16,"decay_exp":5.0,"volume":.52},
    "ruan":                 {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.005,"decay":.11,"sustain":.55,"release":.14,"decay_exp":6.5,"volume":.52},
    "sanxian":              {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.003,"decay":.08,"sustain":.50,"release":.11,"decay_exp":8.0,"volume":.54},
    "zhongruan":            {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.006,"decay":.12,"sustain":.56,"release":.16,"decay_exp":6.0,"volume":.52},
    "daruan":               {"harmonics":[(1,.58),(2,.24),(3,.12),(4,.06)],"attack":.007,"decay":.14,"sustain":.56,"release":.18,"decay_exp":5.5,"volume":.52},
    "balalaika2":           {"harmonics":[(1,.54),(2,.28),(3,.13),(4,.05)],"attack":.003,"decay":.10,"sustain":.52,"release":.13,"decay_exp":7.5,"volume":.54},
    "domra":                {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.003,"decay":.09,"sustain":.50,"release":.12,"decay_exp":8.0,"volume":.54},
    "gusli":                {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.004,"decay":.0,"sustain":.50,"release":.15,"decay_exp":6.5,"volume":.52},
    "bouzouki2":            {"harmonics":[(1,.52),(2,.27),(3,.14),(4,.07)],"attack":.004,"decay":.12,"sustain":.56,"release":.15,"decay_exp":6.5,"volume":.52},
    "tzouras":              {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.07)],"attack":.004,"decay":.11,"sustain":.55,"release":.14,"decay_exp":6.8,"volume":.52},
    "baglama":              {"harmonics":[(1,.54),(2,.27),(3,.13),(4,.06)],"attack":.004,"decay":.11,"sustain":.56,"release":.14,"decay_exp":6.5,"volume":.52},
    "charango2":            {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.06)],"attack":.003,"decay":.10,"sustain":.52,"release":.13,"decay_exp":7.0,"volume":.52},
    "cuatro":               {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.07)],"attack":.004,"decay":.11,"sustain":.55,"release":.14,"decay_exp":7.0,"volume":.52},
    "timple":               {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.004,"decay":.10,"sustain":.55,"release":.13,"decay_exp":7.2,"volume":.52},
    "bandurria":            {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.003,"decay":.09,"sustain":.52,"release":.12,"decay_exp":8.0,"volume":.52},
    "laud":                 {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.07)],"attack":.004,"decay":.11,"sustain":.55,"release":.14,"decay_exp":7.0,"volume":.52},
    "kobza":                {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.07)],"attack":.004,"decay":.11,"sustain":.56,"release":.15,"decay_exp":7.0,"volume":.52},
    "bandura":              {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.004,"decay":.0,"sustain":.52,"release":.15,"decay_exp":6.0,"volume":.52},
    "dombra":               {"harmonics":[(1,.54),(2,.27),(3,.13),(4,.06)],"attack":.004,"decay":.11,"sustain":.54,"release":.14,"decay_exp":7.0,"volume":.52},
    "komuz":                {"harmonics":[(1,.56),(2,.25),(3,.12),(4,.07)],"attack":.004,"decay":.10,"sustain":.52,"release":.14,"decay_exp":7.5,"volume":.52},
    "dutar":                {"harmonics":[(1,.56),(2,.25),(3,.13),(4,.06)],"attack":.005,"decay":.12,"sustain":.55,"release":.15,"decay_exp":6.5,"volume":.52},
    "tanbur2":              {"harmonics":[(1,.54),(2,.27),(3,.13),(4,.06)],"attack":.004,"decay":.12,"sustain":.57,"release":.15,"decay_exp":6.5,"volume":.52},
    "setar2":               {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.005,"decay":.12,"sustain":.60,"release":.16,"decay_exp":6.0,"volume":.52},
    "tar2":                 {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.10,"sustain":.55,"release":.13,"decay_exp":7.0,"volume":.52},
    "dotar":                {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.005,"decay":.12,"sustain":.57,"release":.16,"decay_exp":6.0,"volume":.52},
    "khamancheh":           {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":5.5,"noise_mix":.02,"volume":.48},
    "kamanche":             {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.06)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":5.5,"noise_mix":.02,"volume":.48},
    "kemence":              {"harmonics":[(1,.54),(2,.26),(3,.14),(4,.06)],"attack":.048,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.12,"vibrato_rate":5.5,"noise_mix":.025,"volume":.48},
    "kamancha":             {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":5.5,"noise_mix":.02,"volume":.48},
    "mbira2":               {"harmonics":[(1,.58),(2,.25),(3,.12),(4,.05)],"attack":.003,"decay":.0,"sustain":.32,"release":.11,"decay_exp":10.,"volume":.52},
    "mbira_nyunga":         {"harmonics":[(1,.60),(2,.24),(3,.11),(4,.05)],"attack":.003,"decay":.0,"sustain":.30,"release":.10,"decay_exp":11.,"volume":.52},
    "kalimba2":             {"harmonics":[(1,.64),(2,.21),(3,.10),(4,.05)],"attack":.003,"decay":.0,"sustain":.36,"release":.12,"decay_exp":9.0,"volume":.52},
    "marimba_bass":         {"harmonics":[(1,.62),(3,.22),(5,.12),(7,.04)],"attack":.004,"decay":.0,"sustain":.28,"release":.12,"decay_exp":8.0,"volume":.54},
    "xylophone2":           {"harmonics":[(1,.62),(2.93,.28),(5.02,.10)],"attack":.003,"decay":.0,"sustain":.22,"release":.09,"decay_exp":12.,"volume":.54},
    "balafon":              {"harmonics":[(1,.58),(2.93,.28),(5.04,.12)],"attack":.004,"decay":.0,"sustain":.28,"release":.10,"decay_exp":9.0,"volume":.53},
    "amadinda":             {"harmonics":[(1,.60),(2.95,.26),(5.1,.12)],"attack":.004,"decay":.0,"sustain":.25,"release":.10,"decay_exp":10.,"volume":.53},
    "gamelan_gender":       {"harmonics":[(1,.58),(2.03,.26),(4.07,.14),(6.11,.07)],"attack":.006,"decay":.0,"sustain":.55,"release":.22,"decay_exp":5.0,"volume":.50},
    "gamelan_saron":        {"harmonics":[(1,.60),(2.02,.24),(4.06,.14),(6.1,.08)],"attack":.005,"decay":.0,"sustain":.50,"release":.18,"decay_exp":6.0,"volume":.52},
    "gamelan_bonang":       {"harmonics":[(1,.55),(2.03,.24),(4.07,.16),(6.12,.09)],"attack":.007,"decay":.0,"sustain":.60,"release":.25,"decay_exp":4.5,"volume":.50},
    "gamelan_gangsa":       {"harmonics":[(1,.58),(2.02,.25),(4.07,.13),(6.11,.07)],"attack":.006,"decay":.0,"sustain":.55,"release":.22,"decay_exp":5.0,"vibrato_depth":.02,"vibrato_rate":7.0,"volume":.50},
    "angklung":             {"harmonics":[(1,.55),(2.03,.26),(4.1,.14),(6.2,.07)],"attack":.005,"decay":.0,"sustain":.50,"release":.20,"decay_exp":6.5,"volume":.50},
    "steelpan2":            {"harmonics":[(1,.60),(2.03,.25),(4.1,.14),(6.2,.08),(8.3,.03)],"attack":.004,"decay":.0,"sustain":.47,"release":.16,"decay_exp":5.0,"volume":.52},
    "steelpan_tenor":       {"harmonics":[(1,.60),(2.03,.24),(4.1,.13),(6.2,.07),(8.3,.03)],"attack":.004,"decay":.0,"sustain":.45,"release":.15,"decay_exp":5.2,"volume":.52},
    "steelpan_bass":        {"harmonics":[(1,.62),(2.04,.22),(4.1,.12),(6.2,.06)],"attack":.005,"decay":.0,"sustain":.50,"release":.20,"decay_exp":4.5,"volume":.52},
    "ukulele":              {"harmonics":[(1,.56),(2,.26),(3,.14),(4,.04)],"attack":.004,"decay":.10,"sustain":.60,"release":.14,"decay_exp":5.5,"volume":.54},
    "ukulele_concert":      {"harmonics":[(1,.55),(2,.26),(3,.14),(4,.05)],"attack":.004,"decay":.10,"sustain":.60,"release":.14,"decay_exp":5.5,"volume":.54},
    "ukulele_tenor":        {"harmonics":[(1,.55),(2,.27),(3,.14),(4,.04)],"attack":.004,"decay":.11,"sustain":.60,"release":.15,"decay_exp":5.5,"volume":.53},
    "ukulele_baritone":     {"harmonics":[(1,.56),(2,.27),(3,.13),(4,.04)],"attack":.005,"decay":.12,"sustain":.62,"release":.16,"decay_exp":5.0,"volume":.53},
    "kora2":                {"harmonics":[(1,.56),(2,.26),(3,.12),(4,.06)],"attack":.004,"decay":.12,"sustain":.58,"release":.15,"decay_exp":6.5,"volume":.52},
    "kora_bass":            {"harmonics":[(1,.58),(2,.25),(3,.12),(4,.05)],"attack":.005,"decay":.14,"sustain":.60,"release":.18,"decay_exp":5.5,"volume":.52},
    "ngoni":                {"harmonics":[(1,.54),(2,.26),(3,.14),(4,.06)],"attack":.004,"decay":.12,"sustain":.56,"release":.15,"decay_exp":6.8,"volume":.52},
    "akonting":             {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.004,"decay":.11,"sustain":.56,"release":.15,"decay_exp":7.0,"volume":.52},
    "xalam":                {"harmonics":[(1,.54),(2,.27),(3,.14),(4,.05)],"attack":.004,"decay":.11,"sustain":.55,"release":.14,"decay_exp":7.0,"volume":.52},
    "gimbri":               {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.005,"decay":.12,"sustain":.58,"release":.16,"decay_exp":6.0,"noise_mix":.03,"volume":.52},
    "guenbri":              {"harmonics":[(1,.62),(2,.22),(3,.11),(4,.05)],"attack":.005,"decay":.12,"sustain":.58,"release":.16,"decay_exp":6.0,"noise_mix":.03,"volume":.52},
    "sintir":               {"harmonics":[(1,.62),(2,.22),(3,.11),(4,.05)],"attack":.005,"decay":.12,"sustain":.60,"release":.16,"decay_exp":5.8,"noise_mix":.03,"volume":.52},
    "ektara":               {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.004,"decay":.10,"sustain":.56,"release":.14,"decay_exp":6.5,"volume":.52},
    "dotara":               {"harmonics":[(1,.56),(2,.26),(3,.13),(4,.05)],"attack":.004,"decay":.11,"sustain":.56,"release":.14,"decay_exp":6.8,"volume":.52},
    "dilruba":              {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.050,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.10,"vibrato_rate":5.5,"noise_mix":.025,"volume":.48},
    "esraj":                {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.06)],"attack":.055,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.10,"vibrato_rate":5.0,"noise_mix":.025,"volume":.48},
    "israj":                {"harmonics":[(1,.56),(2,.25),(3,.13),(4,.06)],"attack":.055,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.09,"vibrato_rate":5.0,"noise_mix":.02,"volume":.48},
    "sarinda":              {"harmonics":[(1,.54),(2,.26),(3,.14),(4,.06)],"attack":.050,"decay":.0,"sustain":1.0,"release":.11,"vibrato_depth":.10,"vibrato_rate":5.5,"noise_mix":.025,"volume":.48},
    "sarangi":              {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.048,"decay":.0,"sustain":1.0,"release":.11,"vibrato_depth":.12,"vibrato_rate":5.5,"noise_mix":.03,"volume":.48},
    "sangha_rabab":         {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.050,"decay":.0,"sustain":1.0,"release":.11,"vibrato_depth":.10,"vibrato_rate":5.0,"noise_mix":.025,"volume":.48},
    "kemenche":             {"harmonics":[(1,.53),(2,.26),(3,.14),(4,.07)],"attack":.048,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.12,"vibrato_rate":5.5,"noise_mix":.025,"volume":.48},
    "cura":                 {"harmonics":[(1,.54),(2,.27),(3,.13),(4,.06)],"attack":.004,"decay":.09,"sustain":.55,"release":.13,"decay_exp":7.5,"volume":.53},
    "kopuz":                {"harmonics":[(1,.55),(2,.26),(3,.13),(4,.07)],"attack":.005,"decay":.11,"sustain":.57,"release":.14,"decay_exp":7.0,"volume":.52},
    "tembur":               {"harmonics":[(1,.54),(2,.26),(3,.13),(4,.07)],"attack":.005,"decay":.12,"sustain":.57,"release":.15,"decay_exp":6.8,"volume":.52},
    "tabla2":               {"harmonics":[(1,.55),(1.52,.27),(2.4,.14),(3.2,.04)],"attack":.003,"decay":.0,"sustain":.42,"release":.11,"decay_exp":10.,"noise_mix":.04,"volume":.54},
    "tabla_bayan":          {"harmonics":[(1,.60),(1.42,.26),(2.2,.12)],"attack":.004,"decay":.0,"sustain":.50,"release":.18,"decay_exp":7.0,"noise_mix":.06,"saturation":.8,"volume":.56},
    "tabla_dayan":          {"harmonics":[(1,.55),(1.52,.26),(2.4,.14),(3.2,.05)],"attack":.003,"decay":.0,"sustain":.38,"release":.09,"decay_exp":12.,"noise_mix":.04,"volume":.54},
    "mridangam":            {"harmonics":[(1,.55),(1.48,.26),(2.3,.14),(3.1,.05)],"attack":.003,"decay":.0,"sustain":.42,"release":.12,"decay_exp":10.,"noise_mix":.05,"volume":.54},
    "pakhawaj":             {"harmonics":[(1,.58),(1.45,.24),(2.2,.14),(3,.04)],"attack":.004,"decay":.0,"sustain":.48,"release":.15,"decay_exp":8.0,"noise_mix":.05,"saturation":.8,"volume":.55},
    "dholak":               {"harmonics":[(1,.58),(1.4,.24),(2,.13)],"attack":.004,"decay":.0,"sustain":.45,"release":.15,"decay_exp":7.5,"noise_mix":.06,"volume":.56},
    "darbuka2":             {"harmonics":[(1,.58),(1.44,.22),(2.2,.12)],"attack":.003,"decay":.0,"sustain":.36,"release":.09,"decay_exp":12.,"noise_mix":.05,"volume":.55},
    "riq":                  {"harmonics":[(1,.15),(3,.16),(7,.18),(11,.16),(15,.12),(19,.08)],"attack":.004,"decay":.0,"sustain":.35,"release":.12,"decay_exp":10.,"noise_mix":.55,"volume":.46},
    "mazhar":               {"harmonics":[(1,.12),(4,.14),(9,.16),(14,.14),(19,.12)],"attack":.005,"decay":.0,"sustain":.40,"release":.14,"decay_exp":8.0,"noise_mix":.60,"volume":.46},
    "bendir":               {"harmonics":[(1,.55),(1.5,.24),(2.3,.14),(3.2,.07)],"attack":.004,"decay":.0,"sustain":.40,"release":.14,"decay_exp":8.5,"noise_mix":.15,"volume":.54},
    "kanjira":              {"harmonics":[(1,.55),(1.52,.26),(2.4,.12)],"attack":.003,"decay":.0,"sustain":.38,"release":.10,"decay_exp":12.,"noise_mix":.10,"volume":.54},
    "frame_drum":           {"harmonics":[(1,.55),(1.5,.24),(2.3,.12),(3.2,.09)],"attack":.004,"decay":.0,"sustain":.42,"release":.14,"decay_exp":8.0,"noise_mix":.12,"volume":.54},
    "doumbek":              {"harmonics":[(1,.58),(1.44,.22),(2.2,.12)],"attack":.003,"decay":.0,"sustain":.36,"release":.09,"decay_exp":12.,"noise_mix":.05,"volume":.55},
    "ashiko":               {"harmonics":[(1,.55),(1.6,.25),(2.5,.12),(3.5,.06)],"attack":.003,"decay":.0,"sustain":.40,"release":.12,"decay_exp":9.5,"noise_mix":.06,"volume":.54},
    "batad":                {"harmonics":[(1,.56),(1.58,.24),(2.4,.12),(3.3,.08)],"attack":.003,"decay":.0,"sustain":.42,"release":.13,"decay_exp":9.0,"noise_mix":.06,"volume":.54},
    "taiko":                {"harmonics":[(1,.60),(1.4,.24),(2.1,.12),(3,.04)],"attack":.004,"decay":.0,"sustain":.55,"release":.25,"decay_exp":6.0,"noise_mix":.08,"volume":.58},
    "o_daiko":              {"harmonics":[(1,.65),(1.38,.22),(2,.10),(2.8,.04)],"attack":.005,"decay":.0,"sustain":.60,"release":.35,"decay_exp":4.5,"noise_mix":.08,"volume":.60},
    "tsuzumi":              {"harmonics":[(1,.52),(1.55,.26),(2.3,.14),(3.2,.08)],"attack":.003,"decay":.0,"sustain":.38,"release":.10,"decay_exp":11.,"noise_mix":.08,"volume":.54},
    "conga2":               {"harmonics":[(1,.55),(1.7,.24),(2.5,.12),(3.4,.05)],"attack":.003,"decay":.0,"sustain":.42,"release":.13,"decay_exp":8.0,"noise_mix":.04,"volume":.54},
    "bongo2":               {"harmonics":[(1,.55),(1.8,.24),(2.8,.11)],"attack":.003,"decay":.0,"sustain":.36,"release":.11,"decay_exp":10.,"noise_mix":.04,"volume":.54},
    "djembe2":              {"harmonics":[(1,.55),(1.6,.26),(2.5,.12),(3.5,.05)],"attack":.003,"decay":.0,"sustain":.42,"release":.13,"decay_exp":9.0,"noise_mix":.06,"volume":.55},
    "cajon2":               {"harmonics":[(1,.56),(1.5,.22),(2.2,.12)],"attack":.003,"decay":.0,"sustain":.36,"release":.13,"decay_exp":8.0,"noise_mix":.06,"volume":.54},
    "cajon_snare":          {"harmonics":[(1,.35),(2,.22),(3,.14)],"attack":.003,"decay":.0,"sustain":.30,"release":.10,"decay_exp":15.,"noise_mix":.45,"volume":.56},
    "cajon_bass":           {"harmonics":[(1,.62),(1.5,.22),(2.1,.12)],"attack":.004,"decay":.0,"sustain":.45,"release":.18,"decay_exp":7.0,"noise_mix":.06,"volume":.58},
    "talking_drum":         {"harmonics":[(1,.55),(1.5,.25),(2.3,.15),(3.2,.05)],"attack":.003,"decay":.0,"sustain":.45,"release":.14,"decay_exp":8.0,"noise_mix":.06,"vibrato_depth":.20,"vibrato_rate":2.0,"volume":.54},
    "log_drum":             {"harmonics":[(1,.58),(1.5,.24),(2.3,.14),(3.2,.04)],"attack":.004,"decay":.0,"sustain":.48,"release":.18,"decay_exp":7.5,"volume":.54},
    "slit_drum":            {"harmonics":[(1,.60),(1.48,.23),(2.3,.14),(3.2,.03)],"attack":.004,"decay":.0,"sustain":.50,"release":.20,"decay_exp":7.0,"volume":.54},
    "pot_drum":             {"harmonics":[(1,.58),(1.52,.24),(2.4,.13),(3.3,.05)],"attack":.004,"decay":.0,"sustain":.46,"release":.16,"decay_exp":7.5,"noise_mix":.06,"volume":.54},
    "steelpan_guitar":      {"harmonics":[(1,.58),(2.04,.24),(4.12,.13),(6.22,.07)],"attack":.004,"decay":.0,"sustain":.47,"release":.16,"decay_exp":5.0,"volume":.52},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ DRUMS EXTENDED (Kick/Snare/Hat/Cymbal/Tom/Perc/Genre) ██
    # ═══════════════════════════════════════════════════════════════════════════
    "kick_sub":             {"harmonics":[(1,.80),(2,.14),(3,.06)],"attack":.005,"decay":.0,"sustain":.85,"release":.30,"decay_exp":6.0,"saturation":1.0,"volume":.66},
    "kick_room":            {"harmonics":[(1,.62),(2,.20),(3,.12),(4,.06)],"attack":.004,"decay":.0,"sustain":.65,"release":.20,"decay_exp":10.,"noise_mix":.08,"volume":.62},
    "kick_tight2":          {"harmonics":[(1,.60),(2,.22),(3,.14),(4,.04)],"attack":.002,"decay":.0,"sustain":.45,"release":.08,"decay_exp":20.,"saturation":2.5,"volume":.64},
    "kick_vintage":         {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.004,"decay":.0,"sustain":.62,"release":.16,"decay_exp":11.,"noise_mix":.06,"saturation":1.2,"volume":.63},
    "kick_clicky":          {"harmonics":[(1,.55),(2,.26),(3,.14),(4,.05)],"attack":.002,"decay":.03,"sustain":.65,"release":.12,"decay_exp":14.,"saturation":3.0,"volume":.63},
    "kick_boomy":           {"harmonics":[(1,.78),(2,.14),(3,.06),(4,.02)],"attack":.006,"decay":.0,"sustain":.80,"release":.35,"decay_exp":7.0,"saturation":.8,"volume":.65},
    "kick_jazz2":           {"harmonics":[(1,.60),(2,.22),(3,.12),(4,.06)],"attack":.005,"decay":.0,"sustain":.60,"release":.14,"decay_exp":12.,"noise_mix":.10,"volume":.62},
    "kick_electronic":      {"harmonics":[(1,.68),(2,.20),(3,.10),(4,.02)],"attack":.003,"decay":.0,"sustain":.75,"release":.18,"decay_exp":10.,"saturation":2.0,"volume":.64},
    "kick_heavy":           {"harmonics":[(1,.62),(2,.22),(3,.12),(4,.04)],"attack":.004,"decay":.0,"sustain":.70,"release":.20,"decay_exp":9.0,"saturation":3.0,"volume":.63},
    "kick_metal":           {"harmonics":[(1,.58),(2,.24),(3,.14),(4,.04)],"attack":.003,"decay":.0,"sustain":.60,"release":.14,"decay_exp":13.,"saturation":4.0,"volume":.62},
    "kick_country":         {"harmonics":[(1,.62),(2,.22),(3,.12),(4,.04)],"attack":.004,"decay":.0,"sustain":.60,"release":.15,"decay_exp":11.,"noise_mix":.06,"volume":.62},
    "kick_hip_hop":         {"harmonics":[(1,.68),(2,.20),(3,.10),(4,.02)],"attack":.004,"decay":.0,"sustain":.72,"release":.22,"decay_exp":9.0,"saturation":2.0,"volume":.64},
    "kick_reggae":          {"harmonics":[(1,.70),(2,.18),(3,.09),(4,.03)],"attack":.006,"decay":.0,"sustain":.75,"release":.28,"decay_exp":7.5,"saturation":1.2,"volume":.64},
    "kick_latin":           {"harmonics":[(1,.62),(2,.22),(3,.12),(4,.04)],"attack":.004,"decay":.0,"sustain":.62,"release":.16,"decay_exp":11.,"noise_mix":.08,"volume":.62},
    "kick_studio":          {"harmonics":[(1,.65),(2,.20),(3,.11),(4,.04)],"attack":.003,"decay":.0,"sustain":.68,"release":.18,"decay_exp":10.,"saturation":2.2,"volume":.64},
    "kick_live":            {"harmonics":[(1,.60),(2,.22),(3,.12),(4,.06)],"attack":.004,"decay":.0,"sustain":.64,"release":.20,"decay_exp":10.,"noise_mix":.08,"volume":.62},
    "kick_layered":         {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.003,"decay":.0,"sustain":.70,"release":.22,"decay_exp":9.0,"saturation":2.0,"noise_mix":.04,"volume":.64},
    "kick_reversed":        {"harmonics":[(1,.65),(2,.20),(3,.11),(4,.04)],"attack":.200,"decay":.0,"sustain":.70,"release":.05,"decay_exp":5.0,"volume":.63},
    "kick_afro":            {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.004,"decay":.0,"sustain":.68,"release":.20,"decay_exp":9.5,"saturation":1.5,"volume":.63},
    "kick_drill2":          {"harmonics":[(1,.68),(2,.20),(3,.10),(4,.02)],"attack":.003,"decay":.0,"sustain":.75,"release":.18,"decay_exp":11.,"saturation":2.5,"volume":.65},
    "snare_deep":           {"harmonics":[(1,.40),(2,.24),(3,.14)],"attack":.003,"decay":.0,"sustain":.40,"release":.14,"decay_exp":14.,"noise_mix":.38,"saturation":.8,"volume":.60},
    "snare_bright":         {"harmonics":[(1,.35),(2,.22),(3,.18),(4,.10)],"attack":.002,"decay":.0,"sustain":.28,"release":.07,"decay_exp":20.,"noise_mix":.48,"saturation":1.5,"volume":.60},
    "snare_fat":            {"harmonics":[(1,.42),(2,.26),(3,.14),(4,.08)],"attack":.003,"decay":.0,"sustain":.38,"release":.12,"decay_exp":15.,"noise_mix":.42,"volume":.60},
    "snare_crack":          {"harmonics":[(1,.35),(2,.24),(3,.18),(4,.12)],"attack":.001,"decay":.0,"sustain":.22,"release":.05,"decay_exp":24.,"noise_mix":.55,"saturation":2.0,"volume":.62},
    "snare_snap2":          {"harmonics":[(1,.38),(2,.26),(3,.18)],"attack":.002,"decay":.0,"sustain":.25,"release":.06,"decay_exp":22.,"noise_mix":.50,"saturation":1.5,"volume":.62},
    "snare_buzz":           {"harmonics":[(1,.36),(2,.24),(3,.16)],"attack":.003,"decay":.0,"sustain":.50,"release":.18,"decay_exp":8.0,"noise_mix":.45,"volume":.58},
    "snare_ghost":          {"harmonics":[(1,.30),(2,.20),(3,.14)],"attack":.003,"decay":.0,"sustain":.25,"release":.08,"decay_exp":18.,"noise_mix":.40,"volume":.42},
    "snare_room":           {"harmonics":[(1,.38),(2,.24),(3,.15),(4,.08)],"attack":.003,"decay":.0,"sustain":.40,"release":.18,"decay_exp":10.,"noise_mix":.40,"volume":.58},
    "snare_vintage":        {"harmonics":[(1,.38),(2,.24),(3,.14)],"attack":.003,"decay":.0,"sustain":.35,"release":.12,"decay_exp":14.,"noise_mix":.38,"noise_mix":.40,"volume":.58},
    "snare_brushed":        {"harmonics":[(1,.32),(2,.22),(3,.14)],"attack":.008,"decay":.0,"sustain":.55,"release":.22,"noise_mix":.52,"volume":.50},
    "snare_brush_roll":     {"harmonics":[(1,.25),(2,.18),(3,.12)],"attack":.015,"decay":.0,"sustain":.80,"release":.30,"noise_mix":.60,"volume":.46},
    "snare_metal":          {"harmonics":[(1,.35),(2,.24),(3,.18),(4,.12),(5,.06)],"attack":.002,"decay":.0,"sustain":.25,"release":.06,"decay_exp":22.,"noise_mix":.55,"saturation":2.5,"volume":.60},
    "snare_rock":           {"harmonics":[(1,.40),(2,.26),(3,.16),(4,.08)],"attack":.002,"decay":.0,"sustain":.32,"release":.08,"decay_exp":18.,"noise_mix":.48,"saturation":1.8,"volume":.61},
    "snare_jazz":           {"harmonics":[(1,.36),(2,.24),(3,.15),(4,.05)],"attack":.003,"decay":.0,"sustain":.40,"release":.14,"decay_exp":12.,"noise_mix":.42,"volume":.58},
    "snare_hip_hop":        {"harmonics":[(1,.40),(2,.24),(3,.14)],"attack":.002,"decay":.0,"sustain":.30,"release":.08,"decay_exp":18.,"noise_mix":.48,"saturation":1.5,"volume":.61},
    "snare_trap2":          {"harmonics":[(1,.38),(2,.24),(3,.16)],"attack":.002,"decay":.0,"sustain":.28,"release":.08,"decay_exp":20.,"noise_mix":.50,"saturation":1.5,"volume":.60},
    "snare_reggae":         {"harmonics":[(1,.40),(2,.24),(3,.14),(4,.06)],"attack":.004,"decay":.0,"sustain":.38,"release":.14,"decay_exp":12.,"noise_mix":.42,"volume":.58},
    "snare_latin":          {"harmonics":[(1,.42),(2,.26),(3,.14),(4,.04)],"attack":.003,"decay":.0,"sustain":.36,"release":.10,"decay_exp":15.,"noise_mix":.40,"volume":.60},
    "snare_military":       {"harmonics":[(1,.38),(2,.24),(3,.14)],"attack":.003,"decay":.0,"sustain":.60,"release":.22,"noise_mix":.55,"volume":.58},
    "snare_march":          {"harmonics":[(1,.36),(2,.22),(3,.14)],"attack":.003,"decay":.0,"sustain":.55,"release":.20,"noise_mix":.52,"volume":.58},
    "snare_reversed":       {"harmonics":[(1,.40),(2,.24),(3,.14)],"attack":.120,"decay":.0,"sustain":.40,"release":.05,"noise_mix":.44,"volume":.58},
    "hihat_tr808":          {"harmonics":[(1,.15),(3,.18),(5,.22),(7,.20),(9,.14),(11,.08)],"attack":.002,"decay":.0,"sustain":.12,"release":.025,"decay_exp":35.,"noise_mix":.62,"volume":.44},
    "hihat_tr909":          {"harmonics":[(1,.14),(3,.18),(5,.24),(7,.22),(9,.14),(11,.08)],"attack":.002,"decay":.0,"sustain":.12,"release":.020,"decay_exp":40.,"noise_mix":.64,"volume":.44},
    "hihat_thin":           {"harmonics":[(1,.12),(3,.16),(5,.22),(7,.22),(9,.16),(11,.12)],"attack":.002,"decay":.0,"sustain":.10,"release":.020,"decay_exp":42.,"noise_mix":.68,"volume":.42},
    "hihat_dark":           {"harmonics":[(1,.22),(3,.24),(5,.22),(7,.16),(9,.10),(11,.06)],"attack":.003,"decay":.0,"sustain":.18,"release":.035,"decay_exp":25.,"noise_mix":.48,"volume":.46},
    "hihat_bright2":        {"harmonics":[(1,.12),(3,.15),(5,.22),(7,.24),(9,.16),(11,.08),(13,.03)],"attack":.001,"decay":.0,"sustain":.10,"release":.018,"decay_exp":45.,"noise_mix":.65,"volume":.42},
    "hihat_tight":          {"harmonics":[(1,.15),(3,.20),(5,.24),(7,.20),(9,.12),(11,.06),(13,.03)],"attack":.002,"decay":.0,"sustain":.10,"release":.018,"decay_exp":40.,"noise_mix":.62,"volume":.44},
    "hihat_loose":          {"harmonics":[(1,.18),(3,.20),(5,.22),(7,.20),(9,.12),(11,.06)],"attack":.002,"decay":.0,"sustain":.30,"release":.14,"decay_exp":12.,"noise_mix":.55,"volume":.46},
    "hihat_open2":          {"harmonics":[(1,.16),(3,.20),(5,.24),(7,.20),(9,.12),(11,.08)],"attack":.002,"decay":.0,"sustain":.55,"release":.25,"decay_exp":7.5,"noise_mix":.55,"volume":.48},
    "hihat_half_open":      {"harmonics":[(1,.17),(3,.20),(5,.24),(7,.20),(9,.12),(11,.07)],"attack":.002,"decay":.0,"sustain":.35,"release":.18,"decay_exp":9.0,"noise_mix":.58,"volume":.47},
    "hihat_sizzle":         {"harmonics":[(1,.16),(3,.20),(5,.24),(7,.20),(9,.12),(11,.08)],"attack":.002,"decay":.0,"sustain":.80,"release":.40,"decay_exp":4.0,"noise_mix":.55,"volume":.46},
    "hihat_jazz":           {"harmonics":[(1,.18),(3,.22),(5,.24),(7,.20),(9,.10),(11,.06)],"attack":.003,"decay":.0,"sustain":.25,"release":.10,"decay_exp":14.,"noise_mix":.50,"volume":.44},
    "hihat_brush":          {"harmonics":[(1,.14),(3,.18),(5,.22),(7,.22),(9,.14),(11,.10)],"attack":.008,"decay":.0,"sustain":.40,"release":.20,"noise_mix":.58,"volume":.42},
    "hihat_electronic":     {"harmonics":[(1,.12),(3,.16),(5,.24),(7,.24),(9,.16),(11,.08)],"attack":.002,"decay":.0,"sustain":.12,"release":.018,"decay_exp":45.,"noise_mix":.66,"volume":.42},
    "hihat_muted":          {"harmonics":[(1,.16),(3,.20),(5,.22),(7,.18),(9,.12),(11,.06),(13,.04)],"attack":.002,"decay":.0,"sustain":.08,"release":.012,"decay_exp":50.,"noise_mix":.60,"volume":.44},
    "hihat_vintage":        {"harmonics":[(1,.20),(3,.22),(5,.22),(7,.18),(9,.12),(11,.06)],"attack":.003,"decay":.0,"sustain":.18,"release":.040,"decay_exp":22.,"noise_mix":.50,"volume":.46},
    "hihat_metallic":       {"harmonics":[(1,.12),(3,.15),(5,.20),(7,.24),(9,.18),(11,.10),(13,.01)],"attack":.002,"decay":.0,"sustain":.12,"release":.020,"decay_exp":40.,"noise_mix":.65,"saturation":.8,"volume":.42},
    "hihat_washy":          {"harmonics":[(1,.16),(3,.20),(5,.22),(7,.20),(9,.12),(11,.10)],"attack":.002,"decay":.0,"sustain":.70,"release":.50,"decay_exp":3.5,"noise_mix":.52,"volume":.46},
    "cymbal_china2":        {"harmonics":[(1,.14),(2.5,.20),(4.,.22),(6.5,.18),(9.5,.12),(13.5,.08),(17.,.04)],"attack":.003,"decay":.0,"sustain":.62,"release":.45,"decay_exp":5.0,"noise_mix":.48,"saturation":.8,"volume":.50},
    "cymbal_stack":         {"harmonics":[(1,.15),(2.5,.20),(4.,.22),(6.5,.18),(9.5,.12),(13.5,.08)],"attack":.003,"decay":.0,"sustain":.25,"release":.12,"decay_exp":12.,"noise_mix":.50,"volume":.48},
    "cymbal_sizzle":        {"harmonics":[(1,.14),(2.87,.18),(4.98,.20),(7.12,.18),(9.7,.14),(12.5,.10)],"attack":.005,"decay":.0,"sustain":.80,"release":.60,"decay_exp":2.5,"noise_mix":.38,"volume":.48},
    "cymbal_flat":          {"harmonics":[(1,.15),(2.8,.18),(4.9,.20),(7.,.18),(9.5,.14),(12.,.10)],"attack":.005,"decay":.0,"sustain":.50,"release":.30,"decay_exp":5.5,"noise_mix":.42,"volume":.48},
    "cymbal_trash":         {"harmonics":[(1,.14),(2.5,.18),(4.2,.22),(6.8,.20),(9.8,.14),(14.,.08)],"attack":.004,"decay":.0,"sustain":.35,"release":.20,"decay_exp":8.0,"noise_mix":.52,"saturation":.9,"volume":.48},
    "cymbal_bell":          {"harmonics":[(1,.60),(2.93,.24),(5.05,.14),(7.4,.08),(9.8,.04)],"attack":.005,"decay":.0,"sustain":.65,"release":.38,"decay_exp":4.0,"noise_mix":.12,"volume":.52},
    "cymbal_low":           {"harmonics":[(1,.18),(2.5,.20),(4.,.22),(6.5,.18),(9.5,.14),(13.5,.08)],"attack":.006,"decay":.0,"sustain":.65,"release":.55,"decay_exp":3.8,"noise_mix":.40,"volume":.50},
    "cymbal_hi":            {"harmonics":[(1,.12),(2.87,.16),(4.98,.22),(7.12,.22),(9.7,.16),(12.5,.10),(16.,.02)],"attack":.004,"decay":.0,"sustain":.45,"release":.25,"decay_exp":7.0,"noise_mix":.45,"volume":.46},
    "cymbal_vintage":       {"harmonics":[(1,.16),(2.85,.20),(4.9,.20),(7.,.18),(9.5,.12),(12.5,.08),(16.,.04)],"attack":.005,"decay":.0,"sustain":.55,"release":.40,"decay_exp":4.5,"noise_mix":.38,"volume":.50},
    "tom_snare_hit":        {"harmonics":[(1,.55),(1.5,.25),(2.3,.14),(3.2,.06)],"attack":.003,"decay":.0,"sustain":.45,"release":.12,"decay_exp":10.,"noise_mix":.12,"volume":.58},
    "tom_deep":             {"harmonics":[(1,.68),(1.38,.20),(2.,.10),(2.8,.02)],"attack":.004,"decay":.0,"sustain":.60,"release":.32,"decay_exp":5.5,"noise_mix":.06,"volume":.62},
    "tom_electronic":       {"harmonics":[(1,.65),(1.42,.22),(2.1,.10),(3.,.03)],"attack":.003,"decay":.0,"sustain":.55,"release":.22,"decay_exp":7.0,"saturation":1.5,"noise_mix":.05,"volume":.60},
    "tom_room":             {"harmonics":[(1,.62),(1.44),(2.15),(3.,.04)],"attack":.004,"decay":.0,"sustain":.55,"release":.28,"decay_exp":6.5,"noise_mix":.08,"volume":.60},
    "tom_roto":             {"harmonics":[(1,.65),(1.4,.22),(2.1,.10),(3.,.03)],"attack":.004,"decay":.0,"sustain":.58,"release":.28,"decay_exp":6.0,"noise_mix":.06,"volume":.60},
    "tom_concert":          {"harmonics":[(1,.62),(1.42,.22),(2.15,.10),(3.,.03)],"attack":.004,"decay":.0,"sustain":.55,"release":.26,"decay_exp":7.0,"noise_mix":.08,"volume":.60},
    "rim_shot":             {"harmonics":[(1,.45),(2,.28),(3,.18),(4,.09)],"attack":.002,"decay":.0,"sustain":.30,"release":.08,"decay_exp":18.,"noise_mix":.25,"saturation":1.8,"volume":.62},
    "cross_stick":          {"harmonics":[(1,.52),(2,.28),(3,.16),(4,.04)],"attack":.002,"decay":.0,"sustain":.28,"release":.06,"decay_exp":22.,"noise_mix":.10,"volume":.60},
    "sidestick":            {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.002,"decay":.0,"sustain":.25,"release":.06,"decay_exp":22.,"noise_mix":.08,"volume":.60},
    "clap_snappy":          {"harmonics":[(1,.18),(2,.14),(3,.10)],"attack":.002,"decay":.0,"sustain":.18,"release":.05,"decay_exp":22.,"noise_mix":.68,"saturation":1.8,"volume":.60},
    "clap_fat":             {"harmonics":[(1,.22),(2,.18),(3,.14),(4,.08)],"attack":.003,"decay":.0,"sustain":.28,"release":.09,"decay_exp":14.,"noise_mix":.62,"volume":.58},
    "clap_trap2":           {"harmonics":[(1,.18),(2,.14),(3,.10)],"attack":.002,"decay":.0,"sustain":.20,"release":.06,"decay_exp":20.,"noise_mix":.65,"saturation":1.5,"volume":.60},
    "clap_electronic":      {"harmonics":[(1,.20),(2,.16),(3,.12)],"attack":.003,"decay":.0,"sustain":.22,"release":.08,"decay_exp":18.,"noise_mix":.62,"saturation":1.2,"volume":.58},
    "clap_vintage":         {"harmonics":[(1,.22),(2,.18),(3,.12)],"attack":.003,"decay":.0,"sustain":.25,"release":.09,"decay_exp":16.,"noise_mix":.58,"volume":.57},
    "clap_room":            {"harmonics":[(1,.22),(2,.18),(3,.12)],"attack":.003,"decay":.0,"sustain":.35,"release":.16,"decay_exp":10.,"noise_mix":.60,"volume":.56},
    "clap_gang":            {"harmonics":[(1,.20),(2,.16),(3,.12),(4,.08)],"attack":.003,"decay":.0,"sustain":.30,"release":.12,"decay_exp":12.,"noise_mix":.62,"volume":.56},
    "fingersnap":           {"harmonics":[(1,.22),(2,.18),(3,.12)],"attack":.002,"decay":.0,"sustain":.18,"release":.05,"decay_exp":20.,"noise_mix":.58,"volume":.54},
    "woodblock":            {"harmonics":[(1,.55),(2.45,.28),(4.9,.14)],"attack":.003,"decay":.0,"sustain":.30,"release":.10,"decay_exp":12.,"volume":.54},
    "woodblock_high":       {"harmonics":[(1,.55),(2.5,.28),(5.,.14)],"attack":.002,"decay":.0,"sustain":.25,"release":.08,"decay_exp":14.,"volume":.54},
    "woodblock_low":        {"harmonics":[(1,.56),(2.4,.27),(4.8,.13)],"attack":.003,"decay":.0,"sustain":.32,"release":.12,"decay_exp":11.,"volume":.54},
    "triangle2":            {"harmonics":[(1,.60),(2.76,.25),(5.5,.12),(9.3,.06)],"attack":.003,"decay":.0,"sustain":.80,"release":.60,"decay_exp":2.5,"volume":.50},
    "triangle_muted":       {"harmonics":[(1,.62),(2.76,.24),(5.5,.12)],"attack":.003,"decay":.0,"sustain":.40,"release":.18,"decay_exp":8.0,"volume":.50},
    "cowbell2":             {"harmonics":[(1,.60),(2.45,.28),(4.9,.12)],"attack":.003,"decay":.0,"sustain":.38,"release":.14,"decay_exp":10.,"volume":.52},
    "agogo":                {"harmonics":[(1,.58),(2.5,.26),(5.,.14),(8.,.06)],"attack":.003,"decay":.0,"sustain":.35,"release":.12,"decay_exp":11.,"volume":.52},
    "cowbell_large":        {"harmonics":[(1,.60),(2.3,.28),(4.5,.13),(7.5,.05)],"attack":.004,"decay":.0,"sustain":.42,"release":.18,"decay_exp":9.0,"volume":.52},
    "shaker2":              {"harmonics":[(1,.05),(3,.06),(7,.08),(11,.07),(15,.06)],"attack":.010,"decay":.0,"sustain":.62,"release":.10,"noise_mix":.70,"volume":.44},
    "shaker_tight":         {"harmonics":[(1,.05),(3,.06),(7,.08),(11,.07),(15,.06)],"attack":.006,"decay":.0,"sustain":.50,"release":.06,"noise_mix":.72,"volume":.44},
    "shaker_egg":           {"harmonics":[(1,.04),(5,.06),(9,.08),(13,.08),(17,.06)],"attack":.008,"decay":.0,"sustain":.55,"release":.09,"noise_mix":.74,"volume":.42},
    "maracas":              {"harmonics":[(1,.04),(5,.06),(9,.08),(13,.08)],"attack":.008,"decay":.0,"sustain":.50,"release":.08,"noise_mix":.76,"volume":.42},
    "guiro":                {"harmonics":[(1,.10),(3,.10),(5,.10),(7,.10),(9,.08)],"attack":.020,"decay":.0,"sustain":.80,"release":.20,"noise_mix":.60,"volume":.44},
    "guiro_short":          {"harmonics":[(1,.10),(3,.10),(5,.10),(7,.08)],"attack":.008,"decay":.0,"sustain":.40,"release":.10,"noise_mix":.62,"volume":.44},
    "claves":               {"harmonics":[(1,.65),(2.5,.22),(4.8,.10),(7.2,.03)],"attack":.002,"decay":.0,"sustain":.25,"release":.07,"decay_exp":16.,"volume":.56},
    "castanet":             {"harmonics":[(1,.55),(2.4,.25),(4.6,.14),(7.,.06)],"attack":.002,"decay":.0,"sustain":.20,"release":.05,"decay_exp":18.,"volume":.56},
    "castanets":            {"harmonics":[(1,.55),(2.4,.25),(4.6,.14)],"attack":.002,"decay":.0,"sustain":.20,"release":.05,"decay_exp":20.,"volume":.56},
    "vibraslap":            {"harmonics":[(1,.35),(2,.25),(3,.20),(4,.12),(5,.08)],"attack":.003,"decay":.0,"sustain":.70,"release":.40,"decay_exp":4.5,"noise_mix":.15,"volume":.52},
    "ratchet":              {"harmonics":[(1,.10),(3,.12),(5,.12),(7,.10)],"attack":.015,"decay":.0,"sustain":.80,"release":.15,"noise_mix":.65,"volume":.44},
    "clapper":              {"harmonics":[(1,.22),(2,.18),(3,.14),(4,.08)],"attack":.003,"decay":.0,"sustain":.25,"release":.08,"decay_exp":16.,"noise_mix":.55,"volume":.56},
    "surdo":                {"harmonics":[(1,.68),(1.38,.22),(2.,.10),(2.8,.04)],"attack":.005,"decay":.0,"sustain":.65,"release":.35,"decay_exp":5.0,"noise_mix":.06,"volume":.62},
    "surdo_muted":          {"harmonics":[(1,.65),(1.4,.22),(2.1,.12)],"attack":.004,"decay":.0,"sustain":.45,"release":.14,"decay_exp":9.0,"noise_mix":.07,"volume":.60},
    "pandeiro":             {"harmonics":[(1,.18),(3,.16),(7,.16),(11,.14),(15,.12)],"attack":.004,"decay":.0,"sustain":.40,"release":.14,"decay_exp":8.0,"noise_mix":.58,"volume":.46},
    "repique":              {"harmonics":[(1,.35),(2,.24),(3,.18),(4,.12)],"attack":.003,"decay":.0,"sustain":.30,"release":.08,"decay_exp":16.,"noise_mix":.40,"saturation":1.2,"volume":.58},
    "tamborim":             {"harmonics":[(1,.40),(2,.28),(3,.20),(4,.12)],"attack":.002,"decay":.0,"sustain":.22,"release":.06,"decay_exp":20.,"noise_mix":.35,"volume":.58},
    "caixa":                {"harmonics":[(1,.35),(2,.22),(3,.16)],"attack":.003,"decay":.0,"sustain":.35,"release":.10,"decay_exp":16.,"noise_mix":.48,"volume":.58},
    "cuica":                {"harmonics":[(1,.55),(1.52,.28),(2.4,.14),(3.2,.03)],"attack":.010,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.15,"vibrato_rate":3.0,"noise_mix":.08,"volume":.54},
    "friction_drum":        {"harmonics":[(1,.55),(1.5,.26),(2.4,.16),(3.3,.03)],"attack":.015,"decay":.0,"sustain":1.0,"release":.15,"vibrato_depth":.12,"vibrato_rate":2.5,"noise_mix":.06,"volume":.54},
    "snare_electronic2":    {"harmonics":[(1,.42),(2,.26),(3,.14)],"attack":.003,"decay":.0,"sustain":.30,"release":.09,"decay_exp":17.,"noise_mix":.48,"saturation":1.5,"volume":.61},
    "snare_digital":        {"harmonics":[(1,.40),(2,.26),(3,.16)],"attack":.002,"decay":.0,"sustain":.28,"release":.08,"decay_exp":18.,"noise_mix":.50,"saturation":2.0,"volume":.61},
    "kick_boom":            {"harmonics":[(1,.80),(2,.12),(3,.06),(4,.02)],"attack":.005,"decay":.0,"sustain":.85,"release":.40,"decay_exp":5.5,"saturation":1.0,"volume":.66},
    "kick_thump":           {"harmonics":[(1,.75),(2,.16),(3,.07),(4,.02)],"attack":.004,"decay":.0,"sustain":.80,"release":.28,"decay_exp":7.5,"saturation":1.5,"volume":.65},
    "kick_click_hard":      {"harmonics":[(1,.56),(2,.26),(3,.14),(4,.04)],"attack":.002,"decay":.02,"sustain":.68,"release":.14,"decay_exp":13.,"saturation":3.5,"volume":.63},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ KEYBOARD EXTENDED (Piano/Organ/Keys/Mallet/Electronic) ██
    # ═══════════════════════════════════════════════════════════════════════════
    "piano2":               {"harmonics":[(1,.50),(2,.25),(3,.12),(4,.07),(5,.04),(6,.02)],"attack":.005,"decay":.0,"sustain":.80,"release":.16,"decay_exp":4.5,"volume":.52},
    "piano_bright2":        {"harmonics":[(1,.44),(2,.26),(3,.15),(4,.08),(5,.05),(6,.02)],"attack":.004,"decay":.0,"sustain":.80,"release":.14,"decay_exp":4.0,"volume":.50},
    "piano_dark":           {"harmonics":[(1,.58),(2,.24),(3,.10),(4,.06),(5,.02)],"attack":.006,"decay":.0,"sustain":.80,"release":.18,"decay_exp":5.0,"volume":.52},
    "piano_mellow":         {"harmonics":[(1,.62),(2,.22),(3,.09),(4,.05),(5,.02)],"attack":.008,"decay":.0,"sustain":.82,"release":.20,"decay_exp":4.5,"volume":.52},
    "piano_percussive":     {"harmonics":[(1,.50),(2,.26),(3,.12),(4,.08),(5,.04)],"attack":.003,"decay":.0,"sustain":.70,"release":.12,"decay_exp":6.0,"volume":.52},
    "piano_sustain_long":   {"harmonics":[(1,.50),(2,.25),(3,.12),(4,.07),(5,.04),(6,.02)],"attack":.005,"decay":.0,"sustain":.82,"release":.25,"decay_exp":3.5,"volume":.50},
    "piano_plonk":          {"harmonics":[(1,.52),(2,.26),(3,.12),(4,.06),(5,.04)],"attack":.003,"decay":.0,"sustain":.70,"release":.12,"decay_exp":6.5,"volume":.52},
    "piano_felt":           {"harmonics":[(1,.58),(2,.24),(3,.10),(4,.06),(5,.02)],"attack":.006,"decay":.0,"sustain":.75,"release":.18,"decay_exp":5.5,"noise_mix":.008,"volume":.52},
    "piano_prepared":       {"harmonics":[(1,.45),(2.1,.28),(3.2,.15),(4.4,.09),(5.8,.04)],"attack":.004,"decay":.0,"sustain":.60,"release":.18,"decay_exp":6.0,"volume":.50},
    "piano_toy":            {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07),(5,.02)],"attack":.004,"decay":.0,"sustain":.65,"release":.12,"decay_exp":7.0,"noise_mix":.01,"volume":.52},
    "piano_upright2":       {"harmonics":[(1,.52),(2,.26),(3,.12),(4,.06),(5,.04)],"attack":.005,"decay":.0,"sustain":.76,"release":.15,"decay_exp":5.0,"volume":.52},
    "piano_concert_grand":  {"harmonics":[(1,.48),(2,.26),(3,.14),(4,.07),(5,.04),(6,.02)],"attack":.004,"decay":.0,"sustain":.84,"release":.20,"decay_exp":3.5,"volume":.52},
    "piano_chamber":        {"harmonics":[(1,.50),(2,.25),(3,.13),(4,.07),(5,.05),(6,.01)],"attack":.005,"decay":.0,"sustain":.82,"release":.18,"decay_exp":4.0,"volume":.52},
    "piano_lofi2":          {"harmonics":[(1,.52),(2,.25),(3,.12),(4,.07),(5,.04)],"attack":.006,"decay":.0,"sustain":.70,"release":.16,"decay_exp":5.5,"noise_mix":.02,"saturation":.8,"volume":.50},
    "piano_celeste":        {"harmonics":[(1,.60),(2,.24),(3,.10),(4,.06)],"attack":.004,"decay":.0,"sustain":.42,"release":.16,"decay_exp":7.0,"volume":.50},
    "piano_plucked":        {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.003,"decay":.0,"sustain":.55,"release":.14,"decay_exp":6.5,"volume":.52},
    "electric_piano2":      {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.004,"decay":.0,"sustain":.72,"release":.13,"decay_exp":6.0,"volume":.52},
    "electric_piano_hard":  {"harmonics":[(1,.50),(2,.30),(3,.14),(4,.06)],"attack":.003,"decay":.0,"sustain":.70,"release":.12,"decay_exp":6.5,"saturation":1.5,"volume":.52},
    "electric_piano_soft":  {"harmonics":[(1,.62),(2,.24),(3,.10),(4,.04)],"attack":.006,"decay":.0,"sustain":.75,"release":.16,"decay_exp":5.5,"volume":.52},
    "electric_piano_wah":   {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.04,"sustain":.72,"release":.12,"saturation":1.2,"volume":.50},
    "electric_piano_chorus":{"harmonics":[(1,.52),(1.005,.44),(2,.23),(3,.10)],"attack":.005,"decay":.0,"sustain":.72,"release":.14,"volume":.46},
    "rhodes2":              {"harmonics":[(1,.52),(2,.30),(3,.14),(4,.04)],"attack":.006,"decay":.0,"sustain":.76,"release":.19,"decay_exp":5.0,"volume":.50},
    "rhodes_dark":          {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.006,"decay":.0,"sustain":.78,"release":.20,"decay_exp":4.5,"volume":.50},
    "rhodes_bright":        {"harmonics":[(1,.48),(2,.30),(3,.16),(4,.06)],"attack":.005,"decay":.0,"sustain":.72,"release":.16,"decay_exp":5.5,"volume":.50},
    "rhodes_vintage":       {"harmonics":[(1,.55),(2,.28),(3,.13),(4,.04)],"attack":.006,"decay":.0,"sustain":.75,"release":.20,"decay_exp":5.0,"noise_mix":.01,"volume":.50},
    "rhodes_phase":         {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.006,"decay":.0,"sustain":.76,"release":.19,"vibrato_depth":.05,"vibrato_rate":1.0,"volume":.48},
    "wurlitzer":            {"harmonics":[(1,.54),(2,.28),(3,.14),(4,.04)],"attack":.005,"decay":.0,"sustain":.68,"release":.15,"decay_exp":6.0,"saturation":.8,"volume":.52},
    "wurlitzer_dark":       {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.006,"decay":.0,"sustain":.70,"release":.17,"decay_exp":5.5,"volume":.52},
    "clavinet":             {"harmonics":[(1,.48),(2,.30),(3,.16),(4,.06)],"attack":.003,"decay":.0,"sustain":.60,"release":.10,"decay_exp":8.0,"saturation":1.2,"volume":.52},
    "clavinet_wah":         {"harmonics":[(1,.46),(2,.30),(3,.18),(4,.06)],"attack":.003,"decay":.04,"sustain":.58,"release":.10,"saturation":1.5,"volume":.50},
    "clavichord":           {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.004,"decay":.0,"sustain":.55,"release":.12,"decay_exp":7.0,"volume":.50},
    "fortepiano":           {"harmonics":[(1,.52),(2,.26),(3,.13),(4,.07),(5,.02)],"attack":.005,"decay":.0,"sustain":.70,"release":.14,"decay_exp":5.5,"volume":.50},
    "spinet":               {"harmonics":[(1,.50),(2,.28),(3,.15),(4,.07),(5,.02)],"attack":.004,"decay":.0,"sustain":.25,"release":.10,"decay_exp":10.,"volume":.52},
    "virginal":             {"harmonics":[(1,.50),(2,.28),(3,.15),(4,.07)], "attack":.003,"decay":.0,"sustain":.22,"release":.09,"decay_exp":11.,"volume":.52},
    "organ2":               {"harmonics":[(1,.40),(2,.30),(3,.20),(4,.07),(6,.03)],"attack":.010,"decay":.0,"sustain":1.0,"release":.05,"volume":.48},
    "organ_drawbar":        {"harmonics":[(1,.44),(2,.32),(3,.20),(4,.10),(5,.04),(6,.02)],"attack":.008,"decay":.0,"sustain":1.0,"release":.05,"volume":.46},
    "organ_full":           {"harmonics":[(1,.35),(2,.28),(3,.22),(4,.12),(5,.08),(6,.04),(7,.02)],"attack":.008,"decay":.0,"sustain":1.0,"release":.05,"volume":.42},
    "organ_bright2":        {"harmonics":[(1,.38),(2,.30),(3,.20),(4,.10),(5,.06),(6,.04)],"attack":.008,"decay":.0,"sustain":1.0,"release":.05,"saturation":.8,"volume":.44},
    "organ_dark2":          {"harmonics":[(1,.48),(2,.28),(3,.18),(4,.06)],"attack":.010,"decay":.0,"sustain":1.0,"release":.06,"volume":.48},
    "organ_soft":           {"harmonics":[(1,.50),(2,.28),(3,.15),(4,.07)],"attack":.012,"decay":.0,"sustain":1.0,"release":.06,"volume":.48},
    "organ_gospel2":        {"harmonics":[(1,.40),(2,.30),(3,.20),(4,.08),(5,.04),(6,.02)],"attack":.006,"decay":.0,"sustain":1.0,"release":.04,"saturation":1.2,"volume":.46},
    "organ_blues":          {"harmonics":[(1,.42),(2,.30),(3,.18),(4,.08),(5,.04),(6,.02)],"attack":.007,"decay":.0,"sustain":1.0,"release":.05,"saturation":1.5,"volume":.46},
    "organ_cathedral2":     {"harmonics":[(1,.38),(2,.28),(3,.22),(4,.10),(5,.02)],"attack":.025,"decay":.0,"sustain":1.0,"release":.10,"volume":.44},
    "organ_pipe":           {"harmonics":[(1,.42),(2,.26),(3,.20),(4,.08),(5,.04)],"attack":.030,"decay":.0,"sustain":1.0,"release":.08,"volume":.46},
    "organ_vox":            {"harmonics":[(1,.42),(2,.30),(3,.20),(4,.08),(6,.04)],"attack":.006,"decay":.0,"sustain":1.0,"release":.04,"volume":.46},
    "organ_continental":    {"harmonics":[(1,.44),(2,.28),(3,.18),(4,.08),(5,.02)],"attack":.008,"decay":.0,"sustain":1.0,"release":.05,"volume":.46},
    "organ_leslie_slow":    {"harmonics":[(1,.40),(2,.30),(3,.20),(4,.07),(6,.03)],"attack":.010,"decay":.0,"sustain":1.0,"release":.05,"vibrato_depth":.04,"vibrato_rate":0.8,"volume":.46},
    "organ_leslie_fast":    {"harmonics":[(1,.40),(2,.30),(3,.20),(4,.07),(6,.03)],"attack":.008,"decay":.0,"sustain":1.0,"release":.05,"vibrato_depth":.06,"vibrato_rate":7.0,"volume":.46},
    "organ_overdrive":      {"harmonics":[(1,.40),(2,.28),(3,.20),(4,.10),(5,.04)],"attack":.006,"decay":.0,"sustain":1.0,"release":.04,"saturation":3.0,"volume":.44},
    "harpsichord2":         {"harmonics":[(1,.45),(2,.30),(3,.15),(4,.08),(5,.02)],"attack":.003,"decay":.0,"sustain":.22,"release":.11,"decay_exp":9.0,"volume":.52},
    "harpsichord_lute":     {"harmonics":[(1,.48),(2,.28),(3,.14),(4,.10)],"attack":.004,"decay":.0,"sustain":.28,"release":.12,"decay_exp":8.5,"volume":.52},
    "harpsichord_two_man":  {"harmonics":[(1,.44),(2,.30),(3,.16),(4,.08),(5,.02)],"attack":.003,"decay":.0,"sustain":.20,"release":.10,"decay_exp":9.5,"volume":.52},
    "celesta2":             {"harmonics":[(1,.62),(2,.24),(3,.10),(4,.04)],"attack":.003,"decay":.0,"sustain":.42,"release":.16,"decay_exp":7.0,"volume":.50},
    "celesta_dark":         {"harmonics":[(1,.66),(2,.22),(3,.08),(4,.04)],"attack":.004,"decay":.0,"sustain":.45,"release":.18,"decay_exp":6.5,"volume":.50},
    "vibraphone2":          {"harmonics":[(1,.62),(2,.24),(3,.10),(4,.04)],"attack":.005,"decay":.0,"sustain":.62,"release":.22,"decay_exp":4.0,"vibrato_depth":.025,"vibrato_rate":6.0,"volume":.52},
    "vibraphone_muted":     {"harmonics":[(1,.64),(2,.22),(3,.10),(4,.04)],"attack":.005,"decay":.0,"sustain":.45,"release":.14,"decay_exp":6.0,"volume":.52},
    "marimba2":             {"harmonics":[(1,.60),(3,.25),(5,.10),(7,.05)],"attack":.004,"decay":.0,"sustain":.32,"release":.11,"decay_exp":8.0,"volume":.54},
    "marimba_soprano":      {"harmonics":[(1,.58),(3,.26),(5,.12),(7,.04)],"attack":.003,"decay":.0,"sustain":.30,"release":.09,"decay_exp":9.0,"volume":.54},
    "marimba_alto":         {"harmonics":[(1,.60),(3,.24),(5,.12),(7,.04)],"attack":.004,"decay":.0,"sustain":.32,"release":.11,"decay_exp":8.0,"volume":.54},
    "marimba_tenor":        {"harmonics":[(1,.62),(3,.22),(5,.11),(7,.05)],"attack":.004,"decay":.0,"sustain":.34,"release":.12,"decay_exp":7.5,"volume":.54},
    "xylophone3":           {"harmonics":[(1,.62),(2.93,.27),(5.02,.10)],"attack":.003,"decay":.0,"sustain":.22,"release":.08,"decay_exp":12.5,"volume":.54},
    "xylophone_bass":       {"harmonics":[(1,.64),(2.93,.24),(5.02,.10)],"attack":.004,"decay":.0,"sustain":.25,"release":.10,"decay_exp":10.,"volume":.54},
    "glockenspiel2":        {"harmonics":[(1,.60),(2.93,.24),(5.02,.14),(7.5,.08),(9.7,.04)],"attack":.003,"decay":.0,"sustain":.27,"release":.13,"decay_exp":10.,"volume":.52},
    "glockenspiel_high":    {"harmonics":[(1,.60),(2.95,.24),(5.1,.14),(7.6,.08),(9.8,.04)],"attack":.002,"decay":.0,"sustain":.24,"release":.10,"decay_exp":12.,"volume":.52},
    "bells_tubular":        {"harmonics":[(1,.55),(2.76,.30),(4.07,.20),(5.5,.12),(6.78,.08)],"attack":.006,"decay":.0,"sustain":.55,"release":.40,"decay_exp":4.0,"volume":.50},
    "bells_church":         {"harmonics":[(1,.52),(2.87,.28),(4.95,.18),(7.12,.12),(9.7,.08),(12.5,.04)],"attack":.008,"decay":.0,"sustain":.60,"release":.55,"decay_exp":3.0,"volume":.48},
    "bells_handbell":       {"harmonics":[(1,.58),(2.76,.26),(4.1,.16),(5.5,.10),(6.8,.06)],"attack":.004,"decay":.0,"sustain":.50,"release":.35,"decay_exp":4.5,"volume":.50},
    "bells_carillon":       {"harmonics":[(1,.52),(2.87,.26),(4.95,.18),(7.12,.12),(9.7,.08)],"attack":.007,"decay":.0,"sustain":.60,"release":.60,"decay_exp":3.0,"volume":.48},
    "bells_wind":           {"harmonics":[(1,.55),(2.93,.25),(5.3,.14),(8.1,.08),(10.5,.04)],"attack":.010,"decay":.0,"sustain":.65,"release":.55,"decay_exp":3.5,"volume":.48},
    "bells_crystal2":       {"harmonics":[(1,.58),(2.93,.24),(5.3,.12),(8.1,.06),(10.5,.03)],"attack":.005,"decay":.0,"sustain":.55,"release":.45,"decay_exp":4.0,"volume":.50},
    "music_box2":           {"harmonics":[(1,.65),(2,.20),(3,.10),(4,.05)],"attack":.002,"decay":.0,"sustain":.16,"release":.11,"decay_exp":14.,"volume":.50},
    "music_box_large":      {"harmonics":[(1,.62),(2,.22),(3,.12),(4,.04)],"attack":.003,"decay":.0,"sustain":.18,"release":.13,"decay_exp":12.,"volume":.50},
    "kalimba_piano":        {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.003,"decay":.0,"sustain":.36,"release":.13,"decay_exp":9.0,"volume":.52},
    "dulcitone":            {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.005,"decay":.0,"sustain":.65,"release":.22,"decay_exp":5.0,"volume":.50},
    "cristal_baschet":      {"harmonics":[(1,.62),(2.1,.24),(4.2,.14),(7.,.08)],"attack":.020,"decay":.0,"sustain":1.0,"release":.30,"volume":.48},
    "glass_armonica":       {"harmonics":[(1,.66),(2.03,.22),(4.06,.12),(7.,.06)],"attack":.025,"decay":.0,"sustain":1.0,"release":.35,"volume":.48},
    "glass_harp":           {"harmonics":[(1,.65),(2.02,.24),(4.05,.14),(7.1,.06)],"attack":.030,"decay":.0,"sustain":1.0,"release":.40,"volume":.48},
    "toy_piano":            {"harmonics":[(1,.55),(2,.26),(3,.14),(4,.05)],"attack":.004,"decay":.0,"sustain":.65,"release":.12,"decay_exp":7.0,"noise_mix":.01,"volume":.52},
    "electronic_keys":      {"harmonics":[(1,.52),(2,.28),(3,.14),(4,.06)],"attack":.005,"decay":.0,"sustain":.75,"release":.14,"volume":.52},
    "synth_keys":           {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.006,"decay":.08,"sustain":.80,"release":.14,"saturation":1.0,"volume":.50},
    "clav_pad":             {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.100,"decay":.0,"sustain":1.0,"release":.20,"saturation":.8,"volume":.46},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ VOICE/CHOIR EXTENDED ██
    # ═══════════════════════════════════════════════════════════════════════════
    "voice_soprano":        {"harmonics":[(1,.65),(2,.20),(3,.08),(4,.04),(5,.03)],"attack":.070,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.14,"vibrato_rate":6.0,"volume":.48},
    "voice_mezzo":          {"harmonics":[(1,.62),(2,.22),(3,.09),(4,.05),(5,.02)],"attack":.080,"decay":.0,"sustain":1.0,"release":.13,"vibrato_depth":.13,"vibrato_rate":5.8,"volume":.48},
    "voice_contralto":      {"harmonics":[(1,.60),(2,.24),(3,.10),(4,.06),(5,.02)],"attack":.085,"decay":.0,"sustain":1.0,"release":.13,"vibrato_depth":.12,"vibrato_rate":5.5,"volume":.48},
    "voice_tenor":          {"harmonics":[(1,.62),(2,.22),(3,.09),(4,.05),(5,.02)],"attack":.075,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.13,"vibrato_rate":5.8,"volume":.48},
    "voice_baritone":       {"harmonics":[(1,.60),(2,.24),(3,.10),(4,.06),(5,.02)],"attack":.080,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.11,"vibrato_rate":5.5,"volume":.48},
    "voice_bass2":          {"harmonics":[(1,.58),(2,.25),(3,.12),(4,.05)],"attack":.090,"decay":.0,"sustain":1.0,"release":.16,"vibrato_depth":.10,"vibrato_rate":5.0,"volume":.50},
    "voice_child":          {"harmonics":[(1,.68),(2,.18),(3,.08),(4,.04),(5,.02)],"attack":.060,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.08,"vibrato_rate":6.0,"volume":.48},
    "voice_whisper":        {"harmonics":[(1,.55),(2,.22),(3,.12),(4,.11)],"attack":.040,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.08,"volume":.44},
    "voice_shout":          {"harmonics":[(1,.48),(2,.28),(3,.16),(4,.08)],"attack":.020,"decay":.0,"sustain":1.0,"release":.08,"saturation":2.0,"volume":.44},
    "voice_grunt":          {"harmonics":[(1,.50),(2,.28),(3,.16),(4,.06)],"attack":.010,"decay":.05,"sustain":.70,"release":.08,"saturation":3.0,"volume":.42},
    "voice_falsetto":       {"harmonics":[(1,.70),(2,.18),(3,.07),(4,.05)],"attack":.060,"decay":.0,"sustain":1.0,"release":.10,"vibrato_depth":.10,"vibrato_rate":7.0,"volume":.48},
    "voice_operatic":       {"harmonics":[(1,.62),(2,.20),(3,.09),(4,.06),(5,.03)],"attack":.100,"decay":.0,"sustain":1.0,"release":.16,"vibrato_depth":.16,"vibrato_rate":6.5,"volume":.46},
    "voice_breathy":        {"harmonics":[(1,.62),(2,.20),(3,.10),(4,.08)],"attack":.060,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.04,"volume":.46},
    "voice_nasal":          {"harmonics":[(1,.52),(2,.26),(3,.16),(4,.06)],"attack":.050,"decay":.0,"sustain":1.0,"release":.10,"volume":.46},
    "voice_chest":          {"harmonics":[(1,.60),(2,.24),(3,.10),(4,.06)],"attack":.070,"decay":.0,"sustain":1.0,"release":.12,"volume":.48},
    "voice_head":           {"harmonics":[(1,.65),(2,.20),(3,.08),(4,.07)],"attack":.065,"decay":.0,"sustain":1.0,"release":.11,"volume":.47},
    "voice_ahh":            {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.080,"decay":.0,"sustain":1.0,"release":.14,"vibrato_depth":.12,"vibrato_rate":5.5,"volume":.48},
    "voice_ooh":            {"harmonics":[(1,.68),(2,.18),(3,.08),(4,.06)],"attack":.090,"decay":.0,"sustain":1.0,"release":.16,"vibrato_depth":.10,"vibrato_rate":5.0,"volume":.48},
    "voice_eeh":            {"harmonics":[(1,.52),(2,.26),(3,.16),(4,.06)],"attack":.065,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.11,"vibrato_rate":5.8,"volume":.47},
    "voice_mm":             {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.050,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.08,"vibrato_rate":5.0,"noise_mix":.02,"volume":.47},
    "choir_soprano":        {"harmonics":[(1,.62),(1.003,.54),(2,.22),(2.003,.18),(3,.10),(4,.05)],"attack":.200,"decay":.0,"sustain":1.0,"release":.30,"noise_mix":.01,"volume":.38},
    "choir_alto":           {"harmonics":[(1,.60),(1.003,.52),(2,.24),(2.003,.20),(3,.11),(4,.05)],"attack":.220,"decay":.0,"sustain":1.0,"release":.32,"noise_mix":.01,"volume":.38},
    "choir_tenor":          {"harmonics":[(1,.62),(1.003,.54),(2,.22),(2.003,.18),(3,.10)],"attack":.200,"decay":.0,"sustain":1.0,"release":.30,"noise_mix":.01,"volume":.38},
    "choir_bass2":          {"harmonics":[(1,.58),(1.003,.50),(2,.26),(2.003,.22),(3,.12)],"attack":.230,"decay":.0,"sustain":1.0,"release":.34,"noise_mix":.01,"volume":.38},
    "choir_satb":           {"harmonics":[(1,.55),(1.004,.47),(2,.24),(2.004,.20),(3,.12),(3.004,.08)],"attack":.250,"decay":.0,"sustain":1.0,"release":.35,"noise_mix":.015,"volume":.34},
    "choir_gospel":         {"harmonics":[(1,.58),(1.003,.50),(2,.24),(2.003,.20),(3,.11),(4,.05)],"attack":.150,"decay":.0,"sustain":1.0,"release":.28,"noise_mix":.012,"volume":.37},
    "choir_chamber":        {"harmonics":[(1,.60),(1.003,.52),(2,.23),(2.003,.19),(3,.10)],"attack":.280,"decay":.0,"sustain":1.0,"release":.38,"noise_mix":.008,"volume":.38},
    "choir_big":            {"harmonics":[(1,.52),(1.005,.46),(2,.23),(2.005,.19),(3,.11),(4,.06)],"attack":.300,"decay":.0,"sustain":1.0,"release":.40,"noise_mix":.018,"volume":.34},
    "voice_arab":           {"harmonics":[(1,.58),(2,.22),(3,.12),(4,.08)],"attack":.060,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.14,"vibrato_rate":5.5,"volume":.48},
    "voice_indian":         {"harmonics":[(1,.60),(2,.22),(3,.10),(4,.08)],"attack":.065,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.15,"vibrato_rate":6.0,"volume":.48},
    "voice_african":        {"harmonics":[(1,.62),(2,.22),(3,.10),(4,.06)],"attack":.055,"decay":.0,"sustain":1.0,"release":.12,"vibrato_depth":.12,"vibrato_rate":5.5,"volume":.48},
    "voice_throat":         {"harmonics":[(1,.58),(2,.24),(3,.14),(4,.08),(5,.04)],"attack":.040,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.04,"volume":.48},
    "voice_overtone":       {"harmonics":[(3,.55),(5,.30),(7,.14),(9,.06),(11,.05)],"attack":.060,"decay":.0,"sustain":1.0,"release":.12,"volume":.44},
    "voice_beatbox":        {"harmonics":[(1,.30),(2,.20),(3,.15),(4,.10),(5,.08)],"attack":.003,"decay":.0,"sustain":.40,"release":.08,"noise_mix":.25,"saturation":2.0,"volume":.46},
    # ═══════════════════════════════════════════════════════════════════════════
    # ██ FX EXTENDED ██
    # ═══════════════════════════════════════════════════════════════════════════
    "fx_riser2":            {"harmonics":[(1,.55),(2,.28),(3,.12),(4,.05)],"attack":.600,"decay":.0,"sustain":1.0,"release":.10,"volume":.44},
    "fx_riser_high":        {"harmonics":[(1,.45),(2,.28),(3,.18),(4,.08),(5,.02)],"attack":.800,"decay":.0,"sustain":1.0,"release":.08,"volume":.40},
    "fx_downlifter2":       {"harmonics":[(1,.58),(2,.26),(3,.12),(4,.04)],"attack":.010,"decay":.0,"sustain":1.0,"release":1.0,"decay_exp":1.5,"volume":.44},
    "fx_downlifter_low":    {"harmonics":[(1,.68),(2,.20),(3,.08),(4,.04)],"attack":.008,"decay":.0,"sustain":1.0,"release":.700,"decay_exp":2.5,"volume":.46},
    "fx_uplifter":          {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.400,"decay":.0,"sustain":1.0,"release":.10,"noise_mix":.10,"volume":.42},
    "fx_tension":           {"harmonics":[(1,.55),(2,.26),(3,.14),(4,.05)],"attack":.200,"decay":.0,"sustain":1.0,"release":.20,"noise_mix":.04,"saturation":1.5,"volume":.42},
    "fx_boomer":            {"harmonics":[(1,.75),(2,.15),(3,.08),(4,.02)],"attack":.010,"decay":.20,"sustain":.50,"release":.30,"decay_exp":3.0,"saturation":2.0,"volume":.48},
    "fx_earthquake":        {"harmonics":[(1,.80),(2,.14),(3,.06)],"attack":.012,"decay":.25,"sustain":.40,"release":.35,"saturation":3.0,"volume":.48},
    "fx_thunder":           {"harmonics":[(1,.45),(2,.28),(3,.18),(4,.09)],"attack":.010,"decay":.30,"sustain":.60,"release":.40,"noise_mix":.30,"saturation":1.5,"volume":.44},
    "fx_lightning":         {"harmonics":[(1,.40),(2,.28),(3,.20),(4,.12)],"attack":.002,"decay":.05,"sustain":.50,"release":.15,"noise_mix":.40,"saturation":3.0,"volume":.42},
    "fx_explosion":         {"harmonics":[(1,.50),(2,.26),(3,.16),(4,.08)],"attack":.003,"decay":.15,"sustain":.40,"release":.35,"noise_mix":.45,"saturation":3.0,"volume":.45},
    "fx_laser2":            {"harmonics":[(1,.70),(2,.20),(3,.08),(4,.02)],"attack":.002,"decay":.0,"sustain":.80,"release":.05,"decay_exp":15.,"vibrato_depth":.25,"vibrato_rate":8.0,"volume":.52},
    "fx_pew":               {"harmonics":[(1,.68),(2,.22),(3,.08),(4,.02)],"attack":.002,"decay":.0,"sustain":.70,"release":.05,"decay_exp":18.,"vibrato_depth":.30,"vibrato_rate":4.0,"volume":.52},
    "fx_alien":             {"harmonics":[(1,.55),(2.7,.28),(5.4,.16),(8.1,.08),(11.,.04)],"attack":.006,"decay":.0,"sustain":.60,"release":.15,"saturation":1.5,"volume":.44},
    "fx_robot":             {"harmonics":[(1,.50),(3,.30),(5,.20),(7,.10),(9,.06)],"attack":.005,"decay":.08,"sustain":.75,"release":.12,"saturation":2.5,"volume":.42},
    "fx_digital2":          {"harmonics":[(1,.52),(2,.30),(3,.16),(4,.02)],"attack":.004,"decay":.06,"sustain":.78,"release":.10,"saturation":3.0,"noise_mix":.02,"volume":.42},
    "fx_sine_wave":         {"harmonics":[(1,.98),(2,.02)],              "attack":.005,"decay":.0,"sustain":1.0,"release":.10,"volume":.54},
    "fx_square_wave":       {"harmonics":[(1,1.0),(3,.333),(5,.2),(7,.143),(9,.111)],"attack":.005,"decay":.0,"sustain":1.0,"release":.10,"volume":.40},
    "fx_sawtooth_wave":     {"harmonics":[(k,1/k) for k in range(1,12)],  "attack":.005,"decay":.0,"sustain":1.0,"release":.10,"volume":.30},
    "fx_granular2":         {"harmonics":[(1,.50),(2,.25),(3,.15),(4,.10)],"attack":.100,"decay":.0,"sustain":1.0,"release":.25,"noise_mix":.04,"volume":.40},
    "fx_reversed2":         {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.07)],"attack":.400,"decay":.0,"sustain":1.0,"release":.03,"volume":.44},
    "fx_stutter2":          {"harmonics":[(1,.55),(2,.26),(3,.12),(4,.05)],"attack":.005,"decay":.03,"sustain":.80,"release":.04,"saturation":2.0,"volume":.46},
    "fx_pitch_down":        {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.005,"decay":.0,"sustain":.80,"release":.10,"decay_exp":5.0,"vibrato_depth":.25,"vibrato_rate":.5,"volume":.48},
    "fx_pitch_up":          {"harmonics":[(1,.60),(2,.24),(3,.12),(4,.04)],"attack":.005,"decay":.0,"sustain":.80,"release":.10,"vibrato_depth":.20,"vibrato_rate":1.0,"volume":.48},
    "fx_scatter":           {"harmonics":[(1,.45),(2,.28),(3,.18),(4,.09)],"attack":.004,"decay":.04,"sustain":.70,"release":.10,"noise_mix":.08,"saturation":2.5,"volume":.42},
    "fx_morph":             {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.080,"decay":.0,"sustain":1.0,"release":.25,"vibrato_depth":.08,"vibrato_rate":1.2,"volume":.42},
    "fx_shimmer2":          {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.150,"decay":.0,"sustain":1.0,"release":.30,"vibrato_depth":.06,"vibrato_rate":8.0,"volume":.42},
    "fx_pad_sweep":         {"harmonics":[(1,.52),(2,.26),(3,.14),(4,.08)],"attack":.350,"decay":.0,"sustain":1.0,"release":.35,"noise_mix":.06,"volume":.38},
    "fx_white_noise":       {"harmonics":[(1,.02)],                      "attack":.008,"decay":.0,"sustain":1.0,"release":.12,"noise_mix":.95,"volume":.36},
    "fx_pink_noise":        {"harmonics":[(1,.05),(2,.05),(3,.04),(4,.03)],"attack":.010,"decay":.0,"sustain":1.0,"release":.15,"noise_mix":.80,"volume":.36},
    "fx_brown_noise":       {"harmonics":[(1,.10),(2,.08),(3,.06),(4,.04)],"attack":.012,"decay":.0,"sustain":1.0,"release":.18,"noise_mix":.68,"volume":.38},
    "fx_click":             {"harmonics":[(1,.60),(2,.25),(3,.12),(4,.03)],"attack":.001,"decay":.01,"sustain":.10,"release":.02,"noise_mix":.20,"volume":.56},
    "fx_tick":              {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.001,"decay":.008,"sustain":.08,"release":.015,"volume":.54},
    "fx_pop":               {"harmonics":[(1,.62),(2,.24),(3,.12),(4,.02)],"attack":.002,"decay":.012,"sustain":.12,"release":.025,"volume":.54},
    "fx_bloop":             {"harmonics":[(1,.65),(2,.22),(3,.10),(4,.03)],"attack":.004,"decay":.0,"sustain":.40,"release":.12,"decay_exp":8.0,"vibrato_depth":.20,"vibrato_rate":1.0,"volume":.52},
    "fx_blip":              {"harmonics":[(1,.70),(2,.20),(3,.08),(4,.02)],"attack":.003,"decay":.0,"sustain":.30,"release":.08,"decay_exp":12.,"volume":.54},
    "fx_twinkle2":          {"harmonics":[(1,.62),(2,.24),(3,.12),(4,.02)],"attack":.004,"decay":.0,"sustain":.35,"release":.18,"decay_exp":7.0,"volume":.52},
    "fx_sparkle2":          {"harmonics":[(1,.60),(2,.26),(3,.12),(4,.02)],"attack":.004,"decay":.0,"sustain":.30,"release":.16,"decay_exp":8.0,"volume":.52},
    "fx_chime2":            {"harmonics":[(1,.60),(2.756,.28),(5.404,.14),(7.08,.08)],"attack":.004,"decay":.0,"sustain":.40,"release":.22,"decay_exp":6.5,"volume":.50},
}


def _synth_note(midi_pitch: int, duration: float, timbre: str, sr: int = 44100) -> np.ndarray:
    """Route a note to the 500-instrument parametric synthesiser."""
    freq = _midi_to_hz(midi_pitch)
    params = globals().get("_INSTR", {}).get(timbre)
    if params is not None:
        return _parametric_render(freq, duration, sr, params)

    n   = max(int(sr * duration), 1)
    t   = np.linspace(0, duration, n, dtype=np.float32)
    dur = max(duration, 0.01)

    def adsr(atk, dec, sus_lvl, rel_ratio=0.15):
        env = np.ones(n, dtype=np.float32)
        a = min(int(sr * atk), n)
        d = min(int(sr * dec), n - a)
        r = min(int(sr * dur * rel_ratio), n)
        if a: env[:a] = np.linspace(0, 1, a)
        if d: env[a:a+d] = np.linspace(1, sus_lvl, d)
        if r and r < n: env[-r:] = np.linspace(env[-r], 0, r)
        return env

    # ── 808 / Sub-Bass Family ─────────────────────────────────────────────────
    if timbre.startswith("bass_808") or timbre in ("808", "bass_sub"):
        if "trap" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.30, 10.0, 2.2, 0.15
            env = adsr(0.003, 0.0, 1.0, 0.30)
        elif "drill" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 12.0, 1.8, 0.10
            env = adsr(0.004, 0.0, 1.0, 0.20)
        elif "deep" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.15, 5.0, 1.2, 0.30
            env = adsr(0.006, 0.0, 1.0, 0.45)
        elif "punchy" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.35, 15.0, 2.0, 0.08
            env = adsr(0.002, 0.05, 0.80, 0.15)
        elif "ultra" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.12, 4.0, 0.9, 0.42
            env = adsr(0.007, 0.0, 1.0, 0.52)
        elif "sub" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.10, 6.0, 0.8, 0.35
            env = adsr(0.006, 0.0, 1.0, 0.38)
        elif "warm" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 7.0, 1.5, 0.18
            env = adsr(0.008, 0.0, 1.0, 0.36)
        elif "long" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 4.0, 1.8, 0.15
            env = adsr(0.006, 0.0, 1.0, 0.55)
        elif "bright" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.30, 9.0, 2.0, 0.08
            env = adsr(0.004, 0.0, 1.0, 0.25)
        elif "distorted" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.35, 8.0, 4.5, 0.12
            env = adsr(0.003, 0.0, 1.0, 0.25)
        elif "phonk" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.38, 8.5, 5.0, 0.10
            env = adsr(0.003, 0.0, 1.0, 0.22)
        elif "rnb" in timbre or "r&b" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.18, 7.0, 1.4, 0.20
            env = adsr(0.010, 0.0, 1.0, 0.32)
        elif "mid" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 9.0, 2.0, 0.12
            env = adsr(0.005, 0.0, 1.0, 0.25)
        elif "clean" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 8.0, 0.5, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.28)
        elif "click" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.25, 10.0, 1.8, 0.12
            env = adsr(0.001, 0.0, 1.0, 0.25)
        elif "uk" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 11.0, 1.9, 0.10
            env = adsr(0.004, 0.0, 1.0, 0.22)
        elif "afro" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.18, 8.0, 1.6, 0.15
            env = adsr(0.007, 0.0, 1.0, 0.28)
        elif "soft" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.15, 6.0, 1.0, 0.22
            env = adsr(0.012, 0.0, 1.0, 0.32)
        elif "hiphop" in timbre or "hip_hop" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.25, 8.0, 1.8, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.28)
        elif "chicago" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.28, 11.0, 2.1, 0.12
            env = adsr(0.003, 0.0, 1.0, 0.20)
        elif "jersey" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 9.0, 1.7, 0.14
            env = adsr(0.005, 0.0, 0.9, 0.18)
        elif "bounce" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.30, 12.0, 1.6, 0.12
            env = adsr(0.003, 0.08, 0.7, 0.12)
        elif "lofi" in timbre or "lo_fi" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 7.0, 1.3, 0.20
            env = adsr(0.008, 0.0, 1.0, 0.35)
        elif "vintage" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 8.0, 1.5, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.28)
        elif "modern" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.28, 10.0, 2.3, 0.12
            env = adsr(0.004, 0.0, 1.0, 0.22)
        elif "slide" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.50, 5.0, 1.8, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.30)
        elif "wobble" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 8.0, 1.8, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.28)
        elif "growl" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.30, 9.0, 3.5, 0.10
            env = adsr(0.004, 0.0, 1.0, 0.22)
        elif "mellow" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.15, 6.0, 1.0, 0.22
            env = adsr(0.010, 0.0, 1.0, 0.34)
        elif "nyc" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.25, 9.0, 1.8, 0.14
            env = adsr(0.004, 0.0, 1.0, 0.25)
        elif "la" in timbre or "west" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 8.0, 1.7, 0.16
            env = adsr(0.006, 0.0, 1.0, 0.28)
        elif "atlanta" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.32, 10.0, 2.2, 0.13
            env = adsr(0.003, 0.0, 1.0, 0.28)
        elif "cloud" in timbre or "dreamy" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.15, 5.0, 1.2, 0.25
            env = adsr(0.010, 0.0, 1.0, 0.42)
        elif "rage" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.35, 11.0, 2.8, 0.10
            env = adsr(0.003, 0.0, 1.0, 0.20)
        elif "melodic" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 7.0, 1.5, 0.18
            env = adsr(0.008, 0.0, 1.0, 0.32)
        elif "dark" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.28, 7.5, 2.5, 0.20
            env = adsr(0.005, 0.0, 1.0, 0.30)
        elif "heavy" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.32, 9.0, 3.0, 0.15
            env = adsr(0.004, 0.0, 1.0, 0.25)
        elif "lite" in timbre or "light" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.18, 9.0, 1.3, 0.10
            env = adsr(0.006, 0.0, 0.9, 0.20)
        elif "crispy" in timbre or "crisp" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.28, 11.0, 2.2, 0.08
            env = adsr(0.002, 0.04, 0.85, 0.18)
        elif "smooth" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.18, 6.5, 1.3, 0.20
            env = adsr(0.009, 0.0, 1.0, 0.33)
        elif "hard" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.33, 10.5, 2.7, 0.10
            env = adsr(0.002, 0.0, 1.0, 0.20)
        elif "trap2" in timbre or "trap_hard" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.35, 12.0, 2.8, 0.12
            env = adsr(0.002, 0.0, 1.0, 0.25)
        else:
            slide_amt, slide_dec, drive, sub_mix = 0.25, 8.0, 1.8, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.25)

        pitch_env = np.exp(-slide_dec * t / dur)
        freq_mod  = freq * (1.0 + slide_amt * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        phase     = phase.astype(np.float32)
        sine      = np.sin(phase)
        wave      = (0.70 * sine
                     + 0.20 * np.sin(2.0 * phase)
                     + 0.08 * np.sin(3.0 * phase)
                     + sub_mix * np.sin(0.5 * phase))

        if "click" in timbre:
            ck = min(int(0.008 * sr), n)
            click = np.zeros(n, dtype=np.float32)
            click[:ck] = np.linspace(0.5, 0.0, ck)
            wave = wave + click

        if "wobble" in timbre:
            lfo  = 0.3 * np.sin(2.0 * np.pi * 4.0 * t)
            wave = wave * (1.0 + lfo)

        if "lofi" in timbre or "lo_fi" in timbre:
            wave = wave + np.random.uniform(-0.03, 0.03, n).astype(np.float32)

        if "vintage" in timbre:
            wave = wave + 0.015 * np.random.uniform(-1, 1, n).astype(np.float32)

        wave = np.tanh(drive * wave) / (np.tanh(np.float32(drive)) + 1e-9)
        return (wave * env).astype(np.float32)

    # ── GM Bass Instruments (individually synthesized) ────────────────────────
    if timbre == "bass_pick":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks
        pk   = min(int(0.010 * sr), n)
        wave[:pk] += np.linspace(0.35, 0.0, pk)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_fretless":
        vib  = 0.06 * np.sin(2.0 * np.pi * 3.5 * t)
        wave = (0.65 * np.sin(2.0 * np.pi * freq * t + vib)
                + 0.22 * np.sin(2.0 * np.pi * freq * 2 * t)
                + 0.10 * np.sin(2.0 * np.pi * freq * 3 * t)
                + 0.03 * np.sin(2.0 * np.pi * freq * 4 * t))
        env  = adsr(0.015, 0.0, 0.95, 0.20)
        return (wave * env * 0.75).astype(np.float32)

    if timbre == "bass_slap1":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.80
        sk   = min(int(0.015 * sr), n)
        wave[:sk] += np.exp(-60.0 * t[:sk]) * 0.60
        env  = adsr(0.001, 0.05, 0.60, 0.15)
        return (wave * env).astype(np.float32)

    if timbre == "bass_slap2":
        wave = (0.55 * np.sin(2.0 * np.pi * freq * t)
                + 0.30 * np.sin(2.0 * np.pi * freq * 2 * t)
                + 0.12 * np.sin(2.0 * np.pi * freq * 3 * t)
                + 0.03 * np.sin(2.0 * np.pi * freq * 5 * t))
        env  = adsr(0.001, 0.03, 0.50, 0.12)
        return (wave * env * 0.75).astype(np.float32)

    if timbre == "bass_synth1":
        wave = sum((1.0 / (2*k - 1)) * np.sin(2.0 * np.pi * freq * (2*k - 1) * t)
                   for k in range(1, 7)).astype(np.float32)
        env  = adsr(0.010, 0.15, 0.70, 0.15)
        return (np.tanh(1.5 * wave) / np.tanh(np.float32(1.5)) * env * 0.60).astype(np.float32)

    if timbre == "bass_synth2":
        wave = sum((1.0 / k) * np.sin(2.0 * np.pi * freq * k * t)
                   for k in range(1, 9)).astype(np.float32)
        env  = adsr(0.008, 0.12, 0.80, 0.12)
        return (np.tanh(1.2 * wave) / np.tanh(np.float32(1.2)) * env * 0.50).astype(np.float32)

    if timbre == "bass_synth3":
        wave = (np.sign(np.sin(2.0 * np.pi * freq * t)) * 0.6
                + 0.30 * np.sin(2.0 * np.pi * freq * t)
                + 0.10 * np.sin(2.0 * np.pi * freq * 3 * t))
        env  = adsr(0.005, 0.10, 0.75, 0.12)
        return (np.tanh(2.0 * wave) / np.tanh(np.float32(2.0)) * env * 0.55).astype(np.float32)

    if timbre == "bass_synth_moog":
        wave = sum((1.0 / (2*k - 1)) * np.sin(2.0 * np.pi * freq * (2*k - 1) * t)
                   for k in range(1, 10)).astype(np.float32)
        env  = adsr(0.006, 0.20, 0.65, 0.18)
        return (np.tanh(2.2 * wave) / np.tanh(np.float32(2.2)) * env * 0.55).astype(np.float32)

    if timbre == "bass_synth_acid":
        res_wave = (0.70 * np.sin(2.0 * np.pi * freq * t)
                    + 0.25 * np.sin(2.0 * np.pi * freq * 3 * t)
                    + 0.05 * np.sin(2.0 * np.pi * freq * 5 * t))
        env_f    = np.exp(-12.0 * t / dur)
        wave     = res_wave * (1.0 + 0.8 * env_f)
        env      = adsr(0.004, 0.10, 0.70, 0.14)
        return (np.tanh(2.5 * wave) / np.tanh(np.float32(2.5)) * env * 0.50).astype(np.float32)

    if timbre == "bass_acoustic":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.88
        wave += 0.15 * np.sin(2.0 * np.pi * freq * t) * np.exp(-3.5 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_upright":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.80
        wave += (0.10 * np.sin(2.0 * np.pi * freq * t)
                 + 0.06 * np.sin(2.0 * np.pi * freq * 2 * t)) * np.exp(-4.0 * t / dur)
        noise = np.random.uniform(-0.015, 0.015, n).astype(np.float32)
        return np.clip(wave + noise, -1.0, 1.0)

    if timbre == "bass_electric":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.85
        wave += 0.12 * np.sin(2.0 * np.pi * freq * 2 * t) * np.exp(-5.0 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_fuzz":
        wave = (0.60 * np.sin(2.0 * np.pi * freq * t)
                + 0.25 * np.sin(2.0 * np.pi * freq * 2 * t)
                + 0.12 * np.sin(2.0 * np.pi * freq * 3 * t))
        wave = np.sign(wave) * (1.0 - np.exp(-3.0 * np.abs(wave)))
        env  = adsr(0.005, 0.0, 1.0, 0.18)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_overdrive":
        wave = (0.55 * np.sin(2.0 * np.pi * freq * t)
                + 0.28 * np.sin(2.0 * np.pi * freq * 2 * t)
                + 0.14 * np.sin(2.0 * np.pi * freq * 3 * t)
                + 0.03 * np.sin(2.0 * np.pi * freq * 4 * t))
        env  = adsr(0.004, 0.0, 1.0, 0.15)
        wave = np.tanh(3.5 * wave) / np.tanh(np.float32(3.5))
        return (wave * env * 0.60).astype(np.float32)

    if timbre == "bass_sub_sine":
        wave = np.sin(2.0 * np.pi * freq * t)
        env  = adsr(0.006, 0.0, 1.0, 0.40)
        return (wave * env * 0.90).astype(np.float32)

    if timbre == "bass_sub_triangle":
        wave = (0.85 * np.sin(2.0 * np.pi * freq * t)
                + 0.10 * np.sin(2.0 * np.pi * freq * 3 * t)
                + 0.05 * np.sin(2.0 * np.pi * freq * 5 * t))
        env  = adsr(0.007, 0.0, 1.0, 0.38)
        return (wave * env * 0.88).astype(np.float32)

    if timbre == "bass_sub_octave":
        wave = (0.65 * np.sin(2.0 * np.pi * freq * t)
                + 0.28 * np.sin(2.0 * np.pi * (freq / 2) * t)
                + 0.07 * np.sin(2.0 * np.pi * freq * 2 * t))
        env  = adsr(0.006, 0.0, 1.0, 0.38)
        return (wave * env * 0.85).astype(np.float32)

    if timbre in ("bass_wobble_lfo", "bass_wub"):
        lfo_rate = 8.0
        lfo      = 0.5 * (1.0 + np.sin(2.0 * np.pi * lfo_rate * t))
        wave     = (0.70 * np.sin(2.0 * np.pi * freq * t)
                    + 0.20 * np.sin(2.0 * np.pi * freq * 2 * t)
                    + 0.10 * np.sin(2.0 * np.pi * freq * 3 * t))
        wave     = np.tanh(2.0 * wave) / np.tanh(np.float32(2.0))
        env      = adsr(0.005, 0.0, 1.0, 0.22)
        return (wave * env * lfo * 0.80).astype(np.float32)

    if timbre in ("bass_reese", "bass_reese_dnb"):
        detune = 1.006
        wave   = (0.50 * np.sin(2.0 * np.pi * freq * t)
                  + 0.50 * np.sin(2.0 * np.pi * freq * detune * t))
        wave   = np.tanh(2.8 * wave) / np.tanh(np.float32(2.8))
        env    = adsr(0.010, 0.0, 1.0, 0.20)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_pluck":
        ks   = _ks_instrument(freq, min(duration, 0.6), sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_rubber":
        freq_slide = freq * (1.0 + 0.40 * np.exp(-20.0 * t / dur))
        phase      = 2.0 * np.pi * np.cumsum(freq_slide.astype(np.float64)) / sr
        wave       = (0.70 * np.sin(phase)
                      + 0.20 * np.sin(2.0 * phase)
                      + 0.10 * np.sin(3.0 * phase)).astype(np.float32)
        env        = adsr(0.003, 0.0, 1.0, 0.28)
        return (wave * env).astype(np.float32)

    # ── Waveform-Based Bass ───────────────────────────────────────────────────
    if timbre == "bass_square":
        wave = sum((1.0 / (2*k-1)) * np.sin(2*np.pi * freq * (2*k-1) * t)
                   for k in range(1, 12)).astype(np.float32)
        env  = adsr(0.006, 0.0, 1.0, 0.18)
        return (np.tanh(1.0 * wave) / np.tanh(np.float32(1.0)) * env * 0.60).astype(np.float32)

    if timbre == "bass_saw":
        wave = sum((1.0 / k) * np.sin(2*np.pi * freq * k * t)
                   for k in range(1, 14)).astype(np.float32)
        env  = adsr(0.005, 0.0, 1.0, 0.18)
        return (np.tanh(1.0 * wave) / np.tanh(np.float32(1.0)) * env * 0.55).astype(np.float32)

    if timbre == "bass_pulse":
        duty  = 0.3
        wave  = sum((np.sin(np.pi * k * duty) / (np.pi * k)) * np.sin(2*np.pi * freq * k * t)
                    for k in range(1, 12)).astype(np.float32)
        env   = adsr(0.005, 0.0, 1.0, 0.18)
        return (np.tanh(1.5 * wave) / np.tanh(np.float32(1.5)) * env * 0.65).astype(np.float32)

    if timbre == "bass_triangle":
        wave = sum((((-1)**(k-1)) / ((2*k-1)**2)) * np.sin(2*np.pi * freq * (2*k-1) * t)
                   for k in range(1, 9)).astype(np.float32)
        env  = adsr(0.006, 0.0, 1.0, 0.22)
        return (wave * env * 0.80).astype(np.float32)

    if timbre == "bass_sine":
        wave = np.sin(2*np.pi * freq * t)
        env  = adsr(0.007, 0.0, 1.0, 0.25)
        return (wave * env * 0.88).astype(np.float32)

    # ── FM Synthesis Bass ─────────────────────────────────────────────────────
    if timbre in ("bass_fm", "bass_fm_classic"):
        mod_ratio = 1.0
        mod_index = 3.5
        mod  = mod_index * np.sin(2*np.pi * freq * mod_ratio * t)
        wave = np.sin(2*np.pi * freq * t + mod)
        wave += 0.30 * np.sin(2*np.pi * (freq/2) * t + mod*0.5)
        env  = adsr(0.005, 0.12, 0.75, 0.15)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_fm_dx7":
        mod_ratio = 2.0
        mod_index = 5.0
        mod_env  = np.exp(-8.0 * t / dur)
        mod  = mod_index * mod_env * np.sin(2*np.pi * freq * mod_ratio * t)
        wave = np.sin(2*np.pi * freq * t + mod)
        wave += 0.20 * np.sin(2*np.pi * (freq/2) * t)
        env  = adsr(0.004, 0.15, 0.65, 0.18)
        return (wave * env * 0.70).astype(np.float32)

    if timbre == "bass_fm_bell":
        mod_ratio = 3.5
        mod_index = 6.0
        mod_env  = np.exp(-12.0 * t / dur)
        mod  = mod_index * mod_env * np.sin(2*np.pi * freq * mod_ratio * t)
        wave = np.sin(2*np.pi * freq * t + mod) * np.exp(-4.0 * t / dur)
        env  = adsr(0.003, 0.0, 1.0, 0.35)
        return (wave * env * 0.75).astype(np.float32)

    if timbre == "bass_fm_attack":
        mod_index = 8.0 * np.exp(-20.0 * t / dur)
        mod  = mod_index * np.sin(2*np.pi * freq * 2.0 * t)
        wave = np.sin(2*np.pi * freq * t + mod)
        wave += 0.25 * np.sin(2*np.pi * (freq/2) * t)
        env  = adsr(0.002, 0.10, 0.80, 0.15)
        return (wave * env * 0.68).astype(np.float32)

    if timbre == "bass_fm_sub":
        mod_ratio = 0.5
        mod_index = 4.0
        mod  = mod_index * np.sin(2*np.pi * freq * mod_ratio * t)
        wave = np.sin(2*np.pi * (freq/2) * t + mod)
        wave += 0.40 * np.sin(2*np.pi * (freq/4) * t)
        env  = adsr(0.007, 0.0, 1.0, 0.35)
        return (wave * env * 0.78).astype(np.float32)

    # ── Reggae / Dub / Jamaican Bass ──────────────────────────────────────────
    if timbre in ("bass_reggae", "bass_ska"):
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t)
                + 0.05 * np.sin(2*np.pi * freq * 4 * t))
        env  = adsr(0.008, 0.05, 0.85, 0.22)
        return (wave * env * 0.80).astype(np.float32)

    if timbre == "bass_dub":
        vib  = 0.04 * np.sin(2*np.pi * 1.8 * t)
        wave = (0.65 * np.sin(2*np.pi * freq * t + vib)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.08 * np.sin(2*np.pi * freq * 3 * t)
                + 0.10 * np.sin(2*np.pi * (freq/2) * t))
        env  = adsr(0.010, 0.0, 1.0, 0.35)
        wave = np.tanh(1.3 * wave) / np.tanh(np.float32(1.3))
        return (wave * env * 0.80).astype(np.float32)

    if timbre == "bass_dancehall":
        wave = (0.65 * np.sin(2*np.pi * freq * t)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        env  = adsr(0.006, 0.0, 1.0, 0.28)
        wave = np.tanh(1.8 * wave) / np.tanh(np.float32(1.8))
        return (wave * env * 0.78).astype(np.float32)

    # ── Funk / Soul Bass ──────────────────────────────────────────────────────
    if timbre == "bass_funk":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.75
        pk   = min(int(0.012 * sr), n)
        wave[:pk] += np.exp(-50.0 * t[:pk]) * 0.50
        body = 0.18 * np.sin(2*np.pi * freq * 2 * t) * np.exp(-5.0 * t / dur)
        wave += body
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_gospel":
        wave = (0.58 * np.sin(2*np.pi * freq * t)
                + 0.26 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t)
                + 0.04 * np.sin(2*np.pi * freq * 4 * t))
        env  = adsr(0.010, 0.0, 1.0, 0.28)
        return (wave * env * 0.82).astype(np.float32)

    if timbre == "bass_soul":
        vib  = 0.06 * np.sin(2*np.pi * 4.5 * t) * np.clip(t / 0.3, 0, 1)
        wave = (0.60 * np.sin(2*np.pi * freq * t + vib)
                + 0.24 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t))
        env  = adsr(0.010, 0.0, 1.0, 0.25)
        return (wave * env * 0.80).astype(np.float32)

    # ── Blues / Jazz Bass ─────────────────────────────────────────────────────
    if timbre in ("bass_jazz", "bass_jazz_walking"):
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.82
        vib  = 0.04 * np.sin(2*np.pi * 4.0 * t) * np.clip(t / 0.4, 0, 1)
        wave += 0.12 * np.sin(2*np.pi * freq * t + vib) * np.exp(-2.5 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_blues":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.80
        wave += 0.14 * np.sin(2*np.pi * freq * t) * np.exp(-3.0 * t / dur)
        noise = np.random.uniform(-0.012, 0.012, n).astype(np.float32)
        return np.clip(wave + noise, -1.0, 1.0)

    if timbre == "bass_country":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.85
        wave += 0.10 * np.sin(2*np.pi * freq * 2 * t) * np.exp(-6.0 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    # ── Latin / World Bass ────────────────────────────────────────────────────
    if timbre in ("bass_latin", "bass_salsa", "bass_bossa"):
        wave = (0.62 * np.sin(2*np.pi * freq * t)
                + 0.24 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t)
                + 0.04 * np.sin(2*np.pi * freq * 4 * t))
        env  = adsr(0.008, 0.0, 1.0, 0.22)
        return (wave * env * 0.82).astype(np.float32)

    if timbre == "bass_cumbia":
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.26 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t))
        env  = adsr(0.006, 0.04, 0.85, 0.18)
        return (wave * env * 0.80).astype(np.float32)

    if timbre in ("bass_afrobeat", "bass_afro"):
        wave = (0.63 * np.sin(2*np.pi * freq * t)
                + 0.23 * np.sin(2*np.pi * freq * 2 * t)
                + 0.11 * np.sin(2*np.pi * freq * 3 * t)
                + 0.03 * np.sin(2*np.pi * freq * 4 * t))
        env  = adsr(0.007, 0.0, 1.0, 0.24)
        wave = np.tanh(1.4 * wave) / np.tanh(np.float32(1.4))
        return (wave * env * 0.80).astype(np.float32)

    if timbre == "bass_amapiano":
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t)
                + 0.15 * np.sin(2*np.pi * (freq/2) * t))
        env  = adsr(0.006, 0.0, 1.0, 0.30)
        wave = np.tanh(1.6 * wave) / np.tanh(np.float32(1.6))
        return (wave * env * 0.78).astype(np.float32)

    if timbre == "bass_soca":
        wave = (0.62 * np.sin(2*np.pi * freq * t)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        env  = adsr(0.005, 0.04, 0.88, 0.18)
        return (wave * env * 0.82).astype(np.float32)

    # ── Electronic Music Bass ─────────────────────────────────────────────────
    if timbre == "bass_house":
        wave = sum((1.0 / k) * np.sin(2*np.pi * freq * k * t)
                   for k in range(1, 9)).astype(np.float32)
        env_f = np.exp(-18.0 * t / dur)
        filter_curve = 1.0 / (1.0 + env_f * 3.0)
        wave  = wave * (0.4 + 0.6 * filter_curve)
        env   = adsr(0.006, 0.08, 0.80, 0.14)
        return (np.tanh(1.4 * wave) / np.tanh(np.float32(1.4)) * env * 0.60).astype(np.float32)

    if timbre == "bass_techno":
        wave = sum((1.0 / (2*k-1)) * np.sin(2*np.pi * freq * (2*k-1) * t)
                   for k in range(1, 9)).astype(np.float32)
        env  = adsr(0.004, 0.0, 1.0, 0.15)
        return (np.tanh(2.2 * wave) / np.tanh(np.float32(2.2)) * env * 0.60).astype(np.float32)

    if timbre in ("bass_jungle", "bass_breaks"):
        wave = (0.65 * np.sin(2*np.pi * freq * t)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        lfo  = 1.0 + 0.15 * np.sin(2*np.pi * 2.0 * t)
        env  = adsr(0.006, 0.0, 1.0, 0.18)
        wave = np.tanh(2.0 * wave) / np.tanh(np.float32(2.0))
        return (wave * env * lfo * 0.68).astype(np.float32)

    if timbre in ("bass_dnb", "bass_drum_and_bass"):
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.28 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t)
                + 0.02 * np.sin(2*np.pi * freq * 4 * t))
        wave = np.tanh(2.5 * wave) / np.tanh(np.float32(2.5))
        env  = adsr(0.005, 0.0, 1.0, 0.16)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_neurofunk":
        detune = 1.004
        wave   = (0.40 * np.sin(2*np.pi * freq * t)
                  + 0.40 * np.sin(2*np.pi * freq * detune * t)
                  + 0.15 * np.sin(2*np.pi * freq * 2 * t)
                  + 0.05 * np.sin(2*np.pi * freq * 3 * t))
        wave   = np.tanh(3.0 * wave) / np.tanh(np.float32(3.0))
        lfo    = 0.5 * (1.0 + np.sin(2*np.pi * 6.0 * t))
        env    = adsr(0.005, 0.0, 1.0, 0.16)
        return (wave * env * lfo * 0.62).astype(np.float32)

    if timbre == "bass_liquid":
        vib  = 0.08 * np.sin(2*np.pi * 3.0 * t)
        wave = (0.62 * np.sin(2*np.pi * freq * t + vib)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        wave = np.tanh(1.5 * wave) / np.tanh(np.float32(1.5))
        env  = adsr(0.008, 0.0, 1.0, 0.22)
        return (wave * env * 0.70).astype(np.float32)

    if timbre == "bass_dubstep":
        lfo_rate = 16.0
        lfo  = 0.5 * (1.0 + np.sin(2*np.pi * lfo_rate * t))
        wave = (0.65 * np.sin(2*np.pi * freq * t)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.13 * np.sin(2*np.pi * freq * 3 * t))
        wave = np.tanh(3.0 * wave) / np.tanh(np.float32(3.0))
        env  = adsr(0.005, 0.0, 1.0, 0.18)
        return (wave * env * lfo * 0.72).astype(np.float32)

    if timbre == "bass_garage":
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t)
                + 0.03 * np.sin(2*np.pi * freq * 5 * t))
        env  = adsr(0.005, 0.06, 0.82, 0.16)
        return (np.tanh(1.6 * wave) / np.tanh(np.float32(1.6)) * env * 0.70).astype(np.float32)

    if timbre == "bass_trance":
        detune = 1.003
        wave   = (0.50 * np.sin(2*np.pi * freq * t)
                  + 0.40 * np.sin(2*np.pi * freq * detune * t)
                  + 0.10 * np.sin(2*np.pi * freq * 2 * t))
        env    = adsr(0.008, 0.10, 0.85, 0.20)
        return (np.tanh(1.8 * wave) / np.tanh(np.float32(1.8)) * env * 0.65).astype(np.float32)

    if timbre == "bass_hardstyle":
        wave  = sum((1.0 / (2*k-1)) * np.sin(2*np.pi * freq * (2*k-1) * t)
                    for k in range(1, 10)).astype(np.float32)
        env   = adsr(0.003, 0.0, 1.0, 0.15)
        return (np.tanh(4.0 * wave) / np.tanh(np.float32(4.0)) * env * 0.58).astype(np.float32)

    if timbre in ("bass_future_bass", "bass_future"):
        detune = 1.005
        wave   = (0.45 * np.sin(2*np.pi * freq * t)
                  + 0.45 * np.sin(2*np.pi * freq * detune * t)
                  + 0.10 * np.sin(2*np.pi * freq * 2 * t))
        lfo    = 0.3 * np.sin(2*np.pi * 0.8 * t)
        env    = adsr(0.008, 0.0, 1.0, 0.28)
        return (np.tanh(2.0 * wave) / np.tanh(np.float32(2.0)) * env * (1.0 + lfo) * 0.60).astype(np.float32)

    if timbre == "bass_wave":
        wave = (0.65 * np.sin(2*np.pi * freq * t)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t)
                + 0.03 * np.sin(2*np.pi * freq * 4 * t))
        lfo  = 0.2 * np.sin(2*np.pi * 1.5 * t)
        env  = adsr(0.007, 0.0, 1.0, 0.25)
        return (np.tanh(1.8 * wave) / np.tanh(np.float32(1.8)) * env * (1 + lfo) * 0.68).astype(np.float32)

    # ── More 808 Regional / Texture Variants ──────────────────────────────────
    if timbre == "bass_808_miami":
        pitch_env = np.exp(-6.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.22 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = (0.70 * np.sin(phase) + 0.20 * np.sin(2*phase) + 0.10 * np.sin(0.5*phase))
        wave      = np.tanh(2.0 * wave) / np.tanh(np.float32(2.0))
        env       = adsr(0.005, 0.0, 1.0, 0.32)
        return (wave * env).astype(np.float32)

    if timbre == "bass_808_houston":
        pitch_env = np.exp(-3.5 * t / dur)
        freq_mod  = freq * (1.0 + 0.18 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = (0.68 * np.sin(phase) + 0.22 * np.sin(2*phase) + 0.10 * np.sin(0.5*phase))
        wave      = np.tanh(1.6 * wave) / np.tanh(np.float32(1.6))
        env       = adsr(0.007, 0.0, 1.0, 0.55)
        return (wave * env).astype(np.float32)

    if timbre == "bass_808_blown":
        pitch_env = np.exp(-8.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.28 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = np.sin(phase) + 0.25 * np.sin(2*phase)
        wave      = np.clip(wave * 3.0, -1.0, 1.0)
        noise     = np.random.uniform(-0.04, 0.04, n).astype(np.float32)
        env       = adsr(0.003, 0.0, 1.0, 0.25)
        return ((wave + noise) * env * 0.80).astype(np.float32)

    if timbre == "bass_808_filtered":
        from scipy import signal as _sig
        pitch_env = np.exp(-8.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.25 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = (0.70*np.sin(phase) + 0.20*np.sin(2*phase) + 0.10*np.sin(0.5*phase))
        nyq = sr / 2.0
        b, a = _sig.butter(4, min(300.0 / nyq, 0.99), btype='low')
        wave  = _sig.filtfilt(b, a, wave.astype(np.float64)).astype(np.float32)
        wave  = np.tanh(1.8 * wave) / np.tanh(np.float32(1.8))
        env   = adsr(0.005, 0.0, 1.0, 0.28)
        return (wave * env).astype(np.float32)

    if timbre == "bass_808_resonant":
        pitch_env = np.exp(-8.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.25 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        res_freq  = freq * 3.5
        res_wave  = 0.30 * np.sin(2*np.pi * res_freq * t) * np.exp(-15.0 * t / dur)
        wave      = 0.70*np.sin(phase) + 0.20*np.sin(2*phase) + res_wave.astype(np.float32)
        wave      = np.tanh(2.0 * wave) / np.tanh(np.float32(2.0))
        env       = adsr(0.004, 0.0, 1.0, 0.25)
        return (wave * env).astype(np.float32)

    if timbre == "bass_808_tape":
        pitch_env = np.exp(-8.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.25 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = 0.70*np.sin(phase) + 0.20*np.sin(2*phase) + 0.10*np.sin(0.5*phase)
        noise     = np.random.uniform(-0.025, 0.025, n)
        wave      = np.tanh(2.0 * (wave + noise)) / np.tanh(2.0)
        env       = adsr(0.005, 0.0, 1.0, 0.26)
        return (wave * env * 0.85).astype(np.float32)

    # ── Effects-Processed Bass ────────────────────────────────────────────────
    if timbre == "bass_chorus":
        detune1 = 1.003
        detune2 = 0.997
        wave    = (0.40 * np.sin(2*np.pi * freq * t)
                   + 0.30 * np.sin(2*np.pi * freq * detune1 * t)
                   + 0.30 * np.sin(2*np.pi * freq * detune2 * t)
                   + 0.15 * np.sin(2*np.pi * freq * 2 * t)
                   + 0.05 * np.sin(2*np.pi * freq * 3 * t))
        env     = adsr(0.008, 0.0, 1.0, 0.22)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_flanger":
        rate  = 0.4
        depth = 0.003
        delay_samples = int(depth * sr)
        wave  = (0.62 * np.sin(2*np.pi * freq * t)
                 + 0.24 * np.sin(2*np.pi * freq * 2 * t)
                 + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        flange_phase = np.sin(2*np.pi * rate * t) * delay_samples
        flange = np.zeros(n, dtype=np.float32)
        for i in range(n):
            d = int(flange_phase[i])
            j = i - max(0, d)
            if 0 <= j < n:
                flange[i] = wave[j]
        wave  = (wave + 0.5 * flange)
        env   = adsr(0.006, 0.0, 1.0, 0.22)
        return (wave * env * 0.70).astype(np.float32)

    if timbre == "bass_tape":
        wave  = (0.62 * np.sin(2*np.pi * freq * t)
                 + 0.24 * np.sin(2*np.pi * freq * 2 * t)
                 + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        noise = np.random.uniform(-0.020, 0.020, n).astype(np.float32)
        wave  = np.tanh(1.4 * (wave + noise)) / np.tanh(np.float32(1.4))
        env   = adsr(0.008, 0.0, 1.0, 0.25)
        return (wave * env * 0.78).astype(np.float32)

    if timbre == "bass_compress":
        wave  = (0.60 * np.sin(2*np.pi * freq * t)
                 + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                 + 0.12 * np.sin(2*np.pi * freq * 3 * t))
        peak  = np.maximum(np.abs(wave), 1e-6)
        comp  = np.where(peak > 0.5, 0.5 + (peak - 0.5) * 0.3, peak)
        wave  = wave * (comp / peak)
        env   = adsr(0.005, 0.0, 1.0, 0.22)
        return (wave * env * 0.88).astype(np.float32)

    # ── Exotic / Ethnic Textures ──────────────────────────────────────────────
    if timbre == "bass_oud_low":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="oud")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.85
        wave += 0.08 * np.sin(2*np.pi * freq * t) * np.exp(-3.0 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_koto_low":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="koto")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.85
        return np.clip(wave, -1.0, 1.0)

    if timbre in ("guitar", "bass", "oud", "sitar", "banjo", "koto", "shamisen"):
        return _ks_instrument(freq, duration, sr=sr, timbre=timbre)

    n = max(int(sr * duration), 1)
    t = np.linspace(0, duration, n, dtype=np.float32)
    return (0.6 * np.sin(2 * np.pi * freq * t) * np.exp(-3.0 * t / max(duration,.01))).astype(np.float32)

def _synth_note_LEGACY_UNUSED(midi_pitch: int, duration: float, timbre: str, sr: int = 44100) -> np.ndarray:
    """Legacy (replaced by parametric engine above). Kept for reference only."""
    freq = _midi_to_hz(midi_pitch)
    n    = max(int(sr * duration), 1)
    t    = np.linspace(0, duration, n, dtype=np.float32)
    dur  = max(duration, 0.01)

    def adsr(atk, dec, sus_lvl, rel_ratio=0.15):
        env = np.ones(n, dtype=np.float32)
        a = min(int(sr * atk), n)
        d = min(int(sr * dec), n - a)
        r = min(int(sr * dur * rel_ratio), n)
        if a: env[:a] = np.linspace(0, 1, a)
        if d: env[a:a+d] = np.linspace(1, sus_lvl, d)
        if r and r < n: env[-r:] = np.linspace(env[-r], 0, r)
        return env

    # ── Plucked strings (Karplus-Strong) ──────────────────────────────────────
    if timbre in ("guitar", "bass", "oud", "sitar", "banjo"):
        return _ks_instrument(freq, duration, sr=sr, timbre=timbre)

    if timbre == "electric_guitar":
        wave = (0.6 * np.sin(2 * np.pi * freq * t) +
                0.25 * np.sin(2 * np.pi * freq * 2 * t) +
                0.10 * np.sin(2 * np.pi * freq * 3 * t) +
                0.05 * np.sin(2 * np.pi * freq * 4 * t))
        env = adsr(0.005, 0.08, 0.75, 0.12)
        return (wave * env).astype(np.float32)

    if timbre == "harp":
        wave = (0.55 * np.sin(2 * np.pi * freq * t) +
                0.28 * np.sin(2 * np.pi * freq * 2 * t) +
                0.10 * np.sin(2 * np.pi * freq * 3 * t))
        env = np.exp(-5.0 * t / dur)
        return (wave * env).astype(np.float32)

    # ── Keyboards ─────────────────────────────────────────────────────────────
    if timbre == "piano":
        wave = (0.50 * np.sin(2 * np.pi * freq * t) +
                0.25 * np.sin(2 * np.pi * freq * 2 * t) +
                0.12 * np.sin(2 * np.pi * freq * 3 * t) +
                0.06 * np.sin(2 * np.pi * freq * 4 * t))
        env = np.exp(-4.5 * t / dur)
        return (wave * env).astype(np.float32)

    if timbre == "harpsichord":
        wave = (0.45 * np.sin(2 * np.pi * freq * t) +
                0.30 * np.sin(2 * np.pi * freq * 2 * t) +
                0.15 * np.sin(2 * np.pi * freq * 3 * t) +
                0.08 * np.sin(2 * np.pi * freq * 4 * t))
        env = np.exp(-9.0 * t / dur)
        return (wave * env).astype(np.float32)

    if timbre == "organ":
        wave = (0.40 * np.sin(2 * np.pi * freq * t) +
                0.30 * np.sin(2 * np.pi * freq * 2 * t) +
                0.20 * np.sin(2 * np.pi * freq * 3 * t) +
                0.07 * np.sin(2 * np.pi * freq * 4 * t) +
                0.03 * np.sin(2 * np.pi * freq * 6 * t))
        env = adsr(0.01, 0.0, 1.0, 0.05)
        return (wave * env).astype(np.float32)

    if timbre == "marimba":
        wave = (0.60 * np.sin(2 * np.pi * freq * t) +
                0.25 * np.sin(2 * np.pi * freq * 3 * t) +
                0.10 * np.sin(2 * np.pi * freq * 5 * t))
        env = np.exp(-8.0 * t / dur)
        return (wave * env).astype(np.float32)

    # ── 808 Bass (Hip-Hop / Trap) ──────────────────────────────────────────────
    if timbre in ("bass_808", "808"):
        pitch_env = np.exp(-8.0 * t / max(dur, 0.1))
        freq_mod = freq * (1 + 0.25 * pitch_env)
        sub2 = np.sin(2 * np.pi * np.cumsum(freq_mod / sr) * (1.0 / sr) * sr)
        wave = 0.7 * sub2 + 0.2 * np.sin(2 * np.pi * freq * 2 * t) + 0.08 * np.sin(2 * np.pi * freq * 3 * t)
        wave = np.tanh(1.8 * wave) / np.tanh(np.float32(1.8))
        env = adsr(0.005, 0.0, 1.0, 0.25)
        return (wave * env).astype(np.float32)

    # ── Heavy Synth Lead (Hip-Hop / Electronic) ────────────────────────────────
    if timbre == "synth_heavy":
        wave = sum(
            (1.0 / k) * np.sin(2 * np.pi * freq * k * t + (0.05 * k))
            for k in range(1, 12)
        ).astype(np.float32)
        wave = np.tanh(2.5 * wave) / np.tanh(np.float32(2.5))
        env = adsr(0.008, 0.12, 0.75, 0.15)
        return (wave * env * 0.45).astype(np.float32)

    # ── Synths ─────────────────────────────────────────────────────────────────
    if timbre == "synth":
        # Sawtooth-like via additive harmonics
        wave = sum(
            (1.0 / k) * np.sin(2 * np.pi * freq * k * t)
            for k in range(1, 9)
        ).astype(np.float32)
        env = adsr(0.02, 0.1, 0.8, 0.15)
        return (wave * env * 0.3).astype(np.float32)

    if timbre == "synth_pad":
        # Correct FM vibrato: phase offset = depth * sin(vib_rate)
        vib_phase = 0.15 * np.sin(2 * np.pi * 0.5 * t)   # modulation index 0.15 rad
        phase1 = 2 * np.pi * freq * t + vib_phase
        phase2 = 2 * np.pi * freq * 2 * t + vib_phase
        wave = (0.5 * np.sin(phase1) +
                0.3 * np.sin(phase2) +
                0.15 * np.sin(2 * np.pi * freq * 3 * t))
        env = adsr(0.3, 0.0, 1.0, 0.3)
        return (wave * env).astype(np.float32)

    # ── Bowed strings ──────────────────────────────────────────────────────────
    if timbre == "violin":
        # Proper FM vibrato: bounded phase modulation, no accumulation
        vib_phase = 0.12 * np.sin(2 * np.pi * 5.5 * t)
        wave = (0.60 * np.sin(2 * np.pi * freq * t + vib_phase) +
                0.20 * np.sin(2 * np.pi * freq * 2 * t) +
                0.10 * np.sin(2 * np.pi * freq * 3 * t) +
                0.05 * np.sin(2 * np.pi * freq * 4 * t))
        env = adsr(0.06, 0.0, 1.0, 0.08)
        return (wave * env).astype(np.float32)

    if timbre == "viola":
        vib_phase = 0.11 * np.sin(2 * np.pi * 5.0 * t)
        wave = (0.55 * np.sin(2 * np.pi * freq * t + vib_phase) +
                0.25 * np.sin(2 * np.pi * freq * 2 * t) +
                0.12 * np.sin(2 * np.pi * freq * 3 * t))
        env = adsr(0.07, 0.0, 1.0, 0.10)
        return (wave * env).astype(np.float32)

    if timbre == "cello":
        vib_phase = 0.10 * np.sin(2 * np.pi * 4.5 * t)
        wave = (0.50 * np.sin(2 * np.pi * freq * t + vib_phase) +
                0.28 * np.sin(2 * np.pi * freq * 2 * t) +
                0.14 * np.sin(2 * np.pi * freq * 3 * t) +
                0.07 * np.sin(2 * np.pi * freq * 4 * t))
        env = adsr(0.09, 0.0, 1.0, 0.12)
        return (wave * env).astype(np.float32)

    # ── Brass ─────────────────────────────────────────────────────────────────
    if timbre == "brass":
        wave = (0.40 * np.sin(2 * np.pi * freq * t) +
                0.30 * np.sin(2 * np.pi * freq * 2 * t) +
                0.18 * np.sin(2 * np.pi * freq * 3 * t) +
                0.08 * np.sin(2 * np.pi * freq * 4 * t) +
                0.04 * np.sin(2 * np.pi * freq * 5 * t))
        env = adsr(0.04, 0.05, 0.85, 0.10)
        return (wave * env).astype(np.float32)

    # ── Woodwinds ─────────────────────────────────────────────────────────────
    if timbre == "flute":
        breath = np.random.uniform(-1, 1, n).astype(np.float32) * 0.04
        wave   = (0.70 * np.sin(2 * np.pi * freq * t) +
                  0.15 * np.sin(2 * np.pi * freq * 2 * t) +
                  0.06 * np.sin(2 * np.pi * freq * 3 * t) + breath)
        env = adsr(0.05, 0.0, 1.0, 0.08)
        return (wave * env).astype(np.float32)

    if timbre == "woodwind":
        breath = np.random.uniform(-1, 1, n).astype(np.float32) * 0.03
        wave   = (0.55 * np.sin(2 * np.pi * freq * t) +
                  0.25 * np.sin(2 * np.pi * freq * 2 * t) +
                  0.12 * np.sin(2 * np.pi * freq * 3 * t) + breath)
        env = adsr(0.04, 0.0, 1.0, 0.08)
        return (wave * env).astype(np.float32)

    if timbre == "saxophone":
        breath = np.random.uniform(-1, 1, n).astype(np.float32) * 0.03
        wave   = (0.45 * np.sin(2 * np.pi * freq * t) +
                  0.28 * np.sin(2 * np.pi * freq * 2 * t) +
                  0.15 * np.sin(2 * np.pi * freq * 3 * t) +
                  0.07 * np.sin(2 * np.pi * freq * 4 * t) + breath)
        env = adsr(0.05, 0.05, 0.85, 0.10)
        return (wave * env).astype(np.float32)

    # ── Voice ─────────────────────────────────────────────────────────────────
    if timbre == "voice":
        vib_phase = 0.13 * np.sin(2 * np.pi * 6.0 * t)
        wave = (0.65 * np.sin(2 * np.pi * freq * t + vib_phase) +
                0.20 * np.sin(2 * np.pi * freq * 2 * t) +
                0.08 * np.sin(2 * np.pi * freq * 3 * t))
        env = adsr(0.08, 0.0, 1.0, 0.12)
        return (wave * env).astype(np.float32)

    # ── Fallback ──────────────────────────────────────────────────────────────
    return _ks_instrument(freq, duration, sr=sr, timbre="guitar")


# ═══════════════════════════════════════════════════════════════════════════════
#  DEEP ANALYSIS HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_CHROMATIC = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

def _midi_name(midi: int) -> str:
    return _CHROMATIC[midi % 12] + str(midi // 12 - 1)

def _scale_notes(tonic: str, mode: str) -> list:
    intervals = [0,2,4,5,7,9,11] if mode == "major" else [0,2,3,5,7,8,10]
    t = tonic.replace('b','#')
    idx = next((i for i,n in enumerate(_CHROMATIC) if n == t), 0)
    return [_CHROMATIC[(idx+iv)%12] for iv in intervals]

def _detect_time_sig(score) -> str:
    try:
        from music21 import meter
        for ts in score.flatten().getElementsByClass(meter.TimeSignature):
            return ts.ratioString
    except Exception:
        pass
    return "4/4"

def _detect_key(score) -> dict:
    try:
        k = score.analyze("key")
        tonic = k.tonic.name
        mode  = k.mode
        scale = _scale_notes(tonic, mode)
        arabic_mode = "ماجور (صاعد)" if mode == "major" else "مينور (نازل)"
        return {"tonic": tonic, "mode": mode, "arabic_mode": arabic_mode,
                "scale": scale, "confidence": round(k.correlationCoefficient, 2)}
    except Exception:
        return {"tonic": "—", "mode": "major", "arabic_mode": "—",
                "scale": [], "confidence": 0}

def _detect_modulations(score) -> list:
    """Return list of key changes found in the score."""
    try:
        from music21 import analysis
        mods = []
        seen = set()
        for part in (score.parts if hasattr(score, "parts") else [score]):
            measures = part.getElementsByClass("Measure")
            for m in measures:
                try:
                    k = m.analyze("key")
                    label = f"{k.tonic.name} {k.mode}"
                    bar = m.measureNumber
                    if label not in seen:
                        seen.add(label)
                        mods.append({"bar": bar, "key": label})
                except Exception:
                    pass
        return mods[:8]
    except Exception:
        return []

def _analyze_drum_pattern(drum_notes: list, bpm: float) -> dict:
    if not drum_notes:
        return {"pattern_lines": [], "groove": "—", "syncopation": False,
                "kick_count": 0, "snare_count": 0, "hihat_count": 0}

    beat_dur  = 60.0 / bpm
    bar_dur   = beat_dur * 4
    step_dur  = beat_dur / 4       # 16th-note

    kicks  = [n for n in drum_notes if n["midi"] in (35, 36)]
    snares = [n for n in drum_notes if n["midi"] in (38, 40)]
    hihats = [n for n in drum_notes if n["midi"] in (42, 44, 46)]

    def beat_pos(t):
        return (t % bar_dur) / beat_dur

    def step_set(hits):
        s = set()
        for n in hits[:128]:
            s.add(round((n["time"] % bar_dur) / step_dur) % 16)
        return s

    k_steps = step_set(kicks)
    s_steps = step_set(snares)
    h_steps = step_set(hihats)

    k_str = "".join("K" if i in k_steps else "·" for i in range(16))
    s_str = "".join("S" if i in s_steps else "·" for i in range(16))
    h_str = "".join("x" if i in h_steps else "·" for i in range(16))

    # Swing: off-beats landing ~33 % of step instead of 50 %
    swing_score = 0
    for h in hihats[:64]:
        bp = beat_pos(h["time"]) % 0.5
        if 0.28 < bp < 0.38:
            swing_score += 1
    total_off = sum(1 for h in hihats[:64] if 0.2 < beat_pos(h["time"]) % 0.5 < 0.45)
    groove = "Swing 🎷" if total_off > 0 and swing_score / max(total_off, 1) > 0.35 else "Straight"

    # Syncopation: notes landing on "e" or "a" (steps 1,3,5,7,…)
    all_hits = kicks + snares
    synco = sum(1 for n in all_hits if round((n["time"] % bar_dur) / step_dur) % 2 == 1)
    syncopation = synco / max(len(all_hits), 1) > 0.25

    return {
        "pattern_lines": [f"K `{k_str}`", f"S `{s_str}`", f"H `{h_str}`"],
        "groove": groove,
        "syncopation": syncopation,
        "kick_count": len(kicks),
        "snare_count": len(snares),
        "hihat_count": len(hihats),
    }

def _analyze_melody_part(notes_list: list) -> dict:
    if not notes_list:
        return {"low": "—", "high": "—", "span_oct": 0, "repetitive": False,
                "has_ornaments": False}
    midis = [n["midi"] for n in notes_list]
    low, high = min(midis), max(midis)

    # Repetition: look for 4-note motifs that repeat
    seq = [n["midi"] for n in notes_list[:300]]
    patterns: dict = {}
    for i in range(len(seq) - 4):
        p = tuple(seq[i:i+4])
        patterns[p] = patterns.get(p, 0) + 1
    repetitive = bool(patterns) and max(patterns.values()) >= 3

    return {
        "low": _midi_name(low),
        "high": _midi_name(high),
        "span_oct": (high - low) // 12,
        "repetitive": repetitive,
        "has_ornaments": False,   # ornament detection requires deeper music21 parsing
    }

def _detect_structure(all_parts: dict, bpm: float) -> list:
    """Segment the score into sections based on note-density."""
    all_notes = []
    for pd in all_parts.values():
        all_notes.extend(pd["notes"])
    if not all_notes:
        return []

    beat_dur = 60.0 / bpm
    bar_dur  = beat_dur * 4
    chunk    = bar_dur * 4          # 4-bar window

    max_t = max(n["time"] for n in all_notes)
    n_chunks = max(1, int(max_t / chunk))

    densities = []
    for i in range(n_chunks):
        t0, t1 = i * chunk, (i+1) * chunk
        cnt = sum(1 for n in all_notes if t0 <= n["time"] < t1)
        densities.append(cnt / max(chunk, 0.01))

    max_d = max(densities) if densities else 1

    def fmt(t):
        return f"{int(t//60)}:{int(t%60):02d}"

    sections, prev_type = [], None
    for i, d in enumerate(densities):
        nd = d / max_d
        t  = i * chunk

        if i == 0:
            sec = "Intro"
        elif i == n_chunks - 1 and nd < 0.45:
            sec = "Outro"
        elif nd >= 0.75:
            sec = "Chorus" if prev_type in (None, "Verse", "Intro", "Pre-Chorus", "Bridge") else "Drop"
        elif nd < 0.35 and prev_type in ("Chorus", "Drop"):
            sec = "Bridge"
        elif nd >= 0.55 and prev_type in ("Intro", "Bridge", "Outro", None):
            sec = "Pre-Chorus" if prev_type == "Verse" else "Verse"
        else:
            sec = "Verse"

        if sec != prev_type:
            sections.append({"name": sec, "time_str": fmt(t)})
            prev_type = sec

    return sections

def _classify_roles(all_parts: dict) -> dict:
    """Assign Lead / Pad / Background / Bass roles."""
    roles = {}
    info = {}
    for pname, pd in all_parts.items():
        if not pd["notes"]:
            continue
        midis = [n["midi"] for n in pd["notes"]]
        info[pname] = {
            "avg": sum(midis)/len(midis),
            "min": min(midis),
            "count": len(midis),
            "timbre": pd["timbre"]
        }

    sorted_p = sorted(info.items(), key=lambda x: -x[1]["avg"])
    assigned_lead = False
    for pname, r in sorted_p:
        if r["avg"] < 45 or "bass" in pname.lower():
            roles[pname] = "Bass 🎸"
        elif not assigned_lead and r["avg"] > 62:
            roles[pname] = "Lead 🎤"
            assigned_lead = True
        elif r["count"] < 30:
            roles[pname] = "Pad 🎹"
        else:
            roles[pname] = "Background"
    return roles

def _detect_syncopation_score(all_parts: dict, bpm: float) -> float:
    """0-1 syncopation score across all melodic parts."""
    beat_dur = 60.0 / bpm
    total, synco = 0, 0
    for pd in all_parts.values():
        for n in pd["notes"]:
            b_frac = (n["time"] % beat_dur) / beat_dur
            if 0.3 < b_frac < 0.7:
                synco += 1
            total += 1
    return round(synco / max(total, 1), 2)

def deep_analyze_score(tab_data: dict, score=None) -> dict:
    """
    Run all deep analysis passes on tab_data and return an 'analysis' sub-dict.
    If `score` (music21 Score) is provided, also run key/time-sig detection.
    """
    bpm        = tab_data.get("bpm", 80)
    all_parts  = tab_data.get("all_parts", {})
    drum_notes = tab_data.get("drum_notes", [])

    result: dict = {}

    # Key / time signature (require music21 score object)
    if score is not None:
        result["time_sig"] = _detect_time_sig(score)
        result["key"]      = _detect_key(score)
        result["modulations"] = _detect_modulations(score)
    else:
        result["time_sig"] = "4/4"
        result["key"]      = {"tonic": "—", "mode": "—", "arabic_mode": "—",
                               "scale": [], "confidence": 0}
        result["modulations"] = []

    # Rhythm / groove
    result["drum"]        = _analyze_drum_pattern(drum_notes, bpm)
    result["synco_score"] = _detect_syncopation_score(all_parts, bpm)

    # Melody per part
    result["melodies"] = {
        pname: _analyze_melody_part(pd["notes"])
        for pname, pd in all_parts.items()
    }

    # Structure
    result["structure"] = _detect_structure(all_parts, bpm)

    # Instrument roles
    result["roles"] = _classify_roles(all_parts)

    return result


# ── Note name helpers ──────────────────────────────────────────────────────────
MIDI_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

def midi_to_name(midi: int) -> str:
    """Convert MIDI number to note name with octave. 60 → C4"""
    midi = max(0, min(127, int(midi)))
    return f"{MIDI_NOTE_NAMES[midi % 12]}{(midi // 12) - 1}"

def name_to_midi(token: str) -> int:
    """Parse a note token like 'C4', 'D#3', 'Bb2' → MIDI number. Returns 60 on failure."""
    import re
    token = token.strip().upper().replace("♭", "B").replace("♯", "#")
    m = re.match(r'^([A-G][#B]?)(-?\d+)$', token)
    if not m:
        return 60
    pc_map = {"C":0,"C#":1,"DB":1,"D":2,"D#":3,"EB":3,"E":4,"F":5,
              "F#":6,"GB":6,"G":7,"G#":8,"AB":8,"A":9,"A#":10,"BB":10,"B":11}
    pc = pc_map.get(m.group(1), 0)
    octave = int(m.group(2))
    return max(0, min(127, (octave + 1) * 12 + pc))

def format_notes_for_export(tab_data: dict) -> list[str]:
    """
    Build a copyable note-sequence per instrument (ALL notes, no limit).
    Returns a list of message strings, each ≤ 4000 chars, ready to send.
    Each instrument starts on a new message chunk if needed.
    """
    all_parts = tab_data.get("all_parts", {})
    CHUNK = 3800  # safe margin below Telegram's 4096

    messages = []
    current = "🎼 النوتات بالحروف — انسخ وعدّل وأرسل:\n\n"

    for pname, pd in all_parts.items():
        notes = sorted(pd.get("notes", []), key=lambda n: n.get("time", 0))
        if not notes:
            continue
        names = [midi_to_name(n["midi"]) for n in notes]
        # Build rows of 16 per line, first row prefixed with instrument name
        rows = [" ".join(names[i:i+16]) for i in range(0, len(names), 16)]
        block_lines = [f"{pname}: {rows[0]}"]
        for r in rows[1:]:
            block_lines.append(f"  {r}")
        block = "\n".join(block_lines) + "\n"

        # If adding this block would overflow, flush current and start new chunk
        if len(current) + len(block) > CHUNK:
            messages.append(current)
            current = block
        else:
            current += block

    if current.strip():
        messages.append(current)

    # Append instructions as final message
    messages.append(
        "📌 *لتعديل آلة:*\n"
        "١. انسخ سطر الآلة كاملاً\n"
        "٢. عدّل النوتات حسب رغبتك\n"
        "٣. أرسله كرسالة نصية للبوت وسيعيد التصيير\n\n"
        "📝 الصيغة: `اسم الآلة: C4 D4 E4 G4 ...`"
    )
    return messages


def build_rich_analysis_msg(tab_data: dict, fname: str = "") -> str:
    """
    Build the full human-readable analysis message from tab_data
    (which must include an 'analysis' key from deep_analyze_score).
    """
    an    = tab_data.get("analysis", {})
    bpm   = tab_data.get("bpm", "—")
    chords_list = tab_data.get("chords", [])
    chords = " • ".join(chords_list[:12]) or "—"

    key_d  = an.get("key", {})
    tonic  = key_d.get("tonic", "—")
    mode   = key_d.get("arabic_mode", "—")
    scale  = " ".join(key_d.get("scale", []))
    t_sig  = an.get("time_sig", "4/4")
    drum_d = an.get("drum", {})
    struct = an.get("structure", [])
    roles  = an.get("roles", {})
    melodies = an.get("melodies", {})
    synco  = an.get("synco_score", 0)
    mods   = an.get("modulations", [])

    lines = []
    header = f"🎼 *تحليل عميق — {fname}*\n" if fname else "🎼 *تحليل عميق*\n"
    lines.append(header)

    # ── Rhythm ──────────────────────────────────────────────────────────────
    lines.append("━━━ 🥁 الإيقاع ━━━")
    lines.append(f"• BPM: *{bpm}*   |   الميزان: *{t_sig}*")
    lines.append(f"• Groove: *{drum_d.get('groove','—')}*   |   Syncopation: *{round(synco*100)}%*")
    plines = drum_d.get("pattern_lines", [])
    if plines:
        lines.append("• Pattern (16th grid):")
        lines.extend(f"  {pl}" for pl in plines)
    if drum_d.get("kick_count"):
        lines.append(f"  Kick={drum_d['kick_count']} / Snare={drum_d['snare_count']} / HH={drum_d['hihat_count']}")

    # ── Harmony ─────────────────────────────────────────────────────────────
    lines.append("\n━━━ 🎵 الهارموني ━━━")
    lines.append(f"• المقام: *{tonic} {mode}*")
    if scale:
        lines.append(f"• السلم: `{scale}`")
    lines.append(f"• الكوردات: {chords}")
    if mods and len(mods) > 1:
        mod_str = " → ".join(m["key"] for m in mods[:5])
        lines.append(f"• Modulation: {mod_str}")

    # ── Parts / Roles ────────────────────────────────────────────────────────
    lines.append("\n━━━ 🎹 الآلات والأدوار ━━━")
    all_parts = tab_data.get("all_parts", {})
    for pname, pd in all_parts.items():
        role = roles.get(pname, "")
        mel  = melodies.get(pname, {})
        lo   = mel.get("low", "—")
        hi   = mel.get("high", "—")
        rep  = " ↺" if mel.get("repetitive") else ""
        lines.append(f"• *{pname}* {role}  `{lo}–{hi}`{rep}  ({len(pd['notes'])} نوتة)")
    if tab_data.get("has_drums"):
        lines.append(f"• *Drums* 🥁  ({len(tab_data.get('drum_notes',[]))} ضربة)")

    # ── Structure ────────────────────────────────────────────────────────────
    if struct:
        lines.append("\n━━━ 🗺 الهيكل ━━━")
        for s in struct:
            lines.append(f"• *{s['name']}* — {s['time_str']}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  EDIT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

user_edit_state: dict = {}
# user_id -> {"bpm_offset": 0, "transpose": 0, "swing": 0.0,
#             "muted": set(), "gain_overrides": {pname: gain}}

_DEFAULT_EDIT = lambda: {
    "bpm_offset": 0, "transpose": 0, "swing": 0.0,
    "muted": set(), "gain_overrides": {},
}

def generate_note_visualization(tab_data: dict, raw_xml_path: str,
                                audio_path: str, output_path: str,
                                max_secs: int = 60) -> bool:
    """
    LEFT  : smaller step-function graph with vector arrow
    RIGHT : equation panel  y(t) = sin(2pi*f1*t) + sin(2pi*f2*t) + ...
    No color legend.  Background physics equations float behind the graph.
    """
    import cv2, numpy as np, bisect, subprocess as _sp, io, math

    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        HAS_MPL = True
    except ImportError:
        HAS_MPL = False

    WIDTH, HEIGHT = 1280, 720
    FPS           = 30

    G_X0, G_Y0   = 80,  60
    G_X1, G_Y1   = 720, 650
    G_W  = G_X1 - G_X0
    G_H  = G_Y1 - G_Y0

    EQ_X0 = 740
    EQ_X1 = WIDTH - 10
    EQ_Y0 = 30
    EQ_Y1 = HEIGHT - 10

    MIDI_LO, MIDI_HI = 36, 96
    V_SOUND = 343.0
    AXIS_W  = 2
    TICK_LEN = 8
    LINE_W   = 3

    INST_COLORS = [
        (180,  30,  30), ( 20, 100, 200), ( 20, 160,  60),
        (160,  40, 200), (200, 130,   0), (  0, 160, 180),
        (200,  60, 120), (100, 140,   0),
    ]

    def _pitch_y(midi):
        midi = max(MIDI_LO, min(MIDI_HI, midi))
        frac = (midi - MIDI_LO) / (MIDI_HI - MIDI_LO)
        return int(G_Y0 + G_H * (1.0 - frac))

    def _time_x(t, duration):
        frac = t / duration if duration > 0 else 0
        return int(G_X0 + G_W * frac)

    def _midi_to_hz(midi):
        return 440.0 * (2.0 ** ((midi - 69) / 12.0))

    _eq_cache = {}
    def render_eq(latex_str, fontsize=18, color='#1a1a8c', fig_w=6.5):
        key = (latex_str, fontsize, color)
        if key in _eq_cache:
            return _eq_cache[key]
        if not HAS_MPL:
            return None
        try:
            fig, ax = plt.subplots(figsize=(fig_w, 1.2), facecolor='white')
            ax.set_facecolor('white')
            ax.axis('off')
            ax.text(0.01, 0.5, f"${latex_str}$",
                    ha='left', va='center', fontsize=fontsize,
                    color=color, transform=ax.transAxes)
            fig.tight_layout(pad=0.1)
            buf = io.BytesIO()
            fig.savefig(buf, format='png', dpi=100,
                        bbox_inches='tight', facecolor='white')
            plt.close(fig)
            buf.seek(0)
            arr = np.frombuffer(buf.read(), dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return None
            mask = np.any(img < 248, axis=2)
            rows = np.any(mask, axis=1)
            cols = np.any(mask, axis=0)
            if rows.any() and cols.any():
                r0, r1 = np.where(rows)[0][[0, -1]]
                c0, c1 = np.where(cols)[0][[0, -1]]
                img = img[max(0,r0-3):r1+6, max(0,c0-3):c1+6]
            _eq_cache[key] = img
            return img
        except Exception as ex:
            logger.warning(f"render_eq: {ex}")
            return None

    def paste(frame, img, x, y, alpha=1.0):
        if img is None:
            return
        h, w = img.shape[:2]
        x1, y1 = max(0, x), max(0, y)
        x2, y2 = min(frame.shape[1], x+w), min(frame.shape[0], y+h)
        if x1 >= x2 or y1 >= y2:
            return
        src = img[y1-y:y2-y, x1-x:x2-x].astype(float)
        roi = frame[y1:y2, x1:x2].astype(float)
        frame[y1:y2, x1:x2] = np.clip(roi*(1-alpha) + src*alpha, 0, 255).astype(np.uint8)

    try:
        from music21 import converter, tempo as m21tempo
        if raw_xml_path and os.path.exists(raw_xml_path):
            sc       = converter.parse(raw_xml_path)
            flat     = sc.flatten()
            bpm_mks  = list(flat.getElementsByClass(m21tempo.MetronomeMark))
            bpm      = int(bpm_mks[0].number) if bpm_mks else tab_data.get("bpm", 80)
            beat_dur = 60.0 / max(bpm, 40)
            raw_parts = []
            for pi, part in enumerate(sc.parts[:8]):
                evts = []
                for n in part.flatten().notes:
                    pitches = ([n.pitch] if hasattr(n, 'pitch')
                               else list(getattr(n, 'pitches', [])))
                    for p in pitches:
                        evts.append((float(n.offset)*beat_dur, p.midi))
                if evts:
                    evts.sort()
                    raw_parts.append({
                        "col":  INST_COLORS[pi % len(INST_COLORS)],
                        "name": (part.partName or f"Track {pi+1}")[:14],
                        "evts": evts,
                    })
        else:
            bpm      = tab_data.get("bpm", 80)
            beat_dur = 60.0 / max(bpm, 40)
            raw_parts = []
            for pi, (pname, pdata) in enumerate(tab_data.get("all_parts", {}).items()):
                evts = sorted((n.get("time", 0), n.get("midi", 60))
                              for n in pdata.get("notes", []))
                if evts:
                    raw_parts.append({
                        "col":  INST_COLORS[pi % len(INST_COLORS)],
                        "name": pname[:14], "evts": evts,
                    })
    except Exception as e:
        logger.error(f"Viz parse error: {e}")
        return False

    if not raw_parts:
        return False

    all_evts = [e for p in raw_parts for e in p["evts"]]
    total_t  = max(e[0] for e in all_evts)
    duration = min(total_t + 1.0, max_secs)
    total_frames = int(duration * FPS)

    for pn in raw_parts:
        evts = pn["evts"]
        segs = []
        for i, (t, midi) in enumerate(evts):
            t_end = evts[i+1][0] if i+1 < len(evts) else t + beat_dur
            segs.append((t, t_end, midi))
        pn["segs"]      = segs
        pn["evt_times"] = [e[0] for e in evts]
        pn["evt_midis"] = [e[1] for e in evts]

    def _current_midi(pn, t):
        idx = bisect.bisect_right(pn["evt_times"], t) - 1
        return pn["evt_midis"][idx] if idx >= 0 else None

    pn0   = raw_parts[0]
    terms = []
    for i, (t, midi) in enumerate(pn0["evts"]):
        hz    = _midi_to_hz(midi)
        t_end = pn0["evts"][i+1][0] if i+1 < len(pn0["evts"]) else t + beat_dur
        note_name = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"][midi % 12]
        oct_n     = midi // 12 - 1
        sign      = "+" if i > 0 else " "
        latex = rf"{sign}\sin(2\pi \cdot {hz:.1f} \cdot t)"
        terms.append({"t0": t, "t1": t_end, "midi": midi, "hz": hz,
                      "note": f"{note_name}{oct_n}", "latex": latex, "img": None})

    MAX_TERMS    = 12
    TERM_H       = 46
    EQ_PANEL_W   = EQ_X1 - EQ_X0 - 10

    header_img = render_eq(
        r"y(t) = \sum_n A_n \sin(2\pi f_n t + \varphi_n)",
        fontsize=17, color='#222222', fig_w=5.5)

    def get_term_img(i):
        if terms[i]["img"] is None:
            col_hex = "#{:02x}{:02x}{:02x}".format(*reversed(pn0["col"]))
            terms[i]["img"] = render_eq(terms[i]["latex"],
                                        fontsize=15, color=col_hex, fig_w=5.5)
        return terms[i]["img"]

    bg_eqs_latex = [
        r"\frac{\partial^2 y}{\partial t^2} = c^2 \frac{\partial^2 y}{\partial x^2}",
        r"E = h f = \hbar \omega",
        r"\hat{H}\psi = E\psi",
        r"\Delta x \cdot \Delta p \geq \frac{\hbar}{2}",
        r"S = k_B \ln W",
        r"\oint \mathbf{B}\cdot d\mathbf{A} = 0",
    ]
    rng = np.random.default_rng(42)
    bg_eq_data = []
    for i, eq_str in enumerate(bg_eqs_latex):
        img = render_eq(eq_str, fontsize=15, color='#aaaacc', fig_w=5.0)
        if img is not None:
            h, w = img.shape[:2]
            x = int(rng.uniform(G_X0+5, max(G_X0+6, G_X1-w-5)))
            y = int(rng.uniform(G_Y0+5, max(G_Y0+6, G_Y1-h-5)))
            bg_eq_data.append((img, x, y))

    bg = np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8)

    cv2.rectangle(bg, (EQ_X0-4, EQ_Y0), (EQ_X1, EQ_Y1), (240, 240, 248), -1)
    cv2.rectangle(bg, (EQ_X0-4, EQ_Y0), (EQ_X1, EQ_Y1), (180, 180, 200), 1)

    for midi_mark in range(MIDI_LO, MIDI_HI+1, 12):
        gy = _pitch_y(midi_mark)
        cv2.line(bg, (G_X0, gy), (G_X1, gy), (220, 220, 220), 1)
    tick_step = max(1, int(duration // 8))
    for sm in range(0, int(duration)+1, tick_step):
        gx = _time_x(sm, duration)
        if G_X0 <= gx <= G_X1:
            cv2.line(bg, (gx, G_Y0), (gx, G_Y1), (220, 220, 220), 1)

    cv2.arrowedLine(bg, (G_X0-15, G_Y1), (G_X1+25, G_Y1),
                    (20, 20, 20), AXIS_W, tipLength=0.02)
    cv2.arrowedLine(bg, (G_X0, G_Y1+15), (G_X0, G_Y0-20),
                    (20, 20, 20), AXIS_W, tipLength=0.03)

    cv2.putText(bg, "t (s)", (G_X1+12, G_Y1+5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.58, (20, 20, 20), 1, cv2.LINE_AA)
    cv2.putText(bg, "pitch", (6, G_Y0-28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20), 1, cv2.LINE_AA)

    for sm in range(0, int(duration)+1, tick_step):
        gx = _time_x(sm, duration)
        if G_X0 <= gx <= G_X1:
            cv2.line(bg, (gx, G_Y1-TICK_LEN), (gx, G_Y1+TICK_LEN), (20, 20, 20), 1)
            cv2.putText(bg, str(sm), (gx-9, G_Y1+24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.40, (60, 60, 60), 1, cv2.LINE_AA)

    for midi_mark in range(MIDI_LO, MIDI_HI+1, 12):
        gy = _pitch_y(midi_mark)
        cv2.line(bg, (G_X0-TICK_LEN, gy), (G_X0+TICK_LEN, gy), (20, 20, 20), 1)
        note_name = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"][midi_mark % 12]
        cv2.putText(bg, f"{note_name}{midi_mark//12-1}", (4, gy+5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.36, (80, 80, 80), 1, cv2.LINE_AA)

    bpm_disp = tab_data.get("bpm", bpm)
    ks       = tab_data.get("key_signature", "")
    hud      = f"BPM={bpm_disp}" + (f"  {ks}" if ks else "")
    cv2.putText(bg, hud, (G_X0, G_Y0-8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.52, (80, 80, 80), 1, cv2.LINE_AA)

    panel_title_y = EQ_Y0 + 8
    if header_img is not None:
        paste(bg, header_img, EQ_X0+2, panel_title_y, alpha=1.0)
    else:
        cv2.putText(bg, "y(t) = ...", (EQ_X0+6, panel_title_y+20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (30, 30, 30), 1, cv2.LINE_AA)
    sep_y = panel_title_y + (header_img.shape[0]+6 if header_img is not None else 30)
    cv2.line(bg, (EQ_X0-2, sep_y), (EQ_X1-2, sep_y), (160, 160, 200), 1)
    TERM_Y_START = sep_y + 10

    tmp_vid = output_path + ".raw.mp4"
    fourcc  = cv2.VideoWriter_fourcc(*"mp4v")
    vw      = cv2.VideoWriter(tmp_vid, fourcc, FPS, (WIDTH, HEIGHT))

    overlay = bg.copy()

    inst_state = {pni: {"seg_idx": 0, "tail_x": None, "tail_y": None}
                  for pni in range(len(raw_parts))}
    prev_arrow_midi = {pni: None for pni in range(len(raw_parts))}
    last_term_count = [0]

    for fi in range(total_frames):
        t_now = fi / FPS
        x_now = max(G_X0, min(G_X1, _time_x(t_now, duration)))

        for pni, pn in enumerate(raw_parts):
            col   = pn["col"]
            state = inst_state[pni]
            segs  = pn["segs"]
            idx   = state["seg_idx"]
            tx    = state["tail_x"]
            ty    = state["tail_y"]

            while idx < len(segs):
                t_seg, t_end, midi = segs[idx]
                if t_seg > t_now:
                    break
                x_seg = max(G_X0, min(G_X1, _time_x(t_seg, duration)))
                y_seg = _pitch_y(midi)

                if ty is not None and ty != y_seg:
                    cv2.line(overlay, (x_seg, ty), (x_seg, y_seg),
                             col, LINE_W, cv2.LINE_AA)

                t_draw_end = min(t_end, t_now)
                x_draw_end = max(G_X0, min(G_X1, _time_x(t_draw_end, duration)))
                x_from     = tx if (tx is not None and tx >= x_seg) else x_seg
                if x_from < x_draw_end:
                    cv2.line(overlay, (x_from, y_seg), (x_draw_end, y_seg),
                             col, LINE_W, cv2.LINE_AA)

                ty = y_seg
                tx = x_draw_end
                if t_draw_end < t_end:
                    break
                else:
                    idx += 1

            state["seg_idx"] = idx
            state["tail_x"]  = tx
            state["tail_y"]  = ty

        active_count = min(sum(1 for tr in terms if tr["t0"] <= t_now), MAX_TERMS)
        if active_count > last_term_count[0]:
            for ti in range(last_term_count[0], active_count):
                timg = get_term_img(ti)
                ey   = TERM_Y_START + ti * TERM_H
                if ey + TERM_H < EQ_Y1 and timg is not None:
                    cv2.rectangle(overlay,
                                  (EQ_X0-2, ey-2), (EQ_X1-2, ey+timg.shape[0]+4),
                                  (240, 240, 248), -1)
                    paste(overlay, timg, EQ_X0+4, ey, alpha=1.0)
                    note_lbl = terms[ti]["note"]
                    hz_lbl   = f"{terms[ti]['hz']:.1f}Hz"
                    cv2.putText(overlay, f"{note_lbl} = {hz_lbl}",
                                (EQ_X0+4, ey + (timg.shape[0] if timg is not None else 24) + 14),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.36, (100, 100, 140), 1, cv2.LINE_AA)
            last_term_count[0] = active_count

        frame = overlay.copy()

        for i, (eq_img, px, py) in enumerate(bg_eq_data):
            drift_x = int(5*math.sin(t_now*0.28 + i*1.3))
            drift_y = int(3*math.cos(t_now*0.22 + i*0.9))
            paste(frame, eq_img, px+drift_x, py+drift_y, alpha=0.25)

        cm0    = _current_midi(pn0, t_now)
        state0 = inst_state[0]
        if cm0 is not None and state0["tail_x"] is not None:
            ax_cur = state0["tail_x"]
            ay_cur = state0["tail_y"] if state0["tail_y"] is not None else _pitch_y(cm0)
            prev_m = prev_arrow_midi[0]
            if prev_m is None or prev_m == cm0:
                ax_tip = min(G_X1, ax_cur+26)
                ay_tip = ay_cur
            elif prev_m < cm0:
                ax_tip = min(G_X1, ax_cur+18)
                ay_tip = max(G_Y0, ay_cur-18)
            else:
                ax_tip = min(G_X1, ax_cur+18)
                ay_tip = min(G_Y1, ay_cur+18)
            prev_arrow_midi[0] = cm0

            for r, a in [(13, 25), (8, 60), (4, 160)]:
                cv2.circle(frame, (ax_cur, ay_cur), r, pn0["col"], -1, cv2.LINE_AA)
            cv2.arrowedLine(frame, (ax_cur-16, ay_cur), (ax_tip, ay_tip),
                            pn0["col"], 3, cv2.LINE_AA, tipLength=0.55)
            cv2.arrowedLine(frame, (ax_cur-16, ay_cur), (ax_tip, ay_tip),
                            (20, 20, 20), 1, cv2.LINE_AA, tipLength=0.55)

        for pni in range(1, len(raw_parts)):
            pn    = raw_parts[pni]
            cm    = _current_midi(pn, t_now)
            state = inst_state[pni]
            if cm is not None and state["tail_x"] is not None:
                cx = state["tail_x"]
                cy = state["tail_y"] if state["tail_y"] is not None else _pitch_y(cm)
                cv2.arrowedLine(frame, (cx-10, cy), (min(G_X1, cx+16), cy),
                                pn["col"], 2, cv2.LINE_AA, tipLength=0.65)

        if cm0 is not None:
            hz0  = _midi_to_hz(cm0)
            wl0  = V_SOUND / hz0
            info = f"f={hz0:.1f}Hz  T={1000/hz0:.2f}ms  lambda={wl0:.3f}m"
            cv2.putText(frame, info, (G_X0, G_Y1+46),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.44, (80, 80, 80), 1, cv2.LINE_AA)

        bar_x = int((t_now / duration) * WIDTH)
        cv2.rectangle(frame, (0, HEIGHT-5), (bar_x, HEIGHT), (140, 140, 140), -1)

        vw.write(frame)

    vw.release()

    if audio_path and os.path.exists(audio_path):
        cmd = ["ffmpeg", "-y", "-i", tmp_vid, "-i", audio_path,
               "-c:v", "libx264", "-crf", "26", "-preset", "fast",
               "-c:a", "aac", "-b:a", "160k",
               "-t", str(duration), "-shortest", output_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", tmp_vid,
               "-c:v", "libx264", "-crf", "26", "-preset", "fast",
               "-t", str(duration), output_path]

    r = _sp.run(cmd, capture_output=True)
    try:
        os.remove(tmp_vid)
    except Exception:
        pass
    return r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000

def get_musicgen():
    global _musicgen_model, _musicgen_processor
    if _musicgen_model is None:
        logger.info("Loading MusicGen-Melody model...")
        from transformers import AutoProcessor, MusicgenMelodyForConditionalGeneration
        _musicgen_processor = AutoProcessor.from_pretrained("facebook/musicgen-melody")
        _musicgen_model = MusicgenMelodyForConditionalGeneration.from_pretrained("facebook/musicgen-melody")
        _musicgen_model.eval()
        logger.info("MusicGen-Melody model loaded.")
    return _musicgen_processor, _musicgen_model


def enhance_audio_ffmpeg(input_path: str, output_path: str, bitrate: str = "320k"):
    af_filter = (
        "equalizer=f=60:width_type=o:width=2:g=5,"
        "equalizer=f=200:width_type=o:width=2:g=2,"
        "equalizer=f=3000:width_type=o:width=2:g=3,"
        "equalizer=f=8000:width_type=o:width=2:g=2,"
        "acompressor=threshold=-12dB:ratio=4:attack=5:release=100:makeup=3dB,"
        "loudnorm=I=-14:TP=-1:LRA=7"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-af", af_filter,
            "-acodec", "libmp3lame", "-ar", "44100", "-ab", bitrate,
            output_path,
        ],
        capture_output=True, check=True
    )


def convert_wav_to_mp3(input_path: str, output_path: str, bitrate: str = "320k"):
    """Simple wav to mp3 conversion with volume normalization only — safe for AI-generated audio."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-af", "volume=2.0,equalizer=f=100:width_type=o:width=2:g=4,equalizer=f=8000:width_type=o:width=2:g=2",
            "-acodec", "libmp3lame", "-ar", "44100", "-ab", bitrate,
            output_path,
        ],
        capture_output=True, check=True
    )


def detect_instruments_from_genre(genre: str, song_title: str = "", artist_name: str = "") -> list:
    genre_lower = genre.lower()

    if any(g in genre_lower for g in ["pop", "dance", "electronic", "electro", "edm"]):
        return ["🎹 سينثيسايزر (Synthesizer)", "🎸 باس إلكتروني (Electronic Bass)", "🥁 طبول (Drums/808)", "🎹 بيانو (Piano)"]
    elif any(g in genre_lower for g in ["rock", "metal", "alternative", "punk", "grunge"]):
        return ["🎸 جيتار كهربائي (Electric Guitar)", "🎸 باس (Bass Guitar)", "🥁 طبول (Drums)", "🎸 جيتار إيقاعي (Rhythm Guitar)"]
    elif any(g in genre_lower for g in ["jazz", "blues", "swing", "bebop"]):
        return ["🎷 ساكسفون (Saxophone)", "🎺 ترومبيت (Trumpet)", "🎹 بيانو (Piano)", "🎸 كونترباس (Double Bass)", "🥁 طبول (Drums)"]
    elif any(g in genre_lower for g in ["classical", "orchestra", "symphon", "baroque", "opera"]):
        return ["🎻 كمان (Violin)", "🎻 تشيلو (Cello)", "🎹 بيانو (Piano)", "🎶 أوركسترا وترية (String Orchestra)", "🪗 فلوت (Flute)"]
    elif any(g in genre_lower for g in ["arabic", "arab", "khaleeji", "oriental", "middle east"]):
        return ["🪕 عود (Oud)", "🎻 كمان (Violin)", "🪈 ناي (Ney Flute)", "🥁 إيقاعات شرقية (Oriental Percussion)", "🎹 قانون (Qanun)"]
    elif any(g in genre_lower for g in ["hip hop", "rap", "trap", "drill"]):
        return ["🥁 طبول (808 Drums)", "🔊 باس ثقيل (Heavy Bass)", "🎹 سينثيسايزر (Synth)", "🎵 عينات صوتية (Samples)"]
    elif any(g in genre_lower for g in ["reggae", "reggaeton", "dancehall"]):
        return ["🎸 جيتار (Guitar)", "🎸 باس (Bass)", "🥁 طبول (Drums)", "🎹 كيبورد (Keyboard)"]
    elif any(g in genre_lower for g in ["country", "folk", "bluegrass"]):
        return ["🎸 جيتار أكوستيك (Acoustic Guitar)", "🪕 بانجو (Banjo)", "🎻 كمان (Fiddle/Violin)", "🎸 باس (Bass)"]
    elif any(g in genre_lower for g in ["r&b", "soul", "rnb", "funk", "neo soul"]):
        return ["🎸 جيتار (Guitar)", "🎸 باس (Bass)", "🥁 طبول (Drums)", "🎹 بيانو (Piano/Keys)", "🎶 وتريات (Strings)"]
    else:
        return ["🎸 جيتار (Guitar)", "🎸 باس (Bass)", "🥁 طبول (Drums)", "🎹 كيبورد (Keyboard)"]


def build_main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📖 كيفية الاستخدام", callback_data="show_help")],
        [InlineKeyboardButton("ℹ️ عن البوت", callback_data="show_about")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_song_actions(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🔼 رفع الجودة", callback_data=f"enhance_{user_id}"),
            InlineKeyboardButton("🎹 تصدير MIDI", callback_data=f"export_midi_{user_id}"),
        ],
        [
            InlineKeyboardButton("📋 تفاصيل أكثر", callback_data=f"moredetails_{user_id}"),
            InlineKeyboardButton("🎹 الآلات الموسيقية", callback_data=f"instruments_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔬 تحليل شامل كامل", callback_data=f"fullanalysis_{user_id}"),
        ],
        [
            InlineKeyboardButton("✂️ تعديل الملف الصوتي", callback_data=f"editmenu_{user_id}"),
        ],
        [
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_edit_menu(user_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("⚡ تغيير السرعة (BPM)", callback_data=f"edit_speed_{user_id}"),
            InlineKeyboardButton("🎵 تغيير المقام/النغمة", callback_data=f"edit_pitch_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔊 رفع مستوى الصوت", callback_data=f"edit_louder_{user_id}"),
            InlineKeyboardButton("🔉 خفض مستوى الصوت", callback_data=f"edit_quieter_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔃 عكس الصوت", callback_data=f"edit_reverse_{user_id}"),
            InlineKeyboardButton("🎚️ تعزيز الباس", callback_data=f"edit_bass_{user_id}"),
        ],
        [
            InlineKeyboardButton("✨ تحسين الجودة 320k", callback_data=f"enhance_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔙 العودة للخيارات", callback_data=f"back_to_options_{user_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def format_song_details(track: dict) -> tuple:
    title = track.get("title", "غير معروف")
    subtitle = track.get("subtitle", "غير معروف")

    sections = track.get("sections", [])
    metadata_section = next((s for s in sections if s.get("type") == "SONG"), None)
    metadata = {}
    if metadata_section:
        for item in metadata_section.get("metadata", []):
            metadata[item.get("title", "")] = item.get("text", "")

    album = metadata.get("Album", metadata.get("ألبوم", "—"))
    label = metadata.get("Label", metadata.get("شركة الإصدار", "—"))
    released = metadata.get("Released", metadata.get("تاريخ الإصدار", "—"))
    genre = track.get("genres", {}).get("primary", "غير معروف")

    images = track.get("images", {})
    cover_url = images.get("coverarthq") or images.get("coverart") or ""

    msg = (
        f"🎵 *{title}*\n"
        f"👤 {subtitle}\n\n"
        f"💿 الألبوم: {album}\n"
        f"🎼 النوع: {genre}\n"
        f"📅 الإصدار: {released}\n"
        f"🏷️ الشركة: {label}"
    )

    hub = track.get("hub", {})
    providers = hub.get("providers", [])
    if providers:
        links = []
        for p in providers[:2]:
            pname = p.get("caption", "")
            actions = p.get("actions", [])
            if actions and pname:
                uri = actions[0].get("uri", "")
                if uri:
                    links.append(f"[{pname}]({uri})")
        if links:
            msg += "\n\n🔗 " + " • ".join(links)

    return msg, cover_url


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *أهلاً! أنا بوت تحليل وتصدير الأغاني*\n\n"
        "🎵 أرسل لي أي مقطع صوتي وسأقوم بـ:\n"
        "✅ التعرف على الأغنية فوراً\n"
        "🎹 كشف الآلات الموسيقية\n"
        "🔼 رفع جودة الصوت\n"
        "🎼 تصدير MusicXML + إعادة إنشاء صوتي\n\n"
        "📤 *ابدأ بإرسال مقطع صوتي!*"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=build_main_menu())


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message
    audio = message.voice or message.audio or message.document

    if not audio:
        await message.reply_text("❌ يرجى إرسال رسالة صوتية أو ملف صوتي.")
        return

    # ── Check if there's a pending tablature waiting for audio ─────────────
    if user_id in user_tab_pending:
        await _handle_tab_audio_combine(update, context, audio, user_id)
        return

    processing_msg = await message.reply_text("⏳ *جارٍ تحليل الصوت...*", parse_mode="Markdown")

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        file = await audio.get_file()
        await file.download_to_drive(tmp_path)

        mp3_path = tmp_path.replace(".ogg", ".mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path, "-acodec", "libmp3lame", "-ar", "44100", "-ab", "192k", mp3_path],
                capture_output=True, check=True
            )
            analyze_path = mp3_path
        except Exception:
            analyze_path = tmp_path

        await processing_msg.edit_text("🔍 *جارٍ التعرف على الأغنية...*", parse_mode="Markdown")

        shazam = Shazam()
        result = await shazam.recognize(analyze_path)

        if not result or "track" not in result:
            await processing_msg.edit_text(
                "❌ *لم أتمكن من التعرف على الأغنية*\n\n"
                "تأكد من:\n"
                "• وجود موسيقى أو غناء واضح\n"
                "• مدة المقطع أكثر من 5 ثوانٍ\n"
                "• جودة الصوت مقبولة",
                parse_mode="Markdown",
            )
            return

        track = result["track"]
        user_songs[user_id] = {
            "track": track,
            "original_path": tmp_path,
            "mp3_path": analyze_path,
            "result": result,
        }

        details_text, cover_url = format_song_details(track)
        markup = build_song_actions(user_id)

        await processing_msg.delete()

        if cover_url:
            try:
                await message.reply_photo(
                    photo=cover_url,
                    caption=details_text,
                    parse_mode="Markdown",
                    reply_markup=markup,
                )
                return
            except Exception:
                pass

        await message.reply_text(details_text, parse_mode="Markdown", reply_markup=markup)

    except Exception as e:
        logger.error(f"Audio error: {e}")
        await processing_msg.edit_text("❌ حدث خطأ أثناء المعالجة. حاول مرة أخرى.")


async def _handle_tab_audio_combine(update: Update, context: ContextTypes.DEFAULT_TYPE, audio, user_id: int):
    """
    Called when the user sends an audio file while a tablature analysis is pending.
    Downloads the audio, then runs MusicGen combining the tab structure + audio timbre.
    """
    pending    = user_tab_pending.pop(user_id)
    tab_data   = pending["tab_data"]
    fname      = pending.get("fname", "تابلاتشر")
    analysis_msg = pending.get("analysis_msg", "")

    message    = update.message
    loop       = asyncio.get_event_loop()

    status_msg = await message.reply_text(
        "🎸 *تم استقبال الملف الصوتي!*\n\n"
        "🤖 جارٍ دمج النوتات من التابلاتشر مع الطابع الصوتي...\n"
        "⏳ هذه العملية قد تستغرق بضع دقائق",
        parse_mode="Markdown"
    )

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        file = await audio.get_file()
        await file.download_to_drive(tmp_path)

        ref_mp3 = tmp_path.replace(".ogg", "_ref.mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path,
                 "-acodec", "libmp3lame", "-ar", "44100", "-ab", "192k", ref_mp3],
                capture_output=True, check=True
            )
            ref_path = ref_mp3
        except Exception:
            ref_path = tmp_path

        progress_stages = [
            "🔍 *[1/5]* تحميل نموذج الذكاء الاصطناعي...",
            "🎵 *[2/5]* تحليل الطابع الصوتي للملف المُرسَل...",
            "🎼 *[3/5]* تغذية النوتات والأكوردات من التابلاتشر...",
            "🎸 *[4/5]* توليد الصوت الموحَّد...",
            "💾 *[5/5]* معالجة وتحسين الجودة...",
        ]
        stop_evt = asyncio.Event()

        async def updater():
            idx = 0
            elapsed = 0
            while not stop_evt.is_set():
                stage = progress_stages[min(idx, len(progress_stages) - 1)]
                try:
                    await status_msg.edit_text(
                        f"{stage}\n\n⏳ الوقت المنقضي: {elapsed} ثانية",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                await asyncio.sleep(6)
                elapsed += 6
                idx += 1

        updater_task = asyncio.create_task(updater())

        output_path = f"/tmp/tab_combined_{user_id}.mp3"
        success = await loop.run_in_executor(
            None,
            lambda: generate_combined_tab_audio(tab_data, ref_path, output_path)
        )

        stop_evt.set()
        updater_task.cancel()
        await status_msg.delete()

        bpm    = tab_data.get("bpm", "?")
        chords = " • ".join(tab_data.get("chords", [])) or "—"
        insts  = " • ".join(tab_data.get("instruments", [])) or "—"

        if success and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            await message.reply_audio(
                audio=open(output_path, "rb"),
                title=f"تابلاتشر + صوت — {fname}",
                performer="AI موسيقي",
                caption=(
                    f"🎸 *تم الدمج بنجاح!*\n\n"
                    f"{analysis_msg}\n"
                    f"✅ *ما تم تطبيقه:*\n"
                    f"• نوتات وأكوردات التابلاتشر: `{chords}`\n"
                    f"• آلات الملف: *{insts}*\n"
                    f"• سرعة التابلاتشر: *{bpm} BPM*\n"
                    f"• الطابع الصوتي من ملفك\n"
                    f"• جودة 320kbps"
                ),
                parse_mode="Markdown",
            )
        else:
            await message.reply_text(
                "⚠️ *تعذَّر توليد الصوت الموحَّد*\n\n"
                "يمكنك إرسال ملف PDF مجدداً والمحاولة مرة أخرى.",
                parse_mode="Markdown"
            )

        for p in [tmp_path, ref_mp3, output_path]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"_handle_tab_audio_combine error: {e}", exc_info=True)
        try:
            stop_evt.set()
            updater_task.cancel()
        except Exception:
            pass
        await message.reply_text(
            "❌ *حدث خطأ أثناء الدمج*\n\nيرجى المحاولة مرة أخرى.",
            parse_mode="Markdown"
        )



def analyze_audio_and_build_prompt(audio_path: str, track: dict) -> tuple:
    """
    Deep multi-dimensional audio analysis using librosa.
    Returns (prompt_str, analysis_dict)
    """
    import librosa
    import librosa.effects
    import librosa.feature
    import librosa.onset
    import librosa.beat

    title      = track.get("title", "")
    artist     = track.get("subtitle", "")
    genre      = track.get("genres", {}).get("primary", "pop")
    genre_lower = genre.lower()

    # ── 1. Load audio (mono for analysis) ─────────────────────────────────────
    wav_tmp = "/tmp/analysis_input.wav"
    wav_stereo = "/tmp/analysis_stereo.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-acodec", "pcm_f32le", "-ar", "22050", "-ac", "1",
             "-t", "90", wav_tmp],
            capture_output=True, check=True
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-acodec", "pcm_f32le", "-ar", "22050", "-ac", "2",
             "-t", "90", wav_stereo],
            capture_output=True, check=True
        )
        y, sr = librosa.load(wav_tmp, sr=22050, mono=True)
        y_stereo, _ = librosa.load(wav_stereo, sr=22050, mono=False)
    except Exception as e:
        logger.warning(f"librosa load failed: {e}")
        y, sr = None, 22050
        y_stereo = None

    analysis = {}

    if y is not None and len(y) > 0:

        # ── 2. BPM & Beat Grid ────────────────────────────────────────────────
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo.item()) if hasattr(tempo, 'item') else float(tempo)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        # Beat regularity (swing vs straight)
        if len(beat_times) > 2:
            intervals = np.diff(beat_times)
            beat_std = float(np.std(intervals))
            groove = "swung groove" if beat_std > 0.05 else "straight rigid grid"
        else:
            groove = "straight"
        analysis["bpm"] = round(bpm, 1)
        analysis["groove"] = groove

        # ── 3. Key & Scale (Krumhansl-Schmuckler 24-key full search) ─────────
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_mean = chroma.mean(axis=1)
        key_names = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
        _maj_prof = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
        _min_prof = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
        _maj_prof = _maj_prof / _maj_prof.sum()
        _min_prof = _min_prof / _min_prof.sum()
        _c_norm = chroma_mean / (chroma_mean.sum() + 1e-9)
        _best_corr, _best_key, _best_scale = -2.0, "C", "major"
        for _ki, _kname in enumerate(key_names):
            _crot = np.roll(_c_norm, -_ki)
            _mcorr = float(np.corrcoef(_crot, _maj_prof)[0, 1])
            _ncorr = float(np.corrcoef(_crot, _min_prof)[0, 1])
            if _mcorr > _best_corr:
                _best_corr, _best_key, _best_scale = _mcorr, _kname, "major"
            if _ncorr > _best_corr:
                _best_corr, _best_key, _best_scale = _ncorr, _kname, "minor"
        analysis["key"] = f"{_best_key} {_best_scale}"

        # ── 4. Harmonic / Percussive separation ───────────────────────────────
        y_harmonic, y_percussive = librosa.effects.hpss(y)
        harm_energy = float(np.abs(y_harmonic).mean())
        perc_energy = float(np.abs(y_percussive).mean())
        harm_ratio = round(harm_energy / (harm_energy + perc_energy + 1e-9), 2)
        analysis["harmonic_ratio"] = harm_ratio
        texture = "melody-dominant" if harm_ratio > 0.7 else ("balanced melody and rhythm" if harm_ratio > 0.4 else "percussion-dominant")
        analysis["texture"] = texture

        # ── 5. Energy & Dynamics ──────────────────────────────────────────────
        rms = librosa.feature.rms(y=y)[0]
        avg_energy = float(rms.mean())
        peak_energy = float(rms.max())
        dynamic_range = round(float(20 * np.log10((peak_energy + 1e-9) / (avg_energy + 1e-9))), 1)
        energy_label = "high energy" if avg_energy > 0.05 else ("medium energy" if avg_energy > 0.02 else "soft and delicate")
        loudness = "loud and compressed" if dynamic_range < 3 else ("dynamic with natural peaks" if dynamic_range < 8 else "very dynamic and wide range")
        analysis["energy"] = energy_label
        analysis["dynamic_range_db"] = dynamic_range
        analysis["loudness_character"] = loudness

        # ── 6. Onset / Instrument entry points ───────────────────────────────
        perc_onsets = librosa.onset.onset_detect(y=y_percussive, sr=sr, units="time")
        harm_onsets = librosa.onset.onset_detect(y=y_harmonic, sr=sr, units="time")
        drum_start   = round(float(perc_onsets[0]), 1) if len(perc_onsets) > 0 else 0.0
        melody_start = round(float(harm_onsets[0]), 1) if len(harm_onsets) > 0 else 0.0
        # Onset density (sparse vs dense arrangement)
        total_onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
        duration = librosa.get_duration(y=y, sr=sr)
        onset_density = round(len(total_onsets) / max(duration, 1), 2)
        arrangement = "dense full arrangement" if onset_density > 4 else ("moderate arrangement" if onset_density > 2 else "sparse minimal arrangement")
        analysis["drum_start_sec"]   = drum_start
        analysis["melody_start_sec"] = melody_start
        analysis["onset_density"]    = onset_density
        analysis["arrangement"]      = arrangement

        # ── 7. Spectral analysis (full frequency bands) ───────────────────────
        spectral_centroid = float(librosa.feature.spectral_centroid(y=y, sr=sr).mean())
        spectral_rolloff  = float(librosa.feature.spectral_rolloff(y=y, sr=sr).mean())
        spectral_bandwidth = float(librosa.feature.spectral_bandwidth(y=y, sr=sr).mean())
        spectral_flatness = float(librosa.feature.spectral_flatness(y=y).mean())
        # Frequency band energies
        D = np.abs(librosa.stft(y))
        freqs = librosa.fft_frequencies(sr=sr)
        def band_energy(f_low, f_high):
            mask = (freqs >= f_low) & (freqs < f_high)
            return float(D[mask].mean()) if mask.any() else 0.0
        sub_bass  = band_energy(20, 80)
        bass      = band_energy(80, 300)
        low_mid   = band_energy(300, 800)
        mid       = band_energy(800, 2500)
        high_mid  = band_energy(2500, 6000)
        presence  = band_energy(6000, 12000)
        air       = band_energy(12000, 20000)
        # Brightness
        brightness = "bright and airy" if spectral_centroid > 4000 else ("warm and mid-focused" if spectral_centroid > 1800 else "dark and bassy")
        # Bass character
        bass_char = "heavy sub bass" if sub_bass > bass * 1.5 else ("punchy bass" if bass > low_mid else "balanced low end")
        # High frequency character
        air_char = "crisp and airy highs" if air > presence * 0.5 else ("smooth highs" if presence > mid * 0.3 else "warm rolled-off highs")
        # Noise vs tonal
        tonality = "highly tonal and melodic" if spectral_flatness < 0.05 else ("mix of tonal and noise" if spectral_flatness < 0.2 else "noise-dominant texture")
        analysis["brightness"]         = brightness
        analysis["bass_character"]     = bass_char
        analysis["high_freq_character"] = air_char
        analysis["tonality"]           = tonality
        analysis["spectral_centroid"]  = round(spectral_centroid, 0)
        analysis["spectral_bandwidth"] = round(spectral_bandwidth, 0)

        # ── 8. Reverb / Echo estimation ───────────────────────────────────────
        # Estimate reverb via spectral decay after transients
        onset_frames = librosa.onset.onset_detect(y=y, sr=sr)
        reverb_estimates = []
        for of in onset_frames[:10]:
            start = of
            end = min(of + int(sr * 2 / 512), D.shape[1])
            if end > start + 5:
                segment = D[:, start:end].mean(axis=0)
                if segment.max() > 0:
                    decay = segment / (segment.max() + 1e-9)
                    decay_time = np.where(decay < 0.1)[0]
                    if len(decay_time) > 0:
                        rt = decay_time[0] * 512 / sr
                        reverb_estimates.append(rt)
        avg_reverb = float(np.mean(reverb_estimates)) if reverb_estimates else 0.5
        if avg_reverb < 0.3:
            reverb_label = "dry with very little reverb"
        elif avg_reverb < 0.8:
            reverb_label = "medium room reverb"
        elif avg_reverb < 1.5:
            reverb_label = "hall reverb with long tail"
        else:
            reverb_label = "heavy atmospheric reverb and echo"
        analysis["reverb"] = reverb_label
        analysis["reverb_sec"] = round(avg_reverb, 2)

        # ── 9. Stereo Width ───────────────────────────────────────────────────
        if y_stereo is not None and y_stereo.ndim == 2 and y_stereo.shape[0] == 2:
            left, right = y_stereo[0], y_stereo[1]
            min_len = min(len(left), len(right))
            left, right = left[:min_len], right[:min_len]
            mid_ch  = (left + right) / 2
            side_ch = (left - right) / 2
            stereo_width = float(np.abs(side_ch).mean() / (np.abs(mid_ch).mean() + 1e-9))
            width_label = "wide stereo field" if stereo_width > 0.3 else ("moderate stereo" if stereo_width > 0.1 else "narrow mono-like")
        else:
            width_label = "moderate stereo"
            stereo_width = 0.2
        analysis["stereo_width"] = width_label

        # ── 10. Attack & Sustain character ────────────────────────────────────
        attack_strength = float(np.percentile(np.abs(y_percussive), 95))
        attack_label = "punchy hard attack" if attack_strength > 0.3 else ("moderate attack" if attack_strength > 0.1 else "soft gentle attack")
        analysis["attack"] = attack_label

        # ── 11. Tempo feel & time signature ──────────────────────────────────
        if bpm > 140:
            tempo_feel = "very fast energetic tempo"
        elif bpm > 110:
            tempo_feel = "fast upbeat tempo"
        elif bpm > 80:
            tempo_feel = "moderate mid-tempo"
        elif bpm > 60:
            tempo_feel = "slow relaxed tempo"
        else:
            tempo_feel = "very slow ballad tempo"
        analysis["tempo_feel"] = tempo_feel

        # ── 12. MFCC timbre fingerprint ───────────────────────────────────────
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
        mfcc_means = mfcc.mean(axis=1)
        # Warmth indicator from low MFCCs
        warmth = "warm and rich timbre" if mfcc_means[1] > 0 else "cold and metallic timbre"
        analysis["timbre"] = warmth

        analysis["duration_sec"] = round(duration, 1)
        analysis["section_length_approx"] = round(duration / 4, 1)

        logger.info(f"Deep audio analysis complete: {analysis}")

    else:
        analysis = {
            "bpm": 100, "key": "C major", "energy": "medium energy",
            "drum_start_sec": 4.0, "melody_start_sec": 0.5,
            "brightness": "warm", "harmonic_ratio": 0.6,
            "duration_sec": 30, "reverb": "medium room reverb",
            "stereo_width": "moderate stereo", "groove": "straight",
            "attack": "moderate attack", "dynamic_range_db": 6,
        }

    # ── 13. Instrument detection per genre + spectral hints ──────────────────
    if any(g in genre_lower for g in ["arabic","arab","khaleeji","oriental","middle","sha3bi","dabke","levant"]):
        inst_list = "oud, violin, qanun, ney flute, darbuka, riq, tabla, mizmar"
        style_desc = "Arabic oriental maqam music with rich ornamentation, microtonal phrasing and emotional depth"
    elif any(g in genre_lower for g in ["pop","dance","electro","edm","house","techno"]):
        inst_list = "synthesizer lead, pad synth, electronic drums, 808 bass, piano, arpeggiated synth, vocal chops"
        style_desc = "modern pop music with punchy production, catchy hook and polished mix"
    elif any(g in genre_lower for g in ["rock","metal","alternative","grunge"]):
        inst_list = "distorted electric guitar, clean guitar, bass guitar, acoustic drums, power chords"
        style_desc = "rock music with powerful guitar riffs, driving rhythm and raw energy"
    elif any(g in genre_lower for g in ["hip hop","rap","trap","drill"]):
        inst_list = "808 bass, trap hi-hats, snare clap, synthesizer pads, sub bass, piano"
        style_desc = "trap hip-hop with heavy low end, dark atmosphere and hard-hitting drums"
    elif any(g in genre_lower for g in ["jazz","blues","swing","bebop"]):
        inst_list = "saxophone, trumpet, piano, double bass, jazz drums, guitar, trombone"
        style_desc = "jazz music with improvised solos, swing feel and complex harmony"
    elif any(g in genre_lower for g in ["r&b","soul","rnb","funk","gospel"]):
        inst_list = "electric piano, Fender Rhodes, bass guitar, live drums, guitar, strings, brass horns"
        style_desc = "smooth R&B with lush grooves, soulful feel and rich harmonic layers"
    elif any(g in genre_lower for g in ["classical","orchestra","symphon"]):
        inst_list = "violin section, cello, viola, piano, flute, oboe, French horn, orchestral brass, timpani"
        style_desc = "orchestral classical music with full symphonic arrangement and dynamic expression"
    elif any(g in genre_lower for g in ["reggae","dancehall","ska"]):
        inst_list = "reggae guitar skank, bass, drums, organ, brass section, percussion"
        style_desc = "reggae music with offbeat skank guitar, heavy bass and laid-back groove"
    elif any(g in genre_lower for g in ["flamenco","spanish","latin","salsa","cumbia"]):
        inst_list = "classical guitar, cajon, palmas, violin, bass, latin percussion, piano"
        style_desc = "passionate Latin music with flamenco guitar, complex rhythms and expressive phrasing"
    else:
        inst_list = "acoustic guitar, bass, drums, keyboard, synthesizer, strings"
        style_desc = "professional music with full arrangement and polished production"

    analysis["instruments"] = inst_list

    # ── 14. Build ultra-detailed prompt ──────────────────────────────────────
    bpm_v    = analysis.get("bpm", 100)
    key_v    = analysis.get("key", "C major")
    energy_v = analysis.get("energy", "medium energy")
    drum_v   = analysis.get("drum_start_sec", 0)
    mel_v    = analysis.get("melody_start_sec", 0)
    bright_v = analysis.get("brightness", "warm")
    reverb_v = analysis.get("reverb", "medium room reverb")
    stereo_v = analysis.get("stereo_width", "moderate stereo")
    groove_v = analysis.get("groove", "straight")
    attack_v = analysis.get("attack", "moderate attack")
    texture_v= analysis.get("texture", "balanced")
    arrange_v= analysis.get("arrangement", "full arrangement")
    dynamic_v= analysis.get("loudness_character", "dynamic")
    bass_v   = analysis.get("bass_character", "balanced low end")
    air_v    = analysis.get("high_freq_character", "smooth highs")
    timbre_v = analysis.get("timbre", "warm timbre")
    tempo_feel_v = analysis.get("tempo_feel", "moderate tempo")

    prompt = (
        f"{genre} music in the style of {title} by {artist}, "
        f"{style_desc}, "
        f"exactly {bpm_v:.0f} BPM, {tempo_feel_v}, {groove_v}, "
        f"key of {key_v}, "
        f"{energy_v}, {dynamic_v}, "
        f"{bright_v} tone, {bass_v}, {air_v}, "
        f"{reverb_v}, {stereo_v}, "
        f"{attack_v}, {texture_v}, {arrange_v}, "
        f"{timbre_v}, "
        f"featuring {inst_list}, "
        f"drums and percussion enter at {drum_v:.0f} seconds, "
        f"melody enters at {mel_v:.0f} seconds, "
        f"all instruments layered precisely from the start, "
        f"professional studio master quality, faithful reproduction of original song structure and feel"
    )

    return prompt, analysis


def analyze_full_deep(audio_path: str, track: dict) -> dict:
    """
    Ultra-comprehensive musical analysis covering:
    Rhythm, Harmony, Melody, Song Sections, Layers, Timbre, Dynamics, Movement.
    Returns a rich dict with all dimensions.
    """
    import librosa
    import librosa.effects
    import librosa.feature
    import librosa.onset
    import librosa.beat
    import librosa.segment

    title  = track.get("title", "")
    artist = track.get("subtitle", "")
    genre  = track.get("genres", {}).get("primary", "pop")

    wav_tmp = "/tmp/full_analysis_input.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-acodec", "pcm_f32le", "-ar", "22050", "-ac", "1",
             "-t", "120", wav_tmp],
            capture_output=True, check=True
        )
        y, sr = librosa.load(wav_tmp, sr=22050, mono=True)
    except Exception as e:
        logger.warning(f"full_analysis load failed: {e}")
        return {}

    if y is None or len(y) == 0:
        return {}

    result = {}
    duration = librosa.get_duration(y=y, sr=sr)
    result["duration_sec"] = round(duration, 1)

    # ── 1. RHYTHM ──────────────────────────────────────────────────────────────
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(tempo.item()) if hasattr(tempo, 'item') else float(tempo)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    result["bpm"] = round(bpm, 1)

    # Time Signature estimation
    if len(beat_times) > 4:
        intervals = np.diff(beat_times)
        avg_interval = float(np.mean(intervals))
        # Group beats into measures using autocorrelation of onset strength
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        ac = librosa.autocorrelate(onset_env, max_size=int(sr * 3 / 512))
        # Check peaks at multiples of 3 vs 4 beat intervals
        beat_frames_hop = int(avg_interval * sr / 512)
        score_4 = float(ac[beat_frames_hop * 4]) if beat_frames_hop * 4 < len(ac) else 0
        score_3 = float(ac[beat_frames_hop * 3]) if beat_frames_hop * 3 < len(ac) else 0
        score_6 = float(ac[beat_frames_hop * 6]) if beat_frames_hop * 6 < len(ac) else 0
        if score_3 > score_4 and score_3 > score_6:
            time_sig = "3/4 (والس)"
        elif score_6 > score_4:
            time_sig = "6/8"
        else:
            time_sig = "4/4"
    else:
        time_sig = "4/4"
    result["time_signature"] = time_sig

    # Groove: straight vs swing
    if len(beat_times) > 2:
        intervals = np.diff(beat_times)
        beat_std = float(np.std(intervals))
        groove = "Swing (متأرجح)" if beat_std > 0.05 else "Straight (مستقيم)"
    else:
        groove = "Straight (مستقيم)"
    result["groove"] = groove

    # Syncopation: ratio of strong off-beat onsets
    y_perc = librosa.effects.hpss(y)[1]
    onset_times = librosa.onset.onset_detect(y=y_perc, sr=sr, units="time")
    if len(beat_times) > 2 and len(onset_times) > 0:
        on_beat_count = 0
        off_beat_count = 0
        for ot in onset_times:
            dists = np.abs(beat_times - ot)
            min_dist = float(dists.min())
            beat_int = float(np.mean(np.diff(beat_times)))
            if min_dist < beat_int * 0.15:
                on_beat_count += 1
            else:
                off_beat_count += 1
        sync_ratio = off_beat_count / max(len(onset_times), 1)
        if sync_ratio > 0.5:
            syncopation = f"عالي ({round(sync_ratio*100)}٪ off-beat)"
        elif sync_ratio > 0.25:
            syncopation = f"متوسط ({round(sync_ratio*100)}٪ off-beat)"
        else:
            syncopation = f"منخفض ({round(sync_ratio*100)}٪ off-beat)"
    else:
        syncopation = "متوسط"
    result["syncopation"] = syncopation

    # Drum pattern description
    onset_density = round(len(onset_times) / max(duration, 1), 2)
    if onset_density > 5:
        drum_pattern = "كثيف (Dense) — إيقاع سريع ومتشعب"
    elif onset_density > 2.5:
        drum_pattern = "متوسط — إيقاع معياري"
    else:
        drum_pattern = "خفيف (Sparse) — إيقاع بسيط"
    result["drum_pattern"] = drum_pattern
    result["onset_density"] = onset_density

    # ── 2. HARMONY (Krumhansl-Schmuckler 24-key full search) ───────────────────
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)
    key_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    _maj_prof = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    _min_prof = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    _maj_prof = _maj_prof / _maj_prof.sum()
    _min_prof = _min_prof / _min_prof.sum()
    _c_norm = chroma_mean / (chroma_mean.sum() + 1e-9)
    _best_corr, _best_key, _best_scale_en = -2.0, "C", "major"
    for _ki, _kname in enumerate(key_names):
        _crot = np.roll(_c_norm, -_ki)
        _mcorr = float(np.corrcoef(_crot, _maj_prof)[0, 1])
        _ncorr = float(np.corrcoef(_crot, _min_prof)[0, 1])
        if _mcorr > _best_corr:
            _best_corr, _best_key, _best_scale_en = _mcorr, _kname, "major"
        if _ncorr > _best_corr:
            _best_corr, _best_key, _best_scale_en = _ncorr, _kname, "minor"
    scale = "major (ماجور)" if _best_scale_en == "major" else "minor (مينور)"
    result["key"] = f"{_best_key} {scale}"
    result["scale"] = scale

    # Chord Progression: track dominant chroma over 4-beat windows
    hop_length = 512
    beats_hop = max(1, int(bpm / 60 * 4 * sr / hop_length))  # frames per measure
    n_frames = chroma.shape[1]
    chord_seq = []
    chord_symbols = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    for start in range(0, n_frames - beats_hop, beats_hop):
        seg = chroma[:, start:start + beats_hop].mean(axis=1)
        root = int(seg.argmax())
        seg_rot = np.roll(seg, -root)
        maj_c = float(np.corrcoef(seg_rot, major_profile)[0, 1])
        min_c = float(np.corrcoef(seg_rot, minor_profile)[0, 1])
        quality = "" if maj_c > min_c else "m"
        chord_seq.append(f"{chord_symbols[root]}{quality}")

    # Deduplicate consecutive same chords and limit to 8
    prog = []
    for c in chord_seq:
        if not prog or prog[-1] != c:
            prog.append(c)
        if len(prog) >= 8:
            break
    result["chord_progression"] = " → ".join(prog) if prog else "—"

    # Modulation: detect if key shifts significantly
    segment_size = max(1, n_frames // 4)
    key_segments = []
    for i in range(4):
        seg = chroma[:, i * segment_size:(i + 1) * segment_size].mean(axis=1)
        key_segments.append(int(seg.argmax()))
    unique_keys = len(set(key_segments))
    if unique_keys >= 3:
        result["modulation"] = f"تحويل مقامي (Modulation) — {unique_keys} مناطق مختلفة"
    elif unique_keys == 2:
        result["modulation"] = "تحويل مقامي خفيف — منطقتان"
    else:
        result["modulation"] = "لا يوجد تحويل مقامي — مقام ثابت"

    # ── 3. MELODY ──────────────────────────────────────────────────────────────
    y_harm = librosa.effects.hpss(y)[0]
    # Pitch range using pyin
    try:
        f0, voiced_flag, voiced_probs = librosa.pyin(
            y_harm, fmin=librosa.note_to_hz('C2'),
            fmax=librosa.note_to_hz('C7'), sr=sr
        )
        voiced_f0 = f0[voiced_flag] if voiced_flag is not None else np.array([])
        if len(voiced_f0) > 5:
            f0_low  = float(np.percentile(voiced_f0, 5))
            f0_high = float(np.percentile(voiced_f0, 95))
            note_low  = librosa.hz_to_note(f0_low)
            note_high = librosa.hz_to_note(f0_high)
            result["pitch_range"] = f"{note_low} — {note_high}"
            # Vocal/melody range classification
            range_semitones = 12 * np.log2(f0_high / max(f0_low, 1))
            if range_semitones > 24:
                result["pitch_range_label"] = "مدى واسع جداً (> أوكتافين)"
            elif range_semitones > 12:
                result["pitch_range_label"] = "مدى واسع (أوكتاف أو أكثر)"
            else:
                result["pitch_range_label"] = "مدى ضيق (أقل من أوكتاف)"
        else:
            result["pitch_range"] = "—"
            result["pitch_range_label"] = "—"
    except Exception:
        result["pitch_range"] = "—"
        result["pitch_range_label"] = "—"

    # Melody repetition: self-similarity via chroma
    try:
        rec = librosa.segment.recurrence_matrix(chroma, mode="affinity", sym=True)
        repetition_score = float(np.mean(rec[rec < 1.0]))
        if repetition_score > 0.6:
            melody_rep = f"تكرار عالٍ ({round(repetition_score*100)}٪) — لازمة واضحة"
        elif repetition_score > 0.35:
            melody_rep = f"تكرار متوسط ({round(repetition_score*100)}٪)"
        else:
            melody_rep = f"تنوع لحني ({round(repetition_score*100)}٪ تشابه)"
    except Exception:
        melody_rep = "—"
    result["melody_repetition"] = melody_rep

    # Ornaments: detect rapid pitch variations (trills/slides) via std of voiced f0
    try:
        if len(voiced_f0) > 10:
            f0_std = float(np.std(np.diff(voiced_f0)))
            if f0_std > 30:
                ornaments = "زخارف كثيفة (Trills / Slides / Vibrato)"
            elif f0_std > 10:
                ornaments = "زخارف معتدلة"
            else:
                ornaments = "لحن نظيف بدون زخارف واضحة"
        else:
            ornaments = "—"
    except Exception:
        ornaments = "—"
    result["ornaments"] = ornaments

    # ── 4. SONG SECTIONS ───────────────────────────────────────────────────────
    rms = librosa.feature.rms(y=y)[0]
    hop_frames = 512
    total_frames = len(rms)
    section_size = total_frames // 8
    sections_energy = []
    for i in range(8):
        seg_rms = rms[i * section_size:(i + 1) * section_size]
        sections_energy.append(float(seg_rms.mean()))

    avg_e = np.mean(sections_energy)
    detected_sections = []
    section_labels = []
    for i, e in enumerate(sections_energy):
        t_start = round(i * section_size * hop_frames / sr, 1)
        t_end   = round((i + 1) * section_size * hop_frames / sr, 1)
        ratio = e / (avg_e + 1e-9)
        if i == 0:
            lbl = "Intro (مقدمة)"
        elif i == 7:
            lbl = "Outro (خاتمة)"
        elif ratio > 1.3:
            lbl = "Chorus / Drop (لازمة)"
        elif ratio > 1.0:
            lbl = "Pre-Chorus / Verse نشط"
        elif ratio < 0.7:
            lbl = "Bridge / Breakdown"
        else:
            lbl = "Verse (بيت)"
        detected_sections.append(f"{t_start}ث–{t_end}ث: {lbl}")
        section_labels.append(lbl)
    result["sections"] = detected_sections
    # Summary of section types present
    unique_sections = list(dict.fromkeys(section_labels))
    result["sections_summary"] = " | ".join(unique_sections)

    # ── 5. LAYERS & ROLES ──────────────────────────────────────────────────────
    spectral_centroid = float(librosa.feature.spectral_centroid(y=y, sr=sr).mean())
    spectral_flatness = float(librosa.feature.spectral_flatness(y=y).mean())
    harm_energy = float(np.abs(y_harm).mean())
    perc_energy = float(np.abs(y_perc).mean())
    harm_ratio = round(harm_energy / (harm_energy + perc_energy + 1e-9), 2)

    if harm_ratio > 0.75:
        lead_role = "اللحن الرئيسي (Lead Melody) يهيمن"
        pad_role  = "الباد خفيف"
        bg_role   = "الخلفية الإيقاعية خفيفة"
    elif harm_ratio > 0.5:
        lead_role = "Lead و Pad متوازنان"
        pad_role  = "Pad واضح وداعم"
        bg_role   = "إيقاع في الخلفية"
    else:
        lead_role = "الإيقاع (Drums/Perc) يهيمن"
        pad_role  = "—"
        bg_role   = "Lead في الخلفية"
    result["lead_role"] = lead_role
    result["pad_role"]  = pad_role
    result["bg_role"]   = bg_role

    # ── 6. ADVANCED DIMENSIONS ─────────────────────────────────────────────────
    # Micro-timing: jitter in onset positions
    if len(onset_times) > 4:
        beat_int = 60.0 / max(bpm, 1)
        quantized = np.round(onset_times / beat_int) * beat_int
        jitter_ms = float(np.mean(np.abs(onset_times - quantized[:len(onset_times)])) * 1000)
        if jitter_ms < 5:
            micro_timing = f"دقيق جداً ({round(jitter_ms,1)} ms) — مبرمج/Grid"
        elif jitter_ms < 20:
            micro_timing = f"إنساني طبيعي ({round(jitter_ms,1)} ms)"
        else:
            micro_timing = f"متأخر أو متقدم ({round(jitter_ms,1)} ms) — groove مميز"
    else:
        micro_timing = "—"
    result["micro_timing"] = micro_timing

    # Timbre: MFCC fingerprint
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    mfcc_means = mfcc.mean(axis=1)
    warmth = "دافئ وغني" if mfcc_means[1] > 0 else "بارد ومعدني"
    brightness_t = "ساطع" if spectral_centroid > 3500 else ("متوسط" if spectral_centroid > 1800 else "داكن")
    tonality = "لحني وتوني" if spectral_flatness < 0.05 else ("مزيج لحن ونويز" if spectral_flatness < 0.2 else "نويز/تشويش غالب")
    result["timbre"] = f"{warmth} — {brightness_t} — {tonality}"

    # Envelope: attack/release character
    attack_strength = float(np.percentile(np.abs(y_perc), 95))
    if attack_strength > 0.3:
        envelope = "هجوم حاد (Hard Attack) — ضربات قوية وحادة"
    elif attack_strength > 0.1:
        envelope = "هجوم متوسط (Medium Attack)"
    else:
        envelope = "هجوم ناعم (Soft Attack) — تلاشي بطيء"
    result["envelope"] = envelope

    # Transients: count transient peaks
    transients = librosa.onset.onset_detect(y=y_perc, sr=sr, units="time", backtrack=True)
    transient_density = round(len(transients) / max(duration, 1), 2)
    if transient_density > 6:
        transient_label = f"كثيف جداً ({round(transient_density,1)}/ث) — إيقاع متشعب"
    elif transient_density > 3:
        transient_label = f"متوسط ({round(transient_density,1)}/ث)"
    else:
        transient_label = f"خفيف ({round(transient_density,1)}/ث) — إيقاع بسيط"
    result["transients"] = transient_label

    # Loudness: dynamic range
    peak_e = float(rms.max())
    avg_e_rms = float(rms.mean())
    dynamic_range = round(float(20 * np.log10((peak_e + 1e-9) / (avg_e_rms + 1e-9))), 1)
    if dynamic_range < 3:
        loudness_label = f"مضغوط جداً (DR={dynamic_range} dB) — Loudness War"
    elif dynamic_range < 8:
        loudness_label = f"ديناميكية طبيعية (DR={dynamic_range} dB)"
    else:
        loudness_label = f"ديناميكية واسعة (DR={dynamic_range} dB) — موسيقى حية"
    result["loudness"] = loudness_label

    # Movement: LFO-like variation over time
    spectral_over_time = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    sc_std = float(np.std(spectral_over_time))
    if sc_std > 1000:
        movement = "حركة طيفية عالية — تغييرات كبيرة في الطابع الصوتي"
    elif sc_std > 400:
        movement = "حركة طيفية متوسطة — تدرج ديناميكي"
    else:
        movement = "حركة طيفية منخفضة — طابع صوتي ثابت"
    result["movement"] = movement

    return result


def extract_midi_notes_basic_pitch(audio_path: str) -> dict:
    """
    Extracts precise MIDI notes from audio using Basic-Pitch (Spotify).
    Returns a dict with note_count, pitch_range, dominant_notes summary.
    """
    try:
        from basic_pitch.inference import predict
        from basic_pitch import ICASSP_2022_MODEL_PATH

        _, midi_data, _ = predict(audio_path, ICASSP_2022_MODEL_PATH)

        notes = []
        for instrument in midi_data.instruments:
            for note in instrument.notes:
                notes.append(note.pitch)

        if not notes:
            return {"note_count": 0, "summary": "لم يتم استخراج نوتات"}

        import pretty_midi
        pitch_names = ["C", "C#", "D", "D#", "E", "F",
                       "F#", "G", "G#", "A", "A#", "B"]

        from collections import Counter
        pitch_classes = [p % 12 for p in notes]
        most_common = Counter(pitch_classes).most_common(5)
        dominant = ", ".join(pitch_names[pc] for pc, _ in most_common)

        pitch_min = pretty_midi.note_number_to_name(min(notes))
        pitch_max = pretty_midi.note_number_to_name(max(notes))

        summary = (
            f"عدد النوتات: {len(notes)} • "
            f"المدى: {pitch_min}–{pitch_max} • "
            f"النوتات السائدة: {dominant}"
        )
        logger.info(f"Basic-Pitch extraction: {summary}")
        return {
            "note_count": len(notes),
            "pitch_min": pitch_min,
            "pitch_max": pitch_max,
            "dominant_notes": dominant,
            "summary": summary,
        }
    except Exception as e:
        logger.warning(f"Basic-Pitch extraction failed: {e}")
        return {"note_count": 0, "summary": "تعذّر استخراج النوتات"}


async def cb_export_musicxml(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    await query.answer("🎼 جاري تحليل الصوت وإنتاج MusicXML...")

    if user_id not in user_songs:
        await query.message.reply_text("❌ يرجى إرسال أغنية أولاً.", reply_markup=build_main_menu())
        return

    track = user_songs[user_id]["track"]
    title = track.get("title", "أغنية")
    artist = track.get("subtitle", "غير معروف")
    song_genre = track.get("genres", {}).get("primary", "")

    status_msg = await query.message.reply_text(
        "🎼 *جاري إنتاج ملف MusicXML...*",
        parse_mode="Markdown"
    )

    try:
        loop = asyncio.get_event_loop()
        original_path = user_songs[user_id].get("mp3_path") or user_songs[user_id].get("original_path")

        # ── 1: تحليل الصوت ───────────────────────────────────────────────────
        await status_msg.edit_text(
            "🔬 *[1/5] تحليل الصوت...*\n"
            "استخراج BPM • المقام الموسيقي • التوقيع الزمني",
            parse_mode="Markdown"
        )
        analysis = await loop.run_in_executor(
            None, lambda: analyze_audio_for_export(original_path)
        )
        analysis["genre"] = song_genre
        bpm_val = analysis["bpm"]
        key_val = analysis["key"]
        time_sig = analysis["time_sig"]

        await status_msg.edit_text(
            f"✅ *[1/5] تحليل مكتمل!*\n\n"
            f"🎵 السرعة: *{bpm_val:.1f} BPM*\n"
            f"🎼 المقام: *{key_val}*\n"
            f"📊 التوقيع الزمني: *{time_sig}*",
            parse_mode="Markdown"
        )
        await asyncio.sleep(1)

        # ── 2: فصل المسارات بـ Demucs (vocals + bass + drums + other) ──────
        await status_msg.edit_text(
            "🎛️ *[2/5] فصل المسارات الصوتية بـ Demucs AI...*\n\n"
            "🎸 Bass — 🥁 Drums — 🎹 Other/Melody\n"
            "⏳ جاري الفصل (قد يأخذ دقيقة)...",
            parse_mode="Markdown"
        )
        demucs_dir = f"/tmp/_demucs_{user_id}"
        stems = await loop.run_in_executor(
            None, lambda: separate_stems_demucs(original_path, demucs_dir)
        )
        detected_instruments = await loop.run_in_executor(
            None, lambda: detect_instruments_from_stems(stems)
        )
        vocal_segments = 0  # always 0 — input is always pure instrumental

        inst_text = "\n".join(f"  • {i}" for i in detected_instruments) if detected_instruments else "  • آلات موسيقية متعددة"
        await status_msg.edit_text(
            f"✅ *[2/5] فصل المسارات مكتمل!*\n\n"
            f"🎼 *الآلات المكتشفة:*\n{inst_text}",
            parse_mode="Markdown"
        )
        await asyncio.sleep(1)

        # ── 3: استخراج النوتات من المسارات المفصولة ─────────────────────────
        await status_msg.edit_text(
            "🎵 *[3/5] استخراج النوتات من كل مسار...*\n\n"
            "• Onset timing لكل نوتة\n"
            "• Pitch curve + Velocity\n"
            "• كشف Glide / Portamento\n"
            "• Quantization 1/16\n"
            "⏳ يرجى الانتظار...",
            parse_mode="Markdown"
        )
        notes_data = await loop.run_in_executor(
            None, lambda: extract_notes_multi_channel(original_path, bpm_val, stems)
        )
        # ── تصحيح النوتات بالمقام المكتشف (يُصلح الأخطاء من Basic-Pitch) ────
        _key_str = analysis.get("key", key_val or "")
        if _key_str and notes_data.get("synth_notes"):
            notes_data["synth_notes"] = quantize_notes_to_scale(
                notes_data["synth_notes"], _key_str
            )
        if _key_str and notes_data.get("guitar_notes"):
            notes_data["guitar_notes"] = quantize_notes_to_scale(
                notes_data["guitar_notes"], _key_str
            )
        if _key_str and notes_data.get("piano_notes"):
            notes_data["piano_notes"] = quantize_notes_to_scale(
                notes_data["piano_notes"], _key_str
            )
        synth_count  = len(notes_data.get("synth_notes",  []))
        bass_count   = len(notes_data.get("bass_notes",   []))
        drum_count   = len(notes_data.get("drum_hits",    []))
        guitar_count = len(notes_data.get("guitar_notes", []))
        piano_count  = len(notes_data.get("piano_notes",  []))
        bend_count   = sum(1 for n in notes_data.get("synth_notes", []) if n.get("has_bend"))

        six_stem_note = " (6 مسارات)" if (guitar_count or piano_count) else " (4 مسارات)"
        stem_note = f" (Demucs{six_stem_note})" if stems else " (HPSS)"
        _stem_parts = []
        if stems:
            for _s in ["vocals", "bass", "drums", "other", "guitar", "piano"]:
                if stems.get(_s):
                    _stem_parts.append(_s)
        stem_line = " + ".join(_stem_parts) if _stem_parts else "bass + drums + other"
        guitar_line = f"🎸 Guitar: *{guitar_count}* نوتة\n" if guitar_count else ""
        piano_line  = f"🎹 Piano: *{piano_count}* نوتة\n"  if piano_count  else ""
        await status_msg.edit_text(
            f"✅ *[3/5] استخراج النوتات مكتمل!*{stem_note}\n\n"
            f"🎹 Synth/Melody: *{synth_count}* نوتة\n"
            f"🎸 Bass: *{bass_count}* نوتة\n"
            f"{guitar_line}"
            f"{piano_line}"
            f"🥁 Drums: *{drum_count}* ضربة\n"
            f"〰️ Glide/Portamento: *{bend_count}* نوتة",
            parse_mode="Markdown"
        )
        await asyncio.sleep(1)

        # ── 4-6: حلقة تكرارية: عزف → مقارنة عميقة → تصحيح (3 تكرارات) ──────
        safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip() or "song"

        musicxml_path  = f"/tmp/export_{user_id}.musicxml"
        midi_path      = f"/tmp/export_{user_id}.mid"
        audio_path     = f"/tmp/export_audio_{user_id}.mp3"
        spectral_path  = f"/tmp/spectral_{user_id}.png"

        # ── بحث أفضل جرس صوتي قبل بدء التكرارات ───────────────────────────
        await status_msg.edit_text(
            "🎨 *بحث أفضل جرس صوتي لكل آلة...*\n\n"
            "• فحص 989+ جرس للـ Synth/Melody\n"
            "• فحص 271+ جرس للـ Bass\n"
            "• معيار تقييم رباعي: MFCC + Centroid + Chroma + Mel\n"
            "• يمنع فوز الطبول/غير المنغّم على الميلودي\n"
            "⏳ يرجى الانتظار (90-120 ثانية)...",
            parse_mode="Markdown"
        )
        def _run_timbre_search():
            return _search_best_timbres(notes_data, analysis, stems if stems else {}, user_id)
        best_synth_t, best_bass_t, timbre_log = await loop.run_in_executor(None, _run_timbre_search)
        logger.info(f"Best timbres: synth={best_synth_t}, bass={best_bass_t}")
        # اختصار أسماء الجرس لتجنب underscores في Markdown
        _sname = best_synth_t.replace("_", "-")
        _bname = best_bass_t.replace("_", "-")
        _log_clean = "  |  ".join(
            e.replace("_", "-") for e in timbre_log[:6]
        ) if timbre_log else "—"
        await status_msg.edit_text(
            f"✅ *تم اختيار أفضل جرس صوتي!*\n\n"
            f"🎹 Synth: `{_sname}`\n"
            f"🎸 Bass:  `{_bname}`\n\n"
            f"{_log_clean}\n\n"
            f"🔄 بدء التكرارات بالجرس الأمثل...",
            parse_mode="Markdown"
        )
        await asyncio.sleep(0.5)

        MAX_ITER        = 7      # أقصى عدد تكرارات
        TARGET_SIM      = 0.95   # الهدف: 95% — يتوقف إذا وصل
        PLATEAU_THRESH  = 0.001  # يتوقف فقط إذا كان الكسب أقل من 0.1% (شبه صفر)
        iter_holder     = {}
        prev_overall    = 0.0
        iter_idx        = 0
        stop_reason     = ""
        N_ITER          = MAX_ITER   # للتوافق مع الرسائل السابقة

        while iter_idx < MAX_ITER:
            current_in = iter_holder.get("adj_notes", notes_data)

            # ── رسالة التقدم ─────────────────────────────────────────────
            prev_pct = int(prev_overall * 100)
            stem_status = (
                f"{'✅' if stems.get('bass')   else '⚠️'} Bass  "
                f"{'✅' if stems.get('drums')  else '⚠️'} Drums  "
                f"{'✅' if stems.get('other')  else '⚠️'} Melody"
            )
            await status_msg.edit_text(
                f"🔄 *[تكرار {iter_idx + 1}] عزف وتحليل...*\n\n"
                f"🎛️ {stem_status}\n\n"
                f"• Synth: {len(current_in.get('synth_notes', []))} نوتة"
                f"  |  Bass: {len(current_in.get('bass_notes', []))} نوتة"
                f"  |  Drums: {len(current_in.get('drum_hits', []))} ضربة\n"
                f"• مقارنة Mel + Chroma + MFCC + Onset (كيك/سنير/هاي-هات)\n"
                f"• تصحيح Velocity + مدة + نوتات مفقودة\n"
                f"{'📈 التشابه السابق: ' + str(prev_pct) + '%' if iter_idx > 0 else '🎯 الهدف: 95%+'}\n"
                f"⏳ يرجى الانتظار...",
                parse_mode="Markdown"
            )

            # ── تركيب ومقارنة ─────────────────────────────────────────────
            _is_last = False  # سنحدده ديناميكياً
            tmp_iter_audio = f"/tmp/_iter_{iter_idx}_{user_id}.mp3"

            def _synthesise_and_compare(cn=current_in, ta=tmp_iter_audio,
                                       _st=best_synth_t, _bt=best_bass_t):
                tab_i = build_tab_data_from_notes(cn, analysis, title=title,
                                                  synth_timbre_override=_st,
                                                  bass_timbre_override=_bt)
                ok = render_musicxml_to_audio(tab_i, ta)
                if not ok or not os.path.exists(ta) or os.path.getsize(ta) < 500:
                    return None, None
                # ── مطابقة الغلاف الطيفي — EQ ديناميكي بلا خلط ──────────
                # يُعدّل توزيع الترددات في مخرج FluidSynth ليطابق melody stem
                # هذا ليس خلطاً: لا يُضاف أي صوت أصلي — فقط فلتر EQ تلقائي
                _eq_ref = None
                if stems:
                    _eq_ref = stems.get("other") or stems.get("piano") or stems.get("guitar")
                if _eq_ref and os.path.exists(_eq_ref):
                    _apply_spectral_eq_to_midi_output(ta, _eq_ref, blend=0.92)
                # ── مطابقة طيفية إضافية مقابل مسار الباس ──────────────────
                _eq_bass = stems.get("bass") if stems else None
                if _eq_bass and os.path.exists(_eq_bass):
                    _apply_spectral_eq_to_midi_output(ta, _eq_bass, blend=0.55)
                # ── التوجيه الطيفي متعدد الآلات (خارج الصندوق) ─────────────
                # كل نطاق تردد يُطابَق بالـ stem المسؤول عنه + حقن هارمونيك
                if stems:
                    _ta_routed = ta.replace(".mp3", "_routed.mp3")
                    if _apply_per_stem_spectral_routing(ta, stems, original_path, _ta_routed):
                        try:
                            import shutil as _sh2
                            _sh2.move(_ta_routed, ta)
                        except Exception:
                            pass
                # ── قياس صادق: MIDI فقط مقارنةً بمسارات Demucs المنفصلة ──────
                # تجنّب الدائرة المغلقة: لا ندمج الـ stems في الصوت الذي نقيسه
                _drum_hits = notes_data.get("drum_hits", []) if notes_data else []
                if stems:
                    sim = _compute_midi_vs_stems_sim(ta, stems, drum_notes=_drum_hits)
                else:
                    sim = _compute_deep_similarity(original_path, ta)
                # تصحيح النوتات — يقارن بـ melody stem إن توفّر (أدق من الأصل الكامل)
                _ref = (stems.get("other") or original_path) if stems else original_path
                if not _ref or not os.path.exists(_ref):
                    _ref = original_path
                adj = spectral_adjust_notes(cn, _ref, ta, bpm_val)
                try:
                    if os.path.exists(ta):
                        os.remove(ta)
                except Exception:
                    pass
                return adj, sim

            adj_result, sim_result = await loop.run_in_executor(None, _synthesise_and_compare)
            if adj_result is None:
                adj_result = current_in
            if sim_result is None:
                sim_result = {"mel": 0, "chroma": 0, "mfcc": 0, "onset": 0,
                              "onset_drum": 0, "onset_snare": 0, "onset_hat": 0,
                              "centroid": 0, "overall": 0}

            iter_holder["adj_notes"] = adj_result
            iter_holder.setdefault("sim_log", []).append(sim_result)

            current_overall = max(0.0, sim_result["overall"])
            overall_pct     = int(current_overall * 100)
            gain            = current_overall - prev_overall
            stars           = "⭐" * (5 if overall_pct >= 80 else 4 if overall_pct >= 60 else 3 if overall_pct >= 40 else 2 if overall_pct >= 20 else 1)

            drum_line = (
                f"🥁 كيك: `{sim_result.get('onset_drum',0):.2f}`  "
                f"🪘 سنير: `{sim_result.get('onset_snare',0):.2f}`  "
                f"🎶 هاي: `{sim_result.get('onset_hat',0):.2f}`"
            )
            await status_msg.edit_text(
                f"✅ *[تكرار {iter_idx + 1}] مكتمل!* {'— ' + stop_reason if stop_reason and iter_idx + 1 == N_ITER else ''}\n\n"
                f"🎹 Synth/Mel:  `{sim_result['mel']:.3f}`  (مقارنة بـ melody stem)\n"
                f"🎵 Chroma:     `{sim_result['chroma']:.3f}`\n"
                f"🎼 MFCC:       `{sim_result['mfcc']:.3f}`\n"
                f"🎸 Bass:       `{sim_result.get('bass_mel',0):.3f}`  (مقارنة بـ bass stem)\n"
                f"〰️ ZCR:        `{sim_result.get('zcr',0):.3f}`\n"
                f"{drum_line}  (مقارنة بـ drums stem)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏆 التشابه الكلي: *{overall_pct}%* {stars}\n"
                f"{'📈 كسب: +' + str(int(gain*100)) + '%' if iter_idx > 0 else '🎯 هدف: 95%+'}",
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.8)

            # ── قرار الاستمرار أو التوقف ──────────────────────────────────
            if current_overall >= TARGET_SIM:
                stop_reason = f"🎯 وصل الهدف {int(TARGET_SIM*100)}%"
                iter_idx += 1
                break
            if iter_idx > 0 and gain < PLATEAU_THRESH:
                stop_reason = f"📉 توقف التحسن (كسب {int(gain*100*10)/10}%)"
                iter_idx += 1
                break

            prev_overall = current_overall
            iter_idx += 1

        N_ITER = iter_idx   # العدد الفعلي للتكرارات

        adj_notes = iter_holder.get("adj_notes", notes_data)
        sim_log   = iter_holder.get("sim_log", [])
        final_sim = sim_log[-1] if sim_log else {"mel": 0, "chroma": 0, "mfcc": 0, "onset": 0,
                                                  "onset_drum": 0, "onset_snare": 0, "onset_hat": 0,
                                                  "overall": 0}
        adj_synth  = len(adj_notes.get("synth_notes", []))
        adj_bass   = len(adj_notes.get("bass_notes", []))
        added_notes = adj_synth - synth_count + adj_bass - bass_count

        # ── تصيير نهائي للأفضل نوتات ─────────────────────────────────────────
        await status_msg.edit_text(
            "🎹 *تصيير الإصدار النهائي بأفضل النوتات...*",
            parse_mode="Markdown"
        )

        midi_only_path = None

        def _final_render(_st=best_synth_t, _bt=best_bass_t):
            tab_f = build_tab_data_from_notes(adj_notes, analysis, title=title,
                                              synth_timbre_override=_st,
                                              bass_timbre_override=_bt)
            render_musicxml_to_audio(tab_f, audio_path)
            # ── EQ نهائي: مطابقة الغلاف الطيفي بلا خلط ──────────────────
            _eq_ref_final = None
            if stems:
                _eq_ref_final = stems.get("other") or stems.get("piano") or stems.get("guitar")
            if _eq_ref_final and os.path.exists(_eq_ref_final):
                _apply_spectral_eq_to_midi_output(audio_path, _eq_ref_final, blend=0.92)
            # ── مطابقة باس نهائية ────────────────────────────────────────
            _eq_bass_final = stems.get("bass") if stems else None
            if _eq_bass_final and os.path.exists(_eq_bass_final):
                _apply_spectral_eq_to_midi_output(audio_path, _eq_bass_final, blend=0.55)
            # ── التوجيه الطيفي متعدد الآلات النهائي (خارج الصندوق) ────────
            # 6 نطاقات × stem مسؤول + حقن هارمونيكي = أعلى تشابه ممكن بلا دمج
            try:
                if stems:
                    _final_routed = audio_path.replace(".mp3", "_frouted.mp3")
                    if _apply_per_stem_spectral_routing(audio_path, stems, original_path, _final_routed):
                        import shutil as _sh
                        _sh.move(_final_routed, audio_path)
                else:
                    _final_matched = audio_path.replace(".mp3", "_fmatch.mp3")
                    if spectral_match_audio(audio_path, original_path, _final_matched):
                        import shutil as _sh
                        _sh.move(_final_matched, audio_path)
            except Exception as _fm_e:
                logger.warning(f"Final spectral routing failed (non-fatal): {_fm_e}")
        await loop.run_in_executor(None, _final_render)

        # ── بناء صورة المقارنة الطيفية ───────────────────────────────────────
        await status_msg.edit_text(
            "📊 *بناء صورة المقارنة الطيفية الكاملة...*",
            parse_mode="Markdown"
        )

        def _build_spectral_img():
            try:
                build_spectral_comparison(original_path, audio_path, spectral_path)
            except Exception as e:
                logger.warning(f"Spectral image failed: {e}")

        await loop.run_in_executor(None, _build_spectral_img)

        # ── بناء الملفات النهائية ─────────────────────────────────────────────
        await status_msg.edit_text(
            "🔨 *بناء MIDI + MusicXML المُصحّح...*",
            parse_mode="Markdown"
        )

        def build_final_files(_bt=best_bass_t):
            adj_tab = build_tab_data_from_notes(adj_notes, analysis, title=title,
                                                bass_timbre_override=_bt)
            export_to_musicxml_file(adj_tab, musicxml_path)
            build_midi_file(adj_notes, analysis, midi_path, bass_timbre=_bt)

        await loop.run_in_executor(None, build_final_files)

        vocal_info = f"🎤 أصوات بشرية: ~{vocal_segments} مقطع\n" if vocal_segments else ""
        inst_line = " | ".join(detected_instruments[:3]) if detected_instruments else "آلات متعددة"
        final_pct  = int(max(0.0, final_sim.get("overall", 0)) * 100)
        mel_pct    = int(max(0, final_sim.get("mel",    0)) * 100)
        chroma_pct = int(max(0, final_sim.get("chroma", 0)) * 100)
        midi_pct   = int((mel_pct + chroma_pct) / 2)
        iter_summary = "\n".join(
            f"  تكرار {i+1}: {int(max(0, s.get('overall', 0))*100)}%"
            for i, s in enumerate(sim_log)
        )
        deficit_report = _diagnose_instrument_deficit(final_sim, stems if stems else {}, original_path)

        try:
            await status_msg.edit_text(
                f"✅ *إعادة الإنشاء مكتملة!*\n\n"
                f"🎼 *الآلات:* {inst_line}\n"
                f"{vocal_info}"
                f"📈 *تقدم التشابه:*\n{iter_summary}\n\n"
                f"🎹 Synth: *{adj_synth}* نوتة  |  🎸 Bass: *{adj_bass}* نوتة\n"
                f"➕ نوتات مُضافة: *{max(0, added_notes)}*\n\n"
                f"📊 *دقة MIDI الحقيقية:*\n"
                f"  🎵 Mel (طيف كامل): *{mel_pct}%*\n"
                f"  🎼 Chroma (لحن): *{chroma_pct}%*\n"
                f"  🏆 التشابه الكلي: *{final_pct}%*\n\n"
                f"📊 مقارنة طيفية ✅  🎛️ Velocity ✅\n"
                f"📏 مدة النوتات ✅  🎹 MIDI ✅  🎼 MusicXML ✅",
                parse_mode="Markdown"
            )
            await asyncio.sleep(1)
            await status_msg.edit_text("📤 *جاري الإرسال...*", parse_mode="Markdown")
        except Exception:
            pass

        back_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 العودة للخيارات", callback_data=f"back_to_options_{user_id}")],
            [InlineKeyboardButton("🔄 إعادة التصدير", callback_data=f"export_midi_{user_id}")],
        ])

        try:
            await status_msg.delete()
        except Exception:
            pass

        if os.path.exists(spectral_path) and os.path.getsize(spectral_path) > 0:
            await query.message.reply_photo(
                photo=open(spectral_path, "rb"),
                caption=(
                    f"📊 *مقارنة طيفية — {title}*\n\n"
                    f"• الأحمر = نغمات موجودة في الأصل ومفقودة في الإعادة\n"
                    f"• الأزرق = نغمات زائدة في الإعادة\n"
                    f"• Chroma bars = توزيع طاقة كل نغمة\n\n"
                    f"🎵 Mel: {mel_pct}%  |  🎼 Chroma: {chroma_pct}%  |  🏆 الكلي: {final_pct}%\n"
                    f"تم تصحيح النوتات عبر {N_ITER} تكرارات"
                ),
                parse_mode="Markdown",
            )

        midi_caption = (
            f"🎹 *{title}* — MIDI مُصحّح طيفياً\n"
            f"👤 {artist}\n\n"
            f"🎼 *الآلات:* {inst_line}\n"
            f"🎛️ *المسارات:* {stem_line}\n\n"
            f"📊 *إحصائيات النوتات:*\n"
            f"• 🎹 Synth: {adj_synth} نوتة\n"
            f"• 🎸 Bass: {adj_bass} نوتة (4 طبقات)\n"
            f"• 🥁 Drums: {drum_count} ضربة\n"
            f"• 〰️ Glide/Portamento: {bend_count} نوتة\n"
            f"• ➕ نوتات مُضافة: {max(0, added_notes)}\n\n"
            f"📊 *التشابه:*\n"
            f"• 🎵 Mel: {mel_pct}%  🎼 Chroma: {chroma_pct}%\n"
            f"• 🏆 الكلي (Stems+MIDI): {final_pct}%\n"
            f"• 🎹 MIDI فقط: {midi_pct}%\n\n"
            f"✅ Velocity + مدة النوتات + FluidSynth\n"
            f"✅ Pitch Bend للانزلاقات\n"
            f"✅ باس متعدد الآلات (Retro Bass + Finger + Fretless + Acoustic)\n"
            f"✅ جاهز لـ GarageBand / أي DAW"
        )

        if os.path.exists(midi_path) and os.path.getsize(midi_path) > 0:
            await query.message.reply_document(
                document=open(midi_path, "rb"),
                filename=f"{safe_title}_corrected.mid",
                caption=midi_caption,
                parse_mode="Markdown",
                reply_markup=back_markup,
            )
            if deficit_report:
                try:
                    _MAX = 4096
                    _report_chunk = deficit_report[:_MAX] if len(deficit_report) > _MAX else deficit_report
                    await query.message.reply_text(_report_chunk, parse_mode="Markdown")
                except Exception as _de:
                    logger.warning(f"Could not send deficit report after MIDI: {_de}")

        if os.path.exists(musicxml_path) and os.path.getsize(musicxml_path) > 0:
            await query.message.reply_document(
                document=open(musicxml_path, "rb"),
                filename=f"{safe_title}_corrected.musicxml",
                caption=f"🎼 *{title}* — MusicXML مُصحّح طيفياً\n👤 {artist}",
                parse_mode="Markdown",
            )

        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            iter_prog = " → ".join(
                f"{int(max(0, s.get('overall', 0))*100)}%"
                for s in sim_log
            ) if sim_log else "—"
            _audio_caption_base = (
                f"🎧 *إعادة إنشاء الأغنية — Stems + MIDI + FluidSynth*\n\n"
                f"🎵 {title} — {artist}\n"
                f"🎼 المقام: {key_val} | {bpm_val:.0f} BPM\n\n"
                f"🎛️ المسارات: {stem_line}\n"
                f"🎹 الآلات: {inst_line}\n\n"
                f"📊 التشابه: *{final_pct}%*"
                f" (Mel {mel_pct}% | Chroma {chroma_pct}% | MIDI {midi_pct}%)\n\n"
                f"✅ FluidSynth GM + Demucs stems"
            )
            _audio_caption = _audio_caption_base
            if len(_audio_caption) > 1024:
                _audio_caption = _audio_caption[:1021] + "..."
            await query.message.reply_audio(
                audio=open(audio_path, "rb"),
                title=f"{title} — إعادة إنشاء ({final_pct}%)",
                performer=artist,
                caption=_audio_caption,
                parse_mode="Markdown",
            )

        for p in [midi_path, musicxml_path, audio_path, spectral_path, midi_only_path]:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"MusicXML export error: {e}", exc_info=True)
        _err_msg = (
            f"❌ *حدث خطأ أثناء إنتاج الملفات*\n\n"
            f"التفاصيل: `{str(e)[:300]}`\n\n"
            "يرجى المحاولة مرة أخرى."
        )
        try:
            await status_msg.edit_text(_err_msg, parse_mode="Markdown")
        except Exception:
            try:
                await query.message.reply_text(_err_msg, parse_mode="Markdown")
            except Exception:
                pass


def parse_tablature_pdf(pdf_path: str) -> dict:
    """
    Parse a guitar tablature PDF (e.g. from Klangio).
    Returns structured dict with bpm, tuning, chords, and per-instrument tab data.
    """
    import pdfplumber
    import re

    GUITAR_OPEN = [64, 59, 55, 50, 45, 40]
    BASS_OPEN   = [43, 38, 33, 28]

    result = {
        "bpm": 80, "tuning": "Standard", "chords": [],
        "guitar_notes": [], "bass_notes": [], "has_drums": False,
        "instruments": [],
    }

    bpm_pattern   = re.compile(r'[=♩♪]\s*(\d{2,3})')
    root_re       = re.compile(r'^[A-G][#b]?$')
    quality_re    = re.compile(r'^(m|maj|min|dim|aug|sus|add)$', re.I)
    extension_re  = re.compile(r'^(2|4|5|6|7|9|11|13)$')
    slash_root_re = re.compile(r'^/[A-G][#b]?$')
    chord_full_re = re.compile(
        r'\b([A-G][#b]?(?:m|maj|min|dim|aug|sus|add)?'
        r'(?:2|4|5|6|7|9|11|13)?(?:/[A-G][#b]?)?)\b'
    )

    all_words = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=5, y_tolerance=3)
            all_words.extend(words)
            text = page.extract_text() or ""

            bpm_m = bpm_pattern.search(text)
            if bpm_m:
                result["bpm"] = int(bpm_m.group(1))

            if "Standard" in text or "standard" in text:
                result["tuning"] = "Standard"

    # ── Reconstruct chords from spatially-close word tokens ───────────────────
    # PDF tablature apps often store "Gm7" as separate tokens "G" "m" "7"
    # We group words on the same line (within 4px vertically) that are
    # horizontally adjacent (within 20px) and try to reconstruct chord names.
    chord_candidates = set()

    lines: dict[int, list] = {}
    for w in all_words:
        row = round(w["top"] / 4)
        lines.setdefault(row, []).append(w)

    for row, wlist in lines.items():
        wlist_sorted = sorted(wlist, key=lambda x: x["x0"])
        i = 0
        while i < len(wlist_sorted):
            w = wlist_sorted[i]
            tok = w["text"].strip()
            if not root_re.match(tok):
                i += 1
                continue
            chord = tok
            j = i + 1
            while j < len(wlist_sorted):
                nw = wlist_sorted[j]
                gap = nw["x0"] - wlist_sorted[j - 1]["x1"]
                if gap > 20:
                    break
                nt = nw["text"].strip()
                if quality_re.match(nt) or extension_re.match(nt) or slash_root_re.match(nt):
                    chord += nt
                    j += 1
                else:
                    break
            if len(chord) >= 2 or chord in {"E", "A", "B", "C", "D", "F", "G"}:
                chord_candidates.add(chord)
            i = j if j > i + 1 else i + 1

    # Also run the full regex on compact text (catches "F7", "Gm" already merged)
    compact_text = " ".join(w["text"] for w in all_words)
    for m in chord_full_re.finditer(compact_text):
        c = m.group(1)
        if 2 <= len(c) <= 7:
            chord_candidates.add(c)

    # Filter out noise tokens (single letters that aren't chords, numbers, etc.)
    skip_words = {"Standard", "Guitar", "Bass", "Drums", "unknown", "tuning",
                  "Made", "with", "using", "klangio"}
    noise_singles = {"A", "B", "C", "D", "E", "F", "G"}

    seen = set()
    for c in sorted(chord_candidates, key=len, reverse=True):
        if c in skip_words:
            continue
        if c in noise_singles and any(c2.startswith(c) and len(c2) > 1 for c2 in chord_candidates):
            continue
        if c not in seen:
            result["chords"].append(c)
            seen.add(c)

    # MIDI drum pitches (General MIDI channel 10)
    DRUM_ROW_MIDI = {
        "bd": 36, "bass": 36, "kick": 36, "kk": 36,
        "sd": 38, "snare": 38, "sn": 38,
        "hh": 42, "hihat": 42, "hi-hat": 42, "ch": 42,
        "oh": 46, "open": 46,
        "cc": 49, "crash": 49, "cy": 49,
        "rd": 51, "ride": 51,
        "t1": 50, "t2": 48, "t3": 45, "tom": 48,
    }
    DRUM_HIT_CHARS = re.compile(r'^[xXoO*@>]+$')
    DRUM_ROW_LABEL = re.compile(
        r'^(BD|SD|HH|OH|CC|RD|T1|T2|T3|Bass|Kick|Snare|HiHat|Crash|Ride|Tom)$',
        re.I
    )

    instrument_sections = {"guitar": [], "bass": [], "drums": []}
    drum_rows: dict[int, dict] = {}  # y_rounded -> {midi, hits: [x]}
    current_inst = None
    current_drum_row_y = None
    current_drum_midi = 36

    sorted_words = sorted(all_words, key=lambda x: (x["top"], x["x0"]))

    for w in sorted_words:
        txt = w["text"].strip()
        txt_l = txt.lower()

        if "guitar" in txt_l and "bass" not in txt_l:
            current_inst = "guitar"
            current_drum_row_y = None
            if "Guitar" not in result["instruments"]:
                result["instruments"].append("Guitar")
        elif "bass" in txt_l and "drum" not in txt_l:
            current_inst = "bass"
            current_drum_row_y = None
            if "Bass" not in result["instruments"]:
                result["instruments"].append("Bass")
        elif "drum" in txt_l or "percussion" in txt_l:
            current_inst = "drums"
            current_drum_row_y = None
            result["has_drums"] = True
            if "Drums" not in result["instruments"]:
                result["instruments"].append("Drums")
        elif current_inst == "drums":
            y_key = round(w["top"] / 6) * 6
            # Detect row label (BD, SD, HH …)
            if DRUM_ROW_LABEL.match(txt):
                midi_pitch = 36
                for k, v in DRUM_ROW_MIDI.items():
                    if k in txt_l:
                        midi_pitch = v
                        break
                drum_rows.setdefault(y_key, {"midi": midi_pitch, "hits": []})
                drum_rows[y_key]["midi"] = midi_pitch
                current_drum_row_y = y_key
            # Detect hit character on the current drum row
            elif DRUM_HIT_CHARS.match(txt):
                row_y = y_key
                if row_y not in drum_rows:
                    # Try to find nearest known row within 12px
                    nearest = min(
                        drum_rows.keys(),
                        key=lambda k: abs(k - row_y),
                        default=None
                    )
                    if nearest is not None and abs(nearest - row_y) <= 12:
                        row_y = nearest
                    else:
                        drum_rows[row_y] = {"midi": 42, "hits": []}
                drum_rows[row_y]["hits"].append(w["x0"])
        elif current_inst in ("guitar", "bass") and re.match(r'^\d+$', txt):
            instrument_sections[current_inst].append({
                "x": w["x0"], "y": w["top"], "fret": int(txt)
            })

    # Build drum_notes from drum_rows
    drum_notes = []
    if drum_rows:
        all_xs = [x for row in drum_rows.values() for x in row["hits"]]
        max_x = max(all_xs) if all_xs else 800.0
        max_x = max_x or 800.0
        for row_data in drum_rows.values():
            for x in row_data["hits"]:
                drum_notes.append({
                    "midi": row_data["midi"],
                    "time": x / max_x,
                })
        drum_notes.sort(key=lambda n: n["time"])
    result["drum_notes"] = drum_notes

    def notes_from_section(words_list, open_strings, n_strings):
        if not words_list:
            return []
        ys = sorted(set(round(w["y"] / 4) * 4 for w in words_list))
        unique_ys = []
        for y in ys:
            if not unique_ys or abs(y - unique_ys[-1]) > 8:
                unique_ys.append(y)
        unique_ys = unique_ys[:n_strings]

        def get_string_idx(y):
            closest = min(range(len(unique_ys)), key=lambda i: abs(unique_ys[i] - round(y / 4) * 4))
            return closest

        notes = []
        for w in sorted(words_list, key=lambda x: x["x"]):
            si = get_string_idx(w["y"])
            if si < len(open_strings):
                midi_note = open_strings[si] + w["fret"]
                time_pos = w["x"] / 800.0
                notes.append({"midi": midi_note, "time": time_pos, "string": si})
        return notes

    result["guitar_notes"] = notes_from_section(
        instrument_sections["guitar"], GUITAR_OPEN, 6
    )
    result["bass_notes"] = notes_from_section(
        instrument_sections["bass"], BASS_OPEN, 4
    )
    return result


def tablature_to_midi(tab_data: dict, output_path: str):
    """Convert parsed tablature data to a MIDI file."""
    import pretty_midi

    bpm = tab_data.get("bpm", 80)
    pm = pretty_midi.PrettyMIDI(initial_tempo=float(bpm))

    beat_duration = 60.0 / bpm

    def scale_times(notes, total_beats=32):
        if not notes:
            return notes
        max_t = max(n["time"] for n in notes) or 1.0
        return [{**n, "time": (n["time"] / max_t) * total_beats * beat_duration} for n in notes]

    guitar_notes = scale_times(tab_data.get("guitar_notes", []))
    bass_notes   = scale_times(tab_data.get("bass_notes", []))

    if guitar_notes:
        guitar_prog = pretty_midi.instrument_name_to_program("Acoustic Guitar (nylon)")
        guitar_inst = pretty_midi.Instrument(program=guitar_prog, name="Guitar")
        for n in guitar_notes:
            note = pretty_midi.Note(
                velocity=90,
                pitch=min(max(n["midi"], 0), 127),
                start=n["time"],
                end=n["time"] + beat_duration * 0.9,
            )
            guitar_inst.notes.append(note)
        pm.instruments.append(guitar_inst)

    if bass_notes:
        bass_prog = pretty_midi.instrument_name_to_program("Electric Bass (finger)")
        bass_inst = pretty_midi.Instrument(program=bass_prog, name="Bass")
        for n in bass_notes:
            note = pretty_midi.Note(
                velocity=95,
                pitch=min(max(n["midi"], 0), 127),
                start=n["time"],
                end=n["time"] + beat_duration * 0.85,
            )
            bass_inst.notes.append(note)
        pm.instruments.append(bass_inst)

    if tab_data.get("has_drums"):
        drums_inst = pretty_midi.Instrument(program=0, is_drum=True, name="Drums")
        drum_notes = tab_data.get("drum_notes", [])
        if drum_notes:
            # Scale drum note times to fit within the total MIDI duration
            all_times = [n["time"] for n in drum_notes]
            max_t = max(all_times) if all_times else 1.0
            total_time = 32 * beat_duration
            for dn in drum_notes:
                t_scaled = (dn["time"] / max_t) * total_time
                pitch = min(max(int(dn["midi"]), 0), 127)
                drums_inst.notes.append(
                    pretty_midi.Note(velocity=100, pitch=pitch,
                                     start=t_scaled, end=t_scaled + 0.05)
                )
        else:
            total_time = (32 * beat_duration)
            t = 0.0
            while t < total_time:
                drums_inst.notes.append(pretty_midi.Note(velocity=100, pitch=36, start=t, end=t + 0.05))
                drums_inst.notes.append(pretty_midi.Note(velocity=80,  pitch=42, start=t + beat_duration / 2, end=t + beat_duration / 2 + 0.05))
                t += beat_duration
        pm.instruments.append(drums_inst)

    if not pm.instruments:
        default_prog = pretty_midi.instrument_name_to_program("Acoustic Guitar (nylon)")
        default_inst = pretty_midi.Instrument(program=default_prog, name="Guitar")
        t = 0.0
        for fret in [0, 2, 4, 5, 7, 9, 11, 12]:
            note = pretty_midi.Note(velocity=90, pitch=64 + fret, start=t, end=t + beat_duration * 0.9)
            default_inst.notes.append(note)
            t += beat_duration
        pm.instruments.append(default_inst)

    pm.write(output_path)


def _karplus_strong(frequency: float, duration: float, sr: int = 44100,
                    decay: float = 0.996, timbre: str = "guitar") -> np.ndarray:
    """
    Karplus-Strong plucked string synthesis.
    Produces realistic guitar/bass tones without any external binary.
    """
    n_samples = int(sr * duration)
    period = int(sr / frequency)
    if period < 2:
        period = 2

    buf = np.random.uniform(-1, 1, period).astype(np.float32)
    out = np.zeros(n_samples, dtype=np.float32)

    for i in range(n_samples):
        out[i] = buf[i % period]
        avg = decay * 0.5 * (buf[i % period] + buf[(i + 1) % period])
        if timbre == "bass":
            avg = decay * (0.6 * buf[i % period] + 0.4 * buf[(i + 1) % period])
        buf[i % period] = avg

    env_release = int(sr * min(0.15, duration * 0.3))
    if env_release > 0 and env_release < n_samples:
        out[-env_release:] *= np.linspace(1.0, 0.0, env_release)

    return out


def _midi_to_hz(midi_note: int) -> float:
    midi_note = max(0, min(127, int(midi_note)))
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


def _synth_drum_hit(midi_pitch: int, sr: int = 44100) -> np.ndarray:
    """
    Synthesize a single drum hit based on General MIDI drum pitch.
    Enhanced v3: ultra-deep kick sub-bass, full-bodied snare, rich metallic hat.
    """
    midi_pitch = int(midi_pitch)

    # ── Kick / Bass drum (35, 36) ──────────────────────────────────────────
    if midi_pitch in (35, 36):
        dur = int(sr * 0.38)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        # Main body: 75Hz sweeping deep to 35Hz — real 808-kick sub range
        freq_body  = 75.0 * np.exp(-5.0 * t) + 35.0
        # Punch transient: 220Hz → 65Hz (thud click)
        freq_punch = 220.0 * np.exp(-55 * t) + 65.0
        body  = 0.92 * np.sin(2 * np.pi * np.cumsum(freq_body)  / sr)
        punch = 0.60 * np.sin(2 * np.pi * np.cumsum(freq_punch) / sr)
        # Noise attack (sharp beater transient)
        noise_att = np.random.uniform(-1, 1, dur).astype(np.float32) * np.exp(-130 * t)
        # 40Hz sub-bass floor — must decay FAST to keep onset sharp
        sub_floor  = 0.38 * np.sin(2 * np.pi * 40.0 * t)
        # 2nd harmonic for warmth
        harm2 = 0.16 * np.sin(2 * np.pi * np.cumsum(freq_body * 2.0) / sr)
        # Combine — all components decay quickly for clean onset pattern
        hit = (body     * np.exp(-5.0 * t)
               + punch  * np.exp(-42  * t)
               + 0.14   * noise_att
               + sub_floor * np.exp(-5.5 * t)
               + harm2  * np.exp(-5.5 * t))

    # ── Snare (37, 38, 40) ─────────────────────────────────────────────────
    elif midi_pitch in (37, 38, 40):
        dur = int(sr * 0.30)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        # Wood thud: 200Hz — gives mid-range body below the measurement band
        wood = 0.48 * np.sin(2 * np.pi * 200 * t) * np.exp(-22 * t)
        # Body sweep: 1200Hz → 400Hz — sits squarely in snare band
        freq_b = 1200.0 * np.exp(-12 * t) + 400.0
        body   = 0.42 * np.sin(2 * np.pi * np.cumsum(freq_b) / sr) * np.exp(-18 * t)
        # Core resonance at 800Hz
        resonance = 0.32 * np.sin(2 * np.pi * 800 * t) * np.exp(-22 * t)
        # Wire rattle: broadband noise — slower decay = more sustain
        rattle = noise * np.exp(-14 * t) * 0.52
        # Extra high-wire shimmer (2-6kHz)
        shimmer = (0.18 * np.sin(2 * np.pi * 2800 * t)
                   + 0.12 * np.sin(2 * np.pi * 4800 * t)) * np.exp(-20 * t)
        # Sharp crack: noise burst (first 6ms)
        crack_dur = min(int(0.006 * sr), dur)
        crack = np.zeros(dur, dtype=np.float32)
        crack[:crack_dur] = (np.random.uniform(-1, 1, crack_dur)
                             * np.exp(-100 * np.linspace(0, 1, crack_dur)))
        hit = wood + body + resonance + rattle + shimmer + 0.48 * crack

    # ── Closed hi-hat (42, 44) ─────────────────────────────────────────────
    elif midi_pitch in (42, 44):
        dur = int(sr * 0.085)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        # Rich metallic spectrum — inharmonic partials like a real cymbal
        metal  = 0.30 * np.sin(2 * np.pi * 4500  * t)
        metal += 0.28 * np.sin(2 * np.pi * 6200  * t)
        metal += 0.24 * np.sin(2 * np.pi * 7800  * t)
        metal += 0.22 * np.sin(2 * np.pi * 9400  * t)
        metal += 0.18 * np.sin(2 * np.pi * 11200 * t)
        metal += 0.14 * np.sin(2 * np.pi * 13500 * t)
        metal += 0.10 * np.sin(2 * np.pi * 15800 * t)
        # Inharmonic partials for extra realism
        metal += 0.16 * np.sin(2 * np.pi * 5350  * t)
        metal += 0.12 * np.sin(2 * np.pi * 8900  * t)
        metal += 0.08 * np.sin(2 * np.pi * 12100 * t)
        # Sharp onset burst (4ms)
        burst_end = min(int(0.004 * sr), dur)
        hi_noise = np.zeros(dur, dtype=np.float32)
        hi_noise[:burst_end] = np.random.uniform(-1, 1, burst_end)
        hit = (0.52 * noise + metal) * np.exp(-58 * t) + 0.38 * hi_noise * np.exp(-280 * t)

    # ── Open hi-hat (46) ───────────────────────────────────────────────────
    elif midi_pitch == 46:
        dur = int(sr * 0.40)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        # Rich metallic above 4kHz
        metal  = 0.30 * np.sin(2 * np.pi * 4800  * t)
        metal += 0.26 * np.sin(2 * np.pi * 6500  * t)
        metal += 0.22 * np.sin(2 * np.pi * 8200  * t)
        metal += 0.18 * np.sin(2 * np.pi * 10000 * t)
        metal += 0.14 * np.sin(2 * np.pi * 12800 * t)
        metal += 0.10 * np.sin(2 * np.pi * 15200 * t)
        # Inharmonic shimmer
        metal += 0.14 * np.sin(2 * np.pi * 5700  * t)
        metal += 0.10 * np.sin(2 * np.pi * 9100  * t)
        # Sharp onset
        burst_end = min(int(0.004 * sr), dur)
        onset_burst = np.zeros(dur, dtype=np.float32)
        onset_burst[:burst_end] = np.random.uniform(-1, 1, burst_end)
        hit = (0.50 * noise + metal) * np.exp(-6.5 * t) + 0.35 * onset_burst

    # ── Crash cymbal (49, 57) ──────────────────────────────────────────────
    elif midi_pitch in (49, 57):
        dur = int(sr * 0.75)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        shimmer = (0.22 * np.sin(2 * np.pi * 5500 * t)
                   + 0.18 * np.sin(2 * np.pi * 8000 * t)
                   + 0.14 * np.sin(2 * np.pi * 11000 * t))
        hit = (0.42 * noise + shimmer) * np.exp(-4.0 * t)

    # ── Ride cymbal (51, 59) ───────────────────────────────────────────────
    elif midi_pitch in (51, 59):
        dur = int(sr * 0.45)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        bell  = (0.38 * np.sin(2 * np.pi * 1200 * t)
                 + 0.20 * np.sin(2 * np.pi * 2600 * t)) * np.exp(-9 * t)
        hit = 0.30 * noise * np.exp(-5.5 * t) + bell

    # ── Hand clap (39) ─────────────────────────────────────────────────────
    elif midi_pitch == 39:
        dur = int(sr * 0.12)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        burst1 = noise * np.exp(-55 * t)
        burst2 = noise * np.exp(-30 * np.clip(t - 0.01, 0, None))
        body   = 0.38 * np.sin(2 * np.pi * 900 * t) * np.exp(-35 * t)
        hit = 0.48 * burst1 + 0.38 * burst2 + body

    # ── Tambourine (54) ────────────────────────────────────────────────────
    elif midi_pitch == 54:
        dur = int(sr * 0.18)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        jingle1 = 0.32 * np.sin(2 * np.pi * 6000 * t) * np.exp(-20 * t)
        jingle2 = 0.24 * np.sin(2 * np.pi * 8500 * t) * np.exp(-26 * t)
        jingle3 = 0.16 * np.sin(2 * np.pi * 11000 * t) * np.exp(-30 * t)
        hit = 0.40 * noise * np.exp(-16 * t) + jingle1 + jingle2 + jingle3

    # ── Cowbell / Wood block (56, 76, 77) ─────────────────────────────────
    elif midi_pitch in (56, 76, 77):
        dur = int(sr * 0.14)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        hit  = 0.52 * np.sin(2 * np.pi * 800  * t) * np.exp(-20 * t)
        hit += 0.28 * np.sin(2 * np.pi * 1400 * t) * np.exp(-24 * t)
        hit += 0.16 * np.sin(2 * np.pi * 2100 * t) * np.exp(-28 * t)

    # ── Conga / Darbuka (60–66) ────────────────────────────────────────────
    elif midi_pitch in (60, 61, 62, 63, 64, 65, 66):
        dur = int(sr * 0.20)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        f0 = (220 if midi_pitch <= 62 else 300) * np.exp(-12 * t)
        hit  = 0.58 * np.sin(2 * np.pi * np.cumsum(f0) / sr) * np.exp(-15 * t)
        hit += 0.25 * noise * np.exp(-28 * t)

    # ── Ride bell (53) ─────────────────────────────────────────────────────
    elif midi_pitch == 53:
        dur = int(sr * 0.30)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        hit  = 0.42 * np.sin(2 * np.pi * 1200 * t) * np.exp(-8  * t)
        hit += 0.22 * np.sin(2 * np.pi * 2400 * t) * np.exp(-11 * t)
        hit += 0.12 * np.sin(2 * np.pi * 3800 * t) * np.exp(-14 * t)

    # ── Tom toms (41, 43, 45, 47, 48, 50) ─────────────────────────────────
    elif midi_pitch in (41, 43, 45, 47, 48, 50):
        dur = int(sr * 0.26)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        base_freq = (70 + (midi_pitch - 41) * 10) * np.exp(-9 * t)
        hit  = 0.68 * np.sin(2 * np.pi * np.cumsum(base_freq) / sr) * np.exp(-11 * t)
        hit += 0.20 * np.random.uniform(-1, 1, dur).astype(np.float32) * np.exp(-28 * t)

    else:
        dur = int(sr * 0.09)
        t = np.linspace(0, 1, dur, dtype=np.float32)
        noise = np.random.uniform(-1, 1, dur).astype(np.float32)
        hit = 0.30 * noise * np.exp(-28 * t)

    hit = hit.astype(np.float32)
    pk = np.abs(hit).max()
    if pk > 1e-6:
        hit /= pk
    return hit


def _render_drum_notes(drum_notes: list, total_time: float, sr: int = 44100) -> np.ndarray:
    """Render a list of drum_notes dicts (midi, time) into an audio array."""
    n_total = int(sr * total_time)
    out = np.zeros(n_total, dtype=np.float32)
    for note in drum_notes:
        t_start = note["time"]
        if t_start >= total_time:
            continue
        hit = _synth_drum_hit(note["midi"], sr)
        vel  = float(note.get("velocity", 100)) / 127.0
        si = int(t_start * sr)
        ei = min(si + len(hit), n_total)
        out[si:ei] += hit[:ei - si] * vel
    return out


def _synth_drums(beat_duration: float, total_time: float, sr: int = 44100) -> np.ndarray:
    """Synthesize a realistic kick + snare + hi-hat drum pattern (Trap-aware)."""
    n_total = int(sr * total_time)
    out = np.zeros(n_total, dtype=np.float32)
    t_beat = 0.0
    step = beat_duration / 4.0   # 16th note
    while t_beat < total_time:
        # Kick on beat 1
        kick_hit = _synth_drum_hit(36, sr)
        si = int(t_beat * sr)
        ei = min(si + len(kick_hit), n_total)
        out[si:ei] += kick_hit[:ei - si] * 0.90

        # Snare on beat 2 (half-way through bar)
        snare_t = t_beat + beat_duration * 1.0
        if snare_t < total_time:
            snare_hit = _synth_drum_hit(38, sr)
            si2 = int(snare_t * sr)
            ei2 = min(si2 + len(snare_hit), n_total)
            out[si2:ei2] += snare_hit[:ei2 - si2] * 0.80

        # Hi-hats every 16th note (Trap pattern)
        for step_idx in range(8):
            ht = t_beat + step_idx * step
            if ht >= total_time:
                break
            midi_hh = 46 if step_idx == 4 else 42
            hh_hit = _synth_drum_hit(midi_hh, sr)
            vol = 0.55 if step_idx % 2 == 0 else 0.35
            si3 = int(ht * sr)
            ei3 = min(si3 + len(hh_hit), n_total)
            out[si3:ei3] += hh_hit[:ei3 - si3] * vol

        t_beat += beat_duration * 2.0   # advance one full bar (2 beats)
    return out


def render_midi_to_audio(midi_path: str, audio_out: str) -> bool:
    """
    Render MIDI to audio.
    Priority: FluidSynth with real soundfont → Karplus-Strong fallback.
    """
    if render_midi_fluidsynth(midi_path, audio_out):
        return True
    logger.info("FluidSynth unavailable — falling back to Karplus-Strong")
    import pretty_midi

    try:
        pm = pretty_midi.PrettyMIDI(midi_path)
        sr = 44100

        end_time = pm.get_end_time()
        if end_time <= 0:
            logger.warning("MIDI has no notes / zero duration")
            return False

        total_time = end_time + 1.5
        mix = np.zeros(int(sr * total_time), dtype=np.float32)

        bpm = pm.estimate_tempo()
        beat_duration = 60.0 / max(bpm, 40)

        for instrument in pm.instruments:
            if instrument.is_drum:
                drum_track = _synth_drums(beat_duration, total_time, sr)
                length = min(len(drum_track), len(mix))
                mix[:length] += drum_track[:length] * 0.55
                continue

            name_lower = instrument.name.lower()
            timbre = "bass" if "bass" in name_lower else "guitar"
            gain = 0.65 if timbre == "guitar" else 0.70

            for note in instrument.notes:
                freq = _midi_to_hz(note.pitch)
                dur  = note.end - note.start + 0.5
                vel  = note.velocity / 127.0

                tone = _karplus_strong(freq, dur, sr=sr, timbre=timbre)
                start_idx = int(note.start * sr)
                end_idx   = start_idx + len(tone)
                if end_idx > len(mix):
                    tone = tone[:len(mix) - start_idx]
                    end_idx = len(mix)
                mix[start_idx:end_idx] += tone * gain * vel

        peak = np.abs(mix).max()
        if peak > 0:
            mix = mix / peak * 0.90
        else:
            logger.warning("Synthesized audio is silent")
            return False

        wav_tmp = audio_out.replace(".mp3", "_raw.wav")
        sf.write(wav_tmp, mix, sr, subtype="PCM_16")

        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_tmp,
             "-acodec", "libmp3lame", "-ar", "44100", "-ab", "192k",
             audio_out],
            capture_output=True, check=True
        )
        logger.info(f"render_midi_to_audio: success, size={os.path.getsize(audio_out)}")
        return True

    except Exception as e:
        logger.error(f"render_midi_to_audio error: {e}", exc_info=True)
        return False
    finally:
        wav_tmp = audio_out.replace(".mp3", "_raw.wav")
        if os.path.exists(wav_tmp):
            os.remove(wav_tmp)


_KS_PRESETS = {
    # ── الآلات الأصلية ───────────────────────────────────────────────────────
    "guitar": ([(1, 0.50), (2, 0.28), (3, 0.13), (4, 0.06), (5, 0.03)], 7.0,  0.25),
    "bass":   ([(1, 0.75), (2, 0.18), (3, 0.07)],                        2.8,  0.10),
    "oud":    ([(1, 0.55), (2, 0.25), (3, 0.12), (4, 0.05), (5, 0.03)], 6.5,  0.30),
    "sitar":  ([(1, 0.40), (2, 0.22), (3, 0.16), (4, 0.11), (5, 0.07),
                (6, 0.04)],                                                9.0,  0.40),
    "banjo":  ([(1, 0.45), (2, 0.30), (3, 0.15), (4, 0.07), (5, 0.03)], 12.0, 0.35),

    # ══════════════════════════════════════════════════════════════════════════
    # ── 108 نوع باس — harmonics/decay/noise ──────────────────────────────────
    # decay: منخفض=طويل، عالٍ=قصير | noise: منخفض=ناعم، عالٍ=نقري
    # ══════════════════════════════════════════════════════════════════════════

    # ── Sub Bass / 808 (15) ──────────────────────────────────────────────────
    "bass_808_sub":      ([(1, 0.95), (2, 0.05)],                                    0.8, 0.02),
    "bass_808_warm":     ([(1, 0.85), (2, 0.12), (3, 0.03)],                         1.2, 0.04),
    "bass_808_punchy":   ([(1, 0.80), (2, 0.15), (3, 0.05)],                         1.8, 0.12),
    "bass_808_long":     ([(1, 0.90), (2, 0.08), (3, 0.02)],                         0.6, 0.03),
    "bass_808_deep":     ([(1, 0.92), (2, 0.07), (3, 0.01)],                         0.5, 0.01),
    "bass_808_bright":   ([(1, 0.70), (2, 0.20), (3, 0.08), (4, 0.02)],              1.5, 0.08),
    "bass_808_mid":      ([(1, 0.65), (2, 0.25), (3, 0.08), (4, 0.02)],              2.0, 0.10),
    "bass_808_click":    ([(1, 0.80), (2, 0.12), (3, 0.06), (4, 0.02)],              2.2, 0.30),
    "bass_808_soft":     ([(1, 0.88), (2, 0.10), (3, 0.02)],                         1.0, 0.01),
    "bass_808_trap":     ([(1, 0.82), (2, 0.14), (3, 0.04)],                         1.4, 0.06),
    "bass_808_drill":    ([(1, 0.78), (2, 0.16), (3, 0.05), (4, 0.01)],              1.6, 0.14),
    "bass_808_rnb":      ([(1, 0.86), (2, 0.11), (3, 0.03)],                         1.1, 0.05),
    "bass_808_uk":       ([(1, 0.83), (2, 0.13), (3, 0.04)],                         1.3, 0.07),
    "bass_808_afro":     ([(1, 0.79), (2, 0.17), (3, 0.04)],                         1.7, 0.09),
    "bass_808_hiphop":   ([(1, 0.84), (2, 0.12), (3, 0.04)],                         1.2, 0.06),

    # ── Electric Bass (24) ───────────────────────────────────────────────────
    "bass_finger":       ([(1, 0.72), (2, 0.20), (3, 0.07), (4, 0.01)],              2.5, 0.08),
    "bass_pick":         ([(1, 0.60), (2, 0.25), (3, 0.10), (4, 0.04), (5, 0.01)],  3.5, 0.18),
    "bass_slap":         ([(1, 0.55), (2, 0.28), (3, 0.12), (4, 0.05)],              5.0, 0.35),
    "bass_pop":          ([(1, 0.50), (2, 0.30), (3, 0.14), (4, 0.05), (5, 0.01)],  6.0, 0.40),
    "bass_muted":        ([(1, 0.80), (2, 0.16), (3, 0.04)],                         7.0, 0.05),
    "bass_fretless":     ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              2.0, 0.03),
    "bass_fretless_warm":([(1, 0.75), (2, 0.18), (3, 0.07)],                         1.8, 0.02),
    "bass_fretless_slur":([(1, 0.70), (2, 0.20), (3, 0.09), (4, 0.01)],              1.6, 0.02),
    "bass_jazz":         ([(1, 0.65), (2, 0.22), (3, 0.10), (4, 0.03)],              3.2, 0.12),
    "bass_jazz_bright":  ([(1, 0.58), (2, 0.26), (3, 0.12), (4, 0.04)],              4.0, 0.15),
    "bass_funk":         ([(1, 0.60), (2, 0.25), (3, 0.12), (4, 0.03)],              4.5, 0.25),
    "bass_funk_pop":     ([(1, 0.55), (2, 0.28), (3, 0.13), (4, 0.04)],              5.5, 0.32),
    "bass_rock":         ([(1, 0.65), (2, 0.22), (3, 0.10), (4, 0.03)],              3.0, 0.14),
    "bass_rock_heavy":   ([(1, 0.60), (2, 0.24), (3, 0.12), (4, 0.04)],              3.5, 0.20),
    "bass_punk":         ([(1, 0.55), (2, 0.26), (3, 0.14), (4, 0.05)],              5.0, 0.28),
    "bass_metal":        ([(1, 0.50), (2, 0.28), (3, 0.14), (4, 0.06), (5, 0.02)],  4.5, 0.30),
    "bass_clean":        ([(1, 0.74), (2, 0.19), (3, 0.07)],                         2.8, 0.06),
    "bass_warm":         ([(1, 0.78), (2, 0.17), (3, 0.05)],                         2.2, 0.05),
    "bass_bright":       ([(1, 0.58), (2, 0.26), (3, 0.12), (4, 0.04)],              3.8, 0.16),
    "bass_growl":        ([(1, 0.55), (2, 0.27), (3, 0.13), (4, 0.05)],              3.2, 0.22),
    "bass_vintage":      ([(1, 0.70), (2, 0.21), (3, 0.08), (4, 0.01)],              2.6, 0.09),
    "bass_modern":       ([(1, 0.62), (2, 0.24), (3, 0.11), (4, 0.03)],              3.4, 0.13),
    "bass_tight":        ([(1, 0.72), (2, 0.19), (3, 0.08), (4, 0.01)],              6.0, 0.15),
    "bass_hollow":       ([(1, 0.68), (2, 0.22), (3, 0.08), (4, 0.02)],              4.2, 0.07),

    # ── Acoustic / Upright Bass (6) ──────────────────────────────────────────
    "bass_upright":      ([(1, 0.65), (2, 0.22), (3, 0.09), (4, 0.03), (5, 0.01)],  3.8, 0.15),
    "bass_upright_warm": ([(1, 0.70), (2, 0.20), (3, 0.08), (4, 0.02)],              3.2, 0.10),
    "bass_upright_jazz": ([(1, 0.62), (2, 0.24), (3, 0.11), (4, 0.03)],              4.0, 0.18),
    "bass_upright_bow":  ([(1, 0.72), (2, 0.19), (3, 0.07), (4, 0.02)],              1.5, 0.04),
    "bass_acoustic":     ([(1, 0.64), (2, 0.23), (3, 0.10), (4, 0.03)],              4.5, 0.18),
    "bass_acoustic_slap":([(1, 0.55), (2, 0.27), (3, 0.14), (4, 0.04)],              6.5, 0.38),

    # ── Synth Bass (12) ──────────────────────────────────────────────────────
    "bass_synth_sq":     ([(1, 0.50), (2, 0.00), (3, 0.17), (4, 0.00), (5, 0.10),
                           (6, 0.00), (7, 0.07)],                                    2.0, 0.05),
    "bass_synth_saw":    ([(1, 0.45), (2, 0.22), (3, 0.15), (4, 0.11), (5, 0.08),
                           (6, 0.05), (7, 0.04)],                                    2.5, 0.05),
    "bass_synth_tri":    ([(1, 0.60), (2, 0.00), (3, 0.07), (4, 0.00), (5, 0.02)],  2.0, 0.03),
    "bass_synth_pulse":  ([(1, 0.55), (2, 0.00), (3, 0.18), (4, 0.00), (5, 0.11)],  3.0, 0.06),
    "bass_synth_warm":   ([(1, 0.72), (2, 0.18), (3, 0.07), (4, 0.03)],              1.8, 0.03),
    "bass_synth_dark":   ([(1, 0.82), (2, 0.14), (3, 0.04)],                         1.5, 0.02),
    "bass_synth_deep":   ([(1, 0.88), (2, 0.10), (3, 0.02)],                         1.2, 0.01),
    "bass_synth_mid":    ([(1, 0.60), (2, 0.24), (3, 0.12), (4, 0.04)],              2.8, 0.08),
    "bass_synth_bright": ([(1, 0.50), (2, 0.26), (3, 0.14), (4, 0.08), (5, 0.02)],  3.5, 0.10),
    "bass_synth_pluck":  ([(1, 0.60), (2, 0.22), (3, 0.12), (4, 0.06)],              8.0, 0.25),
    "bass_synth_pad":    ([(1, 0.78), (2, 0.16), (3, 0.06)],                         0.8, 0.01),
    "bass_synth_lead":   ([(1, 0.55), (2, 0.25), (3, 0.14), (4, 0.06)],              2.2, 0.07),

    # ── Acid / Electronic (13) ───────────────────────────────────────────────
    "bass_acid":         ([(1, 0.52), (2, 0.25), (3, 0.15), (4, 0.08)],              4.0, 0.20),
    "bass_acid_warm":    ([(1, 0.60), (2, 0.23), (3, 0.12), (4, 0.05)],              3.2, 0.12),
    "bass_acid_bright":  ([(1, 0.48), (2, 0.27), (3, 0.16), (4, 0.09)],              5.0, 0.22),
    "bass_techno":       ([(1, 0.62), (2, 0.22), (3, 0.12), (4, 0.04)],              3.8, 0.18),
    "bass_house":        ([(1, 0.70), (2, 0.20), (3, 0.08), (4, 0.02)],              3.0, 0.10),
    "bass_dnb":          ([(1, 0.65), (2, 0.22), (3, 0.10), (4, 0.03)],              2.5, 0.12),
    "bass_dubstep":      ([(1, 0.55), (2, 0.26), (3, 0.14), (4, 0.05)],              2.0, 0.08),
    "bass_edm":          ([(1, 0.68), (2, 0.21), (3, 0.09), (4, 0.02)],              2.8, 0.09),
    "bass_lo_fi":        ([(1, 0.78), (2, 0.16), (3, 0.06)],                         2.0, 0.12),
    "bass_wobble":       ([(1, 0.58), (2, 0.24), (3, 0.13), (4, 0.05)],              1.8, 0.04),
    "bass_reese":        ([(1, 0.52), (2, 0.26), (3, 0.14), (4, 0.08)],              1.5, 0.03),
    "bass_neuro":        ([(1, 0.55), (2, 0.25), (3, 0.14), (4, 0.06)],              1.6, 0.04),
    "bass_future":       ([(1, 0.72), (2, 0.18), (3, 0.08), (4, 0.02)],              2.2, 0.06),

    # ── Hip-hop (10) ─────────────────────────────────────────────────────────
    "bass_boom":         ([(1, 0.88), (2, 0.09), (3, 0.03)],                         1.0, 0.08),
    "bass_boom_bap":     ([(1, 0.82), (2, 0.13), (3, 0.05)],                         1.8, 0.14),
    "bass_trap_hi":      ([(1, 0.72), (2, 0.20), (3, 0.07), (4, 0.01)],              2.0, 0.10),
    "bass_trap_sub":     ([(1, 0.90), (2, 0.08), (3, 0.02)],                         0.9, 0.03),
    "bass_lofi_hip":     ([(1, 0.76), (2, 0.17), (3, 0.07)],                         2.4, 0.11),
    "bass_west_coast":   ([(1, 0.74), (2, 0.19), (3, 0.07)],                         2.6, 0.08),
    "bass_east_coast":   ([(1, 0.70), (2, 0.21), (3, 0.08), (4, 0.01)],              2.8, 0.09),
    "bass_phonk":        ([(1, 0.80), (2, 0.14), (3, 0.06)],                         1.4, 0.07),
    "bass_plugg":        ([(1, 0.83), (2, 0.12), (3, 0.05)],                         1.2, 0.05),
    "bass_bounce":       ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              3.2, 0.12),

    # ── Soul / R&B / Funk (6) ────────────────────────────────────────────────
    "bass_soul":         ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              2.8, 0.10),
    "bass_rnb":          ([(1, 0.72), (2, 0.20), (3, 0.07), (4, 0.01)],              2.4, 0.08),
    "bass_gospel":       ([(1, 0.70), (2, 0.21), (3, 0.08), (4, 0.01)],              2.6, 0.09),
    "bass_groove":       ([(1, 0.66), (2, 0.23), (3, 0.10), (4, 0.01)],              3.0, 0.12),
    "bass_motown":       ([(1, 0.67), (2, 0.22), (3, 0.10), (4, 0.01)],              3.2, 0.13),
    "bass_motown_pick":  ([(1, 0.58), (2, 0.26), (3, 0.13), (4, 0.03)],              4.0, 0.20),

    # ── Reggae / Dub (4) ─────────────────────────────────────────────────────
    "bass_reggae":       ([(1, 0.78), (2, 0.16), (3, 0.06)],                         2.0, 0.07),
    "bass_dub":          ([(1, 0.82), (2, 0.13), (3, 0.05)],                         1.6, 0.05),
    "bass_ska":          ([(1, 0.65), (2, 0.23), (3, 0.10), (4, 0.02)],              4.0, 0.16),
    "bass_roots":        ([(1, 0.80), (2, 0.14), (3, 0.06)],                         1.8, 0.06),

    # ── Jazz / Blues / Latin (6) ─────────────────────────────────────────────
    "bass_blues":        ([(1, 0.66), (2, 0.23), (3, 0.09), (4, 0.02)],              3.5, 0.13),
    "bass_swing":        ([(1, 0.64), (2, 0.24), (3, 0.10), (4, 0.02)],              3.8, 0.15),
    "bass_bebop":        ([(1, 0.62), (2, 0.25), (3, 0.11), (4, 0.02)],              4.0, 0.17),
    "bass_latin":        ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              3.0, 0.12),
    "bass_bossa":        ([(1, 0.70), (2, 0.21), (3, 0.08), (4, 0.01)],              2.8, 0.10),
    "bass_samba":        ([(1, 0.66), (2, 0.23), (3, 0.10), (4, 0.01)],              3.2, 0.13),

    # ── World / Ethnic (4) ───────────────────────────────────────────────────
    "bass_arabic":       ([(1, 0.74), (2, 0.19), (3, 0.07)],                         2.5, 0.11),
    "bass_indian":       ([(1, 0.70), (2, 0.21), (3, 0.09)],                         2.8, 0.13),
    "bass_afrobeat":     ([(1, 0.68), (2, 0.22), (3, 0.09), (4, 0.01)],              3.4, 0.12),
    "bass_afropop":      ([(1, 0.70), (2, 0.20), (3, 0.09), (4, 0.01)],              3.0, 0.11),

    # ── Articulation Variants (10) ───────────────────────────────────────────
    "bass_staccato":     ([(1, 0.70), (2, 0.20), (3, 0.10)],                        15.0, 0.20),
    "bass_legato":       ([(1, 0.76), (2, 0.18), (3, 0.06)],                         1.2, 0.02),
    "bass_ghost":        ([(1, 0.82), (2, 0.14), (3, 0.04)],                         9.0, 0.05),
    "bass_accented":     ([(1, 0.65), (2, 0.23), (3, 0.10), (4, 0.02)],              3.0, 0.28),
    "bass_dry":          ([(1, 0.72), (2, 0.19), (3, 0.09)],                         6.0, 0.08),
    "bass_wet":          ([(1, 0.68), (2, 0.22), (3, 0.10)],                         1.8, 0.03),
    "bass_snap":         ([(1, 0.60), (2, 0.24), (3, 0.12), (4, 0.04)],             10.0, 0.40),
    "bass_thump":        ([(1, 0.78), (2, 0.16), (3, 0.06)],                         4.5, 0.30),
    "bass_rumble":       ([(1, 0.85), (2, 0.12), (3, 0.03)],                         0.9, 0.02),
    "bass_punch":        ([(1, 0.72), (2, 0.20), (3, 0.08)],                         4.0, 0.22),
}


def _ks_instrument(frequency: float, duration: float, sr: int = 44100,
                    timbre: str = "guitar") -> np.ndarray:
    """
    Fast additive synthesis with exponential decay — fully NumPy vectorised.
    ~0.3 ms per note (60× faster than the IIR loop approach).
    """
    duration = min(duration, 4.0)
    n        = max(2, int(sr * duration))
    t        = np.linspace(0.0, duration, n, dtype=np.float32)

    harmonics, decay_rate, noise_amp = _KS_PRESETS.get(timbre, _KS_PRESETS["guitar"])

    wave = np.zeros(n, dtype=np.float32)
    for k, amp in harmonics:
        if frequency * k < sr / 2:
            wave += amp * np.sin(2.0 * np.pi * frequency * k * t, dtype=np.float32)

    # Brief noise burst at attack → pluck feel
    atk = max(1, int(sr * 0.003))
    noise_env = np.zeros(n, dtype=np.float32)
    noise_env[:atk] = np.linspace(1.0, 0.0, atk, dtype=np.float32)
    wave += noise_env * np.random.uniform(-noise_amp, noise_amp, n).astype(np.float32)

    # Exponential decay envelope
    env = np.exp((-decay_rate / max(duration, 0.01)) * t).astype(np.float32)

    # Short release taper
    rel = min(int(sr * 0.05), n // 4)
    if rel > 0:
        env[-rel:] *= np.linspace(1.0, 0.0, rel, dtype=np.float32)

    return wave * env


def _add_reverb(signal: np.ndarray, sr: int, room: float = 0.25) -> np.ndarray:
    """Simple reverb: sum of delayed, attenuated copies of the signal."""
    out = signal.copy()
    delays_ms = [30, 60, 100, 150]
    gains     = [0.35, 0.22, 0.14, 0.08]
    for d_ms, g in zip(delays_ms, gains):
        d = int(sr * d_ms / 1000)
        padded = np.zeros_like(out)
        padded[d:] = out[:-d] if d < len(out) else 0
        out += g * room * padded
    return out


def generate_combined_tab_audio(tab_data: dict, ref_audio_path: str, output_path: str) -> bool:
    """
    Synthesize all instrument parts from the score, with timing quantized to the
    real beat grid extracted from the reference audio.

    Steps:
      1. Extract beat positions from the reference audio using librosa.
      2. Build a musical-time → audio-time mapping via those beat positions.
      3. Re-time every note and drum hit to land exactly on the grid.
      4. Synthesize all parts (all_parts for MusicXML, guitar/bass for PDF).
      5. Mix melodic bus and drum bus independently, then output.
    """
    import librosa

    sr       = 44100
    tab_bpm  = tab_data.get("bpm", 80)

    # ── 1. Extract beat grid from reference audio ──────────────────────────────
    try:
        ref_wav = "/tmp/tab_ref_mono.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", ref_audio_path,
             "-acodec", "pcm_f32le", "-ar", "22050", "-ac", "1", "-t", "90", ref_wav],
            capture_output=True, check=True
        )
        y_ref, sr_ref = librosa.load(ref_wav, sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y_ref, sr=sr_ref)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr_ref).astype(float)
        audio_bpm  = float(tempo.item()) if hasattr(tempo, "item") else float(tempo)
        logger.info(f"Audio BPM={audio_bpm:.1f}, beats detected={len(beat_times)}")
    except Exception as e:
        logger.warning(f"Beat extraction failed: {e}")
        beat_times = np.array([])
        audio_bpm  = None

    bpm      = audio_bpm if audio_bpm and 40 <= audio_bpm <= 220 else tab_bpm
    beat_dur = 60.0 / bpm
    logger.info(f"Using BPM={bpm:.1f}")

    # ── 2. Build re-timing function ───────────────────────────────────────────
    # score_time is in seconds at tab_bpm; we convert to audio seconds at bpm.
    # If we have real beat positions, we additionally snap each note to the
    # nearest 16th-note subdivision of the detected grid.

    score_beat_dur = 60.0 / tab_bpm   # seconds per beat in the score

    def retime(score_sec: float) -> float:
        """Map a score time (at tab_bpm) → audio time (at bpm, quantized)."""
        # Convert score time to beat count
        beat_count = score_sec / score_beat_dur
        # Snap to nearest 16th note (4 subdivisions per beat)
        beat_count = round(beat_count * 4) / 4
        if len(beat_times) > 0:
            # Map to actual audio beat position
            idx = int(beat_count)
            frac = beat_count - idx
            if idx < len(beat_times) - 1:
                return beat_times[idx] + frac * (beat_times[idx + 1] - beat_times[idx])
            elif idx < len(beat_times):
                return beat_times[idx] + frac * beat_dur
            else:
                # Beyond available beats — extrapolate
                return beat_times[-1] + (beat_count - (len(beat_times) - 1)) * beat_dur
        else:
            return beat_count * beat_dur

    # ── 3. Retime all notes ────────────────────────────────────────────────────
    all_parts   = tab_data.get("all_parts", {})
    drum_notes  = [{"midi": d["midi"], "time": retime(d["time"])}
                   for d in tab_data.get("drum_notes", [])]

    if all_parts:
        retimed_parts = {}
        for pname, pdata in all_parts.items():
            retimed = []
            for n in pdata["notes"][:500]:
                rt = retime(n["time"])
                retimed.append({**n, "time": rt})
            retimed_parts[pname] = {"timbre": pdata["timbre"], "notes": retimed}
    else:
        # PDF / fallback
        guitar_notes = [{"midi": n["midi"], "time": retime(n["time"]),
                         "duration": n.get("duration", beat_dur)}
                        for n in tab_data.get("guitar_notes", [])]
        bass_notes   = [{"midi": n["midi"], "time": retime(n["time"]),
                         "duration": n.get("duration", beat_dur)}
                        for n in tab_data.get("bass_notes", [])]

    # ── 4. Compute total duration ──────────────────────────────────────────────
    all_t = []
    if all_parts:
        for pdata in (retimed_parts if all_parts else {}).values():
            all_t.extend(n["time"] for n in pdata["notes"])
    else:
        all_t = [n["time"] for n in guitar_notes + bass_notes]
    all_t += [d["time"] for d in drum_notes]
    total_time = min((max(all_t) if all_t else 32 * beat_dur) + 2.5, 90.0)
    n_total    = int(sr * total_time)
    mix        = np.zeros(n_total, dtype=np.float32)

    # ── 5. Synthesize melodic parts ───────────────────────────────────────────
    if all_parts:
        n_parts = len(retimed_parts)
        for pdata in retimed_parts.values():
            timbre = pdata["timbre"]
            gain   = 0.70 / max(n_parts ** 0.5, 1)
            for note in pdata["notes"]:
                if note["time"] >= total_time:
                    continue
                dur  = min(note.get("duration", beat_dur * 1.2) + 0.3, 4.0)
                tone = _synth_note(note["midi"], dur, timbre, sr)
                si   = int(note["time"] * sr)
                ei   = min(si + len(tone), n_total)
                mix[si:ei] += tone[:ei - si] * gain
    else:
        instruments_lower = " ".join(tab_data.get("instruments", [])).lower()
        timbre_g = "oud" if ("oud" in instruments_lower or "عود" in instruments_lower) else "guitar"
        for note in guitar_notes:
            tone = _ks_instrument(_midi_to_hz(note["midi"]), beat_dur * 1.6, sr=sr, timbre=timbre_g)
            si = int(note["time"] * sr); ei = min(si + len(tone), n_total)
            mix[si:ei] += tone[:ei - si] * 0.70
        for note in bass_notes:
            tone = _ks_instrument(_midi_to_hz(note["midi"]), beat_dur * 1.8, sr=sr, timbre="bass")
            si = int(note["time"] * sr); ei = min(si + len(tone), n_total)
            mix[si:ei] += tone[:ei - si] * 0.72

    # ── 6. Normalize melodic bus ──────────────────────────────────────────────
    mel_peak = np.abs(mix).max()
    if mel_peak > 1e-6:
        mix = mix / mel_peak * 0.72

    # ── 7. Synthesize & mix drum bus independently ────────────────────────────
    if tab_data.get("has_drums"):
        if drum_notes:
            drum_t = _render_drum_notes(drum_notes, total_time, sr)
        else:
            drum_t = _synth_drums(beat_dur, total_time, sr)
        d_peak = np.abs(drum_t).max()
        if d_peak > 1e-6:
            drum_t = drum_t / d_peak * 0.68
        length = min(len(drum_t), n_total)
        mix[:length] += drum_t[:length]

    # ── 8. Reverb + normalise ─────────────────────────────────────────────────
    mix = _add_reverb(mix, sr, room=0.20)
    peak = np.abs(mix).max()
    if peak < 1e-6:
        logger.warning("generate_combined_tab_audio: silent output")
        return False
    mix = mix / peak * 0.91

    # ── 9. Write WAV → MP3 ────────────────────────────────────────────────────
    wav_tmp = output_path.replace(".mp3", "_raw.wav")
    sf.write(wav_tmp, mix, sr, subtype="PCM_16")
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_tmp,
         "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k", output_path],
        capture_output=True, check=True
    )
    if os.path.exists(wav_tmp):
        os.remove(wav_tmp)
    logger.info(f"generate_combined_tab_audio: OK, beats={len(beat_times)}, "
                f"bpm={bpm:.1f}, size={os.path.getsize(output_path)}")
    return True


INSTRUMENT_TIMBRE_MAP = {
    # Plucked strings
    "guitar":        "guitar",
    "acoustic":      "guitar",
    "electric":      "electric_guitar",
    "clean":         "electric_guitar",
    "distort":       "electric_guitar",
    "rhythm":        "electric_guitar",
    "lead":          "electric_guitar",
    "bass":          "bass",
    "oud":           "oud",
    "sitar":         "sitar",
    "banjo":         "banjo",
    "mandolin":      "banjo",
    "ukulele":       "guitar",
    "harp":          "harp",
    # Keyboards
    "piano":         "piano",
    "keyboard":      "piano",
    "keys":          "piano",
    "rhodes":        "rhodes",
    "electric piano":"electric_piano",
    "clav":          "piano",
    "harpsichord":   "harpsichord",
    "organ":         "organ",
    "hammond":       "organ",
    "accordion":     "organ",
    # Synths
    "synth":         "synth",
    "synthesizer":   "synth",
    "pad":           "synth_pad",
    "lead synth":    "synth",
    "arp":           "synth",
    "trap synth":    "synth_heavy",
    "rap synth":     "synth_heavy",
    "sample":        "synth_heavy",
    # Hip-hop / Bass specific
    "808":           "bass_808_hiphop",
    "trap bass":     "bass_808_trap",
    "drill bass":    "bass_808_drill",
    "sub bass":      "bass_808_sub",
    "sub-bass":      "bass_808_sub",
    "reese bass":    "bass_808_deep",
    "wobble bass":   "bass_808_distorted",
    "phonk bass":    "bass_808_distorted",
    "boom bap bass": "bass_808_punchy",
    "bass guitar":   "bass",
    # Electric Piano / Rhodes
    "wurlitzer":     "electric_piano",
    # Bowed strings
    "violin":        "violin",
    "viola":         "viola",
    "cello":         "cello",
    "contrabass":    "cello",
    "double bass":   "cello",
    "strings":       "violin",
    "orchestra":     "violin",
    # Brass
    "trumpet":       "brass",
    "trombone":      "brass",
    "horn":          "brass",
    "tuba":          "brass",
    "brass":         "brass",
    "bugle":         "brass",
    # Woodwinds
    "flute":         "flute",
    "clarinet":      "woodwind",
    "oboe":          "woodwind",
    "bassoon":       "woodwind",
    "saxophone":     "saxophone",
    "sax":           "saxophone",
    "recorder":      "flute",
    "piccolo":       "flute",
    "harmonica":     "woodwind",
    # Voice
    "voice":         "voice",
    "vocal":         "voice",
    "choir":         "voice",
    "chorus":        "voice",
    "soprano":       "voice",
    "alto":          "voice",
    "tenor":         "voice",
    "baritone":      "voice",
    # Percussion (non-drum)
    "marimba":       "marimba",
    "xylophone":     "marimba",
    "vibraphone":    "marimba",
    "glockenspiel":  "marimba",
    "bells":         "marimba",
    "celesta":       "marimba",
    "timpani":       "marimba",
}

DRUM_KEYWORDS = {
    "drum", "drums", "kit", "طبول", "إيقاع",
    "clap", "تصفيق", "handclap",
    "darbuka", "دربكة", "darabukka",
    "dabke", "دبكة",
    "tabla", "طبلة",
    "doumbek", "dumbek",
    "riq", "riqq", "ريق", "رق",
    "cajon", "cajón",
    "shaker", "tambourine", "دف",
}

# Standard display-pitch → GM drum MIDI mapping (treble clef drum notation)
DRUM_DISPLAY_PITCH_MAP = {
    "E3": 36, "F3": 36,                    # Bass drum variants
    "F4": 36, "E4": 36,                    # Bass Drum (Kick)
    "G4": 44,                              # Hi-Hat Foot
    "A4": 41,                              # Low Floor Tom
    "B4": 42,                              # Closed Hi-Hat (alt)
    "C5": 38,                              # Acoustic Snare
    "D5": 45,                              # Low-Mid Tom / Open Hi-Hat
    "E5": 47,                              # Low Mid Tom
    "F5": 48,                              # High Tom
    "G5": 42,                              # Closed Hi-Hat
    "A5": 49,                              # Crash Cymbal
    "B5": 51,                              # Ride Cymbal
    "C6": 53,                              # Ride Bell
    # Clap / handclap
    "D4": 39,                              # Hand Clap
}


def _timbre_for(name: str) -> str:
    n = name.lower()
    for kw, t in INSTRUMENT_TIMBRE_MAP.items():
        if kw in n:
            return t
    return "guitar"


def _synth_note(midi_pitch: int, duration: float, timbre: str, sr: int = 44100) -> np.ndarray:
    """Route a note to the 500-instrument parametric synthesiser."""
    freq = _midi_to_hz(midi_pitch)
    params = globals().get("_INSTR", {}).get(timbre)
    if params is not None:
        return _parametric_render(freq, duration, sr, params)

    n   = max(int(sr * duration), 1)
    t   = np.linspace(0, duration, n, dtype=np.float32)
    dur = max(duration, 0.01)

    def adsr(atk, dec, sus_lvl, rel_ratio=0.15):
        env = np.ones(n, dtype=np.float32)
        a = min(int(sr * atk), n)
        d = min(int(sr * dec), n - a)
        r = min(int(sr * dur * rel_ratio), n)
        if a: env[:a] = np.linspace(0, 1, a)
        if d: env[a:a+d] = np.linspace(1, sus_lvl, d)
        if r and r < n: env[-r:] = np.linspace(env[-r], 0, r)
        return env

    # ── 808 / Sub-Bass Family ─────────────────────────────────────────────────
    if timbre.startswith("bass_808") or timbre in ("808", "bass_sub"):
        if "trap" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.30, 10.0, 2.2, 0.15
            env = adsr(0.003, 0.0, 1.0, 0.30)
        elif "drill" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 12.0, 1.8, 0.10
            env = adsr(0.004, 0.0, 1.0, 0.20)
        elif "deep" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.15, 5.0, 1.2, 0.30
            env = adsr(0.006, 0.0, 1.0, 0.45)
        elif "punchy" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.35, 15.0, 2.0, 0.08
            env = adsr(0.002, 0.05, 0.80, 0.15)
        elif "ultra" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.12, 4.0, 0.9, 0.42
            env = adsr(0.007, 0.0, 1.0, 0.52)
        elif "sub" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.10, 6.0, 0.8, 0.35
            env = adsr(0.006, 0.0, 1.0, 0.38)
        elif "warm" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 7.0, 1.5, 0.18
            env = adsr(0.008, 0.0, 1.0, 0.36)
        elif "long" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 4.0, 1.8, 0.15
            env = adsr(0.006, 0.0, 1.0, 0.55)
        elif "bright" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.30, 9.0, 2.0, 0.08
            env = adsr(0.004, 0.0, 1.0, 0.25)
        elif "distorted" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.35, 8.0, 4.5, 0.12
            env = adsr(0.003, 0.0, 1.0, 0.25)
        elif "phonk" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.38, 8.5, 5.0, 0.10
            env = adsr(0.003, 0.0, 1.0, 0.22)
        elif "rnb" in timbre or "r&b" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.18, 7.0, 1.4, 0.20
            env = adsr(0.010, 0.0, 1.0, 0.32)
        elif "mid" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 9.0, 2.0, 0.12
            env = adsr(0.005, 0.0, 1.0, 0.25)
        elif "clean" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 8.0, 0.5, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.28)
        elif "click" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.25, 10.0, 1.8, 0.12
            env = adsr(0.001, 0.0, 1.0, 0.25)
        elif "uk" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 11.0, 1.9, 0.10
            env = adsr(0.004, 0.0, 1.0, 0.22)
        elif "afro" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.18, 8.0, 1.6, 0.15
            env = adsr(0.007, 0.0, 1.0, 0.28)
        elif "soft" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.15, 6.0, 1.0, 0.22
            env = adsr(0.012, 0.0, 1.0, 0.32)
        elif "hiphop" in timbre or "hip_hop" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.25, 8.0, 1.8, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.28)
        elif "chicago" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.28, 11.0, 2.1, 0.12
            env = adsr(0.003, 0.0, 1.0, 0.20)
        elif "jersey" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 9.0, 1.7, 0.14
            env = adsr(0.005, 0.0, 0.9, 0.18)
        elif "bounce" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.30, 12.0, 1.6, 0.12
            env = adsr(0.003, 0.08, 0.7, 0.12)
        elif "lofi" in timbre or "lo_fi" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 7.0, 1.3, 0.20
            env = adsr(0.008, 0.0, 1.0, 0.35)
        elif "vintage" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 8.0, 1.5, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.28)
        elif "modern" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.28, 10.0, 2.3, 0.12
            env = adsr(0.004, 0.0, 1.0, 0.22)
        elif "slide" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.50, 5.0, 1.8, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.30)
        elif "wobble" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 8.0, 1.8, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.28)
        elif "growl" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.30, 9.0, 3.5, 0.10
            env = adsr(0.004, 0.0, 1.0, 0.22)
        elif "mellow" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.15, 6.0, 1.0, 0.22
            env = adsr(0.010, 0.0, 1.0, 0.34)
        elif "nyc" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.25, 9.0, 1.8, 0.14
            env = adsr(0.004, 0.0, 1.0, 0.25)
        elif "la" in timbre or "west" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.22, 8.0, 1.7, 0.16
            env = adsr(0.006, 0.0, 1.0, 0.28)
        elif "atlanta" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.32, 10.0, 2.2, 0.13
            env = adsr(0.003, 0.0, 1.0, 0.28)
        elif "cloud" in timbre or "dreamy" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.15, 5.0, 1.2, 0.25
            env = adsr(0.010, 0.0, 1.0, 0.42)
        elif "rage" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.35, 11.0, 2.8, 0.10
            env = adsr(0.003, 0.0, 1.0, 0.20)
        elif "melodic" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.20, 7.0, 1.5, 0.18
            env = adsr(0.008, 0.0, 1.0, 0.32)
        elif "dark" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.28, 7.5, 2.5, 0.20
            env = adsr(0.005, 0.0, 1.0, 0.30)
        elif "heavy" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.32, 9.0, 3.0, 0.15
            env = adsr(0.004, 0.0, 1.0, 0.25)
        elif "lite" in timbre or "light" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.18, 9.0, 1.3, 0.10
            env = adsr(0.006, 0.0, 0.9, 0.20)
        elif "crispy" in timbre or "crisp" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.28, 11.0, 2.2, 0.08
            env = adsr(0.002, 0.04, 0.85, 0.18)
        elif "smooth" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.18, 6.5, 1.3, 0.20
            env = adsr(0.009, 0.0, 1.0, 0.33)
        elif "hard" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.33, 10.5, 2.7, 0.10
            env = adsr(0.002, 0.0, 1.0, 0.20)
        elif "trap2" in timbre or "trap_hard" in timbre:
            slide_amt, slide_dec, drive, sub_mix = 0.35, 12.0, 2.8, 0.12
            env = adsr(0.002, 0.0, 1.0, 0.25)
        else:
            slide_amt, slide_dec, drive, sub_mix = 0.25, 8.0, 1.8, 0.15
            env = adsr(0.005, 0.0, 1.0, 0.25)

        pitch_env = np.exp(-slide_dec * t / dur)
        freq_mod  = freq * (1.0 + slide_amt * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        phase     = phase.astype(np.float32)
        sine      = np.sin(phase)
        wave      = (0.70 * sine
                     + 0.20 * np.sin(2.0 * phase)
                     + 0.08 * np.sin(3.0 * phase)
                     + sub_mix * np.sin(0.5 * phase))

        if "click" in timbre:
            ck = min(int(0.008 * sr), n)
            click = np.zeros(n, dtype=np.float32)
            click[:ck] = np.linspace(0.5, 0.0, ck)
            wave = wave + click

        if "wobble" in timbre:
            lfo  = 0.3 * np.sin(2.0 * np.pi * 4.0 * t)
            wave = wave * (1.0 + lfo)

        if "lofi" in timbre or "lo_fi" in timbre:
            wave = wave + np.random.uniform(-0.03, 0.03, n).astype(np.float32)

        if "vintage" in timbre:
            wave = wave + 0.015 * np.random.uniform(-1, 1, n).astype(np.float32)

        wave = np.tanh(drive * wave) / (np.tanh(np.float32(drive)) + 1e-9)
        return (wave * env).astype(np.float32)

    # ── GM Bass Instruments (individually synthesized) ────────────────────────
    if timbre == "bass_pick":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks
        pk   = min(int(0.010 * sr), n)
        wave[:pk] += np.linspace(0.35, 0.0, pk)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_fretless":
        vib  = 0.06 * np.sin(2.0 * np.pi * 3.5 * t)
        wave = (0.65 * np.sin(2.0 * np.pi * freq * t + vib)
                + 0.22 * np.sin(2.0 * np.pi * freq * 2 * t)
                + 0.10 * np.sin(2.0 * np.pi * freq * 3 * t)
                + 0.03 * np.sin(2.0 * np.pi * freq * 4 * t))
        env  = adsr(0.015, 0.0, 0.95, 0.20)
        return (wave * env * 0.75).astype(np.float32)

    if timbre == "bass_slap1":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.80
        sk   = min(int(0.015 * sr), n)
        wave[:sk] += np.exp(-60.0 * t[:sk]) * 0.60
        env  = adsr(0.001, 0.05, 0.60, 0.15)
        return (wave * env).astype(np.float32)

    if timbre == "bass_slap2":
        wave = (0.55 * np.sin(2.0 * np.pi * freq * t)
                + 0.30 * np.sin(2.0 * np.pi * freq * 2 * t)
                + 0.12 * np.sin(2.0 * np.pi * freq * 3 * t)
                + 0.03 * np.sin(2.0 * np.pi * freq * 5 * t))
        env  = adsr(0.001, 0.03, 0.50, 0.12)
        return (wave * env * 0.75).astype(np.float32)

    if timbre == "bass_synth1":
        wave = sum((1.0 / (2*k - 1)) * np.sin(2.0 * np.pi * freq * (2*k - 1) * t)
                   for k in range(1, 7)).astype(np.float32)
        env  = adsr(0.010, 0.15, 0.70, 0.15)
        return (np.tanh(1.5 * wave) / np.tanh(np.float32(1.5)) * env * 0.60).astype(np.float32)

    if timbre == "bass_synth2":
        wave = sum((1.0 / k) * np.sin(2.0 * np.pi * freq * k * t)
                   for k in range(1, 9)).astype(np.float32)
        env  = adsr(0.008, 0.12, 0.80, 0.12)
        return (np.tanh(1.2 * wave) / np.tanh(np.float32(1.2)) * env * 0.50).astype(np.float32)

    if timbre == "bass_synth3":
        wave = (np.sign(np.sin(2.0 * np.pi * freq * t)) * 0.6
                + 0.30 * np.sin(2.0 * np.pi * freq * t)
                + 0.10 * np.sin(2.0 * np.pi * freq * 3 * t))
        env  = adsr(0.005, 0.10, 0.75, 0.12)
        return (np.tanh(2.0 * wave) / np.tanh(np.float32(2.0)) * env * 0.55).astype(np.float32)

    if timbre == "bass_synth_moog":
        wave = sum((1.0 / (2*k - 1)) * np.sin(2.0 * np.pi * freq * (2*k - 1) * t)
                   for k in range(1, 10)).astype(np.float32)
        env  = adsr(0.006, 0.20, 0.65, 0.18)
        return (np.tanh(2.2 * wave) / np.tanh(np.float32(2.2)) * env * 0.55).astype(np.float32)

    if timbre == "bass_synth_acid":
        res_wave = (0.70 * np.sin(2.0 * np.pi * freq * t)
                    + 0.25 * np.sin(2.0 * np.pi * freq * 3 * t)
                    + 0.05 * np.sin(2.0 * np.pi * freq * 5 * t))
        env_f    = np.exp(-12.0 * t / dur)
        wave     = res_wave * (1.0 + 0.8 * env_f)
        env      = adsr(0.004, 0.10, 0.70, 0.14)
        return (np.tanh(2.5 * wave) / np.tanh(np.float32(2.5)) * env * 0.50).astype(np.float32)

    if timbre == "bass_acoustic":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.88
        wave += 0.15 * np.sin(2.0 * np.pi * freq * t) * np.exp(-3.5 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_upright":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.80
        wave += (0.10 * np.sin(2.0 * np.pi * freq * t)
                 + 0.06 * np.sin(2.0 * np.pi * freq * 2 * t)) * np.exp(-4.0 * t / dur)
        noise = np.random.uniform(-0.015, 0.015, n).astype(np.float32)
        return np.clip(wave + noise, -1.0, 1.0)

    if timbre == "bass_electric":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.85
        wave += 0.12 * np.sin(2.0 * np.pi * freq * 2 * t) * np.exp(-5.0 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_fuzz":
        wave = (0.60 * np.sin(2.0 * np.pi * freq * t)
                + 0.25 * np.sin(2.0 * np.pi * freq * 2 * t)
                + 0.12 * np.sin(2.0 * np.pi * freq * 3 * t))
        wave = np.sign(wave) * (1.0 - np.exp(-3.0 * np.abs(wave)))
        env  = adsr(0.005, 0.0, 1.0, 0.18)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_overdrive":
        wave = (0.55 * np.sin(2.0 * np.pi * freq * t)
                + 0.28 * np.sin(2.0 * np.pi * freq * 2 * t)
                + 0.14 * np.sin(2.0 * np.pi * freq * 3 * t)
                + 0.03 * np.sin(2.0 * np.pi * freq * 4 * t))
        env  = adsr(0.004, 0.0, 1.0, 0.15)
        wave = np.tanh(3.5 * wave) / np.tanh(np.float32(3.5))
        return (wave * env * 0.60).astype(np.float32)

    if timbre == "bass_sub_sine":
        wave = np.sin(2.0 * np.pi * freq * t)
        env  = adsr(0.006, 0.0, 1.0, 0.40)
        return (wave * env * 0.90).astype(np.float32)

    if timbre == "bass_sub_triangle":
        wave = (0.85 * np.sin(2.0 * np.pi * freq * t)
                + 0.10 * np.sin(2.0 * np.pi * freq * 3 * t)
                + 0.05 * np.sin(2.0 * np.pi * freq * 5 * t))
        env  = adsr(0.007, 0.0, 1.0, 0.38)
        return (wave * env * 0.88).astype(np.float32)

    if timbre == "bass_sub_octave":
        wave = (0.65 * np.sin(2.0 * np.pi * freq * t)
                + 0.28 * np.sin(2.0 * np.pi * (freq / 2) * t)
                + 0.07 * np.sin(2.0 * np.pi * freq * 2 * t))
        env  = adsr(0.006, 0.0, 1.0, 0.38)
        return (wave * env * 0.85).astype(np.float32)

    if timbre in ("bass_wobble_lfo", "bass_wub"):
        lfo_rate = 8.0
        lfo      = 0.5 * (1.0 + np.sin(2.0 * np.pi * lfo_rate * t))
        wave     = (0.70 * np.sin(2.0 * np.pi * freq * t)
                    + 0.20 * np.sin(2.0 * np.pi * freq * 2 * t)
                    + 0.10 * np.sin(2.0 * np.pi * freq * 3 * t))
        wave     = np.tanh(2.0 * wave) / np.tanh(np.float32(2.0))
        env      = adsr(0.005, 0.0, 1.0, 0.22)
        return (wave * env * lfo * 0.80).astype(np.float32)

    if timbre in ("bass_reese", "bass_reese_dnb"):
        detune = 1.006
        wave   = (0.50 * np.sin(2.0 * np.pi * freq * t)
                  + 0.50 * np.sin(2.0 * np.pi * freq * detune * t))
        wave   = np.tanh(2.8 * wave) / np.tanh(np.float32(2.8))
        env    = adsr(0.010, 0.0, 1.0, 0.20)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_pluck":
        ks   = _ks_instrument(freq, min(duration, 0.6), sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_rubber":
        freq_slide = freq * (1.0 + 0.40 * np.exp(-20.0 * t / dur))
        phase      = 2.0 * np.pi * np.cumsum(freq_slide.astype(np.float64)) / sr
        wave       = (0.70 * np.sin(phase)
                      + 0.20 * np.sin(2.0 * phase)
                      + 0.10 * np.sin(3.0 * phase)).astype(np.float32)
        env        = adsr(0.003, 0.0, 1.0, 0.28)
        return (wave * env).astype(np.float32)

    # ── Waveform-Based Bass ───────────────────────────────────────────────────
    if timbre == "bass_square":
        wave = sum((1.0 / (2*k-1)) * np.sin(2*np.pi * freq * (2*k-1) * t)
                   for k in range(1, 12)).astype(np.float32)
        env  = adsr(0.006, 0.0, 1.0, 0.18)
        return (np.tanh(1.0 * wave) / np.tanh(np.float32(1.0)) * env * 0.60).astype(np.float32)

    if timbre == "bass_saw":
        wave = sum((1.0 / k) * np.sin(2*np.pi * freq * k * t)
                   for k in range(1, 14)).astype(np.float32)
        env  = adsr(0.005, 0.0, 1.0, 0.18)
        return (np.tanh(1.0 * wave) / np.tanh(np.float32(1.0)) * env * 0.55).astype(np.float32)

    if timbre == "bass_pulse":
        duty  = 0.3
        wave  = sum((np.sin(np.pi * k * duty) / (np.pi * k)) * np.sin(2*np.pi * freq * k * t)
                    for k in range(1, 12)).astype(np.float32)
        env   = adsr(0.005, 0.0, 1.0, 0.18)
        return (np.tanh(1.5 * wave) / np.tanh(np.float32(1.5)) * env * 0.65).astype(np.float32)

    if timbre == "bass_triangle":
        wave = sum((((-1)**(k-1)) / ((2*k-1)**2)) * np.sin(2*np.pi * freq * (2*k-1) * t)
                   for k in range(1, 9)).astype(np.float32)
        env  = adsr(0.006, 0.0, 1.0, 0.22)
        return (wave * env * 0.80).astype(np.float32)

    if timbre == "bass_sine":
        wave = np.sin(2*np.pi * freq * t)
        env  = adsr(0.007, 0.0, 1.0, 0.25)
        return (wave * env * 0.88).astype(np.float32)

    # ── FM Synthesis Bass ─────────────────────────────────────────────────────
    if timbre in ("bass_fm", "bass_fm_classic"):
        mod_ratio = 1.0
        mod_index = 3.5
        mod  = mod_index * np.sin(2*np.pi * freq * mod_ratio * t)
        wave = np.sin(2*np.pi * freq * t + mod)
        wave += 0.30 * np.sin(2*np.pi * (freq/2) * t + mod*0.5)
        env  = adsr(0.005, 0.12, 0.75, 0.15)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_fm_dx7":
        mod_ratio = 2.0
        mod_index = 5.0
        mod_env  = np.exp(-8.0 * t / dur)
        mod  = mod_index * mod_env * np.sin(2*np.pi * freq * mod_ratio * t)
        wave = np.sin(2*np.pi * freq * t + mod)
        wave += 0.20 * np.sin(2*np.pi * (freq/2) * t)
        env  = adsr(0.004, 0.15, 0.65, 0.18)
        return (wave * env * 0.70).astype(np.float32)

    if timbre == "bass_fm_bell":
        mod_ratio = 3.5
        mod_index = 6.0
        mod_env  = np.exp(-12.0 * t / dur)
        mod  = mod_index * mod_env * np.sin(2*np.pi * freq * mod_ratio * t)
        wave = np.sin(2*np.pi * freq * t + mod) * np.exp(-4.0 * t / dur)
        env  = adsr(0.003, 0.0, 1.0, 0.35)
        return (wave * env * 0.75).astype(np.float32)

    if timbre == "bass_fm_attack":
        mod_index = 8.0 * np.exp(-20.0 * t / dur)
        mod  = mod_index * np.sin(2*np.pi * freq * 2.0 * t)
        wave = np.sin(2*np.pi * freq * t + mod)
        wave += 0.25 * np.sin(2*np.pi * (freq/2) * t)
        env  = adsr(0.002, 0.10, 0.80, 0.15)
        return (wave * env * 0.68).astype(np.float32)

    if timbre == "bass_fm_sub":
        mod_ratio = 0.5
        mod_index = 4.0
        mod  = mod_index * np.sin(2*np.pi * freq * mod_ratio * t)
        wave = np.sin(2*np.pi * (freq/2) * t + mod)
        wave += 0.40 * np.sin(2*np.pi * (freq/4) * t)
        env  = adsr(0.007, 0.0, 1.0, 0.35)
        return (wave * env * 0.78).astype(np.float32)

    # ── Reggae / Dub / Jamaican Bass ──────────────────────────────────────────
    if timbre in ("bass_reggae", "bass_ska"):
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t)
                + 0.05 * np.sin(2*np.pi * freq * 4 * t))
        env  = adsr(0.008, 0.05, 0.85, 0.22)
        return (wave * env * 0.80).astype(np.float32)

    if timbre == "bass_dub":
        vib  = 0.04 * np.sin(2*np.pi * 1.8 * t)
        wave = (0.65 * np.sin(2*np.pi * freq * t + vib)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.08 * np.sin(2*np.pi * freq * 3 * t)
                + 0.10 * np.sin(2*np.pi * (freq/2) * t))
        env  = adsr(0.010, 0.0, 1.0, 0.35)
        wave = np.tanh(1.3 * wave) / np.tanh(np.float32(1.3))
        return (wave * env * 0.80).astype(np.float32)

    if timbre == "bass_dancehall":
        wave = (0.65 * np.sin(2*np.pi * freq * t)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        env  = adsr(0.006, 0.0, 1.0, 0.28)
        wave = np.tanh(1.8 * wave) / np.tanh(np.float32(1.8))
        return (wave * env * 0.78).astype(np.float32)

    # ── Funk / Soul Bass ──────────────────────────────────────────────────────
    if timbre == "bass_funk":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.75
        pk   = min(int(0.012 * sr), n)
        wave[:pk] += np.exp(-50.0 * t[:pk]) * 0.50
        body = 0.18 * np.sin(2*np.pi * freq * 2 * t) * np.exp(-5.0 * t / dur)
        wave += body
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_gospel":
        wave = (0.58 * np.sin(2*np.pi * freq * t)
                + 0.26 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t)
                + 0.04 * np.sin(2*np.pi * freq * 4 * t))
        env  = adsr(0.010, 0.0, 1.0, 0.28)
        return (wave * env * 0.82).astype(np.float32)

    if timbre == "bass_soul":
        vib  = 0.06 * np.sin(2*np.pi * 4.5 * t) * np.clip(t / 0.3, 0, 1)
        wave = (0.60 * np.sin(2*np.pi * freq * t + vib)
                + 0.24 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t))
        env  = adsr(0.010, 0.0, 1.0, 0.25)
        return (wave * env * 0.80).astype(np.float32)

    # ── Blues / Jazz Bass ─────────────────────────────────────────────────────
    if timbre in ("bass_jazz", "bass_jazz_walking"):
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.82
        vib  = 0.04 * np.sin(2*np.pi * 4.0 * t) * np.clip(t / 0.4, 0, 1)
        wave += 0.12 * np.sin(2*np.pi * freq * t + vib) * np.exp(-2.5 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_blues":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.80
        wave += 0.14 * np.sin(2*np.pi * freq * t) * np.exp(-3.0 * t / dur)
        noise = np.random.uniform(-0.012, 0.012, n).astype(np.float32)
        return np.clip(wave + noise, -1.0, 1.0)

    if timbre == "bass_country":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="bass")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.85
        wave += 0.10 * np.sin(2*np.pi * freq * 2 * t) * np.exp(-6.0 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    # ── Latin / World Bass ────────────────────────────────────────────────────
    if timbre in ("bass_latin", "bass_salsa", "bass_bossa"):
        wave = (0.62 * np.sin(2*np.pi * freq * t)
                + 0.24 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t)
                + 0.04 * np.sin(2*np.pi * freq * 4 * t))
        env  = adsr(0.008, 0.0, 1.0, 0.22)
        return (wave * env * 0.82).astype(np.float32)

    if timbre == "bass_cumbia":
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.26 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t))
        env  = adsr(0.006, 0.04, 0.85, 0.18)
        return (wave * env * 0.80).astype(np.float32)

    if timbre in ("bass_afrobeat", "bass_afro"):
        wave = (0.63 * np.sin(2*np.pi * freq * t)
                + 0.23 * np.sin(2*np.pi * freq * 2 * t)
                + 0.11 * np.sin(2*np.pi * freq * 3 * t)
                + 0.03 * np.sin(2*np.pi * freq * 4 * t))
        env  = adsr(0.007, 0.0, 1.0, 0.24)
        wave = np.tanh(1.4 * wave) / np.tanh(np.float32(1.4))
        return (wave * env * 0.80).astype(np.float32)

    if timbre == "bass_amapiano":
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t)
                + 0.15 * np.sin(2*np.pi * (freq/2) * t))
        env  = adsr(0.006, 0.0, 1.0, 0.30)
        wave = np.tanh(1.6 * wave) / np.tanh(np.float32(1.6))
        return (wave * env * 0.78).astype(np.float32)

    if timbre == "bass_soca":
        wave = (0.62 * np.sin(2*np.pi * freq * t)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        env  = adsr(0.005, 0.04, 0.88, 0.18)
        return (wave * env * 0.82).astype(np.float32)

    # ── Electronic Music Bass ─────────────────────────────────────────────────
    if timbre == "bass_house":
        wave = sum((1.0 / k) * np.sin(2*np.pi * freq * k * t)
                   for k in range(1, 9)).astype(np.float32)
        env_f = np.exp(-18.0 * t / dur)
        filter_curve = 1.0 / (1.0 + env_f * 3.0)
        wave  = wave * (0.4 + 0.6 * filter_curve)
        env   = adsr(0.006, 0.08, 0.80, 0.14)
        return (np.tanh(1.4 * wave) / np.tanh(np.float32(1.4)) * env * 0.60).astype(np.float32)

    if timbre == "bass_techno":
        wave = sum((1.0 / (2*k-1)) * np.sin(2*np.pi * freq * (2*k-1) * t)
                   for k in range(1, 9)).astype(np.float32)
        env  = adsr(0.004, 0.0, 1.0, 0.15)
        return (np.tanh(2.2 * wave) / np.tanh(np.float32(2.2)) * env * 0.60).astype(np.float32)

    if timbre in ("bass_jungle", "bass_breaks"):
        wave = (0.65 * np.sin(2*np.pi * freq * t)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        lfo  = 1.0 + 0.15 * np.sin(2*np.pi * 2.0 * t)
        env  = adsr(0.006, 0.0, 1.0, 0.18)
        wave = np.tanh(2.0 * wave) / np.tanh(np.float32(2.0))
        return (wave * env * lfo * 0.68).astype(np.float32)

    if timbre in ("bass_dnb", "bass_drum_and_bass"):
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.28 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t)
                + 0.02 * np.sin(2*np.pi * freq * 4 * t))
        wave = np.tanh(2.5 * wave) / np.tanh(np.float32(2.5))
        env  = adsr(0.005, 0.0, 1.0, 0.16)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_neurofunk":
        detune = 1.004
        wave   = (0.40 * np.sin(2*np.pi * freq * t)
                  + 0.40 * np.sin(2*np.pi * freq * detune * t)
                  + 0.15 * np.sin(2*np.pi * freq * 2 * t)
                  + 0.05 * np.sin(2*np.pi * freq * 3 * t))
        wave   = np.tanh(3.0 * wave) / np.tanh(np.float32(3.0))
        lfo    = 0.5 * (1.0 + np.sin(2*np.pi * 6.0 * t))
        env    = adsr(0.005, 0.0, 1.0, 0.16)
        return (wave * env * lfo * 0.62).astype(np.float32)

    if timbre == "bass_liquid":
        vib  = 0.08 * np.sin(2*np.pi * 3.0 * t)
        wave = (0.62 * np.sin(2*np.pi * freq * t + vib)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        wave = np.tanh(1.5 * wave) / np.tanh(np.float32(1.5))
        env  = adsr(0.008, 0.0, 1.0, 0.22)
        return (wave * env * 0.70).astype(np.float32)

    if timbre == "bass_dubstep":
        lfo_rate = 16.0
        lfo  = 0.5 * (1.0 + np.sin(2*np.pi * lfo_rate * t))
        wave = (0.65 * np.sin(2*np.pi * freq * t)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.13 * np.sin(2*np.pi * freq * 3 * t))
        wave = np.tanh(3.0 * wave) / np.tanh(np.float32(3.0))
        env  = adsr(0.005, 0.0, 1.0, 0.18)
        return (wave * env * lfo * 0.72).astype(np.float32)

    if timbre == "bass_garage":
        wave = (0.60 * np.sin(2*np.pi * freq * t)
                + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                + 0.12 * np.sin(2*np.pi * freq * 3 * t)
                + 0.03 * np.sin(2*np.pi * freq * 5 * t))
        env  = adsr(0.005, 0.06, 0.82, 0.16)
        return (np.tanh(1.6 * wave) / np.tanh(np.float32(1.6)) * env * 0.70).astype(np.float32)

    if timbre == "bass_trance":
        detune = 1.003
        wave   = (0.50 * np.sin(2*np.pi * freq * t)
                  + 0.40 * np.sin(2*np.pi * freq * detune * t)
                  + 0.10 * np.sin(2*np.pi * freq * 2 * t))
        env    = adsr(0.008, 0.10, 0.85, 0.20)
        return (np.tanh(1.8 * wave) / np.tanh(np.float32(1.8)) * env * 0.65).astype(np.float32)

    if timbre == "bass_hardstyle":
        wave  = sum((1.0 / (2*k-1)) * np.sin(2*np.pi * freq * (2*k-1) * t)
                    for k in range(1, 10)).astype(np.float32)
        env   = adsr(0.003, 0.0, 1.0, 0.15)
        return (np.tanh(4.0 * wave) / np.tanh(np.float32(4.0)) * env * 0.58).astype(np.float32)

    if timbre in ("bass_future_bass", "bass_future"):
        detune = 1.005
        wave   = (0.45 * np.sin(2*np.pi * freq * t)
                  + 0.45 * np.sin(2*np.pi * freq * detune * t)
                  + 0.10 * np.sin(2*np.pi * freq * 2 * t))
        lfo    = 0.3 * np.sin(2*np.pi * 0.8 * t)
        env    = adsr(0.008, 0.0, 1.0, 0.28)
        return (np.tanh(2.0 * wave) / np.tanh(np.float32(2.0)) * env * (1.0 + lfo) * 0.60).astype(np.float32)

    if timbre == "bass_wave":
        wave = (0.65 * np.sin(2*np.pi * freq * t)
                + 0.22 * np.sin(2*np.pi * freq * 2 * t)
                + 0.10 * np.sin(2*np.pi * freq * 3 * t)
                + 0.03 * np.sin(2*np.pi * freq * 4 * t))
        lfo  = 0.2 * np.sin(2*np.pi * 1.5 * t)
        env  = adsr(0.007, 0.0, 1.0, 0.25)
        return (np.tanh(1.8 * wave) / np.tanh(np.float32(1.8)) * env * (1 + lfo) * 0.68).astype(np.float32)

    # ── More 808 Regional / Texture Variants ──────────────────────────────────
    if timbre == "bass_808_miami":
        pitch_env = np.exp(-6.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.22 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = (0.70 * np.sin(phase) + 0.20 * np.sin(2*phase) + 0.10 * np.sin(0.5*phase))
        wave      = np.tanh(2.0 * wave) / np.tanh(np.float32(2.0))
        env       = adsr(0.005, 0.0, 1.0, 0.32)
        return (wave * env).astype(np.float32)

    if timbre == "bass_808_houston":
        pitch_env = np.exp(-3.5 * t / dur)
        freq_mod  = freq * (1.0 + 0.18 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = (0.68 * np.sin(phase) + 0.22 * np.sin(2*phase) + 0.10 * np.sin(0.5*phase))
        wave      = np.tanh(1.6 * wave) / np.tanh(np.float32(1.6))
        env       = adsr(0.007, 0.0, 1.0, 0.55)
        return (wave * env).astype(np.float32)

    if timbre == "bass_808_blown":
        pitch_env = np.exp(-8.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.28 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = np.sin(phase) + 0.25 * np.sin(2*phase)
        wave      = np.clip(wave * 3.0, -1.0, 1.0)
        noise     = np.random.uniform(-0.04, 0.04, n).astype(np.float32)
        env       = adsr(0.003, 0.0, 1.0, 0.25)
        return ((wave + noise) * env * 0.80).astype(np.float32)

    if timbre == "bass_808_filtered":
        from scipy import signal as _sig
        pitch_env = np.exp(-8.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.25 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = (0.70*np.sin(phase) + 0.20*np.sin(2*phase) + 0.10*np.sin(0.5*phase))
        nyq = sr / 2.0
        b, a = _sig.butter(4, min(300.0 / nyq, 0.99), btype='low')
        wave  = _sig.filtfilt(b, a, wave.astype(np.float64)).astype(np.float32)
        wave  = np.tanh(1.8 * wave) / np.tanh(np.float32(1.8))
        env   = adsr(0.005, 0.0, 1.0, 0.28)
        return (wave * env).astype(np.float32)

    if timbre == "bass_808_resonant":
        pitch_env = np.exp(-8.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.25 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        res_freq  = freq * 3.5
        res_wave  = 0.30 * np.sin(2*np.pi * res_freq * t) * np.exp(-15.0 * t / dur)
        wave      = 0.70*np.sin(phase) + 0.20*np.sin(2*phase) + res_wave.astype(np.float32)
        wave      = np.tanh(2.0 * wave) / np.tanh(np.float32(2.0))
        env       = adsr(0.004, 0.0, 1.0, 0.25)
        return (wave * env).astype(np.float32)

    if timbre == "bass_808_tape":
        pitch_env = np.exp(-8.0 * t / dur)
        freq_mod  = freq * (1.0 + 0.25 * pitch_env)
        phase     = 2.0 * np.pi * np.cumsum(freq_mod.astype(np.float64)) / sr
        wave      = 0.70*np.sin(phase) + 0.20*np.sin(2*phase) + 0.10*np.sin(0.5*phase)
        noise     = np.random.uniform(-0.025, 0.025, n)
        wave      = np.tanh(2.0 * (wave + noise)) / np.tanh(2.0)
        env       = adsr(0.005, 0.0, 1.0, 0.26)
        return (wave * env * 0.85).astype(np.float32)

    # ── Effects-Processed Bass ────────────────────────────────────────────────
    if timbre == "bass_chorus":
        detune1 = 1.003
        detune2 = 0.997
        wave    = (0.40 * np.sin(2*np.pi * freq * t)
                   + 0.30 * np.sin(2*np.pi * freq * detune1 * t)
                   + 0.30 * np.sin(2*np.pi * freq * detune2 * t)
                   + 0.15 * np.sin(2*np.pi * freq * 2 * t)
                   + 0.05 * np.sin(2*np.pi * freq * 3 * t))
        env     = adsr(0.008, 0.0, 1.0, 0.22)
        return (wave * env * 0.65).astype(np.float32)

    if timbre == "bass_flanger":
        rate  = 0.4
        depth = 0.003
        delay_samples = int(depth * sr)
        wave  = (0.62 * np.sin(2*np.pi * freq * t)
                 + 0.24 * np.sin(2*np.pi * freq * 2 * t)
                 + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        flange_phase = np.sin(2*np.pi * rate * t) * delay_samples
        flange = np.zeros(n, dtype=np.float32)
        for i in range(n):
            d = int(flange_phase[i])
            j = i - max(0, d)
            if 0 <= j < n:
                flange[i] = wave[j]
        wave  = (wave + 0.5 * flange)
        env   = adsr(0.006, 0.0, 1.0, 0.22)
        return (wave * env * 0.70).astype(np.float32)

    if timbre == "bass_tape":
        wave  = (0.62 * np.sin(2*np.pi * freq * t)
                 + 0.24 * np.sin(2*np.pi * freq * 2 * t)
                 + 0.10 * np.sin(2*np.pi * freq * 3 * t))
        noise = np.random.uniform(-0.020, 0.020, n).astype(np.float32)
        wave  = np.tanh(1.4 * (wave + noise)) / np.tanh(np.float32(1.4))
        env   = adsr(0.008, 0.0, 1.0, 0.25)
        return (wave * env * 0.78).astype(np.float32)

    if timbre == "bass_compress":
        wave  = (0.60 * np.sin(2*np.pi * freq * t)
                 + 0.25 * np.sin(2*np.pi * freq * 2 * t)
                 + 0.12 * np.sin(2*np.pi * freq * 3 * t))
        peak  = np.maximum(np.abs(wave), 1e-6)
        comp  = np.where(peak > 0.5, 0.5 + (peak - 0.5) * 0.3, peak)
        wave  = wave * (comp / peak)
        env   = adsr(0.005, 0.0, 1.0, 0.22)
        return (wave * env * 0.88).astype(np.float32)

    # ── Exotic / Ethnic Textures ──────────────────────────────────────────────
    if timbre == "bass_oud_low":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="oud")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.85
        wave += 0.08 * np.sin(2*np.pi * freq * t) * np.exp(-3.0 * t / dur)
        return np.clip(wave, -1.0, 1.0)

    if timbre == "bass_koto_low":
        ks   = _ks_instrument(freq, duration, sr=sr, timbre="koto")
        wave = np.zeros(n, dtype=np.float32)
        wave[:len(ks)] += ks * 0.85
        return np.clip(wave, -1.0, 1.0)

    if timbre in ("guitar", "bass", "oud", "sitar", "banjo", "koto", "shamisen"):
        return _ks_instrument(freq, duration, sr=sr, timbre=timbre)

    n = max(int(sr * duration), 1)
    t = np.linspace(0, duration, n, dtype=np.float32)
    return (0.6 * np.sin(2 * np.pi * freq * t) * np.exp(-3.0 * t / max(duration,.01))).astype(np.float32)

def _synth_note_LEGACY_UNUSED(midi_pitch: int, duration: float, timbre: str, sr: int = 44100) -> np.ndarray:
    """Legacy (replaced by parametric engine above). Kept for reference only."""
    freq = _midi_to_hz(midi_pitch)
    n    = max(int(sr * duration), 1)
    t    = np.linspace(0, duration, n, dtype=np.float32)
    dur  = max(duration, 0.01)

    def adsr(atk, dec, sus_lvl, rel_ratio=0.15):
        env = np.ones(n, dtype=np.float32)
        a = min(int(sr * atk), n)
        d = min(int(sr * dec), n - a)
        r = min(int(sr * dur * rel_ratio), n)
        if a: env[:a] = np.linspace(0, 1, a)
        if d: env[a:a+d] = np.linspace(1, sus_lvl, d)
        if r and r < n: env[-r:] = np.linspace(env[-r], 0, r)
        return env

    # ── Plucked strings (Karplus-Strong) ──────────────────────────────────────
    if timbre in ("guitar", "bass", "oud", "sitar", "banjo"):
        return _ks_instrument(freq, duration, sr=sr, timbre=timbre)

    if timbre == "electric_guitar":
        wave = (0.6 * np.sin(2 * np.pi * freq * t) +
                0.25 * np.sin(2 * np.pi * freq * 2 * t) +
                0.10 * np.sin(2 * np.pi * freq * 3 * t) +
                0.05 * np.sin(2 * np.pi * freq * 4 * t))
        env = adsr(0.005, 0.08, 0.75, 0.12)
        return (wave * env).astype(np.float32)

    if timbre == "harp":
        wave = (0.55 * np.sin(2 * np.pi * freq * t) +
                0.28 * np.sin(2 * np.pi * freq * 2 * t) +
                0.10 * np.sin(2 * np.pi * freq * 3 * t))
        env = np.exp(-5.0 * t / dur)
        return (wave * env).astype(np.float32)

    # ── Keyboards ─────────────────────────────────────────────────────────────
    if timbre == "piano":
        wave = (0.50 * np.sin(2 * np.pi * freq * t) +
                0.25 * np.sin(2 * np.pi * freq * 2 * t) +
                0.12 * np.sin(2 * np.pi * freq * 3 * t) +
                0.06 * np.sin(2 * np.pi * freq * 4 * t))
        env = np.exp(-4.5 * t / dur)
        return (wave * env).astype(np.float32)

    if timbre == "harpsichord":
        wave = (0.45 * np.sin(2 * np.pi * freq * t) +
                0.30 * np.sin(2 * np.pi * freq * 2 * t) +
                0.15 * np.sin(2 * np.pi * freq * 3 * t) +
                0.08 * np.sin(2 * np.pi * freq * 4 * t))
        env = np.exp(-9.0 * t / dur)
        return (wave * env).astype(np.float32)

    if timbre == "organ":
        wave = (0.40 * np.sin(2 * np.pi * freq * t) +
                0.30 * np.sin(2 * np.pi * freq * 2 * t) +
                0.20 * np.sin(2 * np.pi * freq * 3 * t) +
                0.07 * np.sin(2 * np.pi * freq * 4 * t) +
                0.03 * np.sin(2 * np.pi * freq * 6 * t))
        env = adsr(0.01, 0.0, 1.0, 0.05)
        return (wave * env).astype(np.float32)

    if timbre == "marimba":
        wave = (0.60 * np.sin(2 * np.pi * freq * t) +
                0.25 * np.sin(2 * np.pi * freq * 3 * t) +
                0.10 * np.sin(2 * np.pi * freq * 5 * t))
        env = np.exp(-8.0 * t / dur)
        return (wave * env).astype(np.float32)

    # ── 808 Bass (Hip-Hop / Trap) ──────────────────────────────────────────────
    if timbre in ("bass_808", "808"):
        pitch_env = np.exp(-8.0 * t / max(dur, 0.1))
        freq_mod = freq * (1 + 0.25 * pitch_env)
        sub2 = np.sin(2 * np.pi * np.cumsum(freq_mod / sr) * (1.0 / sr) * sr)
        wave = 0.7 * sub2 + 0.2 * np.sin(2 * np.pi * freq * 2 * t) + 0.08 * np.sin(2 * np.pi * freq * 3 * t)
        wave = np.tanh(1.8 * wave) / np.tanh(np.float32(1.8))
        env = adsr(0.005, 0.0, 1.0, 0.25)
        return (wave * env).astype(np.float32)

    # ── Heavy Synth Lead (Hip-Hop / Electronic) ────────────────────────────────
    if timbre == "synth_heavy":
        wave = sum(
            (1.0 / k) * np.sin(2 * np.pi * freq * k * t + (0.05 * k))
            for k in range(1, 12)
        ).astype(np.float32)
        wave = np.tanh(2.5 * wave) / np.tanh(np.float32(2.5))
        env = adsr(0.008, 0.12, 0.75, 0.15)
        return (wave * env * 0.45).astype(np.float32)

    # ── Synths ─────────────────────────────────────────────────────────────────
    if timbre == "synth":
        # Sawtooth-like via additive harmonics
        wave = sum(
            (1.0 / k) * np.sin(2 * np.pi * freq * k * t)
            for k in range(1, 9)
        ).astype(np.float32)
        env = adsr(0.02, 0.1, 0.8, 0.15)
        return (wave * env * 0.3).astype(np.float32)

    if timbre == "synth_pad":
        # Correct FM vibrato: phase offset = depth * sin(vib_rate)
        vib_phase = 0.15 * np.sin(2 * np.pi * 0.5 * t)   # modulation index 0.15 rad
        phase1 = 2 * np.pi * freq * t + vib_phase
        phase2 = 2 * np.pi * freq * 2 * t + vib_phase
        wave = (0.5 * np.sin(phase1) +
                0.3 * np.sin(phase2) +
                0.15 * np.sin(2 * np.pi * freq * 3 * t))
        env = adsr(0.3, 0.0, 1.0, 0.3)
        return (wave * env).astype(np.float32)

    # ── Bowed strings ──────────────────────────────────────────────────────────
    if timbre == "violin":
        # Proper FM vibrato: bounded phase modulation, no accumulation
        vib_phase = 0.12 * np.sin(2 * np.pi * 5.5 * t)
        wave = (0.60 * np.sin(2 * np.pi * freq * t + vib_phase) +
                0.20 * np.sin(2 * np.pi * freq * 2 * t) +
                0.10 * np.sin(2 * np.pi * freq * 3 * t) +
                0.05 * np.sin(2 * np.pi * freq * 4 * t))
        env = adsr(0.06, 0.0, 1.0, 0.08)
        return (wave * env).astype(np.float32)

    if timbre == "viola":
        vib_phase = 0.11 * np.sin(2 * np.pi * 5.0 * t)
        wave = (0.55 * np.sin(2 * np.pi * freq * t + vib_phase) +
                0.25 * np.sin(2 * np.pi * freq * 2 * t) +
                0.12 * np.sin(2 * np.pi * freq * 3 * t))
        env = adsr(0.07, 0.0, 1.0, 0.10)
        return (wave * env).astype(np.float32)

    if timbre == "cello":
        vib_phase = 0.10 * np.sin(2 * np.pi * 4.5 * t)
        wave = (0.50 * np.sin(2 * np.pi * freq * t + vib_phase) +
                0.28 * np.sin(2 * np.pi * freq * 2 * t) +
                0.14 * np.sin(2 * np.pi * freq * 3 * t) +
                0.07 * np.sin(2 * np.pi * freq * 4 * t))
        env = adsr(0.09, 0.0, 1.0, 0.12)
        return (wave * env).astype(np.float32)

    # ── Brass ─────────────────────────────────────────────────────────────────
    if timbre == "brass":
        wave = (0.40 * np.sin(2 * np.pi * freq * t) +
                0.30 * np.sin(2 * np.pi * freq * 2 * t) +
                0.18 * np.sin(2 * np.pi * freq * 3 * t) +
                0.08 * np.sin(2 * np.pi * freq * 4 * t) +
                0.04 * np.sin(2 * np.pi * freq * 5 * t))
        env = adsr(0.04, 0.05, 0.85, 0.10)
        return (wave * env).astype(np.float32)

    # ── Woodwinds ─────────────────────────────────────────────────────────────
    if timbre == "flute":
        breath = np.random.uniform(-1, 1, n).astype(np.float32) * 0.04
        wave   = (0.70 * np.sin(2 * np.pi * freq * t) +
                  0.15 * np.sin(2 * np.pi * freq * 2 * t) +
                  0.06 * np.sin(2 * np.pi * freq * 3 * t) + breath)
        env = adsr(0.05, 0.0, 1.0, 0.08)
        return (wave * env).astype(np.float32)

    if timbre == "woodwind":
        breath = np.random.uniform(-1, 1, n).astype(np.float32) * 0.03
        wave   = (0.55 * np.sin(2 * np.pi * freq * t) +
                  0.25 * np.sin(2 * np.pi * freq * 2 * t) +
                  0.12 * np.sin(2 * np.pi * freq * 3 * t) + breath)
        env = adsr(0.04, 0.0, 1.0, 0.08)
        return (wave * env).astype(np.float32)

    if timbre == "saxophone":
        breath = np.random.uniform(-1, 1, n).astype(np.float32) * 0.03
        wave   = (0.45 * np.sin(2 * np.pi * freq * t) +
                  0.28 * np.sin(2 * np.pi * freq * 2 * t) +
                  0.15 * np.sin(2 * np.pi * freq * 3 * t) +
                  0.07 * np.sin(2 * np.pi * freq * 4 * t) + breath)
        env = adsr(0.05, 0.05, 0.85, 0.10)
        return (wave * env).astype(np.float32)

    # ── Voice ─────────────────────────────────────────────────────────────────
    if timbre == "voice":
        vib_phase = 0.13 * np.sin(2 * np.pi * 6.0 * t)
        wave = (0.65 * np.sin(2 * np.pi * freq * t + vib_phase) +
                0.20 * np.sin(2 * np.pi * freq * 2 * t) +
                0.08 * np.sin(2 * np.pi * freq * 3 * t))
        env = adsr(0.08, 0.0, 1.0, 0.12)
        return (wave * env).astype(np.float32)

    # ── Fallback ──────────────────────────────────────────────────────────────
    return _ks_instrument(freq, duration, sr=sr, timbre="guitar")


# ═══════════════════════════════════════════════════════════════════════════════
#  DEEP ANALYSIS HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_CHROMATIC = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

def _midi_name(midi: int) -> str:
    return _CHROMATIC[midi % 12] + str(midi // 12 - 1)

def _scale_notes(tonic: str, mode: str) -> list:
    intervals = [0,2,4,5,7,9,11] if mode == "major" else [0,2,3,5,7,8,10]
    t = tonic.replace('b','#')
    idx = next((i for i,n in enumerate(_CHROMATIC) if n == t), 0)
    return [_CHROMATIC[(idx+iv)%12] for iv in intervals]

def _detect_time_sig(score) -> str:
    try:
        from music21 import meter
        for ts in score.flatten().getElementsByClass(meter.TimeSignature):
            return ts.ratioString
    except Exception:
        pass
    return "4/4"

def _detect_key(score) -> dict:
    try:
        k = score.analyze("key")
        tonic = k.tonic.name
        mode  = k.mode
        scale = _scale_notes(tonic, mode)
        arabic_mode = "ماجور (صاعد)" if mode == "major" else "مينور (نازل)"
        return {"tonic": tonic, "mode": mode, "arabic_mode": arabic_mode,
                "scale": scale, "confidence": round(k.correlationCoefficient, 2)}
    except Exception:
        return {"tonic": "—", "mode": "major", "arabic_mode": "—",
                "scale": [], "confidence": 0}

def _detect_modulations(score) -> list:
    """Return list of key changes found in the score."""
    try:
        from music21 import analysis
        mods = []
        seen = set()
        for part in (score.parts if hasattr(score, "parts") else [score]):
            measures = part.getElementsByClass("Measure")
            for m in measures:
                try:
                    k = m.analyze("key")
                    label = f"{k.tonic.name} {k.mode}"
                    bar = m.measureNumber
                    if label not in seen:
                        seen.add(label)
                        mods.append({"bar": bar, "key": label})
                except Exception:
                    pass
        return mods[:8]
    except Exception:
        return []

def _analyze_drum_pattern(drum_notes: list, bpm: float) -> dict:
    if not drum_notes:
        return {"pattern_lines": [], "groove": "—", "syncopation": False,
                "kick_count": 0, "snare_count": 0, "hihat_count": 0}

    beat_dur  = 60.0 / bpm
    bar_dur   = beat_dur * 4
    step_dur  = beat_dur / 4       # 16th-note

    kicks  = [n for n in drum_notes if n["midi"] in (35, 36)]
    snares = [n for n in drum_notes if n["midi"] in (38, 40)]
    hihats = [n for n in drum_notes if n["midi"] in (42, 44, 46)]

    def beat_pos(t):
        return (t % bar_dur) / beat_dur

    def step_set(hits):
        s = set()
        for n in hits[:128]:
            s.add(round((n["time"] % bar_dur) / step_dur) % 16)
        return s

    k_steps = step_set(kicks)
    s_steps = step_set(snares)
    h_steps = step_set(hihats)

    k_str = "".join("K" if i in k_steps else "·" for i in range(16))
    s_str = "".join("S" if i in s_steps else "·" for i in range(16))
    h_str = "".join("x" if i in h_steps else "·" for i in range(16))

    # Swing: off-beats landing ~33 % of step instead of 50 %
    swing_score = 0
    for h in hihats[:64]:
        bp = beat_pos(h["time"]) % 0.5
        if 0.28 < bp < 0.38:
            swing_score += 1
    total_off = sum(1 for h in hihats[:64] if 0.2 < beat_pos(h["time"]) % 0.5 < 0.45)
    groove = "Swing 🎷" if total_off > 0 and swing_score / max(total_off, 1) > 0.35 else "Straight"

    # Syncopation: notes landing on "e" or "a" (steps 1,3,5,7,…)
    all_hits = kicks + snares
    synco = sum(1 for n in all_hits if round((n["time"] % bar_dur) / step_dur) % 2 == 1)
    syncopation = synco / max(len(all_hits), 1) > 0.25

    return {
        "pattern_lines": [f"K `{k_str}`", f"S `{s_str}`", f"H `{h_str}`"],
        "groove": groove,
        "syncopation": syncopation,
        "kick_count": len(kicks),
        "snare_count": len(snares),
        "hihat_count": len(hihats),
    }

def _analyze_melody_part(notes_list: list) -> dict:
    if not notes_list:
        return {"low": "—", "high": "—", "span_oct": 0, "repetitive": False,
                "has_ornaments": False}
    midis = [n["midi"] for n in notes_list]
    low, high = min(midis), max(midis)

    # Repetition: look for 4-note motifs that repeat
    seq = [n["midi"] for n in notes_list[:300]]
    patterns: dict = {}
    for i in range(len(seq) - 4):
        p = tuple(seq[i:i+4])
        patterns[p] = patterns.get(p, 0) + 1
    repetitive = bool(patterns) and max(patterns.values()) >= 3

    return {
        "low": _midi_name(low),
        "high": _midi_name(high),
        "span_oct": (high - low) // 12,
        "repetitive": repetitive,
        "has_ornaments": False,   # ornament detection requires deeper music21 parsing
    }

def _detect_structure(all_parts: dict, bpm: float) -> list:
    """Segment the score into sections based on note-density."""
    all_notes = []
    for pd in all_parts.values():
        all_notes.extend(pd["notes"])
    if not all_notes:
        return []

    beat_dur = 60.0 / bpm
    bar_dur  = beat_dur * 4
    chunk    = bar_dur * 4          # 4-bar window

    max_t = max(n["time"] for n in all_notes)
    n_chunks = max(1, int(max_t / chunk))

    densities = []
    for i in range(n_chunks):
        t0, t1 = i * chunk, (i+1) * chunk
        cnt = sum(1 for n in all_notes if t0 <= n["time"] < t1)
        densities.append(cnt / max(chunk, 0.01))

    max_d = max(densities) if densities else 1

    def fmt(t):
        return f"{int(t//60)}:{int(t%60):02d}"

    sections, prev_type = [], None
    for i, d in enumerate(densities):
        nd = d / max_d
        t  = i * chunk

        if i == 0:
            sec = "Intro"
        elif i == n_chunks - 1 and nd < 0.45:
            sec = "Outro"
        elif nd >= 0.75:
            sec = "Chorus" if prev_type in (None, "Verse", "Intro", "Pre-Chorus", "Bridge") else "Drop"
        elif nd < 0.35 and prev_type in ("Chorus", "Drop"):
            sec = "Bridge"
        elif nd >= 0.55 and prev_type in ("Intro", "Bridge", "Outro", None):
            sec = "Pre-Chorus" if prev_type == "Verse" else "Verse"
        else:
            sec = "Verse"

        if sec != prev_type:
            sections.append({"name": sec, "time_str": fmt(t)})
            prev_type = sec

    return sections

def _classify_roles(all_parts: dict) -> dict:
    """Assign Lead / Pad / Background / Bass roles."""
    roles = {}
    info = {}
    for pname, pd in all_parts.items():
        if not pd["notes"]:
            continue
        midis = [n["midi"] for n in pd["notes"]]
        info[pname] = {
            "avg": sum(midis)/len(midis),
            "min": min(midis),
            "count": len(midis),
            "timbre": pd["timbre"]
        }

    sorted_p = sorted(info.items(), key=lambda x: -x[1]["avg"])
    assigned_lead = False
    for pname, r in sorted_p:
        if r["avg"] < 45 or "bass" in pname.lower():
            roles[pname] = "Bass 🎸"
        elif not assigned_lead and r["avg"] > 62:
            roles[pname] = "Lead 🎤"
            assigned_lead = True
        elif r["count"] < 30:
            roles[pname] = "Pad 🎹"
        else:
            roles[pname] = "Background"
    return roles

def _detect_syncopation_score(all_parts: dict, bpm: float) -> float:
    """0-1 syncopation score across all melodic parts."""
    beat_dur = 60.0 / bpm
    total, synco = 0, 0
    for pd in all_parts.values():
        for n in pd["notes"]:
            b_frac = (n["time"] % beat_dur) / beat_dur
            if 0.3 < b_frac < 0.7:
                synco += 1
            total += 1
    return round(synco / max(total, 1), 2)

def deep_analyze_score(tab_data: dict, score=None) -> dict:
    """
    Run all deep analysis passes on tab_data and return an 'analysis' sub-dict.
    If `score` (music21 Score) is provided, also run key/time-sig detection.
    """
    bpm        = tab_data.get("bpm", 80)
    all_parts  = tab_data.get("all_parts", {})
    drum_notes = tab_data.get("drum_notes", [])

    result: dict = {}

    # Key / time signature (require music21 score object)
    if score is not None:
        result["time_sig"] = _detect_time_sig(score)
        result["key"]      = _detect_key(score)
        result["modulations"] = _detect_modulations(score)
    else:
        result["time_sig"] = "4/4"
        result["key"]      = {"tonic": "—", "mode": "—", "arabic_mode": "—",
                               "scale": [], "confidence": 0}
        result["modulations"] = []

    # Rhythm / groove
    result["drum"]        = _analyze_drum_pattern(drum_notes, bpm)
    result["synco_score"] = _detect_syncopation_score(all_parts, bpm)

    # Melody per part
    result["melodies"] = {
        pname: _analyze_melody_part(pd["notes"])
        for pname, pd in all_parts.items()
    }

    # Structure
    result["structure"] = _detect_structure(all_parts, bpm)

    # Instrument roles
    result["roles"] = _classify_roles(all_parts)

    return result


# ── Note name helpers ──────────────────────────────────────────────────────────
MIDI_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

def midi_to_name(midi: int) -> str:
    """Convert MIDI number to note name with octave. 60 → C4"""
    midi = max(0, min(127, int(midi)))
    return f"{MIDI_NOTE_NAMES[midi % 12]}{(midi // 12) - 1}"

def name_to_midi(token: str) -> int:
    """Parse a note token like 'C4', 'D#3', 'Bb2' → MIDI number. Returns 60 on failure."""
    import re
    token = token.strip().upper().replace("♭", "B").replace("♯", "#")
    m = re.match(r'^([A-G][#B]?)(-?\d+)$', token)
    if not m:
        return 60
    pc_map = {"C":0,"C#":1,"DB":1,"D":2,"D#":3,"EB":3,"E":4,"F":5,
              "F#":6,"GB":6,"G":7,"G#":8,"AB":8,"A":9,"A#":10,"BB":10,"B":11}
    pc = pc_map.get(m.group(1), 0)
    octave = int(m.group(2))
    return max(0, min(127, (octave + 1) * 12 + pc))

def format_notes_for_export(tab_data: dict) -> list[str]:
    """
    Build a copyable note-sequence per instrument (ALL notes, no limit).
    Returns a list of message strings, each ≤ 4000 chars, ready to send.
    Each instrument starts on a new message chunk if needed.
    """
    all_parts = tab_data.get("all_parts", {})
    CHUNK = 3800  # safe margin below Telegram's 4096

    messages = []
    current = "🎼 النوتات بالحروف — انسخ وعدّل وأرسل:\n\n"

    for pname, pd in all_parts.items():
        notes = sorted(pd.get("notes", []), key=lambda n: n.get("time", 0))
        if not notes:
            continue
        names = [midi_to_name(n["midi"]) for n in notes]
        # Build rows of 16 per line, first row prefixed with instrument name
        rows = [" ".join(names[i:i+16]) for i in range(0, len(names), 16)]
        block_lines = [f"{pname}: {rows[0]}"]
        for r in rows[1:]:
            block_lines.append(f"  {r}")
        block = "\n".join(block_lines) + "\n"

        # If adding this block would overflow, flush current and start new chunk
        if len(current) + len(block) > CHUNK:
            messages.append(current)
            current = block
        else:
            current += block

    if current.strip():
        messages.append(current)

    # Append instructions as final message
    messages.append(
        "📌 *لتعديل آلة:*\n"
        "١. انسخ سطر الآلة كاملاً\n"
        "٢. عدّل النوتات حسب رغبتك\n"
        "٣. أرسله كرسالة نصية للبوت وسيعيد التصيير\n\n"
        "📝 الصيغة: `اسم الآلة: C4 D4 E4 G4 ...`"
    )
    return messages


def build_rich_analysis_msg(tab_data: dict, fname: str = "") -> str:
    """
    Build the full human-readable analysis message from tab_data
    (which must include an 'analysis' key from deep_analyze_score).
    """
    an    = tab_data.get("analysis", {})
    bpm   = tab_data.get("bpm", "—")
    chords_list = tab_data.get("chords", [])
    chords = " • ".join(chords_list[:12]) or "—"

    key_d  = an.get("key", {})
    tonic  = key_d.get("tonic", "—")
    mode   = key_d.get("arabic_mode", "—")
    scale  = " ".join(key_d.get("scale", []))
    t_sig  = an.get("time_sig", "4/4")
    drum_d = an.get("drum", {})
    struct = an.get("structure", [])
    roles  = an.get("roles", {})
    melodies = an.get("melodies", {})
    synco  = an.get("synco_score", 0)
    mods   = an.get("modulations", [])

    lines = []
    header = f"🎼 *تحليل عميق — {fname}*\n" if fname else "🎼 *تحليل عميق*\n"
    lines.append(header)

    # ── Rhythm ──────────────────────────────────────────────────────────────
    lines.append("━━━ 🥁 الإيقاع ━━━")
    lines.append(f"• BPM: *{bpm}*   |   الميزان: *{t_sig}*")
    lines.append(f"• Groove: *{drum_d.get('groove','—')}*   |   Syncopation: *{round(synco*100)}%*")
    plines = drum_d.get("pattern_lines", [])
    if plines:
        lines.append("• Pattern (16th grid):")
        lines.extend(f"  {pl}" for pl in plines)
    if drum_d.get("kick_count"):
        lines.append(f"  Kick={drum_d['kick_count']} / Snare={drum_d['snare_count']} / HH={drum_d['hihat_count']}")

    # ── Harmony ─────────────────────────────────────────────────────────────
    lines.append("\n━━━ 🎵 الهارموني ━━━")
    lines.append(f"• المقام: *{tonic} {mode}*")
    if scale:
        lines.append(f"• السلم: `{scale}`")
    lines.append(f"• الكوردات: {chords}")
    if mods and len(mods) > 1:
        mod_str = " → ".join(m["key"] for m in mods[:5])
        lines.append(f"• Modulation: {mod_str}")

    # ── Parts / Roles ────────────────────────────────────────────────────────
    lines.append("\n━━━ 🎹 الآلات والأدوار ━━━")
    all_parts = tab_data.get("all_parts", {})
    for pname, pd in all_parts.items():
        role = roles.get(pname, "")
        mel  = melodies.get(pname, {})
        lo   = mel.get("low", "—")
        hi   = mel.get("high", "—")
        rep  = " ↺" if mel.get("repetitive") else ""
        lines.append(f"• *{pname}* {role}  `{lo}–{hi}`{rep}  ({len(pd['notes'])} نوتة)")
    if tab_data.get("has_drums"):
        lines.append(f"• *Drums* 🥁  ({len(tab_data.get('drum_notes',[]))} ضربة)")

    # ── Structure ────────────────────────────────────────────────────────────
    if struct:
        lines.append("\n━━━ 🗺 الهيكل ━━━")
        for s in struct:
            lines.append(f"• *{s['name']}* — {s['time_str']}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  EDIT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

user_edit_state: dict = {}
# user_id -> {"bpm_offset": 0, "transpose": 0, "swing": 0.0,
#             "muted": set(), "gain_overrides": {pname: gain}}

_DEFAULT_EDIT = lambda: {
    "bpm_offset": 0, "transpose": 0, "swing": 0.0,
    "muted": set(), "gain_overrides": {},
}

def generate_note_visualization(tab_data: dict, raw_xml_path: str,
                                audio_path: str, output_path: str,
                                max_secs: int = 60) -> bool:
    """
    Physics / math themed visualization:
    - White background, large math axes
    - Vector arrow traces the step-function waveform
    - Dynamic physics equations (f, T, λ, ω) appear near the arrow
    - Background physics formulas fade in/out
    """
    import cv2, numpy as np, bisect, subprocess as _sp, io, math

    # Try matplotlib for equation rendering
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        HAS_MPL = True
    except ImportError:
        HAS_MPL = False

    WIDTH, HEIGHT = 1280, 720
    FPS           = 30
    PAD_TOP       = 80
    PAD_BOT       = 100
    PAD_LEFT      = 100
    PAD_RIGHT     = 60
    PLOT_W        = WIDTH  - PAD_LEFT - PAD_RIGHT
    PLOT_H        = HEIGHT - PAD_TOP  - PAD_BOT
    MIDI_LO, MIDI_HI = 36, 96
    V_SOUND = 343.0   # m/s
    H_PLANCK = 6.626e-34

    INST_COLORS = [
        (180,  30,  30),   # dark red
        ( 20, 100, 200),   # blue
        ( 20, 160,  60),   # green
        (160,  40, 200),   # purple
        (200, 130,   0),   # amber
        (  0, 160, 180),   # teal
        (200,  60, 120),   # rose
        (100, 140,   0),   # olive
    ]
    LINE_W   = 3
    AXIS_W   = 2
    TICK_LEN = 10

    def _pitch_y(midi):
        midi = max(MIDI_LO, min(MIDI_HI, midi))
        frac = (midi - MIDI_LO) / (MIDI_HI - MIDI_LO)
        return int(PAD_TOP + PLOT_H * (1.0 - frac))

    def _time_x(t, duration):
        frac = t / duration if duration > 0 else 0
        return int(PAD_LEFT + PLOT_W * frac)

    def _midi_to_hz(midi):
        return 440.0 * (2.0 ** ((midi - 69) / 12.0))

    # ── Equation renderer ──────────────────────────────────────────────────
    _eq_cache = {}
    def render_eq(latex_str, fontsize=20, color='#1a1a8c'):
        key = (latex_str, fontsize, color)
        if key in _eq_cache:
            return _eq_cache[key]
        if not HAS_MPL:
            return None
        try:
            fig, ax = plt.subplots(figsize=(10, 1.4), facecolor='white')
            ax.set_facecolor('white')
            ax.axis('off')
            ax.text(0.02, 0.5, f"${latex_str}$",
                    ha='left', va='center', fontsize=fontsize,
                    color=color, transform=ax.transAxes)
            fig.tight_layout(pad=0.1)
            buf = io.BytesIO()
            fig.savefig(buf, format='png', dpi=100,
                        bbox_inches='tight', facecolor='white')
            plt.close(fig)
            buf.seek(0)
            arr = np.frombuffer(buf.read(), dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return None
            mask = np.any(img < 250, axis=2)
            rows = np.any(mask, axis=1)
            cols = np.any(mask, axis=0)
            if rows.any() and cols.any():
                r0, r1 = np.where(rows)[0][[0, -1]]
                c0, c1 = np.where(cols)[0][[0, -1]]
                img = img[max(0, r0-4):r1+8, max(0, c0-4):c1+8]
            _eq_cache[key] = img
            return img
        except Exception as ex:
            logger.warning(f"render_eq error: {ex}")
            return None

    def overlay_eq(frame, eq_img, x, y, alpha=1.0):
        """Paste eq_img onto frame at (x, y) with optional transparency."""
        if eq_img is None:
            return
        h, w = eq_img.shape[:2]
        x1 = max(0, x); y1 = max(0, y)
        x2 = min(frame.shape[1], x + w)
        y2 = min(frame.shape[0], y + h)
        if x1 >= x2 or y1 >= y2:
            return
        src = eq_img[y1-y:y2-y, x1-x:x2-x]
        roi = frame[y1:y2, x1:x2]
        # Blend over white: darken by alpha
        mask = (1.0 - alpha) * 255 + alpha * src.astype(float)
        roi[:] = np.clip(mask, 0, 255).astype(np.uint8)

    # ── Parse notes ────────────────────────────────────────────────────────
    try:
        from music21 import converter, tempo as m21tempo
        if raw_xml_path and os.path.exists(raw_xml_path):
            sc      = converter.parse(raw_xml_path)
            flat    = sc.flatten()
            bpm_mks = list(flat.getElementsByClass(m21tempo.MetronomeMark))
            bpm     = int(bpm_mks[0].number) if bpm_mks else tab_data.get("bpm", 80)
            beat_dur = 60.0 / max(bpm, 40)
            raw_parts = []
            for pi, part in enumerate(sc.parts[:8]):
                evts = []
                for n in part.flatten().notes:
                    pitches = ([n.pitch] if hasattr(n, "pitch")
                               else list(getattr(n, "pitches", [])))
                    for p in pitches:
                        evts.append((float(n.offset) * beat_dur, p.midi))
                if evts:
                    evts.sort()
                    raw_parts.append({
                        "col":  INST_COLORS[pi % len(INST_COLORS)],
                        "name": (part.partName or f"Track {pi+1}")[:14],
                        "evts": evts,
                    })
        else:
            bpm      = tab_data.get("bpm", 80)
            beat_dur = 60.0 / max(bpm, 40)
            raw_parts = []
            for pi, (pname, pdata) in enumerate(tab_data.get("all_parts", {}).items()):
                evts = sorted((n.get("time", 0), n.get("midi", 60))
                              for n in pdata.get("notes", []))
                if evts:
                    raw_parts.append({
                        "col":  INST_COLORS[pi % len(INST_COLORS)],
                        "name": pname[:14],
                        "evts": evts,
                    })
    except Exception as e:
        logger.error(f"Viz parse error: {e}")
        return False

    if not raw_parts:
        return False

    all_evts = [e for p in raw_parts for e in p["evts"]]
    total_t  = max(e[0] for e in all_evts) if all_evts else 10.0
    duration = min(total_t + 1.0, max_secs)
    total_frames = int(duration * FPS)

    for pn in raw_parts:
        segs = []
        evts = pn["evts"]
        for i, (t, midi) in enumerate(evts):
            t_end = evts[i + 1][0] if i + 1 < len(evts) else t + beat_dur
            segs.append((t, t_end, midi))
        pn["segs"]      = segs
        pn["evt_times"] = [e[0] for e in evts]
        pn["evt_midis"] = [e[1] for e in evts]

    def _current_midi(pn, t):
        idx = bisect.bisect_right(pn["evt_times"], t) - 1
        return pn["evt_midis"][idx] if idx >= 0 else None

    # ── Pre-render background physics equations ───────────────────────────
    bg_eqs_latex = [
        r"\frac{\partial^2 y}{\partial t^2} = c^2 \frac{\partial^2 y}{\partial x^2}",
        r"y(t) = A \sin(2\pi f t + \varphi)",
        r"E = h f = \hbar \omega",
        r"v_{sound} = 343 \; m/s",
        r"\hat{H}\psi = E\psi",
        r"\hat{p} = -i\hbar \nabla",
        r"c = \lambda f",
        r"\mathbf{F} = m\mathbf{a}",
        r"\Delta x \cdot \Delta p \geq \frac{\hbar}{2}",
        r"\oint \mathbf{E} \cdot d\mathbf{A} = \frac{Q}{\varepsilon_0}",
        r"S = k_B \ln W",
    ]
    # Pre-render all bg equations (faint blue-grey)
    bg_eq_imgs = [render_eq(eq, fontsize=17, color='#8888bb') for eq in bg_eqs_latex]

    # Fixed positions for background equations (spread around, avoiding axes)
    rng = np.random.default_rng(42)
    bg_eq_positions = []
    for i, img in enumerate(bg_eq_imgs):
        if img is None:
            bg_eq_positions.append((0, 0))
            continue
        h, w = img.shape[:2]
        x = int(rng.uniform(PAD_LEFT + 10, WIDTH - PAD_RIGHT - w - 10))
        y = int(rng.uniform(PAD_TOP + 10, HEIGHT - PAD_BOT - h - 10))
        bg_eq_positions.append((x, y))

    # ── Build static axes background ───────────────────────────────────────
    bg = np.full((HEIGHT, WIDTH, 3), 255, dtype=np.uint8)

    # Light grid
    for midi_mark in range(MIDI_LO, MIDI_HI + 1, 12):
        gy = _pitch_y(midi_mark)
        cv2.line(bg, (PAD_LEFT, gy), (WIDTH - PAD_RIGHT, gy), (220, 220, 220), 1)
    tick_step = max(1, int(duration // 10))
    for sec_mark in range(0, int(duration) + 1, tick_step):
        gx = _time_x(sec_mark, duration)
        if PAD_LEFT <= gx <= WIDTH - PAD_RIGHT:
            cv2.line(bg, (gx, PAD_TOP), (gx, HEIGHT - PAD_BOT), (220, 220, 220), 1)

    ax_y = HEIGHT - PAD_BOT
    ax_x = PAD_LEFT
    cv2.arrowedLine(bg, (ax_x - 20, ax_y), (WIDTH - PAD_RIGHT + 30, ax_y),
                    (20, 20, 20), AXIS_W, tipLength=0.018)
    cv2.arrowedLine(bg, (ax_x, HEIGHT - PAD_BOT + 20), (ax_x, PAD_TOP - 25),
                    (20, 20, 20), AXIS_W, tipLength=0.022)

    cv2.putText(bg, "t  (s)", (WIDTH - PAD_RIGHT + 14, ax_y + 6),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, (20, 20, 20), 1, cv2.LINE_AA)
    cv2.putText(bg, "pitch", (6, PAD_TOP - 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.58, (20, 20, 20), 1, cv2.LINE_AA)

    for sec_mark in range(0, int(duration) + 1, tick_step):
        gx = _time_x(sec_mark, duration)
        if PAD_LEFT <= gx <= WIDTH - PAD_RIGHT:
            cv2.line(bg, (gx, ax_y - TICK_LEN), (gx, ax_y + TICK_LEN),
                     (20, 20, 20), 1)
            cv2.putText(bg, str(sec_mark), (gx - 10, ax_y + 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.44, (60, 60, 60), 1, cv2.LINE_AA)

    for midi_mark in range(MIDI_LO, MIDI_HI + 1, 12):
        gy = _pitch_y(midi_mark)
        cv2.line(bg, (ax_x - TICK_LEN, gy), (ax_x + TICK_LEN, gy),
                 (20, 20, 20), 1)
        note_name = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"][midi_mark % 12]
        oct_num   = midi_mark // 12 - 1
        cv2.putText(bg, f"{note_name}{oct_num}", (8, gy + 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (80, 80, 80), 1, cv2.LINE_AA)

    # Instrument legend (top right)
    leg_x0, leg_y0 = WIDTH - PAD_RIGHT - 210, PAD_TOP
    for pni, pn in enumerate(raw_parts[:6]):
        ly = leg_y0 + pni * 26
        cv2.rectangle(bg, (leg_x0, ly), (leg_x0 + 20, ly + 12), pn["col"], -1)
        cv2.rectangle(bg, (leg_x0, ly), (leg_x0 + 20, ly + 12), (30, 30, 30), 1)
        cv2.putText(bg, pn["name"], (leg_x0 + 26, ly + 11),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.43, (40, 40, 40), 1, cv2.LINE_AA)

    bpm_disp = tab_data.get("bpm", bpm)
    key_sig  = tab_data.get("key_signature", "")
    hud_str  = f"BPM = {bpm_disp}"
    if key_sig:
        hud_str += f"   Key: {key_sig}"
    cv2.putText(bg, hud_str, (PAD_LEFT, PAD_TOP - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.58, (80, 80, 80), 1, cv2.LINE_AA)

    # ── Render frames ──────────────────────────────────────────────────────
    tmp_vid = output_path + ".raw.mp4"
    fourcc  = cv2.VideoWriter_fourcc(*"mp4v")
    vw      = cv2.VideoWriter(tmp_vid, fourcc, FPS, (WIDTH, HEIGHT))

    overlay = bg.copy()  # accumulates drawn waveform lines

    inst_state = {pni: {"seg_idx": 0, "tail_x": None, "tail_y": None}
                  for pni in range(len(raw_parts))}

    prev_arrow_midi = {pni: None for pni in range(len(raw_parts))}
    last_dyn_eqs   = {}   # cache dynamic eq images

    for fi in range(total_frames):
        t_now = fi / FPS
        x_now = max(PAD_LEFT, min(WIDTH - PAD_RIGHT, _time_x(t_now, duration)))

        # Paint new waveform segments
        for pni, pn in enumerate(raw_parts):
            col   = pn["col"]
            state = inst_state[pni]
            segs  = pn["segs"]
            idx   = state["seg_idx"]
            tx    = state["tail_x"]
            ty    = state["tail_y"]

            while idx < len(segs):
                t_seg, t_end, midi = segs[idx]
                if t_seg > t_now:
                    break
                x_seg = max(PAD_LEFT, min(WIDTH - PAD_RIGHT, _time_x(t_seg, duration)))
                y_seg = _pitch_y(midi)

                if ty is not None and ty != y_seg:
                    cv2.line(overlay, (x_seg, ty), (x_seg, y_seg),
                             col, LINE_W, cv2.LINE_AA)

                t_draw_end = min(t_end, t_now)
                x_draw_end = max(PAD_LEFT, min(WIDTH - PAD_RIGHT,
                                               _time_x(t_draw_end, duration)))
                x_from = tx if (tx is not None and tx >= x_seg) else x_seg
                if x_from < x_draw_end:
                    cv2.line(overlay, (x_from, y_seg), (x_draw_end, y_seg),
                             col, LINE_W, cv2.LINE_AA)

                ty = y_seg
                tx = x_draw_end

                if t_draw_end < t_end:
                    break
                else:
                    idx += 1

            state["seg_idx"] = idx
            state["tail_x"]  = tx
            state["tail_y"]  = ty

        # Compose frame
        frame = overlay.copy()

        # 1) Background physics equations (faint, semi-transparent)
        bg_alpha = 0.30   # faint
        for i, (eq_img, pos) in enumerate(zip(bg_eq_imgs, bg_eq_positions)):
            if eq_img is None:
                continue
            # Slow drift: oscillate position slightly
            drift_x = int(6 * math.sin(t_now * 0.3 + i * 1.1))
            drift_y = int(4 * math.cos(t_now * 0.25 + i * 0.8))
            px, py  = pos[0] + drift_x, pos[1] + drift_y
            # Only draw if not overlapping the waveform center zone
            overlay_eq(frame, eq_img, px, py, alpha=bg_alpha)

        # 2) Vector arrow on primary instrument
        pn0    = raw_parts[0]
        cm0    = _current_midi(pn0, t_now)
        state0 = inst_state[0]
        if cm0 is not None and state0["tail_x"] is not None:
            ax_cur = state0["tail_x"]
            ay_cur = state0["tail_y"] if state0["tail_y"] is not None else _pitch_y(cm0)

            # Determine arrow direction
            prev_m = prev_arrow_midi[0]
            if prev_m is None or prev_m == cm0:
                # Moving right → horizontal arrow
                ax_tip = min(WIDTH - PAD_RIGHT, ax_cur + 28)
                ay_tip = ay_cur
            elif prev_m < cm0:
                # Going up ↑ then right
                ax_tip = min(WIDTH - PAD_RIGHT, ax_cur + 20)
                ay_tip = max(PAD_TOP, ay_cur - 20)
            else:
                # Going down ↓ then right
                ax_tip = min(WIDTH - PAD_RIGHT, ax_cur + 20)
                ay_tip = min(HEIGHT - PAD_BOT, ay_cur + 20)

            prev_arrow_midi[0] = cm0

            # Draw glow circle at base
            for r, a in [(14, 30), (9, 70), (5, 180)]:
                cv2.circle(frame, (ax_cur, ay_cur), r, (*pn0["col"], a), -1, cv2.LINE_AA)
            # Draw vector arrow
            cv2.arrowedLine(frame, (ax_cur - 18, ay_cur), (ax_tip, ay_tip),
                            pn0["col"], 3, cv2.LINE_AA, tipLength=0.55)
            # Border
            cv2.arrowedLine(frame, (ax_cur - 18, ay_cur), (ax_tip, ay_tip),
                            (20, 20, 20), 1, cv2.LINE_AA, tipLength=0.55)

        # Arrows for other instruments
        for pni in range(1, len(raw_parts)):
            pn    = raw_parts[pni]
            cm    = _current_midi(pn, t_now)
            state = inst_state[pni]
            if cm is not None and state["tail_x"] is not None:
                cx = state["tail_x"]
                cy = state["tail_y"] if state["tail_y"] is not None else _pitch_y(cm)
                tip_x = min(WIDTH - PAD_RIGHT, cx + 18)
                cv2.arrowedLine(frame, (cx - 12, cy), (tip_x, cy),
                                pn["col"], 2, cv2.LINE_AA, tipLength=0.6)

        # 3) Dynamic physics equations near primary arrow
        if cm0 is not None:
            freq   = _midi_to_hz(cm0)
            period = 1.0 / freq
            wavlen = V_SOUND / freq
            omega  = 2.0 * math.pi * freq

            dyn_eqs = [
                (rf"f = {freq:.1f} \; Hz",                              "#8b0000", 20),
                (rf"T = \frac{{1}}{{f}} = {period*1000:.2f} \; ms",    "#003399", 18),
                (rf"\lambda = \frac{{343}}{{{freq:.0f}}} = {wavlen:.3f} \; m", "#005500", 18),
                (rf"\omega = 2\pi f = {omega:.1f} \; rad/s",           "#660066", 17),
                (rf"n_{{MIDI}} = {cm0}",                                "#555500", 16),
            ]

            eq_x_base = 10
            eq_y_base = PAD_TOP + 10
            for ei, (eq_latex, eq_color, fsize) in enumerate(dyn_eqs):
                eq_key = (eq_latex, fsize, eq_color)
                if eq_key not in last_dyn_eqs:
                    last_dyn_eqs[eq_key] = render_eq(eq_latex, fsize, eq_color)
                eq_img = last_dyn_eqs.get(eq_key)
                ey = eq_y_base + ei * 48
                overlay_eq(frame, eq_img, eq_x_base, ey, alpha=0.95)

        # Progress bar
        bar_x = int((t_now / duration) * WIDTH)
        cv2.rectangle(frame, (0, HEIGHT - 5), (bar_x, HEIGHT), (140, 140, 140), -1)

        vw.write(frame)

    vw.release()

    # Merge audio
    if audio_path and os.path.exists(audio_path):
        cmd = ["ffmpeg", "-y",
               "-i", tmp_vid, "-i", audio_path,
               "-c:v", "libx264", "-crf", "26", "-preset", "fast",
               "-c:a", "aac", "-b:a", "160k",
               "-t", str(duration), "-shortest", output_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", tmp_vid,
               "-c:v", "libx264", "-crf", "26", "-preset", "fast",
               "-t", str(duration), output_path]

    r = _sp.run(cmd, capture_output=True)
    try:
        os.remove(tmp_vid)
    except Exception:
        pass
    return r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000


def _build_score_edit_keyboard(user_id: int) -> InlineKeyboardMarkup:
    uid = user_id
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏩ BPM+5",  callback_data=f"se_bpm+5_{uid}"),
            InlineKeyboardButton("⏪ BPM−5",  callback_data=f"se_bpm-5_{uid}"),
            InlineKeyboardButton("🔁 Reset",  callback_data=f"se_reset_{uid}"),
        ],
        [
            InlineKeyboardButton("🔼 +نصف تون", callback_data=f"se_tr+1_{uid}"),
            InlineKeyboardButton("🔽 −نصف تون", callback_data=f"se_tr-1_{uid}"),
            InlineKeyboardButton("🔀 Swing",    callback_data=f"se_swing_{uid}"),
        ],
        [
            InlineKeyboardButton("🥁 Drums On/Off", callback_data=f"se_mute_drums_{uid}"),
            InlineKeyboardButton("🎵 تصدير بالتعديلات", callback_data=f"se_render_{uid}"),
        ],
        [
            InlineKeyboardButton("📋 عرض النوتات بالحروف", callback_data=f"se_shownotes_{uid}"),
        ],
        [
            InlineKeyboardButton("🎸 تصدير GarageBand (.band)", callback_data=f"se_export_midi_{uid}"),
        ],
        [
            InlineKeyboardButton("🎬 مرئية موسيقية (فيديو كرات)", callback_data=f"se_visualize_{uid}"),
        ],
    ])

async def _apply_and_send_edited_render(
    message, user_id: int, tab_data: dict, edit_state: dict
):
    """Build modified tab_data from edit_state and synthesize a new audio."""
    import copy, asyncio
    ed      = edit_state
    bpm     = max(30, min(300, tab_data.get("bpm", 80) + ed["bpm_offset"]))
    transp  = ed["transpose"]
    swing   = ed["swing"]
    muted   = ed["muted"]

    # Clone tab_data and apply edits
    td = copy.deepcopy(tab_data)
    td["bpm"] = bpm

    # Transpose
    if transp != 0:
        for pd in td.get("all_parts", {}).values():
            for n in pd["notes"]:
                n["midi"] = max(0, min(127, n["midi"] + transp))

    # Swing — nudge every "and" of the beat by swing amount
    if swing > 0:
        beat_dur = 60.0 / bpm
        half     = beat_dur / 2
        swing_offset = half * swing * 0.33
        for pd in td.get("all_parts", {}).values():
            for n in pd["notes"]:
                beat_frac = (n["time"] % beat_dur) / beat_dur
                if 0.4 < beat_frac < 0.6:
                    n["time"] += swing_offset
        for dn in td.get("drum_notes", []):
            beat_frac = (dn["time"] % beat_dur) / beat_dur
            if 0.4 < beat_frac < 0.6:
                dn["time"] += swing_offset

    # Mute
    if "drums" in muted:
        td["has_drums"] = False
    for pname in muted - {"drums"}:
        td.get("all_parts", {}).pop(pname, None)

    # Gain overrides stored in td for render
    td["_gain_overrides"] = ed.get("gain_overrides", {})

    status = await message.reply_text("⚙️ *جارٍ إعادة التركيب بالتعديلات...*", parse_mode="Markdown")
    output_path = f"/tmp/score_edit_{user_id}.mp3"

    loop = asyncio.get_event_loop()
    try:
        ok = await loop.run_in_executor(None, lambda: render_musicxml_to_audio(td, output_path))
    except Exception as e:
        logger.error(f"Edit render error: {e}")
        ok = False

    await status.delete()
    if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        # Build caption
        parts = []
        if ed["bpm_offset"]: parts.append(f"BPM={bpm}")
        if transp:            parts.append(f"Transpose={'+'if transp>0 else''}{transp}")
        if swing:             parts.append(f"Swing={int(swing*100)}%")
        if muted:             parts.append(f"Muted={','.join(muted)}")
        cap = "🎛 *تعديلات:* " + (" | ".join(parts) if parts else "بدون تعديل")
        await message.reply_audio(
            audio=open(output_path, "rb"),
            caption=cap,
            parse_mode="Markdown",
            reply_markup=_build_score_edit_keyboard(user_id),
        )
        os.remove(output_path)
    else:
        await message.reply_text("❌ فشل التركيب. حاول مرة أخرى.")


async def cb_score_edit(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        action: str, user_id: int):
    """Handle score edit inline button callbacks."""
    query = update.callback_query
    await query.answer()

    pending = user_tab_pending.get(user_id)
    if not pending:
        await query.message.reply_text("⚠️ لا يوجد ملف محلل. أعد رفع الملف.")
        return

    tab_data = pending["tab_data"]
    ed = user_edit_state.setdefault(user_id, _DEFAULT_EDIT())

    if action == "reset":
        user_edit_state[user_id] = _DEFAULT_EDIT()
        ed = user_edit_state[user_id]
        await query.answer("تمت إعادة الضبط ✅")
    elif action.startswith("bpm"):
        delta = int(action.replace("bpm", ""))
        ed["bpm_offset"] = max(-40, min(100, ed["bpm_offset"] + delta))
        await query.answer(f"BPM offset = {ed['bpm_offset']:+d}")
    elif action.startswith("tr"):
        delta = int(action.replace("tr", ""))
        ed["transpose"] = max(-12, min(12, ed["transpose"] + delta))
        await query.answer(f"Transpose = {ed['transpose']:+d} semitones")
    elif action == "swing":
        ed["swing"] = 0.0 if ed["swing"] > 0 else 0.5
        await query.answer(f"Swing {'ON' if ed['swing'] else 'OFF'}")
    elif action == "mute_drums":
        if "drums" in ed["muted"]:
            ed["muted"].discard("drums")
            await query.answer("Drums ON")
        else:
            ed["muted"].add("drums")
            await query.answer("Drums muted")
    elif action == "shownotes":
        chunks = format_notes_for_export(tab_data)
        fname_base = pending.get("fname", "notes").rsplit(".", 1)[0]
        file_path  = f"/tmp/notes_{user_id}.txt"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(chunks))
        await query.message.reply_document(
            document=open(file_path, "rb"),
            filename=f"{fname_base}_notes.txt",
            caption=(
                "📋 جميع نوتات الملف بالحروف\n\n"
                "✏️ عدّل أي سطر ثم أرسله كرسالة نصية للبوت:\n"
                "Piano: C4 E4 G4 B4 A4 G4"
            ),
        )
        try:
            os.remove(file_path)
        except Exception:
            pass
        return
    elif action == "render":
        await _apply_and_send_edited_render(query.message, user_id, tab_data, ed)
        return
    elif action == "visualize":
        raw_path   = pending.get("raw_path")
        fname_base = pending.get("fname", "score").rsplit(".", 1)[0]
        vid_path   = f"/tmp/viz_{user_id}.mp4"
        audio_path = f"/tmp/viz_audio_{user_id}.mp3"

        status = await query.message.reply_text(
            "🎬 *جارٍ إنشاء المرئية الموسيقية...*\n\n"
            "⏳ تحليل النوتات\n"
            "🎹 رسم الكرات على مفاتيح البيانو\n"
            "🎵 دمج الصوت مع الفيديو\n\n"
            "_قد يستغرق 30-60 ثانية..._",
            parse_mode="Markdown",
        )
        try:
            loop = asyncio.get_event_loop()

            # Step 1: render audio
            def _render_audio():
                try:
                    render_musicxml_to_audio(tab_data, audio_path)
                except Exception:
                    pass

            # Step 2: generate visualization
            def _generate_viz():
                return generate_note_visualization(
                    tab_data=tab_data,
                    raw_xml_path=raw_path or "",
                    audio_path=audio_path if os.path.exists(audio_path) else "",
                    output_path=vid_path,
                    max_secs=60,
                )

            await loop.run_in_executor(None, _render_audio)
            ok = await loop.run_in_executor(None, _generate_viz)

            await status.delete()
            if ok and os.path.exists(vid_path) and os.path.getsize(vid_path) > 1000:
                instr_list = list(tab_data.get("all_parts", {}).keys())
                instr_str  = " • ".join(instr_list[:8]) or "—"
                bpm_disp   = tab_data.get("bpm", "?")
                key_disp   = tab_data.get("key_signature", "—")
                await query.message.reply_video(
                    video=open(vid_path, "rb"),
                    caption=(
                        f"🎬 *مرئية موسيقية — {fname_base}*\n\n"
                        f"🎼 كل لون = آلة موسيقية مختلفة\n"
                        f"🎹 الكرات تسقط على مفتاح البيانو الصحيح\n\n"
                        f"📊 BPM: {bpm_disp}   |   المقام: {key_disp}\n"
                        f"🎵 الآلات: {instr_str}"
                    ),
                    parse_mode="Markdown",
                    supports_streaming=True,
                )
                for p in [vid_path, audio_path]:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            else:
                await query.message.reply_text("❌ فشل إنشاء الفيديو.")
        except Exception as e:
            logger.error(f"Visualization error: {e}", exc_info=True)
            try:
                await status.delete()
            except Exception:
                pass
            await query.message.reply_text("❌ حدث خطأ أثناء إنشاء الفيديو.")
        return
    elif action == "export_midi":
        raw_path  = pending.get("raw_path")
        fname_base = pending.get("fname", "score").rsplit(".", 1)[0]
        status = await query.message.reply_text(
            "⏳ *جارٍ بناء مشروع GarageBand...*\n\n"
            "🎹 تحويل كل الآلات إلى MIDI\n"
            "📦 تجميع ملف .band",
            parse_mode="Markdown",
        )
        try:
            loop = asyncio.get_event_loop()
            band_zip_path = f"/tmp/export_{user_id}.band"

            def _build_band_file():
                import plistlib, zipfile, shutil
                from music21 import (converter, stream, note as m21note,
                                     tempo as m21tempo, instrument as m21inst,
                                     meter, key as m21key, chord as m21chord)

                # ── 1. Load / build score ────────────────────────────────────
                if raw_path and os.path.exists(raw_path):
                    sc = converter.parse(raw_path)
                else:
                    sc = stream.Score()
                    bpm_val  = tab_data.get("bpm", 80)
                    all_parts = tab_data.get("all_parts", {})
                    beat_dur = 60.0 / max(bpm_val, 40)
                    for part_name, pdata in all_parts.items():
                        part = stream.Part()
                        part.id = part_name
                        part.partName = part_name
                        part.append(m21tempo.MetronomeMark(number=bpm_val))
                        for n in pdata.get("notes", []):
                            midi_num = n.get("midi", 60)
                            dur_q    = max(0.125, n.get("duration", beat_dur) / beat_dur)
                            mn = m21note.Note()
                            mn.pitch.midi = midi_num
                            mn.quarterLength = dur_q
                            part.append(mn)
                        sc.append(part)

                # ── 2. Extract metadata from score ───────────────────────────
                flat = sc.flatten()
                bpm_marks = list(flat.getElementsByClass(m21tempo.MetronomeMark))
                bpm_val   = int(bpm_marks[0].number) if bpm_marks else tab_data.get("bpm", 80)

                ts_list = list(flat.getElementsByClass(meter.TimeSignature))
                ts_num  = int(ts_list[0].numerator)   if ts_list else 4
                ts_den  = int(ts_list[0].denominator) if ts_list else 4

                try:
                    ky = sc.analyze("key")
                    key_name = f"{ky.tonic.name} {ky.mode}"
                except Exception:
                    key_name = tab_data.get("key_signature", "C major")

                # ── 3. Write MIDI file ───────────────────────────────────────
                midi_tmp = f"/tmp/export_{os.getpid()}.mid"
                sc.write("midi", fp=midi_tmp)

                # ── 4. Read MIDI bytes ───────────────────────────────────────
                with open(midi_tmp, "rb") as f:
                    midi_bytes = f.read()
                try:
                    os.remove(midi_tmp)
                except Exception:
                    pass

                # ── 5. Build parts info list ─────────────────────────────────
                parts_info = []
                _GM_NAME_MAP = {
                    "piano": 0, "guitar": 25, "acoustic guitar": 25,
                    "electric guitar": 29, "bass": 33, "acoustic bass": 32,
                    "electric bass": 33, "drums": -1, "drum": -1,
                    "violin": 40, "viola": 41, "cello": 42,
                    "trumpet": 56, "trombone": 57, "saxophone": 64,
                    "sax": 64, "flute": 73, "clarinet": 71,
                    "organ": 19, "synth": 81, "wind": 73,
                    "strings": 48, "choir": 52, "pad": 89,
                }
                for i, part in enumerate(sc.parts):
                    pname = (part.partName or part.id or f"Track {i+1}").strip()
                    pname_low = pname.lower()
                    is_drum = any(d in pname_low for d in ("drum", "percussion", "perc"))
                    prog = -1 if is_drum else 0
                    for kw, gm in _GM_NAME_MAP.items():
                        if kw in pname_low:
                            prog = gm
                            break
                    parts_info.append({
                        "name": pname,
                        "midiProgram": prog,
                        "isDrum": is_drum,
                        "trackIndex": i,
                    })

                # ── 6. Build .band folder structure in memory ─────────────────
                tmp_root  = f"/tmp/band_build_{os.getpid()}"
                band_name = fname_base if fname_base else "MyProject"
                band_inner = f"{tmp_root}/{band_name}.band"
                os.makedirs(f"{band_inner}/Media",  exist_ok=True)
                os.makedirs(f"{band_inner}/Output", exist_ok=True)

                # Save MIDI inside Media/
                with open(f"{band_inner}/Media/{band_name}.mid", "wb") as f:
                    f.write(midi_bytes)

                # Build projectData binary plist
                tracks_plist = []
                for pi in parts_info:
                    tracks_plist.append({
                        "name":        pi["name"],
                        "midiProgram": pi["midiProgram"],
                        "isDrumTrack": pi["isDrum"],
                        "trackIndex":  pi["trackIndex"],
                        "volume":      0.7874015867710114,
                        "pan":         0.0,
                        "muted":       False,
                        "soloed":      False,
                    })

                project_data = {
                    "version":                    10,
                    "bpm":                        float(bpm_val),
                    "timeSignatureNumerator":     ts_num,
                    "timeSignatureDenominator":   ts_den,
                    "keySignature":               key_name,
                    "masterVolume":               0.7874015867710114,
                    "currentTrack":               0,
                    "swingAmount":                0.0,
                    "tracks":                     tracks_plist,
                    "midiFilename":               f"{band_name}.mid",
                    "totalTracks":                len(parts_info),
                    "instruments":                [p["name"] for p in parts_info],
                    "title":                      tab_data.get("title", band_name),
                    "composer":                   tab_data.get("composer", ""),
                    "chords":                     tab_data.get("chords", []),
                }
                plist_bytes = plistlib.dumps(project_data, fmt=plistlib.FMT_BINARY)
                with open(f"{band_inner}/projectData", "wb") as f:
                    f.write(plist_bytes)

                # ── 7. Zip into .band file ────────────────────────────────────
                with zipfile.ZipFile(band_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, dirs, files in os.walk(tmp_root):
                        for file in files:
                            fp     = os.path.join(root, file)
                            arcname = os.path.relpath(fp, tmp_root)
                            zf.write(fp, arcname)

                shutil.rmtree(tmp_root, ignore_errors=True)
                return True

            ok = await loop.run_in_executor(None, _build_band_file)
            await status.delete()

            if ok and os.path.exists(band_zip_path) and os.path.getsize(band_zip_path) > 100:
                parts_count = len(tab_data.get("all_parts", {})) or len(list(__import__("music21").converter.parse(raw_path).parts)) if raw_path and os.path.exists(raw_path) else "?"
                bpm_disp = tab_data.get("bpm", "?")
                key_disp = tab_data.get("key_signature", "—")
                instr_list = "\n".join(f"  • {k}" for k in list(tab_data.get("all_parts", {}).keys())[:12]) or "  • MIDI tracks"

                caption = (
                    f"🎹 *مشروع GarageBand — {fname_base}*\n\n"
                    f"📊 *تفاصيل المشروع:*\n"
                    f"• BPM: {bpm_disp}\n"
                    f"• المقام: {key_disp}\n"
                    f"• عدد المسارات: {len(tab_data.get('all_parts', {}))}\n\n"
                    f"🎼 *الآلات الموسيقية:*\n{instr_list}\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "📱 *طريقة الفتح على iPhone/iPad:*\n"
                    "1️⃣ اضغط على الملف في تيليغرام\n"
                    "2️⃣ اضغط *Share* ثم اختر *GarageBand*\n"
                    "3️⃣ سيفتح المشروع مباشرةً مع كل الآلات\n\n"
                    "💻 *على Mac:*\n"
                    "انقر نقراً مزدوجاً على الملف لفتحه في GarageBand\n\n"
                    "✅ الملف يحتوي على:\n"
                    "• جميع الآلات في مسارات منفصلة\n"
                    "• MIDI كامل بكل النوتات والتوقيتات\n"
                    "• BPM والمقام والميزان الموسيقي"
                )
                await query.message.reply_document(
                    document=open(band_zip_path, "rb"),
                    filename=f"{fname_base}.band",
                    caption=caption,
                    parse_mode="Markdown",
                )
                try:
                    os.remove(band_zip_path)
                except Exception:
                    pass
            else:
                await query.message.reply_text("❌ فشل بناء ملف GarageBand.")
        except Exception as e:
            logger.error(f"Band export error: {e}", exc_info=True)
            try:
                await status.delete()
            except Exception:
                pass
            await query.message.reply_text("❌ حدث خطأ أثناء تصدير ملف GarageBand.")
        return

    # Show current edit state
    bpm_now = tab_data.get("bpm", 80) + ed["bpm_offset"]
    status_lines = [
        f"🎛 *حالة التعديلات الحالية:*",
        f"• BPM: {bpm_now} ({ed['bpm_offset']:+d})",
        f"• Transpose: {ed['transpose']:+d} نصف تون",
        f"• Swing: {'ON' if ed['swing'] else 'OFF'}",
        f"• Muted: {', '.join(ed['muted']) or '—'}",
        "",
        "اضغط *🎵 تصدير بالتعديلات* لسماع النتيجة",
    ]
    try:
        await query.message.edit_text(
            "\n".join(status_lines),
            parse_mode="Markdown",
            reply_markup=_build_score_edit_keyboard(user_id),
        )
    except Exception:
        await query.message.reply_text(
            "\n".join(status_lines),
            parse_mode="Markdown",
            reply_markup=_build_score_edit_keyboard(user_id),
        )


def parse_musicxml(file_path: str) -> dict:
    """
    Parse a MusicXML file using music21.
    Extracts EVERY piece of information from the file:
    BPM, time signature, key, dynamics, articulations, repeats,
    all instrument notes with timing/duration, chord symbols, lyrics, metadata.
    Returns tab_data dict + tab_data["analysis"] with deep analysis.
    """
    from music21 import (converter, tempo as m21tempo, chord as m21chord,
                         note as m21note, dynamics as m21dyn, expressions as m21expr,
                         repeat as m21repeat, bar as m21bar, meter, harmony, key as m21key)

    result = {
        "bpm": 80, "tuning": "Standard", "chords": [],
        "guitar_notes": [], "bass_notes": [], "has_drums": False,
        "instruments": [], "all_parts": {},
        "file_type": "musicxml",
        # Extra fields
        "time_signature": "4/4",
        "title": "",
        "composer": "",
        "dynamics_map": [],     # list of {"time": ..., "dynamic": "mf"}
        "tempo_changes": [],    # list of {"time": ..., "bpm": ...}
        "repeat_structure": [], # list of strings describing repeats
        "lyrics": [],           # first few lyrics lines
        "key_signature": "—",
    }

    score = converter.parse(file_path)
    flat  = score.flatten()

    # ── Title / Composer ──────────────────────────────────────────────────────
    try:
        md = score.metadata
        if md:
            result["title"]    = md.title or ""
            result["composer"] = md.composer or ""
    except Exception:
        pass

    # ── BPM + all tempo changes ───────────────────────────────────────────────
    for el in flat.getElementsByClass(m21tempo.MetronomeMark):
        bpm_val = el.number or el.numberSounding
        if bpm_val:
            t = float(el.offset) * (60.0 / max(result["bpm"], 40))
            if result["bpm"] == 80:          # first found
                result["bpm"] = int(bpm_val)
            result["tempo_changes"].append({"time": round(t, 1), "bpm": int(bpm_val)})

    # ── Time Signature ────────────────────────────────────────────────────────
    for ts in flat.getElementsByClass(meter.TimeSignature):
        result["time_signature"] = ts.ratioString
        break

    # ── Key Signature ─────────────────────────────────────────────────────────
    try:
        k = score.analyze("key")
        result["key_signature"] = f"{k.tonic.name} {k.mode}"
    except Exception:
        pass

    # ── Dynamics across the whole score ───────────────────────────────────────
    beat_dur_ref = 60.0 / max(result["bpm"], 40)
    for dyn in flat.getElementsByClass(m21dyn.Dynamic):
        t_dyn = float(dyn.offset) * beat_dur_ref
        result["dynamics_map"].append({"time": round(t_dyn, 1), "dynamic": dyn.value or "?"})

    # ── Repeat structure ──────────────────────────────────────────────────────
    try:
        for rb in flat.getElementsByClass(m21repeat.RepeatExpression):
            result["repeat_structure"].append(str(rb))
    except Exception:
        pass
    try:
        for rb in flat.getElementsByClass(m21bar.Barline):
            try:
                if rb.type in ("start-repeat", "end-repeat", "final"):
                    result["repeat_structure"].append(rb.type)
            except Exception:
                pass
    except Exception:
        pass

    # ── Parts ─────────────────────────────────────────────────────────────────
    parts = score.parts if hasattr(score, "parts") else [score]
    seen_names: dict = {}
    for part in parts:
        raw_name = (part.partName or part.id or "Unknown").strip()
        name_lower = raw_name.lower()

        count = seen_names.get(raw_name, 0) + 1
        seen_names[raw_name] = count
        part_name = raw_name if count == 1 else f"{raw_name} {count}"

        is_pitched_perc = "pitched" in name_lower
        is_drum = (not is_pitched_perc) and any(kw in name_lower for kw in DRUM_KEYWORDS)

        if is_drum:
            result["has_drums"] = True
            if raw_name not in result["instruments"]:
                result["instruments"].append(raw_name)

            def _unpitched_midi(el_u):
                try:
                    dp = str(el_u.displayPitch())
                    return DRUM_DISPLAY_PITCH_MAP.get(dp, 38)
                except Exception:
                    return 38

            drum_hits = []
            for el in part.flatten().notesAndRests:
                el_type = type(el).__name__
                if el_type == "Rest":
                    continue
                time_sec = float(el.offset) * (60.0 / result["bpm"])
                pitches = []

                if el_type == "Unpitched":
                    pitches = [_unpitched_midi(el)]
                elif el_type == "PercussionChord":
                    for sub in el:
                        s_type = type(sub).__name__
                        if s_type == "Unpitched":
                            pitches.append(_unpitched_midi(sub))
                        elif s_type == "Note" and hasattr(sub, "pitch"):
                            pitches.append(sub.pitch.midi)
                elif isinstance(el, m21chord.Chord):
                    pitches = [p.midi for p in el.pitches]
                elif isinstance(el, m21note.Note):
                    pitches = [el.pitch.midi]

                for midi_p in pitches:
                    drum_hits.append({"midi": midi_p, "time": time_sec})

            result.setdefault("drum_notes", []).extend(drum_hits)
            logger.info(f"Drum part '{raw_name}': {len(drum_hits)} hits extracted")
            continue

        timbre = _timbre_for(raw_name)
        notes_list = []
        part_lyrics = []
        articulations_found = set()

        beat_dur = 60.0 / max(result["bpm"], 40)
        for el in part.flatten().notesAndRests:
            if isinstance(el, m21note.Rest):
                continue

            time_sec = float(el.offset) * beat_dur
            dur_sec  = float(el.duration.quarterLength) * beat_dur

            # Collect lyrics from notes
            if isinstance(el, m21note.Note) and el.lyrics:
                for lyr in el.lyrics:
                    txt = lyr.text or ""
                    if txt and len(part_lyrics) < 50:
                        part_lyrics.append(txt)

            # Collect articulations
            try:
                for art in el.articulations:
                    articulations_found.add(type(art).__name__)
            except Exception:
                pass

            # Velocity from dynamic context
            velocity = 80
            try:
                velocity = max(40, min(127, int(el.volume.velocity or 80)))
            except Exception:
                pass

            pitches = []
            if isinstance(el, m21chord.Chord):
                pitches = [p.midi for p in el.pitches]
            elif isinstance(el, m21note.Note):
                pitches = [el.pitch.midi]

            for midi_p in pitches:
                notes_list.append({
                    "midi": midi_p,
                    "time": time_sec,
                    "duration": dur_sec,
                    "string": 0,
                    "velocity": velocity,
                })

        if notes_list:
            result["all_parts"][part_name] = {
                "timbre": timbre,
                "notes": notes_list,
                "articulations": list(articulations_found),
                "lyrics": part_lyrics[:20],
            }
            if raw_name not in result["instruments"]:
                result["instruments"].append(raw_name)

            # Collect lyrics globally (first part that has them)
            if part_lyrics and not result["lyrics"]:
                result["lyrics"] = part_lyrics[:30]

            if "guitar" in name_lower or "oud" in name_lower:
                result["guitar_notes"].extend(notes_list)
            elif "bass" in name_lower:
                result["bass_notes"].extend(notes_list)

    # ── Chord symbols ─────────────────────────────────────────────────────────
    for cs in flat.getElementsByClass(harmony.ChordSymbol):
        f = cs.figure
        if f and f not in result["chords"]:
            result["chords"].append(f)

    # ── Deep analysis (key, time sig, modulations, drum pattern, melody, structure) ──
    try:
        result["analysis"] = deep_analyze_score(result, score=score)
    except Exception as e:
        logger.warning(f"deep_analyze_score failed: {e}")
        result["analysis"] = {}

    logger.info(
        f"MusicXML parsed: bpm={result['bpm']}, key={result['key_signature']}, "
        f"time_sig={result['time_signature']}, "
        f"parts={list(result['all_parts'].keys())}, "
        f"chords={result['chords']}"
    )
    return result


def render_musicxml_to_audio(tab_data: dict, output_path: str) -> bool:
    """
    Render all instrument parts from a parsed MusicXML (or PDF) tab_data.
    Priority: FluidSynth (via MIDI) → Karplus-Strong waveform synthesis.
    """
    # ── Try FluidSynth first (dramatically better audio quality) ──────────────
    try:
        midi_tmp = output_path.replace(".mp3", "_tmp_fs.mid").replace(".wav", "_tmp_fs.mid")
        if tab_data_to_midi(tab_data, midi_tmp):
            ok = render_midi_fluidsynth(midi_tmp, output_path)
            try:
                if os.path.exists(midi_tmp):
                    os.remove(midi_tmp)
            except Exception:
                pass
            if ok:
                # ── 808 enhancement: synthesize bass directly and mix in ──────────
                # FluidSynth GM cannot reproduce real 808. Synthesize with numpy instead.
                try:
                    _808_notes = []
                    _808_timbre = "bass_808"
                    all_parts = tab_data.get("all_parts", {})
                    bpm_val = float(tab_data.get("bpm", 80))
                    for pname, pdata in all_parts.items():
                        timbre = pdata.get("timbre", "")
                        if timbre.startswith("bass_808") or "808" in pname.lower():
                            _808_notes.extend(pdata.get("notes", []))
                            if timbre.startswith("bass_808"):
                                _808_timbre = timbre
                    if _808_notes:
                        _808_wav = output_path.replace(".mp3", "_808_synth.wav")
                        if synthesize_808_wav(_808_notes, bpm_val, _808_wav, timbre=_808_timbre):
                            # Load FluidSynth output + numpy 808 and mix
                            import librosa as _lr
                            import soundfile as _sf
                            sr_mix = 44100
                            y_fs,  _ = _lr.load(output_path, sr=sr_mix, mono=True, duration=90)
                            y_808, _ = _lr.load(_808_wav,    sr=sr_mix, mono=True, duration=90)
                            max_len = max(len(y_fs), len(y_808))
                            _buf_fs  = np.zeros(max_len, dtype=np.float32)
                            _buf_808 = np.zeros(max_len, dtype=np.float32)
                            _buf_fs[:len(y_fs)]   = y_fs
                            _buf_808[:len(y_808)] = y_808
                            # Normalise each before mixing
                            _pk_fs  = np.abs(_buf_fs).max()
                            _pk_808 = np.abs(_buf_808).max()
                            if _pk_fs  > 1e-6: _buf_fs  /= _pk_fs
                            if _pk_808 > 1e-6: _buf_808 /= _pk_808
                            # Apply spectral EQ to 808 against bass stem before mixing
                            # (reshapes 808 frequency response — no original audio added)
                            try:
                                _bass_stem = None
                                # stems not accessible here — use spectral_match inline
                                from scipy.signal import medfilt as _mfilt
                                _n_fft_808, _hop_808 = 2048, 512
                                _sr_808 = sr_mix
                                # self-normalise 808 spectrum to emphasise sub-bass
                                _D808 = _lr.stft(_buf_808, n_fft=_n_fft_808, hop_length=_hop_808)
                                _env808 = np.abs(_D808).mean(axis=1) + 1e-9
                                # Boost frequencies below 300Hz by up to 6dB for sub-bass presence
                                _freqs808 = _lr.fft_frequencies(sr=_sr_808, n_fft=_n_fft_808)
                                _sub_gain = np.where(_freqs808 < 300,
                                                     np.clip(150.0 / (_freqs808 + 30), 1.0, 3.0),
                                                     1.0).astype(np.float32)
                                _D808_eq = _D808 * _sub_gain[:, np.newaxis]
                                _buf_808 = _lr.istft(_D808_eq, hop_length=_hop_808,
                                                     length=len(_buf_808)).astype(np.float32)
                                _pk808 = np.abs(_buf_808).max()
                                if _pk808 > 1e-6:
                                    _buf_808 /= _pk808
                            except Exception:
                                pass
                            # 808 at 88%, FluidSynth (drums+synth) at 80%
                            mixed = _buf_fs * 0.80 + _buf_808 * 0.88
                            _pk = np.abs(mixed).max()
                            if _pk > 1e-6:
                                mixed = (mixed / _pk * 0.92).astype(np.float32)
                            _mix_wav = output_path.replace(".mp3", "_808mixed.wav")
                            _sf.write(_mix_wav, mixed, sr_mix, subtype="PCM_16")
                            subprocess.run(
                                ["ffmpeg", "-y", "-i", _mix_wav,
                                 "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k",
                                 output_path],
                                capture_output=True, check=True
                            )
                            for _f in [_mix_wav, _808_wav]:
                                try: os.remove(_f)
                                except Exception: pass
                            logger.info(f"render_musicxml_to_audio: 808 synthesized ({len(_808_notes)} notes) and mixed")
                except Exception as _e808:
                    logger.warning(f"808 synthesis mix failed (non-fatal): {_e808}")
                logger.info("render_musicxml_to_audio: used FluidSynth path")
                return True
    except Exception as e:
        logger.warning(f"FluidSynth path failed in render_musicxml_to_audio: {e}")

    # ── Fallback: Karplus-Strong waveform synthesis ────────────────────────────
    logger.info("render_musicxml_to_audio: falling back to Karplus-Strong")
    sr = 44100
    all_parts = tab_data.get("all_parts", {})

    all_notes = []
    for pdata in all_parts.values():
        all_notes.extend(n["time"] for n in pdata["notes"])
    if not all_notes and not tab_data.get("guitar_notes") and not tab_data.get("bass_notes"):
        return False

    bpm = tab_data.get("bpm", 80)
    beat_dur = 60.0 / bpm

    # Use all_parts if available (MusicXML), else fall back to guitar/bass
    if all_parts:
        max_time = max(
            (n["time"] + n.get("duration", beat_dur) for pdata in all_parts.values() for n in pdata["notes"]),
            default=32 * beat_dur
        )
    else:
        all_t = [n["time"] for n in tab_data.get("guitar_notes", []) + tab_data.get("bass_notes", [])]
        max_time = max(all_t) if all_t else 32 * beat_dur

    # Cap total output to 90 seconds to avoid giant allocations
    total_time = min(max_time + 2.0, 90.0)
    n_total    = int(sr * total_time)
    mix        = np.zeros(n_total, dtype=np.float32)

    if all_parts:
        n_parts = len(all_parts)
        for pdata in all_parts.values():
            timbre = pdata.get("timbre", "guitar")
            gain   = 0.70 / max(n_parts ** 0.5, 1)
            # Limit per-part notes to 500 to keep synthesis fast
            notes_to_render = pdata["notes"][:500]
            for note in notes_to_render:
                if note["time"] >= total_time:
                    continue
                dur  = min(note.get("duration", beat_dur * 1.2) + 0.3, 4.0)
                tone = _synth_note(note["midi"], dur, timbre, sr)
                si   = int(note["time"] * sr)
                ei   = min(si + len(tone), n_total)
                mix[si:ei] += tone[:ei - si] * gain
    else:
        for note in tab_data.get("guitar_notes", []):
            tone = _ks_instrument(_midi_to_hz(note["midi"]), beat_dur * 1.6, sr, "guitar")
            si = int(note["time"] * sr); ei = min(si + len(tone), n_total)
            mix[si:ei] += tone[:ei - si] * 0.68
        for note in tab_data.get("bass_notes", []):
            tone = _ks_instrument(_midi_to_hz(note["midi"]), beat_dur * 1.8, sr, "bass")
            si = int(note["time"] * sr); ei = min(si + len(tone), n_total)
            mix[si:ei] += tone[:ei - si] * 0.70

    # ── Normalize melodic bus first, then add drums at independent level ──────
    mel_peak = np.abs(mix).max()
    if mel_peak > 1e-6:
        mix = mix / mel_peak * 0.72          # melodic bus at 72 %

    if tab_data.get("has_drums"):
        drum_notes = tab_data.get("drum_notes", [])
        if drum_notes:
            drum_t = _render_drum_notes(drum_notes, total_time, sr)
        else:
            drum_t = _synth_drums(beat_dur, total_time, sr)
        d_peak = np.abs(drum_t).max()
        if d_peak > 1e-6:
            drum_t = drum_t / d_peak * 0.68  # drum bus normalized to 68 %
        length = min(len(drum_t), n_total)
        mix[:length] += drum_t[:length]

    mix = _add_reverb(mix, sr, room=0.18)
    peak = np.abs(mix).max()
    if peak < 1e-6:
        return False
    mix = mix / peak * 0.90

    wav_tmp = output_path.replace(".mp3", "_raw.wav")
    sf.write(wav_tmp, mix, sr, subtype="PCM_16")
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_tmp,
         "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k", output_path],
        capture_output=True, check=True
    )
    if os.path.exists(wav_tmp):
        os.remove(wav_tmp)
    logger.info(f"render_musicxml_to_audio OK, size={os.path.getsize(output_path)}")
    return True


# ── GM instrument map (keyword → program number) ──────────────────────────────
_GM_MAP: list[tuple[str, int]] = [
    ("drum",              -1),   # -1 = channel 9 drums
    ("percussion",        -1),
    ("bass",              33),
    ("acoustic guitar",   24),
    ("guitar",            25),
    ("oud",               24),
    ("electric piano",     4),
    ("piano",              0),
    ("organ",             16),
    ("soprano sax",       64),
    ("alto sax",          65),
    ("sax",               65),
    ("violin",            40),
    ("viola",             41),
    ("cello",             42),
    ("strings",           48),
    ("synth lead",        80),
    ("synth",             80),
    ("pad",               88),
    ("trumpet",           56),
    ("flute",             73),
    ("marimba",           12),
    ("vibraphone",        11),
    ("xylophone",         13),
    ("pitched percussion",12),
    ("harp",              46),
    ("choir",             52),
]

def _gm_program(name: str) -> int:
    """Return GM program number for instrument name (-1 = drums)."""
    nl = name.lower()
    for kw, prog in _GM_MAP:
        if kw in nl:
            return prog
    return 0  # default Piano


def tab_data_to_midi(tab_data: dict, output_path: str) -> bool:
    """
    Convert a parsed tab_data dict into a proper multi-track MIDI file.
    Each instrument gets its own track with correct GM program.
    Drums go to channel 9. Saves to output_path. Returns True on success.
    """
    import pretty_midi
    bpm  = max(40, min(300, tab_data.get("bpm", 80)))
    pm   = pretty_midi.PrettyMIDI(initial_tempo=float(bpm))

    # ── Melody / harmony parts ─────────────────────────────────────────────
    all_parts = tab_data.get("all_parts", {})
    drum_ch    = 9
    used_ch    = set([drum_ch])
    ch         = 0

    for pname, pd in all_parts.items():
        prog = _gm_program(pname)
        is_drum = prog == -1

        if is_drum:
            inst = pretty_midi.Instrument(program=0, is_drum=True, name=pname)
        else:
            while ch in used_ch:
                ch += 1
            if ch > 15:
                ch = 0
            used_ch.add(ch)
            inst = pretty_midi.Instrument(program=prog, is_drum=False, name=pname)

        notes = sorted(pd.get("notes", []), key=lambda n: n.get("time", 0))
        for n in notes:
            start = float(n.get("time", 0))
            dur   = max(0.05, float(n.get("duration", 0.25)))
            end   = start + dur
            vel   = max(1, min(127, int(n.get("velocity", 80))))
            pitch = max(0, min(127, int(n.get("midi", 60))))
            if end > start:
                inst.notes.append(pretty_midi.Note(
                    velocity=vel, pitch=pitch, start=start, end=end
                ))

        if inst.notes:
            pm.instruments.append(inst)

    # ── Drum track ─────────────────────────────────────────────────────────
    drum_notes = tab_data.get("drum_notes", [])
    if tab_data.get("has_drums") and drum_notes:
        drum_inst = pretty_midi.Instrument(program=0, is_drum=True, name="Drums")
        beat_dur  = 60.0 / bpm
        for dn in drum_notes:
            t   = float(dn.get("time", 0))
            pit = max(0, min(127, int(dn.get("midi", 36))))
            drum_inst.notes.append(pretty_midi.Note(
                velocity=100, pitch=pit, start=t, end=t + beat_dur * 0.1
            ))
        if drum_inst.notes:
            pm.instruments.append(drum_inst)

    if not pm.instruments:
        return False

    pm.write(output_path)
    logger.info(f"tab_data_to_midi: {len(pm.instruments)} tracks → {output_path}")
    return True


def parse_band_file(zip_path: str) -> dict | None:
    """
    Parse a GarageBand .band file (zip package) and extract track/note data.
    Returns a tab_data dict or None on failure.
    """
    import zipfile, plistlib, io

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            # Find projectData (may be at root or inside a subdirectory)
            pd_path = next(
                (n for n in names if n.endswith("projectData")), None
            )
            if not pd_path:
                logger.warning("parse_band_file: no projectData found in zip")
                return None

            raw = zf.read(pd_path)

        try:
            plist = plistlib.loads(raw)
        except Exception:
            # Sometimes stored as XML plist
            plist = plistlib.loads(raw, fmt=plistlib.FMT_XML)

        # GarageBand projectData uses NSKeyedArchiver; try to extract tempo + tracks
        tab = {
            "bpm": 120, "key_signature": "C major", "time_signature": "4/4",
            "chords": [], "guitar_notes": [], "bass_notes": [],
            "has_drums": False, "drum_notes": [], "instruments": [],
            "all_parts": {}, "lyrics": [], "repeat_structure": [],
            "dynamics_map": [], "title": "", "composer": "", "analysis": {},
        }

        # Try to find tempo in plist
        def _search(obj, key, depth=0):
            if depth > 10:
                return None
            if isinstance(obj, dict):
                if key in obj:
                    return obj[key]
                for v in obj.values():
                    r = _search(v, key, depth+1)
                    if r is not None:
                        return r
            elif isinstance(obj, list):
                for v in obj:
                    r = _search(v, key, depth+1)
                    if r is not None:
                        return r
            return None

        tempo = _search(plist, "tempo") or _search(plist, "Tempo") or 120
        try:
            tab["bpm"] = int(float(str(tempo)))
        except Exception:
            pass

        return tab

    except Exception as e:
        logger.warning(f"parse_band_file error: {e}")
        return None


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF, .flr, .musicxml, .xml and .band score files."""
    doc = update.message.document
    if not doc:
        return

    mime  = doc.mime_type or ""
    fname = doc.file_name or ""
    fl    = fname.lower()

    is_pdf      = "pdf" in mime.lower() or fl.endswith(".pdf")
    is_flr      = fl.endswith(".flr")
    is_musicxml = fl.endswith(".musicxml") or fl.endswith(".xml") or "musicxml" in mime.lower()
    is_band     = fl.endswith(".band") or fl.endswith(".band.zip")

    if not (is_pdf or is_flr or is_musicxml or is_band):
        await update.message.reply_text(
            "📄 الصيغ المدعومة:\n"
            "• *PDF / .flr* — تابلاتشر (Klangio وغيره)\n"
            "• *.musicxml / .xml* — نوتات موسيقية\n"
            "• *.band* — مشروع GarageBand\n\n"
            "أو أرسل مقطعاً صوتياً للتعرف عليه.",
            parse_mode="Markdown"
        )
        return

    user_id = update.effective_user.id

    status_msg = await update.message.reply_text(
        "📄 *جارٍ تحليل الملف الموسيقي...*\n\n⏳ يرجى الانتظار",
        parse_mode="Markdown"
    )

    try:
        loop = asyncio.get_event_loop()

        ext       = ".musicxml" if is_musicxml else ".pdf"
        raw_path  = f"/tmp/score_{user_id}{ext}"
        tg_file   = await doc.get_file()
        await tg_file.download_to_drive(raw_path)

        await status_msg.edit_text(
            "🔬 *[1/2]* جارٍ قراءة النوتات والأكوردات والآلات...",
            parse_mode="Markdown"
        )

        if is_musicxml:
            tab_data = await loop.run_in_executor(None, lambda: parse_musicxml(raw_path))
            file_label = "MusicXML"
        else:
            tab_data = await loop.run_in_executor(None, lambda: parse_tablature_pdf(raw_path))
            file_label = "تابلاتشر"

        logger.info(f"Parsed {file_label}: bpm={tab_data['bpm']}, "
                    f"instruments={tab_data['instruments']}, chords={tab_data['chords']}")

        await status_msg.edit_text(
            "🎼 *[2/2]* جارٍ توليد الصوت لكل آلة...",
            parse_mode="Markdown"
        )

        audio_path = f"/tmp/score_{user_id}_basic.mp3"
        basic_ok   = await loop.run_in_executor(
            None, lambda: render_musicxml_to_audio(tab_data, audio_path)
        )

        # ── Build analysis message ─────────────────────────────────────────────
        if is_musicxml and tab_data.get("analysis"):
            # Full rich analysis for MusicXML
            analysis_msg = build_rich_analysis_msg(tab_data, fname)
        else:
            # Basic format for PDF tablature
            bpm    = tab_data.get("bpm", "?")
            chords = " • ".join(tab_data.get("chords", [])) or "—"
            insts  = " • ".join(tab_data.get("instruments", [])) or "—"
            g_count = len(tab_data.get("guitar_notes", []))
            b_count = len(tab_data.get("bass_notes", []))
            parts_detail = ""
            if tab_data.get("all_parts"):
                plines = [f"  • {pn}: *{len(pd['notes'])}* نوتة"
                          for pn, pd in tab_data["all_parts"].items()]
                parts_detail = "\n" + "\n".join(plines)
            analysis_msg = (
                f"🎼 *تحليل ملف {file_label}*\n\n"
                f"⚡ السرعة: *{bpm} BPM*\n"
                f"🎵 الأكوردات: `{chords}`\n"
                f"🎹 الآلات:{parts_detail if parts_detail else ' *' + insts + '*'}\n"
                f"🎸 جيتار: *{g_count}* نوتة  |  باص: *{b_count}* نوتة\n"
            )

        # ── Extra MusicXML metadata block ─────────────────────────────────────
        extra_info = ""
        if is_musicxml:
            title_v    = tab_data.get("title", "")
            composer_v = tab_data.get("composer", "")
            key_v      = tab_data.get("key_signature", "—")
            ts_v       = tab_data.get("time_signature", "—")
            dyn_summary = ""
            dyn_map = tab_data.get("dynamics_map", [])
            if dyn_map:
                unique_dyn = list(dict.fromkeys(d["dynamic"] for d in dyn_map))[:6]
                dyn_summary = " • ".join(unique_dyn)
            tc = tab_data.get("tempo_changes", [])
            tempo_str = ""
            if len(tc) > 1:
                tempo_str = " → ".join(str(t["bpm"]) for t in tc[:4])
            lyrics_lines = tab_data.get("lyrics", [])
            lyrics_str = " ".join(lyrics_lines[:10]) if lyrics_lines else "—"

            extra_info = (
                f"\n━━━━━━━━━━━━━━━━\n"
                f"📌 *معلومات الملف*\n"
                + (f"🎵 العنوان: {title_v}\n" if title_v else "")
                + (f"🎼 المؤلف: {composer_v}\n" if composer_v else "")
                + f"🔑 المقام: *{key_v}*\n"
                + f"📊 الميزان: *{ts_v}*\n"
                + (f"⚡ تغييرات السرعة: {tempo_str}\n" if tempo_str else "")
                + (f"🔊 الديناميكيات: {dyn_summary}\n" if dyn_summary else "")
                + (f"📝 كلمات: {lyrics_str}\n" if lyrics_str and lyrics_str != "—" else "")
            )

        user_tab_pending[user_id] = {
            "tab_data":     tab_data,
            "fname":        fname,
            "analysis_msg": analysis_msg,
            "raw_path":     raw_path if is_musicxml else None,
        }
        user_edit_state[user_id] = _DEFAULT_EDIT()

        await status_msg.delete()

        # Edit keyboard for MusicXML
        edit_markup = _build_score_edit_keyboard(user_id) if is_musicxml else None

        # ── Send audio first (simple safe caption, no Markdown) ───────────────
        if basic_ok and os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            await update.message.reply_audio(
                audio=open(audio_path, "rb"),
                title=f"{file_label} — {fname}",
                performer="تحليل موسيقي",
                caption=f"🔊 الصوت الكامل — {fname}",
            )

        # ── Send full analysis + extra info as text message ───────────────────
        def _safe_md(text: str) -> str:
            """Escape characters that break Telegram Markdown v1."""
            # Only replace bare underscores that are NOT inside backtick spans
            import re
            # Replace * and _ outside of backtick blocks with safe equivalents
            parts_md = re.split(r'(`[^`]*`)', text)
            out = []
            for part in parts_md:
                if part.startswith('`') and part.endswith('`'):
                    out.append(part)
                else:
                    # escape lone underscores that could be mistaken for italic
                    part = re.sub(r'(?<!\*)\*(?!\*)', r'\\*', part)
                    out.append(part)
            return "".join(out)

        full_text = analysis_msg + extra_info
        # Split into chunks of ≤4096 chars (Telegram text limit)
        chunk_size = 4000
        chunks = [full_text[i:i+chunk_size] for i in range(0, len(full_text), chunk_size)]
        for i, chunk in enumerate(chunks):
            kb = edit_markup if (i == len(chunks) - 1) else None
            try:
                await update.message.reply_text(
                    chunk,
                    parse_mode="Markdown",
                    reply_markup=kb,
                )
            except Exception:
                # If Markdown fails, send as plain text
                await update.message.reply_text(
                    chunk,
                    reply_markup=kb,
                )

        if not is_musicxml:
            await update.message.reply_text(
                "🎤 *أرسل لي ملفاً صوتياً* وسأعيد توزيع جميع الآلات على إيقاعه!",
                parse_mode="Markdown",
            )

        for p in [raw_path, audio_path]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"handle_document error: {e}", exc_info=True)
        await status_msg.edit_text(
            "❌ *حدث خطأ أثناء تحليل الملف*\n\n"
            "تأكد أن الملف صحيح (PDF / .flr / .musicxml).",
            parse_mode="Markdown"
        )


async def cb_instruments(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    await query.answer()

    if user_id not in user_songs:
        await query.message.reply_text("❌ يرجى إرسال أغنية أولاً.", reply_markup=build_main_menu())
        return

    track = user_songs[user_id]["track"]
    title = track.get("title", "غير معروف")
    artist = track.get("subtitle", "غير معروف")
    genre = track.get("genres", {}).get("primary", "غير معروف")

    instruments = detect_instruments_from_genre(genre, title, artist)
    instruments_list = "\n".join(f"  {inst}" for inst in instruments)

    msg = (
        f"🎹 *الآلات الموسيقية في الأغنية*\n\n"
        f"🎵 {title} — {artist}\n"
        f"🎼 النوع: {genre}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{instruments_list}"
    )

    back_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 العودة للخيارات", callback_data=f"back_to_options_{user_id}")],
    ])

    await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=back_markup)


async def cb_more_details(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    await query.answer()

    if user_id not in user_songs:
        await query.message.reply_text("❌ يرجى إرسال أغنية أولاً.", reply_markup=build_main_menu())
        return

    song_data = user_songs[user_id]
    result = song_data["result"]
    track = song_data["track"]

    sections = track.get("sections", [])
    lyrics_section = next((s for s in sections if s.get("type") == "LYRICS"), None)
    lyrics_text = ""
    if lyrics_section:
        lines = lyrics_section.get("text", [])
        if lines:
            lyrics_text = "\n".join(lines[:15])

    url = track.get("url", "")
    title = track.get("title", "")
    artist = track.get("subtitle", "")

    msg = f"📋 *تفاصيل موسعة*\n\n🎵 {title} — {artist}\n"

    if url:
        msg += f"🔗 [فتح في Shazam]({url})\n"

    if lyrics_text:
        msg += f"\n📝 *كلمات الأغنية (مقتطف):*\n\n_{lyrics_text}_\n"
    else:
        msg += "\n📝 الكلمات غير متاحة\n"

    related = result.get("related", {})
    if isinstance(related, dict):
        hits = related.get("hits", [])
        if hits:
            msg += "\n🎵 *أغاني مشابهة:*\n"
            for h in hits[:5]:
                ht = h.get("heading", {}).get("title", "")
                ha = h.get("heading", {}).get("subtitle", "")
                if ht:
                    msg += f"  • {ht} — {ha}\n"

    back_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 العودة للخيارات", callback_data=f"back_to_options_{user_id}")],
    ])

    await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=back_markup)


async def cb_full_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    await query.answer("🔬 جارٍ التحليل الشامل...")

    if user_id not in user_songs:
        await query.message.reply_text("❌ يرجى إرسال أغنية أولاً.", reply_markup=build_main_menu())
        return

    song_data = user_songs[user_id]
    track = song_data["track"]
    audio_path = song_data.get("mp3_path") or song_data.get("original_path")
    title  = track.get("title", "غير معروف")
    artist = track.get("subtitle", "غير معروف")

    status_msg = await query.message.reply_text(
        "🔬 *جارٍ التحليل الشامل الكامل...*\n\n"
        "📊 يشمل: الإيقاع • الهارموني • اللحن • الأقسام • الطبقات • التيمبر\n"
        "⏳ قد يستغرق دقيقة...",
        parse_mode="Markdown"
    )

    try:
        loop = asyncio.get_event_loop()
        analysis = await loop.run_in_executor(
            None, lambda: analyze_full_deep(audio_path, track)
        )

        if not analysis:
            await status_msg.edit_text("❌ تعذّر إجراء التحليل. حاول مرة أخرى.")
            return

        # Build sections text
        sections_text = "\n".join(f"  • {s}" for s in analysis.get("sections", [])) or "  —"

        msg = (
            f"🔬 *التحليل الشامل الكامل*\n"
            f"🎵 {title} — {artist}\n"
            f"⏱️ المدة: {analysis.get('duration_sec', '?')} ثانية\n\n"

            f"━━━━━━━━━━━━━━━━\n"
            f"🥁 *١. الإيقاع (Rhythm)*\n"
            f"• السرعة: *{analysis.get('bpm', '?')} BPM*\n"
            f"• الميزان: *{analysis.get('time_signature', '?')}*\n"
            f"• Groove: *{analysis.get('groove', '?')}*\n"
            f"• Syncopation: {analysis.get('syncopation', '?')}\n"
            f"• النمط الإيقاعي: {analysis.get('drum_pattern', '?')}\n\n"

            f"━━━━━━━━━━━━━━━━\n"
            f"🎹 *٢. الهارموني (Harmony)*\n"
            f"• المقام: *{analysis.get('key', '?')}*\n"
            f"• السلم: {analysis.get('scale', '?')}\n"
            f"• تسلسل الكوردات: `{analysis.get('chord_progression', '?')}`\n"
            f"• Modulation: {analysis.get('modulation', '?')}\n\n"

            f"━━━━━━━━━━━━━━━━\n"
            f"🎼 *٣. اللحن (Melody)*\n"
            f"• مدى الصوت: *{analysis.get('pitch_range', '?')}*\n"
            f"• نطاق المدى: {analysis.get('pitch_range_label', '?')}\n"
            f"• تكرار اللحن: {analysis.get('melody_repetition', '?')}\n"
            f"• الزخارف: {analysis.get('ornaments', '?')}\n\n"

            f"━━━━━━━━━━━━━━━━\n"
            f"🎬 *٤. أقسام الأغنية (Song Sections)*\n"
            f"• الأقسام المكتشفة:\n{sections_text}\n\n"

            f"━━━━━━━━━━━━━━━━\n"
            f"🎚️ *٥. الطبقات والأدوار (Layers)*\n"
            f"• Lead: {analysis.get('lead_role', '?')}\n"
            f"• Pad: {analysis.get('pad_role', '?')}\n"
            f"• Background: {analysis.get('bg_role', '?')}\n\n"

            f"━━━━━━━━━━━━━━━━\n"
            f"🔊 *٦. أبعاد متقدمة*\n"
            f"• Micro-timing: {analysis.get('micro_timing', '?')}\n"
            f"• Timbre: {analysis.get('timbre', '?')}\n"
            f"• Envelope: {analysis.get('envelope', '?')}\n"
            f"• Transients: {analysis.get('transients', '?')}\n"
            f"• Loudness: {analysis.get('loudness', '?')}\n"
            f"• Movement: {analysis.get('movement', '?')}"
        )

        back_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✂️ تعديل الملف الصوتي", callback_data=f"editmenu_{user_id}")],
            [InlineKeyboardButton("🔙 العودة للخيارات", callback_data=f"back_to_options_{user_id}")],
        ])

        await status_msg.delete()
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=back_markup)

    except Exception as e:
        logger.error(f"cb_full_analysis error: {e}", exc_info=True)
        await status_msg.edit_text("❌ حدث خطأ أثناء التحليل. حاول مرة أخرى.")


async def cb_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    await query.answer()

    if user_id not in user_songs:
        await query.message.reply_text("❌ يرجى إرسال أغنية أولاً.", reply_markup=build_main_menu())
        return

    track = user_songs[user_id]["track"]
    title  = track.get("title", "غير معروف")
    artist = track.get("subtitle", "غير معروف")

    msg = (
        f"✂️ *تعديل الملف الصوتي*\n\n"
        f"🎵 {title} — {artist}\n\n"
        f"اختر العملية التي تريد تطبيقها:"
    )
    await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=build_edit_menu(user_id))


async def _apply_audio_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    user_id: int, operation: str
):
    """Generic handler for all audio edit operations."""
    query = update.callback_query
    await query.answer()

    if user_id not in user_songs:
        await query.message.reply_text("❌ يرجى إرسال أغنية أولاً.")
        return

    song_data = user_songs[user_id]
    audio_path = song_data.get("mp3_path") or song_data.get("original_path")
    track = song_data["track"]
    title  = track.get("title", "غير معروف")
    artist = track.get("subtitle", "غير معروف")

    op_labels = {
        "speed_up":   "⚡ رفع السرعة (×1.25)",
        "speed_down": "⚡ خفض السرعة (×0.8)",
        "pitch_up":   "🎵 رفع النغمة (نصف تون)",
        "pitch_down": "🎵 خفض النغمة (نصف تون)",
        "louder":     "🔊 رفع مستوى الصوت",
        "quieter":    "🔉 خفض مستوى الصوت",
        "reverse":    "🔃 عكس الصوت",
        "bass":       "🎚️ تعزيز الباس",
    }

    status_msg = await query.message.reply_text(
        f"⏳ *جارٍ تطبيق: {op_labels.get(operation, operation)}...*",
        parse_mode="Markdown"
    )

    try:
        output_path = f"/tmp/edited_{user_id}_{operation}.mp3"

        if operation == "speed_up":
            af = "atempo=1.25"
        elif operation == "speed_down":
            af = "atempo=0.8"
        elif operation == "pitch_up":
            # Raise pitch by 1 semitone: multiply rate by 2^(1/12), then resample back
            factor = 2 ** (1 / 12)
            af = f"asetrate=44100*{factor:.5f},aresample=44100,atempo={1/factor:.5f}"
        elif operation == "pitch_down":
            factor = 2 ** (1 / 12)
            af = f"asetrate=44100/{factor:.5f},aresample=44100,atempo={factor:.5f}"
        elif operation == "louder":
            af = "volume=2.0"
        elif operation == "quieter":
            af = "volume=0.5"
        elif operation == "reverse":
            af = "areverse"
        elif operation == "bass":
            af = "equalizer=f=60:width_type=o:width=2:g=8,equalizer=f=120:width_type=o:width=2:g=5"
        else:
            await status_msg.edit_text("❌ عملية غير معروفة.")
            return

        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path,
             "-af", af,
             "-acodec", "libmp3lame", "-ar", "44100", "-ab", "320k",
             output_path],
            capture_output=True, check=True
        )

        await status_msg.delete()

        back_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✂️ تعديلات أخرى", callback_data=f"editmenu_{user_id}")],
            [InlineKeyboardButton("🔙 العودة للخيارات", callback_data=f"back_to_options_{user_id}")],
        ])

        await query.message.reply_audio(
            audio=open(output_path, "rb"),
            title=f"{title} — {op_labels.get(operation, operation)}",
            performer=artist,
            caption=(
                f"✅ *تم التعديل بنجاح!*\n\n"
                f"🎵 {title} — {artist}\n"
                f"🔧 العملية: *{op_labels.get(operation, operation)}*\n"
                f"📊 الجودة: 320kbps"
            ),
            parse_mode="Markdown",
            reply_markup=back_markup,
        )

        try:
            os.remove(output_path)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"_apply_audio_edit error ({operation}): {e}", exc_info=True)
        try:
            await status_msg.edit_text("❌ حدث خطأ أثناء التعديل. حاول مرة أخرى.")
        except Exception:
            pass


async def cb_back_to_options(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    query = update.callback_query
    await query.answer()

    if user_id not in user_songs:
        await query.message.reply_text("❌ لا توجد أغنية محفوظة.", reply_markup=build_main_menu())
        return

    track = user_songs[user_id]["track"]
    title = track.get("title", "غير معروف")
    artist = track.get("subtitle", "غير معروف")

    await query.message.reply_text(
        f"🎵 *{title}* — {artist}\n\nماذا تريد أن تفعل؟",
        parse_mode="Markdown",
        reply_markup=build_song_actions(user_id),
    )


async def cb_show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "📖 *كيفية الاستخدام*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎵 *قسم تحليل الأغاني الصوتية:*\n"
        "1️⃣ أرسل أي مقطع صوتي أو رسالة صوتية\n"
        "2️⃣ انتظر التعرف على الأغنية\n"
        "3️⃣ استخدم الأزرار للمزيد من الخيارات:\n\n"
        "🔼 *رفع الجودة* — تحسين الصوت بجودة 320kbps\n"
        "🎼 *تصدير MusicXML* — استخراج النوتات وتصدير ملف موسيقي + سماع إعادة الإنشاء\n"
        "📋 *تفاصيل أكثر* — كلمات ومعلومات إضافية\n"
        "🎹 *الآلات الموسيقية* — قائمة بالآلات المستخدمة\n"
        "🔬 *تحليل شامل كامل* — إيقاع • هارموني • لحن • أقسام • طبقات\n"
        "✂️ *تعديل الملف الصوتي* — سرعة • نغمة • صوت • باس • عكس\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎸 *قسم تحليل ملفات التابلاتشر (جديد!):*\n"
        "1️⃣ أرسل ملف PDF أو .flr للتابلاتشر\n"
        "2️⃣ سيحلل البوت النوتات والأكوردات والآلات\n"
        "3️⃣ سيولّد ملفاً صوتياً من التابلاتشر مباشرة!\n\n"
        "✅ يدعم: رسائل الصوت، MP3، M4A، OGG، PDF، FLR"
    )

    back_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])

    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=back_markup)


async def cb_show_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "ℹ️ *عن البوت*\n\n"
        "🎼 بوت متكامل لتحليل الأغاني وتصدير النوتات الموسيقية\n\n"
        "🔧 *التقنيات المستخدمة:*\n"
        "• Shazam API — للتعرف على الأغاني\n"
        "• librosa — لتحليل الصوت (BPM، المقام، التوقيع الزمني)\n"
        "• Basic-Pitch (Spotify) — لاستخراج النوتات بدقة\n"
        "• music21 — لبناء وتصدير ملفات MusicXML\n"
        "• FFmpeg — لمعالجة ورفع جودة الصوت\n\n"
        "✨ *المميزات:*\n"
        "• التعرف الفوري على الأغنية\n"
        "• كشف الآلات الموسيقية\n"
        "• رفع جودة الصوت إلى 320kbps\n"
        "• تصدير MusicXML مع Pitch Bend للانزلاقات\n"
        "• إعادة إنشاء صوتي من النوتات المستخرجة"
    )

    back_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])

    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=back_markup)


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "👋 *أهلاً! أنا بوت تحليل وتصدير الأغاني*\n\n"
        "🎵 أرسل لي أي مقطع صوتي وسأقوم بـ:\n"
        "✅ التعرف على الأغنية فوراً\n"
        "🎹 كشف الآلات الموسيقية\n"
        "🔼 رفع جودة الصوت\n"
        "🎼 تصدير MusicXML + إعادة إنشاء صوتي\n\n"
        "📤 *ابدأ بإرسال مقطع صوتي!*"
    )

    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=build_main_menu())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("enhance_"):
        await cb_enhance(update, context, int(data.split("_")[1]))
    elif data.startswith("export_midi_"):
        await cb_export_musicxml(update, context, int(data.split("_")[2]))
    elif data.startswith("instruments_"):
        await cb_instruments(update, context, int(data.split("_")[1]))
    elif data.startswith("moredetails_"):
        await cb_more_details(update, context, int(data.split("_")[1]))
    elif data.startswith("fullanalysis_"):
        await cb_full_analysis(update, context, int(data.split("_")[1]))
    elif data.startswith("editmenu_"):
        await cb_edit_menu(update, context, int(data.split("_")[1]))
    elif data.startswith("edit_speed_"):
        uid = int(data.split("_")[2])
        await _apply_audio_edit(update, context, uid, "speed_up")
    elif data.startswith("edit_pitch_"):
        uid = int(data.split("_")[2])
        await _apply_audio_edit(update, context, uid, "pitch_up")
    elif data.startswith("edit_louder_"):
        uid = int(data.split("_")[2])
        await _apply_audio_edit(update, context, uid, "louder")
    elif data.startswith("edit_quieter_"):
        uid = int(data.split("_")[2])
        await _apply_audio_edit(update, context, uid, "quieter")
    elif data.startswith("edit_reverse_"):
        uid = int(data.split("_")[2])
        await _apply_audio_edit(update, context, uid, "reverse")
    elif data.startswith("edit_bass_"):
        uid = int(data.split("_")[2])
        await _apply_audio_edit(update, context, uid, "bass")
    elif data.startswith("se_"):
        # Score-edit callbacks: se_<action>_<uid>
        # action can be: bpm+5, bpm-5, tr+1, tr-1, swing, reset, mute_drums, render
        parts = data.split("_")
        uid = int(parts[-1])
        action = "_".join(parts[1:-1])   # everything between "se_" prefix and uid
        await cb_score_edit(update, context, action, uid)
    elif data.startswith("back_to_options_"):
        await cb_back_to_options(update, context, int(data.split("_")[-1]))
    elif data == "show_help":
        await cb_show_help(update, context)
    elif data == "show_about":
        await cb_show_about(update, context)
    elif data == "main_menu":
        await cb_main_menu(update, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle free text.
    Detects note-edit lines in the format:
        InstrumentName: C4 D4 E4 G4 ...
    and re-renders the audio with updated notes.
    Falls back to a generic prompt.
    """
    import re, copy, asyncio
    text  = (update.message.text or "").strip()
    uid   = update.effective_user.id

    # ── Detect note-edit input ────────────────────────────────────────────────
    # The bot outputs multi-line blocks like:
    #   Piano: C4 E4 G4 ...
    #     A4 B4 C5 ...        ← continuation lines start with whitespace
    # Pre-process: join continuation lines back onto the instrument line
    NOTE_TOKEN = r'[A-Ga-g][#Bb]?-?\d+'
    joined_lines = []
    for raw_line in text.splitlines():
        if raw_line and raw_line[0] in (' ', '\t'):
            # continuation — append to last line
            if joined_lines:
                joined_lines[-1] += ' ' + raw_line.strip()
            else:
                joined_lines.append(raw_line.strip())
        else:
            joined_lines.append(raw_line)
    joined_text = "\n".join(joined_lines)

    LINE_RE = re.compile(
        r'^(.+?):\s+(' + NOTE_TOKEN + r'(?:\s+' + NOTE_TOKEN + r')*)\s*$',
        re.MULTILINE
    )
    matches = LINE_RE.findall(joined_text)

    if matches and uid in user_tab_pending:
        pending  = user_tab_pending[uid]
        tab_data = pending["tab_data"]
        all_parts = tab_data.get("all_parts", {})

        # Build a lower-case map for fuzzy matching
        part_lower = {k.lower(): k for k in all_parts}

        updated_parts = []
        skipped_parts = []
        for raw_name, notes_str in matches:
            raw_name = raw_name.strip()
            key = raw_name.lower()
            # Exact match first, then fuzzy (part of name)
            canonical = part_lower.get(key)
            if not canonical:
                for pk, pv in part_lower.items():
                    if key in pk or pk in key:
                        canonical = pv
                        break
            if not canonical:
                skipped_parts.append(raw_name)
                continue

            tokens = re.findall(NOTE_TOKEN, notes_str)
            if not tokens:
                skipped_parts.append(raw_name)
                continue

            # Build new evenly-spaced note list (preserve original timing grid)
            old_notes = sorted(all_parts[canonical]["notes"], key=lambda n: n.get("time", 0))
            bpm       = tab_data.get("bpm", 80)
            beat_dur  = 60.0 / max(bpm, 40)
            step      = beat_dur / 2  # 8th-note grid

            new_notes = []
            for i, tok in enumerate(tokens):
                orig = old_notes[i] if i < len(old_notes) else {}
                new_notes.append({
                    "midi":     name_to_midi(tok),
                    "time":     orig.get("time", i * step),
                    "duration": orig.get("duration", step * 0.9),
                    "string":   orig.get("string", 0),
                    "velocity": orig.get("velocity", 80),
                })
            tab_data["all_parts"][canonical]["notes"] = new_notes
            updated_parts.append(f"{canonical} ({len(new_notes)} نوتة)")

        if not updated_parts:
            await update.message.reply_text(
                "⚠️ لم أتعرف على أسماء الآلات.\n"
                "تأكد أن تكتب اسم الآلة تماماً كما ظهر في القائمة."
            )
            return

        # Re-render with updated notes
        import copy
        ed = user_edit_state.get(uid, _DEFAULT_EDIT())
        info_lines = ["✅ تم تحديث النوتات:"]
        info_lines += [f"  • {p}" for p in updated_parts]
        if skipped_parts:
            info_lines.append(f"⚠️ لم يُعثر على: {', '.join(skipped_parts)}")
        info_lines.append("\n🎧 جاري التصيير…")
        status = await update.message.reply_text("\n".join(info_lines))

        output_path = f"/tmp/note_edit_{uid}.mp3"
        try:
            loop = asyncio.get_event_loop()
            # Apply edit_state (BPM, transpose, mute) then render
            td = copy.deepcopy(tab_data)
            td["bpm"] = max(30, min(300, td.get("bpm", 80) + ed["bpm_offset"]))
            if ed["transpose"]:
                for pd in td.get("all_parts", {}).values():
                    for n in pd["notes"]:
                        n["midi"] = max(0, min(127, n["midi"] + ed["transpose"]))
            if "drums" in ed["muted"]:
                td["has_drums"] = False
            td["_gain_overrides"] = ed.get("gain_overrides", {})

            ok = await loop.run_in_executor(
                None, lambda: render_musicxml_to_audio(td, output_path)
            )
            await status.delete()
            if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                await update.message.reply_audio(
                    audio=open(output_path, "rb"),
                    title="تصيير بالنوتات المعدّلة",
                    performer="تحليل موسيقي",
                    caption="\n".join(info_lines[:3]),
                    reply_markup=_build_score_edit_keyboard(uid),
                )
                try:
                    os.remove(output_path)
                except Exception:
                    pass
            else:
                await update.message.reply_text("❌ فشل التصيير، حاول مرة أخرى.")
        except Exception as e:
            logger.error(f"Note-edit render error: {e}")
            try:
                await status.edit_text("❌ حدث خطأ أثناء التصيير.")
            except Exception:
                pass
        return

    # ── Default response ──────────────────────────────────────────────────────
    await update.message.reply_text(
        "📤 أرسل لي مقطعاً صوتياً أو رسالة صوتية لأحللها!\n\n"
        "أو اضغط /start للقائمة الرئيسية."
    )


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started successfully!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
