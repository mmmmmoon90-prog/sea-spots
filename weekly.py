"""
이번 주말 낚시 추천 콘텐츠 자동 생성 (블로그 글 + 인스타 카드뉴스).

- 입력: site/index.html (build.py가 만든 지도, 장소·바다낚시지수 포함), map_template.html (금지체장·금어기 표)
- 출력: site/weekly/index.html (카드·글 모아보기), site/weekly/card-01.png ..., site/weekly/blog.txt, site/weekly/caption.txt
- 다가오는 토·일 바다낚시지수가 좋은 낚시 장소를 고르고(같은 지수 지점·같은 시도 쏠림 방지), 카드 이미지로 렌더링한다.
"""
import datetime as dt
import html
import json
import os
import re
import sys

SITE = os.environ.get("SITE_DIR", "site")
OUT = os.path.join(SITE, "weekly")
MAP_URL = os.environ.get("MAP_URL", "https://recheegoo.s.gy/fishingmap")
TOP_N = int(os.environ.get("TOP_N", "5"))
LV = {"매우좋음": 5, "좋음": 4, "보통": 3, "나쁨": 2, "매우나쁨": 1}
LVTXT = {"매우좋음": "아주 좋음", "좋음": "좋음", "보통": "보통", "나쁨": "나쁨", "매우나쁨": "아주 나쁨"}
WD = "월화수목금토일"
FISH_ALIAS = {"학공치": "학꽁치", "뱅에돔": "벵에돔", "긴꼬리벵에돔": "벵에돔", "망둥이": "망둥어", "호레기": "호래기",
              "쏨벵이": "쏨뱅이", "이면수": "임연수어", "임연수": "임연수어"}
SIDO_SHORT = [("특별자치도", ""), ("특별자치시", ""), ("광역시", ""), ("특별시", "")]


def kst_today():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date()


def load_spots():
    s = open(os.path.join(SITE, "index.html"), encoding="utf-8").read()
    i = s.index("const SPOTS = ") + len("const SPOTS = ")
    j = s.index(";\nconst META", i)
    return json.loads(s[i:j])


def load_index_rows(spots):
    """지도에는 날짜별 지수가 일부만 들어가므로, 원본(data/fishing_index.json)에서 지점별 전체 날짜를 붙인다."""
    path = os.path.join(os.environ.get("DATA_DIR", "data"), "fishing_index.json")
    if not os.path.exists(path):
        return
    st = {x["name"]: x.get("rows", []) for x in json.load(open(path, encoding="utf-8")).get("stations", [])}
    for s in spots:
        if s.get("index") and s["index"].get("name") in st:
            s["index"]["rows"] = st[s["index"]["name"]]


def load_regs():
    """map_template.html의 REGS 표에서 어종명·별칭·크기·전국 공통 금어기를 읽는다."""
    t = open("map_template.html", encoding="utf-8").read()
    block = t[t.index("const REGS = ["):t.index("];", t.index("const REGS = ["))]
    regs = []
    for m in re.finditer(r"\{g: \"[^\"]+\", k: \"([^\"]+)\", al: \[([^\]]*)\](.*?)\},?\n(?=  \{g:|$)", block + "\n", re.S):
        k, al, rest = m.group(1), m.group(2), m.group(3)
        size = re.search(r'size: "([^"]+)"', rest)
        soft = "soft: true" in rest
        periods = [(int(f), int(t_), bool(extra.strip())) for f, t_, extra in re.findall(r"\{f: (\d+), t: (\d+)((?:, (?:re|no): /[^/]*/)*)\}", rest)]
        regs.append({"k": k, "name": re.sub(r"\(.*\)", "", k), "al": re.findall(r'"([^"]+)"', al),
                     "size": size.group(1) if size else "", "soft": soft,
                     "national": [(f, t_) for f, t_, regional in periods if not regional]})
    return regs


def in_range(d, f, t):
    return f <= d <= t if f <= t else d >= f or d <= t


def md(n):
    return f"{n // 100}/{n % 100}"


def short_region(r):
    for a, b in SIDO_SHORT:
        r = r.replace(a, b)
    return r


