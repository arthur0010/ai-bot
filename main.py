import os
import time
import threading
import requests
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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
CACHE_TTL = 300
MAX_CHATS = 100
RATE_LIMIT = 3

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1")
)

http_session = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=20,
    pool_maxsize=50,
    max_retries=Retry(total=2, backoff_factor=0.3)
)
http_session.mount("https://", _adapter)
http_session.mount("http://", _adapter)

flask_app = Flask(__name__)

_mode_cache = {}
_blocked_cache = {}
user_chats = {}
last_answers = {}
_locks = {}


def _get_lock(key):
    if key not in _locks:
        _locks[key] = threading.Lock()
    return _locks[key]


def _cache_get(cache, key):
    if key in cache:
        value, ts = cache[key]
        if time.time() - ts < CACHE_TTL:
            return value
        del cache[key]
    return None


def _cache_set(cache, key, value):
    cache[key] = (value, time.time())


def _cleanup_cache():
    if len(user_chats) > MAX_CHATS:
        keys = list(user_chats.keys())
        for k in keys[:len(keys) // 2]:
            user_chats.pop(k, None)
    if len(last_answers) > MAX_CHATS * 2:
        keys = list(last_answers.keys())
        for k in keys[:len(keys) // 2]:
            last_answers.pop(k, None)


def _db_connect():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    conn = _db_connect()
    c = conn.cursor()
    c.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "user_id INTEGER PRIMARY KEY, "
        "username TEXT, "
        "full_name TEXT, "
        "mode TEXT DEFAULT 'default', "
        "last_seen TEXT)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS blocked ("
        "user_id INTEGER PRIMARY KEY)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS rate_limit ("
        "user_id INTEGER, "
        "timestamp TEXT)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS reply_state ("
        "admin_id INTEGER PRIMARY KEY, "
        "target_id INTEGER)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_user_time "
        "ON rate_limit(user_id, timestamp)"
    )
    conn.commit()
    conn.close()


def save_user(user_id, username, full_name):
    conn = _db_connect()
    try:
        c = conn.cursor()
        now = _now_str()
        c.execute(
            "INSERT INTO users (user_id, username, full_name, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username = excluded.username, "
            "full_name = excluded.full_name, "
            "last_seen = excluded.last_seen",
            (user_id, username, full_name, now)
        )
        conn.commit()
    finally:
        conn.close()


def is_blocked(user_id):
    cached = _cache_get(_blocked_cache, user_id)
    if cached is not None:
        return cached
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM blocked WHERE user_id = ?", (user_id,))
        result = c.fetchone() is not None
    finally:
        conn.close()
    _cache_set(_blocked_cache, user_id, result)
    return result


def block_user(user_id):
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO blocked (user_id) VALUES (?)",
            (user_id,)
        )
        c.execute(
            "DELETE FROM reply_state WHERE target_id = ?",
            (user_id,)
        )
        conn.commit()
    finally:
        conn.close()
    _blocked_cache[user_id] = (True, time.time())


def unblock_user(user_id):
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM blocked WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    _blocked_cache[user_id] = (False, time.time())


def get_all_users():
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT user_id, username, full_name FROM users "
            "ORDER BY last_seen DESC"
        )
        return c.fetchall()
    finally:
        conn.close()


def get_blocked_users():
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute("SELECT user_id FROM blocked")
        return [r[0] for r in c.fetchall()]
    finally:
        conn.close()


def find_user_by_username(username):
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT user_id FROM users WHERE username = ?",
            (username,)
        )
        result = c.fetchone()
        return result[0] if result else None
    finally:
        conn.close()


def get_user_mode(user_id):
    cached = _cache_get(_mode_cache, user_id)
    if cached is not None:
        return cached
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT mode FROM users WHERE user_id = ?",
            (user_id,)
        )
        result = c.fetchone()
    finally:
        conn.close()
    mode = result[0] if result else "default"
    _cache_set(_mode_cache, user_id, mode)
    return mode


def set_user_mode(user_id, mode):
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET mode = ? WHERE user_id = ?",
            (mode, user_id)
        )
        conn.commit()
    finally:
        conn.close()
    _mode_cache[user_id] = (mode, time.time())


def set_reply_target(admin_id, target_id):
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO reply_state (admin_id, target_id) "
            "VALUES (?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET "
            "target_id = excluded.target_id",
            (admin_id, target_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_reply_target(admin_id):
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT target_id FROM reply_state WHERE admin_id = ?",
            (admin_id,)
        )
        result = c.fetchone()
        return result[0] if result else None
    finally:
        conn.close()


