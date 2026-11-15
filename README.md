# Veille — assistant de vie

Dépôt des agents automatiques de la veille. Chaque bloc a son dossier, son workflow
GitHub Actions et publie ses résultats dans la branche **`publication`**, que lira plus tard
l'application iPhone.

## Blocs

| Bloc | Dossier | Workflow | Fréquence | Sortie (branche `publication`) | État |
|---|---|---|---|---|---|
| Météo – cartes Z500 et précipitations J+0 → J+6 | `meteo/cartes_j6/` | `meteo-cartes-j6.yml` | chaque jour, 9h30 | `meteo/cartes-j6/` | ✅ prêt |
| App – météo AROME (demain, sport, déplacement) | `app/meteo/` | `app-publication.yml` | à chaque modification | `app/meteo/` | 🧪 test |
| App – onglet Climat (menus Température, Terre / mer, Banquise, Mers, France) | `app/climat/` | `app-publication.yml` | à chaque modification | `app/climat/` | 🧪 test |
| Climat – température mondiale mensuelle depuis 1850 | `climat/temperature/` | `climat-mensuel.yml` | le 16 de chaque mois | `climat/temperature/` | 🧪 test |
| Climat – banquise arctique et antarctique depuis 1978 | `climat/banquise/` | `climat-banquise.yml` | chaque jour | `climat/banquise/` | 🧪 test |
| Climat – température terres / océans depuis 1850 | `climat/temperature/` | `climat-mensuel.yml` | le 16 de chaque mois | `climat/temperature/` | 🧪 test |
| Climat – température des mers par zone depuis 1880 | `climat/mers/` | `climat-mensuel.yml` | le 16 de chaque mois | `climat/mers/` | 🧪 test |
| Climat – France (température mensuelle, cartes mensuelles de l'année) | `climat/france/` | `climat-mensuel.yml` | le 16 de chaque mois | `climat/france/` | 🧪 test |
| Science – Nature (fiches en français des articles du jour, par thème) | `science/nature/` | `science-nature.yml` | chaque jour, 19h17 UTC | `science/nature/` | 🧪 test |
| App – onglet Nature (thèmes, parutions, fiches, sauvegarde) | `app/nature/` | `app-publication.yml` | à chaque modification | `app/nature/` | 🧪 test |
| Bourse – résultats | — | — | — | — | à venir |
| Sondages présidentielle | — | — | — | — | à venir |
| IA – actus | — | — | — | — | à venir |
| Photo – actus | — | — | — | — | à venir |

## Météo – cartes Z500 et précipitations J+0 → J+6

Chaque matin, le script télécharge les **données brutes** de quatre modèles — GFS et ECMWF IFS
(physiques), AIGFS et ECMWF AIFS (leurs versions IA) — (géopotentiel
500 hPa et pression au niveau de la mer) de **J+0 à J+6 par pas de 6 h**, et **redessine les
cartes** sur l'Europe et l'Atlantique (projection de Lambert), avec le nom du modèle en titre.

Sorties (branche `publication`, dossier `meteo/cartes-j6/`) :
- `<modele>_<HHH>.png` : 25 échéances × 4 modèles = 100 cartes (HHH = heures depuis 00 UTC du jour) ;
- `<modele>.png` et `grille.png` : J+6 seule, et les 4 modèles en grille — GFS | IFS en haut,
  AIGFS | AIFS en bas (pour une notification) ;
- `manifest.json` : runs utilisés, échéances, statut de chaque modèle (lu par la page et l'app) ;
- `index.html` : page avec une **jauge J+0 → J+6** (glisser, flèches ←/→, lecture automatique)
  et une bascule **Z500 & pression / Précipitations** ;
- `precip/<modele>_<HHH>.png` et `precip/manifest.json` : **précipitations sur 6 h et leur type**,
  centrées sur la France, de +6 h à +144 h (script `precip.py`, même grille GFS | IFS / AIGFS | AIFS) :
  - types : pluie, orage / averses (part convective dominante et instabilité), neige,
    verglas / grésil — GFS (types catégoriels, CAPE), IFS (`ptype`, CAPE), AIFS (part de
    neige, part convective) ; l'AIGFS n'affiche le type que si la NOAA le publie ;
  - **grêle** : aucun de ces modèles ne la prévoit ; les hachures sont un **indice de risque**
    estimé (forte instabilité, averses dominantes, cumul important). `SHOW_HAIL = False` pour le retirer ;
  - un échec de ce script n'empêche pas la publication des cartes Z500.

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

## Climat – température mondiale

`climat/temperature/build_temp.py` télécharge **HadCRUT5** (Met Office / CRU, terre + mer,
mensuel depuis janvier 1850, Open Government Licence ; miroir GitHub en secours) et produit,
dans la charte des cartes J+6 :
- `temperature_mensuelle.png` : un point par mois coloré par l'écart (palette Z500), moyenne
  glissante sur 12 mois, record annoté ; échelle de couleurs symétrique, neutre à 0 ;
- `temperature_mensuelle.json` : la série recalculée, pour l'app.

Axe de gauche : écart à **1850-1900** (préindustriel, référence de l'accord de Paris). Axe de
droite : écart à **1991-2020** (normale OMM actuelle). Réglages en haut du script : `REF`, `REF2`,
`LISSAGE_MOIS`, `SEUILS`, `A_LEVELS`. Test hors ligne : `python build_temp.py --csv fichier.csv`.

## Climat – banquise

`climat/banquise/build_banquise.py` télécharge l'étendue quotidienne du **NSIDC Sea Ice Index v4**
(Arctique et Antarctique, depuis novembre 1978) et produit `banquise_arctique.png`,
`banquise_antarctique.png` et `banquise.json`. Écart calculé jour par jour à la climatologie
**1981-2010** (référence du NSIDC), puis moyenné par mois ; la normale 1991-2020 est en pointillés.
Couleurs inversées par rapport à la température : moins de glace = rouge. Réglages en haut du
script : `REF`, `REF2`, `A_MAX`, `JOURS_MIN`.

## Climat – banquise au jour le jour

`climat/banquise/build_banquise_annuel.py` (même source NSIDC) trace l'étendue quotidienne, une
courbe par année de 1979 à l'an dernier (couleur = année), la moyenne 1981-2010, la normale
1991-2020 et l'année en cours en noir : `banquise_annuelle_arctique.png`,
`banquise_annuelle_antarctique.png`.

## Climat – terres et océans

`climat/temperature/build_terre_mer.py` télécharge les séries « land » et « ocean » de
**NOAA Climate at a Glance** pour le Monde et les deux hémisphères, et la série « land_ocean »
pour l'Arctique et l'Antarctique (la NOAA ne les sépare pas), les recalcule en écart à 1850-1900 et
trace les moyennes annuelles (points) et sur 10 ans (traits) : `temperature_terre_mer_<region>.png`
(monde, hn, hs, arctique, antarctique) et `temperature_terre_mer.json`. Les tropiques ne sont pas
publiés par la NOAA.

## Climat – température des mers par zone

`climat/mers/build_sst_zones.py` télécharge **NOAA ERSST v5** (grille 2°, mensuelle) via l'ERDDAP
CoastWatch, fait la moyenne de chaque zone (pondérée par la latitude) et trace l'écart à 1991-2020
depuis 1880 : `sst_monde.png` (série « ocean » de NOAA Climate at a Glance), `sst_med.png`, `sst_nino12.png`, `sst_nino34.png` (avec seuils El Niño / La Niña),
`sst_arctic.png`, et `sst_zones.json`. Zones réglables dans `ZONES` en haut du script.

## Climat – France

`climat/france/build_france.py` télécharge les fichiers annuels **SIM mensuelle** de Météo-France
(analyse Safran-Isba, grille 8 km, depuis août 1958, Licence Ouverte Etalab 2.0), garde les points
situés en France et produit :
- `france_temperature_mensuelle.png` : température moyenne de la France, écart à 1991-2020
  (normale 1961-1990 en pointillés), même gabarit que la température mondiale ;
- `france_carte_temperature_AAAA-MM.png` : anomalie de température, une carte par mois de l'année
  du dernier mois disponible (en janvier : les 12 mois de l'année passée) ;
- `france_carte_precipitations_AAAA-MM.png` : cumul du mois en % de la normale 1991-2020 ;
- `france_carte_temperature.png` / `france_carte_precipitations.png` : copies du dernier mois ;
- `france.json` : séries et liste des cartes disponibles (lue par l'app pour le menu des mois).
Cartes dans le gabarit des cartes J+6 (Lambert, fonds Natural Earth), grille 8 km interpolée à 2 km,
avec les 96 chefs-lieux de département (ceux de la petite couronne en point seul).

## App – onglet Climat

Page `app/climat/index.html` : cinq onglets (Température, Terre / mer, Banquise, Mers, France) et, dans
chacun, un menu de zones. Elle affiche les PNG de `climat/` (chemins relatifs, même arborescence
dans le dépôt et dans la branche `publication`) et retient le dernier choix.

## Science – Nature

`science/nature/build_nature.py` lit chaque soir la liste des articles de recherche de **Nature**
(`https://www.nature.com/nature/articles?type=article`) et ne traite que les nouveaux :
1. **Tri** : une seule requête courte à Claude classe tous les titres du jour dans les thèmes de
   `science/nature/themes.json` — pour l'instant un seul thème, **Climat** : réchauffement climatique et
   AMOC (à compléter librement). Sans IA disponible : tri par les mots-clés du même fichier.
2. **Accès libre seulement** : un article n'a de fiche que s'il porte l'étiquette « Open Access » et une
   licence Creative Commons sur sa page. Les articles payants d'un thème sont listés sans résumé (titre + lien).
3. **Fiche** (Claude, d'après le texte de l'article hors méthodes et références) : titre original + titre
   traduit, l'essentiel en une phrase, le graphique le plus parlant avec sa légende en français et son crédit
   (licence CC), jusqu'à 3 chiffres clés, un résumé de 4 à 6 phrases, un schéma de conclusion en 3 à 5
   étapes, puis le lien vers l'article.
4. Sortie `nature.json` (30 derniers jours). La publication de la veille sert de mémoire : un article
   n'est jamais lu ni résumé deux fois.

Coût : ≈ 10 000 tokens par fiche, avec Claude Haiku par défaut (`MODELE_RESUME` dans le workflow pour
passer à Sonnet), plafond de 12 fiches par soir (`MAX_FICHES`). Le compteur exact est dans
`nature.json` → `usage`. Réglages en haut du script : `JOURS_ARCHIVE`, `MAX_FICHES`, `TEXTE_MAX`.
Test hors ligne (pages Nature enregistrées dans `test/`) : `python science/nature/build_nature.py --test`.

**Clé API** : dépôt GitHub → Settings → Secrets and variables → Actions → New repository secret,
nom `ANTHROPIC_API_KEY` (clé créée sur console.anthropic.com). Sans clé, le bloc fonctionne quand même
(tri par mots-clés, chapeau et résumé d'origine en anglais).

## App – onglet Nature

Page `app/nature/index.html` : choix du thème (avec le nombre d'articles), choix de la parution (la
dernière par défaut, ou les 30 derniers jours), une fiche par article. Le bouton « ☆ Sauvegarder » garde
la fiche complète et son lien dans le menu « ★ Sauvegardés » (mémoire du navigateur pour ce prototype ;
l'app iPhone aura son propre stockage). Les graphiques sont affichés depuis le site de Springer Nature,
avec leur crédit.

## Mise en place

1. Crée un dépôt GitHub (par exemple `veille`).
2. Dépose-y tout le contenu de ce dossier en gardant l'arborescence, y compris le dossier
   caché `.github/workflows/`.
3. Onglet **Actions** → « Météo – cartes J+0 à J+6 (Z500 et précipitations) » → **Run workflow**.
4. Après quelques minutes, les cartes et la page `index.html` sont dans la branche `publication`,
   dossier `meteo/cartes-j6/`. Pour ouvrir la page dans un navigateur, active GitHub Pages sur la
   branche `publication` (dépôt public, ou abonnement GitHub pour un dépôt privé).

En cas d'erreur : copie le journal de l'étape « Construire les cartes » et envoie-le à Claude
(pour plus de détails, ajoute `DEBUG: "1"` dans la section `env` de cette étape).
