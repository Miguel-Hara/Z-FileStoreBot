import time
import asyncio
import re
from typing import Optional, List
from datetime import datetime
from pyrogram import filters, raw, errors
from pyrogram.client import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from bot.utilities.helpers import RateLimiter
from bot.config import config
from bot.database import MongoDB
from fuzzywuzzy import process

db = MongoDB()

async def get_invite_link(bot: Client, chat_id: int) -> Optional[str]:
    chat_data = await db.grp.find_one({"id": chat_id}, {"invite_link": 1})
    if (chat_data and chat_data.get("invite_link") and 
        chat_data["invite_link"].get("link") and 
        time.time() - chat_data["invite_link"].get("timestamp", 0) < config.CACHE_DURATION):
        return chat_data["invite_link"].get("link")
    try:
        invite_link = await bot.invoke(
            raw.functions.messages.ExportChatInvite(  # type: ignore[reportPrivateImportUsage]
                peer=await bot.resolve_peer(chat_id),  # type: ignore[reportPrivateImportUsage]
                legacy_revoke_permanent=True,
                request_needed=True,
            )
        )
        if isinstance(invite_link, raw.types.ChatInviteExported) and invite_link.link:  # type: ignore[reportPrivateImportUsage]
            await db.update_link(chat_id, invite_link.link)
            return invite_link.link
    except errors.FloodWait as e:
        await asyncio.sleep(int(e.value) + 5)  # type: ignore[reportPrivateImportUsage]
        return await get_invite_link(bot, chat_id)
    except errors.ChatAdminRequired:
        await bot.send_message(config.LOG_CHANNEL, f"#Chat_Admin_Required\nBot lacks admin rights.\nChat ID: {chat_id}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except errors.RPCError as e:
        if "CHANNEL_PRIVATE" in str(e):
            await bot.send_message(config.LOG_CHANNEL, f"#Channel_Private\nThe channel/supergroup is not accessible.\nChat ID: {chat_id}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        await bot.send_message(config.LOG_CHANNEL, f"#Invite_Link_Error\n{str(e)}\nChat ID: {chat_id}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return None

async def ai_spell_check(wrong_name):
    async def search_channel(name):
        try:
            channels, _, _ = await asyncio.wait_for(db.get_search_results(name), timeout=40)
            return [channel['title'] for channel in channels]
        except Exception:
            return []

    channel_list = await search_channel(wrong_name)
    if not channel_list: return ""
    
    for _ in range(5):
        closest_match = process.extractOne(wrong_name, channel_list)
        if not closest_match or closest_match[1] <= 80: return ""
        
        channel = closest_match[0]
        channels = await search_channel(channel)
        if channels: return channel
        channel_list.remove(channel)
    
    return ""

@Client.on_message(filters.private | filters.group)
@RateLimiter.hybrid_limiter(func_count=1)
async def search_channels(bot: Client, message: Message):
    try:
        if not message.text or message.text.startswith("/"):
            return
        search_text = message.text.strip()
        if len(search_text) < 3:
            return
        ignore_words = {
            "hindi dub", "hindi dubbed", "dubbed", "dub",
            "please", "pls", "plz", "dedo", "deedo", "mujhe", "dekhna hai", "ka",
            "new", "episode", "ep", "milega?", "milega", "milega yahan", "hain",
            "hai kya", "available", "give", "give me", "ha"
        }
        for word in ignore_words:
            search_text = re.sub(re.escape(word), '', search_text, flags=re.IGNORECASE)
        search_text = " ".join(search_text.split())
        if not search_text or search_text.lower() in {"hindi"}:
            return
        corrected_channel = await ai_spell_check(search_text)
        if not corrected_channel:
            return
        chat = await db.grp.find_one({"title": corrected_channel})
        if not chat:
            return
        link = await get_invite_link(bot, chat['id'])
        if not link:
            return
        buttons = [[InlineKeyboardButton(text="ᴅᴏᴡɴʟᴏᴀᴅ", url=str(link))]]
        await message.reply_text(
            text=f"<b><a href='{link}'>{chat['title']}</a></b>",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
            quote=True
        )
    except Exception as e:
        await bot.send_message(config.LOG_CHANNEL, f"#Search_Error\n{str(e)}")
