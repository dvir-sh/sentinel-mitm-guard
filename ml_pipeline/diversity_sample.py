import argparse
import shutil
from pathlib import Path
from collections import defaultdict

import numpy as np
from PIL import Image
import imagehash
from sklearn.cluster import MiniBatchKMeans
from sklearn.preprocessing import normalize

EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

DEDUP_THRESH = 8
THUMB_SIZE = (32, 32)


def load_images(folder: Path):
    paths = [f for f in folder.iterdir() if f.suffix.lower() in EXTS]
    print(f"Found {len(paths)} images in {folder}")
    return sorted(paths)


def compute_hashes(paths):
    hashes = {}
    for p in paths:
        try:
            hashes[p] = imagehash.dhash(Image.open(p))
        except Exception as e:
            print(f"  Warning: could not hash {p.name}: {e}")
    return hashes


def dedup(paths, hashes, thresh):
    visited = set()
    groups = []

    path_list = [p for p in paths if p in hashes]
    for i, p in enumerate(path_list):
        if p in visited:
            continue
        group = [p]
        visited.add(p)
        for j in range(i + 1, len(path_list)):
            q = path_list[j]
            if q not in visited and (hashes[p] - hashes[q]) <= thresh:
                group.append(q)
                visited.add(q)
        groups.append(group)

    kept = [g[0] for g in groups]
    print(f"  After dedup (thresh={thresh}): {len(path_list)} -> {len(kept)} images "
          f"({len(path_list)-len(kept)} near-duplicates removed)")
    return kept


def extract_features(paths, thumb_size):
    feats, valid = [], []
    for p in paths:
        try:
            img = Image.open(p).convert("RGB").resize(thumb_size, Image.LANCZOS)
            arr = np.array(img, dtype=np.float32).flatten()
            feats.append(arr)
            valid.append(p)
        except Exception as e:
            print(f"  Warning: could not load {p.name}: {e}")
    X = normalize(np.stack(feats))
    return valid, X


def cluster_sample(paths, X, target):
    n = len(paths)
    if n <= target:
        print(f"  {n} images <= target {target}; keeping all.")
        return paths

    print(f"  Clustering {n} images into {target} groups...")
    km = MiniBatchKMeans(n_clusters=target, random_state=42, n_init=5)
    labels = km.fit_predict(X)
    centroids = km.cluster_centers_

    selected = []
    for c in range(target):
        idxs = np.where(labels == c)[0]
        if len(idxs) == 0:
            continue
        cluster_X = X[idxs]
        dists = np.linalg.norm(cluster_X - centroids[c], axis=1)
        best = idxs[np.argmin(dists)]
        selected.append(paths[best])

    print(f"  Selected {len(selected)} representative images.")
    return selected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="./safe_filtered")
    ap.add_argument("--output", default="./safe_150")
    ap.add_argument("--target", type=int, default=150)
    ap.add_argument("--dedup-thresh", type=int, default=DEDUP_THRESH)
    args = ap.parse_args()

    src = Path(args.input)
    dst = Path(args.output)
    dst.mkdir(parents=True, exist_ok=True)

    paths = load_images(src)

    print("\nStep 1: perceptual deduplication...")
    hashes = compute_hashes(paths)
    deduped = dedup(paths, hashes, args.dedup_thresh)

    print("\nStep 2: diversity sampling via clustering...")
    valid_paths, X = extract_features(deduped, THUMB_SIZE)
    selected = cluster_sample(valid_paths, X, args.target)

    print(f"\nCopying {len(selected)} images to {dst}...")
    for p in selected:
        shutil.copy2(p, dst / p.name)

    print(f"\nDone. Final safe set: {len(selected)} images in {dst}")


if __name__ == "__main__":
    main()
