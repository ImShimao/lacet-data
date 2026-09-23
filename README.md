# Les rues de Lacet

Les rues de France, découpées une fois pour toutes, pour que l'app
[Lacet](https://github.com/ImShimao/lacet) trouve une boucle en quelques
secondes partout, et pas seulement chez elle.

## Pourquoi ce dépôt

Lacet calcule ses itinéraires **sur le téléphone**, sans serveur. Il lui faut
donc les rues du coin où l'on se trouve. Jusqu'ici elle les demandait à
[Overpass](https://overpass-api.de), un moteur de requêtes public et gratuit qui
*calcule* l'extraction à la volée : cinq à quarante secondes quand il va bien,
rien du tout quand il sature — et dans le pire cas, vingt minutes d'écran de
chargement, mesurées.

Or les rues ne changent pas d'une heure à l'autre. Elles peuvent être découpées
une fois par mois et servies comme de simples fichiers. C'est ce que contient ce
dépôt.

## Ce qu'il y a dedans

    packs/index.json     la liste des blocs disponibles
    packs/<x>_<y>.lct    un bloc d'un demi-degré, une centaine de tuiles

Chaque archive commence par un répertoire — où est chaque tuile, ce qu'elle
pèse — puis les tuiles compressées, rangées ligne par ligne. L'app lit le
répertoire une fois, puis demande **seulement les tuiles qu'elle veut**, par
plages d'octets (`Range`), que GitHub et jsDelivr servent tous deux. Une sortie
de 10 km dans une ville inconnue, ce sont trois ou quatre requêtes et trois
mégaoctets — environ une seconde.

Le format est décrit en tête de [`scripts/pack_tiles.py`](scripts/pack_tiles.py),
et lu par `src/engine/archive.js` dans l'app.

## Comment c'est fabriqué

[`.github/workflows/rues.yml`](.github/workflows/rues.yml), le 2 de chaque mois :

1. l'extrait OpenStreetMap de la France est téléchargé chez
   [Geofabrik](https://download.geofabrik.de), avec ceux des régions
   frontalières — sans quoi les tuiles du bord n'auraient que la moitié de leurs
   rues ;
2. le tout est coupé à une même emprise, fusionné, puis découpé en tuiles de
   0,05° par [`scripts/region_tiles.py`](scripts/region_tiles.py) — mêmes
   filtres, mêmes étiquettes et mêmes géométries que ce qu'Overpass aurait rendu,
   vérifié voie par voie ;
3. les tuiles sont rangées en blocs par [`scripts/pack_tiles.py`](scripts/pack_tiles.py).

Une tuile n'est publiée que si la source la couvre **entièrement** : une tuile à
cheval sur le bord des données mentirait sur ce qu'elle contient, et il vaut
mieux la laisser à Overpass.

## Données

Rues et étiquettes © les contributeurs d'[OpenStreetMap](https://www.openstreetmap.org/copyright),
sous [ODbL](https://opendatacommons.org/licenses/odbl/). Les fichiers de ce
dépôt en sont une base de données dérivée : ils sont distribués sous la même
licence. Les scripts sont fournis tels quels, et vivent aussi dans le dépôt de
l'app.
