"""
Family Events Mailer – Leverkusen/Köln
Läuft jeden Donnerstag via GitHub Actions und postet Wochenend-Events nach Slack.
Kinder: Halley Malia (5 J.) und Samuel (13 J.)
"""

import os
import json
import datetime
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
import anthropic

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Koordinaten Leverkusen (Stadtmitte)
LAT = 51.0459
LON = 6.9929

CONFIG_DIR = Path(__file__).parent / "config"

# WMO-Wettercodes → lesbare Beschreibung + Emoji
WMO_CODES: dict[int, tuple[str, str]] = {
    0:  ("Klarer Himmel",            "☀️"),
    1:  ("Überwiegend klar",         "🌤️"),
    2:  ("Teilweise bewölkt",        "⛅"),
    3:  ("Bedeckt",                  "☁️"),
    45: ("Nebel",                    "🌫️"),
    48: ("Reifnebel",                "🌫️"),
    51: ("Leichter Nieselregen",     "🌦️"),
    53: ("Mäßiger Nieselregen",      "🌦️"),
    55: ("Starker Nieselregen",      "🌧️"),
    61: ("Leichter Regen",           "🌧️"),
    63: ("Mäßiger Regen",            "🌧️"),
    65: ("Starker Regen",            "🌧️"),
    71: ("Leichter Schneefall",      "🌨️"),
    73: ("Mäßiger Schneefall",       "🌨️"),
    75: ("Starker Schneefall",       "❄️"),
    80: ("Leichte Regenschauer",     "🌦️"),
    81: ("Mäßige Regenschauer",      "🌧️"),
    82: ("Starke Regenschauer",      "⛈️"),
    95: ("Gewitter",                 "⛈️"),
    96: ("Gewitter mit Hagel",       "⛈️"),
    99: ("Starkes Gewitter",         "⛈️"),
}


def get_weekend_dates() -> tuple[datetime.date, datetime.date]:
    today = datetime.date.today()
    days_until_saturday = (5 - today.weekday()) % 7
    saturday = today + datetime.timedelta(days=days_until_saturday)
    sunday = saturday + datetime.timedelta(days=1)
    return saturday, sunday


