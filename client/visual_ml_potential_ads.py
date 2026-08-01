import cv2
import os
import numpy as np
from ultralytics import YOLO

# pre-filter thresholds — tune these before touching the model
MIN_PERIMETER    = 200     # w + h must exceed this (catches tiny icons/dots)
MIN_AREA         = 2_000   # pixel area must exceed this
MAX_ASPECT       = 15.0    # ignore absurdly wide/tall slivers
WIDE_ASPECT      = 5.0     # "wide banner" threshold
WIDE_MIN_AREA    = 80_000  # wide but small means it's probably not a real banner
COLOR_STD_THRESH = 18.0    # per-channel std below this means near-solid color block
LARGE_AREA       = 80_000  # large crops skip the color filter (busy hero images)


class AdBlockVision:
    def __init__(self, omni_weights, your_model_func):
        self.detector    = YOLO(omni_weights)
        self.classify_ad = your_model_func

    @staticmethod
    def _prefilter_crop(crop: np.ndarray, w: int, h: int) -> tuple[bool, str]:
        """
        Returns (skip, reason). skip=True means discard without calling the model.

        Checks in order:
          1. perimeter too small  — likely an icon, not ad-sized
          2. area too small       — same idea, also catches tall-thin slivers
          3. extreme aspect ratio — >15:1 slivers are decorators, not ads
          4. wide but tiny area   — wide aspect (>5:1) with area <80k rules out nav elements
          5. near-solid color     — flat UI chrome (skipped for large crops that may
                                    legitimately be simple hero banners)
        """
        area = w * h

        if w + h < MIN_PERIMETER:
            return True, f"perimeter too small ({w + h} < {MIN_PERIMETER})"

        if area < MIN_AREA:
            return True, f"area too small ({area} < {MIN_AREA})"

        aspect = w / h
        if aspect > MAX_ASPECT:
            return True, f"aspect ratio too extreme ({aspect:.1f} > {MAX_ASPECT})"

        if aspect > WIDE_ASPECT and area < WIDE_MIN_AREA:
            return True, f"wide banner but area too small (aspect={aspect:.1f}, area={area} < {WIDE_MIN_AREA})"

        # only run the color check on smaller crops — large ones can legitimately be flat
        if area < LARGE_AREA:
            channel_stds = [crop[:, :, c].std() for c in range(crop.shape[2])]
            if max(channel_stds) < COLOR_STD_THRESH:
                return True, f"near-solid color (max channel std={max(channel_stds):.1f} < {COLOR_STD_THRESH})"

        return False, ""

    def process_screen(self, image_path, output_path="blocked_result.jpg"):
        img = cv2.imread(image_path)
        if img is None:
            return

        overlay = img.copy()

        results = self.detector(image_path, conf=0.10, verbose=False)[0]

        for box in results.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            w = x2 - x1
            h = y2 - y1

            crop = img[max(0, y1):y2, max(0, x1):x2]
            if crop.size == 0:
                continue

            skip, _ = self._prefilter_crop(crop, w, h)
            if skip:
                continue

            is_ad = self.classify_ad(crop)

            if is_ad:
                cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 255), 3)
                cv2.putText(overlay, "potential AD", (x1, y1 - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        cv2.addWeighted(overlay, 0.7, img, 0.3, 0, img)
        cv2.imwrite(output_path, img)


def my_custom_ad_classifier(image_crop):
    # placeholder — replace with actual model inference
    h, w, _ = image_crop.shape
    return True


if __name__ == "__main__":
    OMNI_MODEL_PATH = "weights/icon_detect/model.pt"
    TEST_IMAGE      = "google_test.png"

    if os.path.exists(OMNI_MODEL_PATH) and os.path.exists(TEST_IMAGE):
        scanner = AdBlockVision(OMNI_MODEL_PATH, my_custom_ad_classifier)
        scanner.process_screen(TEST_IMAGE)
