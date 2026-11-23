"""
추출된 장소를 카카오로 위치 확인 → 같은 장소끼리 합치기 → 근처 화장실·바다낚시지수 붙이기 → 지도와 엑셀 생성.

- 같은 카카오 장소(또는 200m 안의 같은 이름)는 한 곳으로 합침. '영상 수'는 그 장소를 다룬 영상 개수.
- 근처 공중화장실: 카카오에서 반경 700m 안 '화장실' 검색 (data/toilet_cache.json에 저장해 재사용)
- 바다낚시지수: 15km 안의 가장 가까운 지수 지점을 연결 (data/fishing_index.json, 매일 갱신)
결과: site/index.html (지도), output/바다낚시_차박_장소.xlsx, output/not_found.csv

환경변수: KAKAO_REST_API_KEY(필수)
"""
import csv
import glob
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict

import requests
from rapidfuzz import fuzz

KEY = os.environ.get("KAKAO_REST_API_KEY", "")
KAKAO = os.environ.get("KAKAO_API_BASE", "https://dapi.kakao.com") + "/v2/local/search/keyword.json"
DATA = os.environ.get("DATA_DIR", "data")
OUT = os.environ.get("OUTPUT_DIR", "output")
SITE = os.environ.get("SITE_DIR", "site")
HERE = os.path.dirname(os.path.abspath(__file__))
GEO_CACHE = os.path.join(DATA, "geocode_cache.json")
TOILET_CACHE = os.path.join(DATA, "toilet_cache.json")
INDEX_FILE = os.path.join(DATA, "fishing_index.json")
INDEX_RADIUS_KM = float(os.environ.get("INDEX_RADIUS_KM", "15"))


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def km(a_lat, a_lon, b_lat, b_lon):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def kakao(params):
    for attempt in range(5):
        r = requests.get(KAKAO, params=params, headers={"Authorization": f"KakaoAK {KEY}"}, timeout=20)
        if r.status_code == 200:
            return r.json().get("documents", [])
        if r.status_code in (401, 403):
            sys.exit(f"카카오 API 키 오류({r.status_code}): {r.text[:200]}")
        time.sleep(2 ** attempt)
    return []


def simplify(s):
    return re.sub(r"[\s()\[\]·.,'\"-]", "", s or "").lower()


def region_tokens(region):
    toks = []
    for t in (region or "").split():
        toks.append(t[:-1] if len(t) > 2 and t[-1] in "시군구도" else t)
    return [t for t in toks if t]


REGION_FIX = [(r"^전남광주통합특별시", "전남"), (r"^(강원|전북|제주)특별자치도", r"\1"),
              (r"^(서울|부산|대구|인천|광주|대전|울산)(특별시|광역시)", r"\1"), (r"^세종특별자치시", "세종"),
              (r"^경상남도", "경남"), (r"^경상북도", "경북"), (r"^전라남도", "전남"), (r"^전라북도", "전북"),
              (r"^충청남도", "충남"), (r"^충청북도", "충북"), (r"^경기도", "경기")]


def norm_region(addr):
    r = " ".join((addr or "").split()[:2])
    for pat, rep in REGION_FIX:
        r = re.sub(pat, rep, r)
    return r


PLACE_END = r"(항|포구|방파제|방조제|해수욕장|해변|선착장|등대|쉼터|저수지|갯바위|해안|공원|캠핑장|오토캠핑장|휴게소|센터)"


def base_names(name):
    """'사천진항 1번 작은방파제' → ['사천진항'], '33센터 (새만금방조제 F포인트)' → ['33센터', '새만금방조제'] 처럼
    세부 포인트 이름에서 지도에서 찾을 수 있는 상위 장소 이름 후보를 뽑음."""
    out = []
    inner = re.findall(r"\(([^)]*)\)", name)
    main = re.sub(r"\([^)]*\)", " ", name).split()
    for k in range(len(main) - 1, 0, -1):
        out.append(" ".join(main[:k]))
    for part in [" ".join(main)] + inner:
        m = re.match(r"^(.+?" + PLACE_END + r")", part.replace(" ", ""))
        if m:
            out.append(m.group(1))
    seen, res = {name}, []
    for c in out:
        c = c.strip()
        if len(c) >= 2 and c not in seen and not re.search(r"(포인트|구간|구역|근처|번)$", c):
            seen.add(c)
            res.append(c)
    return res[:4]


