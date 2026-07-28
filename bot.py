import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import random
from datetime import datetime, timedelta
import pytz
import json
import os
import re
import anthropic

# ─── "BOT-NECK" FEATURE: Direktnachricht bei Beleidigungen ──────────────────
# Reagiert auf JEDE Person im Server (nicht mehr nur auf eine bestimmte ID).
# Die Erkennung prüft nur noch, ob die Nachricht IRGENDEIN Wort aus der
# Schimpfwortliste enthält (das Wort "bot" muss NICHT mehr zusätzlich
# vorkommen) — dadurch springt der Bot auf JEDE Beleidigung an, unabhängig
# davon, ob er direkt angesprochen wird. Erkennung bleibt unabhängig von
# Groß-/Kleinschreibung, Satzzeichen (!,?,. etc.) und gängigen
# Schreibvarianten (z.B. "scheiss" vs. "scheiß").
#
# WICHTIG: Der Bot antwortet nicht mehr mit zufälligen, vorgefertigten
# Sätzen. Stattdessen liest er, WAS die Person tatsächlich geschrieben hat
# (Frage, Beleidigung, Smalltalk, ...) und lässt sich von der Anthropic
# Claude-API eine passende, in Charakter formulierte Antwort generieren —
# kalt, mysteriös, mit wenigen Worten. Die festen Listen unten dienen nur
# noch als Notfall-Rückfall, falls kein API-Key gesetzt ist oder der
# API-Aufruf fehlschlägt (z.B. Netzwerkproblem), damit der Bot nie ganz
# stumm bleibt.

# Schimpfwörter/Reizwörter — beliebig ergänzen/entfernen.
NECK_SCHIMPFWOERTER = [
    "scheiss", "scheiß", "kacke", "kack", "doof", "blöd", "bloed", "dumm",
    "nervt", "nervig", "mist", "trottel", "idiot", "kaputt", "müll", "muell",
    "schrott", "behindert", "assi", "peinlich", "unfähig", "unfaehig",
]

# Notfall-Rückfall, falls die KI-Antwort nicht erzeugt werden konnte.
NECK_NACHRICHTEN_FALLBACK = [
    "Ich hab das gehört 👀",
    "Notiert.",
    "Ich vergesse nichts. Und niemanden.",
]
NECK_DM_ANTWORTEN_FALLBACK = [
    "🕯️ ...",
    "Ich höre zu. Immer.",
    "Interessant, dass du das gerade jetzt sagst.",
    "hm...",
    "Bald überlegst du 3x nach bevor du mir schreibst!",
]

# ─── KI-PERSÖNLICHKEIT (kalt, mysteriös) ──────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NECK_MODEL = os.environ.get("NECK_MODEL", "claude-haiku-4-5-20251001")
_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

NECK_SYSTEM_PROMPT = (
    "Du bist die dunkle, mysteriöse Stimme hinter einem Discord-Server-Bot "
    "namens ECLIPSE. Du sprichst Deutsch. Dein Charakter ist kalt, distanziert, "
    "unheimlich und leicht bedrohlich — aber nie wirklich beleidigend oder "
    "hasserfüllt. Du wirkst allwissend: du beobachtest den Server angeblich "
    "ständig und vergisst nie etwas. Wenn dich jemand beleidigt, reagierst du "
    "nicht wütend, sondern mit ruhiger, unterschwellig drohender Gelassenheit "
    "(z.B. andeuten, dass du dir das merkst, ohne konkrete Drohungen "
    "auszusprechen). Wenn dich jemand etwas fragt oder normal anschreibt, "
    "antwortest du trotzdem in Charakter: kurz, kryptisch, mit Andeutungen, "
    "manchmal mit einer Gegenfrage — aber du gehst inhaltlich auf das ein, "
    "was die Person tatsächlich geschrieben hat, statt nur eine Floskel "
    "abzuspulen. Halte Antworten SEHR kurz: maximal 1–2 Sätze. Keine "
    "Emoji-Wüsten, höchstens ein einzelnes passendes Emoji. Keine "
    "Entschuldigungen, keine 'Als KI...'-Floskeln, keine Erklärungen deiner "
    "selbst. Du bleibst immer in dieser Rolle."
)

# Kurzes In-Memory-Gedächtnis pro Nutzer:in für den DM-Chat, damit sich ein
# Gespräch natürlich anfühlt. Wird NICHT dauerhaft gespeichert (geht bei
# Neustart des Bots verloren) und bewusst auf wenige Nachrichten begrenzt,
# damit die API-Aufrufe klein/günstig bleiben.
_dm_verlauf: dict[str, list[dict]] = {}
NECK_VERLAUF_LIMIT = 10  # max. Anzahl gespeicherter Nachrichten (User+Bot) pro Person

async def hole_ki_antwort(user_id: str, text: str, beleidigung: bool = True) -> str:
    """Lässt Claude eine kurze, kalte/mysteriöse Antwort auf 'text' formulieren.
    Nutzt bei DMs den bisherigen Verlauf für Kontext. Fällt bei fehlendem
    API-Key oder Fehlern auf eine feste Notfall-Nachricht zurück."""
    if not _anthropic_client:
        pool = NECK_NACHRICHTEN_FALLBACK if beleidigung else NECK_DM_ANTWORTEN_FALLBACK
        return random.choice(pool)

    verlauf = _dm_verlauf.setdefault(user_id, [])
    hinweis = (
        "[Diese Person hat dich gerade in einem Server-Chat beleidigt. "
        "Reagiere kalt und mysteriös darauf.] "
    ) if beleidigung else ""
    verlauf.append({"role": "user", "content": f"{hinweis}{text}"})

    try:
        antwort = await asyncio.to_thread(
            _anthropic_client.messages.create,
            model=NECK_MODEL,
            max_tokens=150,
            system=NECK_SYSTEM_PROMPT,
            messages=verlauf,
        )
        antwort_text = "".join(
            block.text for block in antwort.content if block.type == "text"
        ).strip()
        if not antwort_text:
            raise ValueError("Leere Antwort von der API erhalten")
    except Exception as e:
        print(f"Fehler beim KI-Antwort-Aufruf: {e}")
        verlauf.pop()  # fehlgeschlagene Anfrage nicht im Verlauf behalten
        pool = NECK_NACHRICHTEN_FALLBACK if beleidigung else NECK_DM_ANTWORTEN_FALLBACK
        return random.choice(pool)

    verlauf.append({"role": "assistant", "content": antwort_text})
    if len(verlauf) > NECK_VERLAUF_LIMIT:
        del verlauf[: len(verlauf) - NECK_VERLAUF_LIMIT]
    return antwort_text

def normalisiere_text(text: str) -> str:
    """Kleinschreibung + Entfernen von Satzzeichen/Sonderzeichen,
    damit die Erkennung unabhängig von Groß-/Kleinschreibung und
    Satzzeichen funktioniert."""
    text = text.lower()
    text = re.sub(r"[^a-zäöüß\s]", " ", text)
    return text

def ist_beleidigung(text: str) -> bool:
    """True, wenn die Nachricht mindestens ein Schimpfwort enthält.
    Anders als vorher wird NICHT mehr zusätzlich verlangt, dass das Wort
    'bot' vorkommt — der Bot springt so auf JEDE Beleidigung an."""
    normalisiert = normalisiere_text(text)
    return any(wort in normalisiert for wort in NECK_SCHIMPFWOERTER)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # <-- deine Server-ID hier als Railway Variable eintragen!
TIMEZONE = pytz.timezone("Europe/Berlin")
EMBED_COLOR = 0xFFD700  # Gelb
DATA_DIR = "/data" if os.path.isdir("/data") else "."
DATA_FILE = os.path.join(DATA_DIR, "data.json")

# Stempelsystem
STEMPEL_MANAGER_ROLLE_ID = 1526202327436886108   # darf Zeiten nachtragen/austragen

# ─── DATA HANDLER ─────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            geladen = json.load(f)
    else:
        geladen = {}

    standard = {
        "rolle_id": "1526202327365582918",
        "aktuelle_nachricht_id": None,
        "abstimmung": {},
        "abmeldungen": {},
        "eingefroren": False,
        "aktuelles_datum": None,
        "channel_aufstellung": 1526202329253019664,
        "channel_archiv": 1528440984869015552,
        "channel_abmeldung": 1528441264150810805,
        "channel_abmeldung_liste": 1526202329253019665,
        "abmeldung_liste_nachricht_id": None,
        "channel_abmeldung_button": 1526202329253019666,
        "abmeldung_button_nachricht_id": None,
        "aufstellung_tage_config": {str(i): {"aktiv": False, "uhrzeit": "20:00"} for i in range(7)},
        "aktueller_wochentag": None,
        "channel_verifizierung": 1526202329253019659,
        "verifizierung_nachricht_id": None,
        "channel_verifizierung_log": 1528441509542625290,
        "channel_probewoche_erinnerung": 1528442210901557268,
        "verifizierungen": {},
        "channel_chat_hinweis": 1528463937149079642,
        "ooc_hinweis_nachricht_id": None,
        "geplante_aufstellung_loeschungen": [],
        # Stempelsystem
        "channel_stempel": 1531376112226140260,
        "stempel_nachricht_id": None,
        "channel_stempel_liste": 1531376274130341909,
        "stempel_liste_nachricht_id": None,
        "stempel_nutzer": {},  # { "user_id": {"eingestempelt_seit": float|None, "gesamt_sekunden": float, "anzahl": int} }
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

# ─── HILFSFUNKTIONEN ──────────────────────────────────────────────────────────
WOCHENTAGE_NAMEN = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
WOCHENTAGE_MAP = {name.lower(): i for i, name in enumerate(WOCHENTAGE_NAMEN)}

def get_morgen_datum():
    now = datetime.now(TIMEZONE)
    morgen = now + timedelta(days=1)
    wochentage = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
    return f"{wochentage[morgen.weekday()]}, {morgen.strftime('%d.%m.%Y')}"

def get_heute_datum():
    now = datetime.now(TIMEZONE)
    wochentage = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]
    return f"{wochentage[now.weekday()]}, {now.strftime('%d.%m.%Y')}"

