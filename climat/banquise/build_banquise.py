#!/usr/bin/env python3
"""
Étendue de la banquise arctique et antarctique depuis novembre 1978 : écart à la normale,
dans la charte des cartes J+6 et du graphique de température mondiale.

Donnée : NSIDC Sea Ice Index, version 4 (G02135), étendue quotidienne par hémisphère
(surface où au moins 15 % de l'océan est couvert de glace). Données NOAA, libres d'usage
en citant la source.

Méthode :
  1. climatologie quotidienne (jour de l'année) sur la période de référence, lissée sur 7 jours ;
  2. écart quotidien = étendue du jour - climatologie du même jour ;
  3. moyenne mensuelle de ces écarts (un point par mois sur le graphique).
Travailler en écart quotidien évite les biais d'un mois incomplet (le mois en cours, ou les
débuts de série où les satellites ne donnaient qu'une valeur tous les deux jours).

Référence : 1981-2010 (celle du NSIDC, la plus proche du début des mesures satellitaires).
La normale OMM 1991-2020 est tracée en pointillés, à son écart moyen sur l'année.

Usage :
    python build_banquise.py                    # télécharge les CSV du NSIDC et produit les graphiques
    python build_banquise.py --mensuel DOSSIER  # test hors ligne : mensuel_north.csv / mensuel_south.csv

Sorties (dossier OUT_DIR) :
    banquise_arctique.png, banquise_antarctique.png    les graphiques
    banquise.json                                      les séries mensuelles (lues plus tard par l'app)
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
NSIDC = "https://noaadata.apps.nsidc.org/NOAA/G02135/{h}/daily/data/{H}_seaice_extent_daily_v4.0.csv"
REF = (1981, 2010)            # référence de l'axe (celle du NSIDC)
REF2 = (1991, 2020)           # normale OMM actuelle, tracée en pointillés
LISSAGE_CLIM_J = 7            # lissage de la climatologie quotidienne (jours)
LISSAGE_MOIS = 12             # moyenne glissante tracée en trait plein
JOURS_MIN = 10                # un mois avec moins de jours de mesure n'est pas tracé (sauf le mois en cours)
A_MAX = 3.0                   # échelle de couleurs symétrique : de -A_MAX à +A_MAX (millions de km²)
A_LEVELS = np.round(np.arange(-A_MAX, A_MAX + 0.01, 0.25), 2)
OUT_DIR = pathlib.Path(os.environ.get("OUT_DIR", "sortie"))
TIMEOUT = 60

POLES = {
    "north": {"titre": "Banquise arctique", "fichier": "banquise_arctique.png", "legende": "upper right"},
    "south": {"titre": "Banquise antarctique", "fichier": "banquise_antarctique.png", "legende": "upper left"},
}
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

# Mêmes teintes que la température, mais inversées : moins de glace = rouge, plus de glace = bleu.
CMAP = LinearSegmentedColormap.from_list("anomalie_glace", [
    (0.00, "#7c0f1e"), (0.08, "#b81f2a"), (0.17, "#e0412c"), (0.25, "#f2762e"),
    (0.33, "#f7b733"), (0.40, "#e6e22e"),
    (0.45, "#c4c4bf"), (0.55, "#c4c4bf"),
    (0.60, "#25c7d9"), (0.67, "#1e9be0"), (0.75, "#1f62d6"), (0.83, "#3b2fbf"),
    (1.00, "#2b0b3f")], N=len(A_LEVELS) + 1)
NORM = BoundaryNorm(A_LEVELS, CMAP.N, extend="both")


def fr(x: float, nd: int = 2, signe: bool = True) -> str:
    s = f"{x:+.{nd}f}" if signe else f"{x:.{nd}f}"
    return s.replace(".", ",").replace("-", "−")


# --------------------------------------------------------------------------
# Données
# --------------------------------------------------------------------------
def lire_quotidien(h: str) -> pd.Series:
    """Étendue quotidienne (millions de km²) depuis le CSV v4 du NSIDC."""
    r = requests.get(NSIDC.format(h=h, H=h[0].upper()), timeout=TIMEOUT)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), skiprows=[1], skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    d = pd.to_datetime(dict(year=df["Year"], month=df["Month"], day=df["Day"]))
    s = pd.Series(df["Extent"].astype(float).values, index=d)
    return s[s > 0].sort_index()


def jour_annee(idx: pd.DatetimeIndex) -> np.ndarray:
    """Jour de l'année sur 366 jours (le 1er mars est toujours le jour 61)."""
    doy = idx.dayofyear.values.copy()
    doy[(~idx.is_leap_year) & (idx.month > 2)] += 1
    return doy


