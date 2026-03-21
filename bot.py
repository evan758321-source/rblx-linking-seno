import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import logging
import time
import asyncio
import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from datetime import datetime # t
from typing import Dict, Optional

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("roblox-linker")

LINKED_ACCOUNTS_FILE = "linked_accounts.json"
CONFIG_FILE = "config.json"
ADMIN_ROLE_NAMES = ["T Mod", "Head Mod", "Owner"]
OWNER_ID = 906812064851451915
ROBLOX_USER_URL = "https://users.roblox.com/v1/usernames/users"
ROBLOX_GAMEPASS_URL = "https://inventory.roblox.com/v1/users/{user_id}/items/GamePass/{gamepass_id}"
ROBLOX_PROFILE_URL = "https://www.roblox.com/users/{user_id}/profile"
ROBLOX_AVATAR_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=150x150&format=Png&isCircular=false"
CACHE_TTL = 300
MIN_REQUEST_INTERVAL = 1.0

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

roblox_cache: Dict[str, Dict] = {}
last_request_time: float = 0.0

try:
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
except FileNotFoundError:
    config = {"gamepass_roles": []}

def load_accounts() -> dict:
    try:
        with open(LINKED_ACCOUNTS_FILE, "r") as f:
            raw = json.load(f)
        if "discord_to_roblox" not in raw:
            d2r, r2d = {}, {}
            for did, rid in raw.items():
                d2r[did] = rid
                r2d[str(rid)] = did
            return {"discord_to_roblox": d2r, "roblox_to_discord": r2d, "force_linked_users": []}
        raw.setdefault("force_linked_users", [])
        raw.pop("generated_codes", None)
        return raw
    except FileNotFoundError:
        return {"discord_to_roblox": {}, "roblox_to_discord": {}, "force_linked_users": []}

linked_accounts = load_accounts()

def save_accounts():
    with open(LINKED_ACCOUNTS_FILE, "w") as f:
        json.dump(linked_accounts, f, indent=2)

def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id == OWNER_ID:
        return True
    member_role_names = {r.name for r in interaction.user.roles}
    return bool(member_role_names & set(ADMIN_ROLE_NAMES))

def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin(interaction):
            return True
        raise app_commands.CheckFailure("You don't have an admin role.")
    return app_commands.check(predicate)

def get_cached(key: str):
    entry = roblox_cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["val"]
    return None

def set_cached(key: str, val):
    roblox_cache[key] = {"val": val, "ts": time.time()}

async def throttle():
    global last_request_time
    elapsed = time.time() - last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)
    last_request_time = time.time()

async def fetch_roblox_user_id(username: str) -> Optional[int]:
    key = f"uid:{username.lower()}"
    cached = get_cached(key)
    if cached is not None:
        return cached
    await throttle()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ROBLOX_USER_URL,
                json={"usernames": [username], "excludeBannedUsers": False},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("data"):
                        uid = data["data"][0]["id"]
                        set_cached(key, uid)
                        return uid
                elif resp.status == 429:
                    retry = int(resp.headers.get("Retry-After", 5))
                    log.warning(f"Roblox rate limited, retrying in {retry}s")
                    await asyncio.sleep(retry)
                    return await fetch_roblox_user_id(username)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error(f"Error fetching Roblox user ID for {username}: {e}")
    return None

async def fetch_roblox_username(user_id: int) -> Optional[str]:
    key = f"uname:{user_id}"
    cached = get_cached(key)
    if cached is not None:
        return cached
    await throttle()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://users.roblox.com/v1/users/{user_id}",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    name = data.get("name")
                    if name:
                        set_cached(key, name)
                        return name
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error(f"Error fetching Roblox username for {user_id}: {e}")
    return None

async def fetch_avatar_url(user_id: int) -> Optional[str]:
    key = f"avatar:{user_id}"
    cached = get_cached(key)
    if cached is not None:
        return cached
    await throttle()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                ROBLOX_AVATAR_URL.format(user_id=user_id),
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    url = data.get("data", [{}])[0].get("imageUrl")
                    if url:
                        set_cached(key, url)
                        return url
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return None

async def check_gamepass(user_id: int, gamepass_id: int) -> bool:
    key = f"gp:{user_id}:{gamepass_id}"
    cached = get_cached(key)
    if cached is not None:
        return cached
    await throttle()
    url = ROBLOX_GAMEPASS_URL.format(user_id=user_id, gamepass_id=gamepass_id)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = bool(data.get("data", []))
                    set_cached(key, result)
                    return result
                elif resp.status == 429:
                    retry = int(resp.headers.get("Retry-After", 5))
                    await asyncio.sleep(retry)
                    return await check_gamepass(user_id, gamepass_id)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.error(f"Gamepass check error ({user_id}/{gamepass_id}): {e}")
    return False

