#!/usr/bin/env python3
"""
Cartes de PRÉCIPITATIONS sur 6 h et de leur TYPE, centrées sur la France, de +6 h à +144 h,
pour les mêmes quatre modèles que les cartes Z500 (même disposition de grille) :

  - GFS    : cumul total et convectif, types catégoriels (pluie, neige, pluie verglaçante,
             grésil), CAPE, altitude de l'isotherme 0 °C
  - IFS    : cumul total et convectif, type de précipitation (ptype), CAPE (mucape)
  - AIGFS  : cumul total (et le reste si l'inventaire de la NOAA le fournit)
  - AIFS   : cumul total, convectif et chute de neige

Types affichés : pluie, orage / averses (part convective dominante + instabilité),
neige, verglas / grésil. La GRÊLE n'est prévue par aucun de ces modèles : on affiche seulement
un INDICE DE RISQUE (hachures), déduit de l'instabilité, de la part convective et du cumul.
Mettre SHOW_HAIL = False pour le retirer.

Usage :  python precip.py   |   python precip.py --demo
Sorties : OUT_DIR/precip/<modele>_<HHH>.png et OUT_DIR/precip/manifest.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import multiprocessing as mp
import os
import pathlib
import sys
import tempfile
import time
import traceback

import numpy as np
import xarray as xr

import build_maps as bm  # outils communs (HTTP, GRIB, modèles, fonds de carte…)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import BoundaryNorm, ListedColormap  # noqa: E402

# --------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------
SHOW_HAIL = True                          # indice de risque de grêle (hachures)
OFFSETS = [o for o in bm.OFFSETS if o > 0]  # +6 h … +144 h (pas de cumul à +0 h)
BOX = (-12.0, 16.0, 37.0, 56.0)           # données conservées autour de la France
FRAME_POINTS = [(-5.2, 48.4),   # pointe de Bretagne
                (2.5, 51.1),    # Dunkerque
                (8.3, 49.0),    # Alsace
                (7.6, 43.7),    # Nice
                (9.6, 41.9),    # Corse
                (-1.8, 43.3)]   # Pays basque
FRAME_PAD = 0.10
BINS = [0.2, 1, 2, 5, 10, 20, 50]         # mm en 6 h
CLASSES = {  # code: (libellé, 6 teintes du plus faible au plus fort)
    1: ("Pluie", ["#bfe4ff", "#80c6f6", "#429ee8", "#1f72d0", "#184ca6", "#11306f"]),
    2: ("Orage / averses", ["#ffd6d0", "#ff9d8f", "#ff5c4d", "#e3202f", "#b10f3c", "#74063a"]),
    3: ("Neige", ["#f0e8ff", "#d8c4ff", "#b996ff", "#9567ef", "#6f40cf", "#4a2597"]),
    4: ("Verglas / grésil", ["#ffe3f2", "#ffb6dc", "#ff82c3", "#ef4ea6", "#c62b86", "#8c1562"]),
}
OUT = bm.OUT_DIR / "precip"


def log(msg: str) -> None:
    bm.log(msg)


# --------------------------------------------------------------------------
# Lecture des champs
# --------------------------------------------------------------------------
def norm(da: xr.DataArray) -> xr.DataArray:
    da = da.squeeze(drop=True)
    lat = next(n for n in da.dims if "lat" in n.lower())
    lon = next(n for n in da.dims if "lon" in n.lower())
    da = da.rename({lat: "lat", lon: "lon"})
    da = da.assign_coords(lon=(((da.lon + 180) % 360) - 180)).sortby("lon").sortby("lat")
    lo0, lo1, la0, la1 = BOX
    return da.sel(lon=slice(lo0, lo1), lat=slice(la0, la1))


def first_var(path: str) -> xr.DataArray:
    ds = bm.open_grib(path)
    return ds[next(iter(ds.data_vars))]


def to_mm(da: xr.DataArray) -> xr.DataArray:
    units = str(da.attrs.get("GRIB_units", da.attrs.get("units", ""))).strip()
    return da * 1000.0 if units == "m" else da


# --------------------------------------------------------------------------
# NOAA (GFS sur AWS, AIGFS sur NOMADS) : lecture partielle via les fichiers .idx
# --------------------------------------------------------------------------
def acc_total(s):   # cumul depuis le début du run
    return lambda d: d == f"0-{s} hour acc fcst" or (s % 24 == 0 and d == f"0-{s // 24} day acc fcst")


def acc_6h(s):      # cumul des 6 dernières heures
    return lambda d: d == f"{s - 6}-{s} hour acc fcst"


def specs(s: int) -> dict:
    return {
        "tp_tot": [("APCP", "surface", acc_total(s))], "tp_6h": [("APCP", "surface", acc_6h(s))],
        "cp_tot": [("ACPCP", "surface", acc_total(s))], "cp_6h": [("ACPCP", "surface", acc_6h(s))],
        "csnow": [("CSNOW", "surface", None)], "cfrzr": [("CFRZR", "surface", None)],
        "cicep": [("CICEP", "surface", None)], "cape": [("CAPE", "surface", None)],
        "frz": [("HGT", "0C isotherm", None)],
    }


def idx_fetch(urls: list[str], wanted: dict, tmp: str, tag: str) -> dict:
    out = {}
    for url in urls:
        try:
            r = bm.HTTP.get(url + ".idx", timeout=bm.TIMEOUT)
            r.raise_for_status()
        except Exception:
            continue
        lines = [ln.split(":") for ln in r.text.strip().splitlines()]
        for key, alts in wanted.items():
            if key in out:
                continue
            for name, level, pred in alts:
                for i, p in enumerate(lines):
                    if len(p) > 5 and p[3] == name and p[4] == level and (pred is None or pred(p[5])):
                        start = int(p[1])
                        end = int(lines[i + 1][1]) - 1 if i + 1 < len(lines) else ""
                        g = bm.HTTP.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=bm.TIMEOUT)
                        g.raise_for_status()
                        path = os.path.join(tmp, f"{tag}_{key}.grib2")
                        pathlib.Path(path).write_bytes(g.content)
                        out[key] = norm(first_var(path))
                        break
                if key in out:
                    break
    return out


def noaa_step(model: str, run: dt.datetime, s: int, tmp: str) -> dict:
    if model == "gfs":
        urls = [f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{run:%Y%m%d}/{run:%H}/atmos/"
                f"gfs.t{run:%H}z.pgrb2.0p25.f{s:03d}"]
    else:
        base = f"{bm.NOMADS}/aigfs/prod/aigfs.{run:%Y%m%d}/{run:%H}/model/atmos/grib2/aigfs.t{run:%H}z"
        urls = [f"{base}.sfc.f{s:03d}.grib2", f"{base}.pres.f{s:03d}.grib2"]
    f = idx_fetch(urls, specs(s), tmp, f"{model}_{s:03d}")
    if not any(k in f for k in ("tp_tot", "tp_6h")):
        raise RuntimeError("cumul de précipitations absent")
    return f


def fetch_noaa(model: str, run: dt.datetime, steps: list[int], tmp: str) -> dict:
    out = {}
    with cf.ThreadPoolExecutor(max_workers=8 if model == "gfs" else 4) as pool:
        futs = {pool.submit(noaa_step, model, run, s, tmp): s for s in steps if s > 0}
        for fut in cf.as_completed(futs):
            try:
                out[futs[fut]] = fut.result()
            except Exception as exc:
                log(f"    {model.upper()} +{futs[fut]} h : {exc}")
    return out


# --------------------------------------------------------------------------
# ECMWF IFS / AIFS (ecmwf-opendata) : un fichier par paramètre, toutes échéances
# --------------------------------------------------------------------------
def fetch_ecmwf(model: str, run: dt.datetime, steps: list[int], tmp: str) -> dict:
    from ecmwf.opendata import Client

    client = Client(source="aws", model=model)
    params = ["tp", "cp", "ptype", "mucape"] if model == "ifs" else ["tp", "cp", "sf"]
    common = dict(date=run.strftime("%Y%m%d"), time=run.hour, type="fc", stream="oper", levtype="sfc")
    names = {"tp": "tp_tot", "cp": "cp_tot", "sf": "sf_tot", "ptype": "ptype", "mucape": "cape"}
    out = {s: {} for s in steps}

    def grab(param: str, step_list: list[int]) -> None:
        f = os.path.join(tmp, f"{model}_{param}_{step_list[0]:03d}_{len(step_list)}.grib2")
        client.retrieve(**common, step=step_list, param=param, target=f)
        da = first_var(f)
        if param in ("tp", "cp"):
            da = to_mm(da)
        for s, d in bm.by_step(da).items():
            if s in out:
                out[s][names[param]] = norm(d)

    for param in params:
        try:
            grab(param, steps)
        except Exception as exc:
            log(f"    {model} {param} : requête groupée impossible ({exc}), échéance par échéance")
            for s in steps:
                try:
                    grab(param, [s])
                except Exception:
                    pass
    return {s: v for s, v in out.items() if "tp_tot" in v or s == 0}


FETCHERS = {
    "gfs": lambda r, s, t: fetch_noaa("gfs", r, s, t),
    "ifs": lambda r, s, t: fetch_ecmwf("ifs", r, s, t),
    "aigfs": lambda r, s, t: fetch_noaa("aigfs", r, s, t),
    "aifs": lambda r, s, t: fetch_ecmwf("aifs-single", r, s, t),
}


# --------------------------------------------------------------------------
# Cumul sur 6 h et type de précipitation
# --------------------------------------------------------------------------
def six_hours(cur: dict, prev: dict | None, key: str):
    """Cumul sur 6 h : champ 6 h direct s'il existe, sinon différence de deux cumuls totaux."""
    if f"{key}_6h" in cur:
        return cur[f"{key}_6h"].values.astype("float32")
    if f"{key}_tot" not in cur:
        return None
    tot = cur[f"{key}_tot"].values.astype("float32")
    if prev is None:                       # début du run : cumul nul avant
        return tot
    if f"{key}_tot" not in prev:
        return None
    return tot - prev[f"{key}_tot"].values.astype("float32")


