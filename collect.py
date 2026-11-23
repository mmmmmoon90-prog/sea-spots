"""
channels.json의 낚시·차박 채널에서 영상 제목·설명과 인기 댓글을 YouTube Data API로 모읍니다.

- 영상마다 인기순 댓글 최대 COMMENTS_PER_VIDEO개(기본 200)만 모음 (장소 후기 위주라 전체 댓글은 불필요)
- 하루 할당량을 다 쓰면 멈췄다가 다음 실행 때 이어서 진행
- 이미 모은 영상은 다시 모으지 않음. 새로 올라온 영상만 추가
- 결과: data/videos.json, data/comments/<video_id>.json

환경변수: YOUTUBE_API_KEY(필수), COMMENTS_PER_VIDEO, QUOTA_BUDGET, MAX_VIDEOS(테스트용)
"""
import json
import os
import sys
import time

import requests

API = os.environ.get("YOUTUBE_API_BASE", "https://www.googleapis.com/youtube/v3")
KEY = os.environ.get("YOUTUBE_API_KEY", "")
PER_VIDEO = int(os.environ.get("COMMENTS_PER_VIDEO", "200"))
QUOTA_BUDGET = int(os.environ.get("QUOTA_BUDGET", "9500"))
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "0") or 0)
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", "data")
VIDEOS_FILE = os.path.join(DATA, "videos.json")
COMMENTS_DIR = os.path.join(DATA, "comments")

units = 0


class QuotaExceeded(Exception):
    pass


class Unavailable(Exception):
    pass


def api_get(endpoint, **params):
    global units
    if units >= QUOTA_BUDGET:
        raise QuotaExceeded("이번 실행 예산 소진")
    params["key"] = KEY
    for attempt in range(6):
        try:
            r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        units += 1
        if r.status_code == 200:
            return r.json()
        try:
            reason = (r.json().get("error", {}).get("errors") or [{}])[0].get("reason", "")
        except ValueError:
            reason = ""
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            raise QuotaExceeded("YouTube API 일일 할당량 소진")
        if reason in ("commentsDisabled", "videoNotFound", "forbidden") or r.status_code == 404:
            raise Unavailable(reason or "404")
        if r.status_code >= 500 or "RateLimit" in reason:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"API 오류 {r.status_code} {reason}")
    raise RuntimeError("요청이 계속 실패했습니다")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def channel_videos(ch):
    info = api_get("channels", part="contentDetails", id=ch["id"])
    items = info.get("items") or []
    if not items:
        print(f"  채널을 찾지 못함: {ch['name']}")
        return []
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    out, token = [], None
    while True:
        params = dict(part="snippet", playlistId=uploads, maxResults=50)
        if token:
            params["pageToken"] = token
        page = api_get("playlistItems", **params)
        for it in page.get("items", []):
            sn = it["snippet"]
            vid = sn.get("resourceId", {}).get("videoId")
            title = sn.get("title", "")
            if vid and (not ch["keywords"] or any(k in title for k in ch["keywords"])):
                out.append({"video_id": vid, "title": title, "description": sn.get("description", "")[:5000],
                            "published_at": sn.get("publishedAt", ""), "channel": ch["name"],
                            "category": ch["category"]})
        token = page.get("nextPageToken")
        if not token:
            return out


def top_comments(vid):
    out, token = [], None
    while len(out) < PER_VIDEO:
        params = dict(part="snippet", videoId=vid, maxResults=100, order="relevance", textFormat="plainText")
        if token:
            params["pageToken"] = token
        page = api_get("commentThreads", **params)
        for th in page.get("items", []):
            sn = th["snippet"]["topLevelComment"]["snippet"]
            out.append({"id": th["id"], "author": (sn.get("authorChannelId") or {}).get("value", ""),
                        "text": sn.get("textOriginal", "")[:600], "likes": sn.get("likeCount", 0),
                        "is_creator": (sn.get("authorChannelId") or {}).get("value") == sn.get("channelId")})
        token = page.get("nextPageToken")
        if not token:
            break
    return out[:PER_VIDEO]


def main():
    if not KEY:
        sys.exit("YOUTUBE_API_KEY 환경변수가 없습니다.")
    os.makedirs(COMMENTS_DIR, exist_ok=True)
    cfg = load_json(os.path.join(HERE, "channels.json"), {})["channels"]
    known = {v["video_id"]: v for v in load_json(VIDEOS_FILE, [])}

    try:
        current = set()
        for ch in cfg:
            found = channel_videos(ch)
            new = [v for v in found if v["video_id"] not in known]
            for v in found:
                known.setdefault(v["video_id"], v)
                current.add(v["video_id"])
            print(f"{ch['name']}: 대상 영상 {len(found)}개 (새 영상 {len(new)}개)")
        # 키워드를 바꿔 더는 해당하지 않는 영상은 목록에서 뺌(이미 추출한 영상은 남김)
        ext_dir = os.path.join(os.path.dirname(COMMENTS_DIR), "extracted")
        known = {k: v for k, v in known.items()
                 if k in current or os.path.exists(os.path.join(ext_dir, k + ".json"))}
        save_json(VIDEOS_FILE, list(known.values()))

        todo = [v for v in known.values() if not os.path.exists(os.path.join(COMMENTS_DIR, v["video_id"] + ".json"))]
        if MAX_VIDEOS:
            todo = todo[:MAX_VIDEOS]
        for i, v in enumerate(todo, 1):
            try:
                cs = top_comments(v["video_id"])
            except Unavailable:
                cs = []
            save_json(os.path.join(COMMENTS_DIR, v["video_id"] + ".json"), cs)
            if i % 50 == 0:
                print(f"  댓글 {i}/{len(todo)}개 영상 완료 (사용 {units} units)", flush=True)
    except QuotaExceeded as e:
        print(f"멈춤: {e}. 다음 실행 때 이어서 수집합니다.")
        save_json(VIDEOS_FILE, list(known.values()))

    done = sum(1 for v in known.values() if os.path.exists(os.path.join(COMMENTS_DIR, v["video_id"] + ".json")))
    print(f"\n진행 상황: 영상 {done}/{len(known)}개 수집 완료, 이번 실행 {units} units 사용")


if __name__ == "__main__":
    main()