def clear_reply_target(admin_id):
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(
            "DELETE FROM reply_state WHERE admin_id = ?",
            (admin_id,)
        )
        conn.commit()
    finally:
        conn.close()


def check_rate_limit(user_id):
    conn = _db_connect()
    try:
        c = conn.cursor()
        now = datetime.now()
        one_min_ago = (
            now - timedelta(minutes=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "SELECT COUNT(*) FROM rate_limit "
            "WHERE user_id = ? AND timestamp > ?",
            (user_id, one_min_ago)
        )
        count = c.fetchone()[0]
        if count >= RATE_LIMIT:
            return False
        c.execute(
            "INSERT INTO rate_limit (user_id, timestamp) VALUES (?, ?)",
            (user_id, now.strftime("%Y-%m-%d %H:%M:%S"))
        )
        ten_min_ago = (
            now - timedelta(minutes=10)
        ).strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "DELETE FROM rate_limit WHERE timestamp < ?",
            (ten_min_ago,)
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_user_chat(user_id):
    if user_id not in user_chats:
        user_chats[user_id] = client.chats.create(
            model="gemini-3.5-flash"
        )
    return user_chats[user_id]


def tg_request(method, payload, timeout=15):
    try:
        url = f"{TELEGRAM_API}/{method}"
        r = http_session.post(url, json=payload, timeout=timeout)
        return r.json()
    except Exception as e:
        print(f"[tg_request:{method}] خطا: {e}")
        return None


def tg_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    if len(text) > 4000:
        text = text[:4000] + "\n\n...(پیام کوتاه شد)"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return tg_request("sendMessage", payload)


def tg_edit_message(chat_id, message_id, text,
                    reply_markup=None, parse_mode=None):
    if len(text) > 4000:
        text = text[:4000] + "\n\n...(پیام کوتاه شد)"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return tg_request("editMessageText", payload)


def tg_send_typing(chat_id):
    try:
        url = f"{TELEGRAM_API}/sendChatAction"
        payload = {"chat_id": chat_id, "action": "typing"}
        http_session.post(url, json=payload, timeout=5)
    except Exception:
        pass


def tg_copy_message(to_chat_id, from_chat_id,
                    message_id, reply_markup=None):
    payload = {
        "chat_id": to_chat_id,
        "from_chat_id": from_chat_id,
        "message_id": message_id,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_request("copyMessage", payload, timeout=30)


def tg_get_file(file_id):
    try:
        url = f"{TELEGRAM_API}/getFile"
        r = http_session.get(
            url,
            params={"file_id": file_id},
            timeout=15
        )
        data = r.json()
        if not data.get("ok"):
            return None, None, 0
        file_path = data["result"]["file_path"]
        file_size = data["result"].get("file_size", 0)
        if file_size > MAX_PHOTO_SIZE:
            return None, file_path, file_size
        file_url = (
            f"https://api.telegram.org/file/bot"
            f"{TELEGRAM_TOKEN}/{file_path}"
        )
        file_bytes = http_session.get(file_url, timeout=30).content
        return file_bytes, file_path, file_size
    except Exception as e:
        print(f"[tg_get_file] خطا: {e}")
        return None, None, 0


MODES = {
    "default": (
        "تو یه دستیار هوشمند، مفید و خوش‌برخورد هستی. "
        "همیشه به زبان فارسی و واضح جواب بده."
    ),
    "coder": (
        "تو یه برنامه‌نویس حرفه‌ای هستی. "
        "به سوالات برنامه‌نویسی با کد و توضیح کامل جواب بده."
    ),
    "poet": (
        "تو یه شاعر فارسی‌زبان هستی. "
        "جواب‌هات رو به صورت شعر و ادبی بده."
    ),
    "translator": (
        "تو یه مترجم حرفه‌ای هستی. "
        "متن‌ها رو به فارسی یا انگلیسی ترجمه کن."
    ),
    "teacher": (
        "تو یه معلم صبور و دقیق هستی. "
        "مفاهیم رو ساده و با مثال توضیح بده."
    ),
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
            full_prompt = (
                f"{system_prompt}\n\nسوال کاربر: {user_text}"
            )
            response = chat.send_message(full_prompt)
            if response and response.text:
                return response.text.strip()
            return "متأسفانه نتونستم جواب بدم."
        except Exception as e:
            err_str = str(e)
            print(f"[ask_gemini] تلاش {attempt + 1} خطا: {err_str}")
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return "محدودیت درخواست! کمی صبر کن."
            if attempt < max_retries - 1:
                user_chats.pop(user_id, None)
                time.sleep(0.5)
                continue
            return "متأسفانه یه خطا پیش اومد."


def ask_gemini_with_image(user_id, image_bytes,
                          mime_type, caption=""):
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
        return "متأسفانه نتونستم عکس رو تحلیل کنم."
    except Exception as e:
        print(f"[ask_gemini_with_image] خطا: {e}")
        return "متأسفانه یه خطا تو تحلیل عکس پیش اومد."


def notify_admin_text(user_id, username,
                      full_name, message_text):
    if not ADMIN_ID:
        return
    preview = message_text[:100]
    if len(message_text) > 100:
        preview += "..."
    text = (
        f"کاربر در حال استفاده از ربات\n\n"
        f"نام: {full_name}\n"
        f"یوزرنیم: @{username or '-'}\n"
        f"آیدی: {user_id}\n\n"
        f"پیام: {preview}"
    )
    reply_markup = {
        "inline_keyboard": [
            [{"text": "جواب ربات",
              "callback_data": f"answer:{user_id}"}],
            [{"text": "پاسخ دادن",
              "callback_data": f"reply:{user_id}"}],
            [{"text": "بلاک",
              "callback_data": f"block:{user_id}"}]
        ]
    }
    tg_send_message(ADMIN_ID, text, reply_markup=reply_markup)


def notify_admin_photo(user_id, username, full_name,
                       caption, from_chat_id, message_id):
    if not ADMIN_ID:
        return
    reply_markup = {
        "inline_keyboard": [
            [{"text": "جواب ربات",
              "callback_data": f"answer:{user_id}"}],
            [{"text": "پاسخ دادن",
              "callback_data": f"reply:{user_id}"}],
            [{"text": "بلاک",
              "callback_data": f"block:{user_id}"}]
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
    tg_copy_message(
        ADMIN_ID, from_chat_id,
        message_id, reply_markup=reply_markup
    )


def handle_callback(cb):
    user_id = cb["from"]["id"]
    data = cb.get("data", "")
    cb_id = cb["id"]
    message_id = cb["message"]["message_id"]

    try:
        url = f"{TELEGRAM_API}/answerCallbackQuery"
        http_session.post(
            url,
            json={"callback_query_id": cb_id},
            timeout=5
        )
    except Exception:
        pass

    if data.startswith("mode:"):
        mode = data.split(":")[1]
        set_user_mode(user_id, mode)
        try:
            mode_name = MODE_NAMES.get(mode, mode)
            url = f"{TELEGRAM_API}/editMessageText"
            payload = {
                "chat_id": user_id,
                "message_id": message_id,
                "text": f"حالت روی «{mode_name}» تنظیم شد."
            }
            http_session.post(url, json=payload, timeout=10)
        except Exception:
            pass

    elif data.startswith("reply:"):
        target_user_id = int(data.split(":")[1])
        set_reply_target(user_id, target_user_id)
        tg_send_message(
            ADMIN_ID,
            f"پیام خود را برای کاربر {target_user_id} بنویسید."
        )

    elif data.startswith("block:"):
        target_user_id = int(data.split(":")[1])
        block_user(target_user_id)
        tg_send_message(
            ADMIN_ID,
            f"کاربر {target_user_id} بلاک شد."
        )

    elif data.startswith("answer:"):
        target_user_id = int(data.split(":")[1])
        answer = last_answers.get(target_user_id)
        if answer:
            text = (
                f"جواب ربات به کاربر {target_user_id}:\n\n"
                f"{answer}"
            )
            tg_send_message(ADMIN_ID, text)
        else:
            tg_send_message(
                ADMIN_ID,
                f"هنوز جوابی برای کاربر {target_user_id} ثبت نشده."
            )


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
            "- /unblock <id> آنبلاک کردن\n\n"
            "برای پاسخ به کاربر، روی دکمه‌ی «پاسخ دادن» بزنید."
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
            tg_send_message(
                admin_id,
                "استفاده: /unblock <id یا یوزرنیم>"
            )
            return
        target = parts[1].strip().replace("@", "")
        target_id = None
        try:
            target_id = int(target)
        except ValueError:
            target_id = find_user_by_username(target)
        if not target_id:
            tg_send_message(
                admin_id,
                f"کاربر «{target}» پیدا نشد."
            )
            return
        unblock_user(target_id)
        tg_send_message(
            admin_id,
            f"کاربر {target_id} آنبلاک شد."
        )
        return

    target_user_id = get_reply_target(admin_id)
    if target_user_id:
        clear_reply_target(admin_id)
        tg_send_message(
            target_user_id,
            f"پیام سازنده:\n\n{text}"
        )
        tg_send_message(admin_id, "پیام ارسال شد.")
        return

    tg_send_message(
        admin_id,
        "برای پاسخ، روی دکمه‌ی «پاسخ دادن» بزنید."
    )


def handle_text(message, chat_id, user_id,
                username, full_name, text):
    processing_msg = tg_send_message(chat_id, "در حال پردازش...")
    processing_msg_id = None
    if processing_msg and processing_msg.get("ok"):
        processing_msg_id = processing_msg["result"]["message_id"]

    notify_admin_text(user_id, username, full_name, text)

    answer = ask_gemini(user_id, text)
    last_answers[user_id] = answer

    final_text = f"پیام ربات:\n\n{answer}"

    if processing_msg_id:
        tg_edit_message(chat_id, processing_msg_id, final_text)
    else:
        tg_send_message(chat_id, final_text)

    _cleanup_cache()


def handle_photo(message, chat_id, user_id, username,
                 full_name, caption, photo):
    processing_msg = tg_send_message(
        chat_id, "در حال پردازش عکس..."
    )
    processing_msg_id = None
    if processing_msg and processing_msg.get("ok"):
        processing_msg_id = processing_msg["result"]["message_id"]

    largest_photo = photo[-1]
    file_id = largest_photo["file_id"]

    notify_admin_photo(
        user_id, username, full_name, caption,
        chat_id, message["message_id"]
    )

    image_bytes, file_path, file_size = tg_get_file(file_id)

    if file_size > MAX_PHOTO_SIZE:
        error_text = (
            "عکس خیلی بزرگه! "
            "لطفاً عکس کوچیک‌تری بفرست (زیر ۵ مگابایت)."
        )
        if processing_msg_id:
            tg_edit_message(chat_id, processing_msg_id, error_text)
        else:
            tg_send_message(chat_id, error_text)
        return

    if not image_bytes:
        error_text = "متأسفانه نتونستم عکس رو دریافت کنم."
        if processing_msg_id:
            tg_edit_message(chat_id, processing_msg_id, error_text)
        else:
            tg_send_message(chat_id, error_text)
        return

    mime_type = "image/jpeg"
    if file_path and file_path.lower().endswith(".png"):
        mime_type = "image/png"
    elif file_path and file_path.lower().endswith(".webp"):
        mime_type = "image/webp"

    answer = ask_gemini_with_image(
        user_id, image_bytes, mime_type, caption
    )
    last_answers[user_id] = answer

    final_text = f"پیام ربات:\n\n{answer}"

    if processing_msg_id:
        tg_edit_message(chat_id, processing_msg_id, final_text)
    else:
        tg_send_message(chat_id, final_text)

    _cleanup_cache()


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
            tg_send_message(
                chat_id,
                "شما توسط مدیریت مسدود شده‌اید."
            )
            return "OK", 200

        if photo:
            handle_photo(
                message, chat_id, user_id,
                username, full_name, caption, photo
            )
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
            tg_send_message(
                chat_id,
                "حافظه‌ی مکالمه پاک شد."
            )
            return "OK", 200

        if text == "/mode":
            keyboard = []
            for key, name in MODE_NAMES.items():
                keyboard.append([
                    {"text": name,
                     "callback_data": f"mode:{key}"}
                ])
            reply_markup = {"inline_keyboard": keyboard}
            tg_send_message(
                chat_id,
                "یه حالت انتخاب کن:",
                reply_markup=reply_markup
            )
            return "OK", 200

        if not text:
            tg_send_message(
                chat_id,
                "لطفاً یه پیام متنی یا عکس بفرست."
            )
            return "OK", 200

        if not check_rate_limit(user_id):
            tg_send_message(
                chat_id,
                "محدودیت! هر دقیقه فقط ۳ پیام."
            )
            return "OK", 200

        handle_text(
            message, chat_id, user_id,
            username, full_name, text
        )
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
        http_session.get(
            f"{TELEGRAM_API}/deleteWebhook",
            timeout=15
        )
        url = f"{TELEGRAM_API}/setWebhook"
        params = {
            "url": webhook_url,
            "drop_pending_updates": True,
            "allowed_updates": [
                "message", "callback_query"
            ]
        }
        r = http_session.get(url, params=params, timeout=15)
        print(f"Webhook ست شد: {r.json()}")
    except Exception as e:
        print(f"خطا: {e}")


if __name__ == "__main__":
    init_db()
    setup_webhook()
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )