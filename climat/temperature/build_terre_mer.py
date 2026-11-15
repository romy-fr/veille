#!/usr/bin/env python3
"""
Température par région : terres émergées contre océans, moyenne annuelle depuis 1850,
dans la charte des cartes J+6. Une image par région (Monde, hémisphères nord et sud,
Arctique, Antarctique), choisie dans l'onglet Climat de l'app.

Donnée : NOAA NCEI, Climate at a Glance (NOAAGlobalTemp), séries mensuelles « land » et
« ocean » (Monde et hémisphères) ou « land_ocean » (Arctique et Antarctique, que la NOAA ne
sépare pas), publiées en écart à la moyenne 1901-2000. Le script les recalcule
en écart à 1850-1900 (référence préindustrielle), mois par mois, puis fait la moyenne par année.
L'année en cours est la moyenne des mois disponibles (signalée sur le graphique).

Usage :
    python build_terre_mer.py                    # télécharge les séries NOAA
    python build_terre_mer.py --annuel FICHIER   # test hors ligne (lignes 'land 1850:x ...' / 'ocean ...')

Sorties (dossier OUT_DIR) :
    temperature_terre_mer_<region>.png, temperature_terre_mer.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import pathlib

import numpy as np
import pandas as pd
import requests

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# --------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------
NOAA = ("https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/global/time-series/"
        "{region}/tavg/{serie}/1/0/1850-{fin}/data.csv")
REF = (1850, 1900)
LISSAGE_ANS = 10
SERIES = {"land": {"nom": "Terres", "couleur": "#e0412c"},
          "ocean": {"nom": "Océans", "couleur": "#1f62d6"},
          "land_ocean": {"nom": "Terres et mers", "couleur": "#5a1d7a"}}
REGIONS = {   # clé de l'app : (région NOAA, titre, séries)
    "monde": ("globe", "Monde", ["land", "ocean"]),
    "hn": ("nhem", "Hémisphère nord", ["land", "ocean"]),
    "hs": ("shem", "Hémisphère sud", ["land", "ocean"]),
    "arctique": ("arctic", "Arctique", ["land_ocean"]),
    "antarctique": ("antarctic", "Antarctique", ["land_ocean"]),
}
OUT_DIR = pathlib.Path(os.environ.get("OUT_DIR", "sortie"))
TIMEOUT = 60
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
MOIS_COURT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def fr(x: float, nd: int = 2, signe: bool = True) -> str:
    s = f"{x:+.{nd}f}" if signe else f"{x:.{nd}f}"
    return s.replace(".", ",").replace("-", "−")


# --------------------------------------------------------------------------
# Données
# --------------------------------------------------------------------------
def lire_noaa(region: str, serie: str) -> tuple[pd.Series, int]:
    """Moyennes annuelles en écart à REF, et nombre de mois de la dernière année."""
    r = requests.get(NOAA.format(region=region, serie=serie, fin=dt.date.today().year), timeout=TIMEOUT)
    r.raise_for_status()
    lignes = [l for l in r.text.splitlines() if l[:6].isdigit()]
    df = pd.read_csv(io.StringIO("\n".join(lignes)), header=None, names=["d", "v"], dtype={"d": str})
    df["annee"], df["mois"] = df["d"].str[:4].astype(int), df["d"].str[4:6].astype(int)
    ref = df[(df["annee"] >= REF[0]) & (df["annee"] <= REF[1])].groupby("mois")["v"].mean()
    df["a"] = df["v"] - df["mois"].map(ref)
    g = df.groupby("annee")["a"]
    return g.mean(), int(g.size().iloc[-1])


def lire_annuel(fichier: str) -> dict[str, pd.Series]:
    """Format de test : une ligne 'regionNOAA_serie 1850:x 1851:y ...' par série."""
    out = {}
    for ligne in pathlib.Path(fichier).read_text().splitlines():
        nom, *paires = ligne.split()
        out[nom] = pd.Series({int(p.split(":")[0]): float(p.split(":")[1]) for p in paires})
    return out


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------
def render(cle: str, ser: dict[str, pd.Series], n_mois: int, path: pathlib.Path) -> dict:
    region, titre, series = REGIONS[cle]
    W, top, bottom, plot_h = 9.6, 1.0, 0.55, 4.6
    H = top + plot_h + bottom
    fig = plt.figure(figsize=(W, H), dpi=110)
    yf = lambda inches_from_top: 1 - inches_from_top / H  # noqa: E731
    ax = fig.add_axes([0.075, (bottom + 0.30) / H, 0.85, (plot_h - 0.30) / H])  # pas d'échelle de couleurs ici

    derniere = int(ser[series[0]].index.max())
    complet = n_mois == 12
    info = {}
    ax.axhline(0, color="#1a1a1a", lw=0.8, zorder=1)
    for k in series:
        p, s = SERIES[k], ser[k]
        s_complet = s if complet else s.iloc[:-1]
        liss = s_complet.rolling(LISSAGE_ANS, center=True, min_periods=LISSAGE_ANS).mean()
        ax.plot(s.index, s.values, color=p["couleur"], lw=0.8, alpha=0.45, zorder=2)
        ax.scatter(s.index, s.values, color=p["couleur"], s=7, lw=0, alpha=0.7, zorder=2)
        ax.plot(liss.index, liss.values, color=p["couleur"], lw=2.2, zorder=3)
        if not complet:                                  # année en cours : point cerclé
            ax.scatter([derniere], [s.iloc[-1]], color=p["couleur"], s=26, edgecolors="#1a1a1a",
                       linewidths=0.7, zorder=4)
        ax.annotate(f"{p['nom']}\n{fr(s.iloc[-1])} °C", xy=(derniere, s.iloc[-1]), xytext=(8, 0),
                    textcoords="offset points", fontsize=9, fontweight="bold", color=p["couleur"],
                    va="center", zorder=5)
        dix = s_complet.iloc[-10:].mean()
        info[k] = {"dernier": round(float(s.iloc[-1]), 3), "moyenne_10_ans": round(float(dix), 3),
                   "annees": [int(y) for y in s.index], "ecart": [round(float(v), 3) for v in s.values]}

    rapport = info["land"]["moyenne_10_ans"] / info["ocean"]["moyenne_10_ans"] if "land" in info else None
    ax.set_xlim(1849, derniere + 9)
    lo = np.floor((min(s.min() for s in ser.values()) - 0.1) * 2) / 2
    hi = np.ceil((max(s.max() for s in ser.values()) + 0.2) * 2) / 2
    ax.set_ylim(lo, hi)
    ax.set_xticks(range(1850, derniere + 1, 25))
    ax.set_yticks(np.arange(lo, hi + 0.01, 0.5))
    ax.yaxis.set_major_formatter(lambda v, _: fr(v, 1) if abs(v) > 1e-9 else "0")
    ax.set_ylabel(f"écart à {REF[0]}-{REF[1]} (°C)", fontsize=9, color="#333333")
    ax.grid(axis="y", color="#e3e3df", lw=0.6, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#9a9a96")
    ax.tick_params(labelsize=8.5, colors="#333333")
    ax.annotate(f"moyenne {REF[0]}-{REF[1]} (préindustriel)", xy=(1962, 0), xytext=(1950, -0.45),
                fontsize=8, color="#333333", ha="center", va="center",
                arrowprops=dict(arrowstyle="-", color="#6b6b6b", lw=0.7), zorder=4)

    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([], [], color="#6b6b6b", lw=0.8, marker="o", ms=3, alpha=0.6, label="année"),
                       Line2D([], [], color="#6b6b6b", lw=2.2, label=f"moyenne sur {LISSAGE_ANS} ans")],
              loc="upper left", frameon=False, fontsize=8, labelcolor="#333333")

    periode = f"{derniere}" + ("" if complet else f" (janv.-{MOIS_COURT[n_mois - 1]})")
    fig.text(0.02, yf(0.18), titre, fontsize=22, fontweight="bold", va="top")
    if rapport:
        resume = (f"10 dernières années : terres {fr(info['land']['moyenne_10_ans'])} °C, "
                  f"océans {fr(info['ocean']['moyenne_10_ans'])} °C (× {fr(rapport, 1, False)})")
    else:
        resume = (f"10 dernières années : {fr(info['land_ocean']['moyenne_10_ans'])} °C "
                  f"(terres et mers confondues)")
    fig.text(0.02, yf(0.62), f"Moyenne annuelle, 1850 → {periode}  ·  {resume}",
             fontsize=10.5, va="top", color="#333333")
    fig.text(0.98, yf(0.18), "Écart à la moyenne préindustrielle (°C)\npoints : années  ·  traits : moyenne sur 10 ans",
             fontsize=9, ha="right", va="top", color="#333333")
    fig.text(0.5, 0.04 / H,
             f"Données : NOAA NCEI, Climate at a Glance (NOAAGlobalTemp), région « {region} »"
             + (", terres et océans séparés" if rapport else ", terres et mers confondues (non séparées par la NOAA)")
             + "\nrecalculées par rapport à 1850-1900 · océans = température de surface de la mer"
             + ("" if complet else f" · {derniere} : moyenne des mois disponibles (point cerclé)"),
             fontsize=7.5, ha="center", color="#555555")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return {"titre": titre, "region_noaa": region,
            "rapport_terre_ocean_10_ans": round(rapport, 2) if rapport else None, **info}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--annuel", help="fichier de moyennes annuelles déjà calculées (test hors ligne)")
    ap.add_argument("--mois", type=int, default=12, help="avec --annuel : nombre de mois de la dernière année")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hors_ligne = lire_annuel(args.annuel) if args.annuel else None
    out = {"source": "NOAA NCEI Climate at a Glance", "reference": f"{REF[0]}-{REF[1]}", "regions": {}}
    for cle, (region, titre, series) in REGIONS.items():
        ser, n = {}, args.mois
        for k in series:
            if hors_ligne is not None:
                ser[k] = hors_ligne[f"{region}_{k}"]
            else:
                ser[k], n = lire_noaa(region, k)
        info = render(cle, ser, n, OUT_DIR / f"temperature_terre_mer_{cle}.png")
        out["regions"][cle] = info
        print(titre, {k: info[k]["dernier"] for k in series}, "rapport", info["rapport_terre_ocean_10_ans"])
    (OUT_DIR / "temperature_terre_mer.json").write_text(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
