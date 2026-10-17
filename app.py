import asyncio, hashlib, ipaddress, os, re, socket, threading, time
from urllib.parse import urljoin, urlparse
import requests, edge_tts
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_file, abort, send_from_directory

app = Flask(__name__)
CACHE = "/tmp/novelflow"; os.makedirs(CACHE, exist_ok=True)
CODE = os.environ.get("ACCESS_CODE", "")          # optional password
JOBS = {}                                          # id -> status dict
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"}
NEXT_RE = re.compile(r"next|下一章|下一页|下章|下一節|下一节|›|»|>>", re.I)

def check_code():
    if CODE and request.args.get("code") != CODE:
        abort(401)

def safe_url(u):
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        abort(400, "Bad link")
    for info in socket.getaddrinfo(p.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            abort(400, "Link not allowed")

def parse(url):
    safe_url(url)
    r = requests.get(url, headers=UA, timeout=20)
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")
    title = (soup.find("h1") or soup.find("title"))
    title = title.get_text(" ", strip=True) if title else url
    nxt = None
    a = soup.find("a", rel="next") or soup.find("link", rel="next")
    if a and a.get("href"):
        nxt = urljoin(url, a["href"])
    if not nxt:
        for a in soup.find_all("a", href=True):
            if NEXT_RE.search(a.get_text(" ", strip=True)) and len(a.get_text(strip=True)) < 25:
                nxt = urljoin(url, a["href"]); break
    if not nxt:  # fallback: bump the last number in the URL
        m = list(re.finditer(r"\d+", urlparse(url).path))
        if m:
            m = m[-1]; path = urlparse(url).path
            nxt = url.replace(path, path[:m.start()] + str(int(m.group()) + 1) + path[m.end():])
    for t in soup(["script", "style", "nav", "header", "footer", "form", "aside", "noscript"]):
        t.decompose()
    best, best_score = None, 0
    cands = []
    for el in soup.find_all(["div", "article", "section", "td", "main"]):
        txt = len(el.get_text(strip=True)); link = len("".join(x.get_text(strip=True) for x in el.find_all("a")))
        cands.append((el, txt, txt - 3 * link))
    if cands:
        top = max(c[2] for c in cands)
        good = [c for c in cands if c[2] >= 0.8 * top]
        best = min(good, key=lambda c: c[1])[0]
    for br in best.find_all("br") if best else []:
        br.replace_with("\n")
    text = best.get_text("\n", strip=True) if best else soup.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    return title, text, nxt

def split(text, n=2500):
    parts, buf = [], ""
    for s in re.findall(r"[^.!?。！？\n]+[.!?。！？]*\n?", text):
        if len(buf) + len(s) > n and buf:
            parts.append(buf); buf = ""
        buf += s
    if buf.strip(): parts.append(buf)
    return parts

async def tts(parts, voice, rate, path):
    with open(path + ".tmp", "wb") as f:
        for p in parts:
            if not p.strip(): continue
            async for ch in edge_tts.Communicate(p, voice, rate=rate).stream():
                if ch["type"] == "audio": f.write(ch["data"])
    os.replace(path + ".tmp", path)

def make_audio(jid, title, text, voice, rate):
    path = f"{CACHE}/{jid}.mp3"
    try:
        body = f"{title}.\n{text}"
        for attempt in range(3):
            try:
                asyncio.run(tts(split(body), voice, rate, path)); JOBS[jid]["status"] = "ready"; break
            except Exception as e:
                JOBS[jid]["error"] = str(e); time.sleep(2)
        else:
            JOBS[jid]["status"] = "error"
    finally:
        files = sorted((os.path.join(CACHE, f) for f in os.listdir(CACHE)), key=os.path.getmtime)
        for old in files[:-30]: os.remove(old)

@app.get("/")
def home(): return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")

@app.get("/api/chapter")
def chapter():
    check_code()
    url = request.args.get("url", "").strip()
    voice = request.args.get("voice", "en-US-AriaNeural")
    rate = request.args.get("rate", "+0%")
    jid = hashlib.sha1(f"{url}|{voice}|{rate}".encode()).hexdigest()[:16]
    if jid in JOBS:
        return jsonify(JOBS[jid]["info"])
    try:
        title, text, nxt = parse(url)
    except requests.RequestException:
        abort(502, "Could not open that link")
    if len(text) < 200:
        abort(422, "Could not find chapter text on that page")
    info = {"id": jid, "title": title, "next": nxt}
    JOBS[jid] = {"status": "working", "info": info}
    threading.Thread(target=make_audio, args=(jid, title, text, voice, rate), daemon=True).start()
    return jsonify(info)

@app.get("/audio/<jid>.mp3")
def audio(jid):
    check_code()
    job = JOBS.get(jid)
    if not job: abort(404)
    for _ in range(240):                          # wait up to ~2 min
        if job["status"] == "ready": break
        if job["status"] == "error": abort(502)
        time.sleep(0.5)
    else:
        abort(504)
    return send_file(f"{CACHE}/{jid}.mp3", mimetype="audio/mpeg", conditional=True)
