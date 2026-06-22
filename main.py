import re
import sqlite3
import threading
from datetime import datetime, date

import os
import re
import sqlite3
import threading
from datetime import datetime, date

from dotenv import load_dotenv
load_dotenv()  # โหลดค่าจากไฟล์ .env ถ้ามี (สำหรับรันที่เครื่องตัวเอง)

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
DB_PATH = "status.db"
ACTIVITIES = ["กินข้าว", "ปวดหนัก", "ปวดน้อย"]
# -------------------

if not api_id or not api_hash or not group_id:
    raise RuntimeError(
        "กรุณาตั้งค่า environment variable: TG_API_ID, TG_API_HASH, TG_GROUP_ID ก่อนรัน "
        "(ดูวิธีตั้งค่าใน README.md)"
    )

# ถ้ามี TG_SESSION (รันบน server) ใช้ string session, ถ้าไม่มี (รันที่คอมตัวเอง) ใช้ไฟล์ปกติ
if session_string:
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
else:
    client = TelegramClient("my_session", api_id, api_hash)

app = Flask(__name__)
CORS(app)


# ===== ส่วนฐานข้อมูล =====

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS current_status (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            status TEXT,
            since TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            username TEXT,
            activity TEXT,
            timestamp TEXT,
            raw_text TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now().isoformat()

    cur.execute(
        "INSERT INTO status_log (user_id, username, activity, timestamp, raw_text) VALUES (?, ?, ?, ?, ?)",
        (data["user_id"], data["username"], data["activity"], now, data["raw"]),
    )
    cur.execute(
        """
        INSERT INTO current_status (user_id, username, status, since)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            status = excluded.status,
            since = excluded.since
        """,
        (data["user_id"], data["username"], data["activity"], now),
    )
    conn.commit()
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

    cur.execute("SELECT user_id, username, status, since FROM current_status")
    rows = cur.fetchall()

    now = datetime.now()
    people = []
    for row in rows:
        since = datetime.fromisoformat(row["since"])
        duration_seconds = int((now - since).total_seconds())
        people.append({
            "user_id": row["user_id"],
            "username": row["username"],
            "status": row["status"],
            "since": row["since"],
            "duration_seconds": duration_seconds,
        })

    today_str = date.today().isoformat()
    cur.execute(
        """
        SELECT activity, COUNT(*) as count FROM status_log
        WHERE timestamp LIKE ? AND activity != 'กลับที่นั่ง'
        GROUP BY activity
        """,
        (f"{today_str}%",),
    )
    activity_counts = {r["activity"]: r["count"] for r in cur.fetchall()}
    out_count = sum(1 for p in people if p["status"] != "กลับที่นั่ง")

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

    # รัน Flask ใน thread แยก (background)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("API พร้อมใช้งานที่ http://localhost:5000/api/status")

    # รัน Telegram listener ใน thread หลัก
    print("กำลังฟังข้อความใหม่จากกลุ่ม Telegram... (กด Ctrl+C เพื่อหยุด)")
    with client:
        client.run_until_disconnected()
