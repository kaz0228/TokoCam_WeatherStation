"""
M.C.S IoT Cloud -> data.json / yesterday.json 収集スクリプト（GitHub Pages 公開用・独立版）
------------------------------------------------------------------------------------
毎正時にM.C.Sから観測データを取得し、
  - data.json      … 最新値 ＋ 直近24時間（1時間ごと）
  - yesterday.json … 前日 00:00〜23:00（0時台の実行時のみ更新）
を書き出し、git add / commit / push で GitHub Pages に反映します。

このスクリプトは単体で動きます（Xの投稿スクリプトには依存しません）。
公開リポジトリに X の認証キーを持ち込まないための構成です。

必要な環境変数:
  MCS_USER, MCS_PASSWORD  … M.C.Sのログイン情報（必須）
  MCS_DEVICE_ID           … 端末ID（既定 MI2525R49）
  MCS_LOGIN_URL           … ログインURL（既定あり）
  SITE_DIR                … index.html を置いた git リポジトリのパス（既定 .）
  GIT_PUSH                … "true" で git push（既定 true）
  HEADLESS                … "false" でブラウザ表示（既定 true）

依存: playwright（pip install playwright && playwright install chromium）
"""

import asyncio
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

# =========================
# 設定
# =========================
MCS_LOGIN_URL = os.getenv("MCS_LOGIN_URL", "https://service.mcs-fs.com/login/ics")
MCS_USER = os.getenv("MCS_USER")
MCS_PASSWORD = os.getenv("MCS_PASSWORD")
MCS_DEVICE_ID = os.getenv("MCS_DEVICE_ID", "MI2525R49")
HEADLESS = os.getenv("HEADLESS", "true").lower() not in ("0", "false", "no")

SITE_DIR = Path(os.getenv("SITE_DIR", ".")).resolve()
DATA_JSON = SITE_DIR / "data.json"
YDAY_JSON = SITE_DIR / "yesterday.json"
HISTORY_CSV = SITE_DIR / "history.csv"
GIT_PUSH = os.getenv("GIT_PUSH", "true").lower() in ("1", "true", "yes")

SELECTOR_USER = os.getenv(
    "MCS_USER_SELECTOR",
    'input[type="text"], input[type="email"], input[name*="user" i], input[name*="id" i]',
)
SELECTOR_PASSWORD = os.getenv("MCS_PASSWORD_SELECTOR", 'input[type="password"]')
SELECTOR_SUBMIT = os.getenv(
    "MCS_SUBMIT_SELECTOR", 'button[type="submit"], input[type="submit"]'
)

# =========================
# 16方位
# =========================
DIRECTIONS_16 = ["北","北北東","北東","東北東","東","東南東","南東","南南東",
                 "南","南南西","南西","西南西","西","西北西","北西","北北西"]

