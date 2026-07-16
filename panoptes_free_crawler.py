# ============================================================
# PANOPTES Free Crawler — API 없이 인터넷을 직접 돌아다니는 크롤러
# ============================================================
# 방식:
#   1) DuckDuckGo 검색결과 HTML을 직접 긁어서 (API 키 불필요) 시드 URL 확보
#   2) 각 URL의 실제 페이지를 열어 본문 텍스트 추출
#   3) 그 페이지 안에서 관련성 높은 새 링크를 발견하면 큐에 추가 (눈덩이)
#   4) depth 제한까지 반복하며 data_lake.json에 계속 누적
#
# 지켜야 할 규칙 (이거 지켜야 오래 씀):
#   - robots.txt에서 금지한 경로는 건너뛴다
#   - 요청 사이 텀을 둔다 (서버에 부담 주지 않기 = 차단도 덜 당함)
#   - User-Agent를 명시한다 (봇인 걸 숨기지 않음)
#   - 로그인 필요한 페이지, 명시적으로 접근 제한한 페이지는 손대지 않는다
#
# 필요한 패키지: pip install requests beautifulsoup4 trafilatura --break-system-packages
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

import os

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")


def call_groq(prompt, max_tokens=500):
    """크롤러가 스스로 조사 전략을 세울 때 쓰는 범용 LLM 호출 함수"""
    if not GROQ_API_KEY:
        print("[SKIP] GROQ_API_KEY 없음 — 자율 쿼리 생성 생략")
        return None
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": max_tokens,
            },
            timeout=20,
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[ERROR] Groq 호출 실패: {e}")
        return None


def generate_investigation_queries(entity):
    """대상 하나만 주어졌을 때, 정보요원처럼 '어떤 각도로 캐야 하는지'를 스스로 브레인스토밍.
    실패하면 최소한의 기본 각도로 폴백해서 파이프라인이 죽지 않게 함."""
    prompt = f"""당신은 기업/인물/이슈를 조사하는 정보 애널리스트입니다.
조사 대상: "{entity}"

이 대상에 대해 "지금 무슨 일이 일어나고 있는지" 종합적으로 파악하려면, 어떤 검색 각도로
나눠서 캐야 할지 6~8개를 정하세요. 뉴스 표면만 훑는 게 아니라, 조직개편·재무·법적분쟁·
해외동향·기술/특허·인사 등 서로 다른 채널을 다양하게 포함하세요.

반드시 아래 형식으로만 답하세요 (다른 설명 없이, 한 줄에 하나씩):
{entity} 키워드1
{entity} 키워드2
..."""
    result = call_groq(prompt)
    if not result:
        return [f"{entity} 최근 동향", f"{entity} 신사업", f"{entity} 인사"]  # 폴백
    queries = [line.strip() for line in result.split("\n") if line.strip() and entity in line]
    print(f"[STRATEGY] '{entity}' 조사 각도 {len(queries)}개 생성: {queries}")
    return queries[:8]


def generate_followup_queries(entity, collected_signals):
    """1차 수집 결과를 보여주고 '더 파볼 곳'을 스스로 찾아내게 함 — 진짜 눈덩이식 확장의 핵심"""
    if not collected_signals:
        return []
    sample = "\n".join(f"- {s['text'][:150]}" for s in collected_signals[:15])
    prompt = f""""{entity}"에 대해 아래와 같은 신호들을 방금 수집했습니다:

{sample}

이 내용을 보고, 아직 안 다뤄졌지만 더 파볼 가치가 있는 후속 조사 각도를 2~4개만 제안하세요.
표면적으로 이미 나온 얘기 반복 말고, 이 신호들 사이의 "연결고리"나 "빠진 부분"에 주목하세요.
반드시 아래 형식으로만 답하세요:
{entity} 키워드1
{entity} 키워드2
..."""
    result = call_groq(prompt)
    if not result:
        return []
    queries = [line.strip() for line in result.split("\n") if line.strip() and entity in line]
    print(f"[FOLLOWUP] 후속 조사 각도 {len(queries)}개 생성: {queries}")
    return queries[:4]


