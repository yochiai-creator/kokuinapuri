# -*- coding: utf-8 -*-
"""
刻印切り出し(GitHub Actions版 v2)

改良点:
 - 金属刻印向けの前処理(コントラスト強調)でOCRの検出率を向上
 - ピンぼけフレームを避ける「鮮明度」をスコアに追加
 - 刻印と無関係な背景の文字に引っ張られないよう、
   最も確からしい文字の周辺だけを切り出す(クラスター切り出し)
 - 通信エラー時の自動リトライ
 - 保存先フォルダに「処理結果一覧.csv」を自動作成・追記
   (処理日時 / 動画名 / 画像名 / 秒数 / 読み取れた文字。Excelで開けます)

実行モード:
  python kokuin.py check   … 新着動画があるかだけ確認(軽量)
  python kokuin.py process … 切り出し本処理
"""

import io
import os
import re
import sys
import json
import math
import time
import tempfile
from datetime import datetime, timezone, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
PROCESSED_FILE = "processed.json"
CSV_NAME = "処理結果一覧.csv"
JST = timezone(timedelta(hours=9))

FILE_PREFIX = os.environ.get("FILE_PREFIX", "kokuin_")
FRAME_INTERVAL_SEC = float(os.environ.get("FRAME_INTERVAL_SEC", "1"))
MAX_FRAMES = int(os.environ.get("MAX_FRAMES", "120"))
CROP_MARGIN = float(os.environ.get("CROP_MARGIN", "0.2"))
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.3"))
OCR_LANGS = os.environ.get("OCR_LANGS", "ja,en").split(",")

# LPガス容器の肩部刻印に含まれる定型パターン
# (多く読み取れているフレームほど「正面から鮮明に写っている」と判断)
STAMP_PATTERNS = [
    r"TP\s?[0-9.,]+",            # 耐圧試験圧力 (例: TP3.0M)
    r"W\s?[0-9]+[.,][0-9]+",     # 質量 (例: W16.9)
    r"V\s?[0-9]+",               # 内容積 (例: V47)
    r"[0-9]{2}\s?-\s?[0-9]{1,2}",# 年月 (例: 12-17)
    r"[0-9]{4,6}",               # 容器番号 (例: 00001)
    r"(LP)?[ガカヵ][スズ]",       # LPガス
]


def pattern_hits(text):
    t = text.replace(" ", "").upper()
    return sum(1 for p in STAMP_PATTERNS if re.search(p, t))


# ============================================================
#  共通ユーティリティ
# ============================================================
def id_of(s):
    """フォルダURLからIDを取り出す(ID単体ならそのまま)"""
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s or "")
    return m.group(1) if m else (s or "").strip()


def with_retry(fn, what="通信", tries=3):
    """一時的な通信エラーに備えて最大3回まで再試行"""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 3 * (i + 1)
            print(f"  {what}に失敗({e})。{wait}秒後に再試行します...")
            time.sleep(wait)


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
    res = with_retry(lambda: svc.files().list(
        q=f"'{folder}' in parents and mimeType contains 'video/' and trashed = false",
        fields="files(id, name, size)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute(), "フォルダの確認")
    return [f for f in res.get("files", []) if f["id"] not in processed]


# ============================================================
#  checkモード:新着があるかだけ調べる(軽量)
# ============================================================
def check():
    svc = drive()
    new = list_new_videos(svc, load_processed())
    print(f"新着動画: {len(new)}件 " + ", ".join(f["name"] for f in new))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"found={'true' if new else 'false'}\n")


# ============================================================
#  processモード:切り出し本処理
# ============================================================
def process():
    import cv2
    import easyocr

    svc = drive()
    processed = load_processed()
    new = list_new_videos(svc, processed)
    if not new:
        print("新着なし")
        return

    print("OCRモデルを読み込み中...")
    reader = easyocr.Reader(OCR_LANGS, verbose=False)

    for f in new:
        print(f"処理開始: {f['name']}")
        try:
            handle_video(svc, reader, f, cv2)
        except Exception as e:
            print(f"  エラー: {e}")
        processed.add(f["id"])
        save_processed(processed)


def handle_video(svc, reader, f, cv2):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "video")
        download_video(svc, f["id"], path)

        best = find_best_frame(path, reader, cv2)
        if best is None:
            print("  → 刻印(文字)を検出できませんでした")
            append_result_csv(svc, tmp, [now_jst(), f["name"], "(検出失敗)", "", ""])
            return

        image = crop(best)
        name = build_filename(svc, best["text"])
        upload_image(svc, image, name, cv2, tmp)
        append_result_csv(svc, tmp,
                          [now_jst(), f["name"], name,
                           f"{best['time']:.1f}", best["text"]])
        print(f"  → {name} を保存({best['time']:.1f}秒地点 / 「{best['text']}」)")


