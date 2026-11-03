"""Dev helper: fetch TikTok pages via plain httpx and dump the __DEFAULT_SCOPE__ hydration JSON."""
import json, re, sys, pathlib
import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
FIX = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
RE = re.compile(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)

def scope(client, url):
    r = client.get(url)
    m = RE.search(r.text)
    if not m:
        print("NO HYDRATION", url, r.status_code, len(r.text)); return None
    return json.loads(m.group(1))["__DEFAULT_SCOPE__"]

def main():
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, follow_redirects=True, timeout=20) as c:
        for name, url in [(n, u) for n, u in (a.split("=", 1) for a in sys.argv[1:])]:
            s = scope(c, url)
            if s is not None:
                (FIX / f"{name}.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))
                print("saved", name, list(s.keys()))
        print("cookies:", list(c.cookies.keys()))

if __name__ == "__main__":
    main()
