"""
Family Events Mailer – Leverkusen/Köln
Läuft jeden Donnerstag via GitHub Actions und postet Wochenend-Events nach Slack + E-Mail.
Familie: Mikel (55), Sandra (51), Halley Malia (5), Samuel (13)
"""

import os
import json
import datetime
import urllib.request
import urllib.error
import urllib.parse
import smtplib
import email.mime.multipart
import email.mime.text
import email.mime.base
import email.encoders
from pathlib import Path
import anthropic

SLACK_WEBHOOK_URL    = os.environ["SLACK_WEBHOOK_URL"]
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
GMAIL_APP_PASSWORD   = os.environ.get("GMAIL_APP_PASSWORD", "")
TICKETMASTER_API_KEY = os.environ.get("TICKETMASTER_API_KEY", "")
EMAIL_TO             = "sievers.mikel@gmail.com"
EMAIL_FROM           = "sievers.mikel@gmail.com"

LAT        = 51.0459
LON        = 6.9929
CONFIG_DIR = Path(__file__).parent / "config"

WMO_CODES: dict[int, tuple[str, str]] = {
    0:  ("Klarer Himmel",        "☀️"),
    1:  ("Überwiegend klar",     "🌤️"),
    2:  ("Teilweise bewölkt",    "⛅"),
    3:  ("Bedeckt",              "☁️"),
    45: ("Nebel",                "🌫️"),
    48: ("Reifnebel",            "🌫️"),
    51: ("Leichter Nieselregen", "🌦️"),
    53: ("Mäßiger Nieselregen",  "🌦️"),
    55: ("Starker Nieselregen",  "🌧️"),
    61: ("Leichter Regen",       "🌧️"),
    63: ("Mäßiger Regen",        "🌧️"),
    65: ("Starker Regen",        "🌧️"),
    71: ("Leichter Schneefall",  "🌨️"),
    73: ("Mäßiger Schneefall",   "🌨️"),
    75: ("Starker Schneefall",   "❄️"),
    80: ("Leichte Schauer",      "🌦️"),
    81: ("Mäßige Schauer",       "🌧️"),
    82: ("Starke Schauer",       "⛈️"),
    95: ("Gewitter",             "⛈️"),
    96: ("Gewitter mit Hagel",   "⛈️"),
    99: ("Starkes Gewitter",     "⛈️"),
}

ACTIVITY_LISTS = {
    "indoor": (
        "Museen (Museum Ludwig, Schokoladenmuseum, Odysseum, NS-Dok, Kunstmuseum Bonn), "
        "Kino, Trampolinhallen, Bowlingbahn, Indoor-Klettern, "
        "Schwimmbäder (Agrippabad, Leverkusen-Bäder), Escape Rooms, Spielhallen, "
        "Konzerthallen, Theater, Bowling"
    ),
    "outdoor": (
        "Rheinufer, Naturpark Bergisches Land, Stadtwald Köln, Kölner Zoo, "
        "Bayer-Erholungsgelände Leverkusen, Fahrradtouren, Spielplätze, "
        "Stadtgärten, Open-Air-Konzerte, Bootsfahrten, Flohmärkte"
    ),
    "gemischt": (
        "Zoo (Innen+Außen), Museen mit Außengelände, überdachte Märkte, "
        "Stadtbummel Köln-Innenstadt, Tierpark Leverkusen, "
        "Konzerte in Hallen, Schwimmbad + Park"
    ),
}

FOCUS_LABEL = {
    "indoor":   "🏠 INDOOR – schlechtes Wetter",
    "outdoor":  "🌳 OUTDOOR – schönes Wetter",
    "gemischt": "🌤️ GEMISCHT – wechselhaftes Wetter",
}


# ── Datum ────────────────────────────────────────────────────────────────────

def get_weekend_dates() -> tuple[datetime.date, datetime.date]:
    today = datetime.date.today()
    saturday = today + datetime.timedelta(days=(5 - today.weekday()) % 7)
    return saturday, saturday + datetime.timedelta(days=1)


# ── Wetter ───────────────────────────────────────────────────────────────────

