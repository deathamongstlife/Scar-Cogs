import discord
from discord.ext import tasks
from redbot.core import commands, Config, modlog, checks
from redbot.core.utils.chat_formatting import box, humanize_timedelta
from redbot.core.utils.predicates import MessagePredicate
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS
from redbot.core.bot import Red
from datetime import datetime, timedelta
import asyncio
import logging
import uuid
from typing import Optional, Dict, List, Union
from abc import ABC, abstractmethod
import json

log = logging.getLogger("red.cog.modmail")

class ModmailExtension(ABC):
    """Interface for modmail extensions/plugins"""
    
    @abstractmethod
    async def on_thread_created(self, thread_data: dict):
        """Called when new thread is created"""
        pass
        
    @abstractmethod
    async def on_message_processed(self, message_data: dict):
        """Called after message processing"""
        pass
        
    @abstractmethod
    async def on_thread_closed(self, thread_data: dict, reason: str):
        """Called when thread is closed"""
        pass

class ModMail(commands.Cog):
    """
    Advanced Modmail System with Plugin Support
    
    A comprehensive modmail solution for Red-DiscordBot featuring:
    - DM to staff channel forwarding
    - Threaded conversations
    - Staff collaboration tools
    - User blocking and rate limiting
    - Snippet responses
    - Thread categorization and priority
    - Comprehensive logging
    - Plugin/extension system
    - Multi-server support
    """
    
    __version__ = "2.0.0"
    __author__ = "Advanced Modmail Team"
    
    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1234567890123456, force_registration=True)
        
        # Extension system
        self.extensions: Dict[str, ModmailExtension] = {}
        self.hooks = {
            "thread_created": [],
            "message_processed": [], 
            "thread_closed": [],
            "user_blocked": [],
            "snippet_used": []
        }
        
        # Background tasks
        self.background_tasks = []
        self._cleanup_lock = asyncio.Lock()
        
        # Rate limiting
        self.rate_limits = {}
        
        # Initialize config structure
        self._init_config()
        
    def _init_config(self):
        """Initialize comprehensive configuration structure"""
        
        # Guild-specific settings
        default_guild = {
            "enabled": False,
            "category_id": None,
            "log_channel_id": None,
            "staff_roles": [],
            "blocked_users": [],
            "auto_response": {
                "enabled": True,
                "message": "Thank you for contacting us! A staff member will be with you shortly.",
                "embed": {
                    "enabled": False,
                    "title": "Modmail Received",
                    "color": 0x3498db,
                    "footer": "Response time: Usually within 24 hours"
                }
            },
            "thread_settings": {
                "auto_close_after": 7200,  # 2 hours in seconds
                "require_close_reason": True,
                "notify_user_on_close": True,
                "delete_on_close": False,
                "close_confirmation": True
            },
            "user_requirements": {
                "min_account_age": 86400,  # 1 day in seconds
                "min_server_age": 0,
                "require_server_member": False,
                "blocked_new_accounts": False
            },
            "rate_limiting": {
                "enabled": True,
                "max_messages": 5,
                "time_window": 300,  # 5 minutes
                "cooldown_message": "You're sending messages too quickly. Please wait before sending another message."
            },
            "snippets": {},
            "thread_priorities": ["low", "normal", "high", "urgent"],
            "categories": ["general", "technical", "billing", "moderation", "other"],
            "anonymous_staff": False,
            "show_user_info": True
        }
        
        # User-specific data (cross-server)
        default_user = {
            "blocked": False,
            "block_reason": None,
            "blocked_at": None,
            "blocked_by": None,
            "total_threads": 0,
            "last_thread_at": None,
            "notes": []
        }
        
        # Thread data (per-guild)
        default_thread = {
            "user_id": None,
            "channel_id": None,
            "guild_id": None,
            "created_at": None,
            "closed_at": None,
            "status": "open",  # open, closed, archived
            "priority": "normal",
            "category": "general",
            "staff_assigned": [],
            "participants": [],
            "message_count": 0,
            "close_reason": None,
            "closed_by": None,
            "escalated": False,
            "escalated_by": None,
            "escalated_at": None,
            "escalation_reason": None,
            "tags": [],
            "notes": [],
            "metadata": {}
        }
        
        # Global settings
        default_global = {
            "globally_blocked_users": {},
            "total_threads_created": 0,
            "extensions_enabled": [],
            "migration_version": "2.0.0"
        }
        
        self.config.register_guild(**default_guild)
        self.config.register_user(**default_user)
        self.config.register_global(**default_global)
        
        # Custom groups for complex relationships
        self.config.init_custom("Thread", 2)  # (guild_id, thread_id)
        self.config.init_custom("ThreadMessages", 3)  # (guild_id, thread_id, page)
        self.config.init_custom("UserConversations", 2)  # (guild_id, user_id)
        
        self.config.register_custom("Thread", **default_thread)
        self.config.register_custom("ThreadMessages", messages=[], page_size=50, created_at=None)
        self.config.register_custom("UserConversations", thread_history=[], active_thread=None)
        
    async def cog_load(self):
        """Initialize async resources and background tasks"""
        log.info("Loading Advanced Modmail System v%s", self.__version__)
        
        # Register modlog case types
        await self._register_modlog_cases()
        
        # Start background tasks
        self.cleanup_task.start()
        self.rate_limit_cleanup.start()
        
        # Load extensions
        await self._load_extensions()
        
        log.info("Advanced Modmail System loaded successfully")
        
    async def cog_unload(self):
        """Clean shutdown of resources"""
        log.info("Unloading Advanced Modmail System")
        
        async with self._cleanup_lock:
            # Cancel background tasks
            self.cleanup_task.cancel()
            self.rate_limit_cleanup.cancel()
            
            for task in self.background_tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                    
        log.info("Advanced Modmail System unloaded")
        
    async def _register_modlog_cases(self):
        """Register custom modlog case types"""
        cases = [
            {
                "name": "modmail_thread_created",
                "default_setting": True,
                "image": "📧",
                "case_str": "Modmail Thread Created"
            },
            {
                "name": "modmail_thread_closed", 
                "default_setting": True,
                "image": "🔒",
                "case_str": "Modmail Thread Closed"
            },
            {
                "name": "modmail_user_blocked",
                "default_setting": True,
                "image": "🚫", 
                "case_str": "User Blocked from Modmail"
            },
            {
                "name": "modmail_escalated",
                "default_setting": True,
                "image": "⚠️",
                "case_str": "Modmail Thread Escalated"
            }
        ]
        
        for case_type in cases:
            try:
                await modlog.register_casetype(**case_type)
            except RuntimeError:
                pass  # Case type already registered
                
    async def _load_extensions(self):
        """Load registered extensions"""
        enabled_extensions = await self.config.extensions_enabled()
        for ext_name in enabled_extensions:
            try:
                # Extensions would be loaded here if they exist
                log.info(f"Extension {ext_name} registered")
            except Exception as e:
                log.error(f"Failed to load extension {ext_name}: {e}")
                
    # Extension/Plugin System
    def register_extension(self, name: str, extension: ModmailExtension):
        """Register a modmail extension"""
        self.extensions[name] = extension
        
        # Auto-register hooks
        for hook_name in self.hooks:
            method_name = f"on_{hook_name}"
            if hasattr(extension, method_name):
                self.hooks[hook_name].append(getattr(extension, method_name))
                
        log.info(f"Registered modmail extension: {name}")
        
    def unregister_extension(self, name: str):
        """Unregister a modmail extension"""
        if name in self.extensions:
            extension = self.extensions.pop(name)
            
            # Remove hooks
            for hook_name in self.hooks:
                method_name = f"on_{hook_name}"
                if hasattr(extension, method_name):
                    hook_method = getattr(extension, method_name)
                    if hook_method in self.hooks[hook_name]:
                        self.hooks[hook_name].remove(hook_method)
                        
            log.info(f"Unregistered modmail extension: {name}")
            
    async def _trigger_hook(self, hook_name: str, *args, **kwargs):
        """Trigger extension hooks"""
        for hook in self.hooks.get(hook_name, []):
            try:
                await hook(*args, **kwargs)
            except Exception as e:
                log.error(f"Error in extension hook {hook_name}: {e}")
                
    # Rate Limiting System
    async def _is_rate_limited(self, user_id: int, guild_id: int) -> bool:
        """Check if user is rate limited"""
        guild_config = await self.config.guild_from_id(guild_id).all()
        rate_config = guild_config.get("rate_limiting", {})
        
        if not rate_config.get("enabled", True):
            return False
            
        key = f"{guild_id}:{user_id}"
        now = datetime.utcnow()
        
        if key not in self.rate_limits:
            self.rate_limits[key] = []
            
        # Clean old entries
        time_window = rate_config.get("time_window", 300)
        cutoff = now - timedelta(seconds=time_window)
        self.rate_limits[key] = [timestamp for timestamp in self.rate_limits[key] if timestamp > cutoff]
        
        # Check rate limit
        max_messages = rate_config.get("max_messages", 5)
        if len(self.rate_limits[key]) >= max_messages:
            return True
            
        # Add current timestamp
        self.rate_limits[key].append(now)
        return False
        
    @tasks.loop(minutes=5)
    async def rate_limit_cleanup(self):
        """Clean up old rate limit entries"""
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=10)
        
        keys_to_remove = []
        for key, timestamps in self.rate_limits.items():
            # Filter out old timestamps
            self.rate_limits[key] = [ts for ts in timestamps if ts > cutoff]
            
            # Remove empty entries
            if not self.rate_limits[key]:
                keys_to_remove.append(key)
                
        for key in keys_to_remove:
            del self.rate_limits[key]
            
    # Message Processing
    @commands.Cog.listener()
    async def on_message_without_command(self, message):
        """Process potential modmail messages"""
        # Only process DMs from non-bots
        if not isinstance(message.channel, discord.DMChannel):
            return
            
        if message.author.bot:
            return
            
        # Find all guilds where modmail is enabled and user has access
        eligible_guilds = []
        
        for guild in self.bot.guilds:
            config = await self.config.guild(guild).all()
            if not config.get("enabled", False):
                continue
                
            # Check if user meets requirements
            if await self._check_user_requirements(message.author, guild, config):
                eligible_guilds.append(guild)
                
        if not eligible_guilds:
            return
            
        # If multiple guilds, let user choose (for now, use first eligible)
        target_guild = eligible_guilds[0]
        
        # Check blocks and rate limits
        if await self._is_user_blocked(message.author.id, target_guild.id):
            return
            
        if await self._is_rate_limited(message.author.id, target_guild.id):
            rate_config = await self.config.guild(target_guild).rate_limiting()
            await message.author.send(rate_config.get("cooldown_message", "You're sending messages too quickly."))
            return
            
        # Process the modmail
        try:
            await self._process_modmail_message(message, target_guild)
        except Exception as e:
            log.exception(f"Error processing modmail from {message.author.id}: {e}")
            await message.author.send("An error occurred while processing your message. Please try again later.")
            
    async def _check_user_requirements(self, user: discord.User, guild: discord.Guild, config: dict) -> bool:
        """Check if user meets requirements to use modmail"""
        requirements = config.get("user_requirements", {})
        
        # Check account age
        min_age = requirements.get("min_account_age", 0)
        if min_age > 0:
            account_age = (discord.utils.utcnow() - user.created_at).total_seconds()
            if account_age < min_age:
                return False
                
        # Check server membership if required
        if requirements.get("require_server_member", False):
            member = guild.get_member(user.id)
            if not member:
                return False
                
            # Check server join age
            min_server_age = requirements.get("min_server_age", 0)
            if min_server_age > 0:
                join_age = (discord.utils.utcnow() - member.joined_at).total_seconds()
                if join_age < min_server_age:
                    return False
                    
        return True
        
    async def _is_user_blocked(self, user_id: int, guild_id: int = None) -> bool:
        """Check if user is blocked from modmail"""
        # Check global blocks
        global_blocks = await self.config.globally_blocked_users()
        if str(user_id) in global_blocks:
            return True
            
        # Check user-specific block
        user_data = await self.config.user_from_id(user_id).all()
        if user_data.get("blocked", False):
            return True
            
        # Check guild-specific blocks
        if guild_id:
            guild_blocks = await self.config.guild_from_id(guild_id).blocked_users()
            if user_id in guild_blocks:
                return True
                
        return False
        
    async def _process_modmail_message(self, message: discord.Message, guild: discord.Guild):
        """Process incoming modmail message"""
        # Get or create thread
        thread_channel = await self._get_or_create_thread(message.author, guild)
        if not thread_channel:
            await message.author.send("Unable to create modmail thread. Please contact an administrator.")
            return
            
        # Get thread data
        thread_id = self._get_thread_id(message.author.id, guild.id)
        thread_data = await self.config.custom("Thread", guild.id, thread_id).all()
        
        # Create user info embed
        user_embed = await self._create_user_info_embed(message.author, guild)
        
        # Forward message to thread
        await self._forward_message_to_thread(message, thread_channel, user_embed)
        
        # Update thread data
        await self._update_thread_data(thread_id, guild.id, {
            "message_count": thread_data["message_count"] + 1
        })
        
        # Send auto-response if this is the first message
        if thread_data["message_count"] == 0:
            await self._send_auto_response(message.author, guild)
            
        # Trigger extension hooks
        message_data = {
            "thread_id": thread_id,
            "user_id": message.author.id,
            "guild_id": guild.id,
            "content": message.content,
            "attachments": [att.url for att in message.attachments],
            "timestamp": message.created_at
        }
        await self._trigger_hook("message_processed", message_data)
        
        # Log to modlog if first message in thread
        if thread_data["message_count"] == 0:
            await self._log_thread_created(message.author, guild, thread_channel)
            
    async def _get_or_create_thread(self, user: discord.User, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Get existing thread or create new one"""
        thread_id = self._get_thread_id(user.id, guild.id)
        
        # Check for existing active thread
        existing_thread = await self.config.custom("UserConversations", guild.id, user.id).active_thread()
        if existing_thread:
            channel = guild.get_channel(existing_thread)
            if channel:
                return channel
                
        # Create new thread
        return await self._create_new_thread(user, guild, thread_id)
        
    def _get_thread_id(self, user_id: int, guild_id: int) -> str:
        """Generate unique thread ID"""
        return f"{user_id}-{guild_id}-{uuid.uuid4().hex[:8]}"
        
    async def _create_new_thread(self, user: discord.User, guild: discord.Guild, thread_id: str) -> Optional[discord.TextChannel]:
        """Create a new modmail thread channel"""
        config = await self.config.guild(guild).all()
        category_id = config.get("category_id")
        
        if not category_id:
            log.error(f"No modmail category set for guild {guild.id}")
            return None
            
        category = guild.get_channel(category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            log.error(f"Invalid modmail category for guild {guild.id}")
            return None
            
        # Create channel
        channel_name = f"modmail-{user.name}-{user.discriminator}"
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True)
            }
            
            # Add staff role permissions
            for role_id in config.get("staff_roles", []):
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                    
            channel = await category.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                topic=f"Modmail thread for {user} (ID: {user.id})"
            )
            
            # Initialize thread data
            thread_data = {
                "user_id": user.id,
                "channel_id": channel.id,
                "guild_id": guild.id,
                "created_at": datetime.utcnow().isoformat(),
                "status": "open",
                "participants": [user.id]
            }
            
            await self.config.custom("Thread", guild.id, thread_id).set(thread_data)
            await self.config.custom("UserConversations", guild.id, user.id).active_thread.set(channel.id)
            
            # Update global counter
            total = await self.config.total_threads_created()
            await self.config.total_threads_created.set(total + 1)
            
            # Trigger extension hook
            await self._trigger_hook("thread_created", thread_data)
            
            return channel
            
        except discord.Forbidden:
            log.error(f"No permission to create modmail channel in guild {guild.id}")
            return None
        except Exception as e:
            log.exception(f"Error creating modmail channel: {e}")
            return None
            
    async def _create_user_info_embed(self, user: discord.User, guild: discord.Guild) -> discord.Embed:
        """Create embed with user information"""
        embed = discord.Embed(title="User Information", color=0x3498db)
        embed.set_thumbnail(url=user.display_avatar.url)
        
        # Basic info
        embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
        embed.add_field(name="Account Created", value=f"<t:{int(user.created_at.timestamp())}:R>", inline=True)
        
        # Server member info
        member = guild.get_member(user.id)
        if member:
            embed.add_field(name="Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:R>", inline=True)
            if member.roles[1:]:  # Exclude @everyone
                roles = ", ".join([role.mention for role in member.roles[1:][:5]])
                if len(member.roles) > 6:
                    roles += f" (+{len(member.roles) - 6} more)"
                embed.add_field(name="Roles", value=roles, inline=False)
        else:
            embed.add_field(name="Server Member", value="No", inline=True)
            
        # Modmail history
        user_data = await self.config.user(user).all()
        embed.add_field(name="Previous Threads", value=str(user_data.get("total_threads", 0)), inline=True)
        
        if user_data.get("last_thread_at"):
            last_thread = datetime.fromisoformat(user_data["last_thread_at"])
            embed.add_field(name="Last Thread", value=f"<t:{int(last_thread.timestamp())}:R>", inline=True)
            
        return embed
        
    async def _forward_message_to_thread(self, message: discord.Message, thread_channel: discord.TextChannel, user_embed: discord.Embed = None):
        """Forward user message to thread channel"""
        # Send user info embed for new threads
        if user_embed:
            await thread_channel.send(embed=user_embed)
            
        # Create message embed
        embed = discord.Embed(description=message.content, color=0x3498db, timestamp=message.created_at)
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        
        if message.attachments:
            if len(message.attachments) == 1 and message.attachments[0].url.lower().endswith(('png', 'jpg', 'jpeg', 'gif', 'webp')):
                embed.set_image(url=message.attachments[0].url)
            else:
                attachment_list = "\n".join([f"[{att.filename}]({att.url})" for att in message.attachments])
                embed.add_field(name="Attachments", value=attachment_list, inline=False)
                
        await thread_channel.send(embed=embed)
        
        # Forward attachments if they're not images
        for attachment in message.attachments:
            if not attachment.url.lower().endswith(('png', 'jpg', 'jpeg', 'gif', 'webp')):
                try:
                    file = await attachment.to_file()
                    await thread_channel.send(file=file)
                except discord.HTTPException:
                    pass  # File too large or other error
                    
    async def _send_auto_response(self, user: discord.User, guild: discord.Guild):
        """Send automatic response to user"""
        config = await self.config.guild(guild).auto_response()
        
        if not config.get("enabled", True):
            return
            
        message = config.get("message", "Thank you for contacting us!")
        
        if config.get("embed", {}).get("enabled", False):
            embed_config = config["embed"]
            embed = discord.Embed(
                title=embed_config.get("title", "Modmail Received"),
                description=message,
                color=embed_config.get("color", 0x3498db)
            )
            
            footer = embed_config.get("footer")
            if footer:
                embed.set_footer(text=footer)
                
            await user.send(embed=embed)
        else:
            await user.send(message)
            
    async def _update_thread_data(self, thread_id: str, guild_id: int, updates: dict):
        """Update thread data"""
        async with self.config.custom("Thread", guild_id, thread_id).all() as thread_data:
            thread_data.update(updates)
            
    async def _log_thread_created(self, user: discord.User, guild: discord.Guild, channel: discord.TextChannel):
        """Log thread creation to modlog"""
        try:
            await modlog.create_case(
                self.bot,
                guild,
                datetime.utcnow(),
                action_type="modmail_thread_created",
                user=user,
                reason=f"Modmail thread created in {channel.mention}"
            )
        except Exception as e:
            log.error(f"Failed to log thread creation: {e}")
            
    # Staff Commands
    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    async def modmail(self, ctx):
        """Modmail system commands"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)
            
    @modmail.command(name="setup")
    @checks.admin_or_permissions(administrator=True)
    async def modmail_setup(self, ctx):
        """Interactive modmail setup"""
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
            
        embed = discord.Embed(title="Modmail Setup", description="Let's set up modmail for your server!", color=0x3498db)
        await ctx.send(embed=embed)
        
        # Category selection
        await ctx.send("First, please mention or provide the ID of the category where modmail threads should be created:")
        
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60)
            category = None
            
            # Try to parse category
            if msg.channel_mentions:
                category = msg.channel_mentions[0]
                if not isinstance(category, discord.CategoryChannel):
                    category = None
            elif msg.content.isdigit():
                category = ctx.guild.get_channel(int(msg.content))
                if not isinstance(category, discord.CategoryChannel):
                    category = None
                    
            if not category:
                await ctx.send("Invalid category. Setup cancelled.")
                return
                
            await self.config.guild(ctx.guild).category_id.set(category.id)
            await ctx.send(f"✅ Modmail category set to: {category.name}")
            
        except asyncio.TimeoutError:
            await ctx.send("Setup timed out.")
            return
            
        # Staff roles
        await ctx.send("Next, please mention the roles that should have access to modmail threads (separate multiple roles with spaces):")
        
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60)
            staff_roles = []
            
            if msg.role_mentions:
                staff_roles = [role.id for role in msg.role_mentions]
            elif msg.content.lower() in ["skip", "none"]:
                pass
            else:
                await ctx.send("No valid roles found. You can set staff roles later with `modmail settings staff`.")
                
            await self.config.guild(ctx.guild).staff_roles.set(staff_roles)
            if staff_roles:
                role_names = [ctx.guild.get_role(role_id).name for role_id in staff_roles]
                await ctx.send(f"✅ Staff roles set to: {', '.join(role_names)}")
            else:
                await ctx.send("✅ No staff roles set. Only administrators will have access.")
                
        except asyncio.TimeoutError:
            await ctx.send("Setup timed out, but modmail category was saved.")
            
        # Enable modmail
        await self.config.guild(ctx.guild).enabled.set(True)
        
        embed = discord.Embed(
            title="✅ Modmail Setup Complete!",
            description=f"Modmail is now enabled for {ctx.guild.name}.\n\nUsers can now send DMs to the bot to create modmail threads.",
            color=0x00ff00
        )
        embed.add_field(name="Next Steps", value="• Configure auto-responses with `modmail settings autoresponse`\n• Add snippets with `modmail snippet add`\n• Set up additional settings with `modmail settings`", inline=False)
        await ctx.send(embed=embed)
        
    @modmail.group(name="settings", invoke_without_command=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def modmail_settings(self, ctx):
        """View and modify modmail settings"""
        if ctx.invoked_subcommand is None:
            await self._show_settings(ctx)
            
    async def _show_settings(self, ctx):
        """Show current modmail settings"""
        config = await self.config.guild(ctx.guild).all()
        
        embed = discord.Embed(title=f"Modmail Settings - {ctx.guild.name}", color=0x3498db)
        
        # Basic settings
        embed.add_field(name="Status", value="✅ Enabled" if config["enabled"] else "❌ Disabled", inline=True)
        
        category = ctx.guild.get_channel(config["category_id"]) if config["category_id"] else None
        embed.add_field(name="Category", value=category.name if category else "Not set", inline=True)
        
        staff_roles = [ctx.guild.get_role(r_id).name for r_id in config["staff_roles"] if ctx.guild.get_role(r_id)]
        embed.add_field(name="Staff Roles", value=", ".join(staff_roles) if staff_roles else "None", inline=True)
        
        # Auto-response
        auto_resp = config["auto_response"]
        embed.add_field(name="Auto Response", value="✅ Enabled" if auto_resp["enabled"] else "❌ Disabled", inline=True)
        
        # Thread settings
        thread_settings = config["thread_settings"]
        auto_close = thread_settings["auto_close_after"]
        if auto_close > 0:
            close_time = humanize_timedelta(seconds=auto_close)
            embed.add_field(name="Auto Close", value=f"After {close_time}", inline=True)
        else:
            embed.add_field(name="Auto Close", value="Disabled", inline=True)
            
        # User requirements
        requirements = config["user_requirements"]
        req_list = []
        if requirements["min_account_age"]:
            req_list.append(f"Account age: {humanize_timedelta(seconds=requirements['min_account_age'])}")
        if requirements["require_server_member"]:
            req_list.append("Must be server member")
        if requirements["min_server_age"]:
            req_list.append(f"Server age: {humanize_timedelta(seconds=requirements['min_server_age'])}")
            
        embed.add_field(name="User Requirements", value="\n".join(req_list) if req_list else "None", inline=False)
        
        await ctx.send(embed=embed)
        
    @modmail_settings.command(name="enable")
    @checks.admin_or_permissions(administrator=True)
    async def settings_enable(self, ctx):
        """Enable modmail system"""
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send("✅ Modmail system enabled.")
        
    @modmail_settings.command(name="disable")
    @checks.admin_or_permissions(administrator=True)
    async def settings_disable(self, ctx):
        """Disable modmail system"""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("❌ Modmail system disabled.")
        
    @modmail_settings.command(name="category")
    @checks.admin_or_permissions(administrator=True)
    async def settings_category(self, ctx, category: discord.CategoryChannel):
        """Set the category for modmail threads"""
        await self.config.guild(ctx.guild).category_id.set(category.id)
        await ctx.send(f"✅ Modmail category set to: {category.name}")
        
    @modmail_settings.command(name="staff")
    @checks.admin_or_permissions(administrator=True)
    async def settings_staff(self, ctx, *roles: discord.Role):
        """Set staff roles that can access modmail"""
        role_ids = [role.id for role in roles]
        await self.config.guild(ctx.guild).staff_roles.set(role_ids)
        
        if roles:
            role_names = [role.name for role in roles]
            await ctx.send(f"✅ Staff roles set to: {', '.join(role_names)}")
        else:
            await ctx.send("✅ Staff roles cleared.")
            
    @modmail_settings.command(name="autoclose")
    @checks.mod_or_permissions(manage_messages=True)
    async def settings_autoclose(self, ctx, time: int):
        """Set auto-close time in seconds (0 to disable)"""
        await self.config.guild(ctx.guild).thread_settings.auto_close_after.set(time)
        
        if time > 0:
            time_str = humanize_timedelta(seconds=time)
            await ctx.send(f"✅ Threads will auto-close after {time_str} of inactivity.")
        else:
            await ctx.send("✅ Auto-close disabled.")
            
    # Thread Management Commands
    @modmail.command(name="close")
    @checks.mod_or_permissions(manage_messages=True)
    async def modmail_close(self, ctx, *, reason: str = None):
        """Close the current modmail thread"""
        if not await self._is_modmail_channel(ctx.channel):
            await ctx.send("This command can only be used in modmail threads.")
            return
            
        # Get thread data
        thread_data = await self._get_thread_data_from_channel(ctx.channel)
        if not thread_data:
            await ctx.send("Could not find thread data.")
            return
            
        config = await self.config.guild(ctx.guild).thread_settings()
        
        # Require reason if configured
        if config.get("require_close_reason", True) and not reason:
            await ctx.send("Please provide a reason for closing this thread.")
            return
            
        # Confirmation if configured
        if config.get("close_confirmation", True):
            embed = discord.Embed(
                title="Close Thread?",
                description=f"Are you sure you want to close this modmail thread?\n\n**Reason:** {reason or 'No reason provided'}",
                color=0xff9900
            )
            
            msg = await ctx.send(embed=embed)
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
            
            def reaction_check(reaction, user):
                return user == ctx.author and str(reaction.emoji) in ["✅", "❌"] and reaction.message.id == msg.id
                
            try:
                reaction, user = await self.bot.wait_for("reaction_add", check=reaction_check, timeout=30)
                
                if str(reaction.emoji) == "❌":
                    await ctx.send("Thread close cancelled.")
                    return
                    
            except asyncio.TimeoutError:
                await ctx.send("Thread close cancelled (timed out).")
                return
                
        # Close the thread
        await self._close_thread(ctx.channel, ctx.author, reason, thread_data)
        
    async def _is_modmail_channel(self, channel: discord.TextChannel) -> bool:
        """Check if channel is a modmail thread"""
        # Simple check - could be enhanced to check thread data
        return channel.name.startswith("modmail-")
        
    async def _get_thread_data_from_channel(self, channel: discord.TextChannel) -> Optional[dict]:
        """Get thread data from channel"""
        # Search through all threads for this channel
        all_threads = await self.config.custom("Thread", channel.guild.id).all()
        
        for thread_id, data in all_threads.items():
            if data.get("channel_id") == channel.id:
                return data
                
        return None
        
    async def _close_thread(self, channel: discord.TextChannel, closer: discord.Member, reason: str, thread_data: dict):
        """Close a modmail thread"""
        try:
            # Update thread data
            thread_id = None
            all_threads = await self.config.custom("Thread", channel.guild.id).all()
            for t_id, data in all_threads.items():
                if data.get("channel_id") == channel.id:
                    thread_id = t_id
                    break
                    
            if thread_id:
                updates = {
                    "status": "closed",
                    "closed_at": datetime.utcnow().isoformat(),
                    "close_reason": reason,
                    "closed_by": closer.id
                }
                await self._update_thread_data(thread_id, channel.guild.id, updates)
                
            # Notify user if configured
            config = await self.config.guild(channel.guild).thread_settings()
            if config.get("notify_user_on_close", True) and thread_data.get("user_id"):
                user = self.bot.get_user(thread_data["user_id"])
                if user:
                    embed = discord.Embed(
                        title="Thread Closed",
                        description=f"Your modmail thread in **{channel.guild.name}** has been closed.",
                        color=0xff6b6b
                    )
                    
                    if reason:
                        embed.add_field(name="Reason", value=reason, inline=False)
                        
                    embed.add_field(name="Closed by", value=str(closer), inline=True)
                    embed.set_footer(text="Thank you for contacting us!")
                    
                    try:
                        await user.send(embed=embed)
                    except discord.Forbidden:
                        pass  # User has DMs disabled
                        
            # Send close message in channel
            embed = discord.Embed(
                title="Thread Closed",
                description=f"This thread has been closed by {closer.mention}.",
                color=0xff6b6b,
                timestamp=datetime.utcnow()
            )
            
            if reason:
                embed.add_field(name="Reason", value=reason, inline=False)
                
            await channel.send(embed=embed)
            
            # Clear active thread for user
            if thread_data.get("user_id"):
                await self.config.custom("UserConversations", channel.guild.id, thread_data["user_id"]).active_thread.set(None)
                
            # Delete channel if configured
            if config.get("delete_on_close", False):
                await asyncio.sleep(5)  # Give time to read the message
                await channel.delete(reason=f"Modmail thread closed by {closer}")
            else:
                # Archive the channel
                await channel.edit(name=f"closed-{channel.name}")
                
                # Remove send permissions for staff
                overwrites = channel.overwrites
                for target, overwrite in overwrites.items():
                    if isinstance(target, discord.Role):
                        overwrite.send_messages = False
                        await channel.set_permissions(target, overwrite=overwrite)
                        
            # Log to modlog
            await modlog.create_case(
                self.bot,
                channel.guild,
                datetime.utcnow(),
                action_type="modmail_thread_closed",
                user=self.bot.get_user(thread_data.get("user_id")) if thread_data.get("user_id") else None,
                moderator=closer,
                reason=reason or "No reason provided"
            )
            
            # Trigger extension hooks
            thread_data["closed_by"] = closer.id
            thread_data["close_reason"] = reason
            await self._trigger_hook("thread_closed", thread_data, reason)
            
        except Exception as e:
            log.exception(f"Error closing thread: {e}")
            await channel.send("An error occurred while closing the thread.")
            
    @modmail.command(name="reply", aliases=["r"])
    @checks.mod_or_permissions(manage_messages=True)
    async def modmail_reply(self, ctx, *, message: str):
        """Reply to the user in this modmail thread"""
        if not await self._is_modmail_channel(ctx.channel):
            await ctx.send("This command can only be used in modmail threads.")
            return
            
        thread_data = await self._get_thread_data_from_channel(ctx.channel)
        if not thread_data or thread_data.get("status") != "open":
            await ctx.send("This thread is not active.")
            return
            
        user = self.bot.get_user(thread_data.get("user_id"))
        if not user:
            await ctx.send("Could not find the user for this thread.")
            return
            
        # Send reply to user
        await self._send_reply_to_user(user, ctx.author, message, ctx.guild)
        
        # Confirm in thread
        embed = discord.Embed(
            description=f"📤 Reply sent to {user.mention}",
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        await ctx.send(embed=embed)
        
        # Delete command message for cleanliness
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass
            
    async def _send_reply_to_user(self, user: discord.User, staff_member: discord.Member, message: str, guild: discord.Guild):
        """Send staff reply to user"""
        config = await self.config.guild(guild).all()
        
        embed = discord.Embed(
            description=message,
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        
        # Anonymous staff setting
        if config.get("anonymous_staff", False):
            embed.set_author(name=f"Staff - {guild.name}", icon_url=guild.icon.url if guild.icon else None)
        else:
            embed.set_author(name=f"{staff_member.display_name} - {guild.name}", icon_url=staff_member.display_avatar.url)
            
        embed.set_footer(text="You can reply to this message to continue the conversation.")
        
        try:
            await user.send(embed=embed)
        except discord.Forbidden:
            # Try sending without embed
            message_content = f"**{guild.name} Staff Response:**\n{message}\n\n*You can reply to continue the conversation.*"
            await user.send(message_content)
            
    @modmail.command(name="areply", aliases=["ar"])
    @checks.mod_or_permissions(manage_messages=True)
    async def modmail_areply(self, ctx, *, message: str):
        """Send an anonymous reply to the user"""
        if not await self._is_modmail_channel(ctx.channel):
            await ctx.send("This command can only be used in modmail threads.")
            return
            
        thread_data = await self._get_thread_data_from_channel(ctx.channel)
        if not thread_data or thread_data.get("status") != "open":
            await ctx.send("This thread is not active.")
            return
            
        user = self.bot.get_user(thread_data.get("user_id"))
        if not user:
            await ctx.send("Could not find the user for this thread.")
            return
            
        # Send anonymous reply
        embed = discord.Embed(
            description=message,
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=f"Staff - {ctx.guild.name}", icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
        embed.set_footer(text="You can reply to this message to continue the conversation.")
        
        try:
            await user.send(embed=embed)
            
            # Confirm in thread
            confirm_embed = discord.Embed(
                description=f"📤 Anonymous reply sent to {user.mention}",
                color=0x00ff00,
                timestamp=datetime.utcnow()
            )
            await ctx.send(embed=confirm_embed)
            
            # Delete command message
            try:
                await ctx.message.delete()
            except discord.Forbidden:
                pass
                
        except discord.Forbidden:
            await ctx.send(f"Could not send message to {user.mention} - they may have DMs disabled.")
            
    # Snippet System
    @modmail.group(name="snippet", invoke_without_command=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def modmail_snippet(self, ctx):
        """Snippet management commands"""
        if ctx.invoked_subcommand is None:
            snippets = await self.config.guild(ctx.guild).snippets()
            
            if not snippets:
                await ctx.send("No snippets configured for this server.")
                return
                
            embed = discord.Embed(title="Available Snippets", color=0x3498db)
            
            for name, data in snippets.items():
                usage_count = data.get("usage_count", 0)
                embed.add_field(
                    name=f"`{name}`",
                    value=f"{data['content'][:100]}{'...' if len(data['content']) > 100 else ''}\n*Used {usage_count} times*",
                    inline=False
                )
                
            await ctx.send(embed=embed)
            
    @modmail_snippet.command(name="add")
    @checks.mod_or_permissions(manage_messages=True)
    async def snippet_add(self, ctx, name: str, *, content: str):
        """Add a new snippet"""
        async with self.config.guild(ctx.guild).snippets() as snippets:
            snippets[name] = {
                "content": content,
                "created_by": ctx.author.id,
                "created_at": datetime.utcnow().isoformat(),
                "usage_count": 0
            }
            
        await ctx.send(f"✅ Snippet `{name}` added successfully.")
        
    @modmail_snippet.command(name="remove", aliases=["delete"])
    @checks.mod_or_permissions(manage_messages=True)
    async def snippet_remove(self, ctx, name: str):
        """Remove a snippet"""
        async with self.config.guild(ctx.guild).snippets() as snippets:
            if name in snippets:
                del snippets[name]
                await ctx.send(f"✅ Snippet `{name}` removed.")
            else:
                await ctx.send(f"❌ Snippet `{name}` not found.")
                
    @modmail_snippet.command(name="use")
    @checks.mod_or_permissions(manage_messages=True)
    async def snippet_use(self, ctx, name: str):
        """Use a snippet as a reply"""
        if not await self._is_modmail_channel(ctx.channel):
            await ctx.send("This command can only be used in modmail threads.")
            return
            
        snippets = await self.config.guild(ctx.guild).snippets()
        
        if name not in snippets:
            await ctx.send(f"❌ Snippet `{name}` not found.")
            return
            
        # Get snippet content
        snippet_data = snippets[name]
        content = snippet_data["content"]
        
        # Variable substitution
        thread_data = await self._get_thread_data_from_channel(ctx.channel)
        if thread_data:
            user = self.bot.get_user(thread_data.get("user_id"))
            if user:
                content = content.replace("{user}", user.mention)
                content = content.replace("{username}", user.name)
                content = content.replace("{server}", ctx.guild.name)
                content = content.replace("{staff}", ctx.author.mention)
                
        # Send as reply
        await self.modmail_reply(ctx, message=content)
        
        # Track usage
        async with self.config.guild(ctx.guild).snippets() as snippets:
            snippets[name]["usage_count"] += 1
            
        # Trigger hook
        await self._trigger_hook("snippet_used", {
            "snippet_name": name,
            "used_by": ctx.author.id,
            "guild_id": ctx.guild.id,
            "content": content
        })
        
    # User Management
    @modmail.group(name="block", invoke_without_command=True)
    @checks.mod_or_permissions(manage_messages=True)
    async def modmail_block(self, ctx, user: discord.User, *, reason: str = None):
        """Block a user from using modmail"""
        await self.config.user(user).blocked.set(True)
        await self.config.user(user).block_reason.set(reason)
        await self.config.user(user).blocked_at.set(datetime.utcnow().isoformat())
        await self.config.user(user).blocked_by.set(ctx.author.id)
        
        # Also add to guild blocklist
        async with self.config.guild(ctx.guild).blocked_users() as blocked:
            if user.id not in blocked:
                blocked.append(user.id)
                
        # Log to modlog
        await modlog.create_case(
            self.bot,
            ctx.guild,
            datetime.utcnow(),
            action_type="modmail_user_blocked",
            user=user,
            moderator=ctx.author,
            reason=reason or "No reason provided"
        )
        
        # Trigger hook
        await self._trigger_hook("user_blocked", {
            "user_id": user.id,
            "blocked_by": ctx.author.id,
            "guild_id": ctx.guild.id,
            "reason": reason
        })
        
        embed = discord.Embed(
            title="User Blocked",
            description=f"{user.mention} has been blocked from using modmail.",
            color=0xff6b6b
        )
        
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
            
        await ctx.send(embed=embed)
        
    @modmail_block.command(name="list")
    @checks.mod_or_permissions(manage_messages=True)
    async def block_list(self, ctx):
        """List blocked users"""
        blocked_users = await self.config.guild(ctx.guild).blocked_users()
        
        if not blocked_users:
            await ctx.send("No users are currently blocked.")
            return
            
        embed = discord.Embed(title="Blocked Users", color=0xff6b6b)
        
        for user_id in blocked_users[:10]:  # Limit to 10 for embed space
            user = self.bot.get_user(user_id)
            user_data = await self.config.user_from_id(user_id).all()
            
            user_str = f"{user} ({user_id})" if user else f"Unknown User ({user_id})"
            reason = user_data.get("block_reason", "No reason")
            
            embed.add_field(name=user_str, value=f"Reason: {reason}", inline=False)
            
        if len(blocked_users) > 10:
            embed.set_footer(text=f"Showing 10 of {len(blocked_users)} blocked users")
            
        await ctx.send(embed=embed)
        
    @modmail.command(name="unblock")
    @checks.mod_or_permissions(manage_messages=True)
    async def modmail_unblock(self, ctx, user: discord.User):
        """Unblock a user from modmail"""
        await self.config.user(user).blocked.set(False)
        await self.config.user(user).block_reason.clear()
        await self.config.user(user).blocked_at.clear()
        await self.config.user(user).blocked_by.clear()
        
        # Remove from guild blocklist
        async with self.config.guild(ctx.guild).blocked_users() as blocked:
            if user.id in blocked:
                blocked.remove(user.id)
                
        await ctx.send(f"✅ {user.mention} has been unblocked from modmail.")
        
    # Utility Commands
    @modmail.command(name="info")
    @checks.mod_or_permissions(manage_messages=True)
    async def modmail_info(self, ctx, user: discord.User = None):
        """Get information about a user's modmail history"""
        if not user:
            # Try to get user from current thread
            if await self._is_modmail_channel(ctx.channel):
                thread_data = await self._get_thread_data_from_channel(ctx.channel)
                if thread_data:
                    user = self.bot.get_user(thread_data.get("user_id"))
                    
        if not user:
            await ctx.send("Please specify a user or use this command in a modmail thread.")
            return
            
        user_data = await self.config.user(user).all()
        
        embed = discord.Embed(title=f"Modmail Info - {user}", color=0x3498db)
        embed.set_thumbnail(url=user.display_avatar.url)
        
        # Basic info
        embed.add_field(name="User ID", value=user.id, inline=True)
        embed.add_field(name="Account Created", value=f"<t:{int(user.created_at.timestamp())}:R>", inline=True)
        
        # Modmail stats
        embed.add_field(name="Total Threads", value=user_data.get("total_threads", 0), inline=True)
        
        if user_data.get("last_thread_at"):
            last_thread = datetime.fromisoformat(user_data["last_thread_at"])
            embed.add_field(name="Last Thread", value=f"<t:{int(last_thread.timestamp())}:R>", inline=True)
            
        # Block status
        if user_data.get("blocked", False):
            embed.add_field(name="Status", value="🚫 Blocked", inline=True)
            
            if user_data.get("block_reason"):
                embed.add_field(name="Block Reason", value=user_data["block_reason"], inline=False)
                
            if user_data.get("blocked_by"):
                blocker = self.bot.get_user(user_data["blocked_by"])
                embed.add_field(name="Blocked By", value=str(blocker) if blocker else "Unknown", inline=True)
        else:
            embed.add_field(name="Status", value="✅ Active", inline=True)
            
        # Notes
        notes = user_data.get("notes", [])
        if notes:
            note_text = "\n".join([f"• {note}" for note in notes[-3:]])  # Show last 3 notes
            if len(notes) > 3:
                note_text += f"\n*... and {len(notes) - 3} more notes*"
            embed.add_field(name="Notes", value=note_text, inline=False)
            
        await ctx.send(embed=embed)
        
    @modmail.command(name="logs")
    @checks.mod_or_permissions(manage_messages=True)
    async def modmail_logs(self, ctx, user: discord.User = None):
        """View modmail thread logs for a user"""
        if not user:
            if await self._is_modmail_channel(ctx.channel):
                thread_data = await self._get_thread_data_from_channel(ctx.channel)
                if thread_data:
                    user = self.bot.get_user(thread_data.get("user_id"))
                    
        if not user:
            await ctx.send("Please specify a user or use this command in a modmail thread.")
            return
            
        # Get user's thread history
        conversations = await self.config.custom("UserConversations", ctx.guild.id, user.id).thread_history()
        
        if not conversations:
            await ctx.send(f"{user.mention} has no modmail history in this server.")
            return
            
        embed = discord.Embed(title=f"Modmail History - {user}", color=0x3498db)
        
        for thread_id in conversations[-5:]:  # Show last 5 threads
            thread_data = await self.config.custom("Thread", ctx.guild.id, thread_id).all()
            
            if thread_data:
                created_at = datetime.fromisoformat(thread_data["created_at"])
                status = thread_data.get("status", "unknown")
                
                value = f"Status: {status.title()}\nCreated: <t:{int(created_at.timestamp())}:R>"
                
                if thread_data.get("closed_at"):
                    closed_at = datetime.fromisoformat(thread_data["closed_at"])
                    value += f"\nClosed: <t:{int(closed_at.timestamp())}:R>"
                    
                if thread_data.get("close_reason"):
                    value += f"\nReason: {thread_data['close_reason']}"
                    
                embed.add_field(name=f"Thread {thread_id[:8]}", value=value, inline=False)
                
        if len(conversations) > 5:
            embed.set_footer(text=f"Showing 5 of {len(conversations)} threads")
            
        await ctx.send(embed=embed)
        
    # Background Tasks
    @tasks.loop(hours=1)
    async def cleanup_task(self):
        """Periodic cleanup task"""
        try:
            await self._auto_close_threads()
            await self._cleanup_old_data()
        except Exception as e:
            log.exception(f"Error in cleanup task: {e}")
            
    async def _auto_close_threads(self):
        """Auto-close inactive threads"""
        for guild in self.bot.guilds:
            config = await self.config.guild(guild).all()
            
            if not config.get("enabled", False):
                continue
                
            auto_close_time = config.get("thread_settings", {}).get("auto_close_after", 0)
            if auto_close_time <= 0:
                continue
                
            # Find threads to close
            all_threads = await self.config.custom("Thread", guild.id).all()
            cutoff_time = datetime.utcnow() - timedelta(seconds=auto_close_time)
            
            for thread_id, thread_data in all_threads.items():
                if thread_data.get("status") != "open":
                    continue
                    
                # Check last activity (simplified - could track actual last message)
                created_at = datetime.fromisoformat(thread_data["created_at"])
                if created_at < cutoff_time:
                    channel = guild.get_channel(thread_data.get("channel_id"))
                    if channel:
                        # Close the thread
                        await self._close_thread(
                            channel,
                            guild.me,
                            "Auto-closed due to inactivity",
                            thread_data
                        )
                        
    async def _cleanup_old_data(self):
        """Clean up old data"""
        # This could include:
        # - Removing very old closed threads
        # - Cleaning up orphaned data
        # - Archiving old conversations
        pass
        
    @cleanup_task.before_loop
    async def before_cleanup_task(self):
        """Wait for bot to be ready before starting cleanup"""
        await self.bot.wait_until_ready()
        
    # Error Handling
    async def cog_command_error(self, ctx, error):
        """Handle command errors"""
        if isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, discord.Forbidden):
                await ctx.send("I don't have permission to perform that action.")
            elif isinstance(original, discord.NotFound):
                await ctx.send("The requested resource was not found.")
            else:
                log.exception(f"Error in modmail command {ctx.command}: {original}")
                await ctx.send("An unexpected error occurred. Please contact an administrator.")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("You don't have permission to use this command.")
        elif isinstance(error, commands.UserInputError):
            await ctx.send_help(ctx.command)
        else:
            log.exception(f"Unhandled error in modmail: {error}")

