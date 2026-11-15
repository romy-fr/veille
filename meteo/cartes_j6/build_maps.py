#!/usr/bin/env python3
"""
Reconstruit des cartes "Géopotentiel 500 hPa + pression au niveau de la mer" de J+0 à J+6
par pas de 6 h, pour quatre modèles, à partir de DONNÉES OUVERTES :

  - GFS   (NOAA)      : domaine public        -> bucket AWS noaa-gfs-bdp-pds
  - IFS   (ECMWF)     : licence CC BY 4.0     -> ECMWF Open Data (miroir AWS)
  - AIFS  (ECMWF)     : licence CC BY 4.0     -> ECMWF Open Data (miroir AWS)
  - UKMO  (Met Office): licence CC BY-SA 4.0  -> bucket AWS met-office-atmospheric-model-data

Les échéances sont alignées sur le run de 00 UTC du jour : la jauge va de +0 h à +144 h
et, pour une même position, les quatre cartes ont la même date de validité.
Si le run de 00 UTC d'un modèle n'est pas encore publié, le script prend le run de 12 UTC
de la veille avec un décalage de 12 h (même dates de validité).

Usage :
    python build_maps.py            # télécharge les données et produit les cartes
    python build_maps.py --demo     # rendu hors ligne avec des champs simulés (test)

Sorties (dossier OUT_DIR) :
    <modele>_<HHH>.png   une carte par modèle et par échéance (HHH = heures depuis 00 UTC du jour)
    <modele>.png         copie de la carte à +144 h (J+6)
    grille.png           les 4 modèles à +144 h (pour la notification)
    manifest.json        description de tout ce qui a été produit (lu par la page et par l'app)
    index.html           page de visualisation avec la jauge J+0 -> J+6
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import multiprocessing as mp
import os
import pathlib
import shutil
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET

import numpy as np
import requests
import xarray as xr

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap  # noqa: E402

# --------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------
LEVEL_HPA = 500                          # niveau du géopotentiel (hPa)
PAS_H = 6                                # pas de la jauge (h) : commun aux 4 modèles
ECHEANCE_MAX_H = 144                     # dernière échéance (J+6)
OFFSETS = list(range(0, ECHEANCE_MAX_H + 1, PAS_H))  # heures depuis 00 UTC du jour

# Points (lon, lat) qui doivent tenir dans le cadre : le rectangle de la carte est calculé autour.
FRAME_POINTS = [(-57, 47.5),   # Terre-Neuve
                (-44, 60),     # sud du Groenland
                (-20, 67),     # Islande
                (27, 71.5),    # cap Nord
                (42, 43),      # Caucase / mer Noire
                (34, 30),      # Proche-Orient
                (-17, 28)]     # Canaries
FRAME_PAD = 0.04                         # marge autour de ces points (fraction du cadre)
DATA_BOX = (-115.0, 95.0, 10.0, 90.0)    # zone de données conservée (lon0, lon1, lat0, lat1)
GRID_MIN_DEG = 0.25                      # les grilles plus fines sont moyennées à ~0,25° (affichage)
Z_LEVELS = np.arange(492, 613, 4)        # couleurs du géopotentiel : une teinte tous les 4 dam
Z_TICKS = np.arange(492, 613, 8)         # graduations de l'échelle : tous les 8 dam
MSL_STEP = 5                             # écart entre isobares (hPa)
OUT_DIR = pathlib.Path(os.environ.get("OUT_DIR", "sortie"))
G0 = 9.80665
TIMEOUT = 120
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "veille-cartes-j6/1.0 (usage personnel, non commercial)"

MODELS = {
    "gfs": {"titre": "GFS", "source": "NOAA / NCEP — domaine public"},
    "ifs": {"titre": "ECMWF IFS", "source": "ECMWF Open Data — CC BY 4.0"},
    "aifs": {"titre": "ECMWF AIFS", "source": "ECMWF Open Data — CC BY 4.0"},
    "ukmo": {"titre": "UKMO", "source": "Met Office — CC BY-SA 4.0"},
}
UKMO_MAX_H = 144                         # le modèle global du Met Office s'arrête à +144 h

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def log(msg: str) -> None:
    print(f"[{utcnow():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Champs
# --------------------------------------------------------------------------
class Field:
    """Champ prêt à tracer : lat/lon 1D croissants, z en dam, msl en hPa (float32)."""

    def __init__(self, lat, lon, z_dam, msl_hpa):
        self.lat, self.lon, self.z, self.msl = lat, lon, z_dam, msl_hpa


def _norm(da: xr.DataArray) -> xr.DataArray:
    """Longitudes -180..180, tri, découpage sur DATA_BOX, allègement des grilles très fines."""
    da = da.squeeze(drop=True)
    lat_name = next(n for n in da.dims if "lat" in n.lower())
    lon_name = next(n for n in da.dims if "lon" in n.lower())
    da = da.rename({lat_name: "lat", lon_name: "lon"})
    da = da.assign_coords(lon=(((da.lon + 180) % 360) - 180)).sortby("lon").sortby("lat")
    lo0, lo1, la0, la1 = DATA_BOX
    da = da.sel(lon=slice(lo0, lo1), lat=slice(la0, la1))
    flat = max(1, int(round(GRID_MIN_DEG / float(abs(da.lat[1] - da.lat[0])))))
    flon = max(1, int(round(GRID_MIN_DEG / float(abs(da.lon[1] - da.lon[0])))))
    if flat > 1 or flon > 1:
        da = da.coarsen(lat=flat, lon=flon, boundary="trim").mean()
    return da


def to_field(z_da: xr.DataArray, msl_da: xr.DataArray, z_kind: str) -> Field:
    z = _norm(z_da)
    msl = _norm(msl_da)
    if msl.shape != z.shape:
        msl = msl.interp(lat=z.lat, lon=z.lon)
    zv = z.values.astype("float32")
    if z_kind == "geopotential":          # m²/s² -> m
        zv = zv / G0
    zv = zv / 10.0                         # m -> dam
    mv = msl.values.astype("float32")
    if np.nanmax(mv) > 2000:              # Pa -> hPa
        mv = mv / 100.0
    return Field(z.lat.values.astype("float32"), z.lon.values.astype("float32"), zv, mv)


def open_grib(path: str) -> xr.Dataset:
    return xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})


def by_step(da: xr.DataArray) -> dict[int, xr.DataArray]:
    """Découpe un DataArray GRIB multi-échéances en {heure: DataArray}."""
    if "step" in da.dims:
        return {int(s / np.timedelta64(1, "h")): da.sel(step=s) for s in da.step.values}
    return {int(da.step.values / np.timedelta64(1, "h")): da}


# --------------------------------------------------------------------------
# GFS (NOAA, bucket AWS public, lecture partielle grâce au fichier .idx)
# --------------------------------------------------------------------------
def _gfs_step(run: dt.datetime, step: int, tmp: str) -> Field:
    base = (f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{run:%Y%m%d}/{run:%H}/atmos/"
            f"gfs.t{run:%H}z.pgrb2.0p25.f{step:03d}")
    idx = HTTP.get(base + ".idx", timeout=TIMEOUT)
    idx.raise_for_status()
    lines = [ln.split(":") for ln in idx.text.strip().splitlines()]
    files = {}
    for key, pattern in {"z": f":HGT:{LEVEL_HPA} mb:", "msl": ":PRMSL:mean sea level:"}.items():
        for i, parts in enumerate(lines):
            if f":{parts[3]}:{parts[4]}:" == pattern:
                start = int(parts[1])
                end = int(lines[i + 1][1]) - 1 if i + 1 < len(lines) else ""
                r = HTTP.get(base, headers={"Range": f"bytes={start}-{end}"}, timeout=TIMEOUT)
                r.raise_for_status()
                files[key] = os.path.join(tmp, f"gfs_{key}_{step:03d}.grib2")
                pathlib.Path(files[key]).write_bytes(r.content)
                break
        else:
            raise RuntimeError(f"champ {pattern} absent de l'index")
    return to_field(open_grib(files["z"])["gh"], open_grib(files["msl"])["prmsl"], "height")


def fetch_gfs(run: dt.datetime, steps: list[int], tmp: str) -> dict[int, Field]:
    out = {}
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_gfs_step, run, s, tmp): s for s in steps}
        for fut in cf.as_completed(futures):
            try:
                out[futures[fut]] = fut.result()
            except Exception as exc:
                log(f"    GFS +{futures[fut]} h : {exc}")
    return out


# --------------------------------------------------------------------------
# ECMWF IFS et AIFS (paquet officiel ecmwf-opendata, miroir AWS)
# --------------------------------------------------------------------------
def fetch_ecmwf(model: str, run: dt.datetime, steps: list[int], tmp: str) -> dict[int, Field]:
    from ecmwf.opendata import Client  # import tardif : inutile en mode démo

    client = Client(source="aws", model=model)
    zparam = "gh" if model == "ifs" else "z"   # IFS : hauteur géopotentielle ; AIFS : géopotentiel
    kind = "height" if zparam == "gh" else "geopotential"
    common = dict(date=run.strftime("%Y%m%d"), time=run.hour, type="fc", stream="oper")

    def grab(step_list: list[int], tag: str) -> dict[int, Field]:
        fz = os.path.join(tmp, f"{model}_z_{tag}.grib2")
        fm = os.path.join(tmp, f"{model}_msl_{tag}.grib2")
        client.retrieve(**common, step=step_list, levtype="pl", levelist=LEVEL_HPA, param=zparam, target=fz)
        client.retrieve(**common, step=step_list, levtype="sfc", param="msl", target=fm)
        zs, ms = by_step(open_grib(fz)[zparam]), by_step(open_grib(fm)["msl"])
        return {s: to_field(zs[s], ms[s], kind) for s in zs if s in ms}

    try:  # une seule requête pour toutes les échéances (le plus rapide)
        return grab(steps, "all")
    except Exception as exc:  # une échéance manquante fait échouer le lot : on passe échéance par échéance
        log(f"    {model} : requête groupée impossible ({exc}), passage échéance par échéance")
    out = {}
    for s in steps:
        try:
            out.update(grab([s], f"{s:03d}"))
        except Exception as exc:
            log(f"    {model} +{s} h : {exc}")
    return out


# --------------------------------------------------------------------------
# UKMO (Met Office, bucket AWS public, NetCDF "un paramètre / une échéance")
# --------------------------------------------------------------------------
UKMO_BUCKET = "https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com"
S3NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def s3_list(prefix: str, delimiter: str | None = None) -> tuple[list[str], list[str]]:
    params = {"list-type": "2", "prefix": prefix}
    if delimiter:
        params["delimiter"] = delimiter
    keys, prefixes = [], []
    while True:
        r = HTTP.get(UKMO_BUCKET + "/", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        keys += [e.text for e in root.iter(f"{S3NS}Key")]
        prefixes += [e.text for e in root.iter(f"{S3NS}Prefix") if e.text != prefix]
        nxt = root.find(f"{S3NS}NextContinuationToken")
        if nxt is None:
            return keys, prefixes
        params["continuation-token"] = nxt.text


def ukmo_root() -> str:
    _, prefixes = s3_list("", delimiter="/")
    for p in prefixes:
        if "global" in p and "determin" in p:
            return p
    raise RuntimeError(f"dossier du modèle global introuvable parmi {prefixes}")


def pick_var(ds: xr.Dataset, needles: tuple[str, ...]) -> xr.DataArray:
    for name, da in ds.data_vars.items():
        meta = " ".join([name, str(da.attrs.get("standard_name", "")),
                         str(da.attrs.get("long_name", ""))]).lower()
        if da.ndim >= 2 and any(n in meta for n in needles):
            return da
    raise RuntimeError(f"variable {needles} introuvable ({list(ds.data_vars)})")


def open_remote_nc(url: str, tmp: str) -> xr.Dataset:
    """Lecture partielle à distance (seuls les morceaux utiles sont téléchargés) ;
    à défaut, téléchargement complet du fichier."""
    try:
        import fsspec

        f = fsspec.open(url, mode="rb", block_size=8 * 2**20).open()
        return xr.open_dataset(f, engine="h5netcdf")
    except Exception as exc:
        log(f"    UKMO : lecture partielle impossible ({exc.__class__.__name__}), téléchargement complet")
        dest = os.path.join(tmp, os.path.basename(url))
        with HTTP.get(url, stream=True, timeout=TIMEOUT) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                shutil.copyfileobj(r.raw, fh, 1 << 20)
        return xr.open_dataset(dest)


def _ukmo_step(root: str, run: dt.datetime, step: int, tmp: str) -> Field:
    run_id = f"{run:%Y%m%dT%H%MZ}"
    keys, _ = s3_list(f"{root}{run_id}/{run_id}-PT{step:04d}H00M-")
    k_msl = next((k for k in keys if "pressure_at_mean_sea_level" in k), None)
    k_z = next((k for k in keys if "geopotential_height_on_pressure_levels" in k), None) \
        or next((k for k in keys if "height_ASL_on_pressure_levels" in k), None)
    if not (k_msl and k_z):
        raise RuntimeError("fichiers absents")
    msl = pick_var(open_remote_nc(f"{UKMO_BUCKET}/{k_msl}", tmp), ("sea_level",))
    z = pick_var(open_remote_nc(f"{UKMO_BUCKET}/{k_z}", tmp), ("geopotential", "height"))
    lev = next(d for d in z.dims if "press" in d.lower())
    target = LEVEL_HPA * 100 if float(z[lev].max()) > 2000 else LEVEL_HPA  # Pa ou hPa
    return to_field(z.sel({lev: target}, method="nearest"), msl, "height")


def fetch_ukmo(run: dt.datetime, steps: list[int], tmp: str) -> dict[int, Field]:
    root = ukmo_root()
    out = {}
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_ukmo_step, root, run, s, tmp): s for s in steps if s <= UKMO_MAX_H}
        for fut in cf.as_completed(futures):
            try:
                out[futures[fut]] = fut.result()
            except Exception as exc:
                log(f"    UKMO +{futures[fut]} h : {exc}")
    return out


FETCHERS = {
    "gfs": fetch_gfs,
    "ifs": lambda r, s, t: fetch_ecmwf("ifs", r, s, t),
    "aifs": lambda r, s, t: fetch_ecmwf("aifs-single", r, s, t),
    "ukmo": fetch_ukmo,
}


# --------------------------------------------------------------------------
# Champs simulés (mode --demo) : dépressions qui se déplacent vers l'est
# --------------------------------------------------------------------------
def demo_fields(seed: int, steps: list[int], shift: int) -> dict[int, Field]:
    rng = np.random.default_rng(seed)
    lat = np.arange(DATA_BOX[2], DATA_BOX[3] + 0.01, 0.5, dtype="float32")
    lon = np.arange(DATA_BOX[0], DATA_BOX[1] + 0.01, 0.5, dtype="float32")
    LON, LAT = np.meshgrid(lon, lat)
    systems = [(rng.uniform(-60, -10), rng.uniform(42, 65), rng.choice([-1, 1]) * rng.uniform(15, 30),
                rng.uniform(0.25, 0.45)) for _ in range(4)]
    out = {}
    for s in steps:
        h = s - shift
        z = 588 - 1.9 * (LAT - 25) + 5 * np.sin(np.radians((LON - 0.3 * h) * 4))
        msl = np.full_like(LON, 1014.0)
        for cx, cy, amp, speed in systems:
            bump = np.exp(-(((LON - cx - speed * h) / 13) ** 2 + ((LAT - cy) / 8) ** 2))
            msl += amp * bump
            z += amp * 0.6 * bump
        out[s] = Field(lat, lon, z.astype("float32"), msl.astype("float32"))
    return out


# --------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------
CMAP = LinearSegmentedColormap.from_list("z500", [
    "#2b0b3f", "#5a1d7a", "#3b2fbf", "#1f62d6", "#1e9be0", "#25c7d9",
    "#3fd18b", "#8fdc3a", "#e6e22e", "#f7b733", "#f2762e", "#e0412c",
    "#b81f2a", "#7c0f1e"], N=len(Z_LEVELS) + 1)  # +2 cases pour les débordements
NORM = BoundaryNorm(Z_LEVELS, CMAP.N, extend="both")
NATURAL_EARTH = [("physical", "ne_50m_coastline"), ("cultural", "ne_50m_admin_0_boundary_lines_land")]
NE_GITHUB = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master"


def ensure_basemap() -> None:
    """Fonds de carte Natural Earth (domaine public) : préchargés depuis le dépôt GitHub officiel,
    au cas où le serveur habituel de cartopy serait injoignable."""
    import cartopy

    root = pathlib.Path(cartopy.config["data_dir"]) / "shapefiles" / "natural_earth"
    for cat, name in NATURAL_EARTH:
        folder = root / cat
        if (folder / f"{name}.shp").exists():
            continue
        folder.mkdir(parents=True, exist_ok=True)
        try:
            for ext in ("shp", "shx", "dbf", "prj"):
                r = HTTP.get(f"{NE_GITHUB}/50m_{cat}/{name}.{ext}", timeout=TIMEOUT)
                r.raise_for_status()
                (folder / f"{name}.{ext}").write_bytes(r.content)
        except Exception as exc:
            log(f"Fond de carte {name} non préchargé ({exc.__class__.__name__}), cartopy essaiera lui-même")


def date_fr(d: dt.datetime) -> str:
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d:%H}h UTC"


_FRAME = {}


def frame():
    """Projection de Lambert et cadre (calculés une fois par processus)."""
    if not _FRAME:
        import cartopy.crs as ccrs

        proj = ccrs.LambertConformal(central_longitude=-8, central_latitude=50, standard_parallels=(35, 65))
        pts = proj.transform_points(ccrs.PlateCarree(), *np.array(FRAME_POINTS, dtype=float).T)
        x0, x1 = pts[:, 0].min(), pts[:, 0].max()
        y0, y1 = pts[:, 1].min(), pts[:, 1].max()
        dx, dy = (x1 - x0) * FRAME_PAD, (y1 - y0) * FRAME_PAD
        _FRAME.update(proj=proj, extent=[x0 - dx, x1 + dx, y0 - dy, y1 + dy])
    return _FRAME["proj"], _FRAME["extent"]


def render(job: tuple) -> str:
    key, field, run, step, path = job
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from PIL import Image

    pc = ccrs.PlateCarree()
    proj, (x0, x1, y0, y1) = frame()
    valid = run + dt.timedelta(hours=step)

    W = 9.6
    map_w = W * 0.96
    map_h = map_w * (y1 - y0) / (x1 - x0)
    top, bottom = 1.0, 0.95
    H = map_h + top + bottom
    fig = plt.figure(figsize=(W, H), dpi=110)
    ax = fig.add_axes([0.02, bottom / H, 0.96, map_h / H], projection=proj)
    ax.set_extent([x0, x1, y0, y1], crs=proj)
    yf = lambda inches_from_top: 1 - inches_from_top / H  # noqa: E731

    cfill = ax.contourf(field.lon, field.lat, np.clip(field.z, Z_LEVELS[0] - 1, Z_LEVELS[-1] + 1),
                        levels=Z_LEVELS, cmap=CMAP, norm=NORM, extend="both", transform=pc)
    lo = np.floor(np.nanmin(field.msl) / MSL_STEP) * MSL_STEP
    hi = np.ceil(np.nanmax(field.msl) / MSL_STEP) * MSL_STEP
    cs = ax.contour(field.lon, field.lat, field.msl, levels=np.arange(lo, hi + 1, MSL_STEP),
                    colors="white", linewidths=0.9, transform=pc)
    ax.clabel(cs, fmt="%d", fontsize=7, inline=True)
    try:
        coast = cfeature.COASTLINE.with_scale("50m")
        next(iter(coast.geometries()))  # force le chargement maintenant
        ax.add_feature(coast, linewidth=0.6, edgecolor="#1a1a1a")
        ax.add_feature(cfeature.BORDERS.with_scale("50m"), linewidth=0.35, edgecolor="#1a1a1a")
    except Exception:
        pass

    fig.text(0.02, yf(0.18), MODELS[key]["titre"], fontsize=22, fontweight="bold", va="top")
    fig.text(0.02, yf(0.62), f"Run {run:%d/%m/%Y %H}h UTC  ·  +{step} h  ·  valable {date_fr(valid)}",
             fontsize=10.5, va="top", color="#333333")
    fig.text(0.98, yf(0.18), f"Géopotentiel {LEVEL_HPA} hPa (dam)\n& pression au niveau de la mer (hPa)",
             fontsize=9, ha="right", va="top", color="#333333")
    cax = fig.add_axes([0.10, 0.50 / H, 0.80, 0.17 / H])
    cb = fig.colorbar(cfill, cax=cax, orientation="horizontal", ticks=Z_TICKS)
    cb.minorticks_off()
    cb.ax.tick_params(labelsize=7)
    fig.text(0.5, 0.10 / H, f"Données : {MODELS[key]['source']}  ·  carte reconstruite à partir des données brutes",
             fontsize=7.5, ha="center", color="#555555")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    # Palette de 256 couleurs : fichiers ~3 fois plus légers, sans perte visible sur ces aplats
    Image.open(path).convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(path, optimize=True)
    return str(path)


def make_grid(paths: list[pathlib.Path], out: pathlib.Path) -> None:
    from PIL import Image

    imgs = [Image.open(p).convert("RGB") for p in paths]
    w, h = imgs[0].size
    grid = Image.new("RGB", (2 * w, 2 * h), "white")
    for i, im in enumerate(imgs[:4]):
        grid.paste(im.resize((w, h)), ((i % 2) * w, (i // 2) * h))
    grid.quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(out, optimize=True)


# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="rendu hors ligne avec des champs simulés")
    args = ap.parse_args()

    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ensure_basemap()
    base = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)  # 00 UTC du jour
    # (run, décalage) : run de 00 UTC du jour, sinon run de 12 UTC de la veille décalé de 12 h
    candidates = [(base, 0), (base - dt.timedelta(hours=12), 12)]
    manifest = {"genere_le": utcnow().isoformat(timespec="minutes") + "Z",
                "base": base.isoformat() + "Z", "pas_h": PAS_H, "offsets": OFFSETS,
                "niveau_hpa": LEVEL_HPA, "demo": args.demo, "modeles": {}}
    workers = max(1, (os.cpu_count() or 2))

    with tempfile.TemporaryDirectory() as tmp, mp.get_context("fork").Pool(workers) as pool:
        for i, key in enumerate(MODELS):
            log(f"{MODELS[key]['titre']} …")
            entry = {"titre": MODELS[key]["titre"], "source": MODELS[key]["source"],
                     "statut": "indisponible", "images": {str(o): None for o in OFFSETS}}
            for run, shift in candidates:
                steps = [o + shift for o in OFFSETS]
                try:
                    td = time.time()
                    fields = demo_fields(i, steps, shift) if args.demo else FETCHERS[key](run, steps, tmp)
                    # Le run de 00 UTC doit être complet jusqu'à J+6, sinon on tente le run de la veille.
                    if shift == 0 and steps[-1] not in fields:
                        raise RuntimeError(f"{len(fields)}/{len(steps)} échéances disponibles, J+6 manquante")
                    if not fields:
                        raise RuntimeError("aucune échéance disponible")
                    log(f"  données : {len(fields)}/{len(steps)} échéances en {time.time() - td:.0f} s")
                    tr = time.time()
                    jobs = [(key, fields[o + shift], run, o + shift, OUT_DIR / f"{key}_{o:03d}.png")
                            for o in OFFSETS if o + shift in fields]
                    for path in pool.imap_unordered(render, jobs):
                        name = pathlib.Path(path).name
                        entry["images"][str(int(name[-7:-4]))] = name
                    log(f"  rendu : {len(jobs)} cartes en {time.time() - tr:.0f} s")
                    entry.update(statut="ok", run=run.isoformat() + "Z", decalage_h=shift)
                    final = OUT_DIR / f"{key}_{ECHEANCE_MAX_H:03d}.png"
                    if final.exists():
                        shutil.copyfile(final, OUT_DIR / f"{key}.png")
                    break
                except Exception as exc:
                    log(f"  échec run {run:%d/%m %H}h : {exc}")
                    if os.environ.get("DEBUG"):
                        traceback.print_exc()
            manifest["modeles"][key] = entry

    finals = [OUT_DIR / f"{k}.png" for k in MODELS]
    if all(p.exists() for p in finals):
        make_grid(finals, OUT_DIR / "grille.png")
        manifest["grille"] = "grille.png"
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    viewer = pathlib.Path(__file__).with_name("index.html")
    if viewer.exists():
        shutil.copyfile(viewer, OUT_DIR / "index.html")

    n = sum(v is not None for m in manifest["modeles"].values() for v in m["images"].values())
    size = sum(p.stat().st_size for p in OUT_DIR.glob("*.png")) / 1e6
    log(f"Terminé : {n} cartes, {size:.1f} Mo d'images, en {time.time() - t0:.0f} s")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