def fetch_weather_forecast(saturday: datetime.date, sunday: datetime.date) -> dict:
    """Ruft die Wettervorhersage für das Wochenende von Open-Meteo ab (kein API-Key nötig)."""
    params = urllib.parse.urlencode({
        "latitude": LAT,
        "longitude": LON,
        "daily": ",".join([
            "weathercode",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "windspeed_10m_max",
        ]),
        "timezone": "Europe/Berlin",
        "forecast_days": 10,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"

    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.loads(response.read().decode())

    daily = data["daily"]
    # Index für Samstag und Sonntag heraussuchen
    result = {}
    for label, target_date in [("Samstag", saturday), ("Sonntag", sunday)]:
        iso = target_date.isoformat()
        if iso in daily["time"]:
            idx = daily["time"].index(iso)
            code = daily["weathercode"][idx]
            desc, emoji = WMO_CODES.get(code, ("Unbekannt", "❓"))
            result[label] = {
                "datum": target_date.strftime("%d.%m.%Y"),
                "beschreibung": desc,
                "emoji": emoji,
                "temp_max": daily["temperature_2m_max"][idx],
                "temp_min": daily["temperature_2m_min"][idx],
                "regen_mm": daily["precipitation_sum"][idx],
                "regen_wahrscheinlichkeit": daily["precipitation_probability_max"][idx],
                "wind_kmh": daily["windspeed_10m_max"][idx],
            }
    return result


def weather_summary(forecast: dict) -> str:
    """Kompakte einzeilige Zusammenfassung pro Tag für Slack-Header."""
    parts = []
    for tag, w in forecast.items():
        parts.append(
            f"{w['emoji']} *{tag} {w['datum']}:* {w['beschreibung']}, "
            f"{w['temp_min']:.0f}–{w['temp_max']:.0f} °C, "
            f"Regen {w['regen_wahrscheinlichkeit']:.0f} %"
        )
    return "\n".join(parts)


def classify_day(w: dict) -> str:
    """Gibt 'indoor', 'outdoor' oder 'gemischt' zurück – rein regelbasiert."""
    regen_stark = w["regen_mm"] >= 3.0 or w["regen_wahrscheinlichkeit"] >= 60
    regen_leicht = w["regen_mm"] >= 1.0 or w["regen_wahrscheinlichkeit"] >= 35
    warm = w["temp_max"] >= 15
    windig = w["wind_kmh"] >= 40

    if regen_stark or windig:
        return "indoor"
    if regen_leicht or not warm:
        return "gemischt"
    return "outdoor"


ACTIVITY_LISTS = {
    "indoor": (
        "Museen (Museum Ludwig, Schokoladenmuseum, Odysseum, NS-Dok), "
        "Kino, Trampolinhallen, Bowlingbahn, Indoor-Klettern, "
        "Aquarium/Zoo-Innenanlagen, Badespaß (Agrippabad, Leverkusen-Bäder), "
        "Escape Rooms, Spielhallen, Bibliotheken mit Kinderprogramm"
    ),
    "outdoor": (
        "Rheinufer-Spaziergänge, Naturpark Bergisches Land, Stadtwald Köln, "
        "Kölner Zoo, Bayer-Erholungsgelände Leverkusen, Fahrradtouren, "
        "Spielplätze, Stadtgärten, Open-Air-Veranstaltungen, Bootsfahrten"
    ),
    "gemischt": (
        "halb Indoor/halb Outdoor: z.B. Zoo (mit Innenanlagen als Rückzug), "
        "Museen mit Außengelände, überdachte Märkte, Stadtbummel Köln-Innenstadt, "
        "Tierpark Leverkusen, Stadtbibliothek + nahegelegener Park"
    ),
}

FOCUS_LABEL = {
    "indoor":   "🏠 INDOOR – schlechtes Wetter",
    "outdoor":  "🌳 OUTDOOR – schönes Wetter",
    "gemischt": "🌤️ GEMISCHT – wechselhaftes Wetter",
}


def load_children() -> list[dict]:
    path = CONFIG_DIR / "children.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)["kinder"]


def children_for_prompt(kinder: list[dict]) -> str:
    lines = []
    for k in kinder:
        interessen = ", ".join(k["interessen"]) if k["interessen"] else "keine Angabe"
        lines.append(f"- {k['name']}, {k['alter']} Jahre alt → Interessen: {interessen}")
    return "\n".join(lines)


def find_events_with_claude(saturday: datetime.date, sunday: datetime.date, forecast: dict) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    sat_str = saturday.strftime("%d.%m.%Y")
    sun_str = sunday.strftime("%d.%m.%Y")

    # Wetterklassifikation je Tag – Python entscheidet, nicht Claude
    sat_typ = classify_day(forecast["Samstag"]) if "Samstag" in forecast else "gemischt"
    sun_typ = classify_day(forecast["Sonntag"]) if "Sonntag" in forecast else "gemischt"

    def day_block(tag: str, w: dict, typ: str) -> str:
        return (
            f"- {tag} ({w['datum']}): {w['emoji']} {w['beschreibung']}, "
            f"{w['temp_min']:.0f}–{w['temp_max']:.0f} °C, "
            f"Regen {w['regen_mm']:.1f} mm / {w['regen_wahrscheinlichkeit']:.0f} %, "
            f"Wind {w['wind_kmh']:.0f} km/h "
            f"→ **Fokus: {FOCUS_LABEL[typ]}**\n"
            f"  Geeignete Aktivitäten: {ACTIVITY_LISTS[typ]}"
        )

    weather_block = ""
    if "Samstag" in forecast:
        weather_block += day_block("Samstag", forecast["Samstag"], sat_typ) + "\n\n"
    if "Sonntag" in forecast:
        weather_block += day_block("Sonntag", forecast["Sonntag"], sun_typ)

    kinder = load_children()
    kinder_text = children_for_prompt(kinder)

    prompt = f"""Du bist ein hilfreicher Familienassistent. Erstelle konkrete Ausflugstipps
für das Wochenende {sat_str}–{sun_str} in der Region Leverkusen/Köln.

══════════════════════════════════════════
KINDER & INTERESSEN
══════════════════════════════════════════
{kinder_text}

Berücksichtige die Interessen bei jeder Empfehlung: Priorisiere Aktivitäten die zu mindestens
einem der genannten Interessen passen. Weise in der Beschreibung kurz darauf hin warum
das Kind diese Aktivität mögen wird.

══════════════════════════════════════════
WETTERVORHERSAGE + AKTIVITÄTSVORGABE
══════════════════════════════════════════
{weather_block}

══════════════════════════════════════════
DEINE AUFGABE
══════════════════════════════════════════
Empfiehl ausschließlich Aktivitäten die zum oben festgelegten Fokus (Indoor / Outdoor / Gemischt)
des jeweiligen Tages passen. Weiche NICHT davon ab.

Gib für jede Empfehlung an:
1. Name und kurze Beschreibung
2. Ort (Adresse oder Stadtteil in Köln/Leverkusen)
3. Öffnungszeiten / wann am besten hingehen
4. Warum es zu den Interessen der Kinder passt
5. Ungefähre Kosten

Struktur:
★ TOP-TIPP (1 Highlight für die ganze Familie, wettergerecht + interessengerecht)
📍 SAMSTAG – {FOCUS_LABEL[sat_typ]}: 2–3 Empfehlungen
📍 SONNTAG – {FOCUS_LABEL[sun_typ]}: 2–3 Empfehlungen

Schreibe kompakt und freundlich auf Deutsch."""

    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
        system=(
            "Du bist ein lokaler Familienassistent für die Region Leverkusen/Köln. "
            "Du folgst den Aktivitätsvorgaben (Indoor/Outdoor/Gemischt) strikt. "
            "Heute ist der " + datetime.date.today().strftime("%d.%m.%Y") + "."
        ),
    )
    return message.content[0].text


def format_slack_message(
    events_text: str,
    saturday: datetime.date,
    sunday: datetime.date,
    forecast: dict,
) -> dict:
    sat_str = saturday.strftime("%d.%m.%Y")
    sun_str = sunday.strftime("%d.%m.%Y")

    sat_typ = classify_day(forecast["Samstag"]) if "Samstag" in forecast else "gemischt"
    sun_typ = classify_day(forecast["Sonntag"]) if "Sonntag" in forecast else "gemischt"

    weather_lines = []
    for tag, w, typ in [
        ("Samstag", forecast.get("Samstag"), sat_typ),
        ("Sonntag", forecast.get("Sonntag"), sun_typ),
    ]:
        if w:
            weather_lines.append(
                f"{w['emoji']} *{tag} {w['datum']}:* {w['beschreibung']}, "
                f"{w['temp_min']:.0f}–{w['temp_max']:.0f} °C, "
                f"Regen {w['regen_wahrscheinlichkeit']:.0f} %  →  _{FOCUS_LABEL[typ]}_"
            )

    return {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"🎉 Familien-Wochenende {sat_str}–{sun_str}",
                    "emoji": True,
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "📍 Region Leverkusen/Köln  •  👧 Halley Malia (5 J.)  •  🧑 Samuel (13 J.)",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*🌤️ Wettervorhersage Leverkusen*\n" + "\n".join(weather_lines),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": events_text},
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"_Wetter: Open-Meteo · Events: Claude Opus · "
                            f"Generiert am {datetime.date.today().strftime('%d.%m.%Y')} · Family Events Mailer_"
                        ),
                    }
                ],
            },
        ]
    }


def post_to_slack(payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode()
            if body != "ok":
                raise RuntimeError(f"Slack antwortete mit: {body}")
        print("✅ Slack-Nachricht erfolgreich gesendet.")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Slack HTTP-Fehler {e.code}: {e.read().decode()}") from e


def main() -> None:
    saturday, sunday = get_weekend_dates()
    sat_str = saturday.strftime("%d.%m.%Y")
    sun_str = sunday.strftime("%d.%m.%Y")
    print(f"🌤️  Rufe Wettervorhersage für {sat_str}–{sun_str} ab …")

    forecast = fetch_weather_forecast(saturday, sunday)
    for tag, w in forecast.items():
        print(f"   {tag}: {w['emoji']} {w['beschreibung']}, {w['temp_min']:.0f}–{w['temp_max']:.0f} °C")

    print("🔍 Suche wettergerechte Events mit Claude …")
    events_text = find_events_with_claude(saturday, sunday, forecast)

    print("📝 Formatiere Slack-Nachricht …")
    payload = format_slack_message(events_text, saturday, sunday, forecast)
    post_to_slack(payload)


if __name__ == "__main__":
    main()
