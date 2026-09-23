"""Découpe un extrait OpenStreetMap en tuiles de rues, livrées dans l'app.

    python scripts/region_tiles.py alsace-latest.osm.pbf alsace.poly public/region --name Alsace

La première recherche dans une zone commençait par télécharger ses rues sur
Overpass : un serveur public, gratuit et souvent saturé. Pour la région où l'on
court, ce script prépare ces rues à l'avance, sur un serveur de GitHub, et les
range dans l'app — un fichier JSON par tuile : une recherche y part tout de
suite, même sans réseau.

**Mêmes voies qu'Overpass, à la tuile près.** Le moteur doit recevoir
exactement ce que lui aurait rendu sa requête (`src/engine/overpass.js`, niveau
de détail 0) : mêmes filtres, mêmes étiquettes, mêmes géométries. Les filtres
sont recopiés ici, et un test (`region.test.js`) vérifie qu'ils n'ont pas
divergé. Comparé sur Andorre : 596 voies sur 596 identiques, au bit près.

**Une tuile n'est gardée que si la source la couvre entièrement.** Une tuile à
cheval sur le bord des données n'aurait que la moitié de ses rues. Avec un seul
extrait régional, cela revient à ne garder que les tuiles entièrement dans le
territoire — et la carte s'arrête alors net à la limite administrative, ce qui
laisse sans rues quelqu'un qui habite à deux kilomètres de cette limite. En
donnant au script un extrait plus large (la région **et ses voisines**, coupées
à une emprise commune) et `--margin`, la carte déborde de quelques kilomètres
tout autour, et ce débord est aussi complet que le reste.

**Un format serré.** Coordonnées en entiers de 1e-7 degré, chaque point écrit
comme l'écart au précédent ; même chose pour les numéros de nœuds.

**Trié sur disque.** Une grande région compte des millions de voies : les
garder toutes en mémoire en attendant de les ranger ne passe pas à l'échelle.
Elles sont triées sur disque, par carré d'un degré, puis rangées carré par
carré.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import tempfile
import time
from collections import defaultdict
from pathlib import Path

TILE_DEG = 0.05
EDGE = 1e-9
# Un carré de tri : 20 × 20 tuiles, un degré de côté.
BUCKET = 20

# Recopiés de `src/engine/overpass.js` — voir la docstring.
EXCLUDED_HIGHWAYS = "motorway|motorway_link|trunk|trunk_link|construction|proposed|raceway|bus_guideway|escape"
BUILTUP_LANDUSE = "residential|retail|commercial|industrial|education"
EXCLUDED_SERVICE = "parking_aisle|driveway|drive-through|slipway"

EXCLUDED = set(EXCLUDED_HIGHWAYS.split("|"))
BUILTUP = set(BUILTUP_LANDUSE.split("|"))
SERVICE = set(EXCLUDED_SERVICE.split("|"))


# --- la limite du territoire -------------------------------------------------------


def read_poly(path: Path) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    """Anneaux extérieurs et trous d'un fichier `.poly` de Geofabrik."""
    outer, holes = [], []
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    i = 1  # la première ligne est le nom
    while i < len(lines):
        name = lines[i]
        i += 1
        if name == "END" or not name:
            continue
        ring = []
        while lines[i] != "END":
            lon, lat = lines[i].split()[:2]
            ring.append((float(lon), float(lat)))
            i += 1
        i += 1
        (holes if name.startswith("!") else outer).append(ring)
    return outer, holes


def crossings_at(lat: float, rings) -> list[float]:
    """Les longitudes où la ligne de latitude `lat` coupe les anneaux, triées."""
    xs = []
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            (xi, yi), (xj, yj) = ring[i], ring[j]
            if (yi > lat) != (yj > lat):
                xs.append((xj - xi) * (lat - yi) / (yj - yi) + xi)
            j = i
    xs.sort()
    return xs