def climatologie(s: pd.Series, ref: tuple[int, int]) -> np.ndarray:
    """Climatologie par jour de l'année (indices 1..366), lissée sur LISSAGE_CLIM_J jours (circulaire)."""
    per = s[(s.index.year >= ref[0]) & (s.index.year <= ref[1])]
    brut = pd.Series(per.values).groupby(jour_annee(per.index)).mean().reindex(range(1, 367))
    k = LISSAGE_CLIM_J // 2
    ext = pd.concat([brut.iloc[-k:], brut, brut.iloc[:k]])
    lisse = ext.rolling(LISSAGE_CLIM_J, center=True, min_periods=1).mean().iloc[k:-k]
    return np.concatenate([[np.nan], lisse.values])


def mensuel(s: pd.Series) -> pd.DataFrame:
    """Moyenne mensuelle des écarts quotidiens à REF et à REF2, et nombre de jours mesurés."""
    doy = jour_annee(s.index)
    a1 = s.values - climatologie(s, REF)[doy]
    a2 = s.values - climatologie(s, REF2)[doy]
    df = pd.DataFrame({"a1": a1, "a2": a2}, index=s.index)
    g = df.groupby(df.index.to_period("M"))
    out = g.mean().rename(columns={"a1": "ecart", "a2": "ecart2"})
    out["jours"] = g.size()
    out.index = out.index.to_timestamp()
    return out


def lire_mensuel(dossier: str, h: str) -> pd.DataFrame:
    df = pd.read_csv(pathlib.Path(dossier) / f"mensuel_{h}.csv")
    df.index = pd.to_datetime(df["mois"])
    return df.rename(columns={"ecart_1981_2010": "ecart", "ecart_1991_2020": "ecart2"})[["ecart", "ecart2", "jours"]]