def investigate(entity, max_pages_per_query=10, max_depth=2):
    """대상 하나를 넣으면, 스스로 조사 전략을 세우고 → 수집하고 → 결과 보고 후속 조사까지
    자동으로 이어가는 전체 파이프라인. 삼성물산이든 블랙록이든 무역이든 그대로 동작."""
    print(f"\n{'='*60}\n[INVESTIGATE START] 대상: {entity}\n{'='*60}")

    all_new_signals = []

    # 1차: 스스로 조사 각도 브레인스토밍 후 수집
    queries = generate_investigation_queries(entity)
    for q in queries:
        results = snowball_crawl(seed_query=q, keyword_filter=entity, max_pages=max_pages_per_query, max_depth=max_depth)
        all_new_signals.extend(results)

    # 2차: 방금 모은 걸 스스로 검토해서 후속 각도 생성 후 추가 수집
    followups = generate_followup_queries(entity, all_new_signals)
    for q in followups:
        results = snowball_crawl(seed_query=q, keyword_filter=entity, max_pages=max_pages_per_query, max_depth=max_depth)
        all_new_signals.extend(results)

    save_merge(all_new_signals)
    print(f"[INVESTIGATE DONE] '{entity}' 총 {len(all_new_signals)}건 신규 수집 시도")


try:
    import trafilatura  # 본문만 깔끔하게 뽑아주는 라이브러리 (있으면 사용, 없으면 BS4로 대체)
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    print("[INFO] trafilatura 없음 — BeautifulSoup 기본 추출로 대체 (설치 권장: pip install trafilatura)")

try:
    from googlenewsdecoder import new_decoderv1  # 구글뉴스 중계링크 → 실제 기사 URL 해독
    HAS_DECODER = True
except ImportError:
    HAS_DECODER = False
    print("[INFO] googlenewsdecoder 없음 — 구글뉴스 링크는 건너뜀 (설치 권장: pip install googlenewsdecoder)")


def resolve_google_news_link(url):
    """news.google.com 중계링크를 실제 발행처 URL로 변환. 다른 URL은 그대로 반환."""
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
    # HTTP 헤더는 latin-1만 허용돼서 한글을 넣으면 인코딩 에러가 남 — 반드시 영문만 사용
    "User-Agent": "Mozilla/5.0 (compatible; PANOPTES-research-bot/0.1; personal portfolio research project)"
}

DATA_LAKE_PATH = "data_lake.json"
_robots_cache = {}


# ---------------- robots.txt 확인 ----------------

def is_allowed(url):
    """해당 URL을 긁어도 되는지 robots.txt 기준으로 확인"""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        rp = robotparser.RobotFileParser()
        rp.set_url(urljoin(base, "/robots.txt"))
        try:
            rp.read()
        except Exception:
            # robots.txt를 못 읽으면 보수적으로 허용 처리 (읽기 실패는 흔함)
            rp = None
        _robots_cache[base] = rp
    rp = _robots_cache[base]
    if rp is None:
        return True
    return rp.can_fetch(HEADERS["User-Agent"], url)


# ---------------- 1단계: 검색결과에서 시드 URL 확보 (API 키 불필요) ----------------

def search_seed_urls(query, max_results=10, debug=True):
    """시드 URL 확보. 주력: Google News RSS (봇차단 없음, 키 불필요, 매우 안정적).
    검색결과 HTML 직접 스크래핑(DDG/SearXNG)은 최근 Anubis/Cloudflare 같은
    PoW 봇차단이 광범위하게 깔려서 단순 requests로는 사실상 못 뚫음 — 최후 시도로만 남겨둠."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote

    # ---- 주력: Google News RSS ----
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

    # ---- 보조: 네이버 뉴스 검색 RSS (기업 관련 국내 소스 보강용) ----
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

    print("[FAIL] RSS 소스도 실패 — 네트워크 연결 자체를 확인해볼 것")
    return []


# ---------------- 2단계: 페이지 본문 + 관련 링크 추출 ----------------

def fetch_page(url):
    """페이지 본문 텍스트와, 그 안에서 발견한 링크 목록을 함께 반환"""
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
        # <p> 태그(본문 단락)만 우선 추출 — 메뉴/네비게이션 텍스트 오염 방지
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(t for t in paragraphs if len(t) > 20)  # 너무 짧은 조각(버튼 라벨 등)은 제외
        if len(text) < 200:
            # <p> 구조가 없는 사이트면 전체 텍스트로 폴백
            text = " ".join(soup.stripped_strings)

    soup = BeautifulSoup(r.text, "html.parser")
    found_links = []
    for a in soup.find_all("a", href=True):
        abs_link = urljoin(url, a["href"])
        if abs_link.startswith("http"):
            found_links.append(abs_link)

    return text[:3000], found_links  # 본문은 3000자로 제한 (신호 추출엔 이 정도면 충분)


# ---------------- 3단계: 눈덩이 크롤 ----------------

def snowball_crawl(seed_query, keyword_filter, max_pages=15, max_depth=2, delay_range=(1.5, 3.5)):
    """
    seed_query: 첫 검색어 (예: "삼성물산 백화점")
    keyword_filter: 이 단어가 URL이나 앵커텍스트에 있어야 다음 큐에 넣음 (관련 없는 링크로 새는 것 방지)
    """
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
        time.sleep(random.uniform(*delay_range))  # 서버 부담 줄이기 + 차단 회피

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

        # 관련성 있어 보이는 링크만 다음 depth 큐에 추가 (눈덩이 확장)
        if depth < max_depth:
            NOISE_PATTERNS = ["articlelist", "search?", "searchword", "login", "signup", "/tag/", "/category/"]
            for link in links:
                clean_link = link.split("#")[0]  # #앵커는 같은 페이지이므로 제거하고 비교
                if clean_link in visited:
                    continue
                if any(p in clean_link.lower() for p in NOISE_PATTERNS):
                    continue  # 목록/검색/로그인 페이지는 콘텐츠가 아니므로 제외
                if any(k in clean_link for k in keyword_filter.split()) and clean_link not in visited:
                    queue.append((clean_link, depth + 1))

    print(f"[DONE] {len(collected)}개 페이지에서 신호 수집 완료")
    return collected


# ---------------- 저장 ----------------

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


# ---------------- 실행 ----------------

if __name__ == "__main__":
    # PANOPTES_ENTITY 환경변수로 조사 대상을 바꿀 수 있음 (기본값: 삼성물산)
    # 예: PANOPTES_ENTITY="블랙록" python panoptes_free_crawler.py
    target_entity = os.environ.get("PANOPTES_ENTITY", "삼성물산")
    investigate(target_entity, max_pages_per_query=10, max_depth=2)    queries = [line.strip() for line in result.split("\n") if line.strip() and entity in line]
    print(f"[STRATEGY] '{entity}' 조사 각도 {len(queries)}개 생성: {queries}")
    return queries[:8]


def generate_followup_queries(entity, collected_signals):
    """1차 수집 결과를 보여주고 '더 파볼 곳'을 스스로 찾아내게 함 — 진짜 눈덩이식 확장의 핵심"""
    if not collected_signals:
        return []
    sample = "\n".join(f"- {s['text'][:150]}" for s in collected_signals[:15])
    prompt = f""""{entity}"에 대해 아래와 같은 신호들을 방금 수집했습니다:

