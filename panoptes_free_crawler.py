# ============================================================
# PANOPTES Free Crawler — API 없이 인터넷을 직접 돌아다니는 크롤러
# ============================================================
import requests
from bs4 import BeautifulSoup
import urllib.robotparser as robotparser
from urllib.parse import urljoin, urlparse
import time
import random
import json
import hashlib
from pathlib import Path
from datetime import datetime

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    print("[INFO] trafilatura 없음 — BeautifulSoup 기본 추출로 대체")

try:
    from googlenewsdecoder import new_decoderv1
    HAS_DECODER = True
except ImportError:
    HAS_DECODER = False
    print("[INFO] googlenewsdecoder 없음 — 구글뉴스 링크는 건너뜀")


def resolve_google_news_link(url):
    if "news.google.com" not in url or not HAS_DECODER:
        return url
    try:
        result = new_decoderv1(url, interval=1)
        if result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
    except Exception as e:
        print(f"[DEBUG] 구글뉴스 링크 해독 실패: {e}")
    return url

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PANOPTES-research-bot/0.1; personal portfolio research project)"
}

DATA_LAKE_PATH = "data_lake.json"
_robots_cache = {}


def is_allowed(url):
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        rp = robotparser.RobotFileParser()
        rp.set_url(urljoin(base, "/robots.txt"))
        try:
            rp.read()
        except Exception:
            rp = None
        _robots_cache[base] = rp
    rp = _robots_cache[base]
    if rp is None:
        return True
    return rp.can_fetch(HEADERS["User-Agent"], url)


def search_seed_urls(query, max_results=10, debug=True):
    import xml.etree.ElementTree as ET
    from urllib.parse import quote

    try:
        rss_url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        r = requests.get(rss_url, headers=HEADERS, timeout=10)
        if debug:
            print(f"[DEBUG] Google News RSS status={r.status_code}, 길이={len(r.text)}자")
        root = ET.fromstring(r.text)
        links = [item.find("link").text for item in root.iter("item") if item.find("link") is not None]
        links = list(dict.fromkeys(links))
        if links:
            print(f"[SEARCH] '{query}' → {len(links)}개 (Google News RSS)")
            return links[:max_results]
    except Exception as e:
        print(f"[DEBUG] Google News RSS 실패: {e}")

    try:
        naver_rss = f"https://search.naver.com/search.naver?where=rss&query={quote(query)}"
        r = requests.get(naver_rss, headers=HEADERS, timeout=10)
        root = ET.fromstring(r.text)
        links = [item.find("link").text for item in root.iter("item") if item.find("link") is not None]
        links = list(dict.fromkeys(links))
        if links:
            print(f"[SEARCH] '{query}' → {len(links)}개 (Naver RSS)")
            return links[:max_results]
    except Exception as e:
        print(f"[DEBUG] Naver RSS 실패: {e}")

    print("[FAIL] RSS 소스도 실패")
    return []


def fetch_page(url):
    if not is_allowed(url):
        print(f"[SKIP] robots.txt 금지: {url}")
        return None, []
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[ERROR] 접근 실패 {url}: {e}")
        return None, []

    if HAS_TRAFILATURA:
        text = trafilatura.extract(r.text) or ""
    else:
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(t for t in paragraphs if len(t) > 20)
        if len(text) < 200:
            text = " ".join(soup.stripped_strings)

    soup = BeautifulSoup(r.text, "html.parser")
    found_links = []
    for a in soup.find_all("a", href=True):
        abs_link = urljoin(url, a["href"])
        if abs_link.startswith("http"):
            found_links.append(abs_link)

    return text[:3000], found_links


def snowball_crawl(seed_query, keyword_filter, max_pages=15, max_depth=2, delay_range=(1.5, 3.5)):
    visited = set()
    queue = [(u, 0) for u in search_seed_urls(seed_query, max_results=8)]
    collected = []

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        if url in visited or depth > max_depth:
            continue
        visited.add(url)

        resolved_url = resolve_google_news_link(url)
        if resolved_url != url:
            print(f"[DECODE] {url[:60]}... → {resolved_url}")
        url = resolved_url

        print(f"[CRAWL depth={depth}] {url}")
        text, links = fetch_page(url)
        time.sleep(random.uniform(*delay_range))

        if not text or len(text) < 100:
            continue

        collected.append({
            "id": "web_" + hashlib.md5(url.encode()).hexdigest()[:10],
            "date": datetime.now().strftime("%Y-%m-%d"),
            "channel": "웹",
            "cluster": "자유탐색",
            "tags": [k for k in keyword_filter.split() if k in text],
            "text": text[:400],
            "source_url": url,
            "collected_at": datetime.now().isoformat(),
        })

        if depth < max_depth:
            NOISE_PATTERNS = ["articlelist", "search?", "searchword", "login", "signup", "/tag/", "/category/"]
            for link in links:
                clean_link = link.split("#")[0]
                if clean_link in visited:
                    continue
                if any(p in clean_link.lower() for p in NOISE_PATTERNS):
                    continue
                if any(k in clean_link for k in keyword_filter.split()) and clean_link not in visited:
                    queue.append((clean_link, depth + 1))

    print(f"[DONE] {len(collected)}개 페이지에서 신호 수집 완료")
    return collected


def load_lake():
    if Path(DATA_LAKE_PATH).exists():
        with open(DATA_LAKE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_merge(new_signals):
    lake = load_lake()
    seen = {s["id"] for s in lake}
    added = [s for s in new_signals if s["id"] not in seen]
    lake.extend(added)
    with open(DATA_LAKE_PATH, "w", encoding="utf-8") as f:
        json.dump(lake, f, ensure_ascii=False, indent=2)
    print(f"[SAVE] 신규 {len(added)}건 추가, 누적 총 {len(lake)}건")


if __name__ == "__main__":
    results = snowball_crawl(
        seed_query="삼성물산 신사업",
        keyword_filter="삼성물산 리테일 플랫폼 사업",
        max_pages=15,
        max_depth=2,
    )
    save_merge(results)
