# -*- coding: utf-8 -*-
"""
GitHub Actions 자동 실행용 뮤지컬 일정 크롤러
==============================================
매일 스케줄에 따라 자동 실행되어 KOPIS(공연예술통합전산망)에서
전국 뮤지컬 일정·공연장·공연시간·티켓가격·예매처 링크를 수집하고
docs/data/musicals.json 으로 저장합니다.
GitHub Pages가 docs/ 를 서빙하므로, 커밋되는 순간 웹앱에 반영됩니다.

필요한 저장소 시크릿(Settings > Secrets and variables > Actions):
  - KOPIS_SERVICE_KEY   kopis.or.kr 에서 발급한 오픈API 인증키
"""

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import requests

SERVICE_KEY = os.environ.get("KOPIS_SERVICE_KEY", "")

DAYS_AHEAD = 90          # 오늘부터 며칠 후까지 수집할지
GENRE_CODE = "GGGA"      # KOPIS 뮤지컬 장르코드

BASE_URL = "http://www.kopis.or.kr/openApi/restful/pblprfr"

OUT_DIR = Path("docs/data")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_list(stdate: str, eddate: str) -> list[dict]:
    """지정 기간의 뮤지컬 목록을 전 페이지 수집."""
    shows, page = [], 1
    while True:
        r = requests.get(BASE_URL, params={
            "service": SERVICE_KEY, "stdate": stdate, "eddate": eddate,
            "cpage": page, "rows": 100, "shcate": GENRE_CODE,
        }, timeout=15)
        if r.status_code == 400:
            break  # KOPIS는 페이지 범위를 넘어서면 빈 목록 대신 400을 준다 → 더 없다는 뜻
        r.raise_for_status()
        items = ET.fromstring(r.content).findall("db")
        if not items:
            break
        for db in items:
            shows.append({
                "id": db.findtext("mt20id", ""),
                "공연명": db.findtext("prfnm", ""),
                # KOPIS는 2026.08.01 형식으로 주는데, 웹앱은 2026-08-01 로 비교하므로 변환
                "시작일": db.findtext("prfpdfrom", "").replace(".", "-"),
                "종료일": db.findtext("prfpdto", "").replace(".", "-"),
                "공연장": db.findtext("fcltynm", ""),
                "상태": db.findtext("prfstate", ""),
                "포스터": db.findtext("poster", ""),
            })
        page += 1
        time.sleep(0.2)
    return shows


def fetch_detail(show_id: str) -> dict:
    """공연 상세: 출연진·런타임·가격·공연시간·제작사·예매처 링크."""
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


def main():
    if not SERVICE_KEY:
        raise SystemExit("!! KOPIS_SERVICE_KEY 시크릿이 설정되지 않았습니다.")

    print("KOPIS 뮤지컬 일정 수집")
    today, end = date.today(), date.today() + timedelta(days=DAYS_AHEAD)

    # 목록 API는 조회기간 제한이 있어 31일 단위로 나눠 조회 후 id 기준 중복 제거
    all_shows: dict[str, dict] = {}
    cursor = today
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=30), end)
        for s in fetch_list(cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")):
            all_shows[s["id"]] = s
        cursor = chunk_end + timedelta(days=1)
    shows = list(all_shows.values())
    print(f"  목록 {len(shows)}건")

    print("  상세정보 수집 중...")
    for i, s in enumerate(shows, 1):
        try:
            s.update(fetch_detail(s["id"]))
        except Exception:
            pass
        if i % 100 == 0:
            print(f"    {i}/{len(shows)}")
        time.sleep(0.2)

    (OUT_DIR / "musicals.json").write_text(
        json.dumps(shows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: docs/data/musicals.json ({len(shows)}건)")


if __name__ == "__main__":
    main()
