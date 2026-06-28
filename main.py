import os
import re
import json
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
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

# ----- ตั้งค่า -----
# ค่าทั้งหมดอ่านจาก environment variable เท่านั้น (ห้ามฝังค่าจริงในโค้ดที่จะขึ้น git)
api_id = int(os.environ.get("TG_API_ID", "0"))
api_hash = os.environ.get("TG_API_HASH", "")
session_string = os.environ.get("TG_SESSION", "")
group_id = int(os.environ.get("TG_GROUP_ID", "0"))
checkin_group_id = int(os.environ.get("TG_GROUP_ID_CHECKIN", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ACTIVITIES = ["กินข้าว", "ปวดหนัก", "ปวดน้อย"]

# ชื่อที่ลงท้ายด้วยคำพวกนี้ ไม่ใช่พนักงานของเรา ให้กรองออก
EXCLUDED_SUFFIXES = [
    "Vv72", "PG688", "Jun88", "MK8", "JL69",
    "F168", "K188", "NM9", "BT678", "TH26",
]

ROUND_LABELS = [
    "กะเช้า(08.00-20.00 น.) รอบที่ 1",
    "กะเช้า(08.00-20.00 น.) รอบที่ 2",
    "กะดึก(20.00-08.00 น.) รอบที่ 1",
    "กะดึก(20.00-08.00 น.) รอบที่ 2",
]
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

# เงื่อนไข SQL กรองชื่อที่ลงท้ายด้วย suffix ที่ไม่ใช่พนักงานของเรา (ใช้ซ้ำหลายจุด)
EXCLUDE_SQL = " AND " + " AND ".join("username NOT LIKE %s" for _ in EXCLUDED_SUFFIXES)
EXCLUDE_PARAMS = tuple(f"%{suf}" for suf in EXCLUDED_SUFFIXES)

SHIFT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shift_assignments.json")


def load_shift_map():
    """โหลดไฟล์รายชื่อกะ (username -> 'เช้า'/'ดึก') ใหม่ทุกครั้งที่เรียก
    เพื่อให้แก้ไฟล์แล้วเห็นผลทันทีโดยไม่ต้อง restart service"""
    try:
        with open(SHIFT_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS round_status (
            round_label TEXT PRIMARY KEY,
            announced_at TIMESTAMPTZ
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS checkin_log (
            user_id TEXT,
            round_label TEXT,
            checked_at TIMESTAMPTZ,
            PRIMARY KEY (user_id, round_label)
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


def get_current_shift_rounds(now, override=None):
    """คืนชื่อ 2 รอบของกะ — ถ้าระบุ override ('เช้า'/'ดึก') จะใช้กะนั้นแทนเวลาจริง"""
    if override == "เช้า":
        return ROUND_LABELS[0], ROUND_LABELS[1]
    if override == "ดึก":
        return ROUND_LABELS[2], ROUND_LABELS[3]

    now_bkk = now.astimezone(BANGKOK_TZ)
    is_day_shift = (now_bkk.hour, now_bkk.minute) >= (8, 5) and (now_bkk.hour, now_bkk.minute) < (20, 5)
    if is_day_shift:
        return ROUND_LABELS[0], ROUND_LABELS[1]
    else:
        return ROUND_LABELS[2], ROUND_LABELS[3]


def save_round_announcement(round_label, now):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO round_status (round_label, announced_at)
        VALUES (%s, %s)
        ON CONFLICT (round_label) DO UPDATE SET announced_at = EXCLUDED.announced_at
        """,
        (round_label, now),
    )
    conn.commit()
    cur.close()
    conn.close()


def save_checkin(user_id, now):
    """บันทึกว่า user_id เช็คชื่อสำหรับ 'รอบล่าสุดที่ถูกประกาศ' (รอบไหนประกาศหลังสุด)"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT round_label FROM round_status ORDER BY announced_at DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return  # ยังไม่มีรอบไหนถูกประกาศเลย ไม่ต้องบันทึก

    current_round = row["round_label"]
    cur.execute(
        """
        INSERT INTO checkin_log (user_id, round_label, checked_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, round_label) DO UPDATE SET checked_at = EXCLUDED.checked_at
        """,
        (user_id, current_round, now),
    )
    conn.commit()
    cur.close()
    conn.close()


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

    if any(username.endswith(suf) for suf in EXCLUDED_SUFFIXES):
        return None  # ลงท้ายด้วย suffix ที่ไม่ใช่พนักงานของเรา ข้ามไปเลย

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


CHAT_IDS = [group_id] + ([checkin_group_id] if checkin_group_id else [])


@client.on(events.NewMessage(chats=CHAT_IDS))
async def handler(event):
    text = event.message.text or ""

    if event.chat_id == group_id:
        if not text:
            return
        data = parse_message(text)
        if data:
            save_status(data)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {data['username']} -> {data['activity']}")

    elif checkin_group_id and event.chat_id == checkin_group_id:
        now = datetime.now(timezone.utc)

        round_label = None
        for label in ROUND_LABELS:
            if label in text:
                round_label = label
                break

        if round_label:
            save_round_announcement(round_label, now)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ประกาศรอบใหม่: {round_label}")
        else:
            # ไม่ใช่ข้อความประกาศ -> นับเป็นเช็คชื่อ ไม่ว่าจะมีข้อความ/แคปชั่นหรือไม่ก็ตาม
            # (ส่งแค่รูปอย่างเดียวไม่มีแคปชั่นก็ต้องนับด้วย)
            sender_id = str(event.sender_id)
            save_checkin(sender_id, now)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] เช็คชื่อจาก user_id {sender_id}")


# ===== ส่วน Flask API =====

@app.route("/")
def serve_dashboard():
    return send_from_directory(".", "dashboard.html")


@app.route("/api/status")
def api_status():
    shift_override = request.args.get("shift")
    if shift_override not in ("เช้า", "ดึก"):
        shift_override = None

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT user_id, username, status, since FROM current_status WHERE username LIKE %s" + EXCLUDE_SQL,
        ("%ODOL%",) + EXCLUDE_PARAMS,
    )
    rows = cur.fetchall()

    now = datetime.now(timezone.utc)
    period_start = get_period_start(now)

    # เวลารวมที่กดกิจกรรมทั้งหมดในรอบกะนี้ ต่อคน
    cur.execute(
        """
        SELECT user_id, activity, timestamp FROM status_log
        WHERE timestamp >= %s AND username LIKE %s
        """ + EXCLUDE_SQL + """
        ORDER BY user_id, timestamp ASC
        """,
        (period_start, "%ODOL%") + EXCLUDE_PARAMS,
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
        """ + EXCLUDE_SQL + """
        GROUP BY activity
        """,
        (period_start, "%ODOL%") + EXCLUDE_PARAMS,
    )
    activity_counts = {r["activity"]: r["count"] for r in cur.fetchall()}
    out_count = sum(1 for p in people if p["status"] != "กลับที่นั่ง")

    # ===== ข้อมูลเช็คชื่อ (รอบที่ 1 / รอบที่ 2 ของกะปัจจุบัน) =====
    round1_label, round2_label = get_current_shift_rounds(now, override=shift_override)
    current_period_start = get_period_start(now)

    cur.execute(
        "SELECT round_label, announced_at FROM round_status WHERE round_label IN (%s, %s)",
        (round1_label, round2_label),
    )
    # ตัดประกาศเก่าที่มาจากก่อนรอบกะปัจจุบันออก (เช่น ประกาศของเมื่อวาน) ถือว่ายังไม่ได้ประกาศในรอบนี้
    announced_map = {
        r["round_label"]: r["announced_at"]
        for r in cur.fetchall()
        if r["announced_at"] and r["announced_at"] >= current_period_start
    }

    user_ids = [p["user_id"] for p in people]
    checkin_map = {}
    if user_ids:
        cur.execute(
            """
            SELECT user_id, round_label, checked_at FROM checkin_log
            WHERE round_label IN (%s, %s) AND user_id = ANY(%s)
            """,
            (round1_label, round2_label, user_ids),
        )
        for r in cur.fetchall():
            checkin_map[(r["user_id"], r["round_label"])] = r["checked_at"]

    def round_status_for(user_id, round_label, is_round1=False):
        announced_at = announced_map.get(round_label)
        checked_at = checkin_map.get((user_id, round_label))

        if not announced_at:
            return "gray", None
        if checked_at and checked_at >= announced_at:
            return "green", checked_at.astimezone(BANGKOK_TZ).strftime("%H:%M")
        if is_round1 and (now - announced_at) > timedelta(hours=1):
            return "holiday", None
        return "red", None

    now_bkk = now.astimezone(BANGKOK_TZ)
    if shift_override in ("เช้า", "ดึก"):
        current_shift = shift_override
    else:
        current_shift = "เช้า" if (8, 5) <= (now_bkk.hour, now_bkk.minute) < (20, 5) else "ดึก"
    shift_map = load_shift_map()

    for p in people:
        person_shift = shift_map.get(p["username"])

        if person_shift and person_shift != current_shift:
            # คนนี้ไม่ได้อยู่กะนี้ ไม่ต้องไปเช็คว่าเช็คชื่อรอบของกะนี้หรือไม่ (ไม่ใช่ภาระของเขา)
            p["checkin"] = {
                "round1": {"label": "ไม่ใช่กะนี้", "status": "offshift", "time": None},
                "round2": {"label": "ไม่ใช่กะนี้", "status": "offshift", "time": None},
            }
            continue

        r1_status, r1_time = round_status_for(p["user_id"], round1_label, is_round1=True)
        r2_status, r2_time = round_status_for(p["user_id"], round2_label)
        p["checkin"] = {
            "round1": {"label": "เช็คชื่อรอบที่ 1", "status": r1_status, "time": r1_time},
            "round2": {"label": "เช็คชื่อรอบที่ 2", "status": r2_status, "time": r2_time},
        }

    cur.close()
    conn.close()
    return jsonify({
        "people": people,
        "summary": {"out_now": out_count, "activity_counts_today": activity_counts},
        "current_shift": current_shift,
        "is_override": shift_override is not None,
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
