#!/usr/bin/env python3

import os
import re
import random
from PIL import Image
from icecream import ic

BORDERS = [
    "/home/jack/Desktop/epoch/static/border_dirty1.png",
    "/home/jack/Desktop/epoch/static/border_dirty1.png",
    "/home/jack/Desktop/epoch/static/border_dirty2.png",
    "/home/jack/Desktop/epoch/static/border_dirty3.png"
]

IMAGE_DIR = "."

def apply_border(img):
    border_path = random.choice(BORDERS)

    border = Image.open(border_path).convert("RGBA")
    border = border.resize(img.size, Image.LANCZOS)

    base = img.convert("RGBA")
    result = Image.alpha_composite(base, border)

    ic(f"Applied border: {border_path}")

    return result

files = sorted(
    f for f in os.listdir(IMAGE_DIR)
    if re.match(r"^clean_\d{3}\.png$", f)
)

ic(f"Found {len(files)} clean images")

for filename in files:
    try:
        input_path = os.path.join(IMAGE_DIR, filename)

        number = re.search(r"(\d{3})", filename).group(1)

        output_filename = f"frame_{number}.png"
        output_path = os.path.join(IMAGE_DIR, output_filename)

        img = Image.open(input_path)
        result = apply_border(img)

        result.save(output_path)

        ic(f"Saved: {output_path}")

    except Exception as e:
        ic(f"ERROR processing {filename}: {e}")

print("Finished.")
