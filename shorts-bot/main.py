"""Daily streamer-news YouTube Shorts bot.

Pipeline: real news sources -> Gemini script -> Edge TTS voice -> Pexels
background -> FFmpeg with burned-in captions -> YouTube upload.
"""
import asyncio
import datetime as dt
import json
import os
import random
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import edge_tts
import requests

ROOT = Path(__file__).parent
WORK = ROOT / "work"
HISTORY = ROOT / "history.json"
UA = {"User-Agent": "Mozilla/5.0 (shorts-bot)"}

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
PEXELS_KEY = os.environ["PEXELS_API_KEY"]
VOICE = os.environ.get("TTS_VOICE", "en-US-AndrewNeural")
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # build video but skip upload

FORMATS = [
    "breaking news recap: what happened, why it matters",
    "'things you didn't know' about the streamer(s) involved, using only facts in the sources",
    "records, numbers and stats angle",
    "drama recap told like a story with a twist at the end",
    "rise-to-fame angle: how the streamer got here",
]


# ---------- 1. Sources ----------
def fetch_reddit():
    items = []
    for sub in ("LivestreamFail", "Twitch", "KickStreaming"):
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=15",
                             headers=UA, timeout=15)
            r.raise_for_status()
            for c in r.json()["data"]["children"]:
                d = c["data"]
                items.append({"title": d["title"], "url": "https://reddit.com" + d["permalink"],
                              "text": d.get("selftext", "")[:800], "score": d.get("score", 0)})
        except Exception as e:
            print(f"reddit {sub} failed: {e}")
    return items


def fetch_google_news():
    items = []
    for q in ("twitch streamer", "kick streamer", "youtube streamer", "streamer record"):
        try:
            url = f"https://news.google.com/rss/search?q={requests.utils.quote(q)}+when:2d&hl=en-US&gl=US&ceid=US:en"
            root = ET.fromstring(requests.get(url, headers=UA, timeout=15).content)
            for it in root.iter("item"):
                items.append({"title": it.findtext("title"), "url": it.findtext("link"),
                              "text": it.findtext("description", "")[:800], "score": 0})
        except Exception as e:
            print(f"news '{q}' failed: {e}")
    return items


def load_history():
    return json.loads(HISTORY.read_text()) if HISTORY.exists() else []


# ---------- 2. Script ----------
def gemini(prompt):
    r = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}},
        timeout=120)
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def write_script(items, history):
    used = {h["url"] for h in history} | {h["title"].lower() for h in history}
    fresh = [i for i in items if i["url"] not in used and i["title"].lower() not in used]
    fresh.sort(key=lambda i: i["score"], reverse=True)
    if not fresh:
        sys.exit("No fresh sources today.")
    fmt = FORMATS[dt.date.today().toordinal() % len(FORMATS)]
    recent = [h["topic"] for h in history[-30:]]
    sources = "\n".join(f"[{n}] {i['title']} | {i['text']} | {i['url']}"
                        for n, i in enumerate(fresh[:40]))
    prompt = f"""You write viral YouTube Shorts about streamers (Twitch, Kick, YouTube).
Pick ONE story from the sources below that is interesting and NOT about these recent topics: {recent}

STRICT RULES:
- Use ONLY facts stated in the sources. Never invent quotes, numbers, or events.
- If something is a rumor, say "reportedly". No insults or unproven accusations.
- Format for today: {fmt}
- Script: 110-150 words (~45 seconds spoken). First sentence is a scroll-stopping hook.
  Short punchy sentences. No emojis, no hashtags, no stage directions.
  End with a line that loops back to the hook or asks viewers to comment.
- Title: under 70 chars, curiosity-driven, no clickbait lies. Add " #shorts".

Return JSON: {{"source_index": int, "topic": "short topic label",
"title": str, "script": str, "description": str (2-3 sentences + source link),
"tags": [10 strings], "search_terms": [4 short stock-footage queries like "gaming setup neon"]}}

SOURCES:
{sources}"""
    data = gemini(prompt)
    data["source"] = fresh[data["source_index"]]
    return data


# ---------- 3. Voice + captions ----------
async def tts(text, mp3):
    words = []
    comm = edge_tts.Communicate(text, VOICE, rate="+8%", boundary="WordBoundary")
    with open(mp3, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append((chunk["offset"] / 1e7, (chunk["offset"] + chunk["duration"]) / 1e7,
                              chunk["text"]))
    return words


def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)])
    return float(out)


def ts(s):
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02}:{s % 60:05.2f}"


