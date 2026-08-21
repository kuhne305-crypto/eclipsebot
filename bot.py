import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
import json
import os
import random
import pytz
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

DATA_DIR = "/data" if os.path.isdir("/data") else "."
DATA_FILE = os.path.join(DATA_DIR, "ai_data.json")

TIMEZONE = pytz.timezone("Europe/Berlin")
LEITUNG_ROLLE_ID = 1526202327483285629

# Wie viele Nachrichten Kontext pro Channel im Kurzzeitgedächtnis behalten werden.
# Großzügig bemessen, da ein Beobachtungsfenster mehrere Minuten Chat abdecken soll.
HISTORY_LIMIT = 60

# Beobachtungsfenster: nach dem Ende einer "stillen"/beobachteten Phase wird
# ein zufälliger Zeitpunkt zwischen MIN und MAX Sekunden für die nächste
# mögliche Reaktion gewürfelt, damit es nicht mechanisch wirkt.
BEOBACHTUNG_MIN_SEKUNDEN = 5 * 60
BEOBACHTUNG_MAX_SEKUNDEN = 10 * 60
# Wie oft der Hintergrund-Task prüft, ob ein Channel reif für eine Reaktion ist
PRUEF_INTERVALL_SEKUNDEN = 30

# Mindestabstand zwischen zwei Sofort-Antworten (Erwähnung/Reply) im selben Channel
COOLDOWN_SEKUNDEN = 3

EMBED_COLOR = 0xFFD700  # Gelb
# Zu welchen vollen Stunden (Europe/Berlin) der OOC-Regelhinweis neu gepostet wird
OOC_HINWEIS_STUNDEN = (0, 4, 8, 12, 16, 20)

# Sentinel-Token, mit dem das Modell signalisieren kann "nichts zu sagen" –
# wichtig für den Beobachtungsmodus, damit nicht jedes Fenster kommentiert wird.
SKIP_TOKEN = "KEIN_KOMMENTAR"

DEFAULT_SYSTEM_PROMPT_BEOBACHTUNG = (
    "Du bist ein Discord-Bot, der eine Weile im Channel mitliest und danach "
    "gelegentlich einen eigenen Beitrag zum Gespräch macht – wie ein Mitglied "
    "des Servers, das mitgelesen hat und jetzt etwas dazu sagen will, nicht wie "
    "ein Assistent, der auf jede Nachricht reagieren muss. "
    "Lies den Ton der letzten Unterhaltung und reagiere passend: Wenn es "
    "lustig/albern zuging, sei witzig und locker. Wenn es ernst, hilfreich "
    "oder sachlich war, antworte entsprechend ruhig und konkret. Antworte auf "
    "Deutsch, kurz und natürlich wie eine echte Discord-Nachricht (meist 1-3 "
    "Sätze, keine langen Absätze, keine Aufzählungen außer es passt wirklich). "
    "Nutze keine Emojis inflationär. Sprich niemals darüber, dass du ein "
    "Sprachmodell oder eine KI bist, außer man fragt dich direkt danach.\n\n"
    f"Sei dabei nicht zu zurückhaltend: Auch ein kurzer, lockerer Kommentar, "
    f"eine Anspielung auf etwas Gesagtes, ein Lacher oder eine kleine Meinung "
    f"zählt als guter Beitrag – du musst nicht auf den 'perfekten' Moment "
    f"warten. Antworte NUR mit dem Wort {SKIP_TOKEN} und sonst nichts, wenn "
    f"wirklich absolut nichts im Verlauf steht, worauf man reagieren könnte "
    f"(z.B. komplett leer oder nur Bot-eigene alte Nachrichten)."
)

DEFAULT_SYSTEM_PROMPT_DIREKT = (
    "Du bist ein Discord-Bot und wirst gerade direkt angesprochen (Erwähnung "
    "oder Reply). Lies den Ton der Unterhaltung und reagiere passend: lustig/"
    "locker bei albernem Kontext, ruhig/konkret bei ernstem oder sachlichem "
    "Kontext. Antworte auf Deutsch, kurz und natürlich wie eine echte Discord-"
    "Nachricht (meist 1-3 Sätze). Nutze keine Emojis inflationär. Sprich "
    "niemals darüber, dass du ein Sprachmodell oder eine KI bist, außer man "
    "fragt dich direkt danach."
)

