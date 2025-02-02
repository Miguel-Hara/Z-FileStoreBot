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

FLOOD_WAIT_MULTIPLIER = 1.5
MAX_CONCURRENT_REQUESTS = 3
request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

filter_text = filters.create(lambda _, __, message: bool(message.text and not message.text.startswith("/")))

async def report_error(bot: Client, error_type: str, details: str, chat_id: Optional[int] = None):
    """Send error reports to log channel."""
    try:
        chat_info = f"\nChat ID: {chat_id}" if chat_id else ""
        error_msg = f"#{error_type}\n{details}{chat_info}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        await bot.send_message(config.LOG_CHANNEL, error_msg)
    except Exception as e:
        print(f"Error sending error report: {str(e)}")

async def handle_flood_wait(func, *args, **kwargs):
    """Handle FloodWait errors by waiting and retrying."""
    while True:
        try:
            return await func(*args, **kwargs)
        except errors.FloodWait as e:
            wait_time = int(float(str(e.value)) * FLOOD_WAIT_MULTIPLIER)
            print(f"FloodWait: Sleeping for {wait_time} seconds")
            await report_error(args[0], "FloodWait", f"Waiting for {wait_time} seconds")
            await asyncio.sleep(wait_time)
        except Exception as e:
            print(f"Unexpected error in handle_flood_wait: {str(e)}")
            raise

async def get_invite_link(bot: Client, chat_id: int) -> Optional[str]:
    """
    Retrieve invite link for a chat.
    Checks the DB cache first, then requests a new link if needed.
    """
    cached_link = await db.get_cached_link(chat_id)
    if cached_link:
        return cached_link

    async with request_semaphore:
        try:
            invite_link = await handle_flood_wait(
                bot.invoke,
                raw.functions.messages.ExportChatInvite(  # type: ignore[reportPrivateImportUsage]
                    peer=await bot.resolve_peer(chat_id), # type: ignore[reportPrivateImportUsage]
                    legacy_revoke_permanent=True,
                    request_needed=False,
                )
            )
            if invite_link and invite_link.link:
                await db.update_link(chat_id, invite_link.link)
                return invite_link.link
        except errors.ChatAdminRequired:
            try:
                chat_info = await bot.get_chat(chat_id)
                chat_name = getattr(chat_info, 'title', f"Chat ID: {chat_id}")
                await bot.send_message(
                    config.LOG_CHANNEL,
                    f"#Error generating invite link for {chat_name} ({chat_id}): Admin privileges required."
                )
            except Exception as log_error:
                print(f"Error logging admin required issue: {str(log_error)}")
        except Exception as e:
            print(f"Error generating invite link for {chat_id}: {str(e)}")
    return None

async def process_channel(bot: Client, chat: dict, search_text: str) -> Optional[dict]:
    """
    Process a single channel by checking if its title contains the search text.
    If it matches, retrieve (or generate) its invite link.
    """
    try:
        async with request_semaphore:
            channel = await handle_flood_wait(bot.get_chat, chat['id'])
            if search_text in channel.title.lower():
                link = await get_invite_link(bot, chat['id'])
                if link:
                    return {'title': channel.title, 'link': link}
    except errors.ChannelInvalid:
        await report_error(bot, "Channel_Invalid", "Channel is invalid, removing from DB", chat['id'])
        await db.delete_chat(chat['id'])
    except Exception as e:
        if "CHANNEL_PRIVATE" in str(e):
            await report_error(bot, "Channel_Private", "Channel is private, removing from DB", chat['id'])
            await db.delete_chat(chat['id'])
        else:
            await report_error(bot, "Channel_Error", f"Error processing channel: {str(e)}", chat['id'])
    return None

async def process_batch(bot: Client, batch: List[dict], search_text: str) -> List[dict]:
    """Process a batch of channels concurrently."""
    tasks = [process_channel(bot, chat, search_text) for chat in batch]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]

@Client.on_message((filters.private | filters.group) & filter_text)
async def search_channels(bot: Client, message: Message):
    """
    Search channels in the database that match the incoming message text.
    Reply with a message containing a button that links to the channel.
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
                await asyncio.sleep(1)
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