# --------------------------------------------------------------------------
# Rendu (même gabarit que la température)
# --------------------------------------------------------------------------
def render(h: str, m: pd.DataFrame, path: pathlib.Path) -> dict:
    dernier = m.index[-1]
    partiel = m["jours"].iloc[-1] < 28
    m = m[(m["jours"] >= JOURS_MIN) | (m.index == dernier)]
    a = m["ecart"]
    decal = float((m["ecart"] - m["ecart2"]).mean())   # niveau de la normale 1991-2020 sur cet axe
    liss = a.rolling(LISSAGE_MOIS, center=True, min_periods=LISSAGE_MOIS).mean()
    rec = a.idxmin()
    x = a.index.year + (a.index.month - 0.5) / 12

    W, top, bottom, plot_h = 9.6, 1.0, 1.15, 4.6
    H = top + plot_h + bottom
    fig = plt.figure(figsize=(W, H), dpi=110)
    yf = lambda inches_from_top: 1 - inches_from_top / H  # noqa: E731
    ax = fig.add_axes([0.075, (bottom + 0.30) / H, 0.85, (plot_h - 0.30) / H])

    ax.axhline(0, color="#1a1a1a", lw=0.8, zorder=1)
    ax.axhline(decal, color="#6b6b6b", lw=0.8, ls=(0, (1, 2)), zorder=1)
    pts = ax.scatter(x, a.values, c=a.values, cmap=CMAP, norm=NORM, s=6, lw=0, zorder=2)
    ax.plot(x, liss.values, color="#1a1a1a", lw=1.4, zorder=3)

    lo = np.floor((a.min() - 0.3) * 2) / 2
    hi = np.ceil((a.max() + 0.3) * 2) / 2
    ax.set_xlim(1978, dernier.year + 2)
    ax.set_ylim(lo, hi)

    # Légende des lignes de référence, dans le coin vide du graphique
    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([], [], color="#1a1a1a", lw=0.8, label=f"moyenne {REF[0]}-{REF[1]}"),
                       Line2D([], [], color="#6b6b6b", lw=0.8, ls=(0, (1, 2)),
                              label=f"normale {REF2[0]}-{REF2[1]} (moyenne sur l'année)")],
              loc=POLES[h]["legende"], frameon=False, fontsize=8, handlelength=2.2, labelcolor="#333333")

    x_rec = rec.year + (rec.month - 0.5) / 12
    ax.annotate(f"record bas : {MOIS[rec.month - 1]} {rec.year}\n{fr(a[rec])} M km²",
                xy=(x_rec, a[rec]), xytext=(x_rec - 6, a[rec] - 0.05), fontsize=8.5, color="#1a1a1a",
                ha="right", va="center", arrowprops=dict(arrowstyle="-", color="#6b6b6b", lw=0.7))

    ax.set_xticks(range(1980, dernier.year + 1, 5))
    ax.set_yticks(np.arange(np.ceil(lo * 2) / 2, hi + 0.01, 0.5))
    ax.yaxis.set_major_formatter(lambda v, _: fr(v, 1) if abs(v) > 1e-9 else "0")
    ax.set_ylabel(f"écart à {REF[0]}-{REF[1]} (millions de km²)", fontsize=9, color="#333333")
    ax.grid(axis="y", color="#e3e3df", lw=0.6, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#9a9a96")
    ax.tick_params(labelsize=8.5, colors="#333333")

    mois_txt = f"{MOIS[dernier.month - 1]} {dernier.year}" + (f" (1er-{int(m['jours'].iloc[-1])})" if partiel else "")
    fig.text(0.02, yf(0.18), POLES[h]["titre"], fontsize=22, fontweight="bold", va="top")
    fig.text(0.02, yf(0.62),
             f"Étendue mensuelle, novembre 1978 → {mois_txt}  ·  "
             f"dernier mois : {fr(a.iloc[-1])} M km²  ·  moyenne sur 12 mois : {fr(a.iloc[-12:].mean())} M km²",
             fontsize=10.5, va="top", color="#333333")
    fig.text(0.98, yf(0.18), "Écart à la moyenne 1981-2010 (millions de km²)\npoints : mois  ·  trait : moyenne sur 12 mois",
             fontsize=9, ha="right", va="top", color="#333333")

    cax = fig.add_axes([0.10, 0.70 / H, 0.80, 0.17 / H])
    cb = fig.colorbar(pts, cax=cax, orientation="horizontal", ticks=A_LEVELS[::4])
    cb.minorticks_off()
    cb.ax.tick_params(labelsize=7)
    cb.ax.xaxis.set_major_formatter(lambda v, _: fr(v, 0) if abs(v) > 1e-9 else "0")
    fig.text(0.5, 0.06 / H,
             "Données : NSIDC Sea Ice Index v4 (NOAA@NSIDC), étendue = surface où au moins 15 % de l'océan est englacé"
             "\nÉcarts calculés jour par jour à la climatologie 1981-2010, puis moyennés par mois",
             fontsize=7.5, ha="center", color="#555555")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return {"record_bas": {"mois": rec.strftime("%Y-%m"), "ecart": round(float(a[rec]), 3)},
            "dernier": {"mois": dernier.strftime("%Y-%m"), "ecart": round(float(a.iloc[-1]), 3),
                        "jours": int(m["jours"].iloc[-1])},
            "decalage_normale_omm": round(decal, 3),
            "mois": [d.strftime("%Y-%m") for d in a.index], "ecart": [round(float(v), 3) for v in a.values]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mensuel", help="dossier contenant mensuel_north.csv et mensuel_south.csv (test hors ligne)")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = {"source": "NSIDC Sea Ice Index v4 (G02135)", "reference": f"{REF[0]}-{REF[1]}", "unite": "millions de km²"}
    for h, p in POLES.items():
        m = lire_mensuel(args.mensuel, h) if args.mensuel else mensuel(lire_quotidien(h))
        out[h] = render(h, m, OUT_DIR / p["fichier"])
        print(f"{p['titre']} : {len(out[h]['mois'])} mois, record bas {out[h]['record_bas']}, "
              f"dernier {out[h]['dernier']}")
    (OUT_DIR / "banquise.json").write_text(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
