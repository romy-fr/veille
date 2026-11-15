#!/usr/bin/env python3
"""
Température mondiale mensuelle depuis 1850 : écart à la normale, dans la charte des cartes J+6.

Donnée : HadCRUT5 (Met Office Hadley Centre & Climatic Research Unit, UEA), moyenne mondiale
terre + mer, mensuelle, de janvier 1850 au dernier mois publié. Licence Open Government Licence.
HadCRUT5 est fourni en écart à 1961-1990 ; le script recalcule l'écart à n'importe quelle période.

Deux références sur le graphique :
  - axe de gauche  : 1850-1900, la référence « préindustrielle » du GIEC et de l'accord de Paris
                     (c'est elle qui donne le seuil de +1,5 °C) ;
  - axe de droite  : 1991-2020, la normale climatologique officielle actuelle de l'OMM.
Le décalage entre les deux est constant : l'axe de droite est le même graphique, gradué autrement.

Usage :
    python build_temp.py                  # télécharge HadCRUT5 et produit le graphique
    python build_temp.py --csv fichier    # utilise un CSV déjà téléchargé (test hors ligne)

Sorties (dossier OUT_DIR) :
    temperature_mensuelle.png    le graphique
    temperature_mensuelle.json   la série recalculée (lue plus tard par l'app)
"""
from __future__ import annotations

import argparse
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
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap  # noqa: E402

# --------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------
HADCRUT = ("https://www.metoffice.gov.uk/hadobs/hadcrut5/data/HadCRUT.5.1.0.0/analysis/diagnostics/"
           "HadCRUT.5.1.0.0.analysis.summary_series.global.monthly.csv")
# Miroir (copie automatique de HadCRUT5 sur GitHub) si le site du Met Office ne répond pas
MIROIR = "https://raw.githubusercontent.com/datasets/global-temp/main/data/monthly.csv"

REF = (1850, 1900)            # référence de l'axe de gauche (préindustriel)
REF2 = (1991, 2020)           # référence de l'axe de droite (normale OMM actuelle)
LISSAGE_MOIS = 12             # moyenne glissante tracée en trait plein
A_MAX = 1.8                   # échelle de couleurs symétrique : de -A_MAX à +A_MAX, neutre à 0
A_LEVELS = np.round(np.arange(-A_MAX, A_MAX + 0.01, 0.2), 2)   # une teinte tous les 0,2 °C
# Mois récents pas encore publiés dans HadCRUT5 : valeurs Copernicus ERA5 (écart à 1850-1900),
# communiquées dans les bulletins mensuels de C3S. Utilisées seulement si HadCRUT5 n'a pas le mois.
COMPLEMENT = {"2026-08": 1.65, "2026-09": 1.63, "2026-10": 1.75}
OUT_DIR = pathlib.Path(os.environ.get("OUT_DIR", "sortie"))
TIMEOUT = 60

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

# Teintes des cartes Z500 (meteo/cartes_j6/build_maps.py), réarrangées en échelle divergente :
# froid (violet → bleu) | gris neutre à 0 | chaud (jaune → rouge sombre), symétrique autour de 0.
CMAP = LinearSegmentedColormap.from_list("anomalie", [
    (0.00, "#2b0b3f"), (0.17, "#3b2fbf"), (0.33, "#1f62d6"), (0.40, "#25c7d9"),
    (0.45, "#c4c4bf"), (0.55, "#c4c4bf"),
    (0.60, "#e6e22e"), (0.67, "#f7b733"), (0.75, "#f2762e"), (0.83, "#e0412c"),
    (0.92, "#b81f2a"), (1.00, "#7c0f1e")], N=len(A_LEVELS) + 1)
NORM = BoundaryNorm(A_LEVELS, CMAP.N, extend="both")


def fr(x: float, nd: int = 1, signe: bool = True) -> str:
    s = f"{x:+.{nd}f}" if signe else f"{x:.{nd}f}"
    return s.replace(".", ",").replace("-", "−")


# --------------------------------------------------------------------------
# Données
# --------------------------------------------------------------------------
def lire(csv_local: str | None) -> tuple[pd.Series, str]:
    """Série mensuelle (index = 1er du mois), en écart à 1961-1990 comme publiée."""
    if csv_local:
        txt, origine = pathlib.Path(csv_local).read_text(), "fichier local"
    else:
        try:
            r = requests.get(HADCRUT, timeout=TIMEOUT)
            r.raise_for_status()
            txt, origine = r.text, "Met Office"
        except Exception as exc:
            print(f"Met Office injoignable ({exc.__class__.__name__}), passage par le miroir GitHub")
            r = requests.get(MIROIR, timeout=TIMEOUT)
            r.raise_for_status()
            txt, origine = r.text, "miroir GitHub"
    df = pd.read_csv(io.StringIO(txt))
    if "Source" in df.columns:                       # format du miroir : Source, Year (AAAA-MM), Mean
        df = df[df["Source"] == "GCAG"]               # « GCAG » = HadCRUT5 dans ce miroir
        temps, val = df["Year"], df["Mean"]
    else:                                            # format Met Office : Time, Anomaly (deg C), ...
        temps, val = df.iloc[:, 0], df.iloc[:, 1]
    s = pd.Series(val.astype(float).values, index=pd.to_datetime(temps.astype(str).str[:7]))
    return s.sort_index().dropna(), origine


