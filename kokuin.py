# -*- coding: utf-8 -*-
"""刻印切り出し(動体検出+追跡版)
固定カメラの映像から「動いてくるボンベ」を検出・追跡し、
1本につき1枚、最も大きく鮮明に写った瞬間を切り出して保存する。
AIの学習・モデルファイルは不要。

  python kokuin.py check   … 新着確認
  python kokuin.py process … 切り出し本処理
"""

import io
import os
import re
import sys
import json
import time
import tempfile
from datetime import datetime, timezone, timedelta

SCOPES = ["https://www.googleapis.com/auth/drive"]
PROCESSED_FILE = "processed.json"
CSV_NAME = "処理結果一覧.csv"
JST = timezone(timedelta(hours=9))

SOURCE_URL = "https://drive.google.com/drive/folders/15qwtydkXB0OYdopFPVyzPbzN2eEulXOE"
OUTPUT_URL = "https://drive.google.com/drive/folders/1nTp2jHx0MZLCJLKUJodWEfFToPnNU0wP"

FILE_PREFIX = "kokuin_"

# ==== 調整パラメータ ====
DETECT_STRIDE = 2       # 何フレームおきに解析するか(大きいほど速い)
MIN_AREA_RATIO = 0.03   # 画面の何割以上の「動く塊」をボンベとみなすか
MATCH_DIST_RATIO = 0.2  # 追跡:前回位置からこの割合(画面幅比)以内なら同じボンベ
MIN_TRACK_FRAMES = 5    # これ未満しか映らなかった塊はノイズとして無視
CROP_MARGIN = 0.10      # 切り出しの余白(ボンベ枠に対する割合)


def id_of(s):
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s or "")
    return m.group(1) if m else (s or "").strip()


SRC = id_of(SOURCE_URL)
OUT = id_of(OUTPUT_URL)


def with_retry(fn, what="通信", tries=3):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"  {what}に失敗({e})。再試行します...")
            time.sleep(3 * (i + 1))


def drive():
    import google.auth
    from googleapiclient.discovery import build
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    return set()


def save_processed(ids):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=1)