def classify(cur: dict, prev: dict | None) -> dict | None:
    tp = six_hours(cur, prev, "tp")
    if tp is None:
        return None
    tp = np.clip(tp, 0, None)
    cp = six_hours(cur, prev, "cp")
    sf = six_hours(cur, prev, "sf")
    ref = cur.get("tp_tot", cur.get("tp_6h"))
    lat, lon = ref.lat.values, ref.lon.values
    val = lambda k: cur[k].values.astype("float32") if k in cur else None  # noqa: E731

    cls = np.ones(tp.shape, dtype="int8")
    has_type = False
    snow = np.zeros(tp.shape, bool)
    frz = np.zeros(tp.shape, bool)
    if "csnow" in cur or "cfrzr" in cur:                       # GFS : types catégoriels
        has_type = True
        snow = (val("csnow") if "csnow" in cur else 0) > 0.5
        frz = ((val("cfrzr") if "cfrzr" in cur else 0) > 0.5) | ((val("cicep") if "cicep" in cur else 0) > 0.5)
    elif "ptype" in cur:                                       # IFS : code de type
        has_type = True
        pt = np.rint(val("ptype"))
        snow = np.isin(pt, [5, 6])
        frz = np.isin(pt, [3, 4, 8, 12])
    elif sf is not None:                                       # AIFS : part de neige
        has_type = True
        snow = np.clip(sf, 0, None) >= 0.5 * np.maximum(tp, 1e-6)

    cape = val("cape")
    conv = None
    if cp is not None:
        has_type = True
        conv = np.clip(cp, 0, None) / np.maximum(tp, 1e-6)
        storm = (conv >= 0.6) & ((cape >= 400) if cape is not None else (tp >= 3))
        cls[storm] = 2
    cls[frz] = 4
    cls[snow] = 3

    hail = None
    if SHOW_HAIL and cape is not None and conv is not None:
        hail = (cape >= 1500) & (conv >= 0.6) & (tp >= 5) & ~snow
        if "frz" in cur:
            hail &= val("frz") <= 3800
    return {"lat": lat, "lon": lon, "tp": tp, "cls": cls, "has_type": has_type, "hail": hail}


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------
_FRAME = {}


