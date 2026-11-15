#!/usr/bin/env python3
"""
Étendue de la banquise au jour le jour, une courbe par année (« spaghetti »), Arctique et
Antarctique, dans la charte des cartes J+6.

Donnée : NSIDC Sea Ice Index v4 (G02135), étendue quotidienne (surface où au moins 15 % de
l'océan est couvert de glace), novembre 1978 → hier.

Sur le graphique :
  - chaque année de 1979 à l'an dernier : un trait fin, coloré du violet (années anciennes)
    au rouge (années récentes) ;
  - moyenne 1981-2010 (référence du NSIDC) en tirets, normale 1991-2020 en pointillés ;
  - l'année en cours en trait noir épais, avec sa dernière valeur ;
  - l'année du plus bas minimum annuel, nommée sur la courbe.

Usage :
    python build_banquise_annuel.py                 # télécharge les CSV du NSIDC
    python build_banquise_annuel.py --spag DOSSIER  # test hors ligne (spag_north.txt / spag_south.txt)

Sorties (dossier OUT_DIR) :
    banquise_annuelle_arctique.png, banquise_annuelle_antarctique.png
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
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
NSIDC = "https://noaadata.apps.nsidc.org/NOAA/G02135/{h}/daily/data/{H}_seaice_extent_daily_v4.0.csv"
REF = (1981, 2010)
REF2 = (1991, 2020)
PREMIERE_ANNEE = 1979          # 1978 ne commence qu'en novembre
OUT_DIR = pathlib.Path(os.environ.get("OUT_DIR", "sortie"))
TIMEOUT = 60
POLES = {
    "north": {"titre": "Banquise arctique", "fichier": "banquise_annuelle_arctique.png"},
    "south": {"titre": "Banquise antarctique", "fichier": "banquise_annuelle_antarctique.png"},
}
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
MOIS_COURT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
DEBUT_MOIS = [1, 32, 61, 92, 122, 153, 183, 214, 245, 275, 306, 336]   # jour (année de 366 j)

# Palette des années : mêmes teintes que les cartes, des anciennes (violet) aux récentes (rouge)
CMAP_ANNEES = LinearSegmentedColormap.from_list("annees", [
    "#3b2fbf", "#1f62d6", "#1e9be0", "#25c7d9", "#3fd18b", "#8fdc3a",
    "#e6e22e", "#f7b733", "#f2762e", "#e0412c", "#b81f2a"])


def fr(x: float, nd: int = 2) -> str:
    return f"{x:.{nd}f}".replace(".", ",").replace("-", "−")


# --------------------------------------------------------------------------
# Données : tableau année × jour de l'année (1..366), en millions de km²
# --------------------------------------------------------------------------
def jour_annee(idx: pd.DatetimeIndex) -> np.ndarray:
    doy = idx.dayofyear.values.copy()
    doy[(~idx.is_leap_year) & (idx.month > 2)] += 1   # le 1er mars est toujours le jour 61
    return doy


def lire_quotidien(h: str) -> pd.DataFrame:
    r = requests.get(NSIDC.format(h=h, H=h[0].upper()), timeout=TIMEOUT)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), skiprows=[1], skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    d = pd.to_datetime(dict(year=df["Year"], month=df["Month"], day=df["Day"]))
    s = pd.Series(df["Extent"].astype(float).values, index=d)
    s = s[s > 0].sort_index()
    t = pd.DataFrame({"annee": s.index.year, "jour": jour_annee(s.index), "v": s.values})
    tab = t.pivot_table(index="annee", columns="jour", values="v").reindex(columns=range(1, 367))
    # Débuts de série : une mesure tous les deux jours -> interpolation sur les trous d'un jour
    tab = tab.T.interpolate(limit=1, limit_area="inside").T
    tab.loc[:, 60] = tab.loc[:, 60].where(tab.index.map(lambda y: y % 4 == 0))  # 29 février
    return tab


def lire_spag(dossier: str, h: str) -> pd.DataFrame:
    """Format de test : 'AAAA:v1,v2,...' (moyennes sur 5 jours centrées sur les jours 3, 8, 13...)
    et '2026odd:...' (un jour sur deux depuis le 1er janvier), valeurs en centièmes de M km²."""
    tab = pd.DataFrame(index=[], columns=range(1, 367), dtype=float)
    for ligne in (pathlib.Path(dossier) / f"spag_{h}.txt").read_text().splitlines():
        cle, vals = ligne.split(":")
        v = [float(x) / 100 if x else np.nan for x in vals.split(",")]
        if cle.endswith("odd"):
            annee, jours = int(cle[:4]), range(1, 367, 2)
        else:
            annee, jours = int(cle), range(3, 367, 5)
        row = pd.Series(np.nan, index=range(1, 367))
        for j, x in zip(jours, v):
            row[j] = x
        tab.loc[annee] = row.interpolate(limit_area="inside").values
    return tab.sort_index()


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------
def render(h: str, tab: pd.DataFrame, path: pathlib.Path) -> dict:
    en_cours = int(tab.index.max())
    annees = [y for y in tab.index if PREMIERE_ANNEE <= y < en_cours]
    jours = np.arange(1, 367)
    clim1 = tab.loc[REF[0]:REF[1]].mean().rolling(7, center=True, min_periods=1).mean()
    clim2 = tab.loc[REF2[0]:REF2[1]].mean().rolling(7, center=True, min_periods=1).mean()
    actu = tab.loc[en_cours].dropna()
    j_der, v_der = int(actu.index[-1]), float(actu.iloc[-1])
    ecart = v_der - float(clim1[j_der])
    # année du plus bas minimum (Arctique : septembre ; Antarctique : février)
    minis = tab.loc[annees].min(axis=1)
    an_rec = int(minis.idxmin())

    W, top, bottom, plot_h = 9.6, 1.0, 1.15, 4.6
    H = top + plot_h + bottom
    fig = plt.figure(figsize=(W, H), dpi=110)
    yf = lambda inches_from_top: 1 - inches_from_top / H  # noqa: E731
    ax = fig.add_axes([0.075, (bottom + 0.30) / H, 0.85, (plot_h - 0.30) / H])

    norm = plt.Normalize(PREMIERE_ANNEE, en_cours - 1)
    for y in annees:
        ax.plot(jours, tab.loc[y].values, color=CMAP_ANNEES(norm(y)), lw=0.7, alpha=0.75, zorder=2)
    ax.plot(jours, clim1.values, color="#1a1a1a", lw=1.0, ls=(0, (5, 3)), zorder=3)
    ax.plot(jours, clim2.values, color="#1a1a1a", lw=1.0, ls=(0, (1, 2)), zorder=3)
    ax.plot(actu.index, actu.values, color="#1a1a1a", lw=2.4, zorder=5)
    ax.scatter([j_der], [v_der], s=28, color="#1a1a1a", zorder=6)

    # étiquettes : année en cours et année record
    d = dt.date(2024, 1, 1) + dt.timedelta(days=j_der - 1)
    ax.annotate(f"{en_cours}\n{d.day} {MOIS[d.month - 1]} : {fr(v_der)} M km²\n({fr(ecart)} vs 1981-2010)",
                xy=(j_der, v_der), xytext=(12, 0 if h == "north" else -34), textcoords="offset points",
                fontsize=8.5, color="#1a1a1a", va="center", fontweight="bold", zorder=7,
                bbox=dict(fc="white", ec="none", pad=1.5, alpha=0.85))
    j_rec = int(tab.loc[an_rec].idxmin())
    ax.annotate(f"{an_rec} : record bas\n{fr(float(minis[an_rec]))} M km²",
                xy=(j_rec, float(minis[an_rec])),
                xytext=(-60, -6) if j_rec > 120 else (60, -4), textcoords="offset points",
                fontsize=8, color="#b81f2a", ha="right" if j_rec > 120 else "left", va="top",
                arrowprops=dict(arrowstyle="-", color="#b81f2a", lw=0.7), zorder=7)

    from matplotlib.lines import Line2D
    loc = "lower left" if h == "north" else "upper left"
    ax.legend(handles=[Line2D([], [], color="#1a1a1a", lw=2.4, label=f"{en_cours}"),
                       Line2D([], [], color="#1a1a1a", lw=1.0, ls=(0, (5, 3)), label=f"moyenne {REF[0]}-{REF[1]}"),
                       Line2D([], [], color="#1a1a1a", lw=1.0, ls=(0, (1, 2)), label=f"normale {REF2[0]}-{REF2[1]}")],
              loc=loc, frameon=False, fontsize=8, handlelength=2.6, labelcolor="#333333")

    ax.set_xlim(1, 366)
    lo = np.floor(np.nanmin(tab.loc[annees + [en_cours]].values) - 1.0)
    hi = np.ceil(np.nanmax(tab.loc[annees + [en_cours]].values) + 0.5)
    ax.set_ylim(max(0, lo), hi)
    ax.set_xticks([j + 15 for j in DEBUT_MOIS])
    ax.set_xticklabels(MOIS_COURT)
    ax.set_xticks(DEBUT_MOIS, minor=True)
    ax.tick_params(axis="x", which="major", length=0)
    ax.tick_params(axis="x", which="minor", length=4, color="#9a9a96")
    ax.set_yticks(np.arange(max(0, lo), hi + 0.1, 2))
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}")
    ax.set_ylabel("étendue (millions de km²)", fontsize=9, color="#333333")
    ax.grid(axis="y", color="#e3e3df", lw=0.6, zorder=0)
    ax.grid(axis="x", which="minor", color="#efefec", lw=0.6, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#9a9a96")
    ax.tick_params(labelsize=8.5, colors="#333333")

    fig.text(0.02, yf(0.18), POLES[h]["titre"], fontsize=22, fontweight="bold", va="top")
    fig.text(0.02, yf(0.62),
             f"Étendue au jour le jour, une courbe par année, {PREMIERE_ANNEE} → {en_cours}  ·  "
             f"aujourd'hui : {fr(v_der)} M km², {fr(ecart)} par rapport à 1981-2010",
             fontsize=10.5, va="top", color="#333333")
    fig.text(0.98, yf(0.18), "Étendue de la banquise (millions de km²)\ncouleur : année",
             fontsize=9, ha="right", va="top", color="#333333")

    cax = fig.add_axes([0.10, 0.70 / H, 0.80, 0.17 / H])
    bornes = np.arange(PREMIERE_ANNEE, en_cours + 1)
    sm = plt.cm.ScalarMappable(cmap=CMAP_ANNEES, norm=BoundaryNorm(bornes, CMAP_ANNEES.N))
    cb = fig.colorbar(sm, cax=cax, orientation="horizontal", ticks=[y + 0.5 for y in range(1980, en_cours, 5)])
    cb.ax.set_xticklabels([str(y) for y in range(1980, en_cours, 5)])
    cb.minorticks_off()
    cb.ax.tick_params(labelsize=7)
    fig.text(0.5, 0.06 / H,
             "Données : NSIDC Sea Ice Index v4 (NOAA@NSIDC), étendue = surface où au moins 15 % de l'océan est englacé"
             "\nMoyenne et normale lissées sur 7 jours",
             fontsize=7.5, ha="center", color="#555555")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return {"jour": f"{en_cours}-{d.month:02d}-{d.day:02d}", "etendue": round(v_der, 3),
            "ecart_1981_2010": round(ecart, 3), "record_bas": an_rec}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spag", help="dossier contenant spag_north.txt et spag_south.txt (test hors ligne)")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for h, p in POLES.items():
        tab = lire_spag(args.spag, h) if args.spag else lire_quotidien(h)
        print(p["titre"], render(h, tab, OUT_DIR / p["fichier"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
