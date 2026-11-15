"""Science – Nature : résumés en français des nouveaux articles de recherche de Nature.

Chaque soir, le script :
1. lit la liste des articles de recherche de Nature (https://www.nature.com/nature/articles?type=article) ;
2. garde ceux qui ne sont pas encore connus et les trie par thème (themes.json) — une seule requête
   courte à l'IA pour tous les titres du jour ;
3. pour les articles d'un thème **en accès libre** (étiquette « Open Access » et licence Creative
   Commons sur la page), lit l'article et demande à l'IA une fiche en français : titre traduit,
   l'essentiel en une phrase, résumé, chiffres clés, schéma de conclusion, choix et légende du
   graphique le plus parlant ;
4. les articles payants d'un thème sont listés sans résumé (titre + lien) ;
5. écrit nature.json (30 derniers jours) dans OUT_DIR, lu par app/nature/index.html.

IA utilisée, dans l'ordre (on passe à la suivante si l'une refuse : clé absente, plus de crédit,
plafond gratuit atteint…) :
  1. Claude (API Anthropic)       si ANTHROPIC_API_KEY est fourni et le compte a du crédit ;
  2. GitHub Models (gratuit)      avec le jeton du workflow (permission « models: read ») ;
  3. traduction hors ligne        Argos Translate, gratuite et illimitée, mais sans schéma ni chiffres
                                  clés (titre, chapeau, résumé et titre du graphique traduits) ;
     le tri se fait alors par les mots-clés de themes.json.
Une fiche « traduction » est refaite par une IA dès qu'une IA redevient disponible.

Variables d'environnement :
  ANTHROPIC_API_KEY  clé API Claude (secret GitHub, facultatif)
  GITHUB_TOKEN       jeton du workflow, pour GitHub Models (facultatif)
  MODELE_GITHUB      modèle GitHub Models (défaut openai/gpt-4.1-mini)
  OUT_DIR            dossier de sortie (défaut sortie/nature)
  PREV_JSON          nature.json publié la veille : évite de refaire les articles déjà traités
  MODELE_TRI / MODELE_RESUME   modèles Claude (défaut : Haiku, le moins cher)
  SANS_TRADUCTION=1  ne pas installer le traducteur hors ligne
  DEBUG=1            journal détaillé

Test hors ligne : python build_nature.py --test   (fixtures de test/, sans réseau ni clé)
"""
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- réglages
LISTE_URL = ("https://www.nature.com/nature/articles?searchType=journalSearch"
             "&sort=PubDate&type=article&page={page}")
BASE = "https://www.nature.com"
JOURS_ARCHIVE = 30          # articles gardés dans nature.json
JOURS_PREMIER_PASSAGE = 7   # au tout premier passage, on remonte ces jours-là seulement
PAGES_MAX = 5               # 20 articles par page
MAX_FICHES = 12             # fiches IA au plus par exécution (plafond de coût / de quota)
TEXTE_MAX = 24000           # caractères de l'article envoyés à Claude (≈ 6 000 tokens)
TEXTE_MAX_GITHUB = 11000    # GitHub Models gratuit : 8 000 tokens par requête au plus
PAUSE = 1.0                 # secondes entre deux pages Nature (politesse)
MODELE_TRI = os.environ.get("MODELE_TRI", "claude-haiku-5-5")
MODELE_RESUME = os.environ.get("MODELE_RESUME", "claude-haiku-5-5")

ICI = pathlib.Path(__file__).resolve().parent
THEMES = json.loads((ICI / "themes.json").read_text("utf-8"))["themes"]
IDS_THEMES = [t["id"] for t in THEMES]
OUT_DIR = pathlib.Path(os.environ.get("OUT_DIR", "sortie/nature"))
PREV_JSON = os.environ.get("PREV_JSON", "")
CLE = os.environ.get("ANTHROPIC_API_KEY", "").strip()
JETON_GITHUB = os.environ.get("GITHUB_TOKEN", "").strip()
MODELE_GITHUB = os.environ.get("MODELE_GITHUB", "openai/gpt-4.1-mini")
SANS_TRADUCTION = os.environ.get("SANS_TRADUCTION") == "1"
DEBUG = os.environ.get("DEBUG") == "1"