def download_video(svc, file_id, dest):
    def _dl():
        req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(dest, "wb") as fp:
            dl = MediaIoBaseDownload(fp, req, chunksize=32 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
    with_retry(_dl, "動画のダウンロード")


# ---------------- フレーム解析 ----------------
def enhance_for_ocr(frame, cv2):
    """金属刻印向け:グレースケール化+コントラスト強調(CLAHE)"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return gray, clahe.apply(gray)


def bbox_of(detection):
    xs = [p[0] for p in detection[0]]
    ys = [p[1] for p in detection[0]]
    return min(xs), min(ys), max(xs), max(ys)


def cluster_bbox(results):
    """最も信頼度の高い文字の近くにある検出をまとめた枠を返す。
    容器の刻印は円弧状に広く並ぶため、横方向はかなり広めに取り、
    離れた場所の無関係な文字(ラベル等)だけを除外する"""
    top = max(results, key=lambda r: r[2])
    tx1, ty1, tx2, ty2 = bbox_of(top)
    th = max(ty2 - ty1, 1)
    cx, cy = (tx1 + tx2) / 2, (ty1 + ty2) / 2

    keep = []
    for r in results:
        x1, y1, x2, y2 = bbox_of(r)
        rx, ry = (x1 + x2) / 2, (y1 + y2) / 2
        if abs(ry - cy) <= th * 8 and abs(rx - cx) <= th * 40:
            keep.append(r)

    xs, ys = [], []
    for r in keep:
        x1, y1, x2, y2 = bbox_of(r)
        xs += [x1, x2]
        ys += [y1, y2]
    x, y = int(min(xs)), int(min(ys))
    w, h = int(max(xs)) - x, int(max(ys)) - y
    return x, y, w, h, keep, top


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

        gray, enhanced = enhance_for_ocr(frame, cv2)
        results = [r for r in reader.readtext(enhanced) if r[2] >= MIN_CONFIDENCE]
        if not results:
            continue

        x, y, w, h, keep, top = cluster_bbox(results)
        if w <= 0 or h <= 0:
            continue

        # 読み取れた文字量(信頼度の合計)…正面ほど全文字が読めて大きくなる
        total_conf = sum(r[2] for r in keep)

        # 鮮明度:文字領域のピンぼけ具合(ラプラシアン分散)
        region = gray[max(0, y):y + h, max(0, x):x + w]
        sharp = cv2.Laplacian(region, cv2.CV_64F).var() if region.size else 0
        sharpness = min(1.0, sharp / 200.0)

        # 定型パターン(容器番号・V・W・年月・TP・ガス)が何個読めたか
        text = " ".join(r[1] for r in keep)
        hits = pattern_hits(text)

        # 円弧状の刻印では「傾き」より「どれだけ定型が読めたか」が正面の証拠
        score = total_conf * (0.5 + 0.5 * sharpness) * (1 + 0.6 * hits)
        t = idx / fps
        print(f"  {t:6.1f}秒: 「{text}」 検出{len(keep)}件 定型{hits}個 鮮明度{sharpness:.2f} スコア{score:.1f}")

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


# ---------------- Driveへの保存 ----------------
def extract_stamp_info(text):
    """読み取った文字列から容器番号とW値(質量)を取り出す"""
    t = text.upper().replace(",", ".")
    # 重量: 「W16.9」「W 16.9」の形(小数は1〜2桁)
    m = re.search(r"W\s*([0-9]{1,3}\.[0-9]{1,2})", t)
    weight = m.group(1) if m else None

    # 容器番号: 単独トークンの4〜6桁数字(例: 00001)
    serial = None
    for tok in re.split(r"\s+", t):
        if re.fullmatch(r"[0-9]{4,6}", tok):
            serial = tok
            break
    if serial is None:
        # 記号とくっついて読まれた場合(例: HXC00001)から数字部分を拾う
        m = re.search(r"[A-Z]([0-9]{4,6})(?![0-9])", t)
        serial = m.group(1) if m else None
    return serial, weight


def build_filename(svc, text):
    """容器番号+W値でファイル名を作る。読めなければ連番にフォールバック"""
    serial, weight = extract_stamp_info(text)
    if serial:
        base = serial + (f"_W{weight}" if weight else "")
        return unique_name(svc, base)
    return f"{FILE_PREFIX}{next_number(svc):04d}.jpg"


def unique_name(svc, base):
    """同名ファイルが既にあれば (2), (3)... を付けて重複を避ける"""
    folder = id_of(os.environ["OUTPUT_FOLDER"])
    res = with_retry(lambda: svc.files().list(
        q=f"'{folder}' in parents and name contains '{base}' and trashed = false",
        fields="files(name)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute(), "同名ファイルの確認")
    existing = {f["name"] for f in res.get("files", [])}
    if f"{base}.jpg" not in existing:
        return f"{base}.jpg"
    n = 2
    while f"{base}({n}).jpg" in existing:
        n += 1
    return f"{base}({n}).jpg"


def next_number(svc):
    folder = id_of(os.environ["OUTPUT_FOLDER"])
    res = with_retry(lambda: svc.files().list(
        q=f"'{folder}' in parents and name contains '{FILE_PREFIX}' and trashed = false",
        fields="files(name)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute(), "連番の確認")
    max_n = 0
    for f in res.get("files", []):
        m = re.match(rf"{re.escape(FILE_PREFIX)}(\d+)", f["name"])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def upload_image(svc, image, name, cv2, tmp):
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError("JPEGエンコードに失敗")
    path = os.path.join(tmp, name)
    with open(path, "wb") as fp:
        fp.write(buf.tobytes())
    media = MediaFileUpload(path, mimetype="image/jpeg")
    with_retry(lambda: svc.files().create(
        body={"name": name, "parents": [id_of(os.environ["OUTPUT_FOLDER"])]},
        media_body=media,
        supportsAllDrives=True,
    ).execute(), "画像のアップロード")


def now_jst():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def append_result_csv(svc, tmp, row):
    """保存先フォルダの処理結果一覧.csvに1行追記(なければ作成)"""
    folder = id_of(os.environ["OUTPUT_FOLDER"])
    line = ",".join('"' + str(c).replace('"', '""').replace("\n", " ") + '"' for c in row)

    res = with_retry(lambda: svc.files().list(
        q=f"'{folder}' in parents and name = '{CSV_NAME}' and trashed = false",
        fields="files(id)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute(), "CSVの確認")
    files = res.get("files", [])

    if files:
        fid = files[0]["id"]
        buf = io.BytesIO()
        req = svc.files().get_media(fileId=fid, supportsAllDrives=True)
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        content = buf.getvalue().decode("utf-8-sig", errors="replace").rstrip("\n")
        content += "\n" + line + "\n"
    else:
        header = '"処理日時","動画ファイル名","保存画像名","動画内の秒数","読み取れた文字"'
        content = header + "\n" + line + "\n"

    path = os.path.join(tmp, "results.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        fp.write(content)
    media = MediaFileUpload(path, mimetype="text/csv")

    if files:
        with_retry(lambda: svc.files().update(
            fileId=files[0]["id"], media_body=media, supportsAllDrives=True,
        ).execute(), "CSVの更新")
    else:
        with_retry(lambda: svc.files().create(
            body={"name": CSV_NAME, "parents": [folder]},
            media_body=media, supportsAllDrives=True,
        ).execute(), "CSVの作成")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        check()
    else:
        process()
