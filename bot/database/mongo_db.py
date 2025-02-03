import datetime
import time
import dns.resolver
from async_lru import alru_cache
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ConfigurationError
from bot.config import config

from .listener import Listener
from .moderation import Moderation


class MongoDB(Moderation, Listener):
    """
    A class representing a MongoDB database connection.

    Parameters:
        name (str | None): The name of the database to connect to. Defaults to config.MONGO_DB_NAME.
    """

    def __init__(self, name: str | None = None) -> None:
        """
        Initializes the MongoDB connection.

        Raises:
            ConfigurationError: If the MongoDB connection configuration is invalid.
        """
        try:
            self.client = AsyncIOMotorClient(host=str(config.MONGO_DB_URL))
        except ConfigurationError:
            dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
            dns.resolver.default_resolver.nameservers = ["8.8.8.8"]
            self.client = AsyncIOMotorClient(host=str(config.MONGO_DB_URL))
        self.db = self.client[name if name else config.MONGO_DB_NAME]
        self.grp = self.db.groups
        self.admins = self.db.admins

    @alru_cache(maxsize=10, ttl=10)
    async def add_user(self, user_id: int) -> bool:
        """
        Adds a user to the database.

        Parameters:
            user_id (int): The ID of the user to add.

        Returns:
            bool: Whether the user was added successfully.
        """
        collection = self.db["Users"]
        result = await collection.update_one(
            filter={"_id": user_id},
            update={"$set": {"_id": user_id}},
            upsert=True,
        )
        return result.acknowledged

    async def add_file(self, file_link: str, file_origin: int, file_data: list[dict]) -> bool:
        """
        Adds a file to the database.

        Parameters:
            file_link (str): The link to the file.
            file_origin (int): The origin of the file.
            file_data (list[dict]): The data associated with the file.

        Returns:
            bool: Whether the file was added successfully.
        """
        collection = self.db["Files"]
        result = await collection.update_one(
            filter={"_id": file_link},
            update={
                "$set": {
                    "file_origin": file_origin,
                    "files": file_data,
                },
            },
            upsert=True,
        )
        return result.acknowledged

    async def delete_link_document(self, base64_file_link: str) -> bool:
        """
        Deletes a link document from the database.

        Parameters:
            base64_file_link (str): The base64-encoded link to the file.

        Returns:
            bool: Whether the document was deleted successfully.
        """
        collection = self.db["Files"]
        result = await collection.delete_one(
            filter={"_id": base64_file_link},
        )
        return result.deleted_count > 0

    async def get_link_document(self, base64_file_link: str) -> dict | None:
        """
        Retrieves a link document from the database.

        Parameters:
            base64_file_link (str): The base64-encoded link to the file.

        Returns:
            dict | None: The document associated with the link, or None if not found.
        """
        collection = self.db["Files"]
        pipeline = [{"$match": {"_id": base64_file_link}}]

        result = await collection.aggregate(pipeline).to_list(length=None)
        return result[0] if result else None

    async def get_user_ids(self) -> tuple[list[int], list[int]]:
        """
        Retrieves the IDs of all users in the database.

        Returns:
            tuple[list[int], list[int]]: A tuple containing two lists of user IDs.
        """
        pipeline = [
            {"$project": {"_id": 1}},
            {"$group": {"_id": None, "user_ids": {"$addToSet": "$_id"}}},
            {"$project": {"_id": 0, "user_ids": 1}},
        ]

        users_collection = self.db["Users"]
        users_codex_collection = self.db["users"]

        user_ids_cursor = users_collection.aggregate(pipeline)
        user_ids = await user_ids_cursor.to_list(length=None)

        user_ids_codex_cursor = users_codex_collection.aggregate(pipeline)
        user_ids_codex = await user_ids_codex_cursor.to_list(length=None)

        main_ids = user_ids[0]["user_ids"] if user_ids else []
        codex_ids = user_ids_codex[0]["user_ids"] if user_ids_codex else []

        return (main_ids, codex_ids)

    async def stats(self) -> tuple[int, int]:
        """
        Retrieves the number of links and users in the database.

        Returns:
            tuple[int, int]: A tuple containing the number of links and users.
        """
        link_count = await self.db["Files"].count_documents({})
        users_count = await self.db["Users"].count_documents({})
        return (link_count, users_count)

    async def cleanup_users(self, unsuccessful_ids: list, unsuccessful_ids_codex: list) -> None:
        """
        Cleans up users from the database based on their IDs.

        Parameters:
            unsuccessful_ids (list): List of user IDs to delete from the database.
            unsuccessful_ids_codex (list): List of user IDs to delete from the CodeXbotz database.
        """
        if unsuccessful_ids:
            await self.db["Users"].delete_many({"_id": {"$in": unsuccessful_ids}})

        if unsuccessful_ids_codex:
            await self.db["users"].delete_many({"_id": {"$in": unsuccessful_ids_codex}})

    # Group Management Methods
    def new_group(self, id, title):
        """
        Creates a new group document.

        Parameters:
            id: Group ID
            title: Group title

        Returns:
            dict: New group document
        """
        return dict(
            id = id,
            title = title,
            chat_status=dict(
                is_disabled=False,
                reason=""
            ),
            invite_link=dict(
                link="",
                timestamp=0
            )
        )

    async def update_link(self, chat_id: int, link: str):
        """Update the invite link for a given chat in the database."""
        await self.grp.update_one(
            {"id": chat_id},
            {
                "$set": {
                    "invite_link.link": link,
                    "invite_link.timestamp": time.time()
                }
            },
            upsert=True
        )

    # async def get_cached_link(self, chat_id: int):
    #     """Retrieve a valid cached invite link if it exists."""
    #     chat = await self.grp.find_one({"id": chat_id}, {"invite_link": 1})
    #     if chat and chat.get("invite_link"):
    #         link_data = chat["invite_link"]
    #         if time.time() - link_data.get("timestamp", 0) < config.CACHE_DURATION:
    #             return link_data.get("link")
    #     return None
    
    async def add_chat(self, chat, title):
        """Add a new chat/group to database"""
        chat = self.new_group(chat, title)
        await self.grp.insert_one(chat)

    async def get_chat(self, chat):
        """Get chat status from database"""
        chat = await self.grp.find_one({'id':int(chat)})
        return False if not chat else chat.get('chat_status')
    
    async def delete_chat(self, chat_id):
        """Delete a chat from database"""
        await self.grp.delete_many({'id': int(chat_id)})
    
    async def total_chat_count(self):
        """Get total number of chats in database"""
        count = await self.grp.count_documents({})
        return count
    
    async def get_all_chats(self):
        """Get all chats from database"""
        return self.grp.find({})

    async def get_db_size(self):
        """Get total database size"""
        return (await self.db.command("dbstats"))['dataSize']

    # Admin Management Methods
    def new_admin(self, admin_id):
        """
        Creates a new admin document.

        Parameters:
            admin_id: Admin user ID

        Returns:
            dict: New admin document
        """
        return {
            "id": int(admin_id),
            "added_on": datetime.datetime.now()
        }

    async def add_admin(self, admin_id):
        """Add a new admin to database"""
        admin_id = int(admin_id)
        if not await self.is_admin(admin_id):
            await self.admins.insert_one(self.new_admin(admin_id))
            return True
        return False

    async def remove_admin(self, admin_id):
        """Remove an admin from database"""
        admin_id = int(admin_id)
        result = await self.admins.delete_one({"id": admin_id})
        return result.deleted_count > 0

    async def is_admin(self, admin_id):
        """Check if user is admin"""
        admin_id = int(admin_id)
        if admin_id in config.ROOT_ADMINS_ID:
            return True
        admin = await self.admins.find_one({"id": admin_id})
        return bool(admin)

    async def get_all_admins(self):
        """Get all admins from database"""
        db_admins = [admin['id'] async for admin in self.admins.find({})]
        all_admins = list(set(db_admins + config.ROOT_ADMINS_ID))
        return all_admins