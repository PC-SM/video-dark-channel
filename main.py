from fastapi import FastAPI, Query
from pydantic import BaseModel
import edge_tts
import asyncio
import base64
import tempfile
import os
import re
import httpx
import subprocess
import random
from typing import List, Optional

app = FastAPI()

# ──────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────

class TTSRequest(BaseModel):
    texto: str
    voz: str = "pt-BR-AntonioNeural"

class JuntarRequest(BaseModel):
    blocos_base64: List[str]
    titulo: str = "audio_final"

class VideoRequest(BaseModel):
    audio_base64: str
    palavras_chave: List[str]
    titulo: str
    pexels_key: str
    # Opções Fase A (todas com valor padrão para manter compatibilidade)
    usar_videos_pexels: bool = True       # True = busca clipes de vídeo; False = fotos
    ken_burns: bool = True                # Efeito zoom/pan nas fotos (quando usar_videos_pexels=False)
    transicoes: bool = True               # Crossfade entre clipes/fotos
    duracao_transicao: float = 0.8        # Segundos de crossfade
    trilha_url: Optional[str] = None      # URL de MP3 de trilha (Pixabay etc) — opcional
    volume_trilha: float = 0.12           # Volume da trilha (0.0–1.0) sob a narração
    overlay_titulo: bool = True           # Exibe título nas primeiras cenas
    watermark_text: str = "CANAL DARK"    # Texto do watermark no canto
    resolucao: str = "1920:1080"          # Resolução final do vídeo

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def converter_pausas(texto: str) -> str:
    texto = re.sub(r'\[PAUSA_LONGA\]', '. . . . .', texto)
    texto = re.sub(r'\[PAUSA\]', '. . .', texto)
    return texto

def sanitizar_titulo(titulo: str) -> str:
    """Remove caracteres especiais para uso em filtros FFmpeg."""
    return re.sub(r"[^\w\s\-]", "", titulo)[:50]

