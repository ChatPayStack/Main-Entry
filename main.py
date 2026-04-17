import redis
from fastapi import FastAPI, Header, Request
import os
from dotenv import load_dotenv
import json
import httpx
import io
from openai import OpenAI
import stripe
from db import get_bot_token

load_dotenv()

app = FastAPI(title="ChatPay Main API")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

client = OpenAI()

async def fetch_voice_bytes(file_id: str, bot_token: str) -> bytes:
    async with httpx.AsyncClient() as client:
        try:
            # 1) get file_path
            r1 = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getFile",
                params={"file_id": file_id},
            )
            try:
                j = r1.json()
            except Exception:
                j = {"_non_json": r1.text}

            if r1.status_code != 200 or not j.get("ok"):
                print("ERR_TELEGRAM_GETFILE_RESP", {"status": r1.status_code, "body": j})
                raise RuntimeError(f"getFile failed: {j}")

            file_path = j["result"]["file_path"]

        except Exception as e:
            print("ERR_TELEGRAM_GETFILE", {"file_id": file_id, "status": getattr(r1, "status_code", None), "err": repr(e)})
            raise
        try:
            # 2) download bytes
            r2 = await client.get(f"https://api.telegram.org/file/bot{bot_token}/{file_path}")
            r2.raise_for_status()
        except Exception as e:
            print("ERR_TELEGRAM_DOWNLOAD", {"file_path": locals().get("file_path"), "status": getattr(r2, "status_code", None), "err": repr(e)})
            raise
        return r2.content


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print("Stripe signature verification failed:", e)
        return {"status": "invalid"}

    if event["type"] == "checkout.session.completed":
        print("Reached the completed part")
        session = event["data"]["object"]
        metadata = session.get("metadata", {})
        business_id = metadata.get("business_id")
        # Push event to worker via Redis
        r.rpush(f"chatpay_queue_{business_id}", json.dumps({
            "type": "stripe_webhook",
            "metadata": metadata,
            "stripe_session_id": session.get("id"),
        }))

        print("Stripe completion pushed to queue")
    
    elif event["type"] in ["checkout.session.expired", "payment_intent.payment_failed"]:
        obj = event["data"]["object"]

        r.rpush("chatpay_queue", json.dumps({
            "type": "stripe_webhook_failed",
            "stripe_session_id": obj.get("id"),
            "metadata": obj.get("metadata", {})
        }))

    return {"status": "ok"}

@app.post("/coinbase-webhook")
async def coinbase_webhook(request: Request):
    try:
        payload = await request.json()
        print("Coinbase webhook received:", payload)

        event_type = payload.get("eventType")

        # ✅ Handle SUCCESS
        if event_type == "onramp.transaction.success":

            partner_ref = payload.get("partnerUserRef")

            if not partner_ref:
                print("❌ Missing partnerUserRef")
                return {"status": "ignored"}

            try:
                thread_id, payment_id = partner_ref.split(":")
            except Exception as e:
                print("❌ Invalid partnerUserRef format:", partner_ref)
                return {"status": "invalid_ref"}

            # Push to Redis (same pattern as Stripe)
            r.rpush("chatpay_queue", json.dumps({
                "type": "coinbase_webhook",
                "thread_id": thread_id,
                "payment_id": payment_id,
                "tx_hash": payload.get("txHash"),
                "amount": payload.get("purchaseAmount"),
                "currency": payload.get("purchaseCurrency"),
            }))

            print("✅ Coinbase success pushed to queue")

        # ❌ Handle FAILURE
        elif event_type == "onramp.transaction.failed":

            partner_ref = payload.get("partnerUserRef")

            if partner_ref:
                try:
                    thread_id, payment_id = partner_ref.split(":")
                except Exception:
                    return {"status": "invalid_ref"}

                r.rpush("chatpay_queue", json.dumps({
                    "type": "coinbase_webhook_failed",
                    "thread_id": thread_id,
                    "payment_id": payment_id,
                }))

                print("❌ Coinbase failure pushed to queue")

        return {"status": "ok"}

    except Exception as e:
        print("❌ Coinbase webhook error:", e)
        return {"status": "error"}

@app.post("/telegram-webhook/{business_id}")
async def telegram_webhook(business_id: str, request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
  
    try:
        msg = await request.json()
        msg["business_id"] = business_id
    except Exception as e:
        print("ERR_REQUEST_JSON", repr(e))
        raise

    print(msg)
    message = msg.get("message") or msg.get("edited_message") or {}
    callback = msg.get("callback_query")
    if callback:
        print("Callback received:", callback.get("data"))
        r.rpush(f"chatpay_queue_{business_id}", json.dumps(msg))
        return {"ok": True}
    if message.get("voice") or message.get("audio"):
        print("Voice")
        voice = message.get("voice")
        file_id = voice["file_id"]

        try:
            bot_token = BOT_TOKEN
            audio_bytes = await fetch_voice_bytes(file_id, bot_token)
        except Exception as e:
            print("ERR_FETCH_VOICE_BYTES", {"file_id": file_id, "err": repr(e)})
            return {"ok": True}

        buf = io.BytesIO(audio_bytes)
        buf.name = "voice.ogg"
        try:
            txt = client.audio.transcriptions.create(
                model="gpt-4o-transcribe",
                file=buf,
            ).text
        except Exception as e:
            print("ERR_TRANSCRIBE", {"file_id": file_id, "err": repr(e)})
            raise
        print(txt)
        message["text"] = txt
        msg["message"] = message
        r.rpush(f"chatpay_queue_{business_id}", json.dumps(msg))
        
    elif "text" in message:
        sender = message.get("from", {})     
        sender_username = sender.get("username")
        print("Message received from", sender_username)
        reply = message.get("reply_to_message")
        if reply:
            if "photo" in reply:
                print("📸 User replied to an image")
                print("Image reply payload:", reply)

            reply_text = reply.get("text")
        else:
            reply_text = None

        r.rpush(f"chatpay_queue_{business_id}", json.dumps(msg))
        print(f"Queued full message from {sender_username}")
    else:
        async with httpx.AsyncClient(timeout=15) as localClient:

            chat_id = message["chat"]["id"]
            print("Message in Mobs Case:",message)
            await localClient.post(
                TELEGRAM_SEND_URL,
                json={"chat_id": chat_id, "text": "Sorry we only support Voice Notes and Text"}
            )

    return {"ok": True}



@app.get("/queue")
def show_queue():
    items = r.lrange("chatpay_queue", 0, -1)  # get all items
    print("📜 Current Queue:", items)
    return {"queue_length": len(items), "items": items}


# -------------------- Health Check --------------------
@app.get("/")
def health_check():
    try:
        ping = r.ping()
    except Exception as e:
        ping = False
        print("Redis ping failed:", repr(e))
    return {"status": "Main API Running", "redis_connected": ping}