def fetch_weather_forecast(saturday: datetime.date, sunday: datetime.date) -> dict:
    params = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON,
        "daily": "weathercode,temperature_2m_max,temperature_2m_min,"
                 "precipitation_sum,precipitation_probability_max,windspeed_10m_max",
        "timezone": "Europe/Berlin",
        "forecast_days": 10,
    })
    with urllib.request.urlopen(
        f"https://api.open-meteo.com/v1/forecast?{params}", timeout=10
    ) as r:
        data = json.loads(r.read())

    daily  = data["daily"]
    result = {}
    for label, d in [("Samstag", saturday), ("Sonntag", sunday)]:
        iso = d.isoformat()
        if iso in daily["time"]:
            i    = daily["time"].index(iso)
            code = daily["weathercode"][i]
            desc, emoji = WMO_CODES.get(code, ("Unbekannt", "❓"))
            result[label] = {
                "datum":                  d.strftime("%d.%m.%Y"),
                "beschreibung":           desc,
                "emoji":                  emoji,
                "temp_max":               daily["temperature_2m_max"][i],
                "temp_min":               daily["temperature_2m_min"][i],
                "regen_mm":               daily["precipitation_sum"][i],
                "regen_wahrscheinlichkeit": daily["precipitation_probability_max"][i],
                "wind_kmh":               daily["windspeed_10m_max"][i],
            }
    return result


def classify_day(w: dict) -> str:
    regen_stark  = w["regen_mm"] >= 3.0 or w["regen_wahrscheinlichkeit"] >= 60
    regen_leicht = w["regen_mm"] >= 1.0 or w["regen_wahrscheinlichkeit"] >= 35
    warm         = w["temp_max"] >= 15
    windig       = w["wind_kmh"] >= 40
    if regen_stark or windig:  return "indoor"
    if regen_leicht or not warm: return "gemischt"
    return "outdoor"


# ── Ticketmaster ─────────────────────────────────────────────────────────────

def fetch_ticketmaster_events(date: datetime.date) -> list[dict]:
    if not TICKETMASTER_API_KEY:
        return []
    start = f"{date.isoformat()}T00:00:00Z"
    end   = f"{date.isoformat()}T23:59:59Z"
    params = urllib.parse.urlencode({
        "apikey":        TICKETMASTER_API_KEY,
        "city":          "Köln",
        "countryCode":   "DE",
        "startDateTime": start,
        "endDateTime":   end,
        "radius":        30,
        "unit":          "km",
        "size":          10,
        "sort":          "relevance,desc",
    })
    url = f"https://app.ticketmaster.com/discovery/v2/events.json?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        events = data.get("_embedded", {}).get("events", [])
        result = []
        for e in events:
            venue  = e.get("_embedded", {}).get("venues", [{}])[0]
            time   = e.get("dates", {}).get("start", {}).get("localTime", "")
            result.append({
                "name":  e.get("name", ""),
                "url":   e.get("url", ""),
                "time":  time[:5] if time else "",
                "venue": venue.get("name", ""),
                "city":  venue.get("city", {}).get("name", ""),
            })
        return result
    except Exception as ex:
        print(f"⚠️  Ticketmaster-Fehler: {ex}")
        return []


def format_ticketmaster_events(events: list[dict]) -> str:
    if not events:
        return "Keine Ticketmaster-Events gefunden."
    lines = []
    for e in events:
        uhrzeit = f" um {e['time']}" if e["time"] else ""
        ort     = f" | {e['venue']}, {e['city']}" if e["venue"] else ""
        link    = f" → {e['url']}" if e["url"] else ""
        lines.append(f"• {e['name']}{uhrzeit}{ort}{link}")
    return "\n".join(lines)


# ── Familie ──────────────────────────────────────────────────────────────────

def load_family() -> dict:
    with open(CONFIG_DIR / "children.json", encoding="utf-8") as f:
        return json.load(f)


def family_for_prompt(family: dict) -> str:
    lines = ["Kinder:"]
    for k in family.get("kinder", []):
        lines.append(f"  - {k['name']}, {k['alter']} J. → {', '.join(k['interessen'])}")
    lines.append("Eltern:")
    for e in family.get("eltern", []):
        lines.append(f"  - {e['name']}, {e['alter']} J. → {', '.join(e['interessen'])}")
    return "\n".join(lines)


# ── Claude ───────────────────────────────────────────────────────────────────

