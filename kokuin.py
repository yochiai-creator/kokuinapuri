# -*- coding: utf-8 -*-
"""
刻印切り出し 完全版
固定カメラ動画から、流れてくるボンベを検出・追跡し、
1本につき一番よく写った1枚をGoogle Driveへ保存する。

使い方:
  python kokuin.py check
  python kokuin.py process
"""

import io
import os
import re
import sys
import json
import csv
import time
import tempfile
from datetime import datetime, timezone, timedelta

SCOPES = ["https://www.googleapis.com/auth/drive"]

SOURCE_URL = "https://drive.google.com/drive/folders/15qwtydkXB0OYdopFPVyzPbzN2eEulXOE"
OUTPUT_URL = "https://drive.google.com/drive/folders/1nTp2jHx0MZLCJLKUJodWEfFToPnNU0wP"

PROCESSED_FILE = "processed.json"
CSV_NAME = "処理結果一覧.csv"
FILE_PREFIX = "kokuin_"

JST = timezone(timedelta(hours=9))

# ===== 検出調整 =====
DETECT_STRIDE = 1
RESIZE_WIDTH = 900

MIN_AREA_RATIO = 0.002
MAX_AREA_RATIO = 0.85

MIN_TRACK_FRAMES = 3
MAX_MISS_FRAMES = 45
MATCH_DIST_RATIO = 0.45

CROP_MARGIN = 0.25

DEBUG_SAVE = True
DEBUG_DIR = "debug_kokuin"


def id_of(s):
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s or "")
    return m.group(1) if m else (s or "").strip()


SRC = id_of(SOURCE_URL)
OUT = id_of(OUTPUT_URL)


def now_jst():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def with_retry(fn, what="通信", tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"  {what}に失敗: {e}")
            print("  再試行します...")
            time.sleep(3 * (i + 1))


