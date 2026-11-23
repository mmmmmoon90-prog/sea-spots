"""
국립해양조사원 바다낚시지수(공공데이터포털)를 받아 data/fishing_index.json에 저장합니다.
매일 실행하면 지도에 붙는 지수가 최신으로 바뀝니다.

환경변수: DATA_GO_KR_KEY (공공데이터포털 '일반 인증키(Decoding)'), 없으면 건너뜀
"""
import json
import os
import sys
import time

import requests

URL = os.environ.get("FISHING_URL", "https://apis.data.go.kr/1192136/fcstFishingv2/GetFcstFishingApiServicev2")
KEY = os.environ.get("DATA_GO_KR_KEY", "")
DATA = os.environ.get("DATA_DIR", "data")
GUBUN = ("갯바위", "선상")

# 응답 필드 이름 후보 (기관이 이름을 바꿔도 대응하도록 여러 개)
F = {
    "name": ["seafsPstnNm", "pstnNm", "fishingPstnNm"],
    "lat": ["lat", "latitude"],
    "lon": ["lot", "lon", "longitude"],
    "date": ["predcYmd", "fcstYmd", "ymd"],
    "noon": ["predcNoonSeCd", "noonSeCd", "amPm"],
    "fish": ["seafsTgfshNm", "tgfshNm", "fishNm"],
    "index": ["totalIndex", "fishingIndex", "idx"],
    "score": ["lastScr", "totalScore", "score"],
    "tide": ["tdlvHrCn", "tideNm", "mlTime"],
    "wave_min": ["minWvhgt"], "wave_max": ["maxWvhgt"],
    "temp_min": ["minWtem"], "temp_max": ["maxWtem"],
}


def pick(item, key):
    for k in F[key]:
        if item.get(k) not in (None, ""):
            return item[k]
    return None


def fetch(gubun):
    out, page = [], 1
    while True:
        params = {"serviceKey": KEY, "type": "json", "gubun": gubun, "pageNo": page, "numOfRows": 300}
        for attempt in range(4):
            try:
                r = requests.get(URL, params=params, timeout=30)
                break
            except requests.RequestException:
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError("바다낚시지수 요청 실패")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"바다낚시지수 응답이 JSON이 아님(키 등록 전일 수 있음): {r.text[:200]}")
        if "response" not in data and "header" in data:
            data = {"response": data}
        head = data.get("response", {}).get("header", {}) or {}
        if str(head.get("resultCode", "00")) not in ("00", "0"):
            raise RuntimeError(f"바다낚시지수 오류: {head.get('resultCode')} {head.get('resultMsg')}")
        body = data.get("response", {}).get("body", {}) or {}
        items = body.get("items") or {}
        if isinstance(items, dict):
            items = items.get("item", [])
        if isinstance(items, dict):
            items = [items]
        out += items
        total = int(body.get("totalCount") or 0)
        if page == 1 and items:
            print(f"  [{gubun}] 응답 필드: {sorted(items[0].keys())}")
        if not items or page * 300 >= total:
            return out
        page += 1


def main():
    if not KEY:
        print("DATA_GO_KR_KEY가 없어 바다낚시지수를 건너뜁니다.")
        return
    stations = {}
    for g in GUBUN:
        try:
            items = fetch(g)
        except RuntimeError as e:
            print(f"  {g}: {e}")
            continue
        for it in items:
            name, lat, lon = pick(it, "name"), pick(it, "lat"), pick(it, "lon")
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            st = stations.setdefault(f"{g}|{name}", {"name": name, "type": g, "lat": lat, "lon": lon, "rows": []})
            st["rows"].append({k: pick(it, k) for k in
                               ("date", "noon", "fish", "index", "score", "tide",
                                "wave_min", "wave_max", "temp_min", "temp_max")})
        print(f"  {g}: 지점 {sum(1 for s in stations.values() if s['type'] == g)}곳, 예보 {len(items)}건")
    if not stations:
        sys.exit("바다낚시지수를 하나도 받지 못했습니다.")
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "fishing_index.json"), "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 9 * 3600)),
                   "stations": list(stations.values())}, f, ensure_ascii=False)
    print(f"바다낚시지수 저장: 지점 {len(stations)}곳")


if __name__ == "__main__":
    main()
