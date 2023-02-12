from typing import List
from pathlib import Path
from pil_utils import BuildImage

from meme_generator import add_meme
from meme_generator.utils import make_jpg_or_gif
from meme_generator.exception import TextOverLength


img_dir = Path(__file__).parent / "images"


def anyasuki(images: List[BuildImage], texts: List[str], args):
    img = images[0]
    text = texts[0] if texts else "阿尼亚喜欢这个"
    frame = BuildImage.open(img_dir / "0.png")
    try:
        frame.draw_text(
            (5, frame.height - 60, frame.width - 5, frame.height - 10),
            text,
            max_fontsize=40,
            fill="white",
            stroke_fill="black",
            stroke_ratio=0.06,
        )
    except ValueError:
        raise TextOverLength(text)

    def make(img: BuildImage) -> BuildImage:
        return frame.copy().paste(
            img.resize((305, 235), keep_ratio=True), (106, 72), below=True
        )

    return make_jpg_or_gif(img, make)


add_meme(
    "anyasuki",
    ["阿尼亚喜欢"],
    anyasuki,
    min_images=1,
    max_images=1,
    min_texts=0,
    max_texts=1,
    default_texts=["阿尼亚喜欢这个"],
)
