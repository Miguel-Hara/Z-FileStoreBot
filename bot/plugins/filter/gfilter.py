import time
import asyncio, re
from typing import Optional, List, Dict
from datetime import datetime
from difflib import SequenceMatcher

from pyrogram import filters, raw, errors
from pyrogram.client import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from bot.utilities.helpers import RateLimiter
from bot.config import config
from bot.database import MongoDB
from fuzzywuzzy import process

db = MongoDB()
request_semaphore = asyncio.Semaphore(3)

filter_text = filters.create(lambda _, __, message: bool(message.text and not message.text.startswith("/")))

async def report_error(bot: Client, error_type: str, details: str, chat_id: Optional[int] = None):
    """Logs errors to a configured log channel."""
    try:
        chat_info = f"\nChat ID: {chat_id}" if chat_id else ""
        error_msg = f"#{error_type}\n{details}{chat_info}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        log_channel = config.LOG_CHANNEL
        if isinstance(log_channel, int) and isinstance(bot, Client):
            await bot.send_message(log_channel, error_msg)
        else:
            print(f"LOG_CHANNEL error: {log_channel}, Bot type: {type(bot)}")
    except Exception as e:
        print(f"Error sending error report: {str(e)}")

async def get_invite_link(bot: Client, chat_id: int) -> Optional[str]:
    async with request_semaphore:
        chat_data = await db.grp.find_one({"id": chat_id}, {"invite_link": 1})
        
        if (chat_data and 
            chat_data.get("invite_link") and 
            chat_data["invite_link"].get("link") and  # Check if link exists and is not empty
            time.time() - chat_data["invite_link"].get("timestamp", 0) < config.CACHE_DURATION):
            return chat_data["invite_link"].get("link")

        while True:
            try:
                invite_link = await bot.invoke(
                    raw.functions.messages.ExportChatInvite( # type: ignore[reportPrivateImportUsage]
                        peer=await bot.resolve_peer(chat_id), # type: ignore[reportPrivateImportUsage]
                        legacy_revoke_permanent=True,
                        request_needed=False,
                    )
                )
                if isinstance(invite_link, raw.types.ChatInviteExported) and invite_link.link: # type: ignore[reportPrivateImportUsage]
                    await db.update_link(chat_id, invite_link.link)
                    return invite_link.link
                return None
            except errors.FloodWait as e:
                wait_time = int(e.value) + 5 # type: ignore[reportPrivateImportUsage]
                print(f"FloodWait: Sleeping for {wait_time} seconds")
                await asyncio.sleep(wait_time)
            except errors.ChatAdminRequired:
                await report_error(bot, "Chat_Admin_Required", "Bot lacks admin rights.", chat_id)
                return None
            except Exception as e:
                print(f"Error generating invite link for {chat_id}: {str(e)}")
                return None
        

async def ai_spell_check(wrong_name):
    async def search_channel(wrong_name):
        channels, offset, total_results = await db.get_search_results(wrong_name)
        return [channel['title'] for channel in channels]

    channel_list = await search_channel(wrong_name)
    if not channel_list:
        return

    for _ in range(5):
        closest_match = process.extractOne(wrong_name, channel_list)
        if not closest_match or closest_match[1] <= 80:
            return 
        channel = closest_match[0]
        channels, offset, total_results = await db.get_search_results(channel)
        if channels:
            return channel
        channel_list.remove(channel)
    return

@Client.on_message((filters.private | filters.group) & filter_text)
@RateLimiter.hybrid_limiter(func_count=1)
async def search_channels(bot: Client, message: Message):
    """
    Search for channels using AI spell check while ignoring noise words.
    If the resulting query (after removing words like “hindi dub”) is too short,
    then do not perform the search.
    """
    try:
        search_text = message.text.strip()
        if len(search_text) < 3:
            return

        ignore_words = {"hindi dub", "hindi dubbed", "dubbed", "dub"}
        filtered_text = search_text
        for word in ignore_words:
            filtered_text = re.sub(re.escape(word), '', filtered_text, flags=re.IGNORECASE)
        filtered_text = " ".join(filtered_text.split())

        if not filtered_text or filtered_text.lower() in {"hindi"}:
            return

        corrected_channel = await ai_spell_check(filtered_text)
        if not corrected_channel:
            return

        chat = await db.grp.find_one({"title": corrected_channel})
        if not chat:
            return

        link = await get_invite_link(bot, chat['id'])
        if not link:
            return

        buttons = [[
            InlineKeyboardButton(
                text="ᴅᴏᴡɴʟᴏᴀᴅ",
                url=str(link)
            )
        ]]
        
        text = f"<b><a href='{link}'>{chat['title']}</a></b>"

        await message.reply_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
            quote=True 
        )

    except Exception as e:
        error_msg = f"Error in search_channels: {str(e)}"
        print(error_msg)
        await report_error(bot, "Search_Error", error_msg)

        try:
            await message.reply_text(
                "🚨 Temporary issue. Please try again later.",
                quote=True
            )
        except:
            pass