# Sections de l'article qu'on n'envoie pas à Claude (méthodes, références, mentions…).
SECTIONS_EXCLUES = {
    "Abstract", "Inline Recommendations", "Methods", "Data availability", "Code availability",
    "References", "Acknowledgements", "Funding", "Author information", "Ethics declarations",
    "Peer review", "Additional information", "Supplementary information", "Source data",
    "Rights and permissions", "About this article", "Change history", "Comments",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) veille-perso/1.0 (+https://github.com/romy-fr/veille)",
    "Accept-Language": "en",
})


def log(*a):
    print(*a, flush=True)


def dbg(*a):
    if DEBUG:
        print("  ·", *a, flush=True)


def get(url):
    for essai in range(3):
        try:
            r = SESSION.get(url, timeout=40)
            if r.status_code == 200:
                return r.text
            dbg(f"{url} → HTTP {r.status_code}")
            if r.status_code in (403, 404):
                break
        except requests.RequestException as e:
            dbg(f"{url} → {e}")
        time.sleep(3 * (essai + 1))
    raise RuntimeError(f"page inaccessible : {url}")


def txt(el):
    if not el:
        return ""
    t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    return re.sub(r"\s+([.,;:)\]])", r"\1", re.sub(r"([(\[])\s+", r"\1", t))


# ---------------------------------------------------------------- lecture de Nature
def lire_liste(html):
    """Cartes de la liste des articles → [{url, doi, titre_en, date, libre, resume_nature, auteurs, vignette}]."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for art in soup.select("article"):
        a = art.select_one("h3 a[href*='/articles/']") or art.select_one("a[href*='/articles/']")
        t = art.select_one("time[datetime]")
        if not a or not t:
            continue
        href = a["href"]
        url = href if href.startswith("http") else BASE + href
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        typ = txt(art.select_one(".c-meta__type"))
        if typ and typ.lower() != "article":
            continue
        img = art.select_one(".c-card__image img")
        out.append({
            "doi": "10.1038/" + slug,
            "url": url,
            "titre_en": txt(a),
            "date": t["datetime"][:10],
            "libre": bool(art.select_one('[data-test="open-access"]')) or "open access" in txt(art).lower(),
            "resume_nature": txt(art.select_one('[data-test="article-description"]')),
            "auteurs": [txt(s) for s in art.select('[itemprop="creator"] [itemprop="name"]')],
            "vignette": img["src"] if img and img.get("src") else None,
        })
    return out


def licence_courte(url_ou_texte):
    s = (url_ou_texte or "").lower()
    if "by-nc-nd" in s or "noncommercial-noderivatives" in s:
        return "CC BY-NC-ND 4.0"
    if "by-nc-sa" in s:
        return "CC BY-NC-SA 4.0"
    if "by-nc" in s or "noncommercial" in s:
        return "CC BY-NC 4.0"
    if "/by/" in s or "creative commons attribution" in s:
        return "CC BY 4.0"
    return None


def lire_article(html):
    """Page d'un article → métadonnées, texte utile, figures, accès libre et licence."""
    soup = BeautifulSoup(html, "html.parser")
    # Appels de références (« helicase¹,² ») : retirés, ils gênent la lecture et la traduction.
    for sup in soup.select("sup"):
        if sup.select_one('a[data-test="citation-ref"], a[href*="#ref-"]'):
            sup.decompose()
    meta = lambda n: [m.get("content", "") for m in soup.select(f'meta[name="{n}"]')]
    ld = {}
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            j = json.loads(s.string or "")
            ld = j.get("mainEntity", j) if isinstance(j, dict) else {}
            if ld:
                break
        except ValueError:
            pass

    droits = txt(soup.select_one('section[data-title="Rights and permissions"]'))
    licence = licence_courte(ld.get("license")) or licence_courte(droits)
    libre = (ld.get("isAccessibleForFree") in (True, "true", "True")) and licence is not None

    # Figures principales (« Fig. n »), avec l'image en 685 px de large.
    figures = []
    for fig in soup.select("figure"):
        titre = txt(fig.select_one("figcaption"))
        m = re.match(r"Fig\.\s*(\d+)", titre)
        img = fig.select_one("img")
        if not m or not img or not img.get("src"):
            continue
        src = img["src"]
        src = "https:" + src if src.startswith("//") else src
        if any(f["numero"] == int(m.group(1)) for f in figures):
            continue
        lien = fig.select_one('a[href*="/figures/"]')
        figures.append({
            "numero": int(m.group(1)),
            "titre": titre,
            "legende": txt(fig.select_one(".c-article-section__figure-description"))[:600],
            "img": src,
            "img_grande": src.replace("/lw685/", "/full/"),
            "lien": BASE + lien["href"] if lien else None,
        })

    # Texte : résumé des auteurs + sections du corps (sans méthodes, références…, ni légendes).
    resume = txt(soup.select_one("#Abs1-content")) or (meta("dc.description") or [""])[0]
    morceaux = []
    for sec in soup.select("section[data-title]"):
        titre = sec["data-title"].strip()
        if titre in SECTIONS_EXCLUES or titre.startswith("Extended data"):
            continue
        for f in sec.select("figure"):
            f.decompose()
        morceaux.append(f"## {titre}\n{txt(sec)}")
    texte = "\n\n".join(morceaux)[:TEXTE_MAX]

    return {
        "titre_en": (meta("dc.title") or meta("citation_title") or [ld.get("headline", "")])[0],
        "auteurs": [inverser_nom(n) for n in meta("dc.creator")],
        "noms": [n.split(",")[0].strip() for n in meta("dc.creator")],
        "sujets": meta("dc.subject"),
        "resume_auteurs": resume,
        "texte": texte,
        "figures": figures,
        "libre": libre,
        "licence": licence,
    }