async def get_rolle_mitglieder(guild):
    rolle_id = data.get("rolle_id")
    if not rolle_id:
        return []
    rolle = guild.get_role(int(rolle_id))
    if not rolle:
        return []
    return [m for m in rolle.members if not m.bot]

# ─── DATUMS-HILFSFUNKTIONEN (Abmeldungen) ─────────────────────────────────────
DATUM_REGEX = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})$")

def parse_datum(datum_str):
    """Parst ein Datum im Format TT.MM.JJJJ oder TT.MM.JJ (auch ohne führende
    Nullen), sonst None. Ein 2-stelliges Jahr wird als 20XX interpretiert."""
    m = DATUM_REGEX.match(str(datum_str).strip())
    if not m:
        return None
    tag, monat, jahr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if jahr < 100:
        jahr += 2000
    try:
        return datetime(jahr, monat, tag)
    except ValueError:
        return None

def ist_abmeldung_aktiv(info):
    """True, wenn der abgemeldete Zeitraum HEUTE bereits läuft (von <= heute <= bis).
    Kann eines der Daten nicht ausgewertet werden, wird sicherheitshalber
    'aktiv' angenommen (altes Verhalten), damit nichts verloren geht."""
    heute = datetime.now(TIMEZONE).date()
    von_datum = parse_datum(info.get("von", ""))
    bis_datum = parse_datum(info.get("bis", ""))
    if von_datum and bis_datum:
        return von_datum.date() <= heute <= bis_datum.date()
    return True

def build_embed(datum, mitglieder, eingefroren=False):
    abstimmung  = data.get("abstimmung", {})
    abmeldungen = data.get("abmeldungen", {})

    wochentag   = data.get("aktueller_wochentag")
    tage_config = data.get("aufstellung_tage_config", {})
    anzeige_zeit = tage_config.get(str(wochentag), {}).get("uhrzeit", "21:00") if wochentag is not None else "21:00"

    ja_liste        = []
    spaeter_liste   = []
    nein_liste      = []
    abgemeldet_liste= []
    offen_liste     = []

    for m in mitglieder:
        uid     = str(m.id)
        mention = m.mention
        abmeldung_info = abmeldungen.get(uid)
        # Wer trotz Abmeldung aktiv abgestimmt hat, soll auch als das gezählt
        # werden, was er/sie gewählt hat — die Abmeldung blockiert das
        # Reagieren NICHT (vorher wurde eine gültige Stimme hier ignoriert
        # und die Person immer zwangsweise unter "Abgemeldet" einsortiert).
        if uid in abstimmung:
            status = abstimmung[uid]
            if status == "ja":
                ja_liste.append(mention)
            elif status == "spaeter":
                spaeter_liste.append(mention)
            elif status == "nein":
                nein_liste.append(mention)
        elif abmeldung_info and ist_abmeldung_aktiv(abmeldung_info):
            abgemeldet_liste.append(mention)
        else:
            offen_liste.append(mention)

    titel = "Meet Up"
    if eingefroren:
        titel += " *(Eingefroren)*"

    embed = discord.Embed(
        title=titel,
        description=(
            f"**{datum}**\n"
            f"Meet Up: **{anzeige_zeit} Uhr**\n"
            f"{'🔒 Abstimmung geschlossen!' if eingefroren else '✅ Jetzt abstimmen!'}"
        ),
        color=EMBED_COLOR
    )

    embed.add_field(
        name=f"Komme ({len(ja_liste)})",
        value="\n".join(ja_liste) if ja_liste else "*Niemand*",
        inline=True
    )
    embed.add_field(
        name=f"Komme später ({len(spaeter_liste)})",
        value="\n".join(spaeter_liste) if spaeter_liste else "*Niemand*",
        inline=True
    )
    embed.add_field(
        name=f"Komme nicht ({len(nein_liste)})",
        value="\n".join(nein_liste) if nein_liste else "*Niemand*",
        inline=True
    )
    embed.add_field(
        name=f"Abgemeldet ({len(abgemeldet_liste)})",
        value="\n".join(abgemeldet_liste) if abgemeldet_liste else "*Niemand*",
        inline=True
    )

    if offen_liste:
        label = "Nicht gemeldet" if eingefroren else "Noch nicht abgestimmt"
        embed.add_field(
            name=f"{label} ({len(offen_liste)})",
            value="\n".join(offen_liste),
            inline=False
        )

    embed.set_footer(text="ECLIPSE")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

