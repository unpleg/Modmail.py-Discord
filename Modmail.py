# main.py
import os
import time
import asyncio
import discord
import aiosqlite
from dotenv import load_dotenv
from discord.ext import commands
from discord import ui, File
from datetime import datetime, timezone

# ---------- CHARGEMENT DE L'ENV ----------
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path=env_path)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
MODMAIL_CHANNEL_ID = int(os.getenv("MODMAIL_CHANNEL_ID"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
RATINGS_CHANNEL_ID = int(os.getenv("RATINGS_CHANNEL_ID"))

STAFF_ROLE_IDS = [int(x) for x in os.getenv("STAFF_ROLE_IDS").split(",") if x.strip()]

CATEGORY_PERMISSIONS = {}
for key, value in os.environ.items():
    if key.startswith("CATEGORY_"):
        name = key[len("CATEGORY_"):]
        ids = [int(i) for i in value.split(",") if i.strip()]
        CATEGORY_PERMISSIONS[name] = ids

DB_PATH = "modmail.db"
TRANSCRIPTS_DIR = "transcripts"

# ---------- BOT ----------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

_active_ticket_locks = {}
_seen_menu_users = set()

# ---------- UTILITAIRES ----------
def is_staff(member: discord.Member) -> bool:
    return any(r.id in STAFF_ROLE_IDS for r in member.roles)

def staff_role_name(member: discord.Member) -> str:
    roles = [r for r in member.roles if r.id in STAFF_ROLE_IDS]
    return max(roles, key=lambda r: r.position).name if roles else "Staff"

def make_embed(title: str, description: str, color=discord.Color.blurple()) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.timestamp = datetime.now(timezone.utc)
    return e

async def log_to_channel(guild: discord.Guild, title: str, description: str, color=discord.Color.dark_grey()):
    try:
        ch = await bot.fetch_channel(LOG_CHANNEL_ID)
        embed = make_embed(title, description, color)
        embed.set_footer(text=f"Serveur: {guild.name}")
        await ch.send(embed=embed)
    except:
        pass

async def send_rating_to_channel(guild: discord.Guild, staff: discord.User, rating: int, user: discord.User):
    try:
        ch = await bot.fetch_channel(RATINGS_CHANNEL_ID)
        embed = make_embed(
            "⭐ Nouvelle évaluation",
            f"**Note :** {rating}/5\n**Staff :** {staff} ({staff.id})\n**Évalué par :** {user} ({user.id})",
            color=discord.Color.gold()
        )
        await ch.send(embed=embed)
    except:
        pass

# ---------- BASE DE DONNÉES ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            thread_id INTEGER UNIQUE,
            category TEXT,
            created REAL,
            first_reply REAL,
            claimed_by INTEGER,
            closed INTEGER DEFAULT 0
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS staff_stats (
            staff_id INTEGER PRIMARY KEY,
            claimed INTEGER DEFAULT 0,
            closed INTEGER DEFAULT 0,
            rating_count INTEGER DEFAULT 0,
            rating_sum INTEGER DEFAULT 0
        )
        """)
        await db.commit()

# ---------- DB HELPERS ----------
async def create_ticket(user_id: int, thread_id: int, category: str):
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tickets (user_id, thread_id, category, created) VALUES (?, ?, ?, ?)", (user_id, thread_id, category, now))
        await db.commit()

async def get_ticket_by_thread(thread_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, claimed_by, closed, category, created, first_reply FROM tickets WHERE thread_id = ?", (thread_id,))
        return await cur.fetchone()

async def get_active_ticket_by_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT thread_id FROM tickets WHERE user_id = ? AND closed = 0", (user_id,))
        r = await cur.fetchone()
        return r[0] if r else None

async def set_claimed(thread_id: int, staff_id: int):
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tickets SET claimed_by = ? WHERE thread_id = ?", (staff_id, thread_id))
        await db.execute("UPDATE tickets SET first_reply = COALESCE(first_reply, ?) WHERE thread_id = ?", (now, thread_id))
        await db.execute("INSERT OR IGNORE INTO staff_stats (staff_id) VALUES (?)", (staff_id,))
        await db.execute("UPDATE staff_stats SET claimed = claimed + 1 WHERE staff_id = ?", (staff_id,))
        await db.commit()

async def set_closed(thread_id: int, staff_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tickets SET closed = 1 WHERE thread_id = ?", (thread_id,))
        await db.execute("INSERT OR IGNORE INTO staff_stats (staff_id) VALUES (?)", (staff_id,))
        await db.execute("UPDATE staff_stats SET closed = closed + 1 WHERE staff_id = ?", (staff_id,))
        await db.commit()

async def transfer_ticket(thread_id: int, new_staff_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tickets SET claimed_by = ? WHERE thread_id = ?", (new_staff_id, thread_id))
        await db.commit()

async def add_rating(staff_id: int, rating: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO staff_stats (staff_id) VALUES (?)", (staff_id,))
        await db.execute("UPDATE staff_stats SET rating_count = rating_count + 1, rating_sum = rating_sum + ? WHERE staff_id = ?", (rating, staff_id))
        await db.commit()

# ---------- TRANSCRIPT ----------
async def export_transcript(thread: discord.Thread) -> str | None:
    os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
    path = os.path.join(TRANSCRIPTS_DIR, f"transcript_{thread.id}.txt")
    lines = []
    try:
        async for m in thread.history(oldest_first=True, limit=None):
            content = m.content or "[PIÈCE JOINTE]"
            timestamp = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{timestamp}] {m.author} ({m.author.id}): {content}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
    except:
        return None

# ---------- VIEWS ----------
class TransferMenu(ui.View):
    def __init__(self, thread_id: int):
        super().__init__(timeout=60)
        self.thread_id = thread_id

    @ui.select(placeholder="Choisis le staff", options=[])
    async def select_callback(self, interaction: discord.Interaction, select: ui.Select):
        staff_id = int(select.values[0])
        await transfer_ticket(self.thread_id, staff_id)
        await interaction.response.send_message(f"✅ Transféré à <@{staff_id}>", ephemeral=True)
        await log_to_channel(interaction.guild, "🔁 Transfert", f"{interaction.user} → {interaction.channel.name} → <@{staff_id}>")

        ticket = await get_ticket_by_thread(self.thread_id)
        if ticket:
            user_id = ticket[0]
            try:
                user = await bot.fetch_user(user_id)
                new_staff = await bot.fetch_user(staff_id)
                await user.send(embed=make_embed("🔄 Ticket transféré", f"Transféré à **{new_staff}**."))
            except:
                pass

    async def populate_and_send(self, interaction: discord.Interaction):
        options = []
        for m in interaction.guild.members:
            if is_staff(m):
                options.append(discord.SelectOption(label=m.display_name[:99], value=str(m.id)))
        if not options:
            return await interaction.response.send_message("❌ Aucun staff trouvé.", ephemeral=True)
        self.children[0].options = options
        await interaction.response.send_message("Choisis :", view=self, ephemeral=True)

class TicketView(ui.View):
    def __init__(self, category: str):
        super().__init__(timeout=None)
        self.category = category

    @ui.button(label="✅ Claim", style=discord.ButtonStyle.primary)
    async def claim_btn(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Tu n'es pas staff.", ephemeral=True)
        allowed = CATEGORY_PERMISSIONS.get(self.category, [])
        if allowed and not any(r.id in allowed for r in interaction.user.roles):
            return await interaction.response.send_message("❌ Accès refusé pour cette catégorie.", ephemeral=True)
        await set_claimed(interaction.channel.id, interaction.user.id)
        await interaction.channel.send(embed=make_embed("📌 Pris en charge", f"{interaction.user.mention} (**{staff_role_name(interaction.user)}**) a pris ce ticket."))
        await log_to_channel(interaction.guild, "📌 Claim", f"{interaction.user} a claimé {interaction.channel.name}")
        await interaction.response.defer()

        ticket = await get_ticket_by_thread(interaction.channel.id)
        if ticket:
            user_id = ticket[0]
            try:
                user = await bot.fetch_user(user_id)
                await user.send(embed=make_embed("✅ En cours", f"**{interaction.user}** a pris en charge votre ticket (**{self.category}**)."))
            except:
                pass

    @ui.button(label="👤 Transférer", style=discord.ButtonStyle.secondary)
    async def give_btn(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Tu n'es pas staff.", ephemeral=True)
        ticket = await get_ticket_by_thread(interaction.channel.id)
        if not ticket or not ticket[1]:
            return await interaction.response.send_message("❌ Le ticket doit d'abord être claimé.", ephemeral=True)
        if interaction.user.id != ticket[1]:
            return await interaction.response.send_message("❌ Seul le propriétaire peut transférer.", ephemeral=True)
        menu = TransferMenu(interaction.channel.id)
        await menu.populate_and_send(interaction)

    @ui.button(label="🔒 Fermer", style=discord.ButtonStyle.danger)
    async def close_btn(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Tu n'es pas staff.", ephemeral=True)
        ticket = await get_ticket_by_thread(interaction.channel.id)
        if not ticket or not ticket[1]:
            return await interaction.response.send_message("❌ Le ticket doit d'abord être claimé.", ephemeral=True)
        if interaction.user.id != ticket[1]:
            return await interaction.response.send_message("❌ Seul le propriétaire peut fermer.", ephemeral=True)

        user_id, claimed_by, closed, category, created, first_reply = ticket
        path = await export_transcript(interaction.channel)
        await set_closed(interaction.channel.id, interaction.user.id)

        try:
            user = await bot.fetch_user(user_id)
            embed = make_embed("🔒 Ticket fermé", f"Votre ticket (**{category}**) a été fermé par **{interaction.user}**.")
            dm_view = ui.View(timeout=86400)

            rating = ui.Select(placeholder="⭐ Évaluez", options=[
                discord.SelectOption(label="1 - Mauvais", value="1"),
                discord.SelectOption(label="2 - Insuffisant", value="2"),
                discord.SelectOption(label="3 - Bien", value="3"),
                discord.SelectOption(label="4 - Très bien", value="4"),
                discord.SelectOption(label="5 - Excellent", value="5"),
            ])
            async def rating_cb(i: discord.Interaction):
                if i.user.id != user_id:
                    return await i.response.send_message("❌ Non autorisé.", ephemeral=True)
                r = int(i.data["values"][0])
                if claimed_by:
                    await add_rating(claimed_by, r)
                    try:
                        staff = await bot.fetch_user(claimed_by)
                        await send_rating_to_channel(i.guild, staff, r, i.user)
                    except:
                        pass
                await i.response.send_message(f"✅ Merci pour {r} ⭐", ephemeral=True)
            rating.callback = rating_cb
            dm_view.add_item(rating)

            if path and os.path.exists(path):
                with open(path, "rb") as f:
                    await user.send(embed=embed, file=File(f, filename=os.path.basename(path)), view=dm_view)
            else:
                await user.send(embed=embed, view=dm_view)
        except:
            pass

        try:
            await interaction.channel.send(embed=make_embed("🔒 Fermé", f"Fermé par {interaction.user.mention}"))
            await interaction.channel.edit(archived=True, locked=False)
            await log_to_channel(interaction.guild, "🔒 Fermé", f"{interaction.user} a fermé {interaction.channel.name}")
            await asyncio.sleep(2)
            await interaction.channel.delete()
        except:
            pass

        await interaction.response.defer()

class CategorySelectView(ui.View):
    def __init__(self, author: discord.User):
        super().__init__(timeout=300)
        self.author = author
        opts = [discord.SelectOption(label=k) for k in CATEGORY_PERMISSIONS.keys()]
        self.select = ui.Select(placeholder="Sélectionnez une catégorie", options=opts)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("❌ Non autorisé.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        category = interaction.data["values"][0]
        try:
            guild = await bot.fetch_guild(GUILD_ID)
            channel = await bot.fetch_channel(MODMAIL_CHANNEL_ID)
            thread = await channel.create_thread(name=f"{category}-{interaction.user.name}", type=discord.ChannelType.public_thread)
            await create_ticket(interaction.user.id, thread.id, category)
            await thread.send(embed=make_embed("📩 Nouveau ticket", f"**Utilisateur :** {interaction.user}\n**Catégorie :** {category}"), view=TicketView(category))

            allowed = CATEGORY_PERMISSIONS.get(category, STAFF_ROLE_IDS)
            mentions = " ".join(f"<@&{rid}>" for rid in allowed if rid in STAFF_ROLE_IDS)
            if mentions:
                await thread.send(content=mentions, allowed_mentions=discord.AllowedMentions(roles=True))

            await log_to_channel(guild, "📩 Ticket", f"{interaction.user} → {category}")
            await interaction.followup.send("✅ Ticket créé.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send("❌ Erreur.", ephemeral=True)

# ---------- EVENTS ----------
@bot.event
async def on_ready():
    await init_db()
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="vos MP"))
    print(f"✅ {bot.user} est prêt !")

    # Restaurer les tickets ouverts
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT thread_id, category FROM tickets WHERE closed = 0")
        rows = await cur.fetchall()
    for thread_id, category in rows:
        try:
            thread = await bot.fetch_channel(thread_id)
            if thread:
                await thread.send(embed=make_embed("🔄 Restauré", "Bot redémarré. Boutons réactivés."), view=TicketView(category))
        except:
            pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        user_id = message.author.id
        existing = await get_active_ticket_by_user(user_id)
        if existing:
            try:
                thread = await bot.fetch_channel(existing)
                await thread.send(embed=make_embed("📨 Message", message.content))
                return
            except:
                pass
        if user_id in _seen_menu_users:
            return
        if user_id not in _active_ticket_locks:
            _active_ticket_locks[user_id] = asyncio.Lock()
        async with _active_ticket_locks[user_id]:
            if await get_active_ticket_by_user(user_id):
                return
            _seen_menu_users.add(user_id)
            try:
                await message.author.send(embed=make_embed("📬 Support", "Choisissez une catégorie :"), view=CategorySelectView(message.author))
            except:
                pass
        if user_id in _active_ticket_locks:
            del _active_ticket_locks[user_id]
        return
    if isinstance(message.channel, discord.Thread):
        ticket = await get_ticket_by_thread(message.channel.id)
        if ticket:
            user_id = ticket[0]
            try:
                user = await bot.fetch_user(user_id)
                await user.send(embed=make_embed("💬 Réponse", f"**{message.author} ({staff_role_name(message.author)})**\n\n{message.content}"))
            except:
                await message.channel.send("⚠️ MP fermés.")
    await bot.process_commands(message)

# ---------- STATS ----------
@bot.command()
async def stats(ctx):
    if not is_staff(ctx.author):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT staff_id, claimed, closed, rating_count, rating_sum FROM staff_stats ORDER BY claimed DESC")
        rows = await cur.fetchall()
    if not rows:
        return await ctx.send("📊 Aucune donnée.")
    lines = []
    for staff_id, claimed, closed, rc, rs in rows:
        try:
            user = await bot.fetch_user(staff_id)
            name = user.name
        except:
            name = f"ID:{staff_id}"
        avg = round(rs / rc, 2) if rc > 0 else 0
        lines.append(f"**{name}** – 📥 {claimed} | 🔒 {closed} | ⭐ {avg}/5")
    await ctx.send(embed=make_embed("📊 Stats du staff", "\n".join(lines)))

# ---------- RUN ----------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
