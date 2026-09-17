import os
import time
import requests
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request
from google import genai
from google.genai import types

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

print(f"ADMIN_ID = '{ADMIN_ID}' (len={len(ADMIN_ID)})")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DB_NAME = "bot.db"
MAX_PHOTO_SIZE = 5 * 1024 * 1024

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1")
)

flask_app = Flask(__name__)


def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            mode TEXT DEFAULT 'default',
            last_seen TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS blocked (
            user_id INTEGER PRIMARY KEY
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit (
            user_id INTEGER,
            timestamp TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS reply_state (
            admin_id INTEGER PRIMARY KEY,
            target_id INTEGER
        )
    """)
    conn.commit()
    conn.close()


def save_user(user_id, username, full_name):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
        INSERT INTO users (user_id, username, full_name, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            last_seen = excluded.last_seen
    """, (user_id, username, full_name, now))
    conn.commit()
    conn.close()


def is_blocked(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM blocked WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None


def block_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blocked (user_id) VALUES (?)", (user_id,))
    c.execute("DELETE FROM reply_state WHERE target_id = ?", (user_id,))
    conn.commit()
    conn.close()


def unblock_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM blocked WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id, username, full_name FROM users ORDER BY last_seen DESC")
    rows = c.fetchall()
    conn.close()
    return rows


def get_blocked_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM blocked")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def find_user_by_username(username):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE username = ?", (username,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None


def get_user_mode(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT mode FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else "default"


def set_user_mode(user_id, mode):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET mode = ? WHERE user_id = ?", (mode, user_id))
    conn.commit()
    conn.close()


def set_reply_target(admin_id, target_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        INSERT INTO reply_state (admin_id, target_id) VALUES (?, ?)
        ON CONFLICT(admin_id) DO UPDATE SET target_id = excluded.target_id
    """, (admin_id, target_id))
    conn.commit()
    conn.close()


def get_reply_target(admin_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT target_id FROM reply_state WHERE admin_id = ?", (admin_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None


def clear_reply_target(admin_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM reply_state WHERE admin_id = ?", (admin_id,))
    conn.commit()
    conn.close()


def check_rate_limit(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    now = datetime.now()
    one_minute_ago = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")

    c.execute("SELECT COUNT(*) FROM rate_limit WHERE user_id = ? AND timestamp > ?", (user_id, one_minute_ago))
    count = c.fetchone()[0]

    if count >= 4:
        conn.close()
        return False

    c.execute("INSERT INTO rate_limit (user_id, timestamp) VALUES (?, ?)",
              (user_id, now.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return True


user_chats = {}


def get_user_chat(user_id):
    if user_id not in user_chats:
        user_chats[user_id] = client.chats.create(model="gemini-3.5-flash")
    return user_chats[user_id]


def tg_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    if len(text) > 4000:
        text = text[:4000] + "\n\n...(پیام کوتاه شد)"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[tg_send_message] خطا: {e}")
        return None


def tg_send_typing(chat_id):
    try:
        requests.post(f"{TELEGRAM_API}/sendChatAction",
                      json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass


def tg_copy_message(to_chat_id, from_chat_id, message_id, reply_markup=None):
    payload = {
        "chat_id": to_chat_id,
        "from_chat_id": from_chat_id,
        "message_id": message_id,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{TELEGRAM_API}/copyMessage", json=payload, timeout=30)
        return r.json()
    except Exception as e:
        print(f"[tg_copy_message] خطا: {e}")
        return None


def tg_get_file(file_id):
    try:
        r = requests.get(f"{TELEGRAM_API}/getFile",
                         params={"file_id": file_id}, timeout=15)
        data = r.json()
        if not data.get("ok"):
            return None, None, 0
        file_path = data["result"]["file_path"]
        file_size = data["result"].get("file_size", 0)

        if file_size > MAX_PHOTO_SIZE:
            print(f"عکس بزرگ‌تر از حد مجاز: {file_size} bytes")
            return None, file_path, file_size

        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        file_bytes = requests.get(file_url, timeout=30).content
        return file_bytes, file_path, file_size
    except Exception as e:
        print(f"[tg_get_file] خطا: {e}")
        return None, None, 0


MODES = {
    "default": "تو یه دستیار هوشمند، مفید و خوش‌برخورد هستی. همیشه به زبان فارسی و واضح جواب بده.",
    "coder": "تو یه برنامه‌نویس حرفه‌ای هستی. به سوالات برنامه‌نویسی با کد و توضیح کامل جواب بده.",
    "poet": "تو یه شاعر فارسی‌زبان هستی. جواب‌هات رو به صورت شعر و ادبی بده.",
    "translator": "تو یه مترجم حرفه‌ای هستی. متن‌ها رو به فارسی یا انگلیسی ترجمه کن.",
    "teacher": "تو یه معلم صبور و دقیق هستی. مفاهیم رو ساده و با مثال توضیح بده.",
}

MODE_NAMES = {
    "default": "پیش‌فرض",
    "coder": "برنامه‌نویس",
    "poet": "شاعر",
    "translator": "مترجم",
    "teacher": "معلم",
}


def ask_gemini(user_id, user_text):
    max_retries = 2
    for attempt in range(max_retries):
        try:
            mode = get_user_mode(user_id)
            system_prompt = MODES.get(mode, MODES["default"])

            chat = get_user_chat(user_id)

            response = chat.send_message(
                f"{system_prompt}\n\nسوال کاربر: {user_text}"
            )

            if response and response.text:
                return response.text.strip()
            return "متأسفانه نتونستم جواب بدم. دوباره امتحان کن."

        except Exception as e:
            err_str = str(e)
            print(f"[ask_gemini] تلاش {attempt + 1} خطا: {err_str}")

            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return "محدودیت درخواست! چند لحظه صبر کن و دوباره امتحان کن."

            if attempt < max_retries - 1:
                user_chats.pop(user_id, None)
                time.sleep(1)
                continue

            return "متأسفانه یه خطا پیش اومد. دوباره امتحان کن."


def ask_gemini_with_image(user_id, image_bytes, mime_type, caption=""):
    try:
        mode = get_user_mode(user_id)
        system_prompt = MODES.get(mode, MODES["default"])

        if caption and caption.strip():
            prompt = (
                f"{system_prompt}\n\n"
                f"کاربر این عکس رو فرستاده و این متن رو هم نوشته:\n"
                f"«{caption}»\n\n"
                f"لطفاً هم عکس رو تحلیل کن، هم به این متن پاسخ بده."
            )
        else:
            prompt = (
                f"{system_prompt}\n\n"
                f"کاربر این عکس رو فرستاده (بدون متن).\n"
                f"لطفاً عکس رو کامل تحلیل کن:\n"
                f"- چی تو عکس می‌بینی؟\n"
                f"- اگه متن داره، بخونش\n"
                f"- جزئیات مهم رو توضیح بده"
            )

        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type=mime_type
        )

        chat = get_user_chat(user_id)
        response = chat.send_message([image_part, prompt])

        if response and response.text:
            return response.text.strip()
        return "متأسفانه نتونستم عکس رو تحلیل کنم. دوباره امتحان کن."

    except Exception as e:
        print(f"[ask_gemini_with_image] خطا: {e}")
        return "متأسفانه یه خطا تو تحلیل عکس پیش اومد. دوباره امتحان کن."


def notify_admin_text(user_id, username, full_name, message_text):
    if not ADMIN_ID:
        return

    text = (
        f"کاربر در حال استفاده از ربات\n\n"
        f"نام: {full_name}\n"
        f"یوزرنیم: @{username or '-'}\n"
        f"آیدی: {user_id}\n\n"
        f"پیام: {message_text[:100]}{'...' if len(message_text) > 100 else ''}"
    )

    reply_markup = {
        "inline_keyboard": [
            [{"text": "پاسخ دادن", "callback_data": f"reply:{user_id}"}],
            [{"text": "بلاک", "callback_data": f"block:{user_id}"}]
        ]
    }

    tg_send_message(ADMIN_ID, text, reply_markup=reply_markup)


def notify_admin_photo(user_id, username, full_name, caption, from_chat_id, message_id):
    if not ADMIN_ID:
        return

    reply_markup = {
        "inline_keyboard": [
            [{"text": "پاسخ دادن", "callback_data": f"reply:{user_id}"}],
            [{"text": "بلاک", "callback_data": f"block:{user_id}"}]
        ]
    }

    header = (
        f"کاربر در حال استفاده از ربات (عکس)\n\n"
        f"نام: {full_name}\n"
        f"یوزرنیم: @{username or '-'}\n"
        f"آیدی: {user_id}"
    )
    if caption and caption.strip():
        header += f"\n\nکپشن: {caption[:150]}"

    tg_send_message(ADMIN_ID, header)
    tg_copy_message(ADMIN_ID, from_chat_id, message_id, reply_markup=reply_markup)


def handle_callback(cb):
    user_id = cb["from"]["id"]
    data = cb.get("data", "")
    cb_id = cb["id"]
    message_id = cb["message"]["message_id"]

    try:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                      json={"callback_query_id": cb_id}, timeout=5)
    except Exception:
        pass

    if data.startswith("mode:"):
        mode = data.split(":")[1]
        set_user_mode(user_id, mode)
        try:
            requests.post(
                f"{TELEGRAM_API}/editMessageText",
                json={
                    "chat_id": user_id,
                    "message_id": message_id,
                    "text": f"حالت روی «{MODE_NAMES.get(mode, mode)}» تنظیم شد."
                },
                timeout=10
            )
        except Exception:
            pass

    elif data.startswith("reply:"):
        target_user_id = int(data.split(":")[1])
        set_reply_target(user_id, target_user_id)
        tg_send_message(ADMIN_ID, f"پیام خود را برای کاربر {target_user_id} بنویسید.")

    elif data.startswith("block:"):
        target_user_id = int(data.split(":")[1])
        block_user(target_user_id)
        tg_send_message(ADMIN_ID, f"کاربر {target_user_id} بلاک شد.")


def handle_admin_message(message):
    text = message.get("text", "")
    admin_id = message["from"]["id"]

    if text == "/start":
        tg_send_message(
            admin_id,
            "سلام ادمین!\n\n"
            "دستورات:\n"
            "- /users لیست کاربران\n"
            "- /blocked لیست بلاک‌شده‌ها\n"
            "- /unblock <id یا یوزرنیم> آنبلاک کردن\n\n"
            "برای پاسخ به کاربر، روی دکمه‌ی «پاسخ دادن» زیر پیام کاربر بزنید."
        )
        return

    if text == "/users":
        users = get_all_users()
        if not users:
            tg_send_message(admin_id, "هیچ کاربری ثبت نشده.")
            return
        response = "لیست کاربران:\n\n"
        for uid, uname, fname in users[:50]:
            response += f"- {fname} | @{uname or '-'} | {uid}\n"
        tg_send_message(admin_id, response)
        return

    if text == "/blocked":
        blocked = get_blocked_users()
        if not blocked:
            tg_send_message(admin_id, "هیچ کاربری بلاک نشده.")
            return
        response = "کاربران بلاک‌شده:\n\n"
        for uid in blocked:
            response += f"- {uid}\n"
        tg_send_message(admin_id, response)
        return

    if text.startswith("/unblock"):
        parts = text.split()
        if len(parts) < 2:
            tg_send_message(admin_id, "استفاده: /unblock <id یا یوزرنیم>")
            return

        target = parts[1].strip().replace("@", "")
        target_id = None
        try:
            target_id = int(target)
        except ValueError:
            target_id = find_user_by_username(target)

        if not target_id:
            tg_send_message(admin_id, f"کاربر «{target}» پیدا نشد.")
            return

        unblock_user(target_id)
        tg_send_message(admin_id, f"کاربر {target_id} آنبلاک شد.")
        return

    target_user_id = get_reply_target(admin_id)
    if target_user_id:
        clear_reply_target(admin_id)
        tg_send_message(target_user_id, f"پیام سازنده:\n\n{text}")
        tg_send_message(admin_id, "پیام ارسال شد.")
        return

    tg_send_message(admin_id, "برای پاسخ، روی دکمه‌ی «پاسخ دادن» زیر پیام کاربر بزنید.")


@flask_app.route("/", methods=["GET"])
def index():
    return "AI Bot is running", 200


@flask_app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            return "OK", 200

        if "callback_query" in data:
            handle_callback(data["callback_query"])
            return "OK", 200

        message = data.get("message")
        if not message:
            return "OK", 200

        user_id = message["from"]["id"]

        if str(user_id) == str(ADMIN_ID):
            handle_admin_message(message)
            return "OK", 200

        chat_id = message["chat"]["id"]
        user = message.get("from", {})
        username = user.get("username", "")
        full_name = user.get("full_name", "")
        text = message.get("text", "")
        caption = message.get("caption", "")
        photo = message.get("photo")

        save_user(user_id, username, full_name)

        if is_blocked(user_id):
            tg_send_message(chat_id, "شما توسط مدیریت مسدود شده‌اید.")
            return "OK", 200

        if photo:
            if not check_rate_limit(user_id):
                tg_send_message(chat_id, "محدودیت! هر دقیقه فقط ۴ پیام می‌تونی بفرستی.")
                return "OK", 200

            largest_photo = photo[-1]
            file_id = largest_photo["file_id"]

            notify_admin_photo(user_id, username, full_name, caption, chat_id, message["message_id"])

            image_bytes, file_path, file_size = tg_get_file(file_id)

            if file_size > MAX_PHOTO_SIZE:
                tg_send_message(chat_id, "عکس خیلی بزرگه! لطفاً عکس کوچیک‌تری بفرست (زیر ۵ مگابایت).")
                return "OK", 200

            if not image_bytes:
                tg_send_message(chat_id, "متأسفانه نتونستم عکس رو دریافت کنم. دوباره امتحان کن.")
                return "OK", 200

            mime_type = "image/jpeg"
            if file_path and file_path.lower().endswith(".png"):
                mime_type = "image/png"
            elif file_path and file_path.lower().endswith(".webp"):
                mime_type = "image/webp"

            tg_send_typing(chat_id)
            answer = ask_gemini_with_image(user_id, image_bytes, mime_type, caption)
            tg_send_message(chat_id, f"پیام ربات:\n\n{answer}")
            return "OK", 200

        if text == "/start":
            tg_send_message(
                chat_id,
                "سلام\n\n"
                "من یه دستیار هوش مصنوعی هستم.\n\n"
                "دستورات:\n"
                "- /mode تغییر حالت\n"
                "- /clear پاک کردن حافظه\n\n"
                "می‌تونی متن بفرستی یا عکس بفرستی تا تحلیلش کنم!"
            )
            return "OK", 200

        if text == "/clear":
            user_chats.pop(user_id, None)
            tg_send_message(chat_id, "حافظه‌ی مکالمه پاک شد.")
            return "OK", 200

        if text == "/mode":
            keyboard = []
            for key, name in MODE_NAMES.items():
                keyboard.append([{"text": name, "callback_data": f"mode:{key}"}])
            reply_markup = {"inline_keyboard": keyboard}
            tg_send_message(chat_id, "یه حالت انتخاب کن:", reply_markup=reply_markup)
            return "OK", 200

        if not text:
            tg_send_message(chat_id, "لطفاً یه پیام متنی یا عکس بفرست.")
            return "OK", 200

        if not check_rate_limit(user_id):
            tg_send_message(chat_id, "محدودیت! هر دقیقه فقط ۴ پیام می‌تونی بفرستی.")
            return "OK", 200

        notify_admin_text(user_id, username, full_name, text)

        tg_send_typing(chat_id)
        answer = ask_gemini(user_id, text)
        tg_send_message(chat_id, f"پیام ربات:\n\n{answer}")

        return "OK", 200

    except Exception as e:
        print(f"[webhook] خطا: {e}")
        import traceback
        traceback.print_exc()
        return "OK", 200


def setup_webhook():
    if not RENDER_URL:
        return
    webhook_url = f"{RENDER_URL}/webhook"
    print(f"Webhook: {webhook_url}")
    try:
        requests.get(f"{TELEGRAM_API}/deleteWebhook", timeout=15)
        r = requests.get(
            f"{TELEGRAM_API}/setWebhook",
            params={"url": webhook_url, "drop_pending_updates": True},
            timeout=15
        )
        print(f"Webhook ست شد: {r.json()}")
    except Exception as e:
        print(f"خطا: {e}")


if __name__ == "__main__":
    init_db()
    setup_webhook()
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)