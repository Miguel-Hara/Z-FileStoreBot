import time
import asyncio
from typing import Optional, List
from datetime import datetime

from pyrogram import filters, raw, errors
from pyrogram.client import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from bot.config import config
from bot.database import MongoDB

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
            chat_data["invite_link"].get("link") and 
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

async def process_channel(bot: Client, chat: dict, search_text: str) -> Optional[dict]:
    """
    Processes a single channel. If its title matches the search text, retrieves or generates an invite link.
    """
    try:
        async with request_semaphore:
            channel = await bot.get_chat(chat['id'])
            if search_text in channel.title.lower():
                link = await get_invite_link(bot, chat['id'])
                if link:
                    return {'title': channel.title, 'link': link}
    except errors.ChannelInvalid:
        await report_error(bot, "Channel_Invalid", "Channel is invalid, removing from DB", chat['id'])
        await db.delete_chat(chat['id'])
    except errors.RPCError as e:
        if "CHANNEL_PRIVATE" in str(e):
            await report_error(bot, "Channel_Private", "Channel is private, removing from DB", chat['id'])
            await db.delete_chat(chat['id'])
        else:
            await report_error(bot, "Channel_Error", f"Error processing channel: {str(e)}", chat['id'])
    return None

async def process_batch(bot: Client, batch: List[dict], search_text: str) -> List[dict]:
    """Processes a batch of channels concurrently."""
    tasks = [process_channel(bot, chat, search_text) for chat in batch]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]

@Client.on_message((filters.private | filters.group) & filter_text)
async def search_channels(bot: Client, message: Message):
    """
    Searches for channels matching the user's query.
    Replies with an invite link button.
    """
    try:
        search_text = message.text.lower().strip()
        if len(search_text) < 3:
            return

        cursor = await db.get_all_chats()
        matched_channels = []
        batch: List[dict] = []
        batch_size = 5

        chats = await cursor.to_list(length=None)
        for chat in chats:
            batch.append(chat)
            if len(batch) >= batch_size:
                results = await process_batch(bot, batch, search_text)
                matched_channels.extend(results)
                batch = []
                await asyncio.sleep(3)

        if batch:
            results = await process_batch(bot, batch, search_text)
            matched_channels.extend(results)

        for channel in matched_channels:
            buttons = [[
                InlineKeyboardButton(
                    text="ᴅᴏᴡɴʟᴏᴀᴅ",
                    url=channel['link']
                )
            ]]
            text = f"<b><a href='{channel['link']}'>{channel['title']}</a></b>"
            await message.reply_text(
                text=text,
                reply_markup=InlineKeyboardMarkup(buttons),
                disable_web_page_preview=True
            )

        if not matched_channels:
            await message.reply_text("No matching channels found.")

    except Exception as e:
        error_msg = f"Error in search_channels: {str(e)}"
        print(error_msg)
        await report_error(bot, "Search_Error", error_msg)
        await message.reply_text("An error occurred while processing your request.")
