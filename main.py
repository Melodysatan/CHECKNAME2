import os
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()  # โหลดค่าจากไฟล์ .env ถ้ามี (สำหรับรันที่เครื่องตัวเอง)

import psycopg2
import psycopg2.extras

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

# ----- ตั้งค่า -----
# ค่าทั้งหมดอ่านจาก environment variable เท่านั้น (ห้ามฝังค่าจริงในโค้ดที่จะขึ้น git)
api_id = int(os.environ.get("TG_API_ID", "0"))
api_hash = os.environ.get("TG_API_HASH", "")
session_string = os.environ.get("TG_SESSION", "")
group_id = int(os.environ.get("TG_GROUP_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ACTIVITIES = ["กินข้าว", "ปวดหนัก", "ปวดน้อย"]
# -------------------

if not api_id or not api_hash or not group_id:
    raise RuntimeError(
        "กรุณาตั้งค่า environment variable: TG_API_ID, TG_API_HASH, TG_GROUP_ID ก่อนรัน "
        "(ดูวิธีตั้งค่าใน README.md)"
    )

if not DATABASE_URL:
    raise RuntimeError(
        "กรุณาตั้งค่า environment variable: DATABASE_URL (connection string ของ PostgreSQL)"
    )

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

# ถ้ามี TG_SESSION (รันบน server) ใช้ string session, ถ้าไม่มี (รันที่คอมตัวเอง) ใช้ไฟล์ปกติ
if session_string:
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
else:
    client = TelegramClient("my_session", api_id, api_hash)

app = Flask(__name__)
CORS(app)


# ===== ส่วนฐานข้อมูล (PostgreSQL) =====

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS current_status (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            status TEXT,
            since TIMESTAMPTZ
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS status_log (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            username TEXT,
            activity TEXT,
            timestamp TIMESTAMPTZ,
            raw_text TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def get_period_start(now):
    """หาจุดเริ่มรอบกะปัจจุบัน (รีเซตทุก 08:05 และ 20:05 'เวลาไทย' เสมอ
    ไม่ว่า server จะรันอยู่ timezone ไหนก็ตาม) — now ต้องเป็น UTC-aware datetime"""
    now_bkk = now.astimezone(BANGKOK_TZ)

    bkk_0805 = now_bkk.replace(hour=8, minute=5, second=0, microsecond=0)
    bkk_2005 = now_bkk.replace(hour=20, minute=5, second=0, microsecond=0)

    if now_bkk >= bkk_2005:
        period_start_bkk = bkk_2005
    elif now_bkk >= bkk_0805:
        period_start_bkk = bkk_0805
    else:
        period_start_bkk = bkk_2005 - timedelta(days=1)

    return period_start_bkk.astimezone(timezone.utc)


# ===== ส่วน parse ข้อความ Telegram =====

def clean_text(text):
    return text.replace("**", "").replace("`", "")


def parse_message(text):
    cleaned = clean_text(text)

    user_match = re.search(r"ผู้ใช้\s*[:：]\s*\[?([^\]\(\n]+)", cleaned)
    userid_match = re.search(r"รหัสผู้ใช้\s*[:：]\s*(\d+)", cleaned)

    if not user_match or not userid_match:
        return None

    username = user_match.group(1).strip()
    user_id = userid_match.group(1).strip()

    if "ODOL" not in username:
        return None  # ไม่ใช่ ODOL ข้ามไปเลย

    if "กลับที่นั่ง" in cleaned and "ลงทะเบียนสำเร็จ" not in cleaned:
        return {"user_id": user_id, "username": username, "activity": "กลับที่นั่ง", "raw": cleaned}

    for act in ACTIVITIES:
        if re.search(rf"ลงทะเบียนสำเร็จ\s*[:：]\s*{act}", cleaned):
            return {"user_id": user_id, "username": username, "activity": act, "raw": cleaned}

    return None


def save_status(data):
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now(timezone.utc)

    cur.execute(
        "INSERT INTO status_log (user_id, username, activity, timestamp, raw_text) VALUES (%s, %s, %s, %s, %s)",
        (data["user_id"], data["username"], data["activity"], now, data["raw"]),
    )
    cur.execute(
        """
        INSERT INTO current_status (user_id, username, status, since)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            username = EXCLUDED.username,
            status = EXCLUDED.status,
            since = EXCLUDED.since
        """,
        (data["user_id"], data["username"], data["activity"], now),
    )
    conn.commit()
    cur.close()
    conn.close()


@client.on(events.NewMessage(chats=group_id))
async def handler(event):
    text = event.message.text
    if not text:
        return
    data = parse_message(text)
    if data:
        save_status(data)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {data['username']} -> {data['activity']}")


# ===== ส่วน Flask API =====

@app.route("/")
def serve_dashboard():
    return send_from_directory(".", "dashboard.html")


@app.route("/api/status")
def api_status():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT user_id, username, status, since FROM current_status WHERE username LIKE %s", ("%ODOL%",))
    rows = cur.fetchall()

    now = datetime.now(timezone.utc)
    period_start = get_period_start(now)

    # เวลารวมที่กดกิจกรรมทั้งหมดในรอบกะนี้ ต่อคน
    cur.execute(
        """
        SELECT user_id, activity, timestamp FROM status_log
        WHERE timestamp >= %s AND username LIKE %s
        ORDER BY user_id, timestamp ASC
        """,
        (period_start, "%ODOL%"),
    )
    log_rows = cur.fetchall()

    user_logs = defaultdict(list)
    for r in log_rows:
        user_logs[r["user_id"]].append(r)

    total_today_map = {}
    for uid, log_list in user_logs.items():
        total = 0.0
        for i, row in enumerate(log_list):
            if row["activity"] == "กลับที่นั่ง":
                continue
            start_t = row["timestamp"]
            end_t = log_list[i + 1]["timestamp"] if i + 1 < len(log_list) else now
            total += (end_t - start_t).total_seconds()
        total_today_map[uid] = int(total)

    people = []
    for row in rows:
        since = row["since"]
        duration_seconds = int((now - since).total_seconds())
        people.append({
            "user_id": row["user_id"],
            "username": row["username"],
            "status": row["status"],
            "since": since.isoformat(),
            "duration_seconds": duration_seconds,
            "total_today_seconds": total_today_map.get(row["user_id"], 0),
        })

    cur.execute(
        """
        SELECT activity, COUNT(*) as count FROM status_log
        WHERE timestamp >= %s AND activity != 'กลับที่นั่ง' AND username LIKE %s
        GROUP BY activity
        """,
        (period_start, "%ODOL%"),
    )
    activity_counts = {r["activity"]: r["count"] for r in cur.fetchall()}
    out_count = sum(1 for p in people if p["status"] != "กลับที่นั่ง")

    cur.close()
    conn.close()
    return jsonify({
        "people": people,
        "summary": {"out_now": out_count, "activity_counts_today": activity_counts},
    })


def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ===== จุดเริ่มโปรแกรม =====

if __name__ == "__main__":
    init_db()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("API พร้อมใช้งานที่ http://localhost:5000/api/status")

    print("กำลังฟังข้อความใหม่จากกลุ่ม Telegram... (กด Ctrl+C เพื่อหยุด)")
    with client:
        client.run_until_disconnected()