def pick(spots, regs, days):
    by_alias = {a: r for r in regs for a in r["al"]}
    canon = lambda sp: by_alias[sp]["name"] if sp in by_alias else FISH_ALIAS.get(sp, sp)
    cands = []
    for s in spots:
        if not s.get("fishing") or not s.get("index"):
            continue
        if any(re.search(r"낚시\s?금지", w) for w in s.get("warnings", [])):
            continue
        fishset = {canon(x) for x in s.get("species", [])}
        best = None
        for r in s["index"].get("rows", []):
            if r.get("date") not in days:
                continue
            lv = LV.get(r.get("index"), 0)
            f = r.get("fish") or ""
            match = canon(f) in fishset
            key = (lv, match)
            if best is None or key > best[0]:
                best = (key, r)
        if not best or best[0][0] < 3:
            continue
        lv, match = best[0]
        score = lv * 10 + (3 if match else 0) + min(s.get("videos_n", 0), 15) * 0.3 + (2 if s.get("good", 0) > s.get("bad", 0) else 0) \
            + (1 if (s.get("fac", {}).get("toilet") == "yes" or s.get("toilets")) else 0)
        cands.append((score, s, best[1], match))
    cands.sort(key=lambda x: -x[0])
    out, used_station, per_sido = [], set(), {}
    for score, s, row, match in cands:
        st = s["index"]["name"]
        sido = s["region"].split(" ")[0]
        if st in used_station or per_sido.get(sido, 0) >= 2:
            continue
        used_station.add(st)
        per_sido[sido] = per_sido.get(sido, 0) + 1
        out.append((s, row, match))
        if len(out) >= TOP_N:
            break
    return out, canon, by_alias


def day_rows(s, day):
    return [r for r in s["index"].get("rows", []) if r.get("date") == day]


def summary_for(s, days, canon):
    fishset = {canon(x) for x in s.get("species", [])}
    parts = []
    for d in days:
        rows = day_rows(s, d)
        if not rows:
            continue
        good = sorted(rows, key=lambda r: -LV.get(r.get("index"), 0))
        top = good[0]
        fishes = [r["fish"] for r in good if LV.get(r.get("index"), 0) >= 4 and r.get("fish") not in ("-", "기타어종") and canon(r["fish"]) in fishset]
        parts.append({"day": d, "lv": top.get("index"), "fish": list(dict.fromkeys(fishes))[:3], "tide": top.get("tide") or "",
                      "wave": f"{top.get('wave_min')}~{top.get('wave_max')}m" if top.get("wave_max") is not None else "",
                      "temp": f"{top.get('temp_min')}~{top.get('temp_max')}℃" if top.get("temp_max") is not None else ""})
    return parts


def fmt_day(d):
    x = dt.date.fromisoformat(d)
    return f"{x.month}/{x.day}({WD[x.weekday()]})"


def size_of(sp, by_alias):
    r = by_alias.get(sp)
    if not r or not r["size"] or r["soft"]:
        return ""
    return re.sub(r" \(.*\)$", "", r["size"]).replace(" 이하", "↓")