def point_to_ring_m(lon: float, lat: float, ring) -> float:
    """Distance du point à la ligne brisée, en mètres (plan local)."""
    kx = math.cos(math.radians(lat)) * 111_320.0
    ky = 110_540.0
    best = float("inf")
    ax, ay = ring[-1]
    ax, ay = (ax - lon) * kx, (ay - lat) * ky
    for bx, by in ring:
        bx, by = (bx - lon) * kx, (by - lat) * ky
        dx, dy = bx - ax, by - ay
        if dx or dy:
            t = max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
        else:
            best = min(best, math.hypot(ax, ay))
        ax, ay = bx, by
    return best


def shipped_tiles(outer, holes, margin_m: float, source) -> set[tuple[int, int]]:
    """Les tuiles à livrer : le territoire, sa marge, et rien d'incomplet.

    `source` est l'emprise (w, s, e, n) réellement couverte par l'extrait donné,
    ou `None` quand il ne couvre que le territoire. Une tuile n'est livrée que si
    elle y tient **entièrement** : c'est la garantie qu'elle contient les mêmes
    rues qu'Overpass aurait rendues.
    """
    tiles = complete_tiles(outer, holes)
    if margin_m <= 0 and source is None:
        return tiles

    if margin_m > 0:
        # Le territoire lui-même, tuile par tuile, puis sa marge : on ne mesure
        # la distance que pour ce qui n'est pas déjà dedans.
        dlat = margin_m / 110_540.0
        vertices = [v for ring in outer + holes for v in ring]
        lons = [v[0] for v in vertices]
        lats = [v[1] for v in vertices]
        dlon = margin_m / (math.cos(math.radians(sum(lats) / len(lats))) * 111_320.0)
        ix0 = math.floor((min(lons) - dlon) / TILE_DEG)
        ix1 = math.floor((max(lons) + dlon) / TILE_DEG)
        iy0 = math.floor((min(lats) - dlat) / TILE_DEG)
        iy1 = math.floor((max(lats) + dlat) / TILE_DEG)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                if (ix, iy) in tiles:
                    continue
                coins = [
                    (ix * TILE_DEG, iy * TILE_DEG),
                    ((ix + 1) * TILE_DEG, iy * TILE_DEG),
                    (ix * TILE_DEG, (iy + 1) * TILE_DEG),
                    ((ix + 1) * TILE_DEG, (iy + 1) * TILE_DEG),
                    ((ix + 0.5) * TILE_DEG, (iy + 0.5) * TILE_DEG),
                ]
                if any(min(point_to_ring_m(x, y, ring) for ring in outer) <= margin_m for x, y in coins):
                    tiles.add((ix, iy))

    if source is not None:
        w, s, e, n = source
        # `EDGE` parce que 142 × 0,05 vaut 7,100000000000001 : sans cette marge,
        # une emprise pourtant alignée sur la grille perdait sa rangée du bord.
        tiles = {
            (ix, iy)
            for ix, iy in tiles
            if ix * TILE_DEG >= w - EDGE
            and (ix + 1) * TILE_DEG <= e + EDGE
            and iy * TILE_DEG >= s - EDGE
            and (iy + 1) * TILE_DEG <= n + EDGE
        }
    return tiles


def complete_tiles(outer, holes) -> set[tuple[int, int]]:
    """Les tuiles entièrement à l'intérieur de la limite.

    Quatre coins dedans ne suffisent pas : une limite en dents de scie peut
    entrer dans la tuile entre deux coins. On exige donc aussi qu'aucun sommet
    de la limite ne tombe dans la tuile.

    Les coins sont testés ligne par ligne : on calcule une fois où chaque ligne
    de latitude coupe la limite, et un coin est dedans si un nombre impair de
    ces coupures le précède. Tester chaque coin contre chaque sommet prenait des
    heures sur la limite de la France.
    """
    rings = outer + holes
    vertices = [v for ring in rings for v in ring]
    lons = [v[0] for v in vertices]
    lats = [v[1] for v in vertices]
    ix0, ix1 = math.floor(min(lons) / TILE_DEG), math.floor(max(lons) / TILE_DEG)
    iy0, iy1 = math.floor(min(lats) / TILE_DEG), math.floor(max(lats) / TILE_DEG)

    with_vertex = {(math.floor(lon / TILE_DEG), math.floor(lat / TILE_DEG)) for lon, lat in vertices}

    # Les coins intérieurs, par ligne de coins.
    inside = {}
    for cy in range(iy0, iy1 + 2):
        xs = crossings_at(cy * TILE_DEG, rings)
        for cx in range(ix0, ix1 + 2):
            inside[(cx, cy)] = bisect.bisect_left(xs, cx * TILE_DEG) % 2 == 1

    tiles = set()
    for ix in range(ix0, ix1 + 1):
        for iy in range(iy0, iy1 + 1):
            if (ix, iy) in with_vertex:
                continue
            if inside[(ix, iy)] and inside[(ix + 1, iy)] and inside[(ix, iy + 1)] and inside[(ix + 1, iy + 1)]:
                tiles.add((ix, iy))
    return tiles