def write_ass(words, total, path):
    if not words:  # fallback: spread evenly
        toks = WORK.joinpath("script.txt").read_text().split()
        step = total / len(toks)
        words = [(i * step, (i + 1) * step, w) for i, w in enumerate(toks)]
    head = """[Script Info]
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginV
Style: Main,DejaVu Sans,96,&H00FFFFFF,&H00000000,&H80000000,1,1,7,3,5,0

[Events]
Format: Layer, Start, End, Style, Text
"""
    lines = []
    for i in range(0, len(words), 2):  # 2 words per caption
        grp = words[i:i + 2]
        end = words[i + 2][0] if i + 2 < len(words) else total
        text = " ".join(w[2] for w in grp).upper()
        color = "{\\c&H00F0FF&}" if i % 4 == 0 else ""  # alternate yellow/white
        lines.append(f"Dialogue: 0,{ts(grp[0][0])},{ts(end)},Main,{color}{{\\fscx110\\fscy110\\t(0,80,\\fscx100\\fscy100)}}{text}")
    path.write_text(head + "\n".join(lines), encoding="utf-8")


# ---------- 4. Background footage ----------
def pexels_clips(terms, n=5):
    urls = []
    for q in terms + ["gaming setup", "neon city night", "esports"]:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": PEXELS_KEY},
                         params={"query": q, "orientation": "portrait", "per_page": 6}, timeout=20)
        for v in r.json().get("videos", []):
            files = [f for f in v["video_files"] if (f.get("height") or 0) >= 1280]
            if files:
                urls.append(min(files, key=lambda f: f["height"])["link"])
                break
        if len(urls) >= n:
            break
    paths = []
    for k, u in enumerate(urls):
        p = WORK / f"raw{k}.mp4"
        p.write_bytes(requests.get(u, timeout=120).content)
        paths.append(p)
    return paths


def build_video(clips, total, audio, ass, out):
    seg = total / len(clips) + 0.5
    parts = []
    for k, c in enumerate(clips):
        p = WORK / f"part{k}.mp4"
        subprocess.run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(c), "-t", f"{seg:.2f}",
                        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,eq=brightness=-0.08",
                        "-an", "-c:v", "libx264", "-preset", "veryfast", str(p)], check=True)
        parts.append(p)
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
    bg = WORK / "bg.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(bg)],
                   check=True)
    music = ROOT / "music.mp3"
    cmd = ["ffmpeg", "-y", "-i", str(bg), "-i", str(audio)]
    if music.exists():
        cmd += ["-stream_loop", "-1", "-i", str(music), "-filter_complex",
                "[2:a]volume=0.12[m];[1:a][m]amix=inputs=2:duration=first[a]", "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-vf", f"ass={ass.name}", "-t", f"{total + 0.3:.2f}", "-c:v", "libx264", "-preset", "medium",
            "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, check=True, cwd=WORK)


# ---------- 5. Upload ----------
def upload(path, meta):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"])
    yt = build("youtube", "v3", credentials=creds)
    body = {"snippet": {"title": meta["title"][:100],
                        "description": f"{meta['description']}\n\nSource: {meta['source']['url']}",
                        "tags": meta["tags"], "categoryId": "20"},
            "status": {"privacyStatus": os.environ.get("YT_PRIVACY", "public"),
                       "selfDeclaredMadeForKids": False}}
    res = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), resumable=True)).execute()
    print("Uploaded: https://youtube.com/shorts/" + res["id"])
    return res["id"]


def main():
    WORK.mkdir(exist_ok=True)
    history = load_history()
    items = fetch_reddit() + fetch_google_news()
    print(f"{len(items)} source items")
    meta = write_script(items, history)
    print("Topic:", meta["topic"], "\nTitle:", meta["title"], "\n", meta["script"])
    (WORK / "script.txt").write_text(meta["script"], encoding="utf-8")

    audio = WORK / "voice.mp3"
    words = asyncio.run(tts(meta["script"], audio))
    total = duration(audio)
    ass = WORK / "subs.ass"
    write_ass(words, total, ass)

    clips = pexels_clips(meta["search_terms"])
    random.shuffle(clips)
    out = WORK / "final.mp4"
    build_video(clips, total, audio, ass, out)

    vid = None if DRY_RUN else upload(out, meta)
    history.append({"date": str(dt.date.today()), "topic": meta["topic"], "title": meta["source"]["title"],
                    "url": meta["source"]["url"], "video": vid})
    HISTORY.write_text(json.dumps(history, indent=1))


if __name__ == "__main__":
    main()
