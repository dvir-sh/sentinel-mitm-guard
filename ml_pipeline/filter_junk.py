import argparse
import shutil
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

MIN_TOTAL = 200
BLUR_THRESH = 40.0
COLOR_THRESH = 15.0

EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def laplacian_variance(gray: np.ndarray) -> float:
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def pixel_std(gray: np.ndarray) -> float:
    return float(gray.std())


def check_image(path: Path):
    try:
        img_pil = Image.open(path).convert("RGB")
    except Exception as e:
        return False, f"cannot open: {e}"

    w, h = img_pil.size
    if w + h < MIN_TOTAL:
        return False, f"too small ({w}x{h}, total={w+h})"

    img_cv = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2GRAY)

    std = pixel_std(img_cv)
    if std < COLOR_THRESH:
        return False, f"nearly single-color (std={std:.1f})"

    blur = laplacian_variance(img_cv)
    if blur < BLUR_THRESH:
        return False, f"too blurry (lap={blur:.1f})"

    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="./safe")
    ap.add_argument("--output", default="./safe_filtered")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.input)
    dst = Path(args.output)
    if not args.dry_run:
        dst.mkdir(parents=True, exist_ok=True)

    files = [f for f in src.iterdir() if f.suffix.lower() in EXTS]
    kept, removed = [], []

    for f in sorted(files):
        keep, reason = check_image(f)
        if keep:
            kept.append(f)
            if not args.dry_run:
                shutil.copy2(f, dst / f.name)
        else:
            removed.append((f, reason))
            print(f"  REMOVE  {f.name}  [{reason}]")

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Results:")
    print(f"  Total   : {len(files)}")
    print(f"  Kept    : {len(kept)}")
    print(f"  Removed : {len(removed)}")
    if not args.dry_run:
        print(f"  Kept images copied to: {dst}")


if __name__ == "__main__":
    main()
