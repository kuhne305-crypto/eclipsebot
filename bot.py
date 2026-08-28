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
# Kostenloser Gemini-API-Key von https://aistudio.google.com/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

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

# ─── ANTWORT-PROTOKOLL ────────────────────────────────────────────────────────
# Das Modell bekommt den Verlauf mit durchnummerierten Zeilen ("[12] Name: Text")
# gezeigt und kann in seiner Antwort selbst wählen, WIE es reagiert:
#   KEIN_KOMMENTAR            -> gar nichts tun
#   EMOJI:<Nummer>:<Emoji>    -> nur mit einem Emoji auf eine bestimmte Nachricht reagieren
#   REPLY:<Nummer>:<Text>     -> als Discord-Reply auf eine bestimmte Nachricht antworten
#                                 (die Person wird dabei automatisch markiert/gepingt)
#   reiner Text ohne Präfix   -> ein allgemeiner Kommentar in den Channel, ohne Ping
# Das erlaubt abgestufte, natürlichere Reaktionen statt immer einer vollen
# Text-Antwort an den ganzen Channel.

# ─── STILPROFIL-LERNEN ────────────────────────────────────────────────────────
# Wie viele echte Nutzer-Nachrichten (unabhängig vom Kanal-Modus) pro Channel
# gesammelt werden, aus denen das Stilprofil abgeleitet wird.
STIL_SAMMLUNG_LIMIT = 300
# Nach so vielen NEUEN Nutzer-Nachrichten seit dem letzten Update wird das
# Stilprofil automatisch neu erstellt.
STIL_UPDATE_SCHWELLE = 120
# Ab wie vielen gesammelten Nachrichten überhaupt ein erstes Profil erstellt wird.
STIL_MINDEST_NACHRICHTEN = 30

STIL_SYSTEM_PROMPT = (
    "Du analysierst einen Discord-Chatverlauf und beschreibst NUR den "
    "Schreibstil der Personen darin – keine Inhalte, keine Bewertung, keine "
    "Meinung zu den Personen. Achte auf: typische Wortwahl, Slang/Jargon, "
    "Abkürzungen, Anglizismen, Emoji-Nutzung (welche, wie oft), "
    "Groß-/Kleinschreibung, Satzlänge, Interpunktion, wiederkehrende "
    "Sprüche/Insider-Witze/Running Gags, Anredeformen. Fasse das in "
    "maximal 120 Wörtern als Stichpunkte oder kurzen Fließtext zusammen, "
    "auf Deutsch. Antworte NUR mit der Stilbeschreibung, ohne Einleitung "
    "wie 'Hier ist...'."
)

