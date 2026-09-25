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
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"


def gemini_models():
    """Ask Google which text models this key can use; newest 'flash' first."""
    r = requests.get(f"{GEMINI_API}/models", headers={"x-goog-api-key": GEMINI_KEY},
                     params={"pageSize": 200}, timeout=30)
    r.raise_for_status()
    skip = ("image", "tts", "audio", "live", "embedding", "vision", "thinking", "learnlm", "gemma")
    names = [m["name"] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])
             and "gemini" in m["name"] and not any(s in m["name"] for s in skip)]
    rank = lambda n: ("flash" in n, "lite" not in n, "preview" not in n and "exp" not in n, n)
    return sorted(names, key=rank, reverse=True)


def gemini(prompt):
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}}
    models = gemini_models()
    print("Gemini models available:", models[:5])
    for model in models[:6]:
        r = requests.post(f"{GEMINI_API}/{model}:generateContent",
                          headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=120)
        if r.ok:
            print("Using", model)
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        print(f"{model} failed: {r.status_code} {r.text[:200]}")
    sys.exit("No Gemini model worked - check GEMINI_API_KEY.")


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
- Script: 70-90 words (~28 seconds spoken). First sentence is a scroll-stopping hook.
  Short punchy sentences. No emojis, no hashtags, no stage directions.
  End with a line that loops back to the hook or asks viewers to comment.
- Title: under 70 chars, curiosity-driven, no clickbait lies. Add " #shorts".

