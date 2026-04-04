import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")

client = AsyncIOMotorClient(MONGODB_URI)
db = client["ChatPay"]
login_collection = db["login"]


async def get_bot_token(business_id: str) -> str | None:
    doc = await login_collection.find_one({"business_id": business_id})
    if not doc:
        return None
    return doc.get("telegram_bot_token")
'''
if __name__ == "__main__":
    import asyncio

    async def test():
        business_id = "791eeccc-4dc0-4231-8fa9-f517804eb0e1"
        token = await get_bot_token(business_id)
        print("Bot token:", token)

    asyncio.run(test())
'''