def wind_direction(degrees: float) -> str:
    degrees %= 360.0
    return DIRECTIONS_16[int((degrees + 11.25) // 22.5) % 16]

# =========================
# M.C.Sレスポンス探索
# =========================
def find_device_records(obj: Any, device_id: str) -> Optional[list]:
    if isinstance(obj, dict):
        if "iotdata" in obj and isinstance(obj["iotdata"], dict):
            rec = obj["iotdata"].get(device_id)
            if isinstance(rec, list):
                return rec
        for v in obj.values():
            r = find_device_records(v, device_id)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_device_records(v, device_id)
            if r is not None:
                return r
    return None

def parse_record(record: dict) -> Optional[dict]:
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    ts = data.get("timestamp") or record.get("datatime")
    ch = data.get("ch_data")
    if ts is None or not isinstance(ch, dict):
        return None
    return {"timestamp": ts, "ch_data": ch, "raw": record}

def parse_mcs_datetime(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")

async def get_mcs_records() -> list[dict]:
    if not MCS_USER or not MCS_PASSWORD:
        raise RuntimeError("MCS_USER / MCS_PASSWORD が設定されていません。")

    captured = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page = await context.new_page()

        async def handle_response(response):
            try:
                if "service.mcs-fs.com" not in response.url:
                    return
                if "json" not in response.headers.get("content-type", "").lower():
                    return
                text = await response.text()
                if "iotdata" not in text:
                    return
                records = find_device_records(json.loads(text), MCS_DEVICE_ID)
                if records:
                    captured.append({"url": response.url, "records": records})
            except Exception:
                pass

        page.on("response", handle_response)
        await page.goto(MCS_LOGIN_URL, wait_until="domcontentloaded")

        try:
            user = page.locator(SELECTOR_USER).first
            password = page.locator(SELECTOR_PASSWORD).first
            await user.wait_for(timeout=15000)
            await user.fill(MCS_USER)
            await password.fill(MCS_PASSWORD)
            await page.locator(SELECTOR_SUBMIT).first.click()
        except PlaywrightTimeoutError:
            await browser.close()
            raise RuntimeError(
                "M.C.Sのログインフォームを自動認識できませんでした。"
                "MCS_USER_SELECTOR / MCS_PASSWORD_SELECTOR / MCS_SUBMIT_SELECTOR を指定してください。"
            )

        await page.wait_for_timeout(5000)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not captured:
            await page.wait_for_timeout(500)
        await browser.close()

    if not captured:
        raise RuntimeError("iotdataを含むM.C.SのJSONレスポンスを取得できませんでした。")
    return captured[-1]["records"]

def select_latest_hourly_record(records: list[dict]) -> dict:
    hourly = []
    for record in records:
        item = parse_record(record)
        if item is None:
            continue
        try:
            dt = parse_mcs_datetime(item["timestamp"])
        except Exception:
            continue
        if dt.minute == 0:
            hourly.append((dt, item))
    if not hourly:
        raise RuntimeError("正時（XX:00）のデータが見つかりませんでした。")
    hourly.sort(key=lambda x: x[0])
    return hourly[-1][1]

# =========================
# レコード変換（チャンネル対応）
# =========================
# 端末 FT2-16CH / MI2525R49（画面の列順より）:
#   ch0 平均風速 / ch1 平均風向 / ch2 最大風速 / ch3 最大風向
#   ch5 日射量 / ch6 湿度 / ch7 気温 / ch9 黒球温度
#   ch24 下向短波 / ch25 上向短波 / ch26 下向長波 / ch27 上向長波 / ch29 地中伝導熱
# 風は「最大風速(ch2)・最大風向(ch3)」を使用。
def record_to_row(item: dict) -> dict:
    ch = item["ch_data"]
    ts = parse_mcs_datetime(item["timestamp"])
    wind_speed = float(ch["2"])
    wind_deg = float(ch["3"])
    return {
        "timestamp": ts.strftime("%Y-%m-%d %H:%M"),
        "date": ts.strftime("%Y-%m-%d"),
        "time": ts.strftime("%H:%M"),
        "temperature": float(ch["7"]),
        "globe_temp": float(ch["9"]),
        "humidity": float(ch["6"]),
        "wind": wind_speed,
        "wind_deg": wind_deg,
        "wind_dir": wind_direction(wind_deg),
        "solar": float(ch["5"]),
        "sw_down": float(ch["24"]),
        "sw_up": float(ch["25"]),
        "lw_down": float(ch["26"]),
        "lw_up": float(ch["27"]),
        "soil_heat": float(ch["29"]),
    }

CSV_FIELDS = [
    "timestamp", "date", "time",
    "temperature", "globe_temp", "humidity",
    "wind", "wind_deg", "wind_dir",
    "solar", "sw_down", "sw_up", "lw_down", "lw_up", "soil_heat",
]
READ_FLOATS = ("temperature", "globe_temp", "humidity", "wind", "wind_deg",
               "solar", "sw_down", "sw_up", "lw_down", "lw_up", "soil_heat")

# =========================
# CSV 読み書き
# =========================
def append_history(row: dict) -> bool:
    existing = set()
    if HISTORY_CSV.exists():
        with HISTORY_CSV.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                existing.add(r.get("timestamp"))
    if row["timestamp"] in existing:
        return False
    new_file = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({k: row.get(k) for k in CSV_FIELDS})
    return True

def _row_from_csv(r: dict, label: str) -> dict:
    item = {"time": label, "timestamp": r["timestamp"]}
    for k in READ_FLOATS:
        try:
            item[k] = float(r[k])
        except (TypeError, ValueError):
            item[k] = None
    item["wind_dir"] = r.get("wind_dir")
    return item

def load_recent_history(now_ts: str, hours: int = 24) -> list[dict]:
    rows = []
    if not HISTORY_CSV.exists():
        return rows
    now = datetime.strptime(now_ts, "%Y-%m-%d %H:%M")
    cutoff = now - timedelta(hours=hours)
    with HISTORY_CSV.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M")
            except (KeyError, ValueError):
                continue
            if dt < cutoff or dt > now:
                continue
            rows.append(_row_from_csv(r, dt.strftime("%m/%d %H:%M")))
    rows.sort(key=lambda x: x["timestamp"])
    return rows

def load_day_history(date_str: str) -> list[dict]:
    rows = []
    if not HISTORY_CSV.exists():
        return rows
    with HISTORY_CSV.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("date") != date_str:
                continue
            dt = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M")
            rows.append(_row_from_csv(r, dt.strftime("%H:%M")))
    rows.sort(key=lambda x: x["timestamp"])
    return rows

# =========================
# JSON 書き出し
# =========================
def write_data_json(latest: dict, history: list[dict]):
    payload = {
        "stamp": latest["timestamp"].replace("-", "/"),
        "latest": latest,
        "history": history,
    }
    DATA_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                         encoding="utf-8")

def write_yesterday_json(now_ts: str):
    now = datetime.strptime(now_ts, "%Y-%m-%d %H:%M")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = load_day_history(yday)
    if not rows:
        print(f"前日（{yday}）のデータがまだありません")
        return
    YDAY_JSON.write_text(
        json.dumps({"date": yday.replace("-", "/"), "history": rows},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"yesterday.json 更新（{yday} / {len(rows)} 点）")

# =========================
# git push
# =========================
def git_push():
    def run(*args):
        return subprocess.run(["git", *args], cwd=SITE_DIR,
                              capture_output=True, text=True)
    targets = [f for f in ("data.json", "history.csv", "yesterday.json")
               if (SITE_DIR / f).exists()]
    run("add", *targets)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    committed = run("commit", "-m", f"update weather {stamp}")
    if committed.returncode != 0 and "nothing to commit" in (
        committed.stdout + committed.stderr
    ):
        print("変更なし（コミットなし）")
        return
    pushed = run("push")
    if pushed.returncode != 0:
        print("git push 失敗:", pushed.stderr.strip(), file=sys.stderr)
    else:
        print("git push 完了")

# =========================
# 1回実行
# =========================
async def run_once():
    records = await get_mcs_records()
    item = select_latest_hourly_record(records)
    row = record_to_row(item)
    print("取得:", row["timestamp"],
          f'気温{row["temperature"]:.1f}℃ 黒球{row["globe_temp"]:.1f}℃ '
          f'最大風速{row["wind"]:.1f}m/s')

    is_new = append_history(row)
    history = load_recent_history(row["timestamp"], hours=24)
    row["time"] = datetime.strptime(
        row["timestamp"], "%Y-%m-%d %H:%M"
    ).strftime("%m/%d %H:%M")
    write_data_json(row, history)
    print(f"data.json 更新（直近24時間 {len(history)} 点）")

    if datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M").hour == 0:
        write_yesterday_json(row["timestamp"])

    if GIT_PUSH and is_new:
        git_push()
    elif not is_new:
        print("同時刻データは追記済みのため push しません")


if __name__ == "__main__":
    asyncio.run(run_once())