def find_events_for_day(
    tag: str,
    date: datetime.date,
    w: dict,
    typ: str,
    family_text: str,
    tm_events: list[dict],
    already_suggested: list[str],
) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    tm_text    = format_ticketmaster_events(tm_events)
    avoid_text = "\n".join(f"- {x}" for x in already_suggested) if already_suggested else "keine"

    prompt = f"""Du bist ein Familienassistent für die Region Leverkusen/Köln.
Erstelle einen Tagesplan für {tag}, den {date.strftime("%d.%m.%Y")}.

══ FAMILIE ══
{family_text}

══ WETTER ══
{w['emoji']} {w['beschreibung']}, {w['temp_min']:.0f}–{w['temp_max']:.0f} °C,
Regen {w['regen_wahrscheinlichkeit']:.0f} % → Fokus: {FOCUS_LABEL[typ]}
Erlaubte Aktivitäten: {ACTIVITY_LISTS[typ]}

══ ECHTE EVENTS HEUTE (Ticketmaster) ══
{tm_text}

══ BEREITS VORGESCHLAGEN (NICHT WIEDERHOLEN) ══
{avoid_text}

══ AUFGABE ══
Erstelle MINDESTENS 6 verschiedene Empfehlungen für diesen Tag.
Integriere die echten Ticketmaster-Events wo passend.
Alle Empfehlungen müssen zum Wetter-Fokus ({FOCUS_LABEL[typ]}) passen.
Keine Wiederholungen aus der "bereits vorgeschlagen"-Liste.

Strukturiere nach Tageszeit mit konkreten Uhrzeiten:
🌅 Vormittag (09:00–12:00)
☀️ Mittag (12:00–14:00) – Essensvorschlag einbauen
🌆 Nachmittag (14:00–18:00)
🌙 Abend (ab 18:00)

Pro Empfehlung:
- Uhrzeit · Name · Ort · für wen geeignet · Kosten ca.

Schreibe kompakt auf Deutsch."""

    msg = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        system=(
            "Du bist ein lokaler Familienassistent für Leverkusen/Köln. "
            "Du folgst dem Wetter-Fokus strikt und wiederholst keine Empfehlungen. "
            f"Heute ist {datetime.date.today().strftime('%d.%m.%Y')}."
        ),
    )
    return msg.content[0].text


def find_events_with_claude(
    saturday: datetime.date,
    sunday: datetime.date,
    forecast: dict,
) -> tuple[str, str]:
    family      = load_family()
    family_text = family_for_prompt(family)

    sat_w   = forecast.get("Samstag", {})
    sun_w   = forecast.get("Sonntag", {})
    sat_typ = classify_day(sat_w) if sat_w else "gemischt"
    sun_typ = classify_day(sun_w) if sun_w else "gemischt"

    print("   🎫 Lade Ticketmaster-Events …")
    sat_tm = fetch_ticketmaster_events(saturday)
    sun_tm = fetch_ticketmaster_events(sunday)
    print(f"   → Samstag: {len(sat_tm)} Events | Sonntag: {len(sun_tm)} Events")

    print("   🤖 Claude plant Samstag …")
    sat_text = find_events_for_day(
        "Samstag", saturday, sat_w, sat_typ, family_text, sat_tm, []
    )

    # Extrahiere grob die Ortsnamen aus Samstag um Duplikate zu vermeiden
    sat_lines = [l.strip() for l in sat_text.splitlines() if l.strip()]

    print("   🤖 Claude plant Sonntag …")
    sun_text = find_events_for_day(
        "Sonntag", sunday, sun_w, sun_typ, family_text, sun_tm, sat_lines
    )

    return sat_text, sun_text


# ── Slack ────────────────────────────────────────────────────────────────────

def _slack_blocks(text: str, limit: int = 2900) -> list[dict]:
    chunks, buf = [], text
    while buf:
        if len(buf) <= limit:
            chunks.append(buf); break
        cut = buf.rfind("\n", 0, limit)
        if cut == -1: cut = limit
        chunks.append(buf[:cut])
        buf = buf[cut:].lstrip("\n")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]