def drive():
    import google.auth
    from googleapiclient.discovery import build

    creds, _ = google.auth.default(scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_processed(ids):
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def list_new_videos(svc, processed):
    res = with_retry(lambda: svc.files().list(
        q=f"'{SRC}' in parents and mimeType contains 'video/' and trashed = false",
        fields="files(id, name, mimeType)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute(), "新着動画確認")

    return [f for f in res.get("files", []) if f["id"] not in processed]


def check():
    svc = drive()
    new = list_new_videos(svc, load_processed())

    if new:
        print(f"新着動画: {len(new)}件")
        for f in new:
            print(f"  - {f['name']}")
    else:
        print("新着動画: 0件")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"found={'true' if new else 'false'}\n")


# ==========================================================
# 動画解析本体
# ==========================================================

def analyze_video(path, log=print):
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("動画を開けませんでした")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    bg = cv2.createBackgroundSubtractorMOG2(
        history=150,
        varThreshold=18,
        detectShadows=False
    )

    if DEBUG_SAVE:
        os.makedirs(DEBUG_DIR, exist_ok=True)

    tracks = []
    next_id = 1
    frame_idx = 0

    log(f"  動画解析開始 fps={fps:.1f} frames={total}")

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        frame_idx += 1

        if frame_idx % DETECT_STRIDE != 0:
            continue

        fh, fw = frame.shape[:2]

        scale = min(1.0, RESIZE_WIDTH / fw)
        if scale < 1.0:
            small = cv2.resize(frame, None, fx=scale, fy=scale)
        else:
            small = frame.copy()

        sh, sw = small.shape[:2]

        blur = cv2.GaussianBlur(small, (5, 5), 0)

        mask1 = bg.apply(blur)

        gray = cv2.cvtColor(blur, cv2.COLOR_BGR2GRAY)

        if frame_idx == 1:
            prev_gray = gray.copy()

        # 背景差分
        _, mask1 = cv2.threshold(mask1, 70, 255, cv2.THRESH_BINARY)

        # フレーム差分も追加
        if "last_gray" not in locals():
            last_gray = gray.copy()

        diff = cv2.absdiff(gray, last_gray)
        _, mask2 = cv2.threshold(diff, 18, 255, cv2.THRESH_BINARY)
        last_gray = gray.copy()

        mask = cv2.bitwise_or(mask1, mask2)

        k_open = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        k_close = cv2.getStructuringElement(cv2.MORPH_RECT, (45, 45))

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k_open)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k_close)
        mask = cv2.dilate(mask, k_open, iterations=2)

        contours, _ = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE
        )

        dets = []
        min_area = MIN_AREA_RATIO * sw * sh
        max_area = MAX_AREA_RATIO * sw * sh

        for c in contours:
            area = cv2.contourArea(c)

            if area < min_area:
                continue

            if area > max_area:
                continue

            x, y, w, h = cv2.boundingRect(c)

            if w < 25 or h < 25:
                continue

            rect_area = w * h
            fill = area / max(rect_area, 1)

            if fill < 0.15:
                continue

            ratio = w / max(h, 1)

            # 線・影・細すぎるノイズを除外
            if ratio > 8.0:
                continue
            if ratio < 0.10:
                continue

            dets.append((
                x / scale,
                y / scale,
                w / scale,
                h / scale
            ))

        t = frame_idx / fps
        full_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        used_ids = set()

        for x, y, w, h in dets:
            cx = x + w / 2
            cy = y + h / 2

            matched = None
            best_dist = fw * MATCH_DIST_RATIO

            for tr in tracks:
                if tr["id"] in used_ids:
                    continue
                if tr["miss"] > MAX_MISS_FRAMES:
                    continue

                d = ((tr["cx"] - cx) ** 2 + (tr["cy"] - cy) ** 2) ** 0.5

                if d < best_dist:
                    best_dist = d
                    matched = tr

            if matched is None:
                matched = {
                    "id": next_id,
                    "cx": cx,
                    "cy": cy,
                    "seen": 0,
                    "miss": 0,
                    "first_time": t,
                    "last_time": t,
                    "best": {"score": -1},
                }
                tracks.append(matched)
                next_id += 1

            used_ids.add(matched["id"])

            matched["cx"] = cx
            matched["cy"] = cy
            matched["seen"] += 1
            matched["miss"] = 0
            matched["last_time"] = t

            x1 = max(0, int(x))
            y1 = max(0, int(y))
            x2 = min(fw, int(x + w))
            y2 = min(fh, int(y + h))

            region = full_gray[y1:y2, x1:x2]

            if region.size:
                sharp = cv2.Laplacian(region, cv2.CV_64F).var()
            else:
                sharp = 0

            size_score = w * h
            sharp_score = min(1.0, sharp / 120.0)

            # 端すぎる画像は少し減点
            center_x = cx / fw
            edge_penalty = 1.0
            if center_x < 0.08 or center_x > 0.92:
                edge_penalty = 0.65

            score = size_score * (0.45 + 0.55 * sharp_score) * edge_penalty

            if score > matched["best"]["score"]:
                matched["best"] = {
                    "score": score,
                    "time": t,
                    "frame": frame.copy(),
                    "box": (x, y, w, h),
                    "sharp": sharp,
                }

        for tr in tracks:
            if tr["id"] not in used_ids:
                tr["miss"] += 1

        if DEBUG_SAVE and frame_idx % 30 == 0:
            dbg = frame.copy()

            for x, y, w, h in dets:
                cv2.rectangle(
                    dbg,
                    (int(x), int(y)),
                    (int(x + w), int(y + h)),
                    (0, 255, 0),
                    3
                )

            cv2.imwrite(
                os.path.join(DEBUG_DIR, f"debug_{frame_idx:06d}.jpg"),
                dbg
            )

    cap.release()

    tracks = sorted(
        tracks,
        key=lambda tr: tr["best"].get("time", 999999)
    )

    results = []

    for tr in tracks:
        if tr["seen"] < MIN_TRACK_FRAMES:
            continue

        if tr["best"]["score"] < 0:
            continue

        duration = tr["last_time"] - tr["first_time"]

        # 瞬間ノイズ除外
        if duration < 0.1 and tr["seen"] < 5:
            continue

        b = tr["best"]

        crop = crop_box(b["frame"], b["box"])

        results.append({
            "time": b["time"],
            "seen": tr["seen"],
            "crop": crop,
            "sharp": b.get("sharp", 0),
        })

        log(
            f"  ボンベ{len(results)}: "
            f"{b['time']:.1f}秒地点を採用 "
            f"({tr['seen']}回検出 / sharp={b.get('sharp', 0):.1f})"
        )

    log(f"  検出結果: {len(results)}本")

    return results


def crop_box(frame, box):
    fh, fw = frame.shape[:2]
    x, y, w, h = box

    mx = w * CROP_MARGIN
    my = h * CROP_MARGIN

    left = max(0, int(x - mx))
    top = max(0, int(y - my))
    right = min(fw, int(x + w + mx))
    bottom = min(fh, int(y + h + my))

    return frame[top:bottom, left:right]


# ==========================================================
# Google Drive処理
# ==========================================================

