"""
Transformation d'un clip repost en contenu "média" (étape 2 du plan nov. 2026).

POST /transform  { "video_url", "niche", "handle", "gemini_key", "key", "lang", "voice", "context" }
 1. télécharge le clip, extrait 3 images
 2. Gemini (images + contexte) -> hook à l'écran, voix off (1-2 phrases), légende unique, titre
 3. edge-tts -> voix off mp3
 4. ffmpeg -> hook en haut, voix off mixée (son d'origine atténué pendant la voix),
    sous-titres de la voix off, watermark @handle, sortie 1080x1920 30fps
 5. upload R2 -> { url, key, hook, voiceover, caption, title, duration }
"""

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import tempfile

import requests as http_requests

logger = logging.getLogger(__name__)

FONT_BOLD = next((p for p in [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
] if os.path.exists(p)), "")
GEMINI_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.5-flash")

VOICES = {"en": "en-US-ChristopherNeural", "fr": "fr-FR-HenriNeural"}

PROMPT = """You are the editor of a {niche_desc} short-video media account ({handle}) on TikTok/Instagram.
You see 3 frames of a short clip (original caption: "{context}").
Write, in {lang_name}:
1. "hook": an on-screen hook, max 6 words, uppercase, punchy, no emoji, no hashtags, no quotes.
2. "voiceover": 1-2 spoken sentences (max 28 words) that add real value: a fact, an identification, a story or a strong opinion — never describe the obvious. Must be true or clearly framed as a guess ("looks like"). No hashtags.
3. "caption": a unique caption for the post: first line = the hook idea rephrased, then ONE engaging question, then 5 relevant hashtags. Max 220 characters.
4. "title": 3-5 words title case.
Return STRICT JSON with exactly these 4 keys."""

NICHE_DESC = {
    "car": "car culture (supercars, muscle cars, JDM, engines)",
    "humour": "French comedy and funny clips",
    "geek": "gaming, anime and tech",
}


def _run(cmd, timeout=300):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-600:]}")
    return r


def _duration(path):
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], 30)
    return float(r.stdout.strip())


def _frames(video, tmp, dur):
    out = []
    for i, frac in enumerate((0.1, 0.5, 0.9)):
        p = os.path.join(tmp, f"frame{i}.jpg")
        _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, dur * frac):.2f}", "-i", video,
              "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", p], 60)
        out.append(p)
    return out


def _gemini(frames, niche, handle, context, lang, key):
    parts = [{"text": PROMPT.format(niche_desc=NICHE_DESC.get(niche, niche), handle=handle,
                                     context=(context or "")[:200].replace('"', "'"),
                                     lang_name="French" if lang == "fr" else "English")}]
    for f in frames:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(open(f, "rb").read()).decode()}})
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"}}
    r = http_requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}",
        json=body, timeout=90)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    data = json.loads(text)
    for k in ("hook", "voiceover", "caption", "title"):
        if not isinstance(data.get(k), str) or not data[k].strip():
            raise RuntimeError(f"Gemini: missing {k}")
    data["hook"] = re.sub(r"[\"'#*]", "", data["hook"]).strip().upper()[:40]
    data["voiceover"] = data["voiceover"].strip()
    data["caption"] = data["caption"].strip()[:300]
    return data


def _tts_edge(text, voice, path):
    import edge_tts

    async def go():
        comm = edge_tts.Communicate(text, voice, rate="+6%", boundary="SentenceBoundary")
        sents = []
        with open(path, "wb") as f:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "SentenceBoundary":
                    sents.append({"t": chunk["offset"] / 1e7, "d": chunk["duration"] / 1e7, "text": chunk["text"]})
        return sents

    return asyncio.run(go())