def format_slack_message(
    sat_text: str,
    sun_text: str,
    saturday: datetime.date,
    sunday: datetime.date,
    forecast: dict,
) -> dict:
    sat_str = saturday.strftime("%d.%m.%Y")
    sun_str = sunday.strftime("%d.%m.%Y")
    sat_typ = classify_day(forecast["Samstag"]) if "Samstag" in forecast else "gemischt"
    sun_typ = classify_day(forecast["Sonntag"]) if "Sonntag" in forecast else "gemischt"

    weather_lines = []
    for tag, key, typ in [("Samstag", "Samstag", sat_typ), ("Sonntag", "Sonntag", sun_typ)]:
        w = forecast.get(key)
        if w:
            weather_lines.append(
                f"{w['emoji']} *{tag} {w['datum']}:* {w['beschreibung']}, "
                f"{w['temp_min']:.0f}–{w['temp_max']:.0f} °C, "
                f"Regen {w['regen_wahrscheinlichkeit']:.0f} %  →  _{FOCUS_LABEL[typ]}_"
            )

    return {"blocks": [
        {"type": "header", "text": {
            "type": "plain_text",
            "text": f"🎉 Familien-Wochenende {sat_str}–{sun_str}",
            "emoji": True,
        }},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "📍 Leverkusen/Köln  •  👨 Mikel & 👩 Sandra  •  👧 Halley Malia (5 J.)  •  🧑 Samuel (13 J.)",
        }]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": "*🌤️ Wettervorhersage*\n" + "\n".join(weather_lines),
        }},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*📍 SAMSTAG {saturday.strftime('%d.%m.')} – {FOCUS_LABEL[sat_typ]}*",
        }},
        *_slack_blocks(sat_text),
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*📍 SONNTAG {sunday.strftime('%d.%m.')} – {FOCUS_LABEL[sun_typ]}*",
        }},
        *_slack_blocks(sun_text),
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"_Wetter: Open-Meteo · Events: Ticketmaster + Claude Opus · "
                    f"{datetime.date.today().strftime('%d.%m.%Y')} · Family Events Mailer_",
        }]},
    ]}


