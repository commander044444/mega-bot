"""MEGA BOT configuration.

For a private repository you may place secrets here, as requested. For Railway,
set BOT_TOKEN and DATABASE_URL as environment variables when possible.
Never commit real credentials to a public repository.
"""
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BALE_BOT_TOKEN_HERE")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://USER:PASSWORD@HOST:5432/DATABASE")
OWNER_ID = int(os.getenv("OWNER_ID", "1967315238"))
BOT_VERSION = os.getenv("BOT_VERSION", "2.0.0 Beta")
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "25"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
SUPPORT_TEXT = os.getenv("SUPPORT_TEXT", "برای پشتیبانی، با تیم مدیریت ربات در ارتباط باشید.")
DONATION_TEXT = os.getenv("DONATION_TEXT", "برای حمایت مالی از توسعه MEGA BOT با مالک ربات هماهنگ کنید.")
