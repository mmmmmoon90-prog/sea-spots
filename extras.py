"""
지도에 붙이는 '이번 주' 정보 두 가지 (build.py가 불러 씀).

1) 이번 주 추천 배지: data/featured.json (인스타 예약 작업이 매주 갱신)
   {"updated": "2026-11-26",
    "items": [{"key": "k18553996", "name": "지세포방파제", "label": "이번 주말 추천", "until": "2026-11-29"}, ...]}
   key가 맞는 장소(없으면 같은 이름 첫 장소)에 s["feat"] = label. until(한국 날짜)이 지나면 표시 안 함.

2) 요즘 조황: ttogan-map 저장소의 data/ipjil/latest.md (매일 17시쯤 갱신)를 읽어
   바다별 어종 순위만 요약해 meta["choh"]로 넣음. 못 읽으면 data/choh.json(지난번 값)을 씀.
   ttogan-map이 비공개라 Actions 비밀값 CHOH_TOKEN(ttogan-map 읽기 전용 토큰)이 있어야 매일 새로 읽음.
   유튜버·채널·선박 이름, 장비명, 영상 링크는 넣지 않음.
"""
import json
import os
import re
import time
import urllib.request

CHOH_URL = os.environ.get(
    "CHOH_URL", "https://raw.githubusercontent.com/mmmmmoon90-prog/ttogan-map/main/data/ipjil/latest.md")
CHOH_API = "https://api.github.com/repos/mmmmmoon90-prog/ttogan-map/contents/data/ipjil/latest.md"
SEAS = ["서해", "남해", "동해", "제주"]
PROV = {"충남", "충북", "전남", "전북", "경남", "경북", "인천", "부산", "울산", "강원", "경기", "제주", "제주도",
        "충청남도", "전라남도", "전라북도", "경상남도", "경상북도", "강원도", "경기도"}


def today_kst():
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 9 * 3600))


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def apply_featured(spots, data_dir):
    feat = _load(os.path.join(data_dir, "featured.json"), {})
    today, out = today_kst(), []
    by_key = {s["key"]: s for s in spots}
    for it in feat.get("items", []):
        if it.get("until") and it["until"] < today:
            continue
        s = by_key.get(it.get("key")) or next((x for x in spots if x["name"] == it.get("name")), None)
        if not s:
            continue
        s["feat"] = it.get("label") or "이번 주 추천"
        out.append({"key": s["key"], "name": s["name"], "label": s["feat"]})
    return {"updated": feat.get("updated"), "items": out} if out else None


def _area(loc):
    loc = re.sub(r"\([^)]*\)", " ", loc)
    words = [w for w in loc.split() if w not in PROV and not re.search(r"(호|미상|추정|선상|워킹|포인트|일대|인근)$", w)]
    return " ".join(words[:2])


def parse_choh(md):
    head = re.search(r"#\s*입질 조황 리포트\s*(\d{4}-\d{2}-\d{2})\s*\(([^)]*)\)", md)
    if not head:
        return None
    seas, cur = [], None
    for line in md.splitlines():
        m = re.match(r"##\s*(\S+)\s*\((\d+)건\)", line)
        if m:
            cur = {"sea": m.group(1), "n": int(m.group(2)), "top": []} if m.group(1) in SEAS else None
            if cur:
                seas.append(cur)
            continue
        m = re.match(r"-\s*\*\*(.+?)\*\*\s*언급\s*(\d+)건\s*·\s*점수\s*(-?\d+)\s*·\s*(.*)", line)
        if m and cur is not None:
            areas = []
            for loc in m.group(4).split(","):
                a = _area(loc)
                if a and a not in areas:
                    areas.append(a)
            cur["top"].append({"f": m.group(1).strip(), "n": int(m.group(2)), "score": int(m.group(3)), "areas": areas[:3]})
    for s in seas:
        s["top"] = [t for t in s["top"] if t["n"] >= 2 and t["score"] > 0][:4]
    return {"date": head.group(1), "note": head.group(2), "seas": seas}


def load_choh(data_dir):
    cache = os.path.join(data_dir, "choh.json")
    try:
        token = os.environ.get("CHOH_TOKEN", "")
        if token:  # ttogan-map이 비공개라 읽기 전용 토큰으로 GitHub API 사용
            req = urllib.request.Request(CHOH_API, headers={
                "Authorization": "Bearer " + token, "Accept": "application/vnd.github.raw"})
        else:
            req = urllib.request.Request(CHOH_URL + "?t=" + str(int(time.time())))
        with urllib.request.urlopen(req, timeout=20) as r:
            data = parse_choh(r.read().decode("utf-8"))
        if data:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return data
    except Exception as e:
        print("조황 리포트를 못 읽어 지난 값을 씀:", e)
    return _load(cache, None)
