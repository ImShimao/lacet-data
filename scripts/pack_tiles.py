"""Range des tuiles de rues en archives, prêtes à être servies à l'octet près.

    python scripts/pack_tiles.py region packs --name France

Une recherche dans un coin jamais visité demandait ses rues à Overpass : un
moteur de requêtes public et gratuit, qui *calcule* l'extraction à la volée —
cinq à quarante secondes quand il va bien, rien du tout quand il va mal. Les
rues, elles, ne changent pas d'une heure à l'autre : elles peuvent être
découpées une fois pour toutes et servies comme des fichiers.

**Une archive par bloc d'un demi-degré**, soit une centaine de tuiles et cinq à
dix mégaoctets. Deux raisons à cette taille : les CDN qui servent les dépôts
GitHub refusent au-delà de vingt mégaoctets, et un bloc trop grand fait
télécharger un répertoire inutilement long.

**On n'en lit que ce dont on a besoin.** L'archive commence par un en-tête et un
répertoire — pour chaque tuile, où elle est et ce qu'elle pèse. L'app lit ce
répertoire (quelques kilooctets), puis demande les tuiles qui l'intéressent par
plages d'octets (`Range`), que GitHub et jsDelivr servent tous deux. Une sortie
de 10 km, ce sont trois ou quatre requêtes et trois mégaoctets.

**Les tuiles sont rangées ligne par ligne**, si bien que les voisines se suivent
dans le fichier : les tuiles d'une même recherche forment deux ou trois suites
contiguës, qu'on demande d'un bloc.

Format (petit-boutiste), identique à `src/engine/archive.js` :

    en-tête, 32 octets
      0   8  « LACETILE »
      8   2  version du format (1)
      10  2  niveau de détail des tuiles
      12  8  côté d'une tuile en degrés (0,05)
      20  4  nombre de tuiles
      24  8  date de fabrication (ms)
    répertoire, 20 octets par tuile, triée par (iy, ix)
      0   4  ix
      4   4  iy
      8   8  position de la tuile dans le fichier
      16  4  longueur
    données : chaque tuile, telle qu'elle est livrée dans l'app, compressée en gzip
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import struct
import time
from pathlib import Path

MAGIC = b"LACETILE"
VERSION = 1
TILE_DEG = 0.05
HEADER = 32
ENTRY = 20
# Le côté d'un bloc, en degrés. Un demi-degré à nos latitudes, c'est environ
# 40 km sur 55, une centaine de tuiles.
BLOCK_DEG = 0.5


def block_of(ix: int, iy: int) -> tuple[int, int]:
    """Le bloc qui contient la tuile — mêmes maths que `blockOf` en JavaScript."""
    per = round(BLOCK_DEG / TILE_DEG)
    return math.floor(ix / per), math.floor(iy / per)


def pack(tiles: dict[tuple[int, int], bytes], level: int, built_at: int) -> bytes:
    """Une archive, ses tuiles triées ligne par ligne."""
    ordered = sorted(tiles.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    header = bytearray(HEADER)
    header[0:8] = MAGIC
    struct.pack_into("<HHdIQ", header, 8, VERSION, level, TILE_DEG, len(ordered), built_at)

    directory = bytearray()
    body = bytearray()
    offset = HEADER + ENTRY * len(ordered)
    for (ix, iy), raw in ordered:
        blob = gzip.compress(raw, 6, mtime=0)
        directory += struct.pack("<iiQI", ix, iy, offset, len(blob))
        body += blob
        offset += len(blob)
    return bytes(header + directory + body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tiles", type=Path, help="dossier de tuiles (sortie de region_tiles.py)")
    parser.add_argument("out", type=Path, help="dossier des archives")
    parser.add_argument("--name", default="Région")
    parser.add_argument("--level", type=int, default=0)
    args = parser.parse_args()

    # L'index de `region_tiles.py` quand il est là ; sinon le dossier lui-même —
    # une grande zone se découpe en plusieurs passes, qui écrivent chacune le
    # sien, et c'est le tas de tuiles qui fait foi.
    index_file = args.tiles / "index.json"
    index = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {}
    keys = index.get("tiles") or sorted(p.stem for p in args.tiles.glob("*_*.json"))
    built_at = int(index.get("builtAt") or time.time() * 1000)

    blocks: dict[tuple[int, int], dict[tuple[int, int], bytes]] = {}
    for key in keys:
        ix, iy = (int(v) for v in key.split("_"))
        blocks.setdefault(block_of(ix, iy), {})[(ix, iy)] = (args.tiles / f"{key}.json").read_bytes()

    args.out.mkdir(parents=True, exist_ok=True)
    listing = []
    total = 0
    for (bx, by), group in sorted(blocks.items()):
        data = pack(group, args.level, built_at)
        (args.out / f"{bx}_{by}.lct").write_bytes(data)
        total += len(data)
        listing.append({"x": bx, "y": by, "tiles": len(group), "bytes": len(data)})

    (args.out / "index.json").write_text(
        json.dumps(
            {
                "v": 1,
                "name": args.name,
                "builtAt": built_at,
                "tileDeg": TILE_DEG,
                "blockDeg": BLOCK_DEG,
                "level": args.level,
                "blocks": listing,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    gros = max((b["bytes"] for b in listing), default=0)
    print(
        f"{args.name} : {len(listing)} blocs, {sum(b['tiles'] for b in listing)} tuiles, "
        f"{total / 1e6:.1f} Mo, le plus gros bloc {gros / 1e6:.1f} Mo"
    )


if __name__ == "__main__":
    main()
