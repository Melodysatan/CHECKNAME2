import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.environ.get("TG_API_ID") or input("ใส่ api_id: "))
api_hash = os.environ.get("TG_API_HASH") or input("ใส่ api_hash: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n===== คัดลอกค่าด้านล่างนี้ไปเก็บไว้ (เป็นความลับ ห้ามแชร์) =====\n")
    print(client.session.save())
    print("\n=================================================\n")