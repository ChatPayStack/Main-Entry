import redis
from fastapi import FastAPI, Header, Request
import os
from dotenv import load_dotenv
import json
import httpx
import io
from openai import OpenAI

load_dotenv()

app = FastAPI(title="ChatPay Main API")

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


client = OpenAI()

async def fetch_voice_bytes(file_id: str) -> bytes:
    async with httpx.AsyncClient() as client:
        try:
            # 1) get file_path
            r1 = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
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
            r2 = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
            r2.raise_for_status()
        except Exception as e:
            print("ERR_TELEGRAM_DOWNLOAD", {"file_path": locals().get("file_path"), "status": getattr(r2, "status_code", None), "err": repr(e)})
            raise
        return r2.content


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad secret")

    try:
        msg = await request.json()
    except Exception as e:
        print("ERR_REQUEST_JSON", repr(e))
        raise

    print(msg)
    message = msg.get("message", {})
    callback = msg.get("callback_query")
    if callback:
        print("Callback received:", callback.get("data"))
        r.rpush("chatpay_queue", json.dumps(msg))
        return {"ok": True}
    if message.get("voice") or message.get("audio"):
        print("Voice")
        voice = message.get("voice")
        file_id = voice["file_id"]

        try:
            audio_bytes = await fetch_voice_bytes(file_id)
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
        r.rpush("chatpay_queue", json.dumps(msg))
        
    elif "text" in message:
        sender = message.get("from", {})     
        sender_username = sender.get("username")
        print("Message received from", sender_username)

        r.rpush("chatpay_queue", json.dumps(msg))
        print(f"Queued full message from {sender_username}")
    else:
        async with httpx.AsyncClient(timeout=15) as localClient:
            chat_id = message["chat"]["id"]
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