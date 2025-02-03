import time
import asyncio
from typing import Optional, List, Dict
from datetime import datetime
from difflib import SequenceMatcher

from pyrogram import filters, raw, errors
from pyrogram.client import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from bot.utilities.helpers import RateLimiter
from bot.config import config
from bot.database import MongoDB
from fuzzywuzzy import fuzz

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

@Client.on_message((filters.private | filters.group) & filter_text)
@RateLimiter.hybrid_limiter(func_count=1)
async def search_channels(bot: Client, message: Message):
    """
    Search for channels matching user's query with fuzzy matching.
    Returns the best matching channel.
    """
    try:
        search_text = message.text.strip()
        if len(search_text) < 3:
            return

        cursor = await db.get_all_chats()
        chats = await cursor.to_list(length=None)
        
        batch_size = 10
        all_matches = []
        
        for i in range(0, len(chats), batch_size):
            batch = chats[i:i + batch_size]
            
            for chat in batch:
                try:
                    db_chat = await db.grp.find_one({"id": chat['id']})
                    if db_chat and 'title' in db_chat:
                        title = db_chat['title']
                    else:
                        channel = await bot.get_chat(chat['id'])
                        title = channel.title
                    
                    similarity = fuzz.token_sort_ratio(search_text.lower(), title.lower())
                    if similarity > 60:  # 60% similarity threshold
                        link = await get_invite_link(bot, chat['id'])
                        if link:
                            all_matches.append({
                                'title': title,
                                'link': link,
                                'similarity': similarity
                            })
                
                except errors.ChannelInvalid:
                    await report_error(bot, "Channel_Invalid", "Channel is invalid, removing from DB", chat['id'])
                    await db.delete_chat(chat['id'])
                except errors.RPCError as e:
                    if "CHANNEL_PRIVATE" in str(e):
                        await report_error(bot, "Channel_Private", "Channel is private, removing from DB", chat['id'])
                        await db.delete_chat(chat['id'])
                    else:
                        await report_error(bot, "Channel_Error", f"Error processing channel: {str(e)}", chat['id'])
            
            if all_matches:
                best_match = max(all_matches, key=lambda x: x['similarity'])
                if best_match['similarity'] > 80:
                    break

        if all_matches:
            best_match = max(all_matches, key=lambda x: x['similarity'])
            
            buttons = [[
                InlineKeyboardButton(
                    text="ᴅᴏᴡɴʟᴏᴀᴅ",
                    url=best_match['link']
                )
            ]]
            
            text = f"<b><a href='{best_match['link']}'>{best_match['title']}</a></b>"
            
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
        await message.reply_text(
            "An error occurred while processing your request.",
            quote=True
        )