# --- l'encodage ------------------------------------------------------------------------


def delta(values: list[int]) -> list[int]:
    return [values[0]] + [values[k] - values[k - 1] for k in range(1, len(values))]


def delta_pairs(xs: list[int], ys: list[int]) -> list[int]:
    out = [xs[0], ys[0]]
    for k in range(1, len(xs)):
        out.append(xs[k] - xs[k - 1])
        out.append(ys[k] - ys[k - 1])
    return out


def tiles_of(xs: list[int], ys: list[int], keep: set[tuple[int, int]]):
    """Les tuiles que touche l'emprise d'une géométrie — comme `splitIntoTiles`."""
    ix0 = math.floor((min(xs) / 1e7 - EDGE) / TILE_DEG)
    ix1 = math.floor((max(xs) / 1e7 + EDGE) / TILE_DEG)
    iy0 = math.floor((min(ys) / 1e7 - EDGE) / TILE_DEG)
    iy1 = math.floor((max(ys) / 1e7 + EDGE) / TILE_DEG)
    for ix in range(ix0, ix1 + 1):
        for iy in range(iy0, iy1 + 1):
            if (ix, iy) in keep:
                yield ix, iy


# --- premier passage : trier sur disque ----------------------------------------------------


class Buckets:
    """Des fichiers de tri, un par carré d'un degré, ouverts à la demande."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.files = {}

    def write(self, ix: int, iy: int, kind: str, oid: int, record: str) -> None:
        key = (ix // BUCKET, iy // BUCKET)
        handle = self.files.get(key)
        if handle is None:
            handle = self.files[key] = (self.folder / f"{key[0]}_{key[1]}.tsv").open("a", encoding="utf-8")
        handle.write(f"{ix}\t{iy}\t{kind}\t{oid}\t{record}\n")

    def close(self) -> list[Path]:
        for handle in self.files.values():
            handle.close()
        return sorted(self.folder.glob("*.tsv"), key=lambda p: tuple(int(v) for v in p.stem.split("_"))[::-1])


def sort_to_disk(pbf: Path, keep: set[tuple[int, int]], buckets: Buckets, locations: str = "flex_mem") -> int:
    """Parcourt l'extrait et range voies, polygones bâtis et traversées par tuile.

    Les filtres reproduisent ceux de la requête Overpass : une voie `highway`
    hors de la liste exclue, sans `service` exclu ni `area=yes` ; un polygone
    `landuse` bâti ; un nœud `highway=crossing`. Une voie qui porte à la fois
    `highway` et un `landuse` bâti est rangée comme voie — c'est ce que fait
    `fromOverpass` de la même voie revenue par la seconde requête.
    """
    count = 0
    # Le filtre ne laisse remonter en Python que ce qui porte une de ces clés :
    # quelques millions d'objets au lieu de centaines de millions de nœuds. Les
    # positions, elles, sont retenues avant le filtre.
    import osmium  # ici, et non en tête : le choix des tuiles se teste sans lui.

    processor = (
        osmium.FileProcessor(str(pbf))
        .with_locations(locations)
        .with_filter(osmium.filter.KeyFilter("highway", "landuse"))
    )
    for obj in processor:
        tags = {t.k: t.v for t in obj.tags}
        if obj.is_node():
            if tags.get("highway") == "crossing" and obj.location.valid():
                x, y = obj.location.x, obj.location.y
                tile = (math.floor(x / 1e7 / TILE_DEG), math.floor(y / 1e7 / TILE_DEG))
                if tile in keep:
                    record = json.dumps([obj.id, x, y, tags], ensure_ascii=False, separators=(",", ":"))
                    buckets.write(tile[0], tile[1], "c", obj.id, record)
            continue
        if not obj.is_way():
            continue

        highway = tags.get("highway")
        road = (
            highway is not None
            and highway not in EXCLUDED
            and tags.get("service") not in SERVICE
            and tags.get("area") != "yes"
        )
        built = tags.get("landuse") in BUILTUP
        if not road and not built:
            continue

        refs, xs, ys = [], [], []
        for node in obj.nodes:
            if not node.location.valid():
                continue
            refs.append(node.ref)
            xs.append(node.location.x)
            ys.append(node.location.y)
        if not xs:
            continue

        if highway is not None:
            kind = "w"
            record = json.dumps([obj.id, tags, delta(refs), delta_pairs(xs, ys)], ensure_ascii=False, separators=(",", ":"))
        else:
            kind = "l"
            record = json.dumps([obj.id, delta_pairs(xs, ys)], separators=(",", ":"))
        for ix, iy in tiles_of(xs, ys, keep):
            buckets.write(ix, iy, kind, obj.id, record)
            if kind == "w":
                count += 1
    return count


# --- second passage : ranger tuile par tuile -------------------------------------------------


def tiles_in(bucket_file: Path):
    """Les tuiles d'un carré, dans l'ordre (ligne, colonne), texte JSON prêt."""
    grouped = defaultdict(lambda: {"w": [], "l": [], "c": []})
    with bucket_file.open(encoding="utf-8") as handle:
        for line in handle:
            ix, iy, kind, oid, record = line.rstrip("\n").split("\t", 4)
            grouped[(int(iy), int(ix))][kind].append((int(oid), record))
    for (iy, ix) in sorted(grouped):
        parts = grouped[(iy, ix)]
        if not parts["w"]:
            continue  # une tuile sans rue — lac, forêt sans chemin : Overpass dira pareil
        body = {k: ",".join(r for _, r in sorted(v)) for k, v in parts.items()}
        yield ix, iy, f'{{"w":[{body["w"]}],"l":[{body["l"]}],"c":[{body["c"]}]}}'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pbf", type=Path)
    parser.add_argument("poly", type=Path)
    parser.add_argument("out", type=Path, help="dossier de sortie")
    parser.add_argument("--name", default="Région")
    # La position de chaque nœud, le temps de reconstituer les voies. En mémoire
    # pour une région ; sur disque pour plus grand (« sparse_file_array,chemin »).
    parser.add_argument("--locations", default="flex_mem", help="index des positions de nœuds (pyosmium)")
    parser.add_argument(
        "--margin",
        type=float,
        default=0.0,
        help="mètres de débord autour du territoire (exige un extrait qui les couvre)",
    )
    parser.add_argument(
        "--source-bbox",
        default=None,
        help="emprise réellement couverte par l'extrait, « w,s,e,n » — au format d'osmium extract",
    )
    args = parser.parse_args()

    started = time.time()
    outer, holes = read_poly(args.poly)
    source = tuple(float(v) for v in args.source_bbox.split(",")) if args.source_bbox else None
    keep = shipped_tiles(outer, holes, args.margin, source)
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as scratch:
        buckets = Buckets(Path(scratch))
        ways = sort_to_disk(args.pbf, keep, buckets, args.locations)

        written = []
        total = 0
        for bucket_file in buckets.close():
            for ix, iy, text in tiles_in(bucket_file):
                key = f"{ix}_{iy}"
                data = text.encode("utf-8")
                (args.out / f"{key}.json").write_bytes(data)
                total += len(data)
                written.append(key)

    index = {"v": 3, "name": args.name, "builtAt": int(time.time() * 1000), "tileDeg": TILE_DEG, "tiles": written}
    if args.margin:
        index["marginM"] = args.margin
    (args.out / "index.json").write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    print(f"{args.name} : {len(written)} tuiles, {total / 1e6:.1f} Mo, {ways} voies, en {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