def post_to_slack(payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        SLACK_WEBHOOK_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode()
        if body != "ok":
            raise RuntimeError(f"Slack: {body}")
        print("✅ Slack gesendet.")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Slack HTTP {e.code}: {e.read().decode()}") from e


# ── E-Mail ───────────────────────────────────────────────────────────────────

def build_ics(sat_text: str, sun_text: str, saturday: datetime.date, sunday: datetime.date) -> bytes:
    def esc(t): return t.replace("\\","\\\\").replace("\n","\\n").replace(",","\\,").replace(";","\\;")
    def d(x):   return x.strftime("%Y%m%d")
    now = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Family Events Mailer//DE\r\n"
        "CALSCALE:GREGORIAN\r\nMETHOD:PUBLISH\r\n"
        f"BEGIN:VEVENT\r\nUID:sat-{saturday.isoformat()}@fem\r\nDTSTAMP:{now}\r\n"
        f"DTSTART;VALUE=DATE:{d(saturday)}\r\nDTEND;VALUE=DATE:{d(saturday+datetime.timedelta(1))}\r\n"
        f"SUMMARY:🎉 Familien-Events Sa {saturday.strftime('%d.%m.')}\r\n"
        f"DESCRIPTION:{esc(sat_text)}\r\nEND:VEVENT\r\n"
        f"BEGIN:VEVENT\r\nUID:sun-{sunday.isoformat()}@fem\r\nDTSTAMP:{now}\r\n"
        f"DTSTART;VALUE=DATE:{d(sunday)}\r\nDTEND;VALUE=DATE:{d(sunday+datetime.timedelta(1))}\r\n"
        f"SUMMARY:🎉 Familien-Events So {sunday.strftime('%d.%m.')}\r\n"
        f"DESCRIPTION:{esc(sun_text)}\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode("utf-8")


def send_email(
    sat_text: str,
    sun_text: str,
    saturday: datetime.date,
    sunday: datetime.date,
    forecast: dict,
) -> None:
    if not GMAIL_APP_PASSWORD:
        print("⚠️  GMAIL_APP_PASSWORD fehlt – E-Mail übersprungen.")
        return

    sat_str = saturday.strftime("%d.%m.%Y")
    sun_str = sunday.strftime("%d.%m.%Y")
    sat_typ = classify_day(forecast["Samstag"]) if "Samstag" in forecast else "gemischt"
    sun_typ = classify_day(forecast["Sonntag"]) if "Sonntag" in forecast else "gemischt"

    weather_html = ""
    for tag, key, typ in [("Samstag","Samstag",sat_typ),("Sonntag","Sonntag",sun_typ)]:
        w = forecast.get(key)
        if w:
            weather_html += (
                f"<tr><td><b>{w['emoji']} {tag} {w['datum']}</b></td>"
                f"<td>{w['beschreibung']}</td><td>{w['temp_min']:.0f}–{w['temp_max']:.0f} °C</td>"
                f"<td>Regen {w['regen_wahrscheinlichkeit']:.0f} %</td>"
                f"<td><i>{FOCUS_LABEL[typ]}</i></td></tr>"
            )

    def to_html(t): return t.replace("\n","<br>")

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:750px;margin:auto;padding:20px;">
<h2 style="color:#2c3e50;">🎉 Familien-Wochenende {sat_str}–{sun_str}</h2>
<p style="color:#666;">📍 Leverkusen/Köln &nbsp;|&nbsp; 👨 Mikel &amp; 👩 Sandra &nbsp;|&nbsp;
👧 Halley Malia (5 J.) &nbsp;|&nbsp; 🧑 Samuel (13 J.)</p><hr>
<h3>🌤️ Wettervorhersage</h3>
<table style="border-collapse:collapse;width:100%;">
<tr style="background:#f0f0f0;">
  <th style="padding:6px;text-align:left;">Tag</th>
  <th style="padding:6px;text-align:left;">Wetter</th>
  <th style="padding:6px;text-align:left;">Temp.</th>
  <th style="padding:6px;text-align:left;">Regen</th>
  <th style="padding:6px;text-align:left;">Fokus</th>
</tr>{weather_html}</table><hr>
<h3>📍 Samstag {sat_str} – {FOCUS_LABEL[sat_typ]}</h3>
<p style="line-height:1.8;">{to_html(sat_text)}</p><hr>
<h3>📍 Sonntag {sun_str} – {FOCUS_LABEL[sun_typ]}</h3>
<p style="line-height:1.8;">{to_html(sun_text)}</p><hr>
<p style="color:#aaa;font-size:12px;">
Wetter: Open-Meteo · Events: Ticketmaster + Claude Opus ·
{datetime.date.today().strftime("%d.%m.%Y")} · Family Events Mailer</p>
</body></html>"""

    msg = email.mime.multipart.MIMEMultipart("mixed")
    msg["Subject"] = f"🎉 Familien-Wochenende {sat_str}–{sun_str} | Leverkusen/Köln"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(email.mime.text.MIMEText(html, "html", "utf-8"))

    ics_part = email.mime.base.MIMEBase("text", "calendar", method="PUBLISH")
    ics_part.set_payload(build_ics(sat_text, sun_text, saturday, sunday))
    email.encoders.encode_base64(ics_part)
    ics_part.add_header("Content-Disposition", "attachment", filename="familien-events.ics")
    msg.attach(ics_part)

    recipients = [r.strip() for r in EMAIL_TO.split(",")]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
        smtp.sendmail(EMAIL_FROM, recipients, msg.as_string())
    print(f"✅ E-Mail gesendet an {EMAIL_TO}.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    saturday, sunday = get_weekend_dates()
    print(f"🌤️  Wettervorhersage {saturday.strftime('%d.%m.')}–{sunday.strftime('%d.%m.')} …")

    forecast = fetch_weather_forecast(saturday, sunday)
    for tag, w in forecast.items():
        typ = classify_day(w)
        print(f"   {tag}: {w['emoji']} {w['beschreibung']}, "
              f"{w['temp_min']:.0f}–{w['temp_max']:.0f} °C → {FOCUS_LABEL[typ]}")

    print("🔍 Suche Events …")
    sat_text, sun_text = find_events_with_claude(saturday, sunday, forecast)

    print("📤 Sende Slack …")
    post_to_slack(format_slack_message(sat_text, sun_text, saturday, sunday, forecast))

    print("📧 Sende E-Mail …")
    send_email(sat_text, sun_text, saturday, sunday, forecast)


if __name__ == "__main__":
    main()