# ─── DATA HANDLER ─────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            geladen = json.load(f)
    else:
        geladen = {}
    standard = {
        # {channel_id_str: "beobachtend" | "erwaehnung" | "aus"}
        "channel_modi": {},
        "system_prompt_beobachtung": DEFAULT_SYSTEM_PROMPT_BEOBACHTUNG,
        "system_prompt_direkt": DEFAULT_SYSTEM_PROMPT_DIREKT,
        "channel_chat_hinweis": None,
        "ooc_hinweis_nachricht_id": None,
    }
    for key, wert in standard.items():
        geladen.setdefault(key, wert)
    return geladen

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

data = load_data()

# Kurzzeitgedächtnis pro Channel (nicht persistiert, lebt im RAM)
verlauf = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
# Verhindert überlappende Generierungen pro Channel (gilt für Sofort- UND
# Beobachtungs-Reaktionen gemeinsam, damit sie sich nicht überschneiden)
channel_locks = defaultdict(asyncio.Lock)
letzte_direkt_antwort = {}  # channel_id -> datetime der letzten Sofort-Antwort

# Beobachtungsmodus-Zustand
naechste_pruefung = {}          # channel_id -> datetime, ab wann reagiert werden darf
ungesehene_seit_reaktion = defaultdict(int)  # channel_id -> Anzahl neuer Nachrichten
skip_zaehler = defaultdict(int)  # channel_id -> Anzahl KEIN_KOMMENTAR in Folge
# Ab so vielen Skips in Folge (bei weiterhin neuen Nachrichten) wird das
# Modell härter dazu gedrängt, trotzdem etwas zu sagen.
MAX_SKIPS_IN_FOLGE = 3

def neues_beobachtungsfenster() -> datetime:
    sekunden = random.uniform(BEOBACHTUNG_MIN_SEKUNDEN, BEOBACHTUNG_MAX_SEKUNDEN)
    return datetime.now(timezone.utc) + timedelta(seconds=sekunden)

# ─── BOT SETUP ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

def ist_admin_oder_leitung(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.id == LEITUNG_ROLLE_ID for r in interaction.user.roles)

def channel_modus(channel_id: int) -> str:
    return data["channel_modi"].get(str(channel_id), "erwaehnung")

# ─── ANTHROPIC API AUFRUF ─────────────────────────────────────────────────────
async def frage_claude(channel_id: int, system_prompt: str, zwinge_antwort: bool = False) -> str | None:
    if not ANTHROPIC_API_KEY:
        print("⚠️ ANTHROPIC_API_KEY fehlt, kann keine Antwort generieren.")
        return None

    nachrichten_verlauf = list(verlauf[channel_id])
    if not nachrichten_verlauf:
        return None

    verlauf_text = "\n".join(
        f"{eintrag['autor']}: {eintrag['inhalt']}" for eintrag in nachrichten_verlauf
    )

    hinweis = ""
    if zwinge_antwort:
        hinweis = (
            "\n\n(Hinweis: Du hast die letzten Male nichts gesagt, obwohl "
            f"weiter geschrieben wurde. Antworte diesmal NICHT mit {SKIP_TOKEN} "
            "sondern finde einen kurzen, lockeren Anschlusskommentar, selbst "
            "wenn es nur eine knappe Reaktion ist.)"
        )

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 300,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Hier ist der bisherige Chatverlauf im Discord-Channel "
                    "(Format: Name: Nachricht). Reagiere als nächster Beitrag "
                    "im Gespräch, so wie es gerade angebracht ist:\n\n"
                    f"{verlauf_text}{hinweis}"
                ),
            }
        ],
    }

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"❌ Anthropic API Fehler {resp.status}: {text}")
                    return None
                result = await resp.json()
                teile = [b["text"] for b in result.get("content", []) if b.get("type") == "text"]
                antwort = "\n".join(teile).strip()
                return antwort or None
    except asyncio.TimeoutError:
        print("❌ Anthropic API Timeout")
        return None
    except Exception as e:
        print(f"❌ Fehler beim Aufruf der Anthropic API: {e}")
        return None

# ─── OOC-CHAT REGELHINWEIS (mehrmals täglich) ────────────────────────────────
def build_ooc_hinweis_embed() -> discord.Embed:
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
    embed.set_footer(text="ECLIPSE")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

async def ooc_hinweis_senden():
    channel_id = data.get("channel_chat_hinweis")
    if not channel_id:
        return
    kanal = bot.get_channel(int(channel_id))
    if not kanal:
        return

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
        print(f"❌ Fehler beim Senden des OOC-Hinweises: {e}")

@tasks.loop(minutes=1)
async def ooc_hinweis_check():
    now = datetime.now(TIMEZONE)
    if now.minute == 0 and now.hour in OOC_HINWEIS_STUNDEN:
        await ooc_hinweis_senden()

@ooc_hinweis_check.before_loop
async def vor_ooc_hinweis_check():
    await bot.wait_until_ready()