def list_new_videos(svc, processed):
    res = with_retry(lambda: svc.files().list(
        q=f"'{SRC}' in parents and mimeType contains 'video/' and trashed = false",
        fields="files(id, name)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute(), "フォルダの確認")
    return [f for f in res.get("files", []) if f["id"] not in processed]


def check():
    svc = drive()
    new = list_new_videos(svc, load_processed())
    print(f"新着動画: {len(new)}件 " + ", ".join(f["name"] for f in new))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"found={'true' if new else 'false'}\n")


# ============================================================
#  動体検出+追跡(この関数が本体)
# ============================================================
def analyze_video(path, log=print):
    """動画から「流れてきたボンベ」を追跡し、1本につき最良の1枚を返す。
    返り値: [{time, crop, seen}, ...] 現れた順"""
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("動画を開けませんでした")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30

    bg = cv2.createBackgroundSubtractorMOG2(
        history=300, varThreshold=32, detectShadows=True)
    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))

    tracks = []   # {id, cx, cy, seen, miss, best:{score,time,frame,box}}
    next_id = 1
    frame_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1
        if frame_idx % DETECT_STRIDE:
            continue

        fh, fw = frame.shape[:2]
        # 解析は縮小画像で(高速化)。切り出しは元解像度から行う
        scale = min(1.0, 640.0 / fw)
        small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1.0 else frame
        sh, sw = small.shape[:2]

        fgmask = bg.apply(small)
        _, fgmask = cv2.threshold(fgmask, 200, 255, cv2.THRESH_BINARY)  # 影(127)を除去
        fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_OPEN, k_open)
        fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_CLOSE, k_close)

        contours, _ = cv2.findContours(fgmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        dets = []
        for c in contours:
            if cv2.contourArea(c) < MIN_AREA_RATIO * sw * sh:
                continue
            x, y, w, h = cv2.boundingRect(c)
            dets.append((x / scale, y / scale, w / scale, h / scale))

        t = frame_idx / fps
        gray = None
        used = set()
        for (x, y, w, h) in dets:
            cx, cy = x + w / 2, y + h / 2
            # 既存の追跡と照合(近い位置=同じボンベ)
            cand = None
            best_d = MATCH_DIST_RATIO * fw
            for tr in tracks:
                if tr["id"] in used or tr["miss"] > 30:
                    continue
                d = ((tr["cx"] - cx) ** 2 + (tr["cy"] - cy) ** 2) ** 0.5
                if d < best_d:
                    best_d = d
                    cand = tr
            if cand is None:
                cand = {"id": next_id, "cx": cx, "cy": cy,
                        "seen": 0, "miss": 0, "best": {"score": -1}}
                tracks.append(cand)
                next_id += 1
            used.add(cand["id"])
            cand.update({"cx": cx, "cy": cy, "miss": 0})
            cand["seen"] += 1

            # スコア = 大きさ × 鮮明度(最も大きく・ブレずに写った瞬間を採用)
            if gray is None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            region = gray[int(y):int(y + h), int(x):int(x + w)]
            sharp = cv2.Laplacian(region, cv2.CV_64F).var() if region.size else 0
            sharpness = min(1.0, sharp / 150.0)
            score = (w * h) * (0.4 + 0.6 * sharpness)
            if score > cand["best"]["score"]:
                cand["best"] = {"score": score, "time": t,
                                "frame": frame.copy(), "box": (x, y, w, h)}

        for tr in tracks:
            if tr["id"] not in used:
                tr["miss"] += 1

    cap.release()

    results = []
    for tr in tracks:
        if tr["seen"] < MIN_TRACK_FRAMES or tr["best"]["score"] < 0:
            continue
        b = tr["best"]
        results.append({"time": b["time"], "seen": tr["seen"],
                        "crop": crop_box(b["frame"], b["box"])})
        log(f"  ボンベ{len(results)}: {b['time']:.1f}秒地点を採用({tr['seen']}回検出)")
    return results


def crop_box(frame, box):
    fh, fw = frame.shape[:2]
    x, y, w, h = box
    mx, my = w * CROP_MARGIN, h * CROP_MARGIN
    left = max(0, int(x - mx))
    top = max(0, int(y - my))
    right = min(fw, int(x + w + mx))
    bottom = min(fh, int(y + h + my))
    return frame[top:bottom, left:right]


# ============================================================
#  Drive まわり
# ============================================================
def process():
    import cv2  # 依存確認

    svc = drive()
    processed = load_processed()
    new = list_new_videos(svc, processed)
    if not new:
        print("新着なし")
        return

    for f in new:
        print(f"処理開始: {f['name']}")
        try:
            handle_video(svc, f)
        except Exception as e:
            print(f"  エラー: {e}")
        processed.add(f["id"])
        save_processed(processed)


def handle_video(svc, f):
    import cv2
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "video.mp4")
        download_video(svc, f["id"], path)

        results = analyze_video(path)
        if not results:
            print("  → ボンベを検出できませんでした")
            append_result_csv(svc, tmp, [now_jst(), f["name"], "(検出なし)", "", ""])
            return

        for r in results:
            name = f"{FILE_PREFIX}{next_number(svc):04d}.jpg"
            upload_image(svc, r["crop"], name, cv2, tmp)
            append_result_csv(svc, tmp, [now_jst(), f["name"], name,
                                         f"{r['time']:.1f}", f"{r['seen']}回検出"])
            print(f"  → {name} を保存({r['time']:.1f}秒地点)")
        print(f"  合計 {len(results)} 本ぶんの画像を保存しました")


def download_video(svc, file_id, dest):
    from googleapiclient.http import MediaIoBaseDownload

    def _dl():
        req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(dest, "wb") as fp:
            dl = MediaIoBaseDownload(fp, req, chunksize=32 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
    with_retry(_dl, "動画のダウンロード")


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
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError("JPEGエンコードに失敗")
    path = os.path.join(tmp, name)
    with open(path, "wb") as fp:
        fp.write(buf.tobytes())
    media = MediaFileUpload(path, mimetype="image/jpeg")
    with_retry(lambda: svc.files().create(
        body={"name": name, "parents": [OUT]},
        media_body=media,
        supportsAllDrives=True,
    ).execute(), "画像のアップロード")


def now_jst():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def append_result_csv(svc, tmp, row):
    from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
    line = ",".join('"' + str(c).replace('"', '""').replace("\n", " ") + '"' for c in row)
    res = with_retry(lambda: svc.files().list(
        q=f"'{OUT}' in parents and name = '{CSV_NAME}' and trashed = false",
        fields="files(id)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute(), "CSV確認")
    files = res.get("files", [])

    if files:
        buf = io.BytesIO()
        req = svc.files().get_media(fileId=files[0]["id"], supportsAllDrives=True)
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        content = buf.getvalue().decode("utf-8-sig", errors="replace").rstrip("\n")
        content += "\n" + line + "\n"
    else:
        header = '"処理日時","動画ファイル名","保存画像名","動画内の秒数","備考"'
        content = header + "\n" + line + "\n"

    path = os.path.join(tmp, "results.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        fp.write(content)
    media = MediaFileUpload(path, mimetype="text/csv")

    if files:
        with_retry(lambda: svc.files().update(
            fileId=files[0]["id"], media_body=media, supportsAllDrives=True,
        ).execute(), "CSV更新")
    else:
        with_retry(lambda: svc.files().create(
            body={"name": CSV_NAME, "parents": [OUT]},
            media_body=media, supportsAllDrives=True,
        ).execute(), "CSV作成")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        check()
    else:
        process()