async def remove_gamepass_roles(member: discord.Member):
    role_ids = {m["role_id"] for m in config.get("gamepass_roles", [])}
    to_remove = [r for r in member.roles if r.id in role_ids]
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Roblox account unlinked")
        except discord.Forbidden:
            log.warning(f"Missing permissions to remove roles from {member}")

def make_embed(title: str, description: str = "", color: discord.Color = discord.Color.blue()) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.timestamp = datetime.utcnow()
    return embed

@bot.tree.command(name="link-roblox", description="Link your Roblox account to your Discord account.")
@app_commands.describe(username="Your Roblox username")
async def link_roblox(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    if discord_id in linked_accounts["discord_to_roblox"]:
        current_roblox_id = linked_accounts["discord_to_roblox"][discord_id]
        current_name = await fetch_roblox_username(current_roblox_id) or str(current_roblox_id)
        embed = make_embed(
            "Already Linked",
            f"You are already linked to **{current_name}**.\nUse `/unlink-roblox` first to switch accounts.",
            discord.Color.orange()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    user_id = await fetch_roblox_user_id(username)
    if not user_id:
        embed = make_embed("User Not Found", f"No Roblox account found with the username `{username}`.", discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    roblox_id_str = str(user_id)
    if roblox_id_str in linked_accounts["roblox_to_discord"]:
        embed = make_embed("Already Claimed", "This Roblox account is already linked to another Discord user.", discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    linked_accounts["discord_to_roblox"][discord_id] = user_id
    linked_accounts["roblox_to_discord"][roblox_id_str] = discord_id
    save_accounts()

    avatar = await fetch_avatar_url(user_id)
    embed = make_embed("Account Linked", f"Successfully linked to **{username}**.", discord.Color.green())
    embed.add_field(name="Roblox ID", value=f"`{user_id}`", inline=True)
    embed.add_field(name="Profile", value=f"[View on Roblox]({ROBLOX_PROFILE_URL.format(user_id=user_id)})", inline=True)
    if avatar:
        embed.set_thumbnail(url=avatar)
    log.info(f"Linked Discord {discord_id} -> Roblox {user_id} ({username})")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="unlink-roblox", description="Unlink your Roblox account from your Discord account.")
async def unlink_roblox(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    if discord_id in linked_accounts.get("force_linked_users", []):
        embed = make_embed("Cannot Unlink", "Your account was force-linked by an admin and cannot be unlinked.", discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    if discord_id not in linked_accounts["discord_to_roblox"]:
        embed = make_embed("Not Linked", "You don't have a Roblox account linked.", discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    roblox_id = str(linked_accounts["discord_to_roblox"][discord_id])
    username = await fetch_roblox_username(int(roblox_id)) or roblox_id

    member = interaction.guild.get_member(interaction.user.id)
    if member:
        await remove_gamepass_roles(member)

    del linked_accounts["discord_to_roblox"][discord_id]
    del linked_accounts["roblox_to_discord"][roblox_id]
    save_accounts()

    embed = make_embed("Account Unlinked", f"Successfully unlinked from **{username}**.", discord.Color.green())
    log.info(f"Unlinked Discord {discord_id} from Roblox {roblox_id}")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="claim-roles", description="Claim Discord roles based on your Roblox gamepasses.")
async def claim_roles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    if discord_id not in linked_accounts["discord_to_roblox"]:
        embed = make_embed("Not Linked", "Link your Roblox account first with `/link-roblox`.", discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    roblox_id = linked_accounts["discord_to_roblox"][discord_id]
    username = await fetch_roblox_username(roblox_id) or str(roblox_id)
    added_roles, already_have, not_eligible = [], [], []

    for mapping in config.get("gamepass_roles", []):
        role = interaction.guild.get_role(mapping["role_id"])
        if not role:
            continue
        if role in interaction.user.roles:
            already_have.append(role.mention)
            continue
        if await check_gamepass(roblox_id, mapping["gamepass_id"]):
            try:
                await interaction.user.add_roles(role, reason="Roblox gamepass verified")
                added_roles.append(role.mention)
            except discord.Forbidden:
                log.warning(f"No permission to assign role {role.name}")
        else:
            not_eligible.append(mapping.get("description", role.name))

    embed = make_embed("Role Claim", color=discord.Color.green() if added_roles else discord.Color.blue())
    embed.add_field(name="Roblox Account", value=f"`{username}`", inline=False)
    if added_roles:
        embed.add_field(name="Roles Added", value="\n".join(added_roles), inline=False)
    if already_have:
        embed.add_field(name="Already Have", value="\n".join(already_have), inline=False)
    if not_eligible and not added_roles and not already_have:
        embed.description = "You don't own any eligible gamepasses."
        embed.color = discord.Color.orange()

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="profile", description="View your linked Roblox profile.")
async def profile(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    if discord_id not in linked_accounts["discord_to_roblox"]:
        embed = make_embed("Not Linked", "You don't have a Roblox account linked. Use `/link-roblox`.", discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    roblox_id = linked_accounts["discord_to_roblox"][discord_id]
    username = await fetch_roblox_username(roblox_id) or "Unknown"
    avatar = await fetch_avatar_url(roblox_id)
    force_linked = discord_id in linked_accounts.get("force_linked_users", [])

    embed = make_embed(f"{username}'s Profile", color=discord.Color.blurple())
    embed.add_field(name="Roblox Username", value=f"`{username}`", inline=True)
    embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
    embed.add_field(name="Profile", value=f"[Open]({ROBLOX_PROFILE_URL.format(user_id=roblox_id)})", inline=True)
    embed.add_field(name="Force Linked", value="Yes" if force_linked else "No", inline=True)
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    if avatar:
        embed.set_thumbnail(url=avatar)

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="whois", description="Look up a Discord user's linked Roblox account.")
@app_commands.describe(user="The Discord user to look up")
async def whois(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(user.id)

    if discord_id not in linked_accounts["discord_to_roblox"]:
        embed = make_embed("Not Linked", f"{user.mention} doesn't have a Roblox account linked.", discord.Color.orange())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    roblox_id = linked_accounts["discord_to_roblox"][discord_id]
    username = await fetch_roblox_username(roblox_id) or "Unknown"
    avatar = await fetch_avatar_url(roblox_id)

    embed = make_embed(f"Roblox Info for {user.name}", color=discord.Color.blurple())
    embed.add_field(name="Discord", value=user.mention, inline=True)
    embed.add_field(name="Roblox", value=f"`{username}`", inline=True)
    embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
    embed.add_field(name="Profile", value=f"[Open]({ROBLOX_PROFILE_URL.format(user_id=roblox_id)})", inline=True)
    if avatar:
        embed.set_thumbnail(url=avatar)

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="lookup-roblox", description="Look up a Roblox username to see if it's linked.")
@app_commands.describe(username="The Roblox username to look up")
async def lookup_roblox(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    user_id = await fetch_roblox_user_id(username)

    if not user_id:
        embed = make_embed("Not Found", f"No Roblox user found with username `{username}`.", discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    roblox_id_str = str(user_id)
    avatar = await fetch_avatar_url(user_id)
    embed = make_embed(f"Roblox: {username}", color=discord.Color.blurple())
    embed.add_field(name="Roblox ID", value=f"`{user_id}`", inline=True)
    embed.add_field(name="Profile", value=f"[Open]({ROBLOX_PROFILE_URL.format(user_id=user_id)})", inline=True)

    if roblox_id_str in linked_accounts["roblox_to_discord"]:
        linked_discord_id = linked_accounts["roblox_to_discord"][roblox_id_str]
        embed.add_field(name="Linked Discord", value=f"<@{linked_discord_id}>", inline=True)
    else:
        embed.add_field(name="Linked Discord", value="Not linked", inline=True)

    if avatar:
        embed.set_thumbnail(url=avatar)

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="list-linked", description="(Admin) List all linked accounts.")
@admin_only()
async def list_linked(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        await interaction.followup.send(embed=make_embed("No Permission", "You are not an admin.", discord.Color.red()), ephemeral=True)
        return

    accounts = linked_accounts["discord_to_roblox"]
    if not accounts:
        await interaction.followup.send(embed=make_embed("Linked Accounts", "No accounts linked yet.", discord.Color.blue()), ephemeral=True)
        return

    pages = []
    items = list(accounts.items())
    page_size = 15
    for i in range(0, len(items), page_size):
        chunk = items[i:i+page_size]
        lines = []
        for did, rid in chunk:
            force = " 🔒" if did in linked_accounts.get("force_linked_users", []) else ""
            lines.append(f"<@{did}> → `{rid}`{force}")
        pages.append("\n".join(lines))

    embed = make_embed(f"Linked Accounts ({len(accounts)} total)", pages[0], discord.Color.blue())
    if len(pages) > 1:
        embed.set_footer(text=f"Page 1/{len(pages)} — use /list-linked for full list")

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="force-link", description="(Admin) Force link a Discord user to a Roblox username.")
@app_commands.describe(discord_user="The Discord user to link", roblox_username="Their Roblox username")
@admin_only()
async def force_link(interaction: discord.Interaction, discord_user: discord.User, roblox_username: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        await interaction.followup.send(embed=make_embed("No Permission", "", discord.Color.red()), ephemeral=True)
        return

    user_id = await fetch_roblox_user_id(roblox_username)
    if not user_id:
        await interaction.followup.send(embed=make_embed("Not Found", f"No Roblox user found: `{roblox_username}`", discord.Color.red()), ephemeral=True)
        return

    discord_id = str(discord_user.id)
    roblox_id_str = str(user_id)

    old_roblox = linked_accounts["discord_to_roblox"].get(discord_id)
    if old_roblox:
        del linked_accounts["roblox_to_discord"][str(old_roblox)]

    linked_accounts["discord_to_roblox"][discord_id] = user_id
    linked_accounts["roblox_to_discord"][roblox_id_str] = discord_id
    if discord_id not in linked_accounts["force_linked_users"]:
        linked_accounts["force_linked_users"].append(discord_id)
    save_accounts()

    embed = make_embed("Force Linked", color=discord.Color.green())
    embed.add_field(name="Discord", value=discord_user.mention, inline=True)
    embed.add_field(name="Roblox", value=f"`{roblox_username}`", inline=True)
    embed.add_field(name="Roblox ID", value=f"`{user_id}`", inline=True)
    log.info(f"Admin {interaction.user.id} force-linked {discord_id} -> {user_id} ({roblox_username})")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="admin-unlink", description="(Admin) Unlink a Discord user's Roblox account.")
@app_commands.describe(discord_user="The Discord user to unlink")
@admin_only()
async def admin_unlink(interaction: discord.Interaction, discord_user: discord.User):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction):
        await interaction.followup.send(embed=make_embed("No Permission", "", discord.Color.red()), ephemeral=True)
        return

    discord_id = str(discord_user.id)
    if discord_id not in linked_accounts["discord_to_roblox"]:
        await interaction.followup.send(embed=make_embed("Not Linked", f"{discord_user.mention} is not linked.", discord.Color.red()), ephemeral=True)
        return

    roblox_id = str(linked_accounts["discord_to_roblox"][discord_id])
    username = await fetch_roblox_username(int(roblox_id)) or roblox_id

    member = interaction.guild.get_member(discord_user.id)
    if member:
        await remove_gamepass_roles(member)

    del linked_accounts["discord_to_roblox"][discord_id]
    del linked_accounts["roblox_to_discord"][roblox_id]
    linked_accounts["force_linked_users"] = [u for u in linked_accounts.get("force_linked_users", []) if u != discord_id]
    save_accounts()

    embed = make_embed("Unlinked", f"Unlinked {discord_user.mention} from **{username}**.", discord.Color.green())
    log.info(f"Admin {interaction.user.id} unlinked {discord_id} from {roblox_id}")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="clear-cache", description="(Admin) Clear the Roblox API cache.")
@admin_only()
async def clear_cache(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("No Permission", "", discord.Color.red()), ephemeral=True)
        return
    count = len(roblox_cache)
    roblox_cache.clear()
    embed = make_embed("Cache Cleared", f"Cleared {count} cached entries.", discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="stats", description="(Admin) View bot statistics.")
@admin_only()
async def stats(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(embed=make_embed("No Permission", "", discord.Color.red()), ephemeral=True)
        return
    total = len(linked_accounts["discord_to_roblox"])
    force = len(linked_accounts.get("force_linked_users", []))
    cached = len(roblox_cache)
    embed = make_embed("Bot Statistics", color=discord.Color.blurple())
    embed.add_field(name="Total Linked Accounts", value=str(total), inline=True)
    embed.add_field(name="Force Linked", value=str(force), inline=True)
    embed.add_field(name="Cache Entries", value=str(cached), inline=True)
    embed.add_field(name="Gamepass Mappings", value=str(len(config.get("gamepass_roles", []))), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tasks.loop(minutes=10)
async def prune_cache():
    now = time.time()
    expired = [k for k, v in roblox_cache.items() if now - v["ts"] > CACHE_TTL]
    for k in expired:
        del roblox_cache[k]
    if expired:
        log.info(f"Pruned {len(expired)} expired cache entries")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingRole):
        embed = make_embed("No Permission", "You don't have the required role to use this command.", discord.Color.red())
    else:
        log.error(f"Command error: {error}")
        embed = make_embed("Error", f"Something went wrong: `{error}`", discord.Color.red())
    try:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send(embed=embed, ephemeral=True)

@bot.event
async def on_ready():
    await bot.tree.sync()
    prune_cache.start()
    log.info(f"Online as {bot.user} (ID: {bot.user.id})")

async def health_check(request):
    return web.Response(
        text=json.dumps({"status": "ok", "linked": len(linked_accounts["discord_to_roblox"])}),
        content_type="application/json"
    )

async def run_webserver():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Web server on port {port}")

async def main():
    await run_webserver()
    await bot.start(os.getenv("DISCORD_TOKEN"))

if __name__ == "__main__":
    asyncio.run(main())