{sample}

이 내용을 보고, 아직 안 다뤄졌지만 더 파볼 가치가 있는 후속 조사 각도를 2~4개만 제안하세요.
표면적으로 이미 나온 얘기 반복 말고, 이 신호들 사이의 "연결고리"나 "빠진 부분"에 주목하세요.
반드시 아래 형식으로만 답하세요:
{entity} 키워드1
{entity} 키워드2
..."""
    result = call_groq(prompt)
    if not result:
        return []
    queries = [line.strip() for line in result.split("\n") if line.strip() and entity in line]
    print(f"[FOLLOWUP] 후속 조사 각도 {len(queries)}개 생성: {queries}")
    return queries[:4]


def investigate(entity, max_pages_per_query=10, max_depth=2):
    """대상 하나를 넣으면, 스스로 조사 전략을 세우고 → 수집하고 → 결과 보고 후속 조사까지
    자동으로 이어가는 전체 파이프라인. 삼성물산이든 블랙록이든 무역이든 그대로 동작."""
    print(f"\n{'='*60}\n[INVESTIGATE START] 대상: {entity}\n{'='*60}")

    all_new_signals = []

    # 1차: 스스로 조사 각도 브레인스토밍 후 수집
    queries = generate_investigation_queries(entity)
    for q in queries:
        results = snowball_crawl(seed_query=q, keyword_filter=entity, max_pages=max_pages_per_query, max_depth=max_depth)
        all_new_signals.extend(results)

    # 2차: 방금 모은 걸 스스로 검토해서 후속 각도 생성 후 추가 수집
    followups = generate_followup_queries(entity, all_new_signals)
    for q in followups:
        results = snowball_crawl(seed_query=q, keyword_filter=entity, max_pages=max_pages_per_query, max_depth=max_depth)
        all_new_signals.extend(results)

    save_merge(all_new_signals)
    print(f"[INVESTIGATE DONE] '{entity}' 총 {len(all_new_signals)}건 신규 수집 시도")
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    print("[INFO] trafilatura 없음 — BeautifulSoup 기본 추출로 대체 (설치 권장: pip install trafilatura)")

try:
    from googlenewsdecoder import new_decoderv1  # 구글뉴스 중계링크 → 실제 기사 URL 해독
    HAS_DECODER = True
except ImportError:
    HAS_DECODER = False
    print("[INFO] googlenewsdecoder 없음 — 구글뉴스 링크는 건너뜀 (설치 권장: pip install googlenewsdecoder)")


def resolve_google_news_link(url):
    """news.google.com 중계링크를 실제 발행처 URL로 변환. 다른 URL은 그대로 반환."""
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
    # HTTP 헤더는 latin-1만 허용돼서 한글을 넣으면 인코딩 에러가 남 — 반드시 영문만 사용
    "User-Agent": "Mozilla/5.0 (compatible; PANOPTES-research-bot/0.1; personal portfolio research project)"
}

DATA_LAKE_PATH = "data_lake.json"
_robots_cache = {}


# ---------------- robots.txt 확인 ----------------

def is_allowed(url):
    """해당 URL을 긁어도 되는지 robots.txt 기준으로 확인"""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        rp = robotparser.RobotFileParser()
        rp.set_url(urljoin(base, "/robots.txt"))
        try:
            rp.read()
        except Exception:
            # robots.txt를 못 읽으면 보수적으로 허용 처리 (읽기 실패는 흔함)
            rp = None
        _robots_cache[base] = rp
    rp = _robots_cache[base]
    if rp is None:
        return True
    return rp.can_fetch(HEADERS["User-Agent"], url)


# ---------------- 1단계: 검색결과에서 시드 URL 확보 (API 키 불필요) ----------------

def search_seed_urls(query, max_results=10, debug=True):
    """시드 URL 확보. 주력: Google News RSS (봇차단 없음, 키 불필요, 매우 안정적).
    검색결과 HTML 직접 스크래핑(DDG/SearXNG)은 최근 Anubis/Cloudflare 같은
    PoW 봇차단이 광범위하게 깔려서 단순 requests로는 사실상 못 뚫음 — 최후 시도로만 남겨둠."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote

    # ---- 주력: Google News RSS ----
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

    # ---- 보조: 네이버 뉴스 검색 RSS (기업 관련 국내 소스 보강용) ----
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

    print("[FAIL] RSS 소스도 실패 — 네트워크 연결 자체를 확인해볼 것")
    return []