def inverser_nom(n):
    """« Polo Rivera, Cristian » → « Cristian Polo Rivera »."""
    if "," in n:
        nom, prenom = [x.strip() for x in n.split(",", 1)]
        return f"{prenom} {nom}"
    return n


# ---------------------------------------------------------------- IA
class Indisponible(Exception):
    """L'IA refuse pour la suite de l'exécution (clé invalide, plus de crédit, quota du jour…)."""


USAGE = {"appels": 0, "entree": 0, "sortie": 0, "par_ia": {}}


def compter(nom, entree, sortie):
    USAGE["appels"] += 1
    USAGE["entree"] += entree
    USAGE["sortie"] += sortie
    USAGE["par_ia"][nom] = USAGE["par_ia"].get(nom, 0) + 1


def poster(url, headers, corps, nom):
    """POST avec nouvelles tentatives ; lève Indisponible si l'IA refuse durablement."""
    for essai in range(4):
        try:
            r = requests.post(url, headers=headers, json=corps, timeout=180)
        except requests.RequestException as e:
            dbg(f"{nom} : {e}")
            time.sleep(10 * (essai + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                raise Indisponible(f"{nom} : réponse illisible ({r.headers.get('content-type')}, "
                                   f"{r.url}) {r.text[:200]!r}")
        msg = r.text[:300].replace("\n", " ")
        if r.status_code == 429 and "per day" not in msg.lower() and essai < 3:
            time.sleep(min(int(r.headers.get("retry-after", "0") or 0), 60) or 15 * (essai + 1))
            continue
        if r.status_code in (500, 502, 503, 529) and essai < 3:
            time.sleep(10 * (essai + 1))
            continue
        if r.status_code == 413 or (r.status_code == 400 and "token" in msg.lower() and "credit" not in msg.lower()):
            raise RuntimeError(f"{nom} : demande trop longue ({msg})")
        raise Indisponible(f"{nom} : HTTP {r.status_code} {msg}")
    raise Indisponible(f"{nom} : pas de réponse")


def appel_claude(modele, systeme, message, max_tokens):
    j = poster("https://api.anthropic.com/v1/messages",
               {"x-api-key": CLE, "anthropic-version": "2023-06-01", "content-type": "application/json"},
               {"model": modele, "max_tokens": max_tokens, "system": systeme,
                "messages": [{"role": "user", "content": message}]}, "Claude")
    u = j.get("usage", {})
    compter("claude", u.get("input_tokens", 0), u.get("output_tokens", 0))
    return "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")


def appel_github(modele, systeme, message, max_tokens):
    j = poster("https://models.github.ai/inference/chat/completions",
               {"Authorization": f"Bearer {JETON_GITHUB}", "Content-Type": "application/json",
                "Accept": "application/vnd.github+json"},
               {"model": modele, "max_tokens": min(max_tokens, 3500), "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": systeme}, {"role": "user", "content": message}]},
               "GitHub Models")
    u = j.get("usage", {})
    compter("github", u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    return j["choices"][0]["message"]["content"] or ""


# IA disponibles, dans l'ordre de préférence. Une IA qui refuse est retirée pour la suite.
IAS = []
if CLE:
    IAS.append({"nom": "claude", "texte_max": TEXTE_MAX,
                "appel": lambda s, m, n, gros: appel_claude(MODELE_RESUME if gros else MODELE_TRI, s, m, n),
                "modele": lambda gros: MODELE_RESUME if gros else MODELE_TRI})
if JETON_GITHUB:
    IAS.append({"nom": "github", "texte_max": TEXTE_MAX_GITHUB,
                "appel": lambda s, m, n, gros: appel_github(MODELE_GITHUB, s, m, n),
                "modele": lambda gros: MODELE_GITHUB})


def ia(systeme, construire, max_tokens, gros=False):
    """Demande à la première IA disponible. `construire(texte_max)` fabrique le message.
    Retourne (réponse JSON, modèle). Lève Indisponible s'il ne reste aucune IA."""
    while IAS:
        f = IAS[0]
        try:
            rep = f["appel"](systeme, construire(f["texte_max"]), max_tokens, gros)
            return extraire_json(rep), f["modele"](gros)
        except Indisponible as e:
            log(f"⚠ {e} → IA suivante")
            IAS.pop(0)
    raise Indisponible("aucune IA disponible")


def extraire_json(s):
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M)
    i, j = s.find("{"), s.rfind("}")
    return json.loads(s[i:j + 1])


