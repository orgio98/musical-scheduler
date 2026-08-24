# -*- coding: utf-8 -*-
"""
GitHub Actions 자동 실행용 통합 크롤러
========================================
매일 스케줄에 따라 자동으로 실행되어
  1) KOPIS 뮤지컬 일정 수집
  2) 공연중인 공연의 캐스팅 이미지 + 할인 문구 수집
  3) 캐스팅 이미지를 Claude API로 판독해 날짜별 캐스트로 변환
을 순서대로 처리하고, 결과를 docs/data/*.json 으로 저장합니다.
GitHub Pages가 docs/ 를 그대로 서빙하므로, 커밋되는 순간 웹앱에 반영됩니다.

필요한 저장소 시크릿(Settings > Secrets and variables > Actions):
  - KOPIS_SERVICE_KEY   (필수) kopis.or.kr 발급 인증키
  - ANTHROPIC_API_KEY   (선택) console.anthropic.com 발급 키.
                        없으면 캐스팅 이미지 판독 단계만 건너뜁니다.
"""

import base64
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import requests

SERVICE_KEY = os.environ.get("KOPIS_SERVICE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DAYS_AHEAD = 90
GENRE_CODE = "GGGA"
MAX_SHOWS = 30  # 캐스팅 이미지·할인정보를 수집할 공연 수 상한 (부하 방지)

BASE_URL = "http://www.kopis.or.kr/openApi/restful/pblprfr"
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1")

OUT_DIR = Path("docs/data")
OUT_DIR.mkdir(parents=True, exist_ok=True)
IMG_DIR = Path("_tmp_casting_images")
IMG_DIR.mkdir(exist_ok=True)


# ═════════════════════════════════════════════
# 1단계: KOPIS 뮤지컬 일정
# ═════════════════════════════════════════════
def fetch_list(stdate: str, eddate: str) -> list[dict]:
    shows, page = [], 1
    while True:
        r = requests.get(BASE_URL, params={
            "service": SERVICE_KEY, "stdate": stdate, "eddate": eddate,
            "cpage": page, "rows": 100, "shcate": GENRE_CODE,
        }, timeout=15)
        r.raise_for_status()
        items = ET.fromstring(r.content).findall("db")
        if not items:
            break
        for db in items:
            shows.append({
                "id": db.findtext("mt20id", ""),
                "공연명": db.findtext("prfnm", ""),
                "시작일": db.findtext("prfpdfrom", ""),
                "종료일": db.findtext("prfpdto", ""),
                "공연장": db.findtext("fcltynm", ""),
                "상태": db.findtext("prfstate", ""),
                "포스터": db.findtext("poster", ""),
            })
        page += 1
        time.sleep(0.2)
    return shows


def fetch_detail(show_id: str) -> dict:
    r = requests.get(f"{BASE_URL}/{show_id}", params={"service": SERVICE_KEY}, timeout=15)
    r.raise_for_status()
    db = ET.fromstring(r.content).find("db")
    if db is None:
        return {}
    ticket_sites = []
    relates = db.find("relates")
    if relates is not None:
        for rel in relates.findall("relate"):
            nm, url = rel.findtext("relatenm", ""), rel.findtext("relateurl", "")
            if url:
                ticket_sites.append(f"{nm}: {url}" if nm else url)
    return {
        "출연진": db.findtext("prfcast", ""),
        "런타임": db.findtext("prfruntime", ""),
        "티켓가격": db.findtext("pcseguidance", ""),
        "공연시간": db.findtext("dtguidance", ""),
        "제작사": db.findtext("entrpsnm", ""),
        "예매처": " | ".join(ticket_sites),
    }


def step1() -> list[dict]:
    print("1단계: KOPIS 일정 수집")
    today, end = date.today(), date.today() + timedelta(days=DAYS_AHEAD)
    all_shows: dict[str, dict] = {}
    cursor = today
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=30), end)
        for s in fetch_list(cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")):
            all_shows[s["id"]] = s
        cursor = chunk_end + timedelta(days=1)
    shows = list(all_shows.values())

    for i, s in enumerate(shows, 1):
        try:
            s.update(fetch_detail(s["id"]))
        except Exception:
            pass
        time.sleep(0.2)

    (OUT_DIR / "musicals.json").write_text(
        json.dumps(shows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → 뮤지컬 {len(shows)}건 저장")
    return shows


def extract_url(예매처: str) -> str:
    m = re.search(r"https?://[^\s|]+", 예매처 or "")
    return m.group(0) if m else ""


def build_auto_shows(shows: list[dict]) -> dict:
    auto = {}
    for s in shows:
        if s.get("상태") != "공연중":
            continue
        url = extract_url(s.get("예매처", ""))
        if url:
            auto[s["공연명"]] = url
    return dict(list(auto.items())[:MAX_SHOWS])


# ═════════════════════════════════════════════
# 2단계: 캐스팅 이미지 + 할인 정보
# ═════════════════════════════════════════════
IMG_URL_PATTERN = re.compile(r"https?:\\?/\\?/[^\s\"'<>\\]+?\.(?:jpg|jpeg|png|webp|gif)", re.I)
IMG_KEYWORDS = ["ticketimage", "detail", "notice", "cast", "info", "goods", "upload"]
DISCOUNT_KEYWORDS = ["할인", "조기예매", "타임세일", "재관람", "특가", "프로모션"]
DISC_RATE = re.compile(r"(\d{1,2})\s*%")


def html_to_text(html: str) -> list[str]:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", html)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return [ln for ln in lines if 4 <= len(ln) <= 120]


def extract_discounts(html: str, show: str, url: str) -> list[dict]:
    found, seen = [], set()
    for ln in html_to_text(html):
        if any(k in ln for k in DISCOUNT_KEYWORDS):
            if ln in seen:
                continue
            seen.add(ln)
            m = DISC_RATE.search(ln)
            found.append({
                "show": show, "title": ln[:80], "from": "", "to": "",
                "disc": m.group(1) if m else "", "link": url,
                "auto": True,  # 웹앱에서 자동/수동 이벤트를 구분하는 표시
            })
    return found[:15]


def step2(targets: dict) -> list[dict]:
    print(f"2단계: 캐스팅 이미지 + 할인정보 수집 (대상 {len(targets)}개 공연)")
    sess = requests.Session()
    sess.headers["User-Agent"] = MOBILE_UA
    all_events = []

    for name, url in targets.items():
        print(f"  ▶ {name}")
        try:
            html = sess.get(url, timeout=20).text
        except Exception as e:
            print(f"    !! 접속 실패: {e}")
            continue

        evs = extract_discounts(html, name, url)
        all_events.extend(evs)

        raw_urls = [u.replace("\\/", "/") for u in IMG_URL_PATTERN.findall(html)]
        urls = [u for u in dict.fromkeys(raw_urls) if any(k in u.lower() for k in IMG_KEYWORDS)]
        if not urls:
            continue
        show_dir = IMG_DIR / name
        show_dir.mkdir(parents=True, exist_ok=True)
        saved = 0
        for i, u in enumerate(urls, 1):
            try:
                data = sess.get(u, timeout=15).content
                ext = u.rsplit(".", 1)[-1][:4]
                (show_dir / f"{i:03d}.{ext}").write_bytes(data)
                saved += 1
            except Exception:
                continue
            time.sleep(0.15)
        print(f"    이미지 {saved}장")

    return all_events


# ═════════════════════════════════════════════
# 3단계: 캐스팅 이미지 AI 판독 (Claude API)
# ═════════════════════════════════════════════
CAST_PROMPT = """이 이미지는 뮤지컬 캐스팅 일정표일 수 있습니다.
날짜별 캐스팅 정보가 있다면 아래 JSON 배열 형식으로만 답하세요. 설명·마크다운 없이 JSON만 출력하세요.
[{"날짜": "2026-08-20", "회차": "19:30", "배역": "지킬", "배우": "홍길동"}]
연도가 이미지에 없으면 가장 그럴듯한 연도로 추정. 회차·배역 구분이 없으면 빈 문자열.
캐스팅 일정표가 아닌 이미지라면 빈 배열 [] 만 출력."""


def step3() -> list[dict]:
    if not ANTHROPIC_KEY:
        print("3단계 건너뜀: ANTHROPIC_API_KEY 시크릿이 없습니다.")
        return []
    if not IMG_DIR.exists() or not any(IMG_DIR.iterdir()):
        print("3단계 건너뜀: 판독할 이미지가 없습니다.")
        return []

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    rows, seen = [], set()
    print("3단계: 캐스팅 이미지 AI 판독")

    for show_dir in sorted(IMG_DIR.iterdir()):
        if not show_dir.is_dir():
            continue
        for img in sorted(show_dir.iterdir()):
            ext = img.suffix.lower().lstrip(".")
            media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                     "gif": "image/gif", "webp": "image/webp"}.get(ext)
            if not media:
                continue
            try:
                data = base64.standard_b64encode(img.read_bytes()).decode()
                msg = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4000,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
                        {"type": "text", "text": CAST_PROMPT},
                    ]}],
                )
                text = "".join(b.text for b in msg.content if b.type == "text")
                text = re.sub(r"```json|```", "", text).strip()
                for row in json.loads(text or "[]"):
                    row["공연명"] = show_dir.name
                    key = (show_dir.name, row.get("날짜"), row.get("회차"), row.get("배역"), row.get("배우"))
                    if key not in seen:
                        seen.add(key)
                        rows.append(row)
            except Exception as e:
                print(f"  ! 판독 실패 {img}: {e}")
    print(f"  → 캐스트 {len(rows)}건")
    return rows


# ═════════════════════════════════════════════
def main():
    if not SERVICE_KEY:
        raise SystemExit("!! KOPIS_SERVICE_KEY 시크릿이 설정되지 않았습니다.")

    shows = step1()
    targets = build_auto_shows(shows)
    auto_events = step2(targets)
    casts = step3()

    (OUT_DIR / "casts.json").write_text(
        json.dumps(casts, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "events.json").write_text(
        json.dumps(auto_events, ensure_ascii=False, indent=2), encoding="utf-8")
    print("저장 완료: docs/data/musicals.json, casts.json, events.json")


if __name__ == "__main__":
    main()