CSS = """*{margin:0;padding:0;box-sizing:border-box}body{font-family:"Noto Sans CJK KR","Noto Sans KR",sans-serif;background:#fff}
.card{width:1080px;height:1350px;position:relative;overflow:hidden;color:#13202b;background:#fff}
.cover{background:linear-gradient(170deg,#0f5c94 0%,#1673b5 55%,#2a8fd0 100%);color:#fff;padding:110px 90px}
.cover .k{display:inline-block;font-size:34px;font-weight:700;background:rgba(255,255,255,.18);border-radius:60px;padding:12px 30px}
.cover h1{font-size:104px;line-height:1.16;font-weight:900;letter-spacing:-.03em;margin-top:56px}
.cover h1 em{font-style:normal;color:#ffd27a}
.cover p{font-size:40px;line-height:1.55;margin-top:46px;color:#dcecf8}
.cover .ft{position:absolute;left:90px;right:90px;bottom:90px;display:flex;align-items:center;gap:24px;font-size:34px;font-weight:700}
.cover .ft img{width:96px;height:96px;border-radius:24px}
.spot{padding:90px 84px}
.rank{display:inline-flex;align-items:center;gap:16px;font-size:38px;font-weight:900;color:#1673b5}
.rank b{width:84px;height:84px;border-radius:50%;background:#1673b5;color:#fff;display:grid;place-items:center;font-size:44px}
.spot h2{font-size:84px;line-height:1.15;font-weight:900;letter-spacing:-.03em;margin-top:34px}
.where{font-size:36px;color:#5b6b77;margin-top:14px}
.days{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:50px}
.day{border-radius:28px;background:#f2f7fb;padding:30px 32px}
.day .d{font-size:32px;font-weight:700;color:#5b6b77}
.day .lv{font-size:52px;font-weight:900;margin-top:8px}
.lv.g{color:#1f8f4e}.lv.m{color:#13202b}.lv.b{color:#c0392b}
.day .x{font-size:28px;color:#5b6b77;margin-top:10px;line-height:1.5}
.fish{margin-top:44px}
.fish .t{font-size:32px;font-weight:700;color:#5b6b77;margin-bottom:16px}
.chips{display:flex;flex-wrap:wrap;gap:14px}
.chip{border-radius:22px;border:3px solid #cfe2f1;background:#eef6fc;padding:16px 24px;font-size:38px;font-weight:900}
.chip small{display:block;font-size:26px;font-weight:500;color:#5b6b77;margin-top:2px}
.chip.no{border-color:#f2b8b0;background:#fdecea;color:#c0392b}
.chip.no small{color:#c0392b}
.fac{margin-top:40px;font-size:32px;color:#13202b;line-height:1.6}
.tip{margin-top:26px;font-size:30px;line-height:1.6;color:#4a5b68;background:#fff6ea;border-radius:22px;padding:22px 28px}
.foot{position:absolute;left:84px;right:84px;bottom:70px;display:flex;justify-content:space-between;align-items:center;font-size:28px;color:#8696a2}
.foot b{color:#1673b5}
.rules{padding:100px 84px}
.rules h2{font-size:80px;font-weight:900;line-height:1.2;letter-spacing:-.03em}
.rules p{font-size:34px;line-height:1.6;color:#4a5b68;margin-top:24px}
.rl{margin-top:44px;display:flex;flex-direction:column;gap:16px}
.ri{display:flex;justify-content:space-between;align-items:center;border-radius:22px;background:#f2f7fb;padding:24px 30px;font-size:38px;font-weight:700}
.ri span{font-size:30px;color:#5b6b77;font-weight:500}
.ri.no{background:#fdecea;color:#c0392b}
.cta{background:#13202b;color:#fff;padding:120px 90px;text-align:center}
.cta img{width:200px;height:200px;border-radius:48px;margin-top:40px}
.cta h2{font-size:84px;line-height:1.2;font-weight:900;margin-top:60px}
.cta p{font-size:38px;line-height:1.6;color:#b8c6d1;margin-top:30px}
.cta .u{display:inline-block;margin-top:60px;background:#ffd27a;color:#13202b;border-radius:80px;padding:28px 50px;font-size:44px;font-weight:900}
.cta .s{position:absolute;left:0;right:0;bottom:80px;font-size:32px;color:#8ea0ad}
"""


def good_week(picks):
    return sum(LV.get(r.get("index"), 0) for _, r, _ in picks) / len(picks) >= 4


def lv_cls(v):
    n = LV.get(v, 3)
    return "g" if n >= 4 else "b" if n <= 2 else "m"


