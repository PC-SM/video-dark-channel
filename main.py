from fastapi import FastAPI
from pydantic import BaseModel
import httpx
import base64
import tempfile
import os
import subprocess
from typing import List

app = FastAPI()

class VideoRequest(BaseModel):
    audio_base64: str
    palavras_chave: List[str]
    titulo: str
    pexels_key: str

@app.post("/montar")
async def montar(req: VideoRequest):
    tmpdir = tempfile.mkdtemp()
    
    audio_path = os.path.join(tmpdir, "narration.mp3")
    with open(audio_path, "wb") as f:
        f.write(base64.b64decode(req.audio_base64))

    async with httpx.AsyncClient() as client:
        imagens = []
        for kw in req.palavras_chave[:3]:
            resp = await client.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": req.pexels_key},
                params={"query": kw, "per_page": 5, "orientation": "landscape"}
            )
            data = resp.json()
            for photo in data.get("photos", []):
                imagens.append(photo["src"]["large"])
            if len(imagens) >= 10:
                break

    img_paths = []
    async with httpx.AsyncClient() as client:
        for idx, url in enumerate(imagens[:10]):
            img_resp = await client.get(url)
            img_path = os.path.join(tmpdir, f"img_{idx}.jpg")
            with open(img_path, "wb") as f:
                f.write(img_resp.content)
            img_paths.append(img_path)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True
    )
    duracao = float(probe.stdout.strip())
    tempo_por_imagem = duracao / len(img_paths)

    list_path = os.path.join(tmpdir, "images.txt")
    with open(list_path, "w") as f:
        for img_path in img_paths:
            f.write(f"file '{img_path}'\n")
            f.write(f"duration {tempo_por_imagem}\n")
        f.write(f"file '{img_paths[-1]}'\n")

    slideshow_path = os.path.join(tmpdir, "slideshow.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
        "-c:v", "libx264", "-r", "24", slideshow_path
    ], capture_output=True)

    video_path = os.path.join(tmpdir, "video_final.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-i", slideshow_path,
        "-i", audio_path,
        "-c:v", "copy", "-c:a", "aac",
        "-shortest", video_path
    ], capture_output=True)

    with open(video_path, "rb") as f:
        video_b64 = base64.b64encode(f.read()).decode()

    for f in os.listdir(tmpdir):
        os.remove(os.path.join(tmpdir, f))
    os.rmdir(tmpdir)

    return {
        "video_base64": video_b64,
        "titulo": req.titulo,
        "formato": "mp4"
    }

@app.get("/")
def health():
    return {"status": "ok"}
