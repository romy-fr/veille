"""Science – Nature : résumés en français des nouveaux articles de recherche de Nature.

Chaque soir, le script :
1. lit la liste des articles de recherche de Nature (https://www.nature.com/nature/articles?type=article) ;
2. garde ceux qui ne sont pas encore connus et les trie par thème (themes.json) — une seule requête
   courte à Claude pour tous les titres du jour ;
3. pour les articles d'un thème **en accès libre** (étiquette « Open Access » et licence Creative
   Commons sur la page), lit l'article et demande à Claude une fiche en français : titre traduit,
   l'essentiel en une phrase, résumé, chiffres clés, schéma de conclusion, choix et légende du
   graphique le plus parlant ;
4. les articles payants d'un thème sont listés sans résumé (titre + lien) ;
5. écrit nature.json (30 derniers jours) dans OUT_DIR, lu par app/nature/index.html.

Variables d'environnement :
  ANTHROPIC_API_KEY  clé API Claude (secret GitHub). Sans clé : tri par mots-clés et résumé
                     d'origine en anglais, sans fiche.
  OUT_DIR            dossier de sortie (défaut sortie/nature)
  PREV_JSON          nature.json publié la veille : évite de refaire les articles déjà traités
  MODELE_TRI / MODELE_RESUME   modèles Claude (défaut : Haiku, le moins cher)
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
MAX_FICHES = 12             # fiches Claude au plus par exécution (plafond de coût)
TEXTE_MAX = 24000           # caractères de l'article envoyés à Claude (≈ 6 000 tokens)
PAUSE = 1.0                 # secondes entre deux pages Nature (politesse)
MODELE_TRI = os.environ.get("MODELE_TRI", "claude-haiku-5-5")
MODELE_RESUME = os.environ.get("MODELE_RESUME", "claude-haiku-5-5")

ICI = pathlib.Path(__file__).resolve().parent
THEMES = json.loads((ICI / "themes.json").read_text("utf-8"))["themes"]
IDS_THEMES = [t["id"] for t in THEMES]
OUT_DIR = pathlib.Path(os.environ.get("OUT_DIR", "sortie/nature"))
PREV_JSON = os.environ.get("PREV_JSON", "")
CLE = os.environ.get("ANTHROPIC_API_KEY", "").strip()
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
USAGE = {"entree": 0, "sortie": 0, "appels": 0}


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
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


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


# ---------------------------------------------------------------- Claude
def claude(modele, systeme, message, max_tokens):
    for essai in range(4):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLE, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": modele, "max_tokens": max_tokens, "system": systeme,
                  "messages": [{"role": "user", "content": message}]},
            timeout=180,
        )
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(10 * (essai + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"API Claude : HTTP {r.status_code} {r.text[:300]}")
        j = r.json()
        USAGE["appels"] += 1
        USAGE["entree"] += j.get("usage", {}).get("input_tokens", 0)
        USAGE["sortie"] += j.get("usage", {}).get("output_tokens", 0)
        return "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    raise RuntimeError("API Claude : trop de refus (429/5xx)")


def extraire_json(s):
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M)
    i, j = s.find("{"), s.rfind("}")
    return json.loads(s[i:j + 1])


def trier_mots_cles(items):
    res = {}
    for it in items:
        t = f" {it['titre_en']} {it.get('resume_nature', '')} ".lower()
        res[it["doi"]] = [th["id"] for th in THEMES if any(k in t for k in th.get("mots_cles", []))]
    return res


def trier(items):
    """Thèmes de chaque article, d'après le titre et le chapeau de Nature. Une seule requête."""
    if not items:
        return {}
    if not CLE:
        return trier_mots_cles(items)
    themes = "\n".join(f'- "{t["id"]}" : {t["description"]}' for t in THEMES)
    liste = "\n".join(f'{i}. {it["titre_en"]} — {it.get("resume_nature") or ""}' for i, it in enumerate(items, 1))
    systeme = ("Tu tries des articles scientifiques par thème. Un article n'entre dans un thème que s'il en "
               "parle vraiment (sujet principal ou application directe), pas par un lien lointain. "
               "Réponds uniquement en JSON.")
    message = (f"Thèmes :\n{themes}\n\nArticles :\n{liste}\n\n"
               'Réponds {"1": ["id", ...], "2": [], ...} avec, pour chaque numéro, la liste des thèmes '
               "(souvent vide).")
    try:
        j = extraire_json(claude(MODELE_TRI, systeme, message, 60 + 25 * len(items)))
    except Exception as e:
        log(f"⚠ tri par Claude impossible ({e}) : tri par mots-clés")
        return trier_mots_cles(items)
    return {it["doi"]: [t for t in j.get(str(i), []) if t in IDS_THEMES] for i, it in enumerate(items, 1)}


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


