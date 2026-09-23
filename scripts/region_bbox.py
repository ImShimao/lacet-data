"""L'emprise à extraire pour une carte régionale qui déborde de sa limite.

    python scripts/region_bbox.py alsace.poly 6000   →  w,s,e,n

La carte livrée déborde du territoire de quelques kilomètres — sans quoi elle
s'arrête net à la limite administrative, et quelqu'un qui habite à deux
kilomètres de cette limite n'a plus de rues à l'ouest. Ce débord doit être aussi
complet que le reste : les extraits des régions voisines sont donc coupés à une
même emprise, celle qu'imprime ce script, et `region_tiles.py` ne garde que les
tuiles qui y tiennent entièrement.

Deux tuiles de sécurité en plus de la marge : une tuile à cheval sur le bord de
l'emprise serait tronquée, et c'est exactement ce qu'on cherche à éviter.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

TILE_DEG = 0.05
SAFETY_TILES = 2


def main() -> None:
    poly = Path(sys.argv[1])
    margin_m = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0

    lons, lats = [], []
    for ligne in poly.read_text(encoding="utf-8").splitlines():
        morceaux = ligne.split()
        if len(morceaux) >= 2:
            try:
                lon, lat = float(morceaux[0]), float(morceaux[1])
            except ValueError:
                continue
            lons.append(lon)
            lats.append(lat)
    if not lons:
        sys.exit(f"{poly} : aucune coordonnée lisible.")

    milieu = (min(lats) + max(lats)) / 2
    dlat = margin_m / 110_540.0 + SAFETY_TILES * TILE_DEG
    dlon = margin_m / (math.cos(math.radians(milieu)) * 111_320.0) + SAFETY_TILES * TILE_DEG
    # Aligné sur la grille des tuiles : une emprise qui coupe une tuile en deux
    # la rendrait inutilisable, et l'arrondi ne coûte que quelques kilomètres.
    w = math.floor((min(lons) - dlon) / TILE_DEG) * TILE_DEG
    e = math.ceil((max(lons) + dlon) / TILE_DEG) * TILE_DEG
    s = math.floor((min(lats) - dlat) / TILE_DEG) * TILE_DEG
    n = math.ceil((max(lats) + dlat) / TILE_DEG) * TILE_DEG
    print(f"{w:.4f},{s:.4f},{e:.4f},{n:.4f}")


if __name__ == "__main__":
    main()
