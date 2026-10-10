import asyncio
import logging
from config import BOT_TOKEN, DATABASE_URL, POLL_TIMEOUT, LOG_LEVEL
from mega_bot.bale_api import BaleAPI
from mega_bot.database import Database
from mega_bot.app import MegaBot


def validate_config() -> None:
    if not BOT_TOKEN or BOT_TOKEN == 'PASTE_YOUR_BALE_BOT_TOKEN_HERE':
        raise SystemExit('BOT_TOKEN را در config.py یا متغیر محیطی BOT_TOKEN تنظیم کن.')
    if not DATABASE_URL or DATABASE_URL.startswith('postgresql://USER:'):
        raise SystemExit('DATABASE_URL را در config.py یا متغیر محیطی DATABASE_URL تنظیم کن.')

async def main() -> None:
    validate_config()
    logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO), format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    api = BaleAPI(BOT_TOKEN)
    db = Database(DATABASE_URL)
    await api.start()
    try:
        await db.connect()
        bot = MegaBot(api, db)
        await bot.run(POLL_TIMEOUT)
    finally:
        await db.close()
        await api.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('MEGA BOT stopped.')
