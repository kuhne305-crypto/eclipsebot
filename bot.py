import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
from datetime import datetime, timedelta
import pytz
import json
import os
import re

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # <-- NEU: deine Server-ID hier als Railway Variable eintragen!
TIMEZONE = pytz.timezone("Europe/Berlin")
EMBED_COLOR = 0xFFD700  # Gelb
DATA_FILE = "data.json"

# ─── DATA HANDLER ─────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "rolle_id": None,
        "aktuelle_nachricht_id": None,
        "abstimmung": {},
        "abmeldungen": {},
        "eingefroren": False,
        "aktuelles_datum": None,
        "channel_aufstellung": None,
        "channel_archiv": None,
        "channel_abmeldung": None,
        "channel_abmeldung_liste": None,
        "abmeldung_liste_nachricht_id": None,
        "channel_abmeldung_button": None,
        "abmeldung_button_nachricht_id": None,
        "aufstellung_wochentage": None,   # None/[] = jeden Tag. Sonst Liste von 0=Montag..6=Sonntag
        "einfrier_uhrzeit": "21:00"
    }

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

def build_embed(datum, mitglieder, eingefroren=False):
    abstimmung  = data.get("abstimmung", {})
    abmeldungen = data.get("abmeldungen", {})

    ja_liste        = []
    spaeter_liste   = []
    nein_liste      = []
    abgemeldet_liste= []
    offen_liste     = []

    for m in mitglieder:
        uid     = str(m.id)
        mention = m.mention
        if uid in abmeldungen:
            abgemeldet_liste.append(mention)
        elif uid in abstimmung:
            status = abstimmung[uid]
            if status == "ja":
                ja_liste.append(mention)
            elif status == "spaeter":
                spaeter_liste.append(mention)
            elif status == "nein":
                nein_liste.append(mention)
        else:
            offen_liste.append(mention)

    titel = "Aufstellung"
    if eingefroren:
        titel += " *(Eingefroren)*"

    embed = discord.Embed(
        title=titel,
        description=(
            f"**{datum}**\n"
            f"Aufstellung: **{data.get('einfrier_uhrzeit', '21:00')} Uhr**\n"
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
        hatte_abmeldung = data["abmeldungen"].pop(str(interaction.user.id), None)
        save_data(data)
        await update_nachricht(interaction.guild)
        if hatte_abmeldung:
            await update_abmeldung_liste(interaction.guild)
        await interaction.response.send_message("Du hast mit **Komme** abgestimmt!", ephemeral=True)

    @discord.ui.button(label="Komme später", style=discord.ButtonStyle.secondary, custom_id="btn_spaeter")
    async def btn_spaeter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_berechtigung(interaction):
            return
        data["abstimmung"][str(interaction.user.id)] = "spaeter"
        hatte_abmeldung = data["abmeldungen"].pop(str(interaction.user.id), None)
        save_data(data)
        await update_nachricht(interaction.guild)
        if hatte_abmeldung:
            await update_abmeldung_liste(interaction.guild)
        await interaction.response.send_message("Du hast mit **Komme später** abgestimmt!", ephemeral=True)

    @discord.ui.button(label="Komme nicht", style=discord.ButtonStyle.danger, custom_id="btn_nein")
    async def btn_nein(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.check_berechtigung(interaction):
            return
        data["abstimmung"][str(interaction.user.id)] = "nein"
        hatte_abmeldung = data["abmeldungen"].pop(str(interaction.user.id), None)
        save_data(data)
        await update_nachricht(interaction.guild)
        if hatte_abmeldung:
            await update_abmeldung_liste(interaction.guild)
        await interaction.response.send_message("Du hast mit **Komme nicht** abgestimmt!", ephemeral=True)

# ─── ABMELDUNG PER BUTTON + MODAL ─────────────────────────────────────────────
class AbmeldungModal(discord.ui.Modal, title="Abmeldung"):
    name_input = discord.ui.TextInput(
        label="Name", placeholder="Wer meldet sich ab?", required=True, max_length=32
    )
    von_input = discord.ui.TextInput(
        label="Von wann?", placeholder="14.07.2026", required=True, max_length=32
    )
    bis_input = discord.ui.TextInput(
        label="Bis wann?", placeholder="16.07.2026", required=True, max_length=32
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

        uid = str(interaction.user.id)
        data["abmeldungen"][uid] = {"name": name, "von": von, "bis": bis, "grund": grund, "typ": "kurzzeit"}
        data["abstimmung"].pop(uid, None)
        save_data(data)

        if not data.get("eingefroren"):
            await update_nachricht(interaction.guild)
        await update_abmeldung_liste(interaction.guild)

        await interaction.response.send_message(
            f"✅ Abmeldung eingetragen!\n"
            f"Name: **{name}**\n"
            f"Von: **{von}**\n"
            f"Bis: **{bis}**\n"
            f"Du wirst in der Aufstellung als Abgemeldet angezeigt.",
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
def parse_datum(datum_str):
    """Versucht ein Datum im Format TT.MM.JJJJ zu parsen, sonst None."""
    try:
        return datetime.strptime(str(datum_str).strip(), "%d.%m.%Y")
    except Exception:
        return None

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

    # Sortierung: wer zuerst wieder zurück ist (frühestes "Bis"-Datum), steht oben.
    # Nicht parsbare Daten landen ans Ende.
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

        block = (
            f"{mention}  ·  {typ_label}\n"
            f"Name: **{name}**\n"
            f"Von: **{von}**  Bis: **{bis}**  Grund: {grund}"
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

# ─── NEUE ABSTIMMUNG POSTEN ───────────────────────────────────────────────────
async def neue_abstimmung_posten(guild, manual_channel=None, verwende_heute=False):
    if manual_channel:
        kanal = manual_channel
    elif data.get("channel_aufstellung"):
        kanal = guild.get_channel(int(data["channel_aufstellung"]))
    else:
        print("Kein Aufstellungs-Channel gesetzt! Bitte /set_aufstellung benutzen.")
        return

    if not kanal:
        print("Aufstellungs-Channel nicht gefunden!")
        return

    datum = get_heute_datum() if verwende_heute else get_morgen_datum()
    data["abstimmung"]      = {}
    data["eingefroren"]     = False
    data["aktuelles_datum"] = datum

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

# ─── TASKS ────────────────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def check_zeit():
    now = datetime.now(TIMEZONE)
    h, m = now.hour, now.minute
    heutiger_wochentag  = now.weekday()
    morgiger_wochentag  = (now + timedelta(days=1)).weekday()

    konfigurierte_tage = data.get("aufstellung_wochentage")  # None/[] = jeden Tag
    einfrier_uhrzeit    = data.get("einfrier_uhrzeit", "21:00")
    try:
        einfrier_h, einfrier_m = map(int, einfrier_uhrzeit.split(":"))
    except Exception:
        einfrier_h, einfrier_m = 21, 0

    # Neue Aufstellung um 23:59 posten, nur wenn morgen ein Aufstellungstag ist
    if h == 23 and m == 59:
        if not konfigurierte_tage or morgiger_wochentag in konfigurierte_tage:
            for guild in bot.guilds:
                await neue_abstimmung_posten(guild)
            await asyncio.sleep(61)

    # Einfrieren zur konfigurierten Uhrzeit, nur wenn heute ein Aufstellungstag ist
    if h == einfrier_h and m == einfrier_m and not data.get("eingefroren", False):
        if not konfigurierte_tage or heutiger_wochentag in konfigurierte_tage:
            for guild in bot.guilds:
                await abstimmung_einfrieren(guild)
            await asyncio.sleep(61)

# ─── SLASH COMMANDS ───────────────────────────────────────────────────────────

@tree.command(name="setrolle", description="Setzt die Rolle die an der Aufstellung teilnimmt")
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

@tree.command(name="aufstellung_tage", description="Legt fest an welchen Wochentagen Aufstellung stattfindet")
@app_commands.describe(tage="Wochentage mit Komma getrennt, z.B. Montag,Donnerstag (leer = jeden Tag)")
@app_commands.checks.has_permissions(administrator=True)
async def aufstellung_tage(interaction: discord.Interaction, tage: str):
    tage = tage.strip()
    if not tage:
        data["aufstellung_wochentage"] = None
        save_data(data)
        await interaction.response.send_message("✅ Aufstellung findet jetzt wieder **jeden Tag** statt.", ephemeral=True)
        return

    eingaben   = [t.strip().lower() for t in tage.split(",") if t.strip()]
    ungueltige = [t for t in eingaben if t not in WOCHENTAGE_MAP]
    if ungueltige:
        await interaction.response.send_message(
            f"❌ Unbekannte Wochentage: {', '.join(ungueltige)}\n"
            f"Gültig sind: {', '.join(WOCHENTAGE_NAMEN)}",
            ephemeral=True
        )
        return

    tage_nums = sorted(set(WOCHENTAGE_MAP[t] for t in eingaben))
    data["aufstellung_wochentage"] = tage_nums
    save_data(data)
    namen = ", ".join(WOCHENTAGE_NAMEN[n] for n in tage_nums)
    await interaction.response.send_message(f"✅ Aufstellung findet jetzt statt an: **{namen}**", ephemeral=True)

@tree.command(name="aufstellung_zeit", description="Legt fest um wie viel Uhr die Aufstellung eingefroren wird")
@app_commands.describe(uhrzeit="Uhrzeit im Format HH:MM, z.B. 21:00")
@app_commands.checks.has_permissions(administrator=True)
async def aufstellung_zeit(interaction: discord.Interaction, uhrzeit: str):
    uhrzeit = uhrzeit.strip()
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", uhrzeit):
        await interaction.response.send_message(
            "❌ Ungültiges Format. Bitte HH:MM verwenden, z.B. 21:00", ephemeral=True
        )
        return
    data["einfrier_uhrzeit"] = uhrzeit
    save_data(data)
    await interaction.response.send_message(f"✅ Aufstellung wird jetzt um **{uhrzeit} Uhr** eingefroren.", ephemeral=True)

@tree.command(name="set_aufstellung", description="Setzt den Channel für die Aufstellungs-Abstimmung")
@app_commands.describe(channel="Der Channel wo die Abstimmung gepostet wird")
@app_commands.checks.has_permissions(administrator=True)
async def set_aufstellung(interaction: discord.Interaction, channel: discord.TextChannel):
    data["channel_aufstellung"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ Aufstellungs-Channel gesetzt: {channel.mention}", ephemeral=True
    )

@tree.command(name="set_archiv", description="Setzt den Channel für das Aufstellungs-Archiv")
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

@tree.command(name="channels", description="Zeigt alle aktuell gesetzten Channels und die Rolle")
@app_commands.checks.has_permissions(administrator=True)
async def channels_info(interaction: discord.Interaction):
    auf    = interaction.guild.get_channel(int(data["channel_aufstellung"]))       if data.get("channel_aufstellung")       else None
    arch   = interaction.guild.get_channel(int(data["channel_archiv"]))            if data.get("channel_archiv")            else None
    abm    = interaction.guild.get_channel(int(data["channel_abmeldung"]))         if data.get("channel_abmeldung")         else None
    liste  = interaction.guild.get_channel(int(data["channel_abmeldung_liste"]))   if data.get("channel_abmeldung_liste")   else None
    button = interaction.guild.get_channel(int(data["channel_abmeldung_button"]))  if data.get("channel_abmeldung_button")  else None
    rolle_id = data.get("rolle_id")
    rolle = interaction.guild.get_role(int(rolle_id)) if rolle_id else None

    tage = data.get("aufstellung_wochentage")
    tage_text = ", ".join(WOCHENTAGE_NAMEN[t] for t in tage) if tage else "Jeden Tag"
    zeit_text = data.get("einfrier_uhrzeit", "21:00")

    await interaction.response.send_message(
        f"**Aktuelle Einstellungen:**\n\n"
        f"Rolle:              {rolle.mention   if rolle   else '❌ Nicht gesetzt – /setrolle benutzen'}\n"
        f"Aufstellung:        {auf.mention     if auf     else '❌ Nicht gesetzt – /set_aufstellung benutzen'}\n"
        f"Archiv:             {arch.mention    if arch    else '❌ Nicht gesetzt – /set_archiv benutzen'}\n"
        f"Abmeldung (Log):    {abm.mention     if abm     else '❌ Nicht gesetzt – /set_abmeldung benutzen'}\n"
        f"Abmeldungs-Liste:   {liste.mention   if liste   else '❌ Nicht gesetzt – /set_abmeldung_liste benutzen'}\n"
        f"Abmeldung-Button:   {button.mention  if button  else '❌ Nicht gesetzt – /set_abmeldung_button benutzen'}\n\n"
        f"Aufstellungs-Tage:  **{tage_text}**\n"
        f"Einfrier-Uhrzeit:   **{zeit_text} Uhr**",
        ephemeral=True
    )

@tree.command(name="abstimmung", description="Postet manuell eine neue Aufstellungs-Abstimmung")
@app_commands.describe(datum="Für welchen Tag gilt die Aufstellung? (Standard: Heute)")
@app_commands.choices(datum=[
    app_commands.Choice(name="Heute", value="heute"),
    app_commands.Choice(name="Morgen", value="morgen"),
])
@app_commands.checks.has_permissions(administrator=True)
async def abstimmung_manuell(interaction: discord.Interaction, datum: app_commands.Choice[str] = None):
    if not data.get("channel_aufstellung"):
        await interaction.response.send_message(
            "❌ Kein Aufstellungs-Channel gesetzt!\nBitte zuerst **/set_aufstellung #channel** benutzen.",
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

@tree.command(name="abmelden", description="Melde dich von der Aufstellung ab")
@app_commands.describe(
    von="Von wann? (z.B. 14.07.2026)",
    bis="Bis wann? (z.B. 16.07.2026)",
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

    uid = str(interaction.user.id)
    data["abmeldungen"][uid] = {"von": von, "bis": bis, "grund": grund, "typ": "kurzzeit"}
    data["abstimmung"].pop(uid, None)
    save_data(data)

    if not data.get("eingefroren"):
        await update_nachricht(interaction.guild)
    await update_abmeldung_liste(interaction.guild)

    await interaction.response.send_message(
        f"✅ Abmeldung eingetragen!\n"
        f"Von: **{von}**\n"
        f"Bis: **{bis}**\n"
        f"Du wirst in der Aufstellung als Abgemeldet angezeigt.",
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
    von="Von wann? (z.B. 14.07.2026)",
    bis="Bis wann? (z.B. 25.08.2026)",
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

    uid = str(interaction.user.id)
    data["abmeldungen"][uid] = {"von": von, "bis": bis, "grund": grund, "typ": "langzeit"}
    data["abstimmung"].pop(uid, None)
    save_data(data)

    if not data.get("eingefroren"):
        await update_nachricht(interaction.guild)
    await update_abmeldung_liste(interaction.guild)

    await interaction.response.send_message(
        f"✅ Langzeit-Abmeldung eingetragen!\n"
        f"Von: **{von}**\n"
        f"Bis: **{bis}**\n"
        f"Du wirst in der Aufstellung als Abgemeldet angezeigt.",
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

# ─── BOT EVENTS ───────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")

    # ── SYNC MIT LOGGING (NEU) ─────────────────────────────────────────────
    # Guild-Sync = SOFORT sichtbar (nur auf deinem Server, super zum Testen)
    # Global-Sync = kann bis zu 1h dauern, dafür auf allen Servern
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
    # ─────────────────────────────────────────────────────────────────────

    bot.add_view(AufstellungView())
    bot.add_view(AbmeldungButtonView())
    check_zeit.start()
    print("Tasks gestartet. Bot ist bereit!")

@bot.event
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
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
