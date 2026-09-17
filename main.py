import os
import requests
from flask import Flask, request
from google import genai
from google.genai import types

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1")
)

flask_app = Flask(__name__)


def tg_send_message(chat_id, text, reply_to_message_id=None):
    if len(text) > 4000:
        text = text[:4000] + "\n\n...(پیام کوتاه شد)"
    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=30)
        return r.json()
    except Exception as e:
        print(f"[tg_send_message] خطا: {e}")
        return None


def tg_send_typing(chat_id):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=10
        )
    except Exception:
        pass


def ask_gemini(user_text):
    try:
        prompt = (
            "تو یه دستیار هوشمند، مفید و خوش‌برخورد هستی. "
            "همیشه به زبان فارسی و واضح جواب بده. "
            "جواب‌هات مفید و مختصر باشن.\n\n"
            f"سوال کاربر: {user_text}"
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                )
            )
        )

        if response and response.text:
            return response.text.strip()
        return "⚠️ متأسفانه نتونستم جواب بدم. دوباره امتحان کن."

    except Exception as e:
        print(f"[ask_gemini] خطا: {e}")
        import traceback
        traceback.print_exc()
        return "⚠️ متأسفانه یه خطا پیش اومد. لطفاً دوباره امتحان کن."


@flask_app.route("/", methods=["GET"])
def index():
    return "Gemini Bot is running ✅", 200


@flask_app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            return "OK", 200

        message = data.get("message")
        if not message:
            return "OK", 200

        chat_id = message["chat"]["id"]
        user_text = message.get("text", "")

        if user_text == "/start":
            tg_send_message(
                chat_id,
                "سلام 👋\n\n"
                "من یه دستیار هوش مصنوعی هستم 🤖\n"
                "هر سوالی داری بپرس، جواب می‌دم!"
            )
            return "OK", 200

        if not user_text:
            tg_send_message(chat_id, "لطفاً یه پیام متنی بفرست 🙏")
            return "OK", 200

        tg_send_typing(chat_id)
        answer = ask_gemini(user_text)
        tg_send_message(chat_id, answer)

        return "OK", 200

    except Exception as e:
        print(f"[webhook] خطا: {e}")
        import traceback
        traceback.print_exc()
        return "OK", 200


def setup_webhook():
    if not RENDER_URL:
        print("⚠️ RENDER_EXTERNAL_URL ست نشده")
        return
    webhook_url = f"{RENDER_URL}/webhook"
    print(f"🔧 در حال ست کردن Webhook روی: {webhook_url}")
    try:
        requests.get(f"{TELEGRAM_API}/deleteWebhook", timeout=15)
        r = requests.get(
            f"{TELEGRAM_API}/setWebhook",
            params={"url": webhook_url, "drop_pending_updates": True},
            timeout=15
        )
        print(f"✅ Webhook ست شد: {r.json()}")
    except Exception as e:
        print(f"❌ خطا تو ست کردن Webhook: {e}")


if __name__ == "__main__":
    setup_webhook()
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)