def process():
    import cv2

    svc = drive()
    processed = load_processed()
    new = list_new_videos(svc, processed)

    if not new:
        print("新着なし")
        return

    print(f"処理対象: {len(new)}件")

    for f in new:
        print(f"処理開始: {f['name']}")

        try:
            handle_video(svc, f)
            processed.add(f["id"])
            save_processed(processed)
            print("  処理済みに登録しました")
        except Exception as e:
            print(f"  エラー: {e}")
            print("  処理済みには登録しません")


def handle_video(svc, f):
    import cv2

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "video.mp4")

        download_video(svc, f["id"], path)

        results = analyze_video(path)

        if not results:
            print("  → ボンベを検出できませんでした")
            append_result_csv(
                svc,
                tmp,
                [now_jst(), f["name"], "(検出なし)", "", ""]
            )
            return

        for r in results:
            number = next_number(svc)
            name = f"{FILE_PREFIX}{number:04d}.jpg"

            upload_image(svc, r["crop"], name, cv2, tmp)

            append_result_csv(
                svc,
                tmp,
                [
                    now_jst(),
                    f["name"],
                    name,
                    f"{r['time']:.1f}",
                    f"{r['seen']}回検出 sharp={r.get('sharp', 0):.1f}",
                ]
            )

            print(f"  → {name} を保存しました")

        print(f"  合計 {len(results)}枚保存しました")


def download_video(svc, file_id, dest):
    from googleapiclient.http import MediaIoBaseDownload

    def _download():
        req = svc.files().get_media(
            fileId=file_id,
            supportsAllDrives=True
        )

        with open(dest, "wb") as fp:
            dl = MediaIoBaseDownload(
                fp,
                req,
                chunksize=32 * 1024 * 1024
            )

            done = False
            while not done:
                status, done = dl.next_chunk()
                if status:
                    print(f"  ダウンロード {int(status.progress() * 100)}%")

    with_retry(_download, "動画ダウンロード")


def next_number(svc):
    res = with_retry(lambda: svc.files().list(
        q=f"'{OUT}' in parents and name contains '{FILE_PREFIX}' and trashed = false",
        fields="files(name)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute(), "連番確認")

    max_n = 0

    for f in res.get("files", []):
        m = re.match(rf"{re.escape(FILE_PREFIX)}(\d+)", f["name"])
        if m:
            max_n = max(max_n, int(m.group(1)))

    return max_n + 1


def upload_image(svc, image, name, cv2, tmp):
    from googleapiclient.http import MediaFileUpload

    ok, buf = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, 95]
    )

    if not ok:
        raise RuntimeError("JPEGエンコードに失敗しました")

    path = os.path.join(tmp, name)

    with open(path, "wb") as fp:
        fp.write(buf.tobytes())

    media = MediaFileUpload(path, mimetype="image/jpeg")

    with_retry(lambda: svc.files().create(
        body={
            "name": name,
            "parents": [OUT],
        },
        media_body=media,
        supportsAllDrives=True,
    ).execute(), "画像アップロード")


def append_result_csv(svc, tmp, row):
    from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

    res = with_retry(lambda: svc.files().list(
        q=f"'{OUT}' in parents and name = '{CSV_NAME}' and trashed = false",
        fields="files(id)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute(), "CSV確認")

    files = res.get("files", [])

    rows = []

    if files:
        buf = io.BytesIO()

        req = svc.files().get_media(
            fileId=files[0]["id"],
            supportsAllDrives=True
        )

        dl = MediaIoBaseDownload(buf, req)

        done = False
        while not done:
            _, done = dl.next_chunk()

        content = buf.getvalue().decode("utf-8-sig", errors="replace")

        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
    else:
        rows.append([
            "処理日時",
            "動画ファイル名",
            "保存画像名",
            "動画内の秒数",
            "備考",
        ])

    rows.append(row)

    path = os.path.join(tmp, "results.csv")

    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerows(rows)

    media = MediaFileUpload(path, mimetype="text/csv")

    if files:
        with_retry(lambda: svc.files().update(
            fileId=files[0]["id"],
            media_body=media,
            supportsAllDrives=True,
        ).execute(), "CSV更新")
    else:
        with_retry(lambda: svc.files().create(
            body={
                "name": CSV_NAME,
                "parents": [OUT],
            },
            media_body=media,
            supportsAllDrives=True,
        ).execute(), "CSV作成")


# ==========================================================
# 起動
# ==========================================================

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"

    if mode == "check":
        check()
    elif mode == "process":
        process()
    else:
        print("使い方:")
        print("  python kokuin.py check")
        print("  python kokuin.py process")
