# Veille — assistant de vie

Dépôt des agents automatiques de la veille. Chaque bloc a son dossier, son workflow
GitHub Actions et publie ses résultats dans la branche **`publication`**, que lira plus tard
l'application iPhone.

## Blocs

| Bloc | Dossier | Workflow | Fréquence | Sortie (branche `publication`) | État |
|---|---|---|---|---|---|
| Météo – cartes Z500 J+0 → J+6 | `meteo/cartes_j6/` | `meteo-cartes-j6.yml` | chaque jour, 9h30 | `meteo/cartes-j6/` | ✅ prêt |
| App – météo AROME (demain, sport, déplacement) | `app/meteo/` | `app-publication.yml` | à chaque modification | `app/meteo/` | 🧪 test |
| Climat – publications | — | — | — | — | à venir |
| Bourse – résultats | — | — | — | — | à venir |
| Sondages présidentielle | — | — | — | — | à venir |
| IA – actus | — | — | — | — | à venir |
| Photo – actus | — | — | — | — | à venir |

## Météo – cartes Z500 J+6

Chaque matin, le script télécharge les **données brutes** de quatre modèles — GFS et ECMWF IFS
(physiques), AIGFS et ECMWF AIFS (leurs versions IA) — (géopotentiel
500 hPa et pression au niveau de la mer) de **J+0 à J+6 par pas de 6 h**, et **redessine les
cartes** sur l'Europe et l'Atlantique (projection de Lambert), avec le nom du modèle en titre.

Sorties (branche `publication`, dossier `meteo/cartes-j6/`) :
- `<modele>_<HHH>.png` : 25 échéances × 4 modèles = 100 cartes (HHH = heures depuis 00 UTC du jour) ;
- `<modele>.png` et `grille.png` : J+6 seule, et les 4 modèles en grille — GFS | IFS en haut,
  AIGFS | AIFS en bas (pour une notification) ;
- `manifest.json` : runs utilisés, échéances, statut de chaque modèle (lu par la page et l'app) ;
- `index.html` : page avec une **jauge J+0 → J+6** (glisser, flèches ←/→, lecture automatique).

Ordre de grandeur par exécution : 0 token Claude, environ 120 Mo téléchargés (seuls les deux
champs utiles de chaque échéance), 2 à 4 minutes, environ 10 Mo d'images publiées.

| Modèle | Type | Fournisseur | Licence | Mention |
|---|---|---|---|---|
| GFS | physique | NOAA / NCEP (bucket AWS) | Domaine public | recommandée |
| IFS | physique | ECMWF Open Data | CC BY 4.0 | « Source : ECMWF » |
| AIGFS | IA | NOAA / NCEP (serveur NOMADS) | Domaine public | recommandée |
| AIFS | IA | ECMWF Open Data | CC BY 4.0 | « Source : ECMWF » |

Fonds de carte : Natural Earth (domaine public). La source est imprimée en bas de chaque carte.

- À une même position de la jauge, les 4 cartes ont la même date de validité. Si un modèle n'a
  pas encore publié son run de 00 UTC jusqu'à J+6, le script prend le run de 12 UTC de la veille
  (décalé de 12 h).
- Réglages en haut de `meteo/cartes_j6/build_maps.py` : `FRAME_POINTS` (lieux à inclure dans
  le cadre), `LEVEL_HPA`, `Z_LEVELS` / `Z_TICKS` / `CMAP` (palette), `MSL_STEP` (isobares).
- Test du rendu sans réseau : `python meteo/cartes_j6/build_maps.py --demo`.

## App – météo AROME (prototype)

Page `app/meteo/index.html`, qui préfigure l'application iPhone. Elle interroge directement le
dernier run **AROME** (Météo-France, 2,5 km) pour un point, via l'API gratuite d'Open-Meteo
(usage non commercial, mention « Weather data by Open-Meteo.com »). Aucun serveur, aucun token.

- **Demain** : résumé en une phrase (temps, températures matin / après-midi, ressenti, humidex,
  orages et chute de température, créneaux de pluie, vent fort), calculé par des règles.
- **Option sport** : préférences (type de temps, température, ressenti), barre de 6h à 22h du
  rouge au vert, meilleur créneau et alertes (orage, humidex ≥ 40, froid ressenti intense).
- **Déplacement** : ville et heure de départ, ville et heure d'arrivée, météo aux deux bouts.

Limites : domaine AROME (France et pays voisins), échéance d'environ 2 jours.
Adresse une fois GitHub Pages activé : `https://<compte>.github.io/veille/app/meteo/`.

## Mise en place

1. Crée un dépôt GitHub (par exemple `veille`).
2. Dépose-y tout le contenu de ce dossier en gardant l'arborescence, y compris le dossier
   caché `.github/workflows/`.
3. Onglet **Actions** → « Météo – cartes Z500 J+6 » → **Run workflow**.
4. Après quelques minutes, les cartes et la page `index.html` sont dans la branche `publication`,
   dossier `meteo/cartes-j6/`. Pour ouvrir la page dans un navigateur, active GitHub Pages sur la
   branche `publication` (dépôt public, ou abonnement GitHub pour un dépôt privé).

En cas d'erreur : copie le journal de l'étape « Construire les cartes » et envoie-le à Claude
(pour plus de détails, ajoute `DEBUG: "1"` dans la section `env` de cette étape).