def build_cards(picks, days, regs, by_alias, canon, today):
    e = html.escape
    d0, d1 = fmt_day(days[0]), fmt_day(days[-1])
    good_wk = good_week(picks)
    cards = []
    cards.append(f"""<div class="card cover"><span class="k">이번 주말 바다낚시</span>
<h1>{e(d0)}~{e(d1)}<br><em>{"낚시 여건 좋은 곳" if good_wk else "그나마 괜찮은 곳"}</em><br>TOP {len(picks)}</h1>
<p>{"국립해양조사원 바다낚시지수로 골랐어요.<br>물때·파도·수온까지 한 번에 확인하세요." if good_wk else "이번 주말은 파도가 높은 바다가 많아요.<br>그중 지수가 나은 곳만 골랐어요. 출발 전 기상특보를 꼭 확인하세요."}</p>
<div class="ft"><img src="icon.png">바다낚시·차박 지도</div></div>""")
    dnum = today.month * 100 + today.day
    for i, (s, row, match) in enumerate(picks, 1):
        parts = summary_for(s, days, canon)
        days_html = "".join(f"""<div class="day"><div class="d">{e(fmt_day(p['day']))}</div><div class="lv {lv_cls(p['lv'])}">{e(LVTXT.get(p['lv'], p['lv'] or '-'))}</div>
<div class="x">{e(p['tide'])}{' · 파도 ' + e(p['wave']) if p['wave'] else ''}{'<br>수온 ' + e(p['temp']) if p['temp'] else ''}</div></div>""" for p in parts)
        good_fish = []
        for p in parts:
            for f in p["fish"]:
                if f not in good_fish:
                    good_fish.append(f)
        sp_list = list(dict.fromkeys(good_fish + [x for x in s.get("species", [])]))[:6]
        chips = []
        for sp in sp_list:
            r = by_alias.get(sp)
            closed = bool(r and not r["soft"] and any(in_range(dnum, f, t) for f, t in r["national"]))
            sub = size_of(sp, by_alias)
            name = r["name"] if r else FISH_ALIAS.get(sp, sp)
            chips.append(f'<div class="chip{" no" if closed else ""}">{"🚫 " if closed else ""}{e(name)}{f"<small>{e(sub)}{" · 금어기 중" if closed else " 방류"}</small>" if sub or closed else ""}</div>')
        fac = []
        if s.get("fac", {}).get("toilet") == "yes" or s.get("toilets"):
            t = s.get("toilets") or []
            fac.append(f"🚻 화장실{(' ' + str(t[0]['m']) + 'm') if t else ''}")
        if s.get("fac", {}).get("water") == "yes":
            fac.append("🚰 개수대")
        if s.get("fac", {}).get("free") == "yes":
            fac.append("무료")
        if s.get("chabak"):
            fac.append("🚐 차박 가능")
        tip = (s.get("tips") or [""])[0]
        cards.append(f"""<div class="card spot"><div class="rank"><b>{i}</b>이번 주말 추천</div>
<h2>{e(s['name'])}</h2><div class="where">{e(short_region(s['region']))} · {e(s.get('kind', ''))} · 지수 지점 {e(s['index']['name'])}</div>
<div class="days">{days_html}</div>
<div class="fish"><div class="t">이곳에서 잡히는 어종</div><div class="chips">{''.join(chips)}</div></div>
{f'<div class="fac">{" · ".join(e(x) for x in fac)}</div>' if fac else ''}
{f'<div class="tip">💡 {e(tip[:70])}</div>' if tip else ''}
<div class="foot"><span>지도에서 <b>{e(s['name'])}</b> 검색</span><span>바다낚시·차박 지도</span></div></div>""")
    closed_now = [r for r in regs if not r["soft"] and any(in_range(dnum, f, t) for f, t in r["national"])]
    sized = [r for r in regs if r["size"] and not r["soft"] and r["name"] in ("감성돔", "우럭", "볼락", "참돔", "광어", "농어", "도다리", "쥐노래미")]
    rows = "".join(f'<div class="ri no">🚫 {e(r["name"])} <span>금어기 ~{md(next(t for f, t in r["national"] if in_range(dnum, f, t)))}</span></div>' for r in closed_now)
    rows += "".join(f'<div class="ri">{e(r["name"])} <span>{e(re.sub(r" [(].*[)]$", "", r["size"]))} 방류</span></div>' for r in sized[:max(0, 6 - len(closed_now))])
    cards.append(f"""<div class="card rules"><h2>출발 전에<br>이것만 기억하세요</h2>
<p>이 크기 이하는 놓아주고, 금어기 어종은 잡으면 안 돼요.<br>낚시로 잡아도 똑같이 적용돼요.</p><div class="rl">{rows}</div>
<div class="foot"><span>수산자원관리법 기준 · 길이는 전장</span><span>바다낚시·차박 지도</span></div></div>""")
    cards.append(f"""<div class="card cta"><img src="icon.png"><h2>포인트 1,000곳 넘게<br>지도에 다 있어요</h2>
<p>화장실·개수대·물때·금어기까지<br>가입 없이 무료로 보세요</p><div class="u">{e(MAP_URL.replace('https://', ''))}</div>
<div class="s">저장해두고 주말에 꺼내 보세요 🎣</div></div>""")
    return cards


