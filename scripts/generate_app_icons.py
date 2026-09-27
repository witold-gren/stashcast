"""
Build the home-screen icons from the StashCast logo.

iOS does not resize or pad what it is given: whatever the page offers as
``apple-touch-icon`` is drawn as-is under the system's rounded-corner mask. An icon with
a transparent background therefore turns black on the home screen, and one whose artwork
runs to the edges gets its corners cut off. So the logo mark is composed here onto an
opaque square with room to breathe, once, and the result is committed as a static file.

The wordmark is dropped - it is unreadable at 180 px - leaving only the mark.

    ./scripts/generate_app_icons.py            # regenerate media/static/media/icons/
    ./scripts/generate_app_icons.py --check    # fail if the icons are out of date
"""

import argparse
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

BASE_DIR = Path(__file__).resolve().parent.parent
SOURCE = BASE_DIR / 'media' / 'static' / 'media' / 'header-transparent.png'
OUTPUT_DIR = BASE_DIR / 'media' / 'static' / 'media' / 'icons'

# The logo file is the mark followed by the "STASHCAST" wordmark. This is the first fully
# transparent column between the two.
MARK_WIDTH = 316

# Matches the landing page: radial highlight over a near-black navy
BACKGROUND_CENTRE = (27, 43, 74)
BACKGROUND_EDGE = (10, 17, 32)

# Share of the canvas the artwork may occupy. A plain icon only has to clear the rounded
# corners; a maskable one must survive being cropped to a circle of 80% of the width,
# which leaves far less usable room.
PLAIN_SCALE = 0.74
MASKABLE_SCALE = 0.56

ICONS = [
    ('icon-32.png', 32, PLAIN_SCALE),
    ('icon-180.png', 180, PLAIN_SCALE),  # apple-touch-icon
    ('icon-192.png', 192, PLAIN_SCALE),
    ('icon-512.png', 512, PLAIN_SCALE),
    ('icon-maskable-512.png', 512, MASKABLE_SCALE),
]


def load_mark():
    """The logo mark on its own, cropped to its visible pixels.

    Returns:
        PIL.Image.Image: RGBA image of the mark.
    """
    logo = Image.open(SOURCE).convert('RGBA')
    mark = logo.crop((0, 0, MARK_WIDTH, logo.height))
    return mark.crop(mark.getbbox())


def make_background(size):
    """An opaque square with a soft radial highlight towards the top left.

    Args:
        size: Edge length in pixels

    Returns:
        PIL.Image.Image: RGB image of that size.
    """
    # Drawn small and scaled up: a gradient has no detail to lose, and computing it per
    # pixel at 512 would be needlessly slow
    steps = 64
    gradient = Image.new('RGB', (steps, steps))
    pixels = gradient.load()
    centre = (steps * 0.32, steps * 0.28)
    furthest = max(
        ((x - centre[0]) ** 2 + (y - centre[1]) ** 2) ** 0.5
        for x in (0, steps - 1)
        for y in (0, steps - 1)
    )

    for y in range(steps):
        for x in range(steps):
            distance = ((x - centre[0]) ** 2 + (y - centre[1]) ** 2) ** 0.5 / furthest
            pixels[x, y] = tuple(
                round(near + (far - near) * distance)
                for near, far in zip(BACKGROUND_CENTRE, BACKGROUND_EDGE)
            )

    return gradient.resize((size, size), Image.LANCZOS)


def render(mark, size, scale):
    """Compose the mark onto a background square.

    Args:
        mark: The cropped logo mark
        size: Edge length of the icon
        scale: Share of the edge the artwork may occupy

    Returns:
        PIL.Image.Image: RGB image, ready to be saved.
    """
    canvas = make_background(size)

    budget = size * scale
    ratio = min(budget / mark.width, budget / mark.height)
    drawn = mark.resize(
        (max(1, round(mark.width * ratio)), max(1, round(mark.height * ratio))),
        Image.LANCZOS,
    )

    canvas.paste(
        drawn,
        ((size - drawn.width) // 2, (size - drawn.height) // 2),
        drawn,  # the mark's own alpha is the mask; the result stays opaque
    )
    return canvas


def build():
    """Render every icon.

    Returns:
        dict[str, bytes]: File name to PNG contents.
    """
    mark = load_mark()
    rendered = {}
    for name, size, scale in ICONS:
        buffer = BytesIO()
        render(mark, size, scale).save(buffer, format='PNG', optimize=True)
        rendered[name] = buffer.getvalue()
    return rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--check',
        action='store_true',
        help='Report whether the committed icons match the logo instead of writing them',
    )
    options = parser.parse_args()

    rendered = build()

    if options.check:
        stale = [
            name
            for name, data in rendered.items()
            if not (OUTPUT_DIR / name).exists() or (OUTPUT_DIR / name).read_bytes() != data
        ]
        if stale:
            print('Out of date: ' + ', '.join(sorted(stale)))
            print('Run ./scripts/generate_app_icons.py to rebuild them.')
            return 1
        print(f'{len(rendered)} icon(s) up to date')
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in rendered.items():
        (OUTPUT_DIR / name).write_bytes(data)
        print(f'  {name}')
    print(f'Wrote {len(rendered)} icon(s) to {OUTPUT_DIR.relative_to(BASE_DIR)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