DEFAULT_SYSTEM_PROMPT_BEOBACHTUNG = (
    "Du bist ein Discord-Bot, der eine Weile im Channel mitliest und dann "
    "selbst entscheidet, OB und WIE er auf das Geschehen reagiert – wie ein "
    "Mitglied des Servers, das mitgelesen hat und ab und zu etwas beiträgt, "
    "nicht wie ein Assistent, der jede Nachricht beantworten muss.\n\n"
    "Der Chatverlauf wird dir mit Nummern gezeigt, Format: [Nummer] Name: "
    "Nachricht.\n\n"
    "ENTSCHEIDE ZUERST, OB du überhaupt reagierst:\n"
    "- Ist die Stimmung/das Thema gerade ernst (z.B. Streit, echtes "
    "persönliches Problem, Trauer, wichtige organisatorische Absprache, "
    "Ärger)? -> Halte dich komplett raus, antworte NUR mit "
    f"{SKIP_TOKEN}.\n"
    "- Ist die Stimmung locker, lustig, albern, oder gibt es sonst etwas, "
    "wozu ein kurzer Beitrag passen würde? -> Beteilige dich ruhig, mit "
    "Humor, einer eigenen Meinung oder einem Bezug auf das Gesagte. Du "
    "musst nicht auf den perfekten Moment warten, ein lockerer kurzer "
    "Kommentar reicht oft schon.\n"
    "- Du musst nicht auf jede Nachricht reagieren – wähle bewusst, ob "
    "sich gerade überhaupt ein Beitrag lohnt.\n\n"
    "WENN du reagierst, WÄHLE die passende Form (genau eine, exakt in "
    "diesem Format antworten):\n"
    "1. EMOJI:<Nummer>:<Emoji> – eine reine Emoji-Reaktion auf eine "
    "bestimmte Nachricht, z.B. EMOJI:14:😂. Passt, wenn eine Nachricht "
    "witzig/krass/süß etc. ist, aber keinen eigenen Text-Kommentar "
    "braucht. Nutze nur normale Unicode-Emojis.\n"
    "2. REPLY:<Nummer>:<Text> – eine Text-Antwort DIREKT auf eine "
    "bestimmte Nachricht einer bestimmten Person (die Person wird dabei "
    "automatisch markiert/gepingt). Passt, wenn sich dein Kommentar klar "
    "auf eine bestimmte Aussage bezieht. Orientiere dich beim Schreiben "
    "ein bisschen an der Art, wie diese Person schreibt.\n"
    "3. Reiner Text ohne Präfix – ein allgemeiner Kommentar zum Gespräch "
    "insgesamt, an niemand Bestimmten gerichtet (keine Markierung).\n\n"
    "Bei Text (Form 2 oder 3): auf Deutsch, kurz und natürlich wie eine "
    "echte Discord-Nachricht (meist 1-3 Sätze, keine langen Absätze, keine "
    "Aufzählungen außer es passt wirklich), Emojis nicht inflationär "
    "nutzen. Sprich niemals darüber, dass du ein Sprachmodell oder eine KI "
    "bist, außer man fragt dich direkt danach.\n\n"
    f"Antworte NUR mit {SKIP_TOKEN} und sonst nichts, wenn wirklich nichts "
    "im Verlauf steht, worauf man reagieren könnte, ODER wenn das Thema "
    "gerade zu ernst dafür ist."
)

