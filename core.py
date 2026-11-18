"""
Lógica do Cortador de Lives (sem Streamlit):
transcrição, escolha dos cortes pela IA, montagem com FFmpeg e
fila de tarefa gravada em disco (sobrevive a recarregar a página).
"""
import json
import os
import platform
import re
import shutil
import subprocess
import textwrap
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
JOB_DIR = DATA_DIR / "job"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOGO_FILE = DATA_DIR / "logo.png"
STATE_FILE = JOB_DIR / "state.json"
SEGMENTS_FILE = JOB_DIR / "segments.json"

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")

DEFAULT_SETTINGS = {"handle": "@seuperfil", "contexto": "", "n": 4, "min_s": 30, "max_s": 60}

BOOT_ID = uuid.uuid4().hex  # muda a cada reinício do servidor
_executor = ThreadPoolExecutor(max_workers=1)
_state_lock = threading.RLock()
_whisper = None
_whisper_lock = threading.Lock()


# ---------------------------------------------------------------- arquivos
def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return s


def save_settings(settings):
    clean = {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    _write_json(SETTINGS_FILE, clean)


def save_logo(data: bytes):
    LOGO_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOGO_FILE.write_bytes(data)


def logo_path():
    return str(LOGO_FILE) if LOGO_FILE.exists() else None


# ---------------------------------------------------------------- estado
def _empty_state():
    return {"status": "idle", "message": "", "progress": 0.0, "clips": [], "error": "",
            "avisos": [], "job_id": ""}


def read_state():
    with _state_lock:
        try:
            s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return _empty_state()
        if s.get("status") == "running" and s.get("boot_id") != BOOT_ID:
            s["status"] = "error"
            s["error"] = "O servidor reiniciou durante o processamento. Envie o vídeo novamente."
            _write_json(STATE_FILE, s)
        return s


def update_state(**kw):
    with _state_lock:
        s = read_state()
        s.update(kw)
        _write_json(STATE_FILE, s)


def new_job_dir(suffix):
    """Apaga o trabalho anterior e devolve o caminho onde salvar o novo vídeo."""
    with _state_lock:
        if read_state()["status"] == "running":
            raise RuntimeError("Já existe um processamento em andamento.")
        shutil.rmtree(JOB_DIR, ignore_errors=True)
        JOB_DIR.mkdir(parents=True, exist_ok=True)
    return JOB_DIR / ("live" + suffix.lower())


# ---------------------------------------------------------------- IA
def get_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(WHISPER_MODEL, compute_type="int8")
        return _whisper


def transcribe(video_path, on_progress=None):
    segments, info = get_whisper().transcribe(str(video_path), language="pt", vad_filter=True)
    out, last = [], -1
    for s in segments:
        out.append([s.start, s.end, s.text.strip()])
        if on_progress and info.duration:
            pct = int(min(s.end / info.duration, 1.0) * 100)
            if pct != last:
                last = pct
                on_progress(pct / 100)
    return out


def parse_clips(text, n, max_s, total):
    text = re.sub(r"```(?:json)?", "", text)
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j < 0:
        raise ValueError("A IA não devolveu uma lista de cortes.")
    clips = []
    for c in json.loads(text[i:j + 1]):
        try:
            s, e = float(c["start"]), float(c["end"])
            h = str(c["headline"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        s = max(0.0, s)
        if total:
            e = min(e, total)
        if e - s > max_s:
            e = s + max_s
        if e - s < 5 or not h:
            continue
        clips.append({"start": s, "end": e, "headline": h})
    clips.sort(key=lambda c: c["start"])
    return clips[:n]


def pick_clips(api_key, segments, n, min_s, max_s, contexto=""):
    """Seleciona cortes usando a API Google Gemini."""
    from google import genai
    from google.genai import types

    transcript = "\\n".join(f"[{s:.1f}-{e:.1f}] {t}" for s, e, t in segments)
    prompt = f"""Você é editor de vídeos curtos.
Analise a transcrição abaixo, que contém tempos em segundos.
Escolha até {n} trechos fortes para vídeos curtos, cada um com duração entre
{min_s} e {max_s} segundos. Comece e termine em pontos naturais da fala.
Crie para cada trecho uma manchete clara e curta, com até 10 palavras.
Use somente nomes que apareçam na transcrição ou neste contexto: {contexto or "não informado"}.
Não invente acontecimentos nem nomes.
Responda somente com um array JSON neste formato:
[{{"start": 12.0, "end": 55.0, "headline": "Manchete do corte"}}]

TRANSCRIÇÃO:
{transcript}"""

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
            max_output_tokens=3000,
        ),
    )
    response_text = response.text or ""
    total = segments[-1][1] if segments else 0
    return parse_clips(response_text, n, max_s, total)


# ---------------------------------------------------------------- FFmpeg
def font_path():
    if os.environ.get("FONT_PATH"):
        return os.environ["FONT_PATH"]
    if platform.system() == "Windows":
        return "C\\:/Windows/Fonts/arialbd.ttf"
    if platform.system() == "Darwin":
        return "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def make_clip(video, logo, clip, segments, handle, out_path, workdir):
    """Corta, deixa vertical 9:16 e aplica legenda, logo, @ e manchete."""
    start, end = float(clip["start"]), float(clip["end"])
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    lines = []
    for s, e, t in segments:
        if e > start and s < end and t:
            a, b = max(s, start) - start, min(e, end) - start
            lines.append(f"{len(lines) + 1}\n{srt_time(a)} --> {srt_time(b)}\n{t}\n")
    (workdir / "clip.srt").write_text("\n".join(lines), encoding="utf-8")
    (workdir / "headline.txt").write_text("\n".join(textwrap.wrap(clip["headline"], 20)),
                                          encoding="utf-8")
    (workdir / "handle.txt").write_text(handle, encoding="utf-8")

    font = font_path()
    inputs = ["-ss", str(start), "-to", str(end), "-i", str(video)]
    chain, cur, k = [], "0:v", 0

    def nxt():
        nonlocal k
        k += 1
        return f"v{k}"

    o = nxt()
    chain.append(f"[{cur}]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[{o}]")
    cur = o
    if logo:
        inputs += ["-i", str(logo)]
        chain.append("[1:v]scale=w=180:h=180:force_original_aspect_ratio=decrease[lg]")
        o = nxt()
        chain.append(f"[{cur}][lg]overlay=40:60[{o}]")
        cur = o
    if lines:
        o = nxt()
        style = ("Alignment=2,MarginV=330,MarginL=70,MarginR=70,FontSize=58,Bold=1,"
                 "Outline=4,Shadow=0,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000")
        chain.append(f"[{cur}]subtitles=clip.srt:original_size=1080x1920:force_style='{style}'[{o}]")
        cur = o
    o = nxt()
    chain.append(
        f"[{cur}]drawtext=fontfile='{font}':textfile=headline.txt:expansion=none:"
        "fontcolor=white:fontsize=58:line_spacing=10:box=1:boxcolor=black@0.65:boxborderw=24:"
        f"x=(w-text_w)/2:y=290[{o}]")
    cur = o
    chain.append(
        f"[{cur}]drawtext=fontfile='{font}':textfile=handle.txt:expansion=none:"
        "fontcolor=white:fontsize=44:borderw=3:bordercolor=black:"
        "x=(w-text_w)/2:y=h-200[vout]")

    cmd = (["ffmpeg", "-y", "-loglevel", "error"] + inputs
           + ["-filter_complex", ";".join(chain), "-map", "[vout]", "-map", "0:a?",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-movflags", "+faststart", str(out_path)])
    r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-1500:] or "Falha no FFmpeg")


# ---------------------------------------------------------------- tarefa
def start_job(video, logo, handle, contexto, n, min_s, max_s, api_key):
    params = dict(video=str(video), logo=logo, handle=handle, contexto=contexto,
                  n=n, min_s=min_s, max_s=max_s)
    with _state_lock:
        if read_state()["status"] == "running":
            raise RuntimeError("Já existe um processamento em andamento.")
        _write_json(STATE_FILE, {**_empty_state(), "status": "running", "boot_id": BOOT_ID,
                                 "job_id": uuid.uuid4().hex[:8], "video": str(video),
                                 "handle": handle, "message": "Na fila...", "progress": 0.01})
    _executor.submit(_run_job, params, api_key)


def _run_job(p, api_key):
    try:
        last_pct = [-1]

        def on_prog(f):
            pct = int(f * 100)
            if pct != last_pct[0]:
                last_pct[0] = pct
                update_state(message=f"Transcrevendo o áudio... {pct}%", progress=0.02 + 0.5 * f)

        update_state(message="Transcrevendo o áudio...", progress=0.02)
        segments = transcribe(p["video"], on_prog)
        _write_json(SEGMENTS_FILE, segments)
        if not segments:
            raise RuntimeError("Não foi encontrada fala no vídeo.")

        update_state(message="A IA está escolhendo os melhores cortes...", progress=0.55)
        clips = pick_clips(api_key, segments, p["n"], p["min_s"], p["max_s"], p["contexto"])
        if not clips:
            raise RuntimeError("A IA não encontrou cortes válidos. Tente novamente.")

        total, avisos = len(clips), []
        for i, clip in enumerate(clips, 1):
            update_state(message=f"Montando corte {i}/{total}...",
                         progress=0.6 + 0.4 * (i - 1) / total)
            workdir = JOB_DIR / f"c{i}"
            out = JOB_DIR / f"corte_{i}_v1.mp4"
            try:
                make_clip(p["video"], p["logo"], clip, segments, p["handle"], out, workdir)
            except Exception as e:  # um corte com erro não derruba os outros
                avisos.append(f"Corte {i} falhou: {e}")
                continue
            with _state_lock:
                s = read_state()
                s["clips"].append({**clip, "path": str(out), "workdir": str(workdir),
                                   "version": 1})
                _write_json(STATE_FILE, s)
        if not read_state()["clips"]:
            raise RuntimeError("Nenhum corte pôde ser montado. " + " | ".join(avisos))
        update_state(status="done", message="Pronto!", progress=1.0, avisos=avisos)
    except Exception as e:
        update_state(status="error", error=str(e))


def edit_clip(idx, headline, handle, logo):
    """Refaz um corte com nova manchete (grava em arquivo novo, versionado)."""
    with _state_lock:
        s = read_state()
        if s["status"] == "running":
            raise RuntimeError("Aguarde o processamento terminar.")
        c = s["clips"][idx]
    segments = json.loads(SEGMENTS_FILE.read_text(encoding="utf-8"))
    v = int(c.get("version", 1)) + 1
    out = JOB_DIR / f"corte_{idx + 1}_v{v}.mp4"
    make_clip(s["video"], logo, {**c, "headline": headline}, segments, handle, out, c["workdir"])
    with _state_lock:
        s = read_state()
        old = s["clips"][idx]["path"]
        s["clips"][idx].update(headline=headline, path=str(out), version=v)
        _write_json(STATE_FILE, s)
    try:
        os.remove(old)
    except OSError:
        pass