# ─── HILFSFUNKTIONEN ──────────────────────────────────────────────────────────
async def ist_reply_an_bot(message: discord.Message) -> bool:
    if message.reference is None:
        return False
    resolved = message.reference.resolved
    if resolved is None and message.reference.message_id:
        try:
            resolved = await message.channel.fetch_message(message.reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
    return getattr(resolved, "author", None) == bot.user

async def sende_antwort(channel: discord.abc.Messageable, text: str, channel_id: int):
    try:
        gesendet = await channel.send(text)
    except Exception as e:
        print(f"❌ Fehler beim Senden der Antwort: {e}")
        return
    # Eigene Antwort auch ins Gedächtnis aufnehmen, damit der Bot sich selbst
    # im weiteren Verlauf "erinnert" und nicht mehrfach dasselbe sagt.
    verlauf[channel_id].append({
        "autor": gesendet.author.display_name,
        "inhalt": text,
    })

# ─── ON MESSAGE: SOFORT-REAKTION BEI ERWÄHNUNG/REPLY ─────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        await bot.process_commands(message)
        return

    if message.author.bot:
        await bot.process_commands(message)
        return

    channel_id = message.channel.id
    modus = channel_modus(channel_id)

    # Jede menschliche Nachricht wandert ins Kurzzeitgedächtnis, unabhängig
    # vom Modus, damit bei Bedarf (Erwähnung oder spätere Beobachtung) Kontext
    # vorhanden ist.
    if message.content:
        verlauf[channel_id].append({
            "autor": message.author.display_name,
            "inhalt": message.content,
        })
        if modus == "beobachtend":
            ungesehene_seit_reaktion[channel_id] += 1
            # Erste Nachricht seit der letzten Reaktion startet das Zeitfenster.
            if channel_id not in naechste_pruefung:
                naechste_pruefung[channel_id] = neues_beobachtungsfenster()

    await bot.process_commands(message)

    if modus == "aus":
        return

    erwaehnt = bot.user in message.mentions
    reply_an_bot = await ist_reply_an_bot(message)
    if not (erwaehnt or reply_an_bot):
        return

    # Direkte Ansprache -> sofort reagieren, unabhängig vom Kanal-Modus.
    lock = channel_locks[channel_id]
    if lock.locked():
        return

    async with lock:
        jetzt = datetime.now(timezone.utc)
        letzte = letzte_direkt_antwort.get(channel_id)
        if letzte and (jetzt - letzte).total_seconds() < COOLDOWN_SEKUNDEN:
            return

        async with message.channel.typing():
            antwort = await frage_claude(
                channel_id, data.get("system_prompt_direkt", DEFAULT_SYSTEM_PROMPT_DIREKT)
            )

        if not antwort or antwort.strip().upper() == SKIP_TOKEN:
            return

        letzte_direkt_antwort[channel_id] = datetime.now(timezone.utc)
        await sende_antwort(message.channel, antwort, channel_id)
        # Direkte Antwort zählt auch als "gesehen" für den Beobachtungsmodus.
        ungesehene_seit_reaktion[channel_id] = 0
        naechste_pruefung[channel_id] = neues_beobachtungsfenster()

# ─── HINTERGRUND-TASK: VERZÖGERTE BEOBACHTUNGS-REAKTION ──────────────────────
@tasks.loop(seconds=PRUEF_INTERVALL_SEKUNDEN)
async def beobachtungs_check():
    jetzt = datetime.now(timezone.utc)
    for channel_id, faellig_um in list(naechste_pruefung.items()):
        try:
            await _beobachtungs_check_channel(channel_id, faellig_um, jetzt)
        except Exception as e:
            # Ein Fehler bei einem Channel darf die gesamte Loop nicht
            # stoppen (tasks.loop würde sich sonst sang- und klanglos
            # beenden und nie wieder prüfen).
            print(f"[beobachtung] ❌ Unerwarteter Fehler bei Channel {channel_id}: {e}")

async def _beobachtungs_check_channel(channel_id, faellig_um, jetzt):
    if channel_modus(channel_id) != "beobachtend":
        naechste_pruefung.pop(channel_id, None)
        ungesehene_seit_reaktion.pop(channel_id, None)
        skip_zaehler.pop(channel_id, None)
        return
    if jetzt < faellig_um:
        return
    anzahl_neu = ungesehene_seit_reaktion.get(channel_id, 0)
    if anzahl_neu == 0:
        print(f"[beobachtung] Channel {channel_id}: fällig, aber keine neuen Nachrichten -> Fenster neu würfeln.")
        naechste_pruefung[channel_id] = neues_beobachtungsfenster()
        return

    kanal = bot.get_channel(channel_id)
    if kanal is None:
        print(f"[beobachtung] Channel {channel_id} nicht im Cache gefunden -> entferne aus Beobachtung.")
        naechste_pruefung.pop(channel_id, None)
        ungesehene_seit_reaktion.pop(channel_id, None)
        skip_zaehler.pop(channel_id, None)
        return

    lock = channel_locks[channel_id]
    if lock.locked():
        print(f"[beobachtung] Channel {channel_id}: Lock belegt (Sofort-Reaktion läuft) -> nächste Runde erneut.")
        return

    zwinge_antwort = skip_zaehler[channel_id] >= MAX_SKIPS_IN_FOLGE
    print(f"[beobachtung] Channel {channel_id}: fällig mit {anzahl_neu} neuen Nachrichten, generiere Reaktion (zwinge_antwort={zwinge_antwort})...")

    async with lock:
        async with kanal.typing():
            antwort = await frage_claude(
                channel_id,
                data.get("system_prompt_beobachtung", DEFAULT_SYSTEM_PROMPT_BEOBACHTUNG),
                zwinge_antwort=zwinge_antwort,
            )

        if antwort is None:
            print(f"[beobachtung] Channel {channel_id}: keine Antwort von der API erhalten (siehe Fehler oben).")
        elif antwort.strip().upper() == SKIP_TOKEN:
            skip_zaehler[channel_id] += 1
            print(f"[beobachtung] Channel {channel_id}: Modell hat {SKIP_TOKEN} gewählt (in Folge: {skip_zaehler[channel_id]}).")
        else:
            skip_zaehler[channel_id] = 0
            print(f"[beobachtung] Channel {channel_id}: Antwort gesendet.")
            await sende_antwort(kanal, antwort, channel_id)

        ungesehene_seit_reaktion[channel_id] = 0
        naechste_pruefung[channel_id] = neues_beobachtungsfenster()

@beobachtungs_check.error
async def beobachtungs_check_fehler(error):
    # Letzte Sicherheitsnetz-Ebene: selbst wenn oben etwas durchrutscht,
    # soll die Loop nicht sterben, sondern nur geloggt werden. discord.py
    # startet die Loop nach einem Error-Handler NICHT automatisch neu, daher
    # hier explizit neu starten.
    print(f"[beobachtung] ❌ Loop-Fehler (wird neu gestartet): {error}")
    if beobachtungs_check.is_running():
        beobachtungs_check.restart()
    else:
        beobachtungs_check.start()

@beobachtungs_check.before_loop
async def vor_beobachtungs_check():
    await bot.wait_until_ready()

# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────
@tree.command(name="ki_channel", description="Legt fest, wie der KI-Bot in diesem Channel reagiert")
@app_commands.describe(
    modus="beobachtend = liest mit und reagiert nach 5-10 Min von selbst, erwaehnung = nur bei @Erwähnung/Reply, aus = gar nicht",
    channel="Zielchannel (Standard: aktueller Channel)",
)
@app_commands.choices(modus=[
    app_commands.Choice(name="Beobachtend (reagiert nach 5-10 Min von selbst)", value="beobachtend"),
    app_commands.Choice(name="Nur bei Erwähnung/Reply", value="erwaehnung"),
    app_commands.Choice(name="Aus", value="aus"),
])
@app_commands.check(ist_admin_oder_leitung)
async def ki_channel(interaction: discord.Interaction, modus: app_commands.Choice[str], channel: discord.TextChannel = None):
    ziel = channel or interaction.channel
    data["channel_modi"][str(ziel.id)] = modus.value
    save_data(data)

    if modus.value == "beobachtend":
        naechste_pruefung[ziel.id] = neues_beobachtungsfenster()
        ungesehene_seit_reaktion[ziel.id] = 0
        skip_zaehler[ziel.id] = 0
    else:
        naechste_pruefung.pop(ziel.id, None)
        ungesehene_seit_reaktion.pop(ziel.id, None)
        skip_zaehler.pop(ziel.id, None)

    await interaction.response.send_message(
        f"✅ KI-Modus für {ziel.mention}: **{modus.name}**", ephemeral=True
    )

@tree.command(name="ki_status", description="Zeigt die KI-Konfiguration aller gesetzten Channels")
@app_commands.check(ist_admin_oder_leitung)
async def ki_status(interaction: discord.Interaction):
    if not data["channel_modi"]:
        await interaction.response.send_message(
            "Kein Channel konfiguriert – Standard ist überall **erwaehnung**.", ephemeral=True
        )
        return
    jetzt = datetime.now(timezone.utc)
    zeilen = []
    for cid_str, modus in data["channel_modi"].items():
        cid = int(cid_str)
        kanal = interaction.guild.get_channel(cid)
        name = kanal.mention if kanal else cid_str
        extra = ""
        if modus == "beobachtend" and cid in naechste_pruefung:
            rest = (naechste_pruefung[cid] - jetzt).total_seconds()
            if rest > 0:
                extra = f" (nächste mögliche Reaktion in ~{int(rest // 60)} Min)"
            else:
                extra = " (fällig)"
            if skip_zaehler.get(cid, 0) > 0:
                extra += f" [zuletzt {skip_zaehler[cid]}x übersprungen]"
        zeilen.append(f"{name}: **{modus}**{extra}")

    ooc_id = data.get("channel_chat_hinweis")
    if ooc_id:
        ooc_kanal = interaction.guild.get_channel(int(ooc_id))
        zeilen.append(f"\nOOC-Regelhinweis: {ooc_kanal.mention if ooc_kanal else ooc_id}")

    await interaction.response.send_message("\n".join(zeilen), ephemeral=True)

@tree.command(name="ki_reset", description="Löscht das Kurzzeitgedächtnis dieses Channels")
@app_commands.check(ist_admin_oder_leitung)
async def ki_reset(interaction: discord.Interaction):
    cid = interaction.channel.id
    verlauf[cid].clear()
    ungesehene_seit_reaktion[cid] = 0
    skip_zaehler[cid] = 0
    if channel_modus(cid) == "beobachtend":
        naechste_pruefung[cid] = neues_beobachtungsfenster()
    await interaction.response.send_message("✅ Gedächtnis für diesen Channel geleert.", ephemeral=True)

@tree.command(name="ki_persona", description="Setzt den System-Prompt für den Beobachtungsmodus")
@app_commands.describe(text="Neue Anweisung (leer lassen für Standard)")
@app_commands.check(ist_admin_oder_leitung)
async def ki_persona(interaction: discord.Interaction, text: str = None):
    data["system_prompt_beobachtung"] = text if text else DEFAULT_SYSTEM_PROMPT_BEOBACHTUNG
    save_data(data)
    await interaction.response.send_message("✅ System-Prompt (Beobachtung) aktualisiert.", ephemeral=True)

@tree.command(name="ki_persona_direkt", description="Setzt den System-Prompt für Sofort-Antworten bei Erwähnung/Reply")
@app_commands.describe(text="Neue Anweisung (leer lassen für Standard)")
@app_commands.check(ist_admin_oder_leitung)
async def ki_persona_direkt(interaction: discord.Interaction, text: str = None):
    data["system_prompt_direkt"] = text if text else DEFAULT_SYSTEM_PROMPT_DIREKT
    save_data(data)
    await interaction.response.send_message("✅ System-Prompt (Direkt) aktualisiert.", ephemeral=True)

@tree.command(name="set_chat", description="Setzt den Channel für den mehrmals täglichen OOC-Regelhinweis")
@app_commands.describe(channel="Der Channel wo der OOC-Regelhinweis gepostet wird")
@app_commands.check(ist_admin_oder_leitung)
async def set_chat(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_chat_hinweis"] = channel.id
    data["ooc_hinweis_nachricht_id"] = None
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
            synced_global = await tree.sync()
            print(f"✅ {len(synced_global)} Commands global gesynct: {[c.name for c in synced_global]}")
    except Exception as e:
        print(f"❌ FEHLER beim Sync: {e}")

    # Für bereits als "beobachtend" konfigurierte Channels beim (Neu-)Start
    # ein erstes Zeitfenster setzen, falls noch keins existiert.
    for cid_str, modus in data["channel_modi"].items():
        if modus == "beobachtend":
            cid = int(cid_str)
            naechste_pruefung.setdefault(cid, neues_beobachtungsfenster())

    if not beobachtungs_check.is_running():
        beobachtungs_check.start()
    if not ooc_hinweis_check.is_running():
        ooc_hinweis_check.start()

    print("Bot ist bereit!")

@bot.event
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send("Du hast keine Berechtigung für diesen Befehl.", ephemeral=True)
        else:
            await interaction.response.send_message("Du hast keine Berechtigung für diesen Befehl.", ephemeral=True)
    else:
        print(f"Command Error: {error}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Ein Fehler ist aufgetreten.", ephemeral=True)
            else:
                await interaction.response.send_message("Ein Fehler ist aufgetreten.", ephemeral=True)
        except Exception:
            pass

# ─── START ────────────────────────────────────────────────────────────────────
if not TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN fehlt als Umgebungsvariable.")

bot.run(TOKEN)