def frame():
    if not _FRAME:
        import cartopy.crs as ccrs

        proj = ccrs.LambertConformal(central_longitude=2.5, central_latitude=46.5, standard_parallels=(44, 49))
        pts = proj.transform_points(ccrs.PlateCarree(), *np.array(FRAME_POINTS, dtype=float).T)
        x0, x1, y0, y1 = pts[:, 0].min(), pts[:, 0].max(), pts[:, 1].min(), pts[:, 1].max()
        dx, dy = (x1 - x0) * FRAME_PAD, (y1 - y0) * FRAME_PAD
        _FRAME.update(proj=proj, extent=[x0 - dx, x1 + dx, y0 - dy, y1 + dy])
    return _FRAME["proj"], _FRAME["extent"]


def render(job: tuple) -> str:
    key, P, run, step, path = job
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from PIL import Image

    pc = ccrs.PlateCarree()
    proj, (x0, x1, y0, y1) = frame()
    W = 8.4
    map_w = W * 0.96
    map_h = map_w * (y1 - y0) / (x1 - x0)
    top, bottom = 1.0, 1.15
    H = map_h + top + bottom
    fig = plt.figure(figsize=(W, H), dpi=110)
    ax = fig.add_axes([0.02, bottom / H, 0.96, map_h / H], projection=proj)
    ax.set_extent([x0, x1, y0, y1], crs=proj)
    ax.set_facecolor("#e6edf2")                      # mer
    yf = lambda inches: 1 - inches / H  # noqa: E731
    try:
        land = cfeature.LAND.with_scale("50m")
        next(iter(land.geometries()))
        ax.add_feature(land, facecolor="#f6f4ee", edgecolor="none", zorder=0)
    except Exception:
        pass

    # Affichage lissé : grille affinée 4 fois (interpolation bilinéaire du cumul, type au plus proche)
    from scipy.ndimage import zoom
    f = 4
    lat = np.linspace(P["lat"][0], P["lat"][-1], len(P["lat"]) * f)
    lon = np.linspace(P["lon"][0], P["lon"][-1], len(P["lon"]) * f)
    tp = zoom(P["tp"], f, order=1)
    classes = CLASSES if P["has_type"] else {1: ("Précipitations (type non fourni)", CLASSES[1][1])}
    codes = list(classes)                    # frontières de types adoucies (vote interpolé)
    cls = np.array(codes)[np.stack([zoom((P["cls"] == c).astype("float32"), f, order=1) for c in codes]).argmax(0)]
    for code, (_, colors) in classes.items():
        mask = (tp < BINS[0]) | ((cls != code) if P["has_type"] else False)
        data = np.ma.masked_where(mask, np.minimum(tp, BINS[-1] - 0.01))
        if data.count():
            ax.contourf(lon, lat, data, levels=BINS, colors=colors, transform=pc, zorder=1)
    if P["hail"] is not None and P["hail"].any():
        hail = zoom(P["hail"].astype(float), f, order=1)
        ax.contourf(lon, lat, hail, levels=[0.5, 1.5], colors="none", hatches=["xxxx"], transform=pc, zorder=2)
    try:
        coast = cfeature.COASTLINE.with_scale("50m")
        next(iter(coast.geometries()))
        ax.add_feature(coast, linewidth=0.6, edgecolor="#2a2a2a", zorder=3)
        ax.add_feature(cfeature.BORDERS.with_scale("50m"), linewidth=0.4, edgecolor="#444", zorder=3)
    except Exception:
        pass

    valid = run + dt.timedelta(hours=step)
    start = valid - dt.timedelta(hours=6)
    fig.text(0.02, yf(0.18), bm.MODELS[key]["titre"], fontsize=20, fontweight="bold", va="top")
    fig.text(0.02, yf(0.60), f"Run {run:%d/%m %H}h UTC  ·  +{step} h  ·  du {bm.date_fr(start).rsplit(' UTC', 1)[0]}"
             f" à {valid:%H}h UTC", fontsize=9.5, va="top", color="#333")
    fig.text(0.98, yf(0.18), "Précipitations sur 6 h (mm)\net type", fontsize=9, ha="right", va="top", color="#333")

    # Légende : une petite barre par type
    n = len(classes)
    for i, (code, (label, colors)) in enumerate(classes.items()):
        x = 0.04 + i * (0.92 / max(n, 1))
        w = 0.92 / max(n, 1) - 0.03
        cax = fig.add_axes([x, 0.62 / H, w, 0.13 / H])
        sm = plt.cm.ScalarMappable(cmap=ListedColormap(colors), norm=BoundaryNorm(BINS, len(colors)))
        cb = fig.colorbar(sm, cax=cax, orientation="horizontal", ticks=BINS[:-1] if n > 2 else BINS)
        cb.ax.tick_params(labelsize=6, length=2)
        cb.ax.set_xticklabels([str(b).rstrip("0").rstrip(".") if isinstance(b, float) else str(b)
                               for b in (BINS[:-1] if n > 2 else BINS)])
        cb.outline.set_linewidth(0.4)
        fig.text(x, 0.80 / H, label, fontsize=8, va="bottom", color="#222")
    note = "Hachures : indice de risque de grêle (estimation, pas une prévision)" \
        if (SHOW_HAIL and P["hail"] is not None) else ""
    fig.text(0.5, 0.30 / H, note, fontsize=7.5, ha="center", color="#444")
    fig.text(0.5, 0.10 / H, f"Données : {bm.MODELS[key]['source']}  ·  carte reconstruite à partir des données brutes",
             fontsize=7, ha="center", color="#555")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    Image.open(path).convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(path, optimize=True)
    return str(path)