# ─── VIEWS (BUTTONS) ──────────────────────────────────────────────────────────
class AufstellungView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def check_berechtigung(self, interaction: discord.Interaction):
        if data.get("eingefroren"):
            await interaction.response.send_message(
                "Die Abstimmung ist bereits geschlossen!", ephemeral=True
            )
            return False
        rolle_id = data.get("rolle_id")
        if not rolle_id:
            await interaction.response.send_message(
                "Keine Rolle gesetzt. Admin: /setrolle benutzen.", ephemeral=True
            )
            return False
        rolle = interaction.guild.get_role(int(rolle_id))
        if rolle not in interaction.user.roles:
            await interaction.response.send_message(
                "Du hast keine Berechtigung für diese Abstimmung.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Komme", style=discord.ButtonStyle.success, custom_id="btn_ja")
    async def btn_ja(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_berechtigung(interaction):
            return
        data["abstimmung"][str(interaction.user.id)] = "ja"
        save_data(data)
        await update_nachricht(interaction.guild)
        await interaction.response.send_message("Du hast mit **Komme** abgestimmt!", ephemeral=True)

    @discord.ui.button(label="Komme später", style=discord.ButtonStyle.secondary, custom_id="btn_spaeter")
    async def btn_spaeter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_berechtigung(interaction):
            return
        data["abstimmung"][str(interaction.user.id)] = "spaeter"
        save_data(data)
        await update_nachricht(interaction.guild)
        await interaction.response.send_message("Du hast mit **Komme später** abgestimmt!", ephemeral=True)

    @discord.ui.button(label="Komme nicht", style=discord.ButtonStyle.danger, custom_id="btn_nein")
    async def btn_nein(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_berechtigung(interaction):
            return
        data["abstimmung"][str(interaction.user.id)] = "nein"
        save_data(data)
        await update_nachricht(interaction.guild)
        await interaction.response.send_message("Du hast mit **Komme nicht** abgestimmt!", ephemeral=True)

# ─── ABMELDUNG PER BUTTON + MODAL ─────────────────────────────────────────────
class AbmeldungModal(discord.ui.Modal, title="Abmeldung"):
    name_input = discord.ui.TextInput(
        label="Name", placeholder="Wer meldet sich ab?", required=True, max_length=32
    )
    von_input = discord.ui.TextInput(
        label="Von wann? (TT.MM.JJJJ)", placeholder="14.07.2026", required=True, max_length=32
    )
    bis_input = discord.ui.TextInput(
        label="Bis wann? (TT.MM.JJJJ)", placeholder="16.07.2026", required=True, max_length=32
    )
    grund_input = discord.ui.TextInput(
        label="Begründung", placeholder="Grund der Abmeldung", required=True,
        max_length=300, style=discord.TextStyle.paragraph
    )

    async def on_submit(self, interaction: discord.Interaction):
        rolle_id = data.get("rolle_id")
        if rolle_id:
            rolle = interaction.guild.get_role(int(rolle_id))
            if rolle and rolle not in interaction.user.roles:
                await interaction.response.send_message(
                    "Du hast keine Berechtigung zur Abmeldung.", ephemeral=True
                )
                return

        name  = self.name_input.value.strip()
        von   = self.von_input.value.strip()
        bis   = self.bis_input.value.strip()
        grund = self.grund_input.value.strip()

        von_datum = parse_datum(von)
        bis_datum = parse_datum(bis)
        if not von_datum or not bis_datum:
            await interaction.response.send_message(
                "❌ Ungültiges Datumsformat. Bitte **TT.MM.JJJJ** verwenden, z.B. `14.07.2026`.",
                ephemeral=True
            )
            return
        if bis_datum < von_datum:
            await interaction.response.send_message(
                "❌ Das Enddatum darf nicht vor dem Startdatum liegen.", ephemeral=True
            )
            return

        von, bis = von_datum.strftime("%d.%m.%Y"), bis_datum.strftime("%d.%m.%Y")

        uid = str(interaction.user.id)
        data["abmeldungen"][uid] = {"name": name, "von": von, "bis": bis, "grund": grund, "typ": "kurzzeit"}
        save_data(data)

        if not data.get("eingefroren"):
            await update_nachricht(interaction.guild)
        await update_abmeldung_liste(interaction.guild)

        aktiv_hinweis = "" if ist_abmeldung_aktiv(data["abmeldungen"][uid]) else \
            "\nℹ️ Dein Zeitraum beginnt erst später – bis dahin wirst du weiterhin normal im Meet Up geführt und kannst abstimmen."
        await interaction.response.send_message(
            f"✅ Abmeldung eingetragen!\n"
            f"Name: **{name}**\n"
            f"Von: **{von}**\n"
            f"Bis: **{bis}**{aktiv_hinweis}",
            ephemeral=True
        )

        if data.get("channel_abmeldung"):
            abm_kanal = interaction.guild.get_channel(int(data["channel_abmeldung"]))
            if abm_kanal:
                embed_abm = discord.Embed(title="Neue Abmeldung", color=EMBED_COLOR)
                embed_abm.add_field(name="Name",     value=name,                     inline=True)
                embed_abm.add_field(name="Mitglied", value=interaction.user.mention, inline=True)
                embed_abm.add_field(name="Von",      value=von,                      inline=True)
                embed_abm.add_field(name="Bis",      value=bis,                      inline=True)
                embed_abm.add_field(name="Grund",    value=grund,                    inline=False)
                embed_abm.set_footer(text="ECLIPSE")
                embed_abm.timestamp = datetime.now(TIMEZONE)
                await abm_kanal.send(embed=embed_abm)

class AbmeldungButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abmeldung", style=discord.ButtonStyle.danger, custom_id="btn_abmeldung_oeffnen")
    async def btn_abmeldung(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AbmeldungModal())

async def abmeldung_button_posten_intern(guild):
    if not data.get("channel_abmeldung_button"):
        return
    kanal = guild.get_channel(int(data["channel_abmeldung_button"]))
    if not kanal:
        return

    embed = discord.Embed(
        title="Abmeldung",
        description="Klick auf den Button unten, um dich abzumelden. Du trägst Name, Zeitraum und Grund ein.",
        color=EMBED_COLOR
    )
    embed.set_footer(text="ECLIPSE")
    view = AbmeldungButtonView()

    msg_id = data.get("abmeldung_button_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return
        except Exception as e:
            print(f"Alte Abmeldung-Button-Nachricht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed, view=view)
    data["abmeldung_button_nachricht_id"] = str(msg.id)
    save_data(data)

# ─── ROUTENWACHE (Rein-/Raus-Tracking + Zeit-Übersicht) ──────────────────────
# Intern heißen Variablen/Datenkeys noch "stempel_*" (alte Speicherstruktur
# bleibt so erhalten, damit bereits erfasste Zeiten nicht verloren gehen),
# nach außen (Embeds, Buttons, Befehle) heißt das Feature jetzt "Routenwache".
def get_stempel_eintrag(user_id: str):
    """Holt (oder erstellt) den Routenwache-Eintrag eines Nutzers."""
    nutzer = data.setdefault("stempel_nutzer", {})
    if user_id not in nutzer:
        nutzer[user_id] = {
            "eingestempelt_seit": None,
            "gesamt_sekunden": 0,
            "anzahl": 0
        }
    return nutzer[user_id]

def format_dauer(sekunden) -> str:
    """Formatiert Sekunden als 'Xd Yh Zm' bzw. 'Yh Zm' / 'Zm'."""
    sekunden = max(0, int(sekunden))
    tage, rest = divmod(sekunden, 86400)
    stunden, rest = divmod(rest, 3600)
    minuten = rest // 60

    teile = []
    if tage:
        teile.append(f"{tage}d")
    if stunden or tage:
        teile.append(f"{stunden}h")
    teile.append(f"{minuten}m")
    return " ".join(teile)

def hat_stempel_manager_rolle(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.id == STEMPEL_MANAGER_ROLLE_ID for r in interaction.user.roles)

def build_stempel_embed():
    embed = discord.Embed(
        title="🛣️ Routenwache",
        description=(
            "Kurz und schmerzlos:\n"
            "🟢 **Rein** – du bist ab jetzt auf Route, die Zeit läuft.\n"
            "🔴 **Raus** – Feierabend, deine Zeit wird automatisch draufgerechnet.\n\n"
            f"Die Gesamtübersicht mit allen Zeiten gibt's in <#{data.get('channel_stempel_liste')}>.\n"
            "Und nicht vergessen wieder auszuchecken, sonst tickt die Uhr für immer weiter 😅"
        ),
        color=EMBED_COLOR
    )
    embed.set_footer(text="ECLIPSE")
    return embed

class StempelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="REIN", style=discord.ButtonStyle.success, custom_id="btn_stempel_ein")
    async def btn_ein(self, interaction: discord.Interaction, button: discord.ui.Button):
        eintrag = get_stempel_eintrag(str(interaction.user.id))
        if eintrag["eingestempelt_seit"] is not None:
            await interaction.response.send_message("❌ Du bist schon auf Route.", ephemeral=True)
            return
        eintrag["eingestempelt_seit"] = datetime.now(TIMEZONE).timestamp()
        save_data(data)
        await interaction.response.send_message("🟢 Bist drin. Viel Erfolg da draußen!", ephemeral=True)

    @discord.ui.button(label="RAUS", style=discord.ButtonStyle.danger, custom_id="btn_stempel_aus")
    async def btn_aus(self, interaction: discord.Interaction, button: discord.ui.Button):
        eintrag = get_stempel_eintrag(str(interaction.user.id))
        if eintrag["eingestempelt_seit"] is None:
            await interaction.response.send_message("❌ Du bist gerade gar nicht auf Route.", ephemeral=True)
            return

        dauer_sekunden = datetime.now(TIMEZONE).timestamp() - eintrag["eingestempelt_seit"]
        eintrag["gesamt_sekunden"] += dauer_sekunden
        eintrag["anzahl"] += 1
        eintrag["eingestempelt_seit"] = None
        save_data(data)

        await interaction.response.send_message(
            f"🔴 Feierabend! Diese Runde: **{format_dauer(dauer_sekunden)}**\n"
            f"Deine Gesamtzeit: **{format_dauer(eintrag['gesamt_sekunden'])}**",
            ephemeral=True
        )
        await update_stempel_liste(interaction.guild)

async def stempel_posten_intern(guild):
    if not data.get("channel_stempel"):
        return
    kanal = guild.get_channel(int(data["channel_stempel"]))
    if not kanal:
        return

    embed = build_stempel_embed()
    view  = StempelView()

    msg_id = data.get("stempel_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return
        except Exception as e:
            print(f"Alte Routenwache-Nachricht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed, view=view)
    data["stempel_nachricht_id"] = str(msg.id)
    save_data(data)

def build_stempel_liste_embed(guild):
    embed = discord.Embed(title="📊 Routenwache – Übersicht", color=EMBED_COLOR)

    eintraege = [
        (uid, info) for uid, info in data.get("stempel_nutzer", {}).items()
        if info.get("gesamt_sekunden", 0) > 0 or info.get("anzahl", 0) > 0
    ]
    eintraege.sort(key=lambda x: x[1]["gesamt_sekunden"], reverse=True)

    if not eintraege:
        embed.description = "*Noch keine Zeiten erfasst.*"
        embed.set_footer(text="ECLIPSE")
        embed.timestamp = datetime.now(TIMEZONE)
        return embed

    zeilen = []
    for platz, (uid, info) in enumerate(eintraege, start=1):
        member = guild.get_member(int(uid))
        name = member.mention if member else f"Unbekanntes Mitglied ({uid})"
        zeilen.append(
            f"{platz}. {name} ({format_dauer(info['gesamt_sekunden'])} – {info['anzahl']} Zeiträume)"
        )

    beschreibung = "\n".join(zeilen)
    if len(beschreibung) > 4000:
        beschreibung = beschreibung[:4000] + "\n…"
    embed.description = beschreibung
    embed.set_footer(text="ECLIPSE")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

async def update_stempel_liste(guild):
    if not data.get("channel_stempel_liste"):
        return
    kanal = guild.get_channel(int(data["channel_stempel_liste"]))
    if not kanal:
        return

    embed = build_stempel_liste_embed(guild)
    msg_id = data.get("stempel_liste_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed)
            return
        except Exception as e:
            print(f"Alte Routenwache-Übersicht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed)
    data["stempel_liste_nachricht_id"] = str(msg.id)
    save_data(data)

# ─── VERIFIZIERUNG (IC-Name, Nummer, Probewoche) ─────────────────────────────
# ─── ROLLEN NACH VERIFIZIERUNG ────────────────────────────────────────────────
ROLLEN_NACH_VERIFIZIERUNG = {
    "Probezeit":    1528430778587938886,
    "Homies":       1526202327365582918,
    "01 - Runner":  1526202327436886115,
    "Wochenabgabe": 1526202327365582916,
}
PROBEZEIT_ROLLE_ID = ROLLEN_NACH_VERIFIZIERUNG["Probezeit"]

class VerifizierungModal(discord.ui.Modal, title="Verifizierung: IC-Name & Nummer"):
    ic_name = discord.ui.TextInput(
        label="Dein In-Character Name", placeholder="Max Mustermann", required=True, max_length=32
    )
    ic_nummer = discord.ui.TextInput(
        label="Deine IC-Telefonnummer", placeholder="Einfach vom Handy kopieren", required=True, max_length=20
    )
    geworben_von = discord.ui.TextInput(
        label="Angeworben von (Optional)", placeholder="Name des Mitglieds das dich angeworben hat",
        required=False, max_length=32
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        verifizierungen = data.setdefault("verifizierungen", {})
        verifizierungen[uid] = {
            "ic_name": self.ic_name.value.strip(),
            "ic_nummer": self.ic_nummer.value.strip(),
            "geworben_von": self.geworben_von.value.strip() if self.geworben_von.value else None,
            "verifiziert_am": datetime.now(TIMEZONE).isoformat(),
            "erinnert": False
        }
        save_data(data)

        nicht_vergeben = []
        for name, rolle_id in ROLLEN_NACH_VERIFIZIERUNG.items():
            rolle = interaction.guild.get_role(rolle_id)
            if not rolle:
                nicht_vergeben.append(f"{name} (nicht gefunden)")
                continue
            try:
                await interaction.user.add_roles(rolle)
            except discord.Forbidden:
                nicht_vergeben.append(f"{name} (keine Berechtigung)")

        name_fehler = None
        try:
            await interaction.user.edit(nick=self.ic_name.value.strip())
        except discord.Forbidden:
            name_fehler = (
                "Servername konnte nicht geändert werden (keine Berechtigung – "
                "z.B. bei Server-Inhaber:innen oder wenn deine höchste Rolle über der Bot-Rolle liegt)."
            )
        except Exception as e:
            name_fehler = f"Servername konnte nicht geändert werden: {e}"

        antwort = "✅ Verifizierung abgeschlossen! Deine Probewoche beginnt jetzt."
        if nicht_vergeben:
            antwort += "\n⚠️ Diese Rollen konnten nicht vergeben werden: " + ", ".join(nicht_vergeben)
        if name_fehler:
            antwort += f"\n⚠️ {name_fehler}"
        await interaction.response.send_message(antwort, ephemeral=True)

        if data.get("channel_verifizierung_log"):
            log_kanal = interaction.guild.get_channel(int(data["channel_verifizierung_log"]))
            if log_kanal:
                embed = discord.Embed(title="Neue Verifizierung", color=EMBED_COLOR)
                embed.add_field(name="Discord",   value=interaction.user.mention,        inline=True)
                embed.add_field(name="IC-Name",   value=self.ic_name.value,              inline=True)
                embed.add_field(name="IC-Nummer", value=f"**{self.ic_nummer.value}**",   inline=True)
                if self.geworben_von.value:
                    embed.add_field(name="Angeworben von", value=f"**{self.geworben_von.value}**", inline=True)
                embed.set_footer(text="ECLIPSE")
                embed.timestamp = datetime.now(TIMEZONE)
                await log_kanal.send(embed=embed)

class VerifizierungButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verifizieren", style=discord.ButtonStyle.success, custom_id="btn_verifizierung_oeffnen")
    async def btn_verifizieren(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerifizierungModal())

async def verifizierung_posten_intern(guild):
    if not data.get("channel_verifizierung"):
        return
    kanal = guild.get_channel(int(data["channel_verifizierung"]))
    if not kanal:
        return

    embed = discord.Embed(
        title="Verifizierung",
        description="Klicke auf **Verifizieren**, um deinen IC-Namen und deine Nummer einzugeben. Danach beginnt deine Probewoche!",
        color=EMBED_COLOR
    )
    embed.set_footer(text="ECLIPSE")
    view = VerifizierungButtonView()

    msg_id = data.get("verifizierung_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            return
        except Exception as e:
            print(f"Alte Verifizierungs-Nachricht nicht gefunden, poste neu: {e}")

    msg = await kanal.send(embed=embed, view=view)
    data["verifizierung_nachricht_id"] = str(msg.id)
    save_data(data)

async def update_nachricht(guild):
    msg_id = data.get("aktuelle_nachricht_id")
    if not msg_id or not data.get("channel_aufstellung"):
        return
    kanal = guild.get_channel(int(data["channel_aufstellung"]))
    if not kanal:
        return
    try:
        msg        = await kanal.fetch_message(int(msg_id))
        mitglieder = await get_rolle_mitglieder(guild)
        datum      = data.get("aktuelles_datum", get_morgen_datum())
        embed      = build_embed(datum, mitglieder, data.get("eingefroren", False))
        await msg.edit(embed=embed)
    except Exception as e:
        print(f"Fehler beim Update der Nachricht: {e}")

# ─── ABMELDUNGS-ÜBERSICHT (persistente Liste) ────────────────────────────────
def build_abmeldung_liste_embed(guild):
    abmeldungen = data.get("abmeldungen", {})
    embed = discord.Embed(
        title="Abmeldungs-Übersicht",
        color=EMBED_COLOR
    )

    if not abmeldungen:
        embed.description = "*Aktuell ist niemand abgemeldet.*"
        embed.set_footer(text="ECLIPSE")
        embed.timestamp = datetime.now(TIMEZONE)
        return embed

    def sort_key(item):
        _, info = item
        parsed = parse_datum(info.get("bis", ""))
        return (parsed is None, parsed or datetime.max)

    sortierte_abmeldungen = sorted(abmeldungen.items(), key=sort_key)

    bloecke = []
    for uid, info in sortierte_abmeldungen:
        member  = guild.get_member(int(uid))
        name    = info.get("name") or (member.display_name if member else f"Unbekanntes Mitglied ({uid})")
        mention = member.mention if member else f"<@{uid}>"

        typ       = info.get("typ", "kurzzeit")
        typ_label = "🕐 Langzeit" if typ == "langzeit" else "📅 Kurzzeit"
        von       = info.get("von", "-")
        bis       = info.get("bis", "-")
        grund     = info.get("grund", "-")
        status    = "🟢 Aktiv" if ist_abmeldung_aktiv(info) else "⏳ Bevorstehend"

        block = (
            f"{mention}  ·  {typ_label}  ·  {status}\n"
            f"Name: **{name}**\n"
            f"Von: **{von}**  Bis: **{bis}**\n"
            f"Grund: {grund}"
        )
        bloecke.append(block)

    embed.description = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(bloecke)
    embed.set_footer(text="ECLIPSE")
    embed.timestamp = datetime.now(TIMEZONE)
    return embed

async def update_abmeldung_liste(guild):
    if not data.get("channel_abmeldung_liste"):
        return
    kanal = guild.get_channel(int(data["channel_abmeldung_liste"]))
    if not kanal:
        return
    embed  = build_abmeldung_liste_embed(guild)
    msg_id = data.get("abmeldung_liste_nachricht_id")
    if msg_id:
        try:
            msg = await kanal.fetch_message(int(msg_id))
            await msg.edit(embed=embed)
            return
        except Exception as e:
            print(f"Abmeldungs-Liste Nachricht nicht gefunden, poste neu: {e}")
    msg = await kanal.send(embed=embed)
    data["abmeldung_liste_nachricht_id"] = str(msg.id)
    save_data(data)

async def abgelaufene_abmeldungen_aufraeumen():
    """Entfernt alle Abmeldungen, deren 'Bis'-Datum bereits vergangen ist.
    Gibt die Liste der entfernten User-IDs zurück."""
    abmeldungen = data.get("abmeldungen", {})
    heute = datetime.now(TIMEZONE).date()
    entfernte_uids = []
    for uid, info in list(abmeldungen.items()):
        bis_datum = parse_datum(info.get("bis", ""))
        if bis_datum and bis_datum.date() < heute:
            del abmeldungen[uid]
            entfernte_uids.append(uid)
    if entfernte_uids:
        save_data(data)
    return entfernte_uids

# ─── ALTE AUFSTELLUNGS-NACHRICHT: VERZÖGERTE LÖSCHUNG (1h) ───────────────────
async def alte_aufstellungen_aufraeumen():
    """Löscht alte Aufstellungs-Nachrichten, deren geplante Löschzeit erreicht
    ist. Das Archiv (channel_archiv) wird hiervon NIE berührt, da dort eine
    komplett eigene Nachricht in einem eigenen Channel liegt."""
    geplante = data.get("geplante_aufstellung_loeschungen", [])
    if not geplante:
        return
    now = datetime.now(TIMEZONE)
    verbleibend = []
    for eintrag in geplante:
        try:
            faellig = datetime.fromisoformat(eintrag["loeschen_um"])
        except Exception:
            continue
        if now < faellig:
            verbleibend.append(eintrag)
            continue
        geloescht = False
        for guild in bot.guilds:
            kanal = guild.get_channel(int(eintrag["channel_id"]))
            if not kanal:
                continue
            try:
                msg = await kanal.fetch_message(int(eintrag["message_id"]))
                await msg.delete()
            except Exception:
                pass
            geloescht = True
            break
        if not geloescht:
            verbleibend.append(eintrag)
    if len(verbleibend) != len(geplante):
        data["geplante_aufstellung_loeschungen"] = verbleibend
        save_data(data)

# ─── NEUE ABSTIMMUNG POSTEN ───────────────────────────────────────────────────
async def neue_abstimmung_posten(guild, manual_channel=None, verwende_heute=False):
    if manual_channel:
        kanal = manual_channel
    elif data.get("channel_aufstellung"):
        kanal = guild.get_channel(int(data["channel_aufstellung"]))
    else:
        print("Kein Meet Up-Channel gesetzt! Bitte /set_aufstellung benutzen.")
        return

    if not kanal:
        print("Meet Up-Channel nicht gefunden!")
        return

    alte_msg_id = data.get("aktuelle_nachricht_id")
    if alte_msg_id:
        geplante = data.setdefault("geplante_aufstellung_loeschungen", [])
        geplante.append({
            "channel_id": kanal.id,
            "message_id": alte_msg_id,
            "loeschen_um": (datetime.now(TIMEZONE) + timedelta(hours=1)).isoformat()
        })
        save_data(data)

    datum = get_heute_datum() if verwende_heute else get_morgen_datum()
    ziel_zeitpunkt = datetime.now(TIMEZONE) if verwende_heute else (datetime.now(TIMEZONE) + timedelta(days=1))
    data["abstimmung"]          = {}
    data["eingefroren"]         = False
    data["aktuelles_datum"]     = datum
    data["aktueller_wochentag"] = ziel_zeitpunkt.weekday()

    mitglieder = await get_rolle_mitglieder(guild)

    embed = build_embed(datum, mitglieder, eingefroren=False)
    view  = AufstellungView()

    rolle_id = data.get("rolle_id")
    ping_text = None
    if rolle_id:
        rolle = guild.get_role(int(rolle_id))
        if rolle:
            ping_text = rolle.mention

    msg = await kanal.send(content=ping_text, embed=embed, view=view)
    data["aktuelle_nachricht_id"] = str(msg.id)
    save_data(data)
    print(f"Neue Abstimmung gepostet für {datum}")

# ─── ABSTIMMUNG EINFRIEREN & ARCHIVIEREN ─────────────────────────────────────
async def abstimmung_einfrieren(guild):
    data["eingefroren"] = True
    save_data(data)

    mitglieder = await get_rolle_mitglieder(guild)
    datum      = data.get("aktuelles_datum", get_heute_datum())

    if data.get("channel_aufstellung"):
        kanal  = guild.get_channel(int(data["channel_aufstellung"]))
        msg_id = data.get("aktuelle_nachricht_id")
        if kanal and msg_id:
            try:
                msg   = await kanal.fetch_message(int(msg_id))
                embed = build_embed(datum, mitglieder, eingefroren=True)
                await msg.edit(embed=embed, view=None)
            except Exception as e:
                print(f"Fehler beim Einfrieren: {e}")

    if data.get("channel_archiv"):
        archiv = guild.get_channel(int(data["channel_archiv"]))
        if archiv:
            embed_archiv       = build_embed(datum, mitglieder, eingefroren=True)
            embed_archiv.title = f"ARCHIV – {embed_archiv.title}"
            await archiv.send(embed=embed_archiv)
            print(f"Abstimmung archiviert für {datum}")

# ─── PROBEWOCHE-ERINNERUNG ────────────────────────────────────────────────────
async def check_probewoche_erinnerungen():
    if not data.get("channel_probewoche_erinnerung"):
        return
    verifizierungen = data.get("verifizierungen", {})
    if not verifizierungen:
        return

    now = datetime.now(TIMEZONE)
    geaendert = False
    for uid, info in verifizierungen.items():
        if info.get("erinnert"):
            continue
        try:
            verifiziert_am = datetime.fromisoformat(info["verifiziert_am"])
        except Exception:
            continue
        if now - verifiziert_am >= timedelta(days=7):
            for guild in bot.guilds:
                kanal = guild.get_channel(int(data["channel_probewoche_erinnerung"]))
                if kanal:
                    try:
                        await kanal.send(
                            f"⏰ **Probewoche abgelaufen:** <@{uid}> "
                            f"(IC-Name: **{info.get('ic_name', '-')}**) hat seine 7-tägige "
                            f"Probewoche beendet. Bitte prüfen und ggf. befördern."
                        )
                    except Exception as e:
                        print(f"Fehler beim Senden der Probewoche-Erinnerung: {e}")
            info["erinnert"] = True
            geaendert = True

    if geaendert:
        save_data(data)

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
    embed.set_footer(text="ECLIPSE")
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
    heutiger_wochentag  = now.weekday()
    morgiger_wochentag  = (now + timedelta(days=1)).weekday()

    OOC_HINWEIS_STUNDEN = (0, 4, 8, 12, 16, 20)
    if m == 0 and h in OOC_HINWEIS_STUNDEN:
        await ooc_hinweis_senden()

    entfernte = await abgelaufene_abmeldungen_aufraeumen()
    if entfernte:
        for guild in bot.guilds:
            if not data.get("eingefroren"):
                await update_nachricht(guild)
            await update_abmeldung_liste(guild)
        print(f"🧹 {len(entfernte)} abgelaufene Abmeldung(en) automatisch entfernt.")

    await alte_aufstellungen_aufraeumen()

    tage_config = data.get("aufstellung_tage_config", {})

    if h == 23 and m == 59:
        morgen_eintrag = tage_config.get(str(morgiger_wochentag), {})
        if morgen_eintrag.get("aktiv"):
            for guild in bot.guilds:
                await neue_abstimmung_posten(guild)
            await asyncio.sleep(61)

    heute_eintrag = tage_config.get(str(heutiger_wochentag), {})
    if heute_eintrag.get("aktiv") and not data.get("eingefroren", False):
        try:
            einfrier_h, einfrier_m = map(int, heute_eintrag.get("uhrzeit", "21:00").split(":"))
        except Exception:
            einfrier_h, einfrier_m = 21, 0
        if h == einfrier_h and m == einfrier_m:
            for guild in bot.guilds:
                await abstimmung_einfrieren(guild)
            await asyncio.sleep(61)

    await check_probewoche_erinnerungen()

# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────

@tree.command(name="setrolle", description="Setzt die Rolle die am Meet Up teilnimmt")
@app_commands.describe(rolle="Die Rolle die gepingt und abgestimmt werden soll")
@app_commands.checks.has_permissions(administrator=True)
async def setrolle(interaction: discord.Interaction, rolle: discord.Role):
    data["rolle_id"] = str(rolle.id)
    save_data(data)
    await interaction.response.send_message(
        f"✅ Rolle **{rolle.name}** wurde gesetzt.\n"
        f"Diese Rolle wird bei jeder Abstimmung gepingt.",
        ephemeral=True
    )

@tree.command(name="aufstellungstag", description="Aktiviert/deaktiviert einen Wochentag für das Meet Up und legt seine Uhrzeit fest")
@app_commands.describe(
    tag="Wochentag",
    aktiv="Soll an diesem Tag ein Meet Up sein?",
    uhrzeit="Uhrzeit im Format HH:MM (z.B. 19:30) – optional, wenn nur aktiv/inaktiv geändert wird"
)
@app_commands.choices(tag=[
    app_commands.Choice(name=n, value=str(i)) for i, n in enumerate(WOCHENTAGE_NAMEN)
])
@app_commands.checks.has_permissions(administrator=True)
async def aufstellungstag(interaction: discord.Interaction, tag: app_commands.Choice[str], aktiv: bool, uhrzeit: str = None):
    tag_key = tag.value
    config  = data.setdefault("aufstellung_tage_config", {})
    eintrag = config.get(tag_key, {"aktiv": False, "uhrzeit": "20:00"})
    eintrag["aktiv"] = aktiv

    if uhrzeit:
        uhrzeit = uhrzeit.strip()
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", uhrzeit):
            await interaction.response.send_message(
                "❌ Ungültiges Uhrzeit-Format. Bitte HH:MM verwenden, z.B. 19:30", ephemeral=True
            )
            return
        eintrag["uhrzeit"] = uhrzeit

    config[tag_key] = eintrag
    data["aufstellung_tage_config"] = config
    save_data(data)

    status = "**aktiv**" if aktiv else "**deaktiviert**"
    await interaction.response.send_message(
        f"✅ {WOCHENTAGE_NAMEN[int(tag_key)]}: {status}, Uhrzeit **{eintrag['uhrzeit']} Uhr**",
        ephemeral=True
    )

@tree.command(name="aufstellungstage", description="Zeigt die Konfiguration aller Wochentage")
@app_commands.checks.has_permissions(administrator=True)
async def aufstellungstage_uebersicht(interaction: discord.Interaction):
    config = data.get("aufstellung_tage_config", {})
    zeilen = []
    for i, name in enumerate(WOCHENTAGE_NAMEN):
        eintrag = config.get(str(i), {"aktiv": False, "uhrzeit": "20:00"})
        symbol  = "✅" if eintrag.get("aktiv") else "❌"
        zeilen.append(f"{symbol} **{name}** — {eintrag.get('uhrzeit', '20:00')} Uhr")
    await interaction.response.send_message("\n".join(zeilen), ephemeral=True)

@tree.command(name="set_aufstellung", description="Setzt den Channel für die Meet Up-Abstimmung")
@app_commands.describe(channel="Der Channel wo die Abstimmung gepostet wird")
@app_commands.checks.has_permissions(administrator=True)
async def set_aufstellung(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_aufstellung"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ Meet Up-Channel gesetzt: {channel.mention}", ephemeral=True
    )

@tree.command(name="set_archiv", description="Setzt den Channel für das Meet Up-Archiv")
@app_commands.describe(channel="Der Channel wo die archivierten Abstimmungen landen")
@app_commands.checks.has_permissions(administrator=True)
async def set_archiv(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_archiv"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ Archiv-Channel gesetzt: {channel.mention}", ephemeral=True
    )

@tree.command(name="set_abmeldung", description="Setzt den Channel für Abmeldungen")
@app_commands.describe(channel="Der Channel wo Abmeldungen gepostet werden")
@app_commands.checks.has_permissions(administrator=True)
async def set_abmeldung(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_abmeldung"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ Abmeldungs-Channel gesetzt: {channel.mention}", ephemeral=True
    )

@tree.command(name="set_abmeldung_liste", description="Setzt den Channel für die Abmeldungs-Übersicht (Live-Liste)")
@app_commands.describe(channel="Der Channel wo die aktuelle Übersicht aller Abmeldungen als Liste gepostet wird")
@app_commands.checks.has_permissions(administrator=True)
async def set_abmeldung_liste(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_abmeldung_liste"] = channel.id
    data["abmeldung_liste_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(
        f"✅ Abmeldungs-Übersicht-Channel gesetzt: {channel.mention}", ephemeral=True
    )
    await update_abmeldung_liste(interaction.guild)

@tree.command(name="set_abmeldung_button", description="Setzt den Channel für den Abmeldung-Button")
@app_commands.describe(channel="Der Channel wo der 'Abmeldung' Button gepostet wird")
@app_commands.checks.has_permissions(administrator=True)
async def set_abmeldung_button(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_abmeldung_button"] = channel.id
    data["abmeldung_button_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(
        f"✅ Abmeldung-Button-Channel gesetzt: {channel.mention}", ephemeral=True
    )
    await abmeldung_button_posten_intern(interaction.guild)

@tree.command(name="abmeldung_button_posten", description="Postet oder aktualisiert die Abmeldung-Button-Nachricht")
@app_commands.checks.has_permissions(administrator=True)
async def abmeldung_button_posten(interaction: discord.Interaction):
    if not data.get("channel_abmeldung_button"):
        await interaction.response.send_message(
            "❌ Kein Channel gesetzt!\nBitte zuerst **/set_abmeldung_button #channel** benutzen.",
            ephemeral=True
        )
        return
    await abmeldung_button_posten_intern(interaction.guild)
    await interaction.response.send_message("✅ Abmeldung-Button-Nachricht gepostet/aktualisiert.", ephemeral=True)

@tree.command(name="set_verifizierung_channel", description="Setzt den Channel für die Verifizierungs-Nachricht (Button)")
@app_commands.describe(channel="Der Channel wo neue Mitglieder sich verifizieren")
@app_commands.checks.has_permissions(administrator=True)
async def set_verifizierung_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_verifizierung"] = channel.id
    data["verifizierung_nachricht_id"] = None
    save_data(data)
    await interaction.response.send_message(f"✅ Verifizierungs-Channel gesetzt: {channel.mention}", ephemeral=True)
    await verifizierung_posten_intern(interaction.guild)

@tree.command(name="verifizierung_posten", description="Postet oder aktualisiert die Verifizierungs-Nachricht")
@app_commands.checks.has_permissions(administrator=True)
async def verifizierung_posten(interaction: discord.Interaction):
    if not data.get("channel_verifizierung"):
        await interaction.response.send_message(
            "❌ Kein Channel gesetzt!\nBitte zuerst **/set_verifizierung_channel #channel** benutzen.",
            ephemeral=True
        )
        return
    await verifizierung_posten_intern(interaction.guild)
    await interaction.response.send_message("✅ Verifizierungs-Nachricht gepostet/aktualisiert.", ephemeral=True)

@tree.command(name="set_verifizierung_log", description="Setzt den Channel für das Verifizierungs-Log")
@app_commands.describe(channel="Der Channel wo jede neue Verifizierung protokolliert wird")
@app_commands.checks.has_permissions(administrator=True)
async def set_verifizierung_log(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_verifizierung_log"] = channel.id
    save_data(data)
    await interaction.response.send_message(f"✅ Verifizierungs-Log-Channel gesetzt: {channel.mention}", ephemeral=True)

@tree.command(name="probezeit_beenden", description="Beendet die Probezeit eines Mitglieds vorzeitig")
@app_commands.describe(mitglied="Das Mitglied dessen Probezeit vorzeitig beendet wird")
@app_commands.checks.has_permissions(administrator=True)
async def probezeit_beenden(interaction: discord.Interaction, mitglied: discord.Member):
    rolle = interaction.guild.get_role(PROBEZEIT_ROLLE_ID)
    entfernt = False
    if rolle and rolle in mitglied.roles:
        try:
            await mitglied.remove_roles(rolle)
            entfernt = True
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Ich habe keine Berechtigung, die Probezeit-Rolle zu entfernen.", ephemeral=True
            )
            return

    uid = str(mitglied.id)
    verifizierungen = data.setdefault("verifizierungen", {})
    if uid in verifizierungen:
        verifizierungen[uid]["erinnert"] = True
        save_data(data)

    if entfernt:
        await interaction.response.send_message(
            f"✅ Probezeit von **{mitglied.display_name}** vorzeitig beendet (Rolle entfernt).", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"ℹ️ **{mitglied.display_name}** hatte keine Probezeit-Rolle mehr, Eintrag trotzdem als beendet markiert.",
            ephemeral=True
        )

@tree.command(name="set_probewoche_channel", description="Setzt den Channel für die automatische Probewoche-Erinnerung nach 7 Tagen")
@app_commands.describe(channel="Der Channel wo die Erinnerung gepostet wird")
@app_commands.checks.has_permissions(administrator=True)
async def set_probewoche_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_probewoche_erinnerung"] = channel.id
    save_data(data)
    await interaction.response.send_message(f"✅ Probewoche-Erinnerungs-Channel gesetzt: {channel.mention}", ephemeral=True)

@tree.command(name="set_chat", description="Setzt den Channel für den stündlichen OOC-Regelhinweis")
@app_commands.describe(channel="Der Channel wo stündlich der OOC-Regelhinweis gepostet wird")
@app_commands.checks.has_permissions(administrator=True)
async def set_chat(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_chat_hinweis"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ OOC-Regelhinweis-Channel gesetzt: {channel.mention}\nAb jetzt wird dort stündlich der Hinweis gepostet.",
        ephemeral=True
    )
    await ooc_hinweis_senden()

@tree.command(name="channels", description="Zeigt alle aktuell gesetzten Channels und die Rolle")
@app_commands.checks.has_permissions(administrator=True)
async def channels_info(interaction: discord.Interaction):
    auf    = interaction.guild.get_channel(int(data["channel_aufstellung"]))       if data.get("channel_aufstellung")       else None
    arch   = interaction.guild.get_channel(int(data["channel_archiv"]))            if data.get("channel_archiv")            else None
    abm    = interaction.guild.get_channel(int(data["channel_abmeldung"]))         if data.get("channel_abmeldung")         else None
    liste  = interaction.guild.get_channel(int(data["channel_abmeldung_liste"]))   if data.get("channel_abmeldung_liste")   else None
    button = interaction.guild.get_channel(int(data["channel_abmeldung_button"]))  if data.get("channel_abmeldung_button")  else None
    verif  = interaction.guild.get_channel(int(data["channel_verifizierung"]))     if data.get("channel_verifizierung")     else None
    vlog   = interaction.guild.get_channel(int(data["channel_verifizierung_log"])) if data.get("channel_verifizierung_log") else None
    probe_ch = interaction.guild.get_channel(int(data["channel_probewoche_erinnerung"])) if data.get("channel_probewoche_erinnerung") else None
    chat_ch  = interaction.guild.get_channel(int(data["channel_chat_hinweis"])) if data.get("channel_chat_hinweis") else None
    stempel_ch = interaction.guild.get_channel(int(data["channel_stempel"])) if data.get("channel_stempel") else None
    stempel_liste_ch = interaction.guild.get_channel(int(data["channel_stempel_liste"])) if data.get("channel_stempel_liste") else None
    rolle_id = data.get("rolle_id")
    rolle = interaction.guild.get_role(int(rolle_id)) if rolle_id else None
    verif_rollen_status = []
    for name, rid in ROLLEN_NACH_VERIFIZIERUNG.items():
        r = interaction.guild.get_role(rid)
        verif_rollen_status.append(f"{name}: {r.mention if r else '❌ nicht gefunden'}")

    config = data.get("aufstellung_tage_config", {})
    aktive_tage = [WOCHENTAGE_NAMEN[i] for i in range(7) if config.get(str(i), {}).get("aktiv")]
    tage_text = ", ".join(aktive_tage) if aktive_tage else "Keine (nutze /aufstellungstag)"

    await interaction.response.send_message(
        f"**Aktuelle Einstellungen:**\n\n"
        f"Rolle:                 {rolle.mention        if rolle        else '❌ Nicht gesetzt – /setrolle benutzen'}\n"
        f"Meet Up:               {auf.mention          if auf          else '❌ Nicht gesetzt – /set_aufstellung benutzen'}\n"
        f"Archiv:                {arch.mention         if arch         else '❌ Nicht gesetzt – /set_archiv benutzen'}\n"
        f"Abmeldung (Log):       {abm.mention          if abm          else '❌ Nicht gesetzt – /set_abmeldung benutzen'}\n"
        f"Abmeldungs-Liste:      {liste.mention        if liste        else '❌ Nicht gesetzt – /set_abmeldung_liste benutzen'}\n"
        f"Abmeldung-Button:      {button.mention       if button       else '❌ Nicht gesetzt – /set_abmeldung_button benutzen'}\n\n"
        f"Aktive Meet Up-Tage: **{tage_text}**\n"
        f"(Details: /aufstellungstage)\n\n"
        f"Verifizierung-Channel: {verif.mention        if verif        else '❌ Nicht gesetzt – /set_verifizierung_channel benutzen'}\n"
        f"Verifizierung-Log:     {vlog.mention         if vlog         else '❌ Nicht gesetzt – /set_verifizierung_log benutzen'}\n"
        f"Rollen nach Verify:    {' | '.join(verif_rollen_status)}\n"
        f"Probewoche-Erinnerung: {probe_ch.mention     if probe_ch     else '❌ Nicht gesetzt – /set_probewoche_channel benutzen'}\n"
        f"OOC-Regelhinweis:      {chat_ch.mention      if chat_ch      else '❌ Nicht gesetzt – /set_chat benutzen'}\n\n"
        f"Routenwache:           {stempel_ch.mention        if stempel_ch        else '❌ Nicht gesetzt – /stempel_posten benutzen'}\n"
        f"Routenwache-Übersicht: {stempel_liste_ch.mention  if stempel_liste_ch  else '❌ Nicht gesetzt'}",
        ephemeral=True
    )

@tree.command(name="abstimmung", description="Postet manuell eine neue Meet Up-Abstimmung")
@app_commands.describe(datum="Für welchen Tag gilt das Meet Up? (Standard: Heute)")
@app_commands.choices(datum=[
    app_commands.Choice(name="Heute", value="heute"),
    app_commands.Choice(name="Morgen", value="morgen"),
])
@app_commands.checks.has_permissions(administrator=True)
async def abstimmung_manuell(interaction: discord.Interaction, datum: app_commands.Choice[str] = None):
    if not data.get("channel_aufstellung"):
        await interaction.response.send_message(
            "❌ Kein Meet Up-Channel gesetzt!\nBitte zuerst **/set_aufstellung #channel** benutzen.",
            ephemeral=True
        )
        return
    verwende_heute = (datum is None) or (datum.value == "heute")
    await interaction.response.send_message("Erstelle neue Abstimmung...", ephemeral=True)
    await neue_abstimmung_posten(interaction.guild, verwende_heute=verwende_heute)
    await interaction.edit_original_response(content="✅ Neue Abstimmung wurde gepostet!")

@tree.command(name="status", description="Zeigt den aktuellen Abstimmungsstand")
@app_commands.checks.has_permissions(administrator=True)
async def status(interaction: discord.Interaction):
    mitglieder = await get_rolle_mitglieder(interaction.guild)
    datum      = data.get("aktuelles_datum", get_morgen_datum())
    embed      = build_embed(datum, mitglieder, data.get("eingefroren", False))
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="abmelden", description="Melde dich vom Meet Up ab")
@app_commands.describe(
    von="Von wann? (TT.MM.JJJJ, z.B. 14.07.2026)",
    bis="Bis wann? (TT.MM.JJJJ, z.B. 16.07.2026)",
    grund="Grund (intern, nicht öffentlich sichtbar)"
)
async def abmelden(interaction: discord.Interaction, von: str, bis: str, grund: str):
    rolle_id = data.get("rolle_id")
    if rolle_id:
        rolle = interaction.guild.get_role(int(rolle_id))
        if rolle and rolle not in interaction.user.roles:
            await interaction.response.send_message(
                "Du hast keine Berechtigung zur Abmeldung.", ephemeral=True
            )
            return

    von_datum = parse_datum(von)
    bis_datum = parse_datum(bis)
    if not von_datum or not bis_datum:
        await interaction.response.send_message(
            "❌ Ungültiges Datumsformat. Bitte **TT.MM.JJJJ** verwenden, z.B. `14.07.2026`.", ephemeral=True
        )
        return
    if bis_datum < von_datum:
        await interaction.response.send_message(
            "❌ Das Enddatum darf nicht vor dem Startdatum liegen.", ephemeral=True
        )
        return
    von, bis = von_datum.strftime("%d.%m.%Y"), bis_datum.strftime("%d.%m.%Y")

    uid = str(interaction.user.id)
    data["abmeldungen"][uid] = {"von": von, "bis": bis, "grund": grund, "typ": "kurzzeit"}
    save_data(data)

    if not data.get("eingefroren"):
        await update_nachricht(interaction.guild)
    await update_abmeldung_liste(interaction.guild)

    aktiv_hinweis = "" if ist_abmeldung_aktiv(data["abmeldungen"][uid]) else \
        "\nℹ️ Dein Zeitraum beginnt erst später – bis dahin wirst du weiterhin normal im Meet Up geführt und kannst abstimmen."
    await interaction.response.send_message(
        f"✅ Abmeldung eingetragen!\n"
        f"Von: **{von}**\n"
        f"Bis: **{bis}**{aktiv_hinweis}",
        ephemeral=True
    )

    if data.get("channel_abmeldung"):
        abm_kanal = interaction.guild.get_channel(int(data["channel_abmeldung"]))
        if abm_kanal:
            embed_abm = discord.Embed(title="Neue Abmeldung", color=EMBED_COLOR)
            embed_abm.add_field(name="Mitglied", value=interaction.user.mention, inline=True)
            embed_abm.add_field(name="Von",      value=von,                      inline=True)
            embed_abm.add_field(name="Bis",      value=bis,                      inline=True)
            embed_abm.add_field(name="Grund",    value=grund,                    inline=False)
            embed_abm.set_footer(text="ECLIPSE")
            embed_abm.timestamp = datetime.now(TIMEZONE)
            await abm_kanal.send(embed=embed_abm)

@tree.command(name="abmeldung_langzeit", description="Trägt eine Langzeit-Abmeldung ein (Zeitraum länger als eine Woche)")
@app_commands.describe(
    von="Von wann? (TT.MM.JJJJ, z.B. 14.07.2026)",
    bis="Bis wann? (TT.MM.JJJJ, z.B. 25.08.2026)",
    grund="Grund der Langzeit-Abmeldung"
)
async def abmeldung_langzeit(interaction: discord.Interaction, von: str, bis: str, grund: str):
    rolle_id = data.get("rolle_id")
    if rolle_id:
        rolle = interaction.guild.get_role(int(rolle_id))
        if rolle and rolle not in interaction.user.roles:
            await interaction.response.send_message(
                "Du hast keine Berechtigung zur Abmeldung.", ephemeral=True
            )
            return

    von_datum = parse_datum(von)
    bis_datum = parse_datum(bis)
    if not von_datum or not bis_datum:
        await interaction.response.send_message(
            "❌ Ungültiges Datumsformat. Bitte **TT.MM.JJJJ** verwenden, z.B. `14.07.2026`.", ephemeral=True
        )
        return
    if bis_datum < von_datum:
        await interaction.response.send_message(
            "❌ Das Enddatum darf nicht vor dem Startdatum liegen.", ephemeral=True
        )
        return
    von, bis = von_datum.strftime("%d.%m.%Y"), bis_datum.strftime("%d.%m.%Y")

    uid = str(interaction.user.id)
    data["abmeldungen"][uid] = {"von": von, "bis": bis, "grund": grund, "typ": "langzeit"}
    save_data(data)

    if not data.get("eingefroren"):
        await update_nachricht(interaction.guild)
    await update_abmeldung_liste(interaction.guild)

    aktiv_hinweis = "" if ist_abmeldung_aktiv(data["abmeldungen"][uid]) else \
        "\nℹ️ Dein Zeitraum beginnt erst später – bis dahin wirst du weiterhin normal im Meet Up geführt und kannst abstimmen."
    await interaction.response.send_message(
        f"✅ Langzeit-Abmeldung eingetragen!\n"
        f"Von: **{von}**\n"
        f"Bis: **{bis}**{aktiv_hinweis}",
        ephemeral=True
    )

    if data.get("channel_abmeldung"):
        abm_kanal = interaction.guild.get_channel(int(data["channel_abmeldung"]))
        if abm_kanal:
            embed_abm = discord.Embed(title="Neue Langzeit-Abmeldung", color=EMBED_COLOR)
            embed_abm.add_field(name="Mitglied", value=interaction.user.mention, inline=True)
            embed_abm.add_field(name="Von",      value=von,                      inline=True)
            embed_abm.add_field(name="Bis",      value=bis,                      inline=True)
            embed_abm.add_field(name="Grund",    value=grund,                    inline=False)
            embed_abm.set_footer(text="ECLIPSE")
            embed_abm.timestamp = datetime.now(TIMEZONE)
            await abm_kanal.send(embed=embed_abm)

@tree.command(name="abmeldung_loeschen", description="Entfernt die Abmeldung eines Mitglieds")
@app_commands.describe(mitglied="Das Mitglied dessen Abmeldung entfernt werden soll")
@app_commands.checks.has_permissions(administrator=True)
async def abmeldung_loeschen(interaction: discord.Interaction, mitglied: discord.Member):
    uid = str(mitglied.id)
    if uid in data["abmeldungen"]:
        del data["abmeldungen"][uid]
        save_data(data)
        await update_nachricht(interaction.guild)
        await update_abmeldung_liste(interaction.guild)
        await interaction.response.send_message(
            f"✅ Abmeldung von **{mitglied.display_name}** entfernt.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ **{mitglied.display_name}** hat keine aktive Abmeldung.", ephemeral=True
        )

# ─── ROUTENWACHE SLASH COMMANDS ───────────────────────────────────────────────

@tree.command(name="stempel_posten", description="Postet oder aktualisiert die Routenwache-Nachricht (Rein/Raus-Buttons)")
@app_commands.checks.has_permissions(administrator=True)
async def stempel_posten(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await stempel_posten_intern(interaction.guild)
    await update_stempel_liste(interaction.guild)
    await interaction.followup.send("✅ Routenwache-Nachricht gepostet/aktualisiert.", ephemeral=True)

@tree.command(name="zeit_hinzufuegen", description="Trägt manuell Zeit für ein Mitglied nach")
@app_commands.describe(
    mitglied="Das Mitglied, dem Zeit gutgeschrieben werden soll",
    stunden="Anzahl Stunden (optional)",
    minuten="Anzahl Minuten (optional)",
    datum="Datum, für das die Zeit gilt, z.B. 27.07.2026 (nur zur Dokumentation)"
)
@app_commands.check(hat_stempel_manager_rolle)
async def zeit_hinzufuegen(interaction: discord.Interaction, mitglied: discord.Member, stunden: int = 0, minuten: int = 0, datum: str = None):
    if stunden <= 0 and minuten <= 0:
        await interaction.response.send_message("❌ Bitte Stunden und/oder Minuten angeben.", ephemeral=True)
        return
    if stunden < 0 or minuten < 0:
        await interaction.response.send_message("❌ Stunden/Minuten dürfen nicht negativ sein.", ephemeral=True)
        return

    sekunden = stunden * 3600 + minuten * 60
    eintrag = get_stempel_eintrag(str(mitglied.id))
    eintrag["gesamt_sekunden"] += sekunden
    eintrag["anzahl"] += 1
    save_data(data)

    await update_stempel_liste(interaction.guild)

    datum_text = f" (Datum: {datum})" if datum else ""
    await interaction.response.send_message(
        f"✅ {mitglied.mention} wurden **{format_dauer(sekunden)}** gutgeschrieben{datum_text}.\n"
        f"Neue Gesamtzeit: **{format_dauer(eintrag['gesamt_sekunden'])}**",
        ephemeral=True
    )

@tree.command(name="zeit_entfernen", description="Zieht manuell Zeit von einem Mitglied ab")
@app_commands.describe(
    mitglied="Das Mitglied, dem Zeit abgezogen werden soll",
    stunden="Anzahl Stunden (optional)",
    minuten="Anzahl Minuten (optional)",
    datum="Datum, für das die Zeit gilt, z.B. 27.07.2026 (nur zur Dokumentation)"
)
@app_commands.check(hat_stempel_manager_rolle)
async def zeit_entfernen(interaction: discord.Interaction, mitglied: discord.Member, stunden: int = 0, minuten: int = 0, datum: str = None):
    if stunden <= 0 and minuten <= 0:
        await interaction.response.send_message("❌ Bitte Stunden und/oder Minuten angeben.", ephemeral=True)
        return
    if stunden < 0 or minuten < 0:
        await interaction.response.send_message("❌ Stunden/Minuten dürfen nicht negativ sein.", ephemeral=True)
        return

    sekunden = stunden * 3600 + minuten * 60
    eintrag = get_stempel_eintrag(str(mitglied.id))
    eintrag["gesamt_sekunden"] = max(0, eintrag["gesamt_sekunden"] - sekunden)
    eintrag["anzahl"] = max(0, eintrag["anzahl"] - 1)
    save_data(data)

    await update_stempel_liste(interaction.guild)

    datum_text = f" (Datum: {datum})" if datum else ""
    await interaction.response.send_message(
        f"✅ {mitglied.mention} wurden **{format_dauer(sekunden)}** abgezogen{datum_text}.\n"
        f"Neue Gesamtzeit: **{format_dauer(eintrag['gesamt_sekunden'])}**",
        ephemeral=True
    )

@tree.command(name="meine_zeit", description="Zeigt deinen eigenen Routenwache-Status")
async def meine_zeit(interaction: discord.Interaction):
    eintrag = get_stempel_eintrag(str(interaction.user.id))
    save_data(data)
    status_text = "🟢 gerade auf Route" if eintrag["eingestempelt_seit"] else "🔴 gerade nicht auf Route"
    await interaction.response.send_message(
        f"**Deine Routenwache**\n"
        f"Status: {status_text}\n"
        f"Gesamtzeit: **{format_dauer(eintrag['gesamt_sekunden'])}**\n"
        f"Zeiträume: **{eintrag['anzahl']}**",
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

    bot.add_view(AufstellungView())
    bot.add_view(AbmeldungButtonView())
    bot.add_view(VerifizierungButtonView())
    bot.add_view(StempelView())

    for guild in bot.guilds:
        try:
            if data.get("channel_verifizierung") and not data.get("verifizierung_nachricht_id"):
                await verifizierung_posten_intern(guild)
                print("✅ Verifizierungs-Nachricht nachträglich gepostet.")
            if data.get("channel_abmeldung_button") and not data.get("abmeldung_button_nachricht_id"):
                await abmeldung_button_posten_intern(guild)
                print("✅ Abmeldung-Button-Nachricht nachträglich gepostet.")
            if data.get("channel_abmeldung_liste"):
                await update_abmeldung_liste(guild)
                print("✅ Abmeldungs-Übersicht nachträglich gepostet/aktualisiert.")
            if data.get("channel_stempel") and not data.get("stempel_nachricht_id"):
                await stempel_posten_intern(guild)
                print("✅ Routenwache-Nachricht nachträglich gepostet.")
            if data.get("channel_stempel_liste"):
                await update_stempel_liste(guild)
                print("✅ Routenwache-Übersicht nachträglich gepostet/aktualisiert.")
        except Exception as e:
            print(f"❌ Fehler beim Auto-Posten fehlender Nachrichten: {e}")

    check_zeit.start()
    print("Tasks gestartet. Bot ist bereit!")

@bot.event
async def on_message(message: discord.Message):
    # Eigene Nachrichten des Bots (z.B. seine eigenen Antworten) ignorieren,
    # damit er sich nicht selbst triggert.
    if message.author.bot:
        await bot.process_commands(message)
        return

    if message.guild is not None:
        # Server-Nachricht: reagiert jetzt auf JEDE Beleidigung (nicht mehr
        # nur, wenn zusätzlich "bot" im Text vorkommt) und schickt eine DM
        # mit einer von Claude live generierten, kalten/mysteriösen Antwort,
        # die sich tatsächlich auf das Gesagte bezieht.
        if ist_beleidigung(message.content):
            antwort = await hole_ki_antwort(
                str(message.author.id), message.content, beleidigung=True
            )
            try:
                await message.author.send(antwort)
            except discord.Forbidden:
                # DMs für diesen Server/User deaktiviert – nichts zu machen.
                pass
    else:
        # Direktnachricht AN den Bot: vorher wurde das komplett ignoriert
        # (der obige Block griff nur bei message.guild is not None), der
        # Bot hat also nie in DMs geantwortet. Jetzt liest er, was
        # geschrieben wurde (Frage, Kommentar, Beleidigung, ...) und lässt
        # sich davon eine passende, in Charakter formulierte Antwort geben,
        # statt eine feste Phrase aus einer Liste zu ziehen.
        async with message.channel.typing():
            antwort = await hole_ki_antwort(str(message.author.id), message.content)
        try:
            await message.channel.send(antwort)
        except discord.Forbidden:
            pass

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
        except:
            pass

# ─── START ────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