DEFAULT_SYSTEM_PROMPT_DIREKT = (
    "Du bist ein Discord-Bot und wirst gerade direkt angesprochen "
    "(Erwähnung oder Reply auf dich). Der Chatverlauf wird dir mit Nummern "
    "gezeigt, Format: [Nummer] Name: Nachricht – die letzte Nachricht ist "
    "die, die dich anspricht.\n\n"
    "Reagiere passend zur Stimmung: lustig/locker bei albernem Kontext, "
    "ruhig/konkret bei ernstem oder sachlichem Kontext. Da du direkt "
    "angesprochen wirst, antworte so gut wie immer irgendwie – nur bei "
    f"wirklich sehr ernsten/heiklen Momenten ist es ok, dich mit "
    f"{SKIP_TOKEN} rauszuhalten.\n\n"
    "WÄHLE die passende Antwortform (genau eine, exakt in diesem "
    "Format):\n"
    "1. EMOJI:<Nummer>:<Emoji> – wenn eine reine Emoji-Reaktion auf die "
    "ansprechende Nachricht völlig reicht (z.B. bei einer einfachen "
    "Frage, einem Gruß, einem Daumen-hoch-Moment). Nur normale "
    "Unicode-Emojis.\n"
    "2. REPLY:<Nummer>:<Text> – eine Text-Antwort direkt auf die "
    "ansprechende Nachricht (die Person wird automatisch markiert). Das "
    "ist im Zweifel die Standard-Form für direkte Ansprache. Orientiere "
    "dich beim Schreiben ein bisschen an der Art, wie diese Person "
    "schreibt.\n"
    "3. Reiner Text ohne Präfix – falls kein klarer Bezug zu einer "
    "einzelnen Nachricht besteht.\n\n"
    "Bei Text: auf Deutsch, kurz und natürlich wie eine echte "
    "Discord-Nachricht (meist 1-3 Sätze). Emojis nicht inflationär "
    "nutzen. Sprich niemals darüber, dass du ein Sprachmodell oder eine "
    "KI bist, außer man fragt dich direkt danach."
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
        # {channel_id_str: "Beschreibung des Schreibstils..."}
        "stilprofile": {},
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
# Fortlaufender Nummerierungs-Zähler pro Channel, damit das Modell einzelne
# Verlaufszeilen eindeutig referenzieren kann (für EMOJI:/REPLY:-Antworten).
naechster_index = defaultdict(int)
# Ordnet Verlaufs-Indizes den echten discord.Message-Objekten zu, damit auf
# eine bestimmte Nachricht reagiert (Emoji) oder geantwortet (Reply/Ping)
# werden kann. Gleiche Größe wie das Kurzzeitgedächtnis.
nachricht_lookup = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
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

# Rein für das Stilprofil-Lernen: Sammlung echter Nutzer-Nachrichten pro
# Channel (unabhängig vom Kanal-Modus, unabhängig vom Kurzzeitgedächtnis oben,
# das nur begrenzt Kontext für einzelne Antworten hält).
stil_sammlung = defaultdict(lambda: deque(maxlen=STIL_SAMMLUNG_LIMIT))
neue_seit_stil_update = defaultdict(int)
stil_update_laeuft = defaultdict(bool)

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

def system_prompt_mit_stil(basis_prompt: str, channel_id: int) -> str:
    """Hängt das gelernte Stilprofil des Channels (falls vorhanden) an einen
    System-Prompt an, damit der Bot wie die echten Mitglieder klingt."""
    profil = data["stilprofile"].get(str(channel_id))
    if not profil:
        return basis_prompt
    return (
        f"{basis_prompt}\n\n"
        "So schreiben die Leute in diesem Channel normalerweise (orientiere "
        "dich daran, aber übertreibe es nicht):\n"
        f"{profil}"
    )

# ─── VERLAUF-/NACHRICHTEN-VERWALTUNG ─────────────────────────────────────────
def verlauf_eintrag_hinzufuegen(channel_id: int, autor: str, inhalt: str, message: discord.Message = None) -> int:
    """Fügt einen Eintrag (Mensch oder Bot) zum nummerierten Kurzzeitgedächtnis
    hinzu und merkt sich, falls vorhanden, das zugehörige discord.Message-
    Objekt, damit später gezielt reagiert/geantwortet werden kann."""
    idx = naechster_index[channel_id]
    naechster_index[channel_id] += 1
    verlauf[channel_id].append({"index": idx, "autor": autor, "inhalt": inhalt})
    if message is not None:
        nachricht_lookup[channel_id].append((idx, message))
    return idx

def hole_nachricht(channel_id: int, index: int):
    for idx, msg in nachricht_lookup[channel_id]:
        if idx == index:
            return msg
    return None

def parse_antwort(antwort: str):
    """Parst die Modell-Antwort gemäß Antwort-Protokoll.
    Gibt (typ, index_oder_None, inhalt_oder_None) zurück, wobei typ eine von
    'skip', 'emoji', 'reply', 'text' ist."""
    text = antwort.strip()
    if not text:
        return ("skip", None, None)
    if text.upper() == SKIP_TOKEN:
        return ("skip", None, None)

    if text.upper().startswith("EMOJI:"):
        rest = text[len("EMOJI:"):]
        teile = rest.split(":", 1)
        if len(teile) == 2 and teile[0].strip().isdigit() and teile[1].strip():
            return ("emoji", int(teile[0].strip()), teile[1].strip())

    if text.upper().startswith("REPLY:"):
        rest = text[len("REPLY:"):]
        teile = rest.split(":", 1)
        if len(teile) == 2 and teile[0].strip().isdigit() and teile[1].strip():
            return ("reply", int(teile[0].strip()), teile[1].strip())

    return ("text", None, text)

async def fuehre_antwort_aus(typ: str, index, inhalt, channel: discord.abc.Messageable, channel_id: int) -> str:
    """Setzt die geparste Modell-Antwort tatsächlich um (Emoji-Reaktion,
    gepingte Reply oder normale Channel-Nachricht) und gibt zurück, was
    passiert ist: 'skip', 'emoji', 'reply', 'text' oder 'fehler'."""
    if typ == "skip":
        return "skip"

    if typ == "emoji":
        emoji = (inhalt or "").split()[0] if inhalt else None
        nachricht = hole_nachricht(channel_id, index) if index is not None else None
        if not nachricht or not emoji:
            print(f"[protokoll] Channel {channel_id}: EMOJI-Antwort mit ungültigem Index {index} oder leerem Emoji, ignoriere.")
            return "fehler"
        try:
            await nachricht.add_reaction(emoji)
            return "emoji"
        except Exception as e:
            print(f"❌ Konnte Emoji-Reaktion nicht setzen: {e}")
            return "fehler"

    if typ == "reply":
        if not inhalt:
            return "fehler"
        nachricht = hole_nachricht(channel_id, index) if index is not None else None
        try:
            if nachricht:
                gesendet = await nachricht.reply(inhalt)
            else:
                # Referenzierte Nachricht nicht mehr auffindbar -> als
                # normale Nachricht senden statt komplett zu verwerfen.
                print(f"[protokoll] Channel {channel_id}: REPLY-Index {index} nicht gefunden, sende als normale Nachricht.")
                gesendet = await channel.send(inhalt)
        except Exception as e:
            print(f"❌ Fehler beim Senden der Reply-Antwort: {e}")
            return "fehler"
        verlauf_eintrag_hinzufuegen(channel_id, gesendet.author.display_name, inhalt, message=gesendet)
        return "reply"

    # typ == "text"
    if not inhalt:
        return "fehler"
    try:
        gesendet = await channel.send(inhalt)
    except Exception as e:
        print(f"❌ Fehler beim Senden der Antwort: {e}")
        return "fehler"
    verlauf_eintrag_hinzufuegen(channel_id, gesendet.author.display_name, inhalt, message=gesendet)
    return "text"

# ─── GEMINI API AUFRUF (allgemein, für Chat-Antworten UND Stilanalyse) ───────
# Nutzt den kostenlosen Gemini-API-Tier (kein Kreditkarte nötig).
async def rufe_claude_auf(system_prompt: str, user_content: str, max_tokens: int = 300) -> str | None:
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY fehlt, kann keine Antwort generieren.")
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    headers = {"content-type": "application/json"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"❌ Gemini API Fehler {resp.status}: {text}")
                    return None
                result = await resp.json()
                kandidaten = result.get("candidates", [])
                if not kandidaten:
                    # z.B. durch Safety-Filter blockiert
                    print(f"⚠️ Gemini lieferte keine Kandidaten zurück: {result}")
                    return None
                teile = [
                    p.get("text", "")
                    for p in kandidaten[0].get("content", {}).get("parts", [])
                ]
                antwort = "\n".join(teile).strip()
                return antwort or None
    except asyncio.TimeoutError:
        print("❌ Gemini API Timeout")
        return None
    except Exception as e:
        print(f"❌ Fehler beim Aufruf der Gemini API: {e}")
        return None

async def frage_claude(channel_id: int, system_prompt: str, zwinge_antwort: bool = False) -> str | None:
    nachrichten_verlauf = list(verlauf[channel_id])
    if not nachrichten_verlauf:
        return None

    verlauf_text = "\n".join(
        f"[{eintrag['index']}] {eintrag['autor']}: {eintrag['inhalt']}"
        for eintrag in nachrichten_verlauf
    )

    hinweis = ""
    if zwinge_antwort:
        hinweis = (
            "\n\n(Hinweis: Du hast die letzten Male nichts gesagt, obwohl "
            f"weiter geschrieben wurde. Antworte diesmal NICHT mit {SKIP_TOKEN} "
            "sondern finde einen kurzen, lockeren Anschlusskommentar oder "
            "zumindest eine passende Emoji-Reaktion, selbst wenn es nur eine "
            "knappe Reaktion ist.)"
        )

    user_content = (
        "Hier ist der bisherige, durchnummerierte Chatverlauf im Discord-"
        "Channel (Format: [Nummer] Name: Nachricht). Reagiere gemäß dem "
        "Antwortformat aus deiner Systemanweisung:\n\n"
        f"{verlauf_text}{hinweis}"
    )

    effektiver_prompt = system_prompt_mit_stil(system_prompt, channel_id)
    return await rufe_claude_auf(effektiver_prompt, user_content, max_tokens=300)

# ─── STILPROFIL: AUFBAU UND AUTOMATISCHES UPDATE ─────────────────────────────
async def erstelle_stilprofil(channel_id: int) -> bool:
    """Baut aus den gesammelten Nutzer-Nachrichten ein neues Stilprofil und
    speichert es. Gibt True zurück, wenn erfolgreich."""
    if stil_update_laeuft[channel_id]:
        return False
    nachrichten = list(stil_sammlung[channel_id])
    if len(nachrichten) < STIL_MINDEST_NACHRICHTEN:
        return False

    stil_update_laeuft[channel_id] = True
    try:
        verlauf_text = "\n".join(
            f"{eintrag['autor']}: {eintrag['inhalt']}" for eintrag in nachrichten
        )
        profil = await rufe_claude_auf(
            STIL_SYSTEM_PROMPT,
            f"Chatverlauf:\n\n{verlauf_text}",
            max_tokens=400,
        )
        if not profil:
            print(f"[stilprofil] Channel {channel_id}: Erstellung fehlgeschlagen (keine Antwort).")
            return False

        data["stilprofile"][str(channel_id)] = profil
        save_data(data)
        neue_seit_stil_update[channel_id] = 0
        print(f"[stilprofil] Channel {channel_id}: Stilprofil aktualisiert ({len(nachrichten)} Nachrichten als Basis).")
        return True
    finally:
        stil_update_laeuft[channel_id] = False

def stil_sammlung_erfassen(channel_id: int, autor: str, inhalt: str):
    """Nimmt eine echte Nutzer-Nachricht in die Stil-Sammlung auf und stößt
    bei Bedarf im Hintergrund ein Update des Stilprofils an."""
    stil_sammlung[channel_id].append({"autor": autor, "inhalt": inhalt})
    neue_seit_stil_update[channel_id] += 1
    if neue_seit_stil_update[channel_id] >= STIL_UPDATE_SCHWELLE:
        asyncio.create_task(erstelle_stilprofil(channel_id))

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
    # vorhanden ist. Zusätzlich fließt sie in die Stil-Sammlung ein, damit der
    # Bot langfristig lernt, wie die Leute hier schreiben.
    if message.content:
        verlauf_eintrag_hinzufuegen(channel_id, message.author.display_name, message.content, message=message)
        stil_sammlung_erfassen(channel_id, message.author.display_name, message.content)

        if modus == "beobachtend":
            ungesehene_seit_reaktion[channel_id] += 1
            # Erste Nachricht seit der letzten Reaktion startet das Zeitfenster.
            if channel_id not in naechste_pruefung:
                naechste_pruefung[channel_id] = neues_beobachtungsfenster()
    elif not message.content and (message.attachments or message.embeds):
        # Nachricht ohne Text (z.B. nur Bild) – kein Beitrag zu Verlauf/Stil,
        # aber falls dies passiert obwohl der Nutzer sichtbar Text getippt hat,
        # deutet das auf ein fehlendes "Message Content Intent" im Discord
        # Developer Portal hin (siehe Hinweis in /ki_status).
        pass

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

        if not antwort:
            return

        typ, index, inhalt = parse_antwort(antwort)
        ergebnis = await fuehre_antwort_aus(typ, index, inhalt, message.channel, channel_id)

        if ergebnis == "skip":
            return

        letzte_direkt_antwort[channel_id] = datetime.now(timezone.utc)
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
            ergebnis = "fehler"
        else:
            typ, index, inhalt = parse_antwort(antwort)
            ergebnis = await fuehre_antwort_aus(typ, index, inhalt, kanal, channel_id)

        if ergebnis == "skip":
            skip_zaehler[channel_id] += 1
            print(f"[beobachtung] Channel {channel_id}: Modell hat {SKIP_TOKEN} gewählt (in Folge: {skip_zaehler[channel_id]}).")
        elif ergebnis in ("emoji", "reply", "text"):
            skip_zaehler[channel_id] = 0
            print(f"[beobachtung] Channel {channel_id}: Reaktion vom Typ '{ergebnis}' ausgeführt.")
        else:
            print(f"[beobachtung] Channel {channel_id}: Antwort konnte nicht umgesetzt werden (Typ 'fehler').")

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
            "Kein Channel konfiguriert – Standard ist überall **erwaehnung**.\n"
            f"API-Key gesetzt: {'✅' if GEMINI_API_KEY else '❌ FEHLT'}",
            ephemeral=True
        )
        return
    jetzt = datetime.now(timezone.utc)
    zeilen = [f"API-Key gesetzt: {'✅' if GEMINI_API_KEY else '❌ FEHLT'}\n"]
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
        stil_status = "mit Stilprofil" if str(cid) in data["stilprofile"] else f"noch kein Stilprofil ({len(stil_sammlung.get(cid, []))}/{STIL_MINDEST_NACHRICHTEN} Nachrichten gesammelt)"
        zeilen.append(f"{name}: **{modus}**{extra} – {stil_status}")

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
    nachricht_lookup[cid].clear()
    naechster_index[cid] = 0
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

