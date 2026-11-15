#!/usr/bin/env python3
"""
Notifications « séances de sport » : la veille de chaque séance, à l'heure choisie, envoie sur l'iPhone
(app ntfy) un résumé du temps prévu entre l'heure de départ et l'heure de retour.

Lancé toutes les heures par GitHub Actions (.github/workflows/sport-notifications.yml).
Séances : variable d'Actions SEANCES (JSON copié depuis la page, onglet Prévisions → Séances),
gardée hors du dépôt pour ne pas publier ses horaires et ses lieux.
Envoi : secret NTFY_TOPIC (sujet ntfy auquel l'iPhone est abonné).

Mêmes règles de résumé que la page (fonction summarize de meteo/cartes_j6/index.html).
Données : Open-Meteo.com (CC BY 4.0) — AROME (Météo-France) à J+1, ECMWF IFS en secours.

Usage : python notif_sport.py [--maintenant 2026-11-15T20:05] [--test] [--sec]
  --test : envoie tout de suite le résumé de la prochaine séance de chaque entrée (vérification)
  --sec  : n'envoie rien, affiche seulement les messages
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
from zoneinfo import ZoneInfo

import requests

PARIS = ZoneInfo("Europe/Paris")
MODELS = ("meteofrance_arome_france", "ecmwf_ifs025")
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "veille-sport/1.0 (usage personnel, non commercial)"

LABELS = {0: "Ensoleillé", 1: "Peu nuageux", 2: "Éclaircies", 3: "Couvert", 45: "Brouillard", 48: "Brouillard givrant",
          51: "Bruine faible", 53: "Bruine", 55: "Bruine forte", 56: "Bruine verglaçante", 57: "Bruine verglaçante forte",
          61: "Pluie faible", 63: "Pluie", 65: "Pluie forte", 66: "Pluie verglaçante", 67: "Pluie verglaçante forte",
          71: "Neige faible", 73: "Neige", 75: "Neige forte", 77: "Grains de neige", 80: "Averses faibles", 81: "Averses",
          82: "Fortes averses", 85: "Averses de neige", 86: "Fortes averses de neige", 95: "Orage", 96: "Orage avec grêle",
          99: "Orage, forte grêle"}
TAGS = {"orage": "zap", "neige": "snowflake", "verglas": "ice_cube", "pluie": "umbrella"}


def label(code: int, is_day: int) -> str:
    if code == 0 and not is_day:
        return "Nuit claire"
    if code in LABELS:
        return LABELS[code]
    return LABELS[max([k for k in LABELS if k <= code] or [0])]


def sev(c: int) -> int:
    return {95: 100, 96: 101, 99: 102}.get(c, c)


def kind(c: int) -> str:
    if c >= 95 or 80 <= c <= 82:
        return "orage"
    if 71 <= c <= 77 or c in (85, 86):
        return "neige"
    if c in (56, 57, 66, 67):
        return "verglas"
    return "pluie"


def num(v: float) -> str:
    return f"{v:.1f}".replace(".", ",") if v < 10 else str(round(v))


def hourly(lat: float, lon: float) -> list[dict]:
    vars_ = "weather_code,temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,is_day"
    r = HTTP.get("https://api.open-meteo.com/v1/forecast", timeout=60, params={
        "latitude": lat, "longitude": lon, "hourly": vars_, "models": ",".join(MODELS), "timezone": "GMT",
        "timeformat": "unixtime", "forecast_days": 3, "wind_speed_unit": "kmh"})
    r.raise_for_status()
    h = r.json()["hourly"]
    g = lambda v, m, i: (h.get(f"{v}_{m}") or [None] * len(h["time"]))[i]  # noqa: E731
    rows = []
    for i, ts in enumerate(h["time"]):
        m = next((m for m in MODELS if g("temperature_2m", m, i) is not None), None)
        if m is None:
            continue
        rows.append({"t": dt.datetime.fromtimestamp(ts, dt.timezone.utc), "code": int(g("weather_code", m, i) or 0),
                     "temp": g("temperature_2m", m, i), "mm": g("precipitation", m, i) or 0.0,
                     "wind": g("wind_speed_10m", m, i), "gust": g("wind_gusts_10m", m, i),
                     "is_day": g("is_day", m, i), "src": m})
    return rows


def summarize(rows: list[dict], t0: dt.datetime, t1: dt.datetime) -> dict | None:
    half = dt.timedelta(minutes=30)
    R = [r for r in rows if t0 - half <= r["t"] <= t1 + half]
    if not R:
        return None
    P = [r for r in rows if t0 < r["t"] <= max(t1, t0 + dt.timedelta(hours=1))]
    first, last = R[0], R[-1]
    worst = max(R, key=lambda r: sev(r["code"]))
    seq = []
    for r in (first, worst, last):
        lab = label(r["code"], r["is_day"])
        if lab not in seq:
            seq.append(lab)
    tmin = min(r["temp"] for r in R)
    mm = sum(r["mm"] for r in P)
    winds = [r["wind"] for r in R if r["wind"] is not None]
    gusts = [r["gust"] for r in R if r["gust"] is not None]
    wind = sum(winds) / len(winds) if winds else None
    gust = max(gusts) if gusts else None
    k, alerts = kind(worst["code"]), []
    if worst["code"] >= 95:
        alerts.append("⚡ orage possible")
    if k == "neige":
        alerts.append("❄️ neige")
    if k == "verglas" or (tmin <= 0.5 and mm >= 0.1 and k != "neige"):
        alerts.append("🧊 risque de verglas")
    elif tmin <= 0:
        alerts.append("🥶 gel")
    if gust is not None and gust >= 60:
        alerts.append(f"💨 rafales {round(gust)} km/h")
    if mm >= 5:
        alerts.append(f"🌧️ {num(mm)} mm")
    a, b = round(first["temp"]), round(last["temp"])
    parts = [" puis ".join(s if i == 0 else s.lower() for i, s in enumerate(seq)),
             f"{a} °C" if a == b else f"{a} → {b} °C", f"{num(mm)} mm" if mm >= 0.1 else "sec"]
    if wind is not None:
        parts.append(f"vent {round(wind)} km/h, rafales {round(gust)}")
    src = ("AROME" if all(r["src"].startswith("meteo") for r in R)
           else "AROME + IFS" if any(r["src"].startswith("meteo") for r in R) else "ECMWF IFS")
    dry = mm < 0.1 and worst["code"] < 50
    return {"text": " · ".join(parts), "alerts": alerts, "src": src,
            "tag": "sunny" if dry and worst["code"] <= 2 else "cloud" if dry else TAGS[k]}


def hm(t: dt.datetime) -> str:
    return f"{t:%H}h" + ("" if t.minute == 0 else f"{t:%M}")


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def message(s: dict, day: dt.date) -> dict | None:
    h, m = map(int, s["depart"].split(":"))
    t0 = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=PARIS)
    t1 = t0 + dt.timedelta(minutes=int(s.get("duree_min", 120)))
    sm = summarize(hourly(float(s["lat"]), float(s["lon"])), t0.astimezone(dt.timezone.utc), t1.astimezone(dt.timezone.utc))
    if not sm:
        return None
    body = sm["text"] + ("\n" + " · ".join(sm["alerts"]) if sm["alerts"] else "")
    body += f"\nSource : {sm['src']} via Open-Meteo.com"
    return {"title": f"{s.get('nom', 'Sport')} {JOURS[day.weekday()]} {hm(t0)} → {hm(t1)} · {s.get('lieu', '')}".strip(" ·"),
            "message": body, "tags": [sm["tag"]] + (["warning"] if sm["alerts"] else []),
            "priority": 4 if sm["alerts"] else 3}


def load() -> list[dict]:
    raw = os.environ.get("SEANCES", "").strip()
    if not raw:
        f = pathlib.Path(__file__).with_name("seances.json")    # test local uniquement
        raw = f.read_text() if f.exists() else "{}"
    data = json.loads(raw)
    return data.get("seances", data if isinstance(data, list) else [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--maintenant", help="heure de Paris simulée, ex. 2026-11-15T20:05")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--sec", action="store_true")
    a = ap.parse_args()
    now = (dt.datetime.fromisoformat(a.maintenant).replace(tzinfo=PARIS) if a.maintenant else dt.datetime.now(PARIS))
    seances = load()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    print(f"{len(seances)} séance(s) configurée(s), heure de Paris {now:%d/%m %H:%M}")
    sent = 0
    for i, s in enumerate(seances, 1):
        try:
            jours = [int(j) for j in s["jours"]]
            if a.test:      # prochaine occurrence, à partir d'aujourd'hui
                day = next(now.date() + dt.timedelta(days=n) for n in range(8)
                           if (now.date() + dt.timedelta(days=n)).isoweekday() in jours)
            else:
                day = now.date() + dt.timedelta(days=1)
                nh = int(str(s.get("notif_veille", "20:00")).split(":")[0])
                if day.isoweekday() not in jours or now.hour != nh:
                    continue
            msg = message(s, day)
            if not msg:
                print(f"  séance {i} : pas de prévision disponible")
                continue
            if a.sec or not topic:
                print(("  (NTFY_TOPIC absent) " if not topic and not a.sec else "  ") + json.dumps(msg, ensure_ascii=False))
                continue
            r = HTTP.post("https://ntfy.sh/", json={"topic": topic, **msg}, timeout=30)
            r.raise_for_status()
            sent += 1
            print(f"  séance {i} : notification envoyée")
        except Exception as exc:  # une séance en erreur n'empêche pas les autres
            print(f"  séance {i} : erreur {exc}")
    print(f"{sent} notification(s) envoyée(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
