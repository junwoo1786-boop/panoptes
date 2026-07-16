# ============================================================
# PANOPTES Compressor — 오래된 원본 신호를 압축된 요약으로 재작성
# ============================================================
# GitHub Actions에서만 실행됨 (Termux 필요 없음).
# 흐름:
#   1) data_lake.json 로드
#   2) COMPRESS_AFTER_DAYS(기본 14일)보다 오래된 "원본(raw)" 신호를 클러스터별로 묶음
#   3) 클러스터당 신호가 2건 이상이면 LLM으로 한 문단 요약 생성
#   4) 원본 여러 건 → 압축 신호 1건으로 교체 (원본 URL 목록은 보존, 본문만 압축)
#   5) 최근 신호·이미 압축된 신호는 그대로 둠
# ============================================================

import json
import os
import requests
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

DATA_LAKE_PATH = "data_lake.json"
COMPRESS_AFTER_DAYS = 14
MIN_SIGNALS_TO_COMPRESS = 2  # 이보다 적으면 압축할 가치가 없으므로 그냥 둠
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")


def load_lake():
    if Path(DATA_LAKE_PATH).exists():
        with open(DATA_LAKE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_lake(signals):
    with open(DATA_LAKE_PATH, "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False, indent=2)


def parse_date(d):
    """date 필드가 다양한 형식으로 들어와서(예: '2026-11(전기)') 최대한 관대하게 파싱"""
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d")
    except Exception:
        return datetime.now()  # 파싱 실패하면 "최신"으로 간주해 압축 대상에서 제외


def call_groq_summarize(cluster_name, signals):
    """클러스터 안의 여러 원본 신호를 하나의 압축 요약으로 변환"""
    if not GROQ_API_KEY:
        print("[SKIP] GROQ_API_KEY 없음 — 압축 생략")
        return None

    combined = "\n".join(f"- ({s['date']}) {s['text'][:300]}" for s in signals)
    prompt = f"""다음은 '{cluster_name}' 주제로 묶인 공개 신호 {len(signals)}건입니다. 이걸 핵심만 남긴 한 문단(3~5문장)으로 압축하세요.
날짜별 세부사항보다 "이 기간 동안 전체적으로 어떤 흐름이었는가"에 집중하세요. 없는 사실은 지어내지 마세요. 한국어로만 답하세요.

{combined}"""

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "max_tokens": 300,
            },
            timeout=20,
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[ERROR] Groq 압축 실패 ({cluster_name}): {e}")
        return None


def compress_lake():
    lake = load_lake()
    cutoff = datetime.now() - timedelta(days=COMPRESS_AFTER_DAYS)

    old_raw = defaultdict(list)  # cluster -> [signals]
    keep = []  # 압축 안 하고 그대로 둘 신호들

    for s in lake:
        if s.get("compressed"):
            keep.append(s)  # 이미 압축된 건 다시 안 건드림
            continue
        if parse_date(s.get("date", "")) < cutoff:
            old_raw[s.get("cluster", "기타")].append(s)
        else:
            keep.append(s)  # 아직 최근 것이면 원본 그대로 유지

    compressed_count = 0
    raw_count_before = sum(len(v) for v in old_raw.values())

    for cluster, sigs in old_raw.items():
        if len(sigs) < MIN_SIGNALS_TO_COMPRESS:
            keep.extend(sigs)  # 압축할 만큼 안 모였으면 원본 유지
            continue

        summary = call_groq_summarize(cluster, sigs)
        if summary is None:
            keep.extend(sigs)  # 압축 실패하면 원본이라도 보존 (데이터 유실 방지)
            continue

        dates = [s.get("date", "") for s in sigs]
        keep.append({
            "id": f"compressed_{cluster}_{sigs[0]['id'][:8]}",
            "date": f"{min(dates)}~{max(dates)}",
            "channel": "압축",
            "cluster": cluster,
            "tags": list(set(t for s in sigs for t in s.get("tags", []))),
            "text": summary,
            "source_count": len(sigs),
            "source_urls": [s.get("source_url", "") for s in sigs],
            "compressed": True,
            "collected_at": datetime.now().isoformat(),
        })
        compressed_count += 1
        print(f"[COMPRESS] '{cluster}' 원본 {len(sigs)}건 → 압축 1건")

    save_lake(keep)
    print(f"\n[DONE] 압축 전 원본 {raw_count_before}건 → 압축 {compressed_count}건 생성")
    print(f"[SAVE] 최종 총 {len(keep)}건 (원본 유지분 + 압축분 + 최근분)")


if __name__ == "__main__":
    compress_lake()
