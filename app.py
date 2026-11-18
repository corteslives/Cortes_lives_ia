
import os
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file, abort
from werkzeug.utils import secure_filename

import core

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = core.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = (
    int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
)
app.config["SECRET_KEY"] = os.environ.get(
    "FLASK_SECRET_KEY",
    "change-this-secret-before-public-deploy"
)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def current_api_key():
    return os.environ.get("GEMINI_API_KEY", "").strip()


@app.get("/")
def index():
    settings = core.load_settings()
    state = core.read_state()

    html_file = APP_DIR / "index.html"

    if not html_file.is_file():
        return jsonify(error="Arquivo index.html não encontrado na pasta principal."), 500

    html = html_file.read_text(encoding="utf-8")

    return render_template_string(
        html,
        settings=settings,
        state=state,
        has_logo=bool(core.logo_path()),
        has_server_key=bool(current_api_key())
    )


@app.get("/api/state")
def api_state():
    state = core.read_state()
    safe_clips = []

    for i, clip in enumerate(state.get("clips", [])):
        safe_clips.append({
            "index": i,
            "headline": clip.get("headline", ""),
            "start": clip.get("start", 0),
            "end": clip.get("end", 0),
            "version": clip.get("version", 1),
            "download_url": f"/download/{i}"
        })

    return jsonify({
        "status": state.get("status", "idle"),
        "message": state.get("message", ""),
        "progress": state.get("progress", 0),
        "error": state.get("error", ""),
        "avisos": state.get("avisos", []),
        "clips": safe_clips
    })


@app.post("/api/start")
def start():
    if core.read_state().get("status") == "running":
        return jsonify(error="Já existe um processamento em andamento."), 409

    video = request.files.get("video")
    youtube_url = request.form.get("youtube_url", "").strip()

    if bool(youtube_url) == bool(video and video.filename):
        return jsonify(error="Escolha um vídeo para enviar OU cole um link do YouTube."), 400

    if video and video.filename and not allowed_file(video.filename):
        return jsonify(error="Formato não aceito. Use MP4, MOV, MKV, WEBM, AVI ou M4V."), 400

    if youtube_url and not (
        youtube_url.startswith("https://www.youtube.com/")
        or youtube_url.startswith("https://youtu.be/")
        or youtube_url.startswith("https://m.youtube.com/")
    ):
        return jsonify(error="Cole um link válido do YouTube começando com https://."), 400

    api_key = current_api_key() or request.form.get("api_key", "").strip()

    if not api_key:
        return jsonify(error="Informe sua chave da API Gemini ou configure GEMINI_API_KEY no servidor."), 400

    try:
        n = max(1, min(10, int(request.form.get("n", 4))))
        min_s = max(5, min(300, int(request.form.get("min_s", 30))))
        max_s = max(min_s, min(600, int(request.form.get("max_s", 60))))
    except ValueError:
        return jsonify(error="Quantidade ou duração inválida."), 400

    suffix = (
        Path(secure_filename(video.filename)).suffix.lower() or ".mp4"
        if video and video.filename else ".mp4"
    )

    upload_path = UPLOAD_DIR / f"live_{uuid.uuid4().hex}{suffix}"

    try:
        if youtube_url:
            import yt_dlp

            ydl_opts = {
                "outtmpl": str(upload_path),
                "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
                "merge_output_format": "mp4",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "max_filesize": int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([youtube_url])

            if not upload_path.exists():
                candidates = list(UPLOAD_DIR.glob(upload_path.stem + ".*"))
                if candidates:
                    upload_path = candidates[0]

            if not upload_path.exists():
                raise RuntimeError(
                    "Não foi possível baixar o vídeo do YouTube. Verifique se o link é público e tente enviar o arquivo."
                )

        else:
            video.save(upload_path)

        job_video = core.new_job_dir(upload_path.suffix or suffix)
        upload_path.replace(job_video)

        logo = core.logo_path()
        handle = request.form.get("handle", "@seuperfil").strip() or "@seuperfil"
        contexto = request.form.get("contexto", "").strip()

        core.save_settings({
            "handle": handle,
            "contexto": contexto,
            "n": n,
            "min_s": min_s,
            "max_s": max_s
        })

        core.start_job(
            job_video,
            logo,
            handle,
            contexto,
            n,
            min_s,
            max_s,
            api_key
        )

    except Exception as exc:
        try:
            upload_path.unlink(missing_ok=True)
        except Exception:
            pass

        return jsonify(error=str(exc)), 400

    return jsonify(ok=True)


@app.post("/api/logo")
def upload_logo():
    logo = request.files.get("logo")

    if not logo or not logo.filename:
        return jsonify(error="Selecione uma imagem."), 400

    ext = Path(secure_filename(logo.filename)).suffix.lower()

    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        return jsonify(error="Use PNG, JPG ou WEBP."), 400

    data = logo.read()

    if len(data) > 10 * 1024 * 1024:
        return jsonify(error="A logo deve ter até 10 MB."), 400

    core.save_logo(data)

    return jsonify(ok=True)


@app.get("/download/<int:index>")
def download(index):
    state = core.read_state()
    clips = state.get("clips", [])

    if index < 0 or index >= len(clips):
        abort(404)

    path = Path(clips[index].get("path", "")).resolve()
    job_root = core.JOB_DIR.resolve()

    if job_root not in path.parents or not path.is_file():
        abort(404)

    return send_file(
        path,
        as_attachment=True,
        download_name=f"corte_{index+1}.mp4"
    )


@app.post("/api/edit/<int:index>")
def edit(index):
    state = core.read_state()

    if state.get("status") == "running":
        return jsonify(error="Aguarde o processamento terminar."), 409

    headline = request.form.get("headline", "").strip()
    handle = request.form.get("handle", "").strip() or "@seuperfil"

    if not headline:
        return jsonify(error="Digite uma manchete."), 400

    try:
        core.edit_clip(index, headline, handle, core.logo_path())
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.errorhandler(413)
def too_large(_):
    return jsonify(
        error="O vídeo ultrapassa o limite de envio configurado no servidor."
    ), 413


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        debug=False
        )
        
