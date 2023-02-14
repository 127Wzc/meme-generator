import random
from typing import List
from pathlib import Path
from pil_utils import BuildImage
from PIL.Image import Image as IMG

from meme_generator import add_meme
from meme_generator.utils import save_gif


img_dir = Path(__file__).parent / "images"


def turn(images: List[BuildImage], texts, args):
    img = images[0].convert("RGBA").circle()
    frames: List[IMG] = []
    for i in range(0, 360, 10):
        frame = BuildImage.new("RGBA", (250, 250), "white")
        frame.paste(img.rotate(i).resize((250, 250)), alpha=True)
        frames.append(frame.image)
    if random.randint(0, 1):
        frames.reverse()
    return save_gif(frames, 0.05)


add_meme("turn", ["转"], turn, min_images=1, max_images=1)
