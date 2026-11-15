#!/usr/bin/env python3
"""
Température de surface de la mer par zone (Monde, Niño 1+2, Niño 3.4, Méditerranée, Arctique),
mensuelle depuis 1880, en écart à la normale 1991-2020, dans la charte des cartes J+6.

Donnée : NOAA ERSST v5 (grille de 2°, mensuelle, 1854 → mois dernier), servie par l'ERDDAP
CoastWatch de la NOAA. Pour l'océan mondial : série « ocean » de NOAA Climate at a Glance
(NOAAGlobalTemp, construite sur ERSST v5), en anomalie seulement. Pour chaque zone : moyenne des cases de mer pondérée par le cosinus de la
latitude, puis écart à la moyenne 1991-2020 du même mois (c'est aussi la référence des indices
El Niño de la NOAA). Avant 1880, les mesures sont trop rares pour une moyenne par zone.

Usage :
    python build_sst_zones.py                  # télécharge ERSST v5 et produit les graphiques
    python build_sst_zones.py --zones FICHIER  # test hors ligne (lignes 'zone|climato x12|écarts en centièmes')

Sorties (dossier OUT_DIR) :
    sst_<zone>.png pour chaque zone, sst_zones.json
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
# Deux serveurs ERDDAP de la NOAA servent le même jeu : si le premier répond 503 (surcharge),
# on réessaie, puis on passe au second.
ERDDAP_SERVEURS = ["https://coastwatch.pfeg.noaa.gov/erddap", "https://upwell.pfeg.noaa.gov/erddap"]
ERDDAP = ("{serveur}/griddap/nceiErsstv5_LonPM180.csv0?"
          "sst[({debut}-01-01):1:(last)][(0.0)][({la0}):{pas}:({la1})][({lo0}):{pas}:({lo1})]")
ESSAIS = 3
DEBUT = 1880
REF = (1991, 2020)
LISSAGE_MOIS = 12
OUT_DIR = pathlib.Path(os.environ.get("OUT_DIR", "sortie"))
TIMEOUT = 300

# lat, lon (bornes incluses), pas de grille (1 = 2°, 2 = 4°), filtre de cases, échelle de couleurs
NOAA_OCEAN = ("https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/global/time-series/"
              "globe/tavg/ocean/1/0/1850-{fin}/data.csv")
ZONES = {
    "monde": {"titre": "Océan mondial", "source": "noaa", "a_max": 1.2,
              "zone": "tous les océans, série « ocean »"},
    "med": {"titre": "Méditerranée", "lat": (30, 46), "lon": (-6, 36), "pas": 1, "a_max": 1.8,
            # on retire la mer Noire, l'Atlantique (golfe de Gascogne compris)
            "garde": lambda la, lo: not (la > 40.5 and lo > 26.5) and lo >= -5 and not (lo < 0 and la > 40)
            and not (la >= 44 and lo < 10),
            "zone": "30-46° N, 5° O-36° E (hors mer Noire et Atlantique)"},
    "nino12": {"titre": "Niño 1+2", "lat": (-10, 0), "lon": (-90, -80), "pas": 1, "a_max": 4.5,
               "garde": lambda la, lo: True, "zone": "0-10° S, 90-80° O (côtes du Pérou et de l'Équateur)",
               "seuils": False},
    "nino34": {"titre": "Niño 3.4", "lat": (-5, 5), "lon": (-170, -120), "pas": 1, "a_max": 3.0,
               "garde": lambda la, lo: True, "zone": "5° N-5° S, 170-120° O (Pacifique équatorial central)",
               "seuils": True},
    "arctic": {"titre": "Océan Arctique", "lat": (66, 88), "lon": (-180, 178), "pas": 2, "a_max": 1.5,
               "garde": lambda la, lo: True, "zone": "au nord de 66° N (eau libre ou sous la glace)"},
}
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

# Même palette divergente que la température mondiale (gris neutre à 0)
CMAP_BASE = [(0.00, "#2b0b3f"), (0.17, "#3b2fbf"), (0.33, "#1f62d6"), (0.40, "#25c7d9"),
             (0.45, "#c4c4bf"), (0.55, "#c4c4bf"),
             (0.60, "#e6e22e"), (0.67, "#f7b733"), (0.75, "#f2762e"), (0.83, "#e0412c"),
             (0.92, "#b81f2a"), (1.00, "#7c0f1e")]


def fr(x: float, nd: int = 2, signe: bool = True) -> str:
    s = f"{x:+.{nd}f}" if signe else f"{x:.{nd}f}"
    return s.replace(".", ",").replace("-", "−")


# --------------------------------------------------------------------------
# Données
# --------------------------------------------------------------------------
def telecharger_noaa() -> tuple[pd.Series, np.ndarray]:
    """Océan mondial : anomalies NOAA recalées sur REF (pas de valeur absolue publiée)."""
    import datetime as dt
    r = requests.get(NOAA_OCEAN.format(fin=dt.date.today().year), timeout=TIMEOUT)
    r.raise_for_status()
    lignes = [l for l in r.text.splitlines() if l[:6].isdigit()]
    df = pd.read_csv(io.StringIO("\n".join(lignes)), header=None, names=["d", "v"], dtype={"d": str})
    s = pd.Series(df["v"].values, index=pd.to_datetime(df["d"], format="%Y%m"))
    s = s[s.index.year >= DEBUT]
    per = s[(s.index.year >= REF[0]) & (s.index.year <= REF[1])]
    clim = per.groupby(per.index.month).mean()
    return s - clim.reindex(s.index.month).values, np.full(12, np.nan)


def telecharger(z: dict) -> tuple[pd.Series, np.ndarray]:
    """Écarts mensuels à REF et climatologie (12 valeurs, °C)."""
    if z.get("source") == "noaa":
        return telecharger_noaa()
    import time

    r, derniere_erreur = None, None
    for serveur in ERDDAP_SERVEURS:
        url = ERDDAP.format(serveur=serveur, debut=DEBUT, la0=z["lat"][0], la1=z["lat"][1],
                            lo0=z["lon"][0], lo1=z["lon"][1], pas=z["pas"])
        for essai in range(ESSAIS):
            try:
                r = requests.get(url, timeout=TIMEOUT)
                r.raise_for_status()
                break
            except requests.RequestException as exc:
                derniere_erreur, r = exc, None
                print(f"  {serveur} : essai {essai + 1}/{ESSAIS} échoué ({exc.__class__.__name__})")
                time.sleep(20 * (essai + 1))
        if r is not None:
            break
    if r is None:
        raise derniere_erreur
    df = pd.read_csv(io.StringIO(r.text), header=None, names=["t", "p", "la", "lo", "sst"]).dropna()
    df = df[[z["garde"](la, lo) for la, lo in zip(df["la"], df["lo"])]]
    df["w"] = np.cos(np.radians(df["la"]))
    df["mois"] = pd.to_datetime(df["t"].str[:7])
    g = df.groupby("mois")
    s = (g.apply(lambda x: (x["sst"] * x["w"]).sum() / x["w"].sum()))
    per = s[(s.index.year >= REF[0]) & (s.index.year <= REF[1])]
    clim = per.groupby(per.index.month).mean()
    return s - clim.reindex(s.index.month).values, clim.values


def lire_zones(fichier: str) -> dict[str, tuple[pd.Series, np.ndarray]]:
    """Format de test : une ligne par zone, 'zone|12 climato|écarts en centièmes de °C'
    pour chaque mois disponible depuis janvier 1880 (octobre 1902 manque dans l'ERDDAP)."""
    out = {}
    for ligne in pathlib.Path(fichier).read_text().splitlines():
        k, c, a = ligne.split("|")
        vals = [int(x) / 100 for x in a.split(",")]
        trou = ZONES[k].get("source") != "noaa"          # le trou d'octobre 1902 ne concerne que l'ERDDAP
        idx = [d for d in pd.date_range(f"{DEBUT}-01-01", periods=len(vals) + 1, freq="MS")
               if not (trou and d == pd.Timestamp("1902-10-01"))][:len(vals)]
        clim = np.array([float(x) for x in c.split(",")]) if c else np.full(12, np.nan)
        out[k] = (pd.Series(vals, index=pd.DatetimeIndex(idx)), clim)
    return out


# --------------------------------------------------------------------------
# Rendu (même gabarit que la température mondiale)
# --------------------------------------------------------------------------
def render(k: str, a: pd.Series, clim: np.ndarray, path: pathlib.Path) -> dict:
    z = ZONES[k]
    levels = np.round(np.linspace(-z["a_max"], z["a_max"], 19), 3)
    cmap = LinearSegmentedColormap.from_list("anomalie", CMAP_BASE, N=len(levels) + 1)
    norm = BoundaryNorm(levels, cmap.N, extend="both")
    liss = a.rolling(LISSAGE_MOIS, center=True, min_periods=LISSAGE_MOIS).mean()
    dernier, rec = a.index[-1], a.idxmax()
    x = a.index.year + (a.index.month - 0.5) / 12
    abs_der = clim[dernier.month - 1] + a.iloc[-1]

    W, top, bottom, plot_h = 9.6, 1.0, 1.15, 4.6
    H = top + plot_h + bottom
    fig = plt.figure(figsize=(W, H), dpi=110)
    yf = lambda inches_from_top: 1 - inches_from_top / H  # noqa: E731
    ax = fig.add_axes([0.075, (bottom + 0.30) / H, 0.85, (plot_h - 0.30) / H])

    ax.axhline(0, color="#1a1a1a", lw=0.8, zorder=1)
    if z.get("seuils"):                              # seuils El Niño / La Niña de la NOAA
        for s_, lab in ((0.5, "El Niño (+0,5 °C)"), (-0.5, "La Niña (−0,5 °C)")):
            ax.axhline(s_, color="#6b6b6b", lw=0.8, ls=(0, (1, 2)), zorder=1)
            ax.text(1881, s_ + (0.06 if s_ > 0 else -0.06), lab, fontsize=8, color="#6b6b6b",
                    va="bottom" if s_ > 0 else "top", zorder=4,
                    bbox=dict(fc="white", ec="none", pad=0.5, alpha=0.85))
    pts = ax.scatter(x, a.values, c=a.values, cmap=cmap, norm=norm, s=5, lw=0, zorder=2)
    ax.plot(x, liss.values, color="#1a1a1a", lw=1.3, zorder=3)
    x_rec = rec.year + (rec.month - 0.5) / 12
    ax.annotate(f"record : {MOIS[rec.month - 1]} {rec.year}\n{fr(a[rec])} °C",
                xy=(x_rec, a[rec]), xytext=(x_rec - 30, a[rec]), fontsize=8.5, color="#1a1a1a",
                ha="right", va="center", arrowprops=dict(arrowstyle="-", color="#6b6b6b", lw=0.7), zorder=5)

    lim = max(abs(a.min()), abs(a.max())) + 0.25
    ax.set_xlim(DEBUT - 1, dernier.year + 2)
    ax.set_ylim(-lim, lim)
    pas_y = 0.5 if lim < 2.5 else 1.0
    ax.set_yticks(np.arange(-np.floor(lim / pas_y) * pas_y, lim + 0.01, pas_y))
    ax.set_xticks(range(DEBUT, dernier.year + 1, 20))
    ax.yaxis.set_major_formatter(lambda v, _: fr(v, 1) if abs(v) > 1e-9 else "0")
    ax.set_ylabel(f"écart à {REF[0]}-{REF[1]} (°C)", fontsize=9, color="#333333")
    ax.grid(axis="y", color="#e3e3df", lw=0.6, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#9a9a96")
    ax.tick_params(labelsize=8.5, colors="#333333")

    fig.text(0.02, yf(0.18), z["titre"], fontsize=22, fontweight="bold", va="top")
    fig.text(0.02, yf(0.62),
             f"Température de surface, janvier {DEBUT} → {MOIS[dernier.month - 1]} {dernier.year}  ·  "
             f"{MOIS[dernier.month - 1]} {dernier.year} : "
             + (f"{fr(abs_der, 1, False)} °C ({fr(a.iloc[-1])})" if np.isfinite(abs_der) else f"{fr(a.iloc[-1])} °C")
             + "  ·  "
             f"moyenne sur 12 mois : {fr(a.iloc[-12:].mean())} °C",
             fontsize=10.5, va="top", color="#333333")
    fig.text(0.98, yf(0.18), f"Écart à la normale {REF[0]}-{REF[1]} (°C)\npoints : mois  ·  trait : moyenne sur 12 mois",
             fontsize=9, ha="right", va="top", color="#333333")

    cax = fig.add_axes([0.10, 0.70 / H, 0.80, 0.17 / H])
    cb = fig.colorbar(pts, cax=cax, orientation="horizontal", ticks=levels[::3])
    cb.minorticks_off()
    cb.ax.tick_params(labelsize=7)
    cb.ax.xaxis.set_major_formatter(lambda v, _: fr(v, 1) if abs(v) > 1e-9 else "0")
    fig.text(0.5, 0.06 / H,
             (f"Données : NOAA ERSST v5 (grille 2°) via ERDDAP CoastWatch · zone : {z['zone']}"
              "\nMoyenne des cases de mer pondérée par la latitude, écart à la moyenne 1991-2020 du même mois"
              if z.get("source") != "noaa" else
              f"Données : NOAA NCEI, Climate at a Glance · {z['zone']}"
              "\nAnomalie recalée sur la moyenne 1991-2020 du même mois"),
             fontsize=7.5, ha="center", color="#555555")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return {"titre": z["titre"], "zone": z["zone"],
            "dernier": {"mois": dernier.strftime("%Y-%m"), "ecart": round(float(a.iloc[-1]), 2),
                        "sst": round(float(abs_der), 2) if np.isfinite(abs_der) else None},
            "record": {"mois": rec.strftime("%Y-%m"), "ecart": round(float(a[rec]), 2)},
            "mois": [d.strftime("%Y-%m") for d in a.index], "ecart": [round(float(v), 2) for v in a.values]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zones", help="fichier d'écarts déjà calculés (test hors ligne)")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.zones:
        donnees = lire_zones(args.zones)
    else:
        donnees = {}
        for k, z in ZONES.items():            # une zone en échec n'empêche pas les autres
            try:
                donnees[k] = telecharger(z)
            except Exception as exc:
                print(f"{z['titre']} : échec ({exc}) — l'ancienne image reste en ligne")
    if not donnees:
        return 1
    out = {"source": "NOAA ERSST v5", "reference": f"{REF[0]}-{REF[1]}"}
    for k, (a, clim) in donnees.items():
        out[k] = render(k, a, clim, OUT_DIR / f"sst_{k}.png")
        print(ZONES[k]["titre"], out[k]["dernier"], "record", out[k]["record"])
    (OUT_DIR / "sst_zones.json").write_text(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
