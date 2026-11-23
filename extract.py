"""
영상 제목·설명·인기 댓글을 Claude가 읽고 '장소' 단위 정보를 뽑습니다. 영상 1개 = 요청 1건.

장소마다: 이름, 지역, 낚시/차박 여부, 종류(방파제·항·해변·노지…), 어종, 화장실·개수대 등 편의시설,
뷰, 팁, 주의사항(금지구역·위험), 시청자 후기.
결과: data/extracted/<video_id>.json  (이미 처리한 영상은 건너뜀)

환경변수: ANTHROPIC_API_KEY(필수), CLAUDE_MODEL(기본 claude-haiku-5-5), CONCURRENCY, EXTRACT_MAX_MINUTES, DRY_RUN
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "6"))
MAX_MINUTES = float(os.environ.get("EXTRACT_MAX_MINUTES", "200"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
DATA = os.environ.get("DATA_DIR", "data")
OUT_DIR = os.path.join(DATA, "extracted")
START = time.time()

SYSTEM = """너는 한국 바다낚시·차박 유튜브 영상 정보를 장소 단위로 정리하는 추출기다.
입력: 영상 제목, 설명, 인기 댓글(번호, 좋아요 수, [채널] 표시는 영상 주인).

규칙
- 영상에서 소개하거나 댓글에서 구체적으로 언급한 '실제 장소'만 뽑는다. 이름이 있는 곳(○○방파제, ○○항, ○○해수욕장, ○○선착장, ○○방조제, ○○계곡, ○○공원 등).
- region: "시도 시군구" 형태(예: "충남 서산", "강원 고성", "경남 거제"). 모르면 영상 맥락으로 추정하고, 정말 모르면 "".
- fishing: 낚시 장소로 소개/언급되면 true. chabak: 차박·노지캠핑·야영이 가능하다고 소개/언급되면 true.
- kind: 방파제/항구/갯바위/선착장/방조제/해변/계곡/강변/노지/무료캠핑장/공원/주차장/기타 중 하나.
- species: 그 장소에서 잡힌다고 한 어종(설명란 '어종:' 목록 포함).
- facilities: 영상·설명·댓글에 근거가 있을 때만 true/false, 근거 없으면 null.
  toilet(화장실), flush_toilet(수세식), water(개수대·음수대), shower, free_parking, free(무료), cooking_ok(취사 가능).
- view: 뷰 특징 짧게(예: "바다 앞", "일몰", "오션뷰", "계곡").
- tips: 실전 팁 짧은 문장 최대 5개(추천 채비, 시즌, 좋은 시간대, 주차 위치 등).
- warnings: 금지·위험·민원 정보 최대 5개(낚시 금지구역, 야영·취사 금지, 테트라포드 위험, 주차장법 등). 근거가 있을 때만.
- reviews: 그 장소에 대한 시청자 경험담 댓글 번호와 평가. good(좋았다/잘 잡혔다), bad(꽝/별로/금지), mixed.
- 장소가 하나도 없으면 spots를 빈 배열로.
결과는 report_spots 도구로 보고한다."""

TOOL = {
    "name": "report_spots",
    "description": "영상에서 찾은 장소들을 보고한다.",
    "input_schema": {
        "type": "object",
        "properties": {"spots": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "region": {"type": "string"},
                "fishing": {"type": "boolean"}, "chabak": {"type": "boolean"},
                "kind": {"type": "string"},
                "species": {"type": "array", "items": {"type": "string"}},
                "facilities": {"type": "object", "properties": {
                    k: {"type": ["boolean", "null"]} for k in
                    ["toilet", "flush_toilet", "water", "shower", "free_parking", "free", "cooking_ok"]}},
                "view": {"type": "array", "items": {"type": "string"}},
                "tips": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "reviews": {"type": "array", "items": {"type": "object", "properties": {
                    "comment": {"type": "integer"}, "verdict": {"type": "string", "enum": ["good", "bad", "mixed"]}},
                    "required": ["comment", "verdict"]}},
            },
            "required": ["name", "region", "fishing", "chabak"],
        }}},
        "required": ["spots"],
    },
}

_client = None


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(max_retries=8)
    return _client


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def build_prompt(v, comments):
    lines = [f"영상 제목: {v['title']}", f"채널: {v['channel']} (주제: {'낚시' if v['category'] == 'fishing' else '차박'})",
             "설명:", v.get("description", "")[:3000], "", "인기 댓글:"]
    budget = 9000
    for i, c in enumerate(comments):
        t = c["text"].replace("\n", " ")[:400]
        line = f"{i}\t👍{c['likes']}{' [채널]' if c.get('is_creator') else ''}\t{t}"
        budget -= len(line)
        if budget < 0:
            break
        lines.append(line)
    return "\n".join(lines)


def extract_one(v):
    comments = load_json(os.path.join(DATA, "comments", v["video_id"] + ".json"), [])
    prompt = build_prompt(v, comments)
    for attempt in range(3):
        msg = client().messages.create(
            model=MODEL, max_tokens=6000, system=SYSTEM, tools=[TOOL],
            tool_choice={"type": "tool", "name": "report_spots"},
            messages=[{"role": "user", "content": prompt}])
        for b in msg.content:
            if getattr(b, "type", "") == "tool_use" and isinstance(b.input, dict):
                spots = b.input.get("spots") or []
                # 후기 댓글 번호를 실제 댓글로 바꿔 저장 (사람 ID는 중복 집계용 해시로만)
                for s in spots:
                    revs = []
                    for r in s.get("reviews") or []:
                        try:
                            c = comments[int(r["comment"])]
                        except (KeyError, ValueError, IndexError, TypeError):
                            continue
                        revs.append({"text": c["text"][:300], "likes": c["likes"], "verdict": r.get("verdict")})
                    s["reviews"] = revs
                return spots
        if getattr(msg, "stop_reason", "") == "max_tokens":
            prompt = prompt[: len(prompt) * 2 // 3]  # 너무 길면 줄여서 재시도
    return []


def main():
    if not DRY_RUN and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY 환경변수가 없습니다.")
    os.makedirs(OUT_DIR, exist_ok=True)
    videos = load_json(os.path.join(DATA, "videos.json"), [])
    todo = [v for v in videos
            if os.path.exists(os.path.join(DATA, "comments", v["video_id"] + ".json"))
            and not os.path.exists(os.path.join(OUT_DIR, v["video_id"] + ".json"))]
    print(f"추출할 영상 {len(todo)}개")
    if DRY_RUN:
        return

    def run(v):
        spots = extract_one(v)
        with open(os.path.join(OUT_DIR, v["video_id"] + ".json"), "w", encoding="utf-8") as f:
            json.dump({"video": {k: v[k] for k in ("video_id", "title", "channel", "category", "published_at")},
                       "spots": spots}, f, ensure_ascii=False)
        return len(spots)

    done = n_spots = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        it = iter(todo)
        futures = set()
        while True:
            while len(futures) < CONCURRENCY * 2 and (time.time() - START) / 60 < MAX_MINUTES:
                v = next(it, None)
                if v is None:
                    break
                futures.add(ex.submit(run, v))
            if not futures:
                break
            fu = next(as_completed(futures))
            futures.discard(fu)
            try:
                n_spots += fu.result()
            except Exception as e:  # 한 영상 실패로 전체를 멈추지 않음 (다음 실행 때 다시 시도)
                print(f"  영상 1개 실패, 다음에 다시 시도: {e}")
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(todo)}개 영상, 장소 {n_spots}건", flush=True)
    print(f"이번 실행: 영상 {done}개에서 장소 {n_spots}건 추출")


if __name__ == "__main__":
    main()