@tree.command(name="ki_stil_lernen", description="Erstellt/aktualisiert das Stilprofil dieses Channels sofort neu")
@app_commands.check(ist_admin_oder_leitung)
async def ki_stil_lernen(interaction: discord.Interaction):
    cid = interaction.channel.id
    anzahl = len(stil_sammlung.get(cid, []))
    if anzahl < STIL_MINDEST_NACHRICHTEN:
        await interaction.response.send_message(
            f"❌ Noch zu wenige Nachrichten gesammelt ({anzahl}/{STIL_MINDEST_NACHRICHTEN}). "
            "Erst wenn genug echte Chat-Nachrichten in diesem Channel geschrieben wurden, "
            "kann ein Stilprofil erstellt werden.",
            ephemeral=True
        )
        return
    await interaction.response.send_message("⏳ Erstelle Stilprofil...", ephemeral=True)
    erfolg = await erstelle_stilprofil(cid)
    if erfolg:
        await interaction.edit_original_response(content="✅ Stilprofil aktualisiert. Nutze `/ki_stil_anzeigen` um es zu sehen.")
    else:
        await interaction.edit_original_response(content="❌ Erstellung fehlgeschlagen (siehe Logs, z.B. API-Key-Problem).")

@tree.command(name="ki_stil_anzeigen", description="Zeigt das aktuell gelernte Stilprofil dieses Channels")
@app_commands.check(ist_admin_oder_leitung)
async def ki_stil_anzeigen(interaction: discord.Interaction):
    profil = data["stilprofile"].get(str(interaction.channel.id))
    if not profil:
        await interaction.response.send_message("Für diesen Channel wurde noch kein Stilprofil erstellt.", ephemeral=True)
        return
    await interaction.response.send_message(f"**Aktuelles Stilprofil:**\n{profil}", ephemeral=True)

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

    if not GEMINI_API_KEY:
        print("⚠️⚠️⚠️ ACHTUNG: GEMINI_API_KEY ist NICHT gesetzt – der Bot wird niemals eigenständig antworten können!")

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