def blog_text(picks, days, canon, by_alias, today):
    d0, d1 = fmt_day(days[0]), fmt_day(days[-1])
    lines = [f"제목: 이번 주말({d0}~{d1}) 바다낚시 어디 갈까? 낚시지수 {"좋은" if good_week(picks) else "괜찮은"} 포인트 TOP{len(picks)}", "",
             f"이번 주말 바다낚시 계획 있으신가요? 국립해양조사원 바다낚시지수 기준으로 {d0}~{d1} 낚시 여건이 " + ("좋은 포인트를 골라봤어요." if good_week(picks) else "그나마 나은 포인트를 골라봤어요. 이번 주말은 파도가 높은 곳이 많으니 출발 전 기상특보를 꼭 확인하세요."),
             "물때·파도·수온과 화장실 여부, 잡히는 어종의 금지체장까지 정리했어요.", f"▶ 전체 지도: {MAP_URL}", ""]
    for i, (s, row, match) in enumerate(picks, 1):
        parts = summary_for(s, days, canon)
        lines.append(f"{i}. {s['name']} ({short_region(s['region'])} · {s.get('kind', '')})")
        for p in parts:
            fish = ", ".join(p["fish"])
            lines.append(f"- {fmt_day(p['day'])}: 낚시 {LVTXT.get(p['lv'], p['lv'])}{' (' + fish + ')' if fish else ''} · {p['tide']}"
                         f"{' · 파도 ' + p['wave'] if p['wave'] else ''}{' · 수온 ' + p['temp'] if p['temp'] else ''}")
        sp = [f"{x}({size_of(x, by_alias)})" if size_of(x, by_alias) else x for x in s.get("species", [])[:6]]
        if sp:
            lines.append("- 잡히는 어종: " + ", ".join(sp))
        fac = []
        if s.get("fac", {}).get("toilet") == "yes" or s.get("toilets"):
            fac.append("화장실")
        if s.get("fac", {}).get("water") == "yes":
            fac.append("개수대")
        if s.get("fac", {}).get("free") == "yes":
            fac.append("무료")
        if fac:
            lines.append("- 편의시설: " + ", ".join(fac))
        if s.get("tips"):
            lines.append("- 팁: " + s["tips"][0])
        if s.get("warnings"):
            lines.append("- 주의: " + s["warnings"][0])
        lines.append("")
    lines += ["※ 낚시 여건은 국립해양조사원 바다낚시지수(지수 지점 기준) 예보라 현장과 다를 수 있어요. 금지구역·야영 규정은 출발 전 현장 안내를 확인하세요.",
              "※ 괄호 안 크기는 금지체장이에요. 그 크기 이하는 놓아줘야 해요.", "",
              "태그: #바다낚시 #주말낚시 #낚시포인트 #바다낚시지수 #물때 #감성돔 #우럭 #볼락 #방파제낚시 #갯바위낚시 #차박 #캠낚"]
    return "\n".join(lines)


def caption_text(picks, days):
    d0, d1 = fmt_day(days[0]), fmt_day(days[-1])
    names = "\n".join(f"{i}. {s['name']} ({short_region(s['region'])})" for i, (s, _, _) in enumerate(picks, 1))
    return (f"이번 주말({d0}~{d1}) 낚시 여건 {'좋은' if good_week(picks) else '그나마 괜찮은'} 곳 TOP{len(picks)} 🎣\n\n{names}\n\n"
            "바다낚시지수·물때·금지체장까지 카드에 정리했어요. 저장해두고 주말에 꺼내 보세요!\n"
            "전국 포인트 지도는 프로필 링크에서 👆\n\n"
            "#바다낚시 #주말낚시 #낚시포인트 #물때 #감성돔 #우럭 #볼락 #방파제낚시 #갯바위낚시 #캠낚 #차박")