# ---------------- 2단계: 페이지 본문 + 관련 링크 추출 ----------------

def fetch_page(url):
    """페이지 본문 텍스트와, 그 안에서 발견한 링크 목록을 함께 반환"""
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
        # <p> 태그(본문 단락)만 우선 추출 — 메뉴/네비게이션 텍스트 오염 방지
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(t for t in paragraphs if len(t) > 20)  # 너무 짧은 조각(버튼 라벨 등)은 제외
        if len(text) < 200:
            # <p> 구조가 없는 사이트면 전체 텍스트로 폴백
            text = " ".join(soup.stripped_strings)

    soup = BeautifulSoup(r.text, "html.parser")
    found_links = []
    for a in soup.find_all("a", href=True):
        abs_link = urljoin(url, a["href"])
        if abs_link.startswith("http"):
            found_links.append(abs_link)

    return text[:3000], found_links  # 본문은 3000자로 제한 (신호 추출엔 이 정도면 충분)


# ---------------- 3단계: 눈덩이 크롤 ----------------

def snowball_crawl(seed_query, keyword_filter, max_pages=15, max_depth=2, delay_range=(1.5, 3.5)):
    """
    seed_query: 첫 검색어 (예: "삼성물산 백화점")
    keyword_filter: 이 단어가 URL이나 앵커텍스트에 있어야 다음 큐에 넣음 (관련 없는 링크로 새는 것 방지)
    """
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
        time.sleep(random.uniform(*delay_range))  # 서버 부담 줄이기 + 차단 회피

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

        # 관련성 있어 보이는 링크만 다음 depth 큐에 추가 (눈덩이 확장)
        if depth < max_depth:
            NOISE_PATTERNS = ["articlelist", "search?", "searchword", "login", "signup", "/tag/", "/category/"]
            for link in links:
                clean_link = link.split("#")[0]  # #앵커는 같은 페이지이므로 제거하고 비교
                if clean_link in visited:
                    continue
                if any(p in clean_link.lower() for p in NOISE_PATTERNS):
                    continue  # 목록/검색/로그인 페이지는 콘텐츠가 아니므로 제외
                if any(k in clean_link for k in keyword_filter.split()) and clean_link not in visited:
                    queue.append((clean_link, depth + 1))

    print(f"[DONE] {len(collected)}개 페이지에서 신호 수집 완료")
    return collected


# ---------------- 저장 ----------------

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


# ---------------- 실행 ----------------

if __name__ == "__main__":
    # PANOPTES_ENTITY 환경변수로 조사 대상을 바꿀 수 있음 (기본값: 삼성물산)
    # 예: PANOPTES_ENTITY="블랙록" python panoptes_free_crawler.py
    target_entity = os.environ.get("PANOPTES_ENTITY", "삼성물산")
    investigate(target_entity, max_pages_per_query=10, max_depth=2)        if url in visited or depth > max_depth:
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