def _tts_gemini(text, lang, key, path):
    """Secours : Gemini TTS (PCM 24 kHz) -> wav ; timings de phrases estimés au prorata des caractères."""
    body = {"contents": [{"parts": [{"text": ("Read this with a confident documentary tone: " if lang == "en"
                                              else "Lis ceci avec un ton documentaire assuré : ") + text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Charon"}}}}}
    r = http_requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash-lite-tts:generateContent?key={key}",
        json=body, timeout=90)
    r.raise_for_status()
    part = next(p for p in r.json()["candidates"][0]["content"]["parts"] if "inlineData" in p)
    pcm = base64.b64decode(part["inlineData"]["data"])
    raw = path + ".pcm"
    open(raw, "wb").write(pcm)
    _run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", raw, path], 60)
    total = _duration(path)
    sents_txt = [s for s in re.split(r"(?<=[.!?])\s+", text) if s]
    n = sum(len(s) for s in sents_txt) or 1
    sents, t = [], 0.0
    for s in sents_txt:
        d = total * len(s) / n
        sents.append({"t": t, "d": d, "text": s}); t += d
    return sents


def _tts(text, voice, path, lang="en", key=""):
    try:
        return _tts_edge(text, voice, path)
    except Exception as e:
        logger.warning(f"edge-tts failed ({str(e)[:120]}), falling back to Gemini TTS")
        return _tts_gemini(text, lang, key, path)


def _ff_escape(s):
    return s.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "\\%").replace(",", "\\,")


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def transform(video_url, niche, handle, gemini_key, r2_key, lang="en", voice=None, context="",
              upload_url=None, upload_secret=None, skip_upload=False):
    voice = voice or VOICES.get(lang, VOICES["en"])
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.mp4")
        with http_requests.get(video_url, stream=True, timeout=180,
                               headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}) as r:
            r.raise_for_status()
            with open(src, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        dur = _duration(src)
        frames = _frames(src, tmp, dur)
        meta = _gemini(frames, niche, handle, context, lang, gemini_key)

        vo = os.path.join(tmp, "vo.mp3")
        sents = _tts(meta["voiceover"], voice, vo, lang=lang, key=gemini_key)
        vo_dur = _duration(vo)
        vo_start = 0.6  # la voix démarre après 0,6 s
        has_audio = bool(_run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                               "-show_entries", "stream=codec_type", "-of", "csv=p=0", src], 30).stdout.strip())

        # --- filtres vidéo : fond 1080x1920, hook haut, sous-titres voix off, watermark
        vf = ["scale=1080:1920:force_original_aspect_ratio=increase", "crop=1080:1920", "fps=30", "setsar=1"]
        if FONT_BOLD:
            font = f"fontfile='{FONT_BOLD.replace(':', chr(92) + ':')}':"
            # hook : 2 lignes max, bandeau haut, visible 4,5 s
            hook_lines = _wrap(meta["hook"], 16)[:2]
            for i, line in enumerate(hook_lines):
                vf.append(f"drawtext={font}text='{_ff_escape(line)}':fontsize=84:fontcolor=white:borderw=6:bordercolor=black:"
                          f"x=(w-text_w)/2:y={300 + i * 100}:enable='between(t,0.3,4.8)'")
            # sous-titres de la voix off (par phrase), bas, 2 lignes
            for s in sents:
                # blocs de ~7 mots (2 lignes max), durée au prorata des caractères
                words = s["text"].split()
                chunks = [" ".join(words[i:i + 7]) for i in range(0, len(words), 7)]
                total = sum(len(c) for c in chunks) or 1
                t = vo_start + s["t"]
                for c in chunks:
                    d = s["d"] * len(c) / total
                    st, en = t, t + d
                    t += d
                    for i, line in enumerate(_wrap(c, 30)[:2]):
                        vf.append(f"drawtext={font}text='{_ff_escape(line)}':fontsize=52:fontcolor=white:borderw=5:bordercolor=black:"
                                  f"x=(w-text_w)/2:y={1480 + i * 68}:enable='between(t,{st:.2f},{en:.2f})'")
            # watermark
            vf.append(f"drawtext={font}text='{_ff_escape(handle)}':fontsize=34:fontcolor=0xFFC832:borderw=3:bordercolor=black:"
                      f"x=(w-text_w)/2:y=1740")
        vf_str = ",".join(vf)

        # --- audio : original atténué pendant la voix, voix off à vo_start
        delay_ms = int(vo_start * 1000)
        if has_audio:
            duck_end = vo_start + vo_dur + 0.3
            af = (f"[0:a]volume='if(between(t,{vo_start - 0.3:.2f},{duck_end:.2f}),0.25,1)':eval=frame[orig];"
                  f"[1:a]adelay={delay_ms}|{delay_ms},volume=1.4[vo];"
                  f"[orig][vo]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=false[a]")
        else:
            af = f"[1:a]adelay={delay_ms}|{delay_ms},volume=1.4,apad[a]"

        out = os.path.join(tmp, "out.mp4")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-i", vo,
               "-filter_complex", af, "-map", "0:v", "-map", "[a]", "-vf", vf_str,
               "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", "-threads", "2", out]
        _run(cmd, 600)
        size_mb = os.path.getsize(out) / 1e6
        result = {"hook": meta["hook"], "voiceover": meta["voiceover"], "caption": meta["caption"],
                  "title": meta["title"], "duration": round(dur, 1), "size_mb": round(size_mb, 1), "key": r2_key}
        if skip_upload:
            keep = os.path.join(tempfile.gettempdir(), os.path.basename(r2_key))
            os.replace(out, keep)
            result["local_path"] = keep
            return result
        with open(out, "rb") as f:
            r = http_requests.post(f"{upload_url}?key={r2_key}",
                                   headers={"Authorization": f"Bearer {upload_secret}", "Content-Type": "video/mp4"},
                                   data=f, timeout=600)
        r.raise_for_status()
        result["url"] = r.json()["url"]
        return result