def page_html(n_cards, days, blog, caption):
    e = html.escape
    imgs = "".join(f'<a href="card-{i:02d}.png" download><img src="card-{i:02d}.png" alt="카드 {i}"></a>' for i in range(1, n_cards + 1))
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>이번 주말 낚시 콘텐츠</title><style>
body{{margin:0;font-family:-apple-system,"Noto Sans KR",sans-serif;background:#f2f5f8;color:#13202b}}
main{{max-width:720px;margin:0 auto;padding:20px 16px 60px}}h1{{font-size:22px}}p{{color:#5b6b77;font-size:14px;line-height:1.6}}
.g{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:16px 0}}.g img{{width:100%;border-radius:10px;display:block;box-shadow:0 2px 8px rgba(0,0,0,.12)}}
h2{{font-size:17px;margin-top:28px}}textarea{{width:100%;box-sizing:border-box;height:260px;font:14px/1.6 inherit;border:1px solid #d6e0e8;border-radius:10px;padding:12px}}
button{{margin-top:8px;border:0;border-radius:10px;background:#1673b5;color:#fff;font-weight:700;font-size:15px;padding:12px 18px;width:100%}}</style></head><body><main>
<h1>이번 주말({e(fmt_day(days[0]))}~{e(fmt_day(days[-1]))}) 낚시 콘텐츠</h1><p>매일 아침 바다낚시지수가 갱신될 때 같이 새로 만들어져요. 이미지를 눌러 저장하세요 (아이폰은 길게 눌러 '사진에 저장').</p>
<div class="g">{imgs}</div>
<h2>인스타 캡션</h2><textarea id="c">{e(caption)}</textarea><button onclick="cp('c')">캡션 복사</button>
<h2>블로그 글</h2><textarea id="b">{e(blog)}</textarea><button onclick="cp('b')">블로그 글 복사</button>
<script>function cp(id){{const t=document.getElementById(id);navigator.clipboard.writeText(t.value).then(()=>alert('복사했어요')).catch(()=>{{t.select();document.execCommand('copy')}})}}</script>
</main></body></html>"""


def main():
    today = kst_today()
    sat = today + dt.timedelta(days=(5 - today.weekday()) % 7)
    days = [sat.isoformat(), (sat + dt.timedelta(days=1)).isoformat()]
    spots, regs = load_spots(), load_regs()
    load_index_rows(spots)
    picks, canon, by_alias = pick(spots, regs, set(days))
    if not picks:
        print("주말 지수가 아직 없거나 좋은 곳이 없어 콘텐츠를 만들지 않습니다.")
        return
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        if f.startswith("card-"):
            os.remove(os.path.join(OUT, f))
    cards = build_cards(picks, days, regs, by_alias, canon, today)
    doc = f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{"".join(cards)}</body></html>'
    tmp = os.path.join(OUT, "_cards.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    import shutil
    shutil.copy(os.path.join(SITE, "icon-512.png"), os.path.join(OUT, "icon.png"))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.goto("file://" + os.path.abspath(tmp))
        pg.wait_for_timeout(800)
        for i, el in enumerate(pg.query_selector_all(".card"), 1):
            el.screenshot(path=os.path.join(OUT, f"card-{i:02d}.png"))
        b.close()
    os.remove(tmp)
    blog, cap = blog_text(picks, days, canon, by_alias, today), caption_text(picks, days)
    open(os.path.join(OUT, "blog.txt"), "w", encoding="utf-8").write(blog)
    open(os.path.join(OUT, "caption.txt"), "w", encoding="utf-8").write(cap)
    open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(page_html(len(cards), days, blog, cap))
    print(f"주말 {days} 추천 {len(picks)}곳:", ", ".join(s["name"] for s, _, _ in picks))


if __name__ == "__main__":
    sys.exit(main())