def rebase(s: pd.Series, ref: tuple[int, int]) -> pd.Series:
    """Écart à la moyenne de la période ref (mois par mois, pour garder la même saisonnalité)."""
    per = s[(s.index.year >= ref[0]) & (s.index.year <= ref[1])]
    clim = per.groupby(per.index.month).mean()
    return s - clim.reindex(s.index.month).values


# --------------------------------------------------------------------------
# Rendu (même gabarit que les cartes : titre gras à gauche, légende à droite,
# échelle de couleurs horizontale en bas, source en pied)
# --------------------------------------------------------------------------
def completer(a: pd.Series) -> tuple[pd.Series, list[pd.Timestamp]]:
    """Ajoute les mois de COMPLEMENT absents de la série (déjà en écart à 1850-1900)."""
    ajout = {pd.Timestamp(k + "-01"): v for k, v in COMPLEMENT.items() if pd.Timestamp(k + "-01") > a.index[-1]}
    return pd.concat([a, pd.Series(ajout, dtype=float)]).sort_index(), sorted(ajout)


def render(a: pd.Series, decal: float, path: pathlib.Path, ajouts: list = ()) -> dict:
    liss = a.rolling(LISSAGE_MOIS, center=True, min_periods=LISSAGE_MOIS).mean()
    dernier, rec = a.index[-1], a.idxmax()
    annees = a.index.year + (a.index.month - 0.5) / 12

    W, top, bottom, plot_h = 9.6, 1.0, 1.15, 4.6
    H = top + plot_h + bottom
    fig = plt.figure(figsize=(W, H), dpi=110)
    yf = lambda inches_from_top: 1 - inches_from_top / H  # noqa: E731
    ax = fig.add_axes([0.075, (bottom + 0.30) / H, 0.85, (plot_h - 0.30) / H])

    ax.axhline(0, color="#1a1a1a", lw=0.8, zorder=1)
    ax.annotate(f"moyenne {REF[0]}-{REF[1]} (préindustriel)", xy=(1962, 0), xytext=(1950, -0.45),
                fontsize=8, color="#333333", ha="center", va="center",
                arrowprops=dict(arrowstyle="-", color="#6b6b6b", lw=0.7), zorder=4)
    ax.axhline(decal, color="#6b6b6b", lw=0.8, ls=(0, (1, 2)), zorder=1)
    ax.text(1851, decal + 0.03, f"normale {REF2[0]}-{REF2[1]}", fontsize=8, color="#6b6b6b", va="bottom",
            bbox=dict(fc="white", ec="none", pad=0.5, alpha=0.85), zorder=4)

    pts = ax.scatter(annees, a.values, c=a.values, cmap=CMAP, norm=NORM, s=5, lw=0, zorder=2)
    if ajouts:                                       # mois ajoutés depuis ERA5 : cerclés
        xa = [d.year + (d.month - 0.5) / 12 for d in ajouts]
        ax.scatter(xa, a[ajouts].values, c=a[ajouts].values, cmap=CMAP, norm=NORM, s=22,
                   edgecolors="#1a1a1a", linewidths=0.7, zorder=4)
    ax.plot(annees, liss.values, color="#1a1a1a", lw=1.4, zorder=3)

    x_rec = rec.year + (rec.month - 0.5) / 12
    ax.annotate(f"record : {MOIS[rec.month - 1]} {rec.year}\n{fr(a[rec], 2)} °C",
                xy=(x_rec, a[rec]), xytext=(x_rec - 38, a[rec] + 0.05), fontsize=8.5, color="#1a1a1a",
                ha="left", va="center", arrowprops=dict(arrowstyle="-", color="#6b6b6b", lw=0.7))

    ax.set_xlim(1849, dernier.year + 2)
    lo = np.floor((a.min() - 0.1) * 5) / 5
    hi = np.ceil((a.max() + 0.15) * 5) / 5
    ax.set_ylim(lo, hi)
    ax.set_xticks(range(1850, dernier.year + 1, 25))
    ax.set_yticks(np.arange(np.ceil(lo * 2) / 2, hi + 0.01, 0.5))
    ax.yaxis.set_major_formatter(lambda v, _: fr(v, 1) if abs(v) > 1e-9 else "0")
    ax.set_ylabel(f"écart à {REF[0]}-{REF[1]} (°C)", fontsize=9, color="#333333")
    ax.grid(axis="y", color="#e3e3df", lw=0.6, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#9a9a96")
    ax.tick_params(labelsize=8.5, colors="#333333")

    ax2 = ax.twinx()                                  # même courbe, graduée par rapport à 1991-2020
    ax2.set_ylim(lo - decal, hi - decal)
    ax2.set_yticks(np.arange(np.ceil((lo - decal) * 2) / 2, hi - decal + 0.01, 0.5))
    ax2.yaxis.set_major_formatter(lambda v, _: fr(v, 1) if abs(v) > 1e-9 else "0")
    ax2.set_ylabel(f"écart à {REF2[0]}-{REF2[1]} (°C)", fontsize=9, color="#6b6b6b")
    ax2.tick_params(labelsize=8.5, colors="#6b6b6b")
    for side in ("top", "left", "bottom"):
        ax2.spines[side].set_visible(False)
    ax2.spines["right"].set_color("#c9c9c5")

    fig.text(0.02, yf(0.18), "Température mondiale", fontsize=22, fontweight="bold", va="top")
    fig.text(0.02, yf(0.62),
             f"Mensuelle, janvier 1850 → {MOIS[dernier.month - 1]} {dernier.year}  ·  "
             f"{MOIS[dernier.month - 1]} {dernier.year} : {fr(a.iloc[-1], 2)} °C  ·  "
             f"moyenne sur 12 mois : {fr(a.iloc[-12:].mean(), 2)} °C",
             fontsize=10.5, va="top", color="#333333")
    fig.text(0.98, yf(0.18), "Écart à la moyenne préindustrielle (°C)\npoints : mois  ·  trait : moyenne sur 12 mois",
             fontsize=9, ha="right", va="top", color="#333333")

    cax = fig.add_axes([0.10, 0.70 / H, 0.80, 0.17 / H])
    cb = fig.colorbar(pts, cax=cax, orientation="horizontal", ticks=A_LEVELS[::3])
    cb.minorticks_off()
    cb.ax.tick_params(labelsize=7)
    cb.ax.xaxis.set_major_formatter(lambda v, _: fr(v, 1) if abs(v) > 1e-9 else "0")
    pied = ("Données : HadCRUT5 (Met Office Hadley Centre & CRU/UEA, terre + mer) — Open Government Licence"
            "  ·  recalculé par rapport à 1850-1900 et 1991-2020")
    if ajouts:
        pied += (f"\nPoints cerclés ({MOIS[ajouts[0].month - 1]} → {MOIS[ajouts[-1].month - 1]} {ajouts[-1].year}) : "
                 "Copernicus C3S / ECMWF (ERA5), en attendant leur publication dans HadCRUT5")
    fig.text(0.5, 0.06 / H, pied,
             fontsize=7.5, ha="center", color="#555555")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return {"record": {"mois": rec.strftime("%Y-%m"), "ecart": round(float(a[rec]), 3)},
            "dernier": {"mois": dernier.strftime("%Y-%m"), "ecart": round(float(a.iloc[-1]), 3)}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="CSV HadCRUT5 (format Met Office ou miroir) déjà téléchargé")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    brut, origine = lire(args.csv)
    a = rebase(brut, REF)                             # écart au préindustriel
    decal = float((a - rebase(brut, REF2)).mean())    # réchauffement de 1850-1900 à 1991-2020
    a, ajouts = completer(a)
    print(f"{origine} : {len(a)} mois, {a.index[0]:%Y-%m} → {a.index[-1]:%Y-%m} ; "
          f"1991-2020 = {decal:+.2f} °C au-dessus de 1850-1900")

    info = render(a, decal, OUT_DIR / "temperature_mensuelle.png", ajouts)
    out = {"source": "HadCRUT5 (Met Office / CRU)", "origine": origine,
           "reference": f"{REF[0]}-{REF[1]}", "normale_omm": f"{REF2[0]}-{REF2[1]}",
           "decalage_normale_omm": round(decal, 3), **info,
           "mois_era5": [d.strftime("%Y-%m") for d in ajouts],
           "mois": [d.strftime("%Y-%m") for d in a.index], "ecart": [round(float(v), 3) for v in a.values]}
    (OUT_DIR / "temperature_mensuelle.json").write_text(json.dumps(out, ensure_ascii=False))
    print(f"Record : {info['record']} ; dernier mois : {info['dernier']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
