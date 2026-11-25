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

class KopisError(RuntimeError):
    pass

SERVICE_KEY = os.environ.get("KOPIS_SERVICE_KEY", "")

DAYS_AHEAD = 90          # 오늘부터 며칠 후까지 수집할지
GENRE_CODE = "GGGA"      # KOPIS 뮤지컬 장르코드

BASE_URLS = ["http://www.kopis.or.kr/openApi/restful/pblprfr",
             "https://www.kopis.or.kr/openApi/restful/pblprfr"]
BASE_URL = BASE_URLS[0]
MIN_KEEP_RATIO = 0.5     # 새로 받은 공연 수가 지난번의 절반도 안 되면 덮어쓰지 않는다

OUT_DIR = Path("docs/data")
OUT_DIR.mkdir(parents=True, exist_ok=True)


# KOPIS 방화벽은 2026년 11월 말부터 python-requests 기본 User-Agent 요청을 "400 Request Blocked"로 막는다.
# 앱 이름을 밝히고, 요청 간격을 두고, 막히면 잠시 기다렸다 다시 시도한다.
HEADERS = {"User-Agent": "musical-scheduler/1.0 (+https://github.com/orgio98/musical-scheduler)",
           "Accept": "application/xml, text/xml"}
MIN_INTERVAL = 0.6   # 초
BLOCK_RETRIES = 3
BLOCK_WAIT = 15      # 초
_last_call = 0.0


def get(url: str, params: dict) -> requests.Response:
    """요청 간격을 지키고, 네트워크 오류·방화벽 차단은 몇 번 다시 시도한다."""
    global _last_call
    last = None
    blocked = 0
    while True:
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        except requests.RequestException as e:
            last = e
            if blocked + 1 > 3:
                raise KopisError(f"KOPIS에 연결하지 못했습니다: {last}")
            blocked += 1
            time.sleep(2 * blocked)
            continue
        if "Request Blocked" in r.text[:2000]:
            blocked += 1
            if blocked > BLOCK_RETRIES:
                raise KopisError("KOPIS 방화벽이 요청을 계속 막고 있습니다 (400 Request Blocked)")
            print(f"  KOPIS 차단 응답 → {BLOCK_WAIT}초 뒤 다시 시도 ({blocked}/{BLOCK_RETRIES})", flush=True)
            time.sleep(BLOCK_WAIT)
            continue
        return r


def fetch_list(stdate: str, eddate: str) -> list[dict]:
    """지정 기간의 뮤지컬 목록을 전 페이지 수집."""
    global BASE_URL
    shows, page = [], 1
    while True:
        params = {"service": SERVICE_KEY, "stdate": stdate, "eddate": eddate,
                  "cpage": page, "rows": 100, "shcate": GENRE_CODE}
        r = get(BASE_URL, params)
        if page == 1 and r.status_code != 200:
            # 첫 페이지부터 실패하면 "목록 끝"이 아니라 오류다. 다른 주소(https)로 한 번 더 시도한다.
            for alt in BASE_URLS:
                if alt == BASE_URL:
                    continue
                r2 = get(alt, params)
                if r2.status_code == 200:
                    print(f"  ! {BASE_URL} 실패({r.status_code}) → {alt} 사용")
                    BASE_URL, r = alt, r2
                    break
        if r.status_code == 400 and page > 1:
            break  # KOPIS는 페이지 범위를 넘어서면 빈 목록 대신 400을 준다 → 더 없다는 뜻
        if r.status_code != 200:
            raise KopisError(f"KOPIS 목록 요청 실패 (HTTP {r.status_code}, {stdate}~{eddate}): {r.text[:300]}")
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise KopisError(f"KOPIS 응답이 XML이 아닙니다 ({stdate}~{eddate}): {r.text[:300]}")
        items = root.findall("db")
        if not items:
            if page == 1:
                print(f"  ! {stdate}~{eddate} 목록이 비어 있습니다. 응답 앞부분: {r.text[:300]}")
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
    return shows


def fetch_detail(show_id: str) -> dict:
    """공연 상세: 출연진·런타임·가격·공연시간·제작사·예매처 링크."""
    r = get(f"{BASE_URL}/{show_id}", {"service": SERVICE_KEY})
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

    out = OUT_DIR / "musicals.json"
    try:
        previous = json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        previous = []
    # KOPIS가 오류나 빈 결과를 주면 기존 데이터를 지우지 않고 실패로 끝낸다 (GitHub이 실패 메일을 보낸다).
    if not shows or len(shows) < len(previous) * MIN_KEEP_RATIO:
        raise SystemExit(f"!! 받은 공연이 {len(shows)}건뿐이라(지난번 {len(previous)}건) 기존 데이터를 그대로 둡니다. KOPIS 상태나 인증키를 확인하세요.")
    out.write_text(json.dumps(shows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: docs/data/musicals.json ({len(shows)}건)")


if __name__ == "__main__":
    try:
        main()
    except KopisError as e:
        raise SystemExit(f"!! {e}\n기존 docs/data/musicals.json 은 그대로 둡니다.")