# --------------------------------------------------------------------------
# Champs simulés (mode --demo)
# --------------------------------------------------------------------------
def demo(seed: int, steps: list[int], shift: int) -> dict:
    rng = np.random.default_rng(seed + 10)
    lat = np.arange(BOX[2], BOX[3] + 0.01, 0.25)
    lon = np.arange(BOX[0], BOX[1] + 0.01, 0.25)
    LON, LAT = np.meshgrid(lon, lat)
    cells = [(rng.uniform(-8, 10), rng.uniform(41, 52), rng.uniform(4, 25), rng.uniform(0.3, 0.6)) for _ in range(5)]
    out = {}
    tot = np.zeros_like(LON)
    ctot = np.zeros_like(LON)
    for s in steps:
        h = s - shift
        rate = np.zeros_like(LON)
        conv = np.zeros_like(LON)
        for k, (cx, cy, amp, sp) in enumerate(cells):
            blob = amp * np.exp(-(((LON - cx - sp * h * 0.2) / 2.2) ** 2 + ((LAT - cy) / 1.6) ** 2))
            rate += blob
            if k % 2 == 0:
                conv += blob
        if s > shift:
            tot = tot + rate
            ctot = ctot + conv
        da = lambda a: xr.DataArray(a.astype("float32"), coords={"lat": lat, "lon": lon}, dims=("lat", "lon"))  # noqa: E731
        out[s] = {"tp_tot": da(tot), "cp_tot": da(ctot), "csnow": da((LAT > 49) & (rate > 1) & (seed == 0)),
                  "cape": da(np.where(conv > 2, 2000.0, 100.0))}
    return out


# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    if ("physical", "ne_50m_land") not in bm.NATURAL_EARTH:   # fond des terres pour ces cartes
        bm.NATURAL_EARTH.append(("physical", "ne_50m_land"))
    bm.ensure_basemap()
    base = bm.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = [(base, 0), (base - dt.timedelta(hours=12), 12)]
    manifest = {"genere_le": bm.utcnow().isoformat(timespec="minutes") + "Z", "base": base.isoformat() + "Z",
                "pas_h": bm.PAS_H, "offsets": OFFSETS, "demo": args.demo, "grele": SHOW_HAIL, "modeles": {}}

    with tempfile.TemporaryDirectory() as tmp, mp.get_context("fork").Pool(max(1, os.cpu_count() or 2)) as pool:
        for i, key in enumerate(bm.MODELS):
            log(f"Précipitations {bm.MODELS[key]['titre']} …")
            entry = {"titre": bm.MODELS[key]["titre"], "source": bm.MODELS[key]["source"],
                     "statut": "indisponible", "images": {str(o): None for o in OFFSETS}}
            for run, shift in candidates:
                steps = [shift] + [o + shift for o in OFFSETS]
                try:
                    td = time.time()
                    raw = demo(i, steps, shift) if args.demo else FETCHERS[key](run, steps, tmp)
                    jobs, types = [], set()
                    for o in OFFSETS:
                        s = o + shift
                        if s not in raw:
                            continue
                        # cumul précédent ; à +0 h du run le cumul est nul
                        prev = None if s - 6 == 0 else raw.get(s - 6)
                        if prev is None and s - 6 != 0:
                            continue
                        P = classify(raw[s], prev)
                        if P is None:
                            continue
                        types.add(P["has_type"])
                        jobs.append((key, P, run, s, OUT / f"{key}_{o:03d}.png"))
                    if shift == 0 and not any(j[3] == bm.ECHEANCE_MAX_H for j in jobs):
                        raise RuntimeError(f"{len(jobs)}/{len(OFFSETS)} échéances, J+6 manquante")
                    if not jobs:
                        raise RuntimeError("aucune échéance exploitable")
                    log(f"  données : {len(jobs)}/{len(OFFSETS)} échéances en {time.time() - td:.0f} s"
                        f"{'' if True in types else ' (type de précipitation non fourni)'}")
                    tr = time.time()
                    for p in pool.imap_unordered(render, jobs):
                        name = pathlib.Path(p).name
                        entry["images"][str(int(name[-7:-4]))] = name
                    log(f"  rendu : {len(jobs)} cartes en {time.time() - tr:.0f} s")
                    entry.update(statut="ok", run=run.isoformat() + "Z", decalage_h=shift, type_fourni=True in types)
                    break
                except Exception as exc:
                    log(f"  échec run {run:%d/%m %H}h : {exc}")
                    if os.environ.get("DEBUG"):
                        traceback.print_exc()
            manifest["modeles"][key] = entry

    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    n = sum(v is not None for m in manifest["modeles"].values() for v in m["images"].values())
    log(f"Précipitations terminées : {n} cartes en {time.time() - t0:.0f} s")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