def geocode(name, region, cache):
    key = f"{region}|{name}"
    if key in cache:
        return cache[key]
    toks = region_tokens(region)
    best = None
    for q in ([f"{region} {name}".strip(), name] if region else [name]):
        docs = kakao({"query": q, "size": 15})
        for d in docs:
            addr = (d.get("address_name") or "") + " " + (d.get("road_address_name") or "")
            if toks and not any(t in addr for t in toks[-1:] + toks[:1]):
                continue  # 지역이 전혀 안 맞으면 버림 (동명이인 장소 방지)
            a, b = simplify(name), simplify(d["place_name"])
            score = fuzz.ratio(a, b)
            if len(b) <= len(a) * 1.6 + 2:  # '신시도항' ↔ '…숲속의집 신시도항'처럼 엉뚱한 긴 이름은 부분일치 인정 안 함
                score = max(score, fuzz.partial_ratio(a, b) - 5)
            if best is None or score > best[0]:
                best = (score, d)
        if best and best[0] >= 75:
            break
    doc = best[1] if best and best[0] >= 70 else None
    cache[key] = None if doc is None else {
        "place_name": doc["place_name"], "lat": float(doc["y"]), "lon": float(doc["x"]),
        "address": doc.get("road_address_name") or doc.get("address_name", ""),
        "addr_region": norm_region(doc.get("address_name")),
        "category": (doc.get("category_name") or "").split(" > ")[-1],
        "url": doc.get("place_url", ""),
    }
    return cache[key]


def toilets_near(lat, lon, cache):
    key = f"{lat:.4f},{lon:.4f}"
    if key not in cache:
        docs = kakao({"query": "화장실", "x": lon, "y": lat, "radius": 700, "sort": "distance", "size": 5})
        cache[key] = [{"name": d["place_name"], "m": int(d.get("distance") or 0)}
                      for d in docs if "화장실" in d["place_name"]]
    return cache[key]


def main():
    if not KEY:
        sys.exit("KAKAO_REST_API_KEY 환경변수가 없습니다.")
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(SITE, exist_ok=True)
    geo, tcache = load_json(GEO_CACHE, {}), load_json(TOILET_CACHE, {})
    for k in [k for k, v in geo.items()  # 예전 규칙으로 엉뚱하게 붙은 긴 이름 결과는 다시 찾기
              if v and len(simplify(v["place_name"])) > len(simplify(k.split("|", 1)[-1])) * 1.6 + 2]:
        del geo[k]

    mentions = []
    for path in glob.glob(os.path.join(DATA, "extracted", "*.json")):
        rec = load_json(path, {})
        for s in rec.get("spots") or []:
            if (s.get("name") or "").strip():
                mentions.append((rec["video"], s))
    print(f"장소 언급 {len(mentions)}건")

    places, missing = {}, []
    for i, (video, s) in enumerate(mentions, 1):
        name, region = s["name"].strip(), (s.get("region") or "").strip()
        g, point = geocode(name, region, geo), None
        if not g:  # 세부 포인트면 상위 장소(항구·방조제 등)에 붙임
            for b in base_names(name):
                g = geocode(b, region, geo)
                if g:
                    point = name
                    break
        if i % 200 == 0:
            save_json(GEO_CACHE, geo)
            print(f"  위치 확인 {i}/{len(mentions)}", flush=True)
        if not g:
            missing.append((video, s))
            continue
        pid = re.search(r"(\d+)\s*$", g["url"] or "")
        key = "k" + pid.group(1) if pid else f"p{round(g['lat'], 4)}_{round(g['lon'], 4)}"
        p = places.setdefault(key, {
            "key": key, "name": g["place_name"], "lat": g["lat"], "lon": g["lon"], "address": g["address"],
            "region": g["addr_region"], "kakao": g["url"], "kinds": Counter(), "fishing": 0, "chabak": 0,
            "species": Counter(), "fac": defaultdict(lambda: [0, 0]), "view": Counter(), "tips": [],
            "warnings": [], "reviews": [], "videos": {}, "aliases": set(), "points": []})
        p["region"] = norm_region(g["addr_region"])
        if point:
            if point not in p["points"]:
                p["points"].append(point)
        else:
            p["aliases"].add(name)
        p["videos"][video["video_id"]] = {"title": video["title"], "channel": video["channel"]}
        if s.get("kind"):
            p["kinds"][s["kind"]] += 1
        p["fishing"] += 1 if s.get("fishing") else 0
        p["chabak"] += 1 if s.get("chabak") else 0
        p["species"].update(x.strip() for x in s.get("species") or [] if x.strip())
        for k, v in (s.get("facilities") or {}).items():
            if v is True:
                p["fac"][k][0] += 1
            elif v is False:
                p["fac"][k][1] += 1
        p["view"].update(x.strip() for x in s.get("view") or [] if x.strip())
        p["tips"] += [t for t in s.get("tips") or [] if t not in p["tips"]]
        p["warnings"] += [t for t in s.get("warnings") or [] if t not in p["warnings"]]
        p["reviews"] += s.get("reviews") or []
    save_json(GEO_CACHE, geo)

    # 바다낚시지수 지점
    idx = load_json(INDEX_FILE, {})
    stations = idx.get("stations") or []

    spots = []
    for n, p in enumerate(places.values(), 1):
        fac = {k: ("yes" if y > no else "no" if no > y else None) for k, (y, no) in p["fac"].items()}
        toilets = toilets_near(p["lat"], p["lon"], tcache)
        if n % 200 == 0:
            save_json(TOILET_CACHE, tcache)
        if toilets and fac.get("toilet") is None:
            fac["toilet"] = "near"
        near = None
        if p["fishing"] and stations:
            cand = [(km(p["lat"], p["lon"], st["lat"], st["lon"]), st) for st in stations]
            cand = [c for c in cand if c[0] <= INDEX_RADIUS_KM]
            if cand:
                d, st = min(cand, key=lambda c: c[0])
                near = {"name": st["name"], "type": st["type"], "km": round(d, 1), "rows": st["rows"][:40]}
        revs = sorted(p["reviews"], key=lambda r: -(r.get("likes") or 0))
        spots.append({
            "key": p["key"], "name": p["name"], "lat": p["lat"], "lon": p["lon"], "address": p["address"],
            "region": p["region"], "kakao": p["kakao"],
            "kind": p["kinds"].most_common(1)[0][0] if p["kinds"] else "",
            "fishing": p["fishing"] > 0, "chabak": p["chabak"] > 0, "videos_n": len(p["videos"]),
            "species": [s for s, _ in p["species"].most_common(12)],
            "fac": fac, "toilets": toilets[:3], "view": [v for v, _ in p["view"].most_common(4)],
            "tips": p["tips"][:6], "warnings": p["warnings"][:6],
            "good": sum(1 for r in p["reviews"] if r.get("verdict") == "good"),
            "bad": sum(1 for r in p["reviews"] if r.get("verdict") == "bad"),
            "reviews": [{"t": r["text"], "v": r.get("verdict"), "l": r.get("likes", 0)} for r in revs[:4]],
            "videos": [{"id": k, **v} for k, v in list(p["videos"].items())[:5]],
            "aliases": ", ".join(sorted(a for a in p["aliases"] if a != p["name"]))[:100],
            "points": p["points"][:12],
            "index": near,
        })
    save_json(TOILET_CACHE, tcache)
    spots.sort(key=lambda s: (-s["videos_n"], -s["good"]))

    meta = {"built_at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 9 * 3600)),
            "index_at": idx.get("fetched_at")}
    with open(os.path.join(HERE, "map_template.html"), encoding="utf-8") as f:
        html = f.read().replace("/*__SPOTS__*/[]", json.dumps(spots, ensure_ascii=False)) \
                       .replace("/*__META__*/{}", json.dumps(meta, ensure_ascii=False))
    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    write_excel(spots, missing)
    print(f"장소 {len(spots)}곳 (낚시 {sum(s['fishing'] for s in spots)}, 차박 {sum(s['chabak'] for s in spots)}), "
          f"위치 못 찾음 {len(missing)}건 → site/index.html, output/")


