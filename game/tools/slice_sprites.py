"""Cut the authored sprite sheet into one keyed PNG per game entity.

The art arrives as a single 4x4 sheet on a grey backdrop. This turns it into the
sixteen transparent sprites the renderer loads, and it is committed rather than
run once by hand so that re-exporting the sheet is a one-command job instead of
an archaeology exercise.

Two things here are less obvious than they look.

**The background is not one colour.** It samples between 107 and 124 across the
sheet, so a global colour key would either leave a grey halo or eat the grey
parts of the art - the Breaker is almost entirely mid-grey. Instead the
background is found by FLOOD FILLING inward from each cell's border: a pixel is
background only if it is grey-ish *and* connected to the edge. The Breaker's
grey is enclosed by its own outline, so it is never reached.

**Alpha is soft, and the edge colour is un-mixed.** Every pixel on the boundary
is a blend of subject and backdrop; keying it to binary alpha leaves a grey
fringe that reads as a dirty outline once the sprite sits on a dark board. So
alpha ramps with distance-from-backdrop, and the colour is then solved back out
of the blend (C = a*F + (1-a)*B, so F = (C - (1-a)B) / a). That is what keeps
the Mender's green arcs and the Suppressor's lightning as glow rather than as a
grey smear.

    python3 game/tools/slice_sprites.py <sheet.png>
"""
import sys, os
from collections import deque
from PIL import Image

# Row-major, matching the authored sheet. Names are the ids the game already
# uses - blueprint ids for the turrets, enemy ids for the drones - so the
# renderer can look a sprite up by the id it already holds.
LAYOUT = [
    ["ballistic", "cannon", "railgun", "rig"],
    ["suppressor", "swarm", "walker", "lance"],
    ["heavy", "harbinger", "mender", "jammer"],
    ["brood", "breaker", "borer", "titan"],
]
TURRETS = {"ballistic", "cannon", "railgun", "rig", "suppressor"}

# Distance from the local backdrop at which a pixel becomes fully opaque, and
# below which it is fully transparent. Generous at the bottom because the
# backdrop is noisy; tight at the top so glow survives.
ALPHA_LOW = 12.0
ALPHA_HIGH = 46.0
## How far a pixel may sit from the backdrop and still be walked THROUGH by the
## flood fill. Higher than ALPHA_LOW on purpose: the fill has to be able to
## cross a faint glow halo to reach the backdrop trapped behind it.
FILL_TOLERANCE = 26.0
## Smallest sealed-off backdrop pocket worth clearing. Below this a near-grey
## region is a highlight in the art rather than trapped backdrop.
POCKET_MIN_PIXELS = 30


def backdrop_of(cell):
    """The cell's own backdrop colour, taken from its corners."""
    w, h = cell.size
    picks = [cell.getpixel(p) for p in
             [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
              (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]]
    # Median per channel: a corner the art happens to reach cannot drag it.
    return tuple(sorted(p[c] for p in picks)[len(picks) // 2] for c in range(3))


def distance(pixel, base):
    return max(abs(pixel[c] - base[c]) for c in range(3))


def key_cell(cell):
    """Flood fill the backdrop from the border, then soften and un-mix the edge."""
    cell = cell.convert("RGB")
    w, h = cell.size
    base = backdrop_of(cell)
    px = cell.load()

    outside = bytearray(w * h)
    queue = deque()
    for x in range(w):
        for y in (0, h - 1):
            queue.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        if x < 0 or y < 0 or x >= w or y >= h or outside[y * w + x]:
            continue
        if distance(px[x, y], base) > FILL_TOLERANCE:
            continue
        outside[y * w + x] = 1
        queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

    # Pockets of backdrop the border fill could not reach.
    #
    # The Mender's glow arcs loop right round to touch themselves, sealing the
    # backdrop inside them; connectivity alone leaves those pockets opaque grey
    # and the sprite ships with holes full of the wrong colour. Anything within
    # ALPHA_LOW of the backdrop is visually indistinguishable from it, so it can
    # be cleared regardless of connectivity - measured, the greyest subject in
    # the set is the Breaker and only 2.2% of its body sits that close.
    #
    # Bounded by region size, because a lone near-grey pixel inside the art is
    # a highlight, not a pocket, and clearing those would punch speckle holes.
    seen = bytearray(w * h)
    for sy in range(h):
        for sx in range(w):
            index = sy * w + sx
            if seen[index] or outside[index]:
                continue
            if distance(px[sx, sy], base) >= ALPHA_LOW:
                continue
            region = []
            stack = [(sx, sy)]
            seen[index] = 1
            while stack:
                x, y = stack.pop()
                region.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if nx < 0 or ny < 0 or nx >= w or ny >= h:
                        continue
                    at = ny * w + nx
                    if seen[at] or outside[at]:
                        continue
                    if distance(px[nx, ny], base) >= ALPHA_LOW:
                        continue
                    seen[at] = 1
                    stack.append((nx, ny))
            if len(region) >= POCKET_MIN_PIXELS:
                for x, y in region:
                    outside[y * w + x] = 1

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    op = out.load()
    for y in range(h):
        for x in range(w):
            colour = px[x, y]
            if not outside[y * w + x]:
                op[x, y] = (colour[0], colour[1], colour[2], 255)
                continue
            span = ALPHA_HIGH - ALPHA_LOW
            alpha = (distance(colour, base) - ALPHA_LOW) / span
            alpha = 0.0 if alpha < 0.0 else (1.0 if alpha > 1.0 else alpha)
            if alpha <= 0.0:
                continue
            # Solve the subject colour back out of the blend, so the backdrop
            # stops contributing grey to a partly transparent pixel.
            unmixed = []
            for c in range(3):
                value = (colour[c] - (1.0 - alpha) * base[c]) / alpha
                unmixed.append(int(max(0.0, min(255.0, value))))
            op[x, y] = (unmixed[0], unmixed[1], unmixed[2], int(alpha * 255.0))
    return out


def trim_square(image, pad_ratio=0.04):
    """Crop to the art, then pad back out to a square so nothing is distorted."""
    box = image.getbbox()
    if box is None:
        return image
    cropped = image.crop(box)
    side = max(cropped.size)
    side += int(side * pad_ratio) * 2
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(cropped, ((side - cropped.size[0]) // 2,
                           (side - cropped.size[1]) // 2))
    return square


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sheet = Image.open(sys.argv[1]).convert("RGB")
    rows = len(LAYOUT)
    cols = len(LAYOUT[0])
    cell_w = sheet.size[0] // cols
    cell_h = sheet.size[1] // rows
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "art")
    os.makedirs(os.path.join(root, "turrets"), exist_ok=True)
    os.makedirs(os.path.join(root, "drones"), exist_ok=True)

    print("%-12s %-9s %s" % ("sprite", "kind", "size"))
    for r, row in enumerate(LAYOUT):
        for c, name in enumerate(row):
            cell = sheet.crop((c * cell_w, r * cell_h,
                               (c + 1) * cell_w, (r + 1) * cell_h))
            keyed = trim_square(key_cell(cell))
            kind = "turrets" if name in TURRETS else "drones"
            path = os.path.normpath(os.path.join(root, kind, "%s.png" % name))
            keyed.save(path)
            print("%-12s %-9s %dx%d" % (name, kind, keyed.size[0], keyed.size[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
