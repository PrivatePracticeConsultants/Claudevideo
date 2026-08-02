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

    python3 game/tools/slice_sprites.py <sheet.png>                 # the 4x4 entity sheet
    python3 game/tools/slice_sprites.py <sheet.png> --labels        # ...with caption text baked in
    python3 game/tools/slice_sprites.py <sheet.png> --projectiles   # the 2x2 projectile sheet
    python3 game/tools/slice_sprites.py <sheet.png> --turret-pairs  # bare bases + detached guns
    python3 game/tools/slice_sprites.py <image.png> --single <name> # one image -> one sprite
"""
import sys, os, math
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

## The 2x2 projectile sheet, row-major. Named by the art each tracer uses rather
## than by damage type, because the Suppressor and the Railgun are both Energy
## and look nothing alike in flight.
PROJECTILE_LAYOUT = [
    ["proj_kinetic", "proj_explosive"],
    ["proj_energy", "proj_arc"],
]

## Share of each cell's height cut off the BOTTOM before keying, when the sheet
## has caption text baked into it. The captions sit in a band under each
## subject; the flood fill cannot remove them - white text is nothing like the
## backdrop - so they would ship inside the sprite as stray words. Cropping the
## band is cruder than detecting text and better than shipping it.
LABEL_BAND = 0.16

## Fraction of a cell trimmed off every side before anything else looks at it.
##
## The authored sheets draw visible divider LINES between cells. Cropping on the
## exact cell boundary puts those lines on the new image's border, which is
## precisely where backdrop_of() samples - so the backdrop was read as the line
## colour, every real backdrop pixel then sat further than FILL_TOLERANCE from
## it, the flood fill spread nowhere, and fifteen of sixteen sprites shipped
## with an opaque rectangle of sky around them. Trimming a little off first is
## the whole fix.
CELL_INSET = 0.03

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


## --- the detached turret sheet -------------------------------------------------
##
## Cutting a pinned base out of a single turret picture never worked. The art
## was drawn as one object, so any horizontal split line runs through the gun's
## own shadow and its mounting yoke: the base kept a slice of barrel and the
## head lost the collar it should pivot on. The fix was to ask for the parts
## separately - bare bases and detached guns - and mount them here.
##
## That sheet is NOT a regular grid. Bare bases sit in a 3x3 block on the left;
## the five guns are one cell of that block plus a ragged column down the right
## whose four cells are four different heights. So the cells are pixel
## rectangles measured off the delivered image rather than a row/column count.
## Inventing a grid that is not there is exactly how the previous sheet shipped
## fifteen of sixteen sprites with an opaque rectangle of sky around them.
TURRET_PAIR_SHEET = (1408, 768)

## id -> (bare base cell, detached gun cell), in TURRET_PAIR_SHEET pixels.
## Bases are matched to families by what the paint says: grey riveted for the
## workhorse Ballistic, orange for the explosive Cannon, blue for the energy
## Railgun, hazard stripes for the industrial Rig, brass for the Suppressor's
## coil.
TURRET_PAIR_CELLS = {
    "ballistic": ((6, 4, 348, 258), (707, 4, 1049, 258)),
    "cannon": ((707, 516, 1049, 765), (1060, 4, 1402, 305)),
    "railgun": ((707, 266, 1049, 509), (1060, 313, 1402, 455)),
    "rig": ((6, 516, 348, 765), (1060, 462, 1402, 605)),
    "suppressor": ((358, 266, 699, 509), (1060, 612, 1402, 765)),
}

## Where the two parts meet, and how big the gun is.
##
## hub is the centre of the dark socket in the base's top face, as a fraction of
## the KEYED AND TRIMMED base. It sits well above the middle because the drum is
## painted from about sixty degrees up, so most of the picture is the near wall
## below the deck. pivot is the centre of the gun's turntable collar, same
## convention. span is the gun's drawn width as a fraction of the base's - the
## one number here that is taste rather than measurement, chosen so the collar
## covers the drum's raised deck without the barrel dwarfing the mount.
##
## All three fractions were read off a 10% grid drawn over the trimmed art, not
## guessed. Getting hub wrong makes the gun float off its drum; getting pivot
## wrong makes it wobble as it turns.
TURRET_MOUNT = {
    "ballistic": {"hub": (0.50, 0.18), "pivot": (0.50, 0.69), "span": 0.55},
    "cannon": {"hub": (0.50, 0.31), "pivot": (0.50, 0.50), "span": 0.55},
    "railgun": {"hub": (0.49, 0.25), "pivot": (0.25, 0.46), "span": 1.05},
    "rig": {"hub": (0.50, 0.30), "pivot": (0.13, 0.70), "span": 0.85},
    "suppressor": {"hub": (0.50, 0.28), "pivot": (0.18, 0.50), "span": 0.90},
}

## Side of the square canvas every mounted part is written onto, and the slack
## left around the furthest-reaching corner so a spinning gun never clips its
## own edge.
MOUNT_CANVAS = 512
MOUNT_PAD = 0.03


def _keyed_part(sheet, rect, scale):
    """One cell of the detached sheet, keyed and cropped to its own artwork."""
    box = tuple(int(round(rect[i] * scale[i % 2])) for i in range(4))
    keyed = key_cell(sheet.crop(box))
    bounds = keyed.getbbox()
    return keyed if bounds is None else keyed.crop(bounds)


def _mount_reach(parts):
    """Half-width of the canvas both parts must share, in base-widths.

    ONE canvas for all five families, not one each. The canvas is what the
    renderer draws at a single span, so a family with a roomier canvas would
    quietly draw a smaller drum than its neighbour - five turrets at five sizes
    from a constant that reads as if it set one. The Ballistic's gatling is the
    family that decides it: the barrels reach further past their collar than
    anything else on the sheet.
    """
    reach = 0.0
    for name, (base, gun) in parts.items():
        spec = TURRET_MOUNT[name]
        hub_x, hub_y = spec["hub"]
        base_h = base.size[1] / float(base.size[0])
        reach = max(reach, hub_x, 1.0 - hub_x, hub_y * base_h, (1.0 - hub_y) * base_h)
        span = spec["span"]
        gun_h = span * gun.size[1] / float(gun.size[0])
        pivot_x, pivot_y = spec["pivot"]
        # A rotating quad sweeps a circle through its furthest CORNER, so the
        # corner distances are what has to fit - not the four edge distances.
        for dx in (pivot_x * span, (1.0 - pivot_x) * span):
            for dy in (pivot_y * gun_h, (1.0 - pivot_y) * gun_h):
                reach = max(reach, math.hypot(dx, dy))
    return reach * (1.0 + MOUNT_PAD)


def _mount_layer(part, anchor, width_px):
    """One part on an empty canvas, its anchor point at the canvas centre.

    A quad turns about its own centre, so putting the pivot there is what makes
    "aim the gun" a plain basis rotation with no per-frame offset arithmetic.
    The base gets the same treatment with its hub as the anchor, which is what
    keeps the gun sitting in the socket while only the gun moves.
    """
    height_px = max(1, int(round(width_px * part.size[1] / float(part.size[0]))))
    scaled = part.resize((max(1, width_px), height_px), Image.LANCZOS)
    layer = Image.new("RGBA", (MOUNT_CANVAS, MOUNT_CANVAS), (0, 0, 0, 0))
    centre = MOUNT_CANVAS // 2
    layer.paste(scaled, (centre - int(round(anchor[0] * scaled.size[0])),
                         centre - int(round(anchor[1] * scaled.size[1]))))
    return layer


def mount_turrets(sheet, root):
    """The detached sheet -> a pinned base, a turning gun, and a flat composite."""
    scale = (sheet.size[0] / float(TURRET_PAIR_SHEET[0]),
             sheet.size[1] / float(TURRET_PAIR_SHEET[1]))
    parts = {}
    for name, (base_rect, gun_rect) in TURRET_PAIR_CELLS.items():
        parts[name] = (_keyed_part(sheet, base_rect, scale),
                       _keyed_part(sheet, gun_rect, scale))

    half = _mount_reach(parts)
    per_base_width = MOUNT_CANVAS / (2.0 * half)
    folder = os.path.join(root, "turrets")
    os.makedirs(folder, exist_ok=True)

    print("%-16s %-12s %s" % ("sprite", "kind", "size"))
    for name in sorted(parts):
        base, gun = parts[name]
        spec = TURRET_MOUNT[name]
        base_layer = _mount_layer(base, spec["hub"], int(round(per_base_width)))
        gun_layer = _mount_layer(gun, spec["pivot"],
                                 int(round(spec["span"] * per_base_width)))
        written = (("%s_base" % name, base_layer), ("%s_head" % name, gun_layer),
                   # The flat composite is the fallback the renderer falls back
                   # to when split art is missing, and it is written from the
                   # SAME canvas so a family cannot end up drawn at two sizes.
                   (name, Image.alpha_composite(base_layer, gun_layer)))
        for out_name, image in written:
            image.save(os.path.normpath(os.path.join(folder, "%s.png" % out_name)))
            print("%-16s %-12s %dx%d"
                  % (out_name, "turrets", image.size[0], image.size[1]))
    print("canvas %d px = %.2f base widths; base drawn at %.0f px"
          % (MOUNT_CANVAS, 2.0 * half, per_base_width))


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


def slice_sheet(sheet, layout, root, labelled):
    rows = len(layout)
    cols = len(layout[0])
    cell_w = sheet.size[0] // cols
    cell_h = sheet.size[1] // rows
    print("%-16s %-12s %s" % ("sprite", "kind", "size"))
    for r, row in enumerate(layout):
        for c, name in enumerate(row):
            inset_x = int(cell_w * CELL_INSET)
            inset_y = int(cell_h * CELL_INSET)
            bottom = (r + 1) * cell_h - inset_y
            if labelled:
                bottom -= int(cell_h * LABEL_BAND)
            cell = sheet.crop((c * cell_w + inset_x, r * cell_h + inset_y,
                               (c + 1) * cell_w - inset_x, bottom))
            keyed = trim_square(key_cell(cell))
            if name in TURRETS:
                # The entity sheet's turrets are the old one-piece art. They are
                # still cut so the sheet round-trips, but the game's turrets now
                # come from the detached sheet - see mount_turrets - and writing
                # a one-piece picture over that composite would leave a base and
                # head that no longer share their canvas with it.
                kind = "turrets_onepiece"
            elif name.startswith("proj_"):
                kind = "fx"
            else:
                kind = "drones"
            folder = os.path.join(root, kind)
            os.makedirs(folder, exist_ok=True)
            keyed.save(os.path.normpath(os.path.join(folder, "%s.png" % name)))
            print("%-16s %-12s %dx%d" % (name, kind, keyed.size[0], keyed.size[1]))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    args = sys.argv[1:]
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "art")
    sheet = Image.open(args[0]).convert("RGB")

    if "--single" in args:
        name = args[args.index("--single") + 1]
        keyed = trim_square(key_cell(sheet))
        os.makedirs(root, exist_ok=True)
        path = os.path.normpath(os.path.join(root, "%s.png" % name))
        keyed.save(path)
        print("%-16s %-12s %dx%d" % (name, "art", keyed.size[0], keyed.size[1]))
        return 0

    if "--projectiles" in args:
        slice_sheet(sheet, PROJECTILE_LAYOUT, root, labelled=True)
        return 0

    if "--turret-pairs" in args:
        mount_turrets(sheet, root)
        return 0

    slice_sheet(sheet, LAYOUT, root, labelled="--labels" in args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