def write_excel(spots, missing):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    yn = {"yes": "O", "no": "X", "near": "근처", None: ""}
    wb = Workbook()
    ws = wb.active
    ws.title = "장소"
    head = ["지역", "장소", "종류", "낚시", "차박", "다룬 영상 수", "세부 포인트", "어종", "화장실", "수세식", "개수대",
            "무료", "취사", "근처 화장실", "뷰", "팁", "주의", "좋았다 후기", "별로 후기", "주소", "카카오맵"]
    ws.append(head)
    for s in sorted(spots, key=lambda s: (s["region"], -s["videos_n"])):
        f = s["fac"]
        ws.append([s["region"], s["name"], s["kind"], "O" if s["fishing"] else "", "O" if s["chabak"] else "",
                   s["videos_n"], " / ".join(s["points"]), ", ".join(s["species"]), yn[f.get("toilet")], yn[f.get("flush_toilet")],
                   yn[f.get("water")], yn[f.get("free")], yn[f.get("cooking_ok")],
                   ", ".join(f'{t["name"]}({t["m"]}m)' for t in s["toilets"]), ", ".join(s["view"]),
                   " / ".join(s["tips"]), " / ".join(s["warnings"]), s["good"], s["bad"], s["address"], s["kakao"]])
    ws2 = wb.create_sheet("위치 못 찾음")
    ws2.append(["지역", "장소", "영상", "채널"])
    for v, s in missing:
        ws2.append([s.get("region", ""), s["name"], v["title"], v["channel"]])
    for sheet in wb.worksheets:
        for c in sheet[1]:
            c.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        for col in sheet.columns:
            w = max(len(str(c.value or "")) for c in col[:200])
            sheet.column_dimensions[col[0].column_letter].width = min(max(6, w * 1.2), 45)
    wb.save(os.path.join(OUT, "바다낚시_차박_장소.xlsx"))


if __name__ == "__main__":
    main()
