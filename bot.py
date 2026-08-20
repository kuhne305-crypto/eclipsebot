import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime
import pytz
import json
import os

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # <-- deine Server-ID hier als Railway Variable eintragen!
TIMEZONE = pytz.timezone("Europe/Berlin")
EMBED_COLOR = 0xFFD700  # Gelb
DATA_DIR = "/data" if os.path.isdir("/data") else "."
DATA_FILE = os.path.join(DATA_DIR, "data.json")

# Leitung: darf ALLES bearbeiten (wie Admin), auch ohne den Discord-Server-
# Berechtigung "Administrator".
LEITUNG_ROLLE_ID = 1526202327483285629

def ist_admin_oder_leitung(interaction: discord.Interaction) -> bool:
    """True für echte Admins ODER Mitglieder mit der Leitungs-Rolle."""
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.id == LEITUNG_ROLLE_ID for r in interaction.user.roles)

# ─── DATA HANDLER ─────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            geladen = json.load(f)
    else:
        geladen = {}

    standard = {
        "channel_chat_hinweis": 1528463937149079642,
        "ooc_hinweis_nachricht_id": None,
    }
    if not geladen:
        return standard
    for key, wert in standard.items():
        geladen.setdefault(key, wert)
    return geladen

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

data = load_data()

# ─── BOT SETUP ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── OOC-CHAT REGELHINWEIS (stündlich) ────────────────────────────────────────
def build_ooc_hinweis_embed():
    embed = discord.Embed(
        title="📢 OOC-CHAT – REGELHINWEIS",
        description="Dieser Channel ist **ausschließlich OOC**.",
        color=EMBED_COLOR
    )
    embed.add_field(
        name="❌ **KEINE** IC-Informationen",
        value="> Alles, was euren Charakter, Storys, Orte oder Geschehnisse ingame betrifft, gehört hier nicht rein.",
        inline=False
    )
    embed.add_field(
        name="❌ **KEINE** IC-Fragen",
        value="> Fragen zu RP-Situationen, Personen oder Abläufen bitte direkt ingame klären.",
        inline=False
    )
    embed.add_field(
        name="❌ **KEINE** IC-Absprachen",
        value="> Absprachen, die das RP beeinflussen könnten, dürfen nicht außerhalb des Spiels stattfinden.",
        inline=False
    )
    embed.add_field(
        name="❌ **KEIN** Meta-Gaming",
        value="> OOC-Wissen darf nicht genutzt werden, um sich ingame Vorteile zu verschaffen.",
        inline=False
    )
    embed.add_field(
        name="❌ **KEIN** Gambo-Talk",
        value="> Kein Reden über Schießereien, Taktiken oder ähnliche Action-Themen.",
        inline=False
    )
    embed.add_field(
        name="❌ **KEINE** OOC Madness",
        value="> Es ist und bleibt ein Spiel, bei dem wir alle gemeinsam Spaß haben wollen. Beleidigungen, Provokationen, unnötige Dramen oder persönliche Angriffe haben hier nichts zu suchen.",
        inline=False
    )
    embed.add_field(
        name="💡 Merke",
        value="> Wer etwas IC klären oder wissen möchte, macht dies **ingame** – nicht hier.",
        inline=False
    )
    embed.set_footer(text="ECLIPSE - wer mich hatet swy Sam Lake hat mich Programmiert!🖕")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

async def ooc_hinweis_senden():
    if not data.get("channel_chat_hinweis"):
        return
    for guild in bot.guilds:
        kanal = guild.get_channel(int(data["channel_chat_hinweis"]))
        if not kanal:
            continue

        alte_msg_id = data.get("ooc_hinweis_nachricht_id")
        if alte_msg_id:
            try:
                alte_msg = await kanal.fetch_message(int(alte_msg_id))
                await alte_msg.delete()
            except Exception:
                pass

        try:
            neue_msg = await kanal.send(embed=build_ooc_hinweis_embed())
            data["ooc_hinweis_nachricht_id"] = str(neue_msg.id)
            save_data(data)
        except Exception as e:
            print(f"Fehler beim Senden des OOC-Hinweises: {e}")

# ─── TASKS ────────────────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def check_zeit():
    now = datetime.now(TIMEZONE)
    h, m = now.hour, now.minute

    OOC_HINWEIS_STUNDEN = (0, 4, 8, 12, 16, 20)
    if m == 0 and h in OOC_HINWEIS_STUNDEN:
        await ooc_hinweis_senden()

# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────

@tree.command(name="set_chat", description="Setzt den Channel für den stündlichen OOC-Regelhinweis")
@app_commands.describe(channel="Der Channel wo stündlich der OOC-Regelhinweis gepostet wird")
@app_commands.check(ist_admin_oder_leitung)
async def set_chat(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_chat_hinweis"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ OOC-Regelhinweis-Channel gesetzt: {channel.mention}\nAb jetzt wird dort regelmäßig der Hinweis gepostet.",
        ephemeral=True
    )
    await ooc_hinweis_senden()

@tree.command(name="ooc_hinweis_posten", description="Postet den OOC-Regelhinweis sofort neu")
@app_commands.check(ist_admin_oder_leitung)
async def ooc_hinweis_posten(interaction: discord.Interaction):
    if not data.get("channel_chat_hinweis"):
        await interaction.response.send_message(
            "❌ Kein Channel gesetzt!\nBitte zuerst **/set_chat #channel** benutzen.",
            ephemeral=True
        )
        return
    await interaction.response.send_message("Poste Hinweis...", ephemeral=True)
    await ooc_hinweis_senden()
    await interaction.edit_original_response(content="✅ OOC-Regelhinweis gepostet/aktualisiert.")

@tree.command(name="channels", description="Zeigt den aktuell gesetzten OOC-Hinweis-Channel")
@app_commands.check(ist_admin_oder_leitung)
async def channels_info(interaction: discord.Interaction):
    chat_ch = interaction.guild.get_channel(int(data["channel_chat_hinweis"])) if data.get("channel_chat_hinweis") else None
    await interaction.response.send_message(
        f"**Aktuelle Einstellungen:**\n\n"
        f"OOC-Regelhinweis: {chat_ch.mention if chat_ch else '❌ Nicht gesetzt – /set_chat benutzen'}",
        ephemeral=True
    )

# ─── BOT EVENTS ───────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")

    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
            print(f"✅ {len(synced)} Commands SOFORT auf Guild {GUILD_ID} gesynct: {[c.name for c in synced]}")
        else:
            print("⚠️ Keine GUILD_ID gesetzt — sync läuft global (kann bis zu 1h dauern).")

        synced_global = await tree.sync()
        print(f"✅ {len(synced_global)} Commands global gesynct: {[c.name for c in synced_global]}")
    except Exception as e:
        print(f"❌ FEHLER beim Sync: {e}")

    check_zeit.start()
    print("Tasks gestartet. Bot ist bereit!")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    await bot.process_commands(message)

@bot.event
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "Du hast keine Berechtigung für diesen Befehl.", ephemeral=True
        )
    else:
        print(f"Command Error: {error}")
        try:
            await interaction.response.send_message("Ein Fehler ist aufgetreten.", ephemeral=True)
        except Exception:
            pass

# ─── START ────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