async def buscar_videos_pexels(palavras: List[str], pexels_key: str, total: int = 8) -> List[str]:
    """Busca clipes de vídeo na Pexels API. Retorna lista de URLs .mp4."""
    urls = []
    por_palavra = max(2, total // len(palavras))
    async with httpx.AsyncClient(timeout=30) as client:
        for palavra in palavras:
            if len(urls) >= total:
                break
            try:
                r = await client.get(
                    "https://api.pexels.com/videos/search",
                    headers={"Authorization": pexels_key},
                    params={
                        "query": palavra,
                        "orientation": "landscape",
                        "size": "medium",
                        "per_page": por_palavra
                    }
                )
                data = r.json()
                for video in data.get("videos", []):
                    # Pega o arquivo HD (1280) ou o maior disponível
                    files = sorted(
                        video.get("video_files", []),
                        key=lambda x: x.get("width", 0),
                        reverse=True
                    )
                    hd = next(
                        (f for f in files if f.get("width", 0) <= 1920 and f.get("file_type") == "video/mp4"),
                        files[0] if files else None
                    )
                    if hd:
                        urls.append(hd["link"])
                        if len(urls) >= total:
                            break
            except Exception:
                continue
    return urls

async def buscar_fotos_pexels(palavras: List[str], pexels_key: str, total: int = 9) -> List[str]:
    """Busca fotos na Pexels API (fallback). Retorna lista de URLs."""
    urls = []
    por_palavra = max(2, total // len(palavras))
    async with httpx.AsyncClient(timeout=30) as client:
        for palavra in palavras:
            if len(urls) >= total:
                break
            try:
                r = await client.get(
                    "https://api.pexels.com/v1/search",
                    headers={"Authorization": pexels_key},
                    params={
                        "query": palavra,
                        "orientation": "landscape",
                        "per_page": por_palavra,
                        "size": "medium"
                    }
                )
                data = r.json()
                for foto in data.get("photos", []):
                    src = foto.get("src", {}).get("large2x") or foto.get("src", {}).get("large")
                    if src:
                        urls.append(src)
                        if len(urls) >= total:
                            break
            except Exception:
                continue
    return urls

async def baixar_arquivo(url: str, destino: str) -> bool:
    """Baixa qualquer arquivo (foto ou vídeo) para o disco."""
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(url)
            with open(destino, "wb") as f:
                f.write(r.content)
        return os.path.getsize(destino) > 1000
    except Exception:
        return False

def get_duracao(path: str) -> float:
    """Retorna duração em segundos de um arquivo de mídia via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True
        )
        return float(result.stdout.strip())
    except Exception:
        return 5.0

# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────

@app.post("/narrar")
async def narrar(
    req: TTSRequest,
    bloco_index: int = Query(0),
    titulo: str = Query("")
):
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmpfile = f.name

    texto_limpo = converter_pausas(req.texto)
    communicate = edge_tts.Communicate(texto_limpo, req.voz)
    await communicate.save(tmpfile)

    with open(tmpfile, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()
    os.unlink(tmpfile)

    return {
        "audio_base64": audio_b64,
        "formato": "mp3",
        "bloco_index": bloco_index,
        "titulo": titulo
    }


@app.post("/juntar")
async def juntar(req: JuntarRequest):
    combined_bytes = b""
    for b64 in req.blocos_base64:
        combined_bytes += base64.b64decode(b64)

    return {
        "audio_base64": base64.b64encode(combined_bytes).decode(),
        "titulo": req.titulo,
        "formato": "mp3"
    }


@app.post("/montar")
async def montar(req: VideoRequest):
    tmpdir = tempfile.mkdtemp()

    try:
        # ── 1. Salvar áudio da narração ──────────────────────────────
        audio_path = os.path.join(tmpdir, "narration.mp3")
        with open(audio_path, "wb") as f:
            f.write(base64.b64decode(req.audio_base64))
        duracao_total = get_duracao(audio_path)

        # ── 2. Buscar mídia visual (vídeos ou fotos) ─────────────────
        media_paths = []

        if req.usar_videos_pexels:
            video_urls = await buscar_videos_pexels(req.palavras_chave, req.pexels_key, total=8)
            for i, url in enumerate(video_urls):
                dest = os.path.join(tmpdir, f"clip_{i:02d}.mp4")
                ok = await baixar_arquivo(url, dest)
                if ok:
                    media_paths.append(("video", dest))

        # Fallback para fotos se não conseguiu vídeos suficientes
        if len(media_paths) < 4:
            foto_urls = await buscar_fotos_pexels(req.palavras_chave, req.pexels_key, total=10)
            for i, url in enumerate(foto_urls):
                dest = os.path.join(tmpdir, f"foto_{i:02d}.jpg")
                ok = await baixar_arquivo(url, dest)
                if ok:
                    media_paths.append(("foto", dest))

        # ── 3. Normalizar clipes — converter tudo para segmentos MP4 ─
        W, H = req.resolucao.split(":")
        segmentos = []
        duracao_por_segmento = duracao_total / max(len(media_paths), 1)
        duracao_por_segmento = max(3.0, min(duracao_por_segmento, 12.0))

        for idx, (tipo, path) in enumerate(media_paths):
            seg_path = os.path.join(tmpdir, f"seg_{idx:02d}.mp4")

            if tipo == "video":
                dur_clip = get_duracao(path)
                # Recorta no máximo duracao_por_segmento do clipe
                dur_usar = min(dur_clip, duracao_por_segmento + req.duracao_transicao)
                cmd = [
                    "ffmpeg", "-y",
                    "-i", path,
                    "-t", str(dur_usar),
                    "-vf", (
                        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,"
                        f"format=yuv420p"
                    ),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-r", "24", "-an",
                    seg_path
                ]
            else:
                # Foto → aplica Ken Burns (zoompan) se habilitado
                if req.ken_burns:
                    # Alterna entre zoom-in e zoom-out com pan aleatório
                    zoom_dir = random.choice(["in", "out"])
                    if zoom_dir == "in":
                        zoom_expr = "min(zoom+0.0015,1.3)"
                        x_expr = "iw/2-(iw/zoom/2)"
                        y_expr = "ih/2-(ih/zoom/2)"
                    else:
                        zoom_expr = "if(lte(zoom,1.0),1.3,max(1.0,zoom-0.0015))"
                        x_expr = "iw/2-(iw/zoom/2)"
                        y_expr = "ih/2-(ih/zoom/2)"

                    n_frames = int(duracao_por_segmento * 24)
                    vf = (
                        f"scale=8000:-1,"
                        f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}'"
                        f":d={n_frames}:s={W}x{H}:fps=24,"
                        f"format=yuv420p"
                    )
                else:
                    vf = (
                        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,"
                        f"format=yuv420p"
                    )
                    n_frames = int(duracao_por_segmento * 24)

                cmd = [
                    "ffmpeg", "-y",
                    "-loop", "1", "-i", path,
                    "-t", str(duracao_por_segmento),
                    "-vf", vf,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-r", "24", "-an",
                    seg_path
                ]

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode == 0 and os.path.exists(seg_path):
                segmentos.append(seg_path)

        if not segmentos:
            raise Exception("Nenhum segmento de vídeo gerado")

        # ── 4. Concatenar com transições xfade ──────────────────────
        slideshow_path = os.path.join(tmpdir, "slideshow.mp4")

        if req.transicoes and len(segmentos) > 1:
            # Monta filter_complex com xfade encadeado
            td = req.duracao_transicao
            inputs = ""
            for seg in segmentos:
                inputs += f"-i {seg} "

            duracoes = [get_duracao(s) for s in segmentos]
            filter_parts = []
            offset = 0.0
            current_label = "[0:v]"

            for i in range(1, len(segmentos)):
                offset += duracoes[i - 1] - td
                next_label = f"[v{i}]" if i < len(segmentos) - 1 else "[vout]"
                transition = random.choice(["fade", "dissolve", "wipeleft", "wiperight", "slideleft"])
                filter_parts.append(
                    f"{current_label}[{i}:v]xfade=transition={transition}"
                    f":duration={td}:offset={offset:.3f}{next_label}"
                )
                current_label = f"[v{i}]"

            filter_complex = ";".join(filter_parts)
            cmd_concat = (
                f"ffmpeg -y {inputs}"
                f'-filter_complex "{filter_complex}" '
                f"-map [vout] -c:v libx264 -preset fast -crf 23 -r 24 "
                f"{slideshow_path}"
            )
            result = subprocess.run(cmd_concat, shell=True, capture_output=True)
            if result.returncode != 0:
                # Fallback: concat simples sem transição
                req.transicoes = False

        if not req.transicoes or len(segmentos) == 1:
            list_path = os.path.join(tmpdir, "segments.txt")
            with open(list_path, "w") as f:
                for seg in segmentos:
                    f.write(f"file '{seg}'\n")
            subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-r", "24",
                slideshow_path
            ], capture_output=True)

        # ── 5. Overlay: título + watermark ───────────────────────────
        if req.overlay_titulo or req.watermark_text:
            overlaid_path = os.path.join(tmpdir, "overlaid.mp4")
            vf_parts = []

            if req.overlay_titulo:
                titulo_safe = sanitizar_titulo(req.titulo)
                # Título aparece nos primeiros 5 segundos com fade in/out
                vf_parts.append(
                    f"drawtext=text='{titulo_safe}'"
                    f":fontsize=52:fontcolor=white@0.9"
                    f":x=(w-text_w)/2:y=h*0.75"
                    f":shadowcolor=black@0.7:shadowx=3:shadowy=3"
                    f":enable='between(t,0.5,5.5)'"
                    f":alpha='if(lt(t,1),t-0.5,if(gt(t,5),5.5-t,1))'"
                )

            if req.watermark_text:
                vf_parts.append(
                    f"drawtext=text='{req.watermark_text}'"
                    f":fontsize=22:fontcolor=white@0.45"
                    f":x=w-text_w-20:y=h-text_h-20"
                    f":shadowcolor=black@0.5:shadowx=2:shadowy=2"
                )

            vf_overlay = ",".join(vf_parts)
            result = subprocess.run([
                "ffmpeg", "-y", "-i", slideshow_path,
                "-vf", vf_overlay,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                overlaid_path
            ], capture_output=True)

            if result.returncode == 0:
                slideshow_path = overlaid_path

        # ── 6. Merge vídeo + narração (+ trilha opcional) ────────────
        video_final_path = os.path.join(tmpdir, "video_final.mp4")

        if req.trilha_url:
            trilha_path = os.path.join(tmpdir, "trilha.mp3")
            ok = await baixar_arquivo(req.trilha_url, trilha_path)

            if ok:
                # Audio ducking: trilha baixa durante narração
                v = req.volume_trilha
                cmd_audio = [
                    "ffmpeg", "-y",
                    "-i", slideshow_path,
                    "-i", audio_path,
                    "-i", trilha_path,
                    "-filter_complex",
                    (
                        f"[2:a]volume={v},aloop=loop=-1:size=2e+09[trilha];"
                        f"[1:a][trilha]amix=inputs=2:duration=first:dropout_transition=3[audio_mix]"
                    ),
                    "-map", "0:v",
                    "-map", "[audio_mix]",
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    video_final_path
                ]
                result = subprocess.run(cmd_audio, capture_output=True)
                if result.returncode != 0:
                    req.trilha_url = None  # Fallback sem trilha

        if not req.trilha_url:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", slideshow_path,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                video_final_path
            ], capture_output=True)

        # ── 7. Retornar vídeo ────────────────────────────────────────
        with open(video_final_path, "rb") as f:
            video_b64 = base64.b64encode(f.read()).decode()

        nome = re.sub(r"[^\w\-]", "_", req.titulo)[:60] + ".mp4"

        return {
            "video_base64": video_b64,
            "titulo": req.titulo,
            "nome_arquivo": nome,
            "formato": "mp4"
        }

    finally:
        # Limpeza
        for fname in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, fname))
            except Exception:
                pass
        try:
            os.rmdir(tmpdir)
        except Exception:
            pass


@app.get("/")
def health():
    return {"status": "ok", "versao": "fase-a"}


@app.get("/legal")
def legal():
    return {
        "terms_of_service": "Este serviço é de uso privado.",
        "privacy_policy": "Nenhum dado pessoal é armazenado."
    }