def fiche(item, art):
    figs = "\n".join(f'Fig. {f["numero"]} — {f["titre"]} {f["legende"][:300]}' for f in art["figures"]) or "(aucune)"
    message = (
        f"Titre : {art['titre_en']}\n"
        f"Chapeau de Nature : {item.get('resume_nature') or '—'}\n"
        f"Mots-clés : {', '.join(art['sujets']) or '—'}\n\n"
        f"Résumé des auteurs :\n{art['resume_auteurs']}\n\n"
        f"Figures :\n{figs}\n\n"
        f"Texte de l'article (extraits) :\n{art['texte']}\n\n"
        + CONSIGNE_FICHE.replace("{themes}", ", ".join(IDS_THEMES))
    )
    j = extraire_json(claude(MODELE_RESUME, SYSTEME_FICHE, message, 1800))
    return {
        "titre_fr": str(j.get("titre_fr") or "").strip(),
        "en_une_phrase": str(j.get("en_une_phrase") or "").strip(),
        "resume": str(j.get("resume") or "").strip(),
        "chiffres_cles": [c for c in (j.get("chiffres_cles") or []) if isinstance(c, dict) and c.get("valeur")][:3],
        "schema": [e for e in (j.get("schema") or []) if isinstance(e, dict) and e.get("texte")][:5],
        "figure": j.get("figure") if isinstance(j.get("figure"), dict) else None,
        "themes": [t for t in (j.get("themes") or []) if t in IDS_THEMES],
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
    e = {k: item[k] for k in ("doi", "url", "titre_en", "date", "auteurs", "resume_nature", "vignette")}
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
    if CLE and budget["fiches"] > 0:
        budget["fiches"] -= 1
        try:
            fi = fiche(item, art)
            choix = fi.pop("figure") or {}
            try:
                num = int(choix.get("numero"))
            except (TypeError, ValueError):
                num = None
            f = figure_choisie(art, num)
            if f:
                f["legende_fr"] = str(choix.get("legende_fr") or "").strip() if f["numero"] == num else ""
                f["credit"] = credit(e, art, f)
            e.update(fi, figure=f, fiche=True, modele=MODELE_RESUME)
            if fi["themes"]:
                e["themes"] = sorted(set(themes_tri) | set(fi["themes"]), key=IDS_THEMES.index)
            return e
        except Exception as ex:
            log(f"  ⚠ fiche impossible ({ex}) : résumé d'origine")
    # Sans clé (ou en cas d'échec) : résumé des auteurs en anglais et première figure.
    f = figure_choisie(art, None)
    if f:
        f["credit"] = credit(e, art, f)
    e.update(resume_auteurs=art["resume_auteurs"], figure=f, fiche=False)
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

    log(f"Nature — {len(articles)} articles déjà publiés, recherche des nouveaux depuis le {limite}"
        f" ({'avec' if CLE else 'SANS'} clé API Claude)")
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

    # Articles libres publiés sans fiche (clé absente, plafond atteint, erreur) : on rattrape.
    if CLE:
        for doi, a in sorted(articles.items(), key=lambda x: x[1]["date"], reverse=True):
            if a.get("acces") == "libre" and not a.get("fiche") and budget["fiches"] > 0:
                log(f"• rattrapage de la fiche : {a['titre_en']}")
                try:
                    it = dict(a, libre=True)
                    articles[doi] = traiter(it, a.get("themes", []), budget)
                except Exception as e:
                    log(f"  ⚠ {e}")

    liste = sorted(articles.values(), key=lambda a: (a["date"], a["doi"]), reverse=True)
    sortie = {
        "maj": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "source": "Nature — https://www.nature.com/nature/articles?type=article",
        "themes": [{"id": t["id"], "nom": t["nom"]} for t in THEMES],
        "articles": liste,
        "vus": vus,
        "usage": dict(USAGE, modele_tri=MODELE_TRI if CLE else None, modele_resume=MODELE_RESUME if CLE else None),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "nature.json").write_text(json.dumps(sortie, ensure_ascii=False, indent=1), "utf-8")
    log(f"→ {OUT_DIR / 'nature.json'} : {len(liste)} articles, "
        f"{sum(1 for a in liste if a.get('fiche'))} fiches · Claude : {USAGE['appels']} appels, "
        f"{USAGE['entree']} tokens en entrée, {USAGE['sortie']} en sortie")


# ---------------------------------------------------------------- test hors ligne
def test():
    """Rejoue le script sur les pages de test/ (copiées de nature.com), sans réseau."""
    global get, claude, CLE, OUT_DIR, PREV_JSON, JOURS_PREMIER_PASSAGE, JOURS_ARCHIVE
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
    print("lecture des pages : ok")

    get = faux_get
    sortie = pathlib.Path(tempfile.mkdtemp())
    # 1) sans clé : tri par mots-clés (aucun thème ici → rien publié)
    CLE, OUT_DIR = "", sortie / "a"
    main()
    j = json.loads((OUT_DIR / "nature.json").read_text("utf-8"))
    assert len(j["vus"]) == 3 and j["articles"] == [], j

    # 2) avec une fausse API : 1 payant + 1 libre dans un thème, fiche complète
    def faux_claude(modele, systeme, message, max_tokens):
        USAGE["appels"] += 1
        if "Thèmes :" in message:
            return '{"1": ["climat"], "2": ["sport"], "3": []}'
        return json.dumps({"titre_fr": "Titre traduit", "en_une_phrase": "Une phrase.", "resume": "Résumé.",
                           "chiffres_cles": [{"valeur": "×3", "sens": "plus de reprises"}],
                           "schema": [{"etape": "Question", "texte": "a"}, {"etape": "Résultat", "texte": "b"}],
                           "figure": {"numero": 2, "legende_fr": "Légende."}, "themes": ["sport", "inconnu"]})
    claude, CLE, OUT_DIR, PREV_JSON = faux_claude, "test", sortie / "b", ""
    main()
    j = json.loads((OUT_DIR / "nature.json").read_text("utf-8"))
    a = {x["doi"][-5:]: x for x in j["articles"]}
    assert a["391-w"]["acces"] == "payant" and "resume" not in a["391-w"]
    f = a["204-0"]
    assert f["fiche"] and f["figure"]["numero"] == 2 and f["figure"]["legende_fr"] == "Légende."
    assert f["figure"]["credit"] == "Fig. 2 · Polo Rivera et al., Nature (2026) · CC BY 4.0", f["figure"]["credit"]
    assert f["themes"] == ["sport"] and f["titre_fr"] == "Titre traduit"

    # 3) passage suivant à partir de la publication précédente : rien de nouveau, rien de perdu
    PREV_JSON, OUT_DIR = str(OUT_DIR / "nature.json"), sortie / "c"
    n = USAGE["appels"]
    main()
    j2 = json.loads((OUT_DIR / "nature.json").read_text("utf-8"))
    assert len(j2["articles"]) == 2 and USAGE["appels"] == n, "aucun nouvel appel attendu"
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
