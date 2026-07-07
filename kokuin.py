# -*- coding: utf-8 -*-
"""
刻印切り出し(GitHub Actions版)

30分おきにGitHubのサーバー上で実行され、共有ドライブの監視フォルダに
新しい動画があれば、刻印が最も正面で読めるフレームを選んで刻印部分を
切り出し、連番画像として保存先フォルダにアップロードします。

実行モード:
  python kokuin.py check   … 新着動画があるかだけ確認(軽量・毎回実行)
  python kokuin.py process … 実際の切り出し処理(新着があるときだけ実行)

必要な環境変数(GitHubのSecrets/Variablesから渡される):
  GDRIVE_SERVICE_ACCOUNT … サービスアカウントのJSONキー(中身まるごと)
  SOURCE_FOLDER          … 監視フォルダのURL(またはID)
  OUTPUT_FOLDER          … 保存先フォルダのURL(またはID)
"""

import io
import os
import re
import sys
import json
import math
import tempfile

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
PROCESSED_FILE = "processed.json"

FILE_PREFIX = os.environ.get("FILE_PREFIX", "kokuin_")
FRAME_INTERVAL_SEC = float(os.environ.get("FRAME_INTERVAL_SEC", "1"))
MAX_FRAMES = int(os.environ.get("MAX_FRAMES", "120"))
CROP_MARGIN = float(os.environ.get("CROP_MARGIN", "0.15"))
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.3"))
OCR_LANGS = os.environ.get("OCR_LANGS", "en").split(",")


def id_of(s):
    """フォルダURLからIDを取り出す(ID単体ならそのまま)"""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s or "")
    return m.group(1) if m else (s or "").strip()


def drive():
    info = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
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
    folder = id_of(os.environ["SOURCE_FOLDER"])
    res = svc.files().list(
        q=f"'{folder}' in parents and mimeType contains 'video/' and trashed = false",
        fields="files(id, name)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute()
    return [f for f in res.get("files", []) if f["id"] not in processed]


# ============ checkモード:新着があるかだけ調べる ============
def check():
    svc = drive()
    new = list_new_videos(svc, load_processed())
    found = "true" if new else "false"
    print(f"新着動画: {len(new)}件 " + ", ".join(f["name"] for f in new))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"found={found}\n")


# ============ processモード:切り出し本処理 ============
def process():
    import cv2
    import easyocr

    svc = drive()
    processed = load_processed()
    reader = None

    for f in list_new_videos(svc, processed):
        print(f"処理開始: {f['name']}")
        try:
            if reader is None:
                print("OCRモデルを読み込み中...")
                reader = easyocr.Reader(OCR_LANGS, verbose=False)
            handle_video(svc, reader, f, cv2)
        except Exception as e:
            print(f"  エラー: {e}")
        processed.add(f["id"])
        save_processed(processed)


def handle_video(svc, reader, f, cv2):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "video")
        req = svc.files().get_media(fileId=f["id"], supportsAllDrives=True)
        with open(path, "wb") as fp:
            dl = MediaIoBaseDownload(fp, req, chunksize=32 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()

        best = find_best_frame(path, reader, cv2)
        if best is None:
            print("  → 刻印(文字)を検出できませんでした")
            return

        image = crop(best)
        name = f"{FILE_PREFIX}{next_number(svc):04d}.jpg"
        upload(svc, image, name, cv2, tmp)
        print(f"  → {name} を保存({best['time']:.1f}秒地点 / 「{best['text']}」)")


def find_best_frame(video_path, reader, cv2):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, int(fps * FRAME_INTERVAL_SEC))

    best, checked = None, 0
    for idx in range(0, total, step):
        if checked >= MAX_FRAMES:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        checked += 1

        results = [r for r in reader.readtext(frame) if r[2] >= MIN_CONFIDENCE]
        if not results:
            continue

        xs = [p[0] for r in results for p in r[0]]
        ys = [p[1] for r in results for p in r[0]]
        x, y = int(min(xs)), int(min(ys))
        w, h = int(max(xs)) - x, int(max(ys)) - y
        if w <= 0 or h <= 0:
            continue

        conf = sum(r[2] for r in results) / len(results)
        top = max(results, key=lambda r: r[2])[0]
        box_w = max(abs(top[1][0] - top[0][0]), 1)
        skew = abs(top[1][1] - top[0][1]) / box_w
        frontal = max(0.0, 1.0 - skew * 3)
        score = conf * frontal * math.sqrt(w * h)
        text = " ".join(r[1] for r in results)
        t = idx / fps
        print(f"  {t:6.1f}秒: 「{text}」 信頼度{conf:.2f} 正面度{frontal:.2f}")

        if best is None or score > best["score"]:
            best = {"score": score, "time": t, "frame": frame.copy(),
                    "x": x, "y": y, "w": w, "h": h, "text": text}
    cap.release()
    return best


def crop(best):
    frame = best["frame"]
    fh, fw = frame.shape[:2]
    mx = int(best["w"] * CROP_MARGIN)
    my = int(best["h"] * CROP_MARGIN)
    left, top = max(0, best["x"] - mx), max(0, best["y"] - my)
    right = min(fw, best["x"] + best["w"] + mx)
    bottom = min(fh, best["y"] + best["h"] + my)
    return frame[top:bottom, left:right]


def next_number(svc):
    folder = id_of(os.environ["OUTPUT_FOLDER"])
    res = svc.files().list(
        q=f"'{folder}' in parents and name contains '{FILE_PREFIX}' and trashed = false",
        fields="files(name)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute()
    max_n = 0
    for f in res.get("files", []):
        m = re.match(rf"{re.escape(FILE_PREFIX)}(\d+)", f["name"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def upload(svc, image, name, cv2, tmp):
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError("JPEGエンコードに失敗")
    path = os.path.join(tmp, name)
    with open(path, "wb") as fp:
        fp.write(buf.tobytes())
    media = MediaFileUpload(path, mimetype="image/jpeg")
    svc.files().create(
        body={"name": name, "parents": [id_of(os.environ["OUTPUT_FOLDER"])]},
        media_body=media,
        supportsAllDrives=True,
    ).execute()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        check()
    else:
        process()