# ---------------------------------------------------------------- traduction hors ligne (secours)
_TRAD = {"etat": None}


def traducteur():
    """Installe Argos Translate et le modèle anglais → français à la première utilisation."""
    if _TRAD["etat"] is None:
        _TRAD["etat"] = False
        if SANS_TRADUCTION:
            return None
        try:
            import subprocess
            log("Installation du traducteur hors ligne (Argos Translate)…")
            pip = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
            subprocess.run(pip + ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"], check=True)
            subprocess.run(pip + ["argostranslate"], check=True)
            import argostranslate.package
            import argostranslate.translate
            if not any(p.from_code == "en" and p.to_code == "fr"
                       for p in argostranslate.package.get_installed_packages()):
                argostranslate.package.update_package_index()
                pkg = next(p for p in argostranslate.package.get_available_packages()
                           if p.from_code == "en" and p.to_code == "fr")
                argostranslate.package.install_from_path(pkg.download())
            _TRAD["etat"] = lambda t: argostranslate.translate.translate(t, "en", "fr") if t else ""
        except Exception as e:
            log(f"⚠ traducteur hors ligne indisponible ({e})")
    return _TRAD["etat"] or None


# ---------------------------------------------------------------- tri par thème
def trier_mots_cles(items):
    res = {}
    for it in items:
        t = f" {it['titre_en']} {it.get('resume_nature', '')} ".lower()
        res[it["doi"]] = [th["id"] for th in THEMES
                          if any(re.search(r"(?<![a-z0-9])" + re.escape(k), t) for k in th.get("mots_cles", []))]
    return res


def trier(items):
    """Thèmes de chaque article, d'après le titre et le chapeau de Nature. Une seule requête."""
    if not items:
        return {}
    themes = "\n".join(f'- "{t["id"]}" : {t["description"]}' for t in THEMES)
    liste = "\n".join(f'{i}. {it["titre_en"]} — {it.get("resume_nature") or ""}' for i, it in enumerate(items, 1))
    systeme = ("Tu tries des articles scientifiques par thème. Un article n'entre dans un thème que s'il en "
               "parle vraiment (sujet principal ou application directe), pas par un lien lointain. "
               "Réponds uniquement en JSON.")
    message = (f"Thèmes :\n{themes}\n\nArticles :\n{liste}\n\n"
               'Réponds {"1": ["id", ...], "2": [], ...} avec, pour chaque numéro, la liste des thèmes '
               "(souvent vide).")
    try:
        j, _ = ia(systeme, lambda _n: message, 80 + 25 * len(items))
    except Exception as e:
        log(f"⚠ tri par IA impossible ({e}) : tri par mots-clés")
        return trier_mots_cles(items)
    return {it["doi"]: [t for t in j.get(str(i), []) if t in IDS_THEMES] for i, it in enumerate(items, 1)}


# ---------------------------------------------------------------- fiches
SYSTEME_FICHE = """Tu rédiges des fiches de lecture d'articles scientifiques pour Romain, data scientist \
passionné de météo, de climat et de sport, qui a une mémoire visuelle. Français clair, concis, précis, \
sans jargon inutile (explique en quelques mots un terme technique indispensable). N'invente rien : chaque \
fait et chaque chiffre doit venir de l'article. Réponds uniquement par un objet JSON valide."""

CONSIGNE_FICHE = """Fiche à produire, en JSON :
{
  "titre_fr": "traduction fidèle et naturelle du titre",
  "en_une_phrase": "l'apport principal de l'article, 25 mots au plus",
  "resume": "4 à 6 phrases : contexte, question, méthode, résultats, limites ou portée",
  "chiffres_cles": [{"valeur": "chiffre court avec unité, ex. « 1,8 °C » ou « ×3 »", "sens": "ce qu'il mesure, 10 mots au plus"}],
  "schema": [{"etape": "Question | Méthode | Résultat | Conséquence (ou titre court)", "texte": "15 mots au plus"}],
  "figure": {"numero": 2, "legende_fr": "ce que montre ce graphique et comment le lire, 2 phrases"},
  "themes": ["identifiants des thèmes concernés parmi : {themes}"]
}
- chiffres_cles : 0 à 3 chiffres marquants tirés de l'article.
- schema : 3 à 5 étapes qui s'enchaînent et résument tout l'article d'un coup d'œil.
- figure : parmi les figures listées, celle qui montre le mieux le résultat principal ; préfère un \
graphique de données à un schéma de méthode. null s'il n'y a aucune figure."""


def fiche_ia(item, art):
    figs = "\n".join(f'Fig. {f["numero"]} — {f["titre"]} {f["legende"][:300]}' for f in art["figures"]) or "(aucune)"

    def construire(texte_max):
        return (f"Titre : {art['titre_en']}\n"
                f"Chapeau de Nature : {item.get('resume_nature') or '—'}\n"
                f"Mots-clés : {', '.join(art['sujets']) or '—'}\n\n"
                f"Résumé des auteurs :\n{art['resume_auteurs']}\n\n"
                f"Figures :\n{figs}\n\n"
                f"Texte de l'article (extraits) :\n{art['texte'][:texte_max]}\n\n"
                + CONSIGNE_FICHE.replace("{themes}", ", ".join(IDS_THEMES)))
    j, modele = ia(SYSTEME_FICHE, construire, 1800, gros=True)
    return {
        "titre_fr": str(j.get("titre_fr") or "").strip(),
        "en_une_phrase": str(j.get("en_une_phrase") or "").strip(),
        "resume": str(j.get("resume") or "").strip(),
        "chiffres_cles": [c for c in (j.get("chiffres_cles") or []) if isinstance(c, dict) and c.get("valeur")][:3],
        "schema": [e for e in (j.get("schema") or []) if isinstance(e, dict) and e.get("texte")][:5],
        "figure": j.get("figure") if isinstance(j.get("figure"), dict) else None,
        "themes": [t for t in (j.get("themes") or []) if t in IDS_THEMES],
        "modele": modele,
    }


def fiche_traduction(item, art, tr):
    """Secours sans IA : titre, chapeau, résumé et titre du graphique traduits."""
    titre_fig = re.sub(r"^Fig\.\s*\d+\s*:?\s*", "", art["figures"][0]["titre"]) if art["figures"] else ""
    return {
        "titre_fr": tr(art["titre_en"] or item["titre_en"]),
        "en_une_phrase": tr(item.get("resume_nature") or ""),
        "resume": tr(art["resume_auteurs"]),
        "chiffres_cles": [], "schema": [], "themes": [],
        "figure": {"numero": art["figures"][0]["numero"], "legende_fr": tr(titre_fig)} if art["figures"] else None,
        "modele": "Argos Translate (traduction automatique)",
    }


# ---------------------------------------------------------------- assemblage
def figure_choisie(art, numero):
    figs = art["figures"]
    if not figs:
        return None
    f = next((x for x in figs if x["numero"] == numero), figs[0])
    return {k: f[k] for k in ("numero", "titre", "img", "img_grande", "lien")}


def credit(item, art, f):
    a = art.get("noms") or [n.split()[-1] for n in item["auteurs"]]
    qui = (a[0] + (" et al." if len(a) > 1 else "")) if a else "Auteurs"
    return f"Fig. {f['numero']} · {qui}, Nature ({item['date'][:4]}) · {art['licence']}"


def traiter(item, themes_tri, budget):
    """Complète un article trié dans un thème. Retourne l'entrée publiée."""
    e = {k: item.get(k) for k in ("doi", "url", "titre_en", "date", "auteurs", "resume_nature", "vignette")}
    e["themes"] = themes_tri
    if not item["libre"]:
        e.update(acces="payant")
        return e
    art = lire_article(get(item["url"]))
    time.sleep(PAUSE)
    if not art["libre"]:
        e.update(acces="payant")
        return e
    e.update(acces="libre", licence=art["licence"], auteurs=art["auteurs"] or item["auteurs"],
             titre_en=art["titre_en"] or item["titre_en"], sujets=art["sujets"])

    fi, mode = None, None
    if IAS and budget["fiches"] > 0:
        try:
            fi, mode = fiche_ia(item, art), "ia"
            budget["fiches"] -= 1
        except Indisponible:
            pass
        except Exception as ex:
            log(f"  ⚠ fiche IA impossible ({ex})")
    if fi is None and (tr := traducteur()):
        try:
            fi, mode = fiche_traduction(item, art, tr), "traduction"
        except Exception as ex:
            log(f"  ⚠ traduction impossible ({ex})")

    if fi is None:   # ni IA ni traducteur : résumé d'origine en anglais et première figure
        f = figure_choisie(art, None)
        if f:
            f["credit"] = credit(e, art, f)
        e.update(resume_auteurs=art["resume_auteurs"], figure=f, fiche=False)
        return e

    choix = fi.pop("figure") or {}
    try:
        num = int(choix.get("numero"))
    except (TypeError, ValueError):
        num = None
    f = figure_choisie(art, num)
    if f:
        f["legende_fr"] = str(choix.get("legende_fr") or "").strip() if f["numero"] == num else ""
        f["credit"] = credit(e, art, f)
    themes_ia = fi.pop("themes")
    e.update(fi, figure=f, fiche=True, mode=mode)
    if themes_ia:
        e["themes"] = sorted(set(themes_tri) | set(themes_ia), key=IDS_THEMES.index)
    return e


def charger_precedent():
    try:
        p = pathlib.Path(PREV_JSON)
        if PREV_JSON and p.is_file():
            return json.loads(p.read_text("utf-8"))
    except ValueError as e:
        log(f"⚠ {PREV_JSON} illisible ({e}) : on repart de zéro")
    return {}


def nouveaux_articles(vus, limite):
    """Parcourt la liste (plus récents d'abord) jusqu'aux articles déjà vus ou trop anciens."""
    items = []
    for page in range(1, PAGES_MAX + 1):
        cartes = lire_liste(get(LISTE_URL.format(page=page)))
        if not cartes:
            if page == 1:
                raise RuntimeError("aucun article reconnu dans la liste de Nature (mise en page changée ?)")
            break
        frais = [c for c in cartes if c["date"] >= limite and c["doi"] not in vus]
        items += frais
        dbg(f"page {page} : {len(cartes)} articles, {len(frais)} nouveaux")
        if cartes[-1]["date"] < limite or not frais:
            break
        time.sleep(PAUSE)
    return items


def main():
    aujourd_hui = dt.date.today()
    prev = charger_precedent()
    garde = (aujourd_hui - dt.timedelta(days=JOURS_ARCHIVE)).isoformat()
    vus = {d: j for d, j in (prev.get("vus") or {}).items() if j >= garde}
    articles = {a["doi"]: a for a in prev.get("articles", []) if a.get("date", "") >= garde}
    limite = garde if prev else (aujourd_hui - dt.timedelta(days=JOURS_PREMIER_PASSAGE)).isoformat()

    log(f"Nature — {len(articles)} articles déjà publiés, recherche des nouveaux depuis le {limite} · "
        f"IA : {', '.join(f['nom'] for f in IAS) or 'aucune'}")
    items = nouveaux_articles(vus, limite)
    log(f"{len(items)} nouveaux articles de recherche ({sum(i['libre'] for i in items)} en accès libre)")

    tri = trier(items)
    budget = {"fiches": MAX_FICHES}
    for it in items:
        th = tri.get(it["doi"], [])
        vus[it["doi"]] = it["date"]
        if not th:
            dbg(f"hors thèmes : {it['titre_en']}")
            continue
        log(f"• [{', '.join(th)}] {'libre ' if it['libre'] else 'payant'} {it['titre_en']}")
        try:
            articles[it["doi"]] = traiter(it, th, budget)
        except Exception as e:
            log(f"  ⚠ {e}")
            vus.pop(it["doi"], None)          # on réessaiera demain

    # Articles libres sans fiche, ou avec une simple traduction : on les refait si une IA répond.
    for doi, a in sorted(articles.items(), key=lambda x: x[1]["date"], reverse=True):
        a_refaire = a.get("acces") == "libre" and (not a.get("fiche") or a.get("mode") == "traduction")
        if not a_refaire or (a.get("mode") == "traduction" and not IAS) or budget["fiches"] <= 0:
            continue
        if not IAS and SANS_TRADUCTION:
            break
        log(f"• rattrapage de la fiche : {a['titre_en']}")
        try:
            nouveau = traiter(dict(a, libre=True), a.get("themes", []), budget)
            if nouveau.get("fiche") or not a.get("fiche"):
                articles[doi] = nouveau
        except Exception as e:
            log(f"  ⚠ {e}")

    liste = sorted(articles.values(), key=lambda a: (a["date"], a["doi"]), reverse=True)
    sortie = {
        "maj": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "source": "Nature — https://www.nature.com/nature/articles?type=article",
        "themes": [{"id": t["id"], "nom": t["nom"]} for t in THEMES],
        "articles": liste,
        "vus": vus,
        "usage": USAGE,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "nature.json").write_text(json.dumps(sortie, ensure_ascii=False, indent=1), "utf-8")
    modes = {}
    for a in liste:
        if a.get("fiche"):
            modes[a.get("mode", "ia")] = modes.get(a.get("mode", "ia"), 0) + 1
    log(f"→ {OUT_DIR / 'nature.json'} : {len(liste)} articles, fiches {modes or 0} · IA : "
        f"{USAGE['appels']} appels {USAGE['par_ia']}, {USAGE['entree']} tokens en entrée, {USAGE['sortie']} en sortie")


# ---------------------------------------------------------------- test hors ligne
def test():
    """Rejoue le script sur les pages de test/ (copiées de nature.com), sans réseau."""
    global get, OUT_DIR, PREV_JSON, JOURS_PREMIER_PASSAGE, JOURS_ARCHIVE, SANS_TRADUCTION
    import tempfile
    JOURS_PREMIER_PASSAGE = JOURS_ARCHIVE = 100000     # les pages de test ne vieillissent pas
    rep = ICI / "test"
    pages = {
        LISTE_URL.format(page=1): "liste.html",
        BASE + "/articles/s41586-026-11204-0": "article_libre.html",
    }

    def faux_get(url):
        if url in pages:
            return (rep / pages[url]).read_text("utf-8")
        if "page=" in url:
            return "<html></html>"
        raise RuntimeError(f"page inattendue : {url}")

    cartes = lire_liste(faux_get(LISTE_URL.format(page=1)))
    assert len(cartes) == 3 and [c["libre"] for c in cartes] == [False, True, True], cartes
    assert cartes[1]["doi"] == "10.1038/s41586-026-11204-0" and cartes[1]["date"] == "2026-11-11"
    art = lire_article(faux_get(BASE + "/articles/s41586-026-11204-0"))
    assert art["libre"] and art["licence"] == "CC BY 4.0", art["licence"]
    assert [f["numero"] for f in art["figures"]] == [1, 2] and art["figures"][0]["img"].startswith("https://media")
    assert "Methods" not in art["texte"] and "PRIMPOL binds" in art["texte"] and "Fig. 1" not in art["texte"]
    assert art["auteurs"][0] == "Cristian Polo Rivera"
    assert art["resume_auteurs"].endswith("CMG helicase."), art["resume_auteurs"][-30:]
    print("lecture des pages : ok")

    get = faux_get
    sortie = pathlib.Path(tempfile.mkdtemp())
    lire = lambda d: json.loads((d / "nature.json").read_text("utf-8"))

    # 1) aucune IA, pas de traducteur : tri par mots-clés (aucun thème ici → rien publié)
    IAS.clear()
    SANS_TRADUCTION, _TRAD["etat"] = True, None
    OUT_DIR = sortie / "a"
    main()
    j = lire(OUT_DIR)
    assert len(j["vus"]) == 3 and j["articles"] == [], j

    # 2) Claude refuse (plus de crédit) → GitHub Models prend le relais : 1 payant + 1 fiche
    def claude_sans_credit(s, m, n, gros):
        raise Indisponible("Claude : HTTP 400 credit balance is too low")

    def faux_github(s, m, n, gros):
        compter("github", 10, 10)
        assert len(m) < 20000
        if "Thèmes :" in m:
            return '{"1": ["climat"], "2": ["climat"], "3": []}'
        return json.dumps({"titre_fr": "Titre traduit", "en_une_phrase": "Une phrase.", "resume": "Résumé.",
                           "chiffres_cles": [{"valeur": "×3", "sens": "plus de reprises"}],
                           "schema": [{"etape": "Question", "texte": "a"}, {"etape": "Résultat", "texte": "b"}],
                           "figure": {"numero": 2, "legende_fr": "Légende."}, "themes": ["climat", "inconnu"]})
    IAS[:] = [{"nom": "claude", "texte_max": TEXTE_MAX, "appel": claude_sans_credit, "modele": lambda g: "claude"},
              {"nom": "github", "texte_max": TEXTE_MAX_GITHUB, "appel": faux_github, "modele": lambda g: "gpt"}]
    OUT_DIR, PREV_JSON = sortie / "b", ""
    main()
    j = lire(OUT_DIR)
    a = {x["doi"][-5:]: x for x in j["articles"]}
    assert a["391-w"]["acces"] == "payant" and "resume" not in a["391-w"]
    f = a["204-0"]
    assert f["fiche"] and f["mode"] == "ia" and f["modele"] == "gpt" and f["figure"]["numero"] == 2
    assert f["figure"]["legende_fr"] == "Légende."
    assert f["figure"]["credit"] == "Fig. 2 · Polo Rivera et al., Nature (2026) · CC BY 4.0", f["figure"]["credit"]
    assert f["themes"] == ["climat"] and f["titre_fr"] == "Titre traduit"
    assert [x["nom"] for x in IAS] == ["github"]

    # 3) plus aucune IA : traduction de secours (faux traducteur), puis rattrapage par IA le lendemain
    IAS.clear()
    SANS_TRADUCTION, _TRAD["etat"] = False, (lambda t: f"FR({t[:20]})" if t else "")
    prev_b = str(OUT_DIR / "nature.json")
    d = lire(OUT_DIR)
    for x in d["articles"]:
        if x["acces"] == "libre":
            x.update(fiche=False)
    (sortie / "b2.json").write_text(json.dumps(d), "utf-8")
    OUT_DIR, PREV_JSON = sortie / "c", str(sortie / "b2.json")
    main()
    f = {x["doi"][-5:]: x for x in lire(OUT_DIR)["articles"]}["204-0"]
    assert f["fiche"] and f["mode"] == "traduction" and f["titre_fr"].startswith("FR(") and f["schema"] == []
    assert f["figure"]["legende_fr"].startswith("FR(Reconstitution"), f["figure"]
    IAS[:] = [{"nom": "github", "texte_max": TEXTE_MAX_GITHUB, "appel": faux_github, "modele": lambda g: "gpt"}]
    OUT_DIR, PREV_JSON = sortie / "d", str(OUT_DIR / "nature.json")
    main()
    f = {x["doi"][-5:]: x for x in lire(OUT_DIR)["articles"]}["204-0"]
    assert f["mode"] == "ia" and f["titre_fr"] == "Titre traduit"

    # 4) passage suivant : rien de nouveau, aucun appel
    OUT_DIR, PREV_JSON = sortie / "e", str(OUT_DIR / "nature.json")
    n = USAGE["appels"]
    main()
    assert len(lire(OUT_DIR)["articles"]) == 2 and USAGE["appels"] == n, "aucun nouvel appel attendu"
    print("tests : ok")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()
    else:
        try:
            main()
        except Exception as e:
            log(f"ÉCHEC : {e}")
            sys.exit(1)