Return JSON: {{"source_index": int, "topic": "short topic label",
"title": str, "script": str, "description": str (2 sentences, no links),
"hook_text": "max 5 words shown big on screen at the start",
"streamers": [Twitch usernames of the streamers in the story, lowercase, e.g. "kaicenat"],
"hashtags": [4 topic hashtags like "#kaicenat"],
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


def ass_escape(s):
    return s.replace("{", "(").replace("}", ")").replace("\\", "/")


def write_ass(words, total, path, hook_text, credits):
    """credits: list of (start, end, text) shown at the bottom while a clip plays."""
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
Style: Hook,DejaVu Sans,84,&H00FFFFFF,&H000000FF,&H00000000,1,3,6,0,8,260
Style: Credit,DejaVu Sans,40,&H00FFFFFF,&H00000000,&H90000000,1,3,4,0,2,230

[Events]
Format: Layer, Start, End, Style, Text
"""
    lines = []
    if hook_text:  # big red-boxed hook for the first 2.5s
        lines.append(f"Dialogue: 1,{ts(0)},{ts(min(2.5, total))},Hook,"
                     f"{{\\fad(0,200)\\fscx60\\fscy60\\t(0,150,\\fscx100\\fscy100)}}{ass_escape(hook_text.upper())}")
    for s, e, text in credits:
        lines.append(f"Dialogue: 1,{ts(s)},{ts(e)},Credit,{ass_escape(text)}")
    for i in range(0, len(words), 2):  # 2 words per caption
        grp = words[i:i + 2]
        end = words[i + 2][0] if i + 2 < len(words) else total
        text = ass_escape(" ".join(w[2] for w in grp).upper())
        color = "{\\c&H00F0FF&}" if i % 4 == 0 else ""  # alternate yellow/white
        lines.append(f"Dialogue: 0,{ts(grp[0][0])},{ts(end)},Main,{color}{{\\fscx110\\fscy110\\t(0,80,\\fscx100\\fscy100)}}{text}")
    path.write_text(head + "\n".join(lines), encoding="utf-8")


# ---------- 4. Footage ----------
def twitch_clips(streamers, n=3):
    """Top Twitch clips of the streamers in the story (no API keys needed) -> [(path, credit)]."""
    urls = []
    for name in streamers[:3]:
        for rng in ("7d", "30d"):
            r = subprocess.run(["yt-dlp", "--flat-playlist", "--playlist-end", "3", "--print", "url",
                                f"https://www.twitch.tv/{name}/clips?filter=clips&range={rng}"],
                               capture_output=True, text=True, timeout=120)
            found = [u for u in r.stdout.split() if u.startswith("http")]
            if found:
                urls += [(u, name) for u in found]
                break
            print(f"no clips for {name} ({rng}):", r.stderr.strip()[-200:])
    # round-robin: best clip of each streamer first, then second-best, ...
    rank = {}
    for i, (u, name) in enumerate(urls):
        rank[u] = sum(1 for _, n in urls[:i] if n == name)
    urls.sort(key=lambda x: rank[x[0]])
    out = []
    for u, name in urls:
        p = WORK / f"twitch{len(out)}.mp4"
        r = subprocess.run(["yt-dlp", "-q", "--no-part", "-f", "b", "-o", str(p), u], timeout=300)
        if r.returncode == 0 and p.exists():
            out.append((p, f"Clip: twitch.tv/{name}"))
            print("twitch clip:", u)
        if len(out) >= n:
            break
    return out


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


ZOOM = "zoompan=z='min(zoom+0.0012,1.12)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30"
FLASH = "fade=t=in:st=0:d=0.15:color=white"


def render_part(src, wide, seg, dst):
    """One segment: slow push-in zoom + white flash on the cut. Wide (16:9) clips sit
    centered over a blurred copy of themselves instead of being cropped."""
    if wide:
        fc = ("[0:v]fps=30,split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,"
              "crop=1080:1920,boxblur=20:2,eq=brightness=-0.15[bg];[b]scale=1080:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{ZOOM},{FLASH}[v]")
    else:
        fc = (f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,"
              f"eq=brightness=-0.08,{ZOOM},{FLASH}[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(src), "-t", f"{seg:.2f}",
                    "-filter_complex", fc, "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast",
                    str(dst)], check=True)


def make_whoosh(path):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=0.45:c=pink:a=0.5",
                    "-af", "highpass=f=500,lowpass=f=5000,afade=t=in:d=0.2,afade=t=out:st=0.2:d=0.25",
                    str(path)], check=True)


MUSIC_MOODS = ["upbeat", "energetic", "electronic", "hip hop", "action", "epic",
               "dramatic", "trailer", "funky", "edm"]


def pick_music():
    """Find a fresh free track online (Openverse: CC0 / CC-BY only, safe for YouTube with
    credit). Falls back to any mp3s in a local music/ folder. -> (path, credit) or (None, None)"""
    mood = random.choice(MUSIC_MOODS)
    try:
        r = requests.get("https://api.openverse.org/v1/audio/", headers=UA, timeout=30, params={
            "q": mood, "license": "cc0,by", "category": "music", "page_size": 20})
        r.raise_for_status()
        tracks = [t for t in r.json().get("results", [])
                  if (t.get("duration") or 0) >= 40000 and t.get("url")]
        random.shuffle(tracks)
        for t in tracks[:5]:
            try:
                data = requests.get(t["url"], headers=UA, timeout=60).content
                if len(data) < 100_000:
                    continue
                p = WORK / "music_dl"
                p.write_bytes(data)
                duration(p)  # make sure ffmpeg can read it
                lic = f"CC {t['license'].upper()} {t.get('license_version') or ''}".strip()
                print(f"Music ({mood}): {t['title']} by {t['creator']}")
                return p, f"Music: \"{t['title']}\" by {t['creator']} ({lic}) {t.get('foreign_landing_url', '')}"
            except Exception as e:
                print("track failed:", e)
    except Exception as e:
        print("music search failed:", e)
    local = sorted((ROOT / "music").glob("*.mp3"))
    return (random.choice(local), None) if local else (None, None)


def build_video(segments, seg, total, audio, ass, out, music=None):
    """segments: [(path, wide)] each shown for `seg` seconds."""
    parts = []
    for k, (src, wide) in enumerate(segments):
        p = WORK / f"part{k}.mp4"
        render_part(src, wide, seg + 0.1, p)
        parts.append(p)
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
    bg = WORK / "bg.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", str(bg)], check=True)

    whoosh = WORK / "whoosh.wav"
    make_whoosh(whoosh)
    cuts = [k * seg for k in range(1, len(segments))]
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(bg), "-i", str(audio)]
    for _ in cuts:
        cmd += ["-i", str(whoosh)]
    fc = []
    mix = ["[1:a]"]
    for n, t in enumerate(cuts):
        ms = max(0, int((t - 0.2) * 1000))  # whoosh leads into the cut
        fc.append(f"[{n + 2}:a]adelay={ms}|{ms},volume=0.5[w{n}]")
        mix.append(f"[w{n}]")
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        fc.append(f"[{len(cuts) + 2}:a]volume=0.13,afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[m]")
        mix.append("[m]")
    fc.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[a]")
    fc.append(f"[0:v]ass={ass.name}[v]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]",
            "-t", f"{total + 0.3:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", str(out)]
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
    tags = [t if t.startswith("#") else "#" + t for t in meta.get("hashtags", [])]
    hashtags = " ".join(dict.fromkeys(["#shorts", "#viral", "#streamer", "#twitch", "#fyp"] +
                                      [t.replace(" ", "") for t in tags]))
    credits = "\n".join(meta.get("credits", [])) or "Footage: Pexels"
    desc = (f"{meta['description']}\n\nCredits:\n{credits}\nNews source: {meta['source']['url']}\n\n"
            f"All clips belong to their respective creators.\n\n{hashtags}")
    body = {"snippet": {"title": meta["title"][:100],
                        "description": desc[:4900],
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

    # Real Twitch clips of the streamers first, stock footage fills the rest.
    tw = twitch_clips([s.lower().strip() for s in meta.get("streamers", [])])
    stock = pexels_clips(meta["search_terms"], n=max(2, 6 - len(tw)))
    random.shuffle(stock)
    segments = [(p, True) for p, _ in tw] + [(p, False) for p in stock]
    segments = segments[:6]
    seg = total / len(segments)
    credit_marks = [(k * seg, (k + 1) * seg, c) for k, (_, c) in enumerate(tw)]
    music, music_credit = pick_music()
    meta["credits"] = ([c for _, c in tw] + (["Stock footage: Pexels"] if stock else [])
                       + ([music_credit] if music_credit else []))

    ass = WORK / "subs.ass"
    write_ass(words, total, ass, meta.get("hook_text", ""), credit_marks)
    out = WORK / "final.mp4"
    build_video(segments, seg, total, audio, ass, out, music)

    vid = None if DRY_RUN else upload(out, meta)
    history.append({"date": str(dt.date.today()), "topic": meta["topic"], "title": meta["source"]["title"],
                    "url": meta["source"]["url"], "video": vid})
    HISTORY.write_text(json.dumps(history, indent=1))


if __name__ == "__main__":
    main()
