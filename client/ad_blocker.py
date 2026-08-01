import os
import json
import time
from datetime import datetime
import threading
import numpy as np
import tensorflow as tf
import cv2

from visual_ml_potential_ads import AdBlockVision

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MODEL_PATH          = os.path.join(BASE_DIR, 'ad_blocker_model_final.keras')
OMNI_MODEL_PATH     = os.path.join(BASE_DIR, 'model.pt')
IMG_SIZE            = (160, 160)
BASE_SCREENSHOT_PATH = os.path.join(BASE_DIR, "_temp_screenshot")
DEBUG_OUTPUT_DIR    = os.path.join(BASE_DIR, "debug_passes")

DETECTION_INTERVAL_SECONDS = 2   # lower = more responsive, higher = less CPU
AD_SCORE_THRESHOLD         = 0.4  # ML confidence needed to flag something as an ad
YOLO_CONF_THRESHOLD        = 0.10
DOM_WALK_DEPTH             = 6    # how many parent levels to climb when locating the ad container

# populated lazily the first time start_adblock_thread() is called
model          = None
scanner        = None
SESSION_DEBUG_DIR = None
_models_loaded = False


def _init_models():
    # models are loaded here rather than at import time so that just importing
    # this module doesn't trigger TensorFlow init when ad-blocking isn't enabled.
    # the top-level 'import tensorflow' still runs on import to ensure TF is
    # initialised before PyQt5, which prevents a known tensor-init crash.
    global model, scanner, SESSION_DEBUG_DIR, _models_loaded

    if _models_loaded:
        return

    session_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    SESSION_DEBUG_DIR = os.path.join(DEBUG_OUTPUT_DIR, f"session_{session_timestamp}")
    os.makedirs(SESSION_DEBUG_DIR, exist_ok=True)

    model   = tf.keras.models.load_model(MODEL_PATH)
    scanner = AdBlockVision(OMNI_MODEL_PATH, lambda crop: classify_crop(crop)[0])

    _models_loaded = True


def classify_crop(image_crop_bgr):
    """Run the ML classifier on a BGR image crop. Returns (is_ad: bool, score: float)."""
    rgb       = cv2.cvtColor(image_crop_bgr, cv2.COLOR_BGR2RGB)
    resized   = cv2.resize(rgb, IMG_SIZE)
    img_array = tf.keras.utils.img_to_array(resized)
    img_array = tf.expand_dims(img_array, 0)
    prediction = model.predict(img_array, verbose=0)[0][0]
    return prediction > AD_SCORE_THRESHOLD, float(prediction)


def draw_debug_circle_box(img, x1, y1, x2, y2, color, label=None, thickness=2, radius=10):
    """Draw a rectangle with circled corners and an optional label onto img in-place."""
    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)

    for cx, cy in [(x1, y1), (x2, y1), (x1, y2), (x2, y2)]:
        cv2.circle(img, (cx, cy), radius, color, thickness)

    if label:
        font       = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.55
        pad        = 4
        (tw, th), _ = cv2.getTextSize(label, font, font_scale, 1)
        cv2.rectangle(img,
                      (x1, y1 - th - pad * 2),
                      (x1 + tw + pad * 2, y1),
                      color, -1)
        cv2.putText(img, label,
                    (x1 + pad, y1 - pad),
                    font, font_scale, (0, 0, 0), 1, cv2.LINE_AA)


def hide_element_robustly(driver, cx, cy, ad_w, ad_h):
    # walks the DOM upward to find the ad's real container element,
    # corrects for high-DPI scaling, and stops climbing once the parent
    # grows significantly larger than the detected bounding box
    script = f"""
    const scale  = window.devicePixelRatio || 1;
    const clickX = arguments[0] / scale;
    const clickY = arguments[1] / scale;
    const mlBoxW = arguments[2] / scale;
    const mlBoxH = arguments[3] / scale;

    let el = document.elementFromPoint(clickX, clickY);
    if (!el) return false;

    let candidate = el;
    for (let i = 0; i < {DOM_WALK_DEPTH}; i++) {{
        const parent = candidate.parentElement;
        if (!parent || parent.tagName === 'BODY' || parent.tagName === 'HTML') break;

        const rect = parent.getBoundingClientRect();
        // stop if the parent is more than 1.5x the ad box — it's probably a page section
        if (rect.width > (mlBoxW * 1.5) || rect.height > (mlBoxH * 1.5)) {{
            break;
        }}
        candidate = parent;
    }}

    candidate.setAttribute('data-ad-blocked', 'true');
    candidate.style.setProperty('visibility',    'hidden', 'important');
    candidate.style.setProperty('height',        '0',      'important');
    candidate.style.setProperty('overflow',      'hidden', 'important');
    candidate.style.setProperty('pointer-events','none',   'important');

    return candidate.tagName + ' | ' + (candidate.className || '').slice(0, 60);
    """
    return driver.execute_script(script, cx, cy, ad_w, ad_h)


def inject_persistent_hider(driver):
    # injects a MutationObserver and scroll listener that re-hides anything
    # already tagged data-ad-blocked, keeping ads hidden through SPA navigation
    # and infinite-scroll page mutations. safe to call more than once.
    script = """
    if (window._adBlockerInjected) return;
    window._adBlockerInjected = true;

    function rehideAll() {
        document.querySelectorAll('[data-ad-blocked="true"]').forEach(el => {
            el.style.setProperty('visibility',    'hidden', 'important');
            el.style.setProperty('height',        '0',      'important');
            el.style.setProperty('overflow',      'hidden', 'important');
            el.style.setProperty('pointer-events','none',   'important');
        });
    }

    window.addEventListener('scroll', rehideAll, { passive: true });

    const observer = new MutationObserver(rehideAll);
    observer.observe(document.body, { childList: true, subtree: true });
    """
    driver.execute_script(script)


def run_detection_pass(driver, pass_number):
    """
    Take a screenshot, run YOLO + ML classifier, hide confirmed ads.

    Produces three files per pass in SESSION_DEBUG_DIR:
      pass_NNNN_original.png  - raw screenshot (useful for retraining)
      pass_NNNN_debug.png     - annotated overlay showing what was found
      pass_NNNN_meta.json     - bounding boxes, scores, and per-pass stats
    """
    pass_prefix   = f"pass_{pass_number:04d}"
    original_path = os.path.join(SESSION_DEBUG_DIR, f"{pass_prefix}_original.png")
    debug_path    = os.path.join(SESSION_DEBUG_DIR, f"{pass_prefix}_debug.png")
    meta_path     = os.path.join(SESSION_DEBUG_DIR, f"{pass_prefix}_meta.json")

    driver.save_screenshot(original_path)
    img       = cv2.imread(original_path)
    debug_img = img.copy()
    img_h, img_w = img.shape[:2]

    try:
        page_url = driver.current_url
    except Exception:
        page_url = ""

    results_yolo     = scanner.detector(original_path, conf=YOLO_CONF_THRESHOLD, verbose=False)[0]
    ad_boxes         = []
    total_candidates = 0
    skipped_by_filter = 0
    detections       = []

    for box in results_yolo.boxes:
        x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
        yolo_conf = float(box.conf[0])
        w = x2 - x1
        h = y2 - y1
        total_candidates += 1

        crop = img[max(0, y1):y2, max(0, x1):x2]
        if crop.size == 0:
            continue

        skip, reason = AdBlockVision._prefilter_crop(crop, w, h)
        if skip:
            skipped_by_filter += 1
            draw_debug_circle_box(debug_img, x1, y1, x2, y2,
                                  color=(90, 90, 90),
                                  label=f"SKIP: {reason.split('(')[0].strip()}",
                                  thickness=1, radius=4)
            detections.append({
                "bbox": [x1, y1, x2, y2],
                "yolo_conf": round(yolo_conf, 4),
                "label": "PREFILTERED",
                "skip_reason": reason,
                "ml_score": None,
            })
            continue

        is_ad, score = classify_crop(crop)

        draw_debug_circle_box(debug_img, x1, y1, x2, y2,
                              color=(0, 165, 255),
                              label=f"YOLO {yolo_conf:.2f}",
                              radius=8)

        if is_ad:
            draw_debug_circle_box(debug_img, x1, y1, x2, y2,
                                  color=(0, 220, 60),
                                  label=f"AD {score:.2f}",
                                  thickness=3, radius=12)
            ad_boxes.append((x1, y1, x2, y2))
        else:
            draw_debug_circle_box(debug_img, x1, y1, x2, y2,
                                  color=(60, 60, 220),
                                  label=f"NOT AD {score:.2f}",
                                  thickness=1, radius=6)

        detections.append({
            "bbox": [x1, y1, x2, y2],
            "yolo_conf": round(yolo_conf, 4),
            "label": "AD" if is_ad else "NOT_AD",
            "skip_reason": None,
            "ml_score": round(score, 4),
        })

    # draw a legend in the top-left corner
    legend = [
        ((0, 165, 255), "YOLO candidate (passed filter)"),
        ((0, 220, 60),  "Confirmed AD"),
        ((60, 60, 220), "Rejected by classifier"),
        ((90, 90, 90),  "Pre-filtered (too small / flat color)"),
    ]
    lx, ly = 20, 20
    for color, text in legend:
        cv2.rectangle(debug_img, (lx, ly), (lx + 20, ly + 16), color, -1)
        cv2.putText(debug_img, text, (lx + 28, ly + 13),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        ly += 26

    # pass summary in the bottom-left corner
    stats_lines = [
        f"Pass:         {pass_number:04d}",
        f"YOLO total:   {total_candidates}",
        f"Pre-filtered: {skipped_by_filter}",
        f"Sent to model:{total_candidates - skipped_by_filter}",
        f"Ads found:    {len(ad_boxes)}",
        datetime.now().strftime("%H:%M:%S"),
    ]
    h_px = debug_img.shape[0]
    sx, sy = 20, h_px - (len(stats_lines) * 22 + 10)
    for line in stats_lines:
        cv2.putText(debug_img, line, (sx, sy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1, cv2.LINE_AA)
        sy += 22

    cv2.imwrite(debug_path, debug_img)

    # JSON sidecar ties the two image files to all the raw detection data,
    # so a cloud pipeline can reconstruct training samples without re-running inference
    meta = {
        "pass_number": pass_number,
        "timestamp":   datetime.now().isoformat(),
        "page_url":    page_url,
        "image_size":  {"width": img_w, "height": img_h},
        "files": {
            "original": os.path.basename(original_path),
            "debug":    os.path.basename(debug_path),
        },
        "thresholds": {
            "ad_score":  AD_SCORE_THRESHOLD,
            "yolo_conf": YOLO_CONF_THRESHOLD,
        },
        "stats": {
            "yolo_total":    total_candidates,
            "prefiltered":   skipped_by_filter,
            "sent_to_model": total_candidates - skipped_by_filter,
            "ads_found":     len(ad_boxes),
        },
        "detections": detections,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    hidden_count = 0
    for (x1, y1, x2, y2) in ad_boxes:
        cx    = (x1 + x2) // 2
        cy    = (y1 + y2) // 2
        box_w = x2 - x1
        box_h = y2 - y1
        try:
            result = hide_element_robustly(driver, cx, cy, box_w, box_h)
            if result:
                hidden_count += 1
        except Exception:
            pass

    return hidden_count


def ad_block_loop(driver):
    """Continuous detection loop, meant to run in a background daemon thread."""
    time.sleep(2)

    try:
        inject_persistent_hider(driver)
    except Exception:
        pass

    pass_number   = 0
    try:
        known_handles = set(driver.window_handles)
    except Exception:
        known_handles = set()

    while True:
        try:
            try:
                current_handles = driver.window_handles
            except Exception:
                # can't get window handles — browser is fully closed
                break

            try:
                active_handle = driver.current_window_handle
            except Exception:
                # active tab was just closed
                active_handle = None

            if active_handle not in current_handles:
                if current_handles:
                    driver.switch_to.window(current_handles[-1])
                else:
                    break

            # check for newly opened tabs and move selenium's focus to the latest one
            current_handles_set = set(current_handles)
            new_handles = current_handles_set - known_handles
            if new_handles:
                latest_new_tab = list(new_handles)[-1]
                driver.switch_to.window(latest_new_tab)
                inject_persistent_hider(driver)

            known_handles = current_handles_set

            try:
                is_hidden = driver.execute_script("return document.visibilityState === 'hidden';")
            except Exception as js_err:
                err_msg = str(js_err).lower()
                if "no such window" in err_msg or "session deleted" in err_msg:
                    break
                else:
                    time.sleep(DETECTION_INTERVAL_SECONDS)
                    continue

            # skip the scan if the tab is in the background
            if is_hidden:
                time.sleep(DETECTION_INTERVAL_SECONDS)
                continue

            pass_number += 1
            run_detection_pass(driver, pass_number)
            inject_persistent_hider(driver)

            time.sleep(DETECTION_INTERVAL_SECONDS)

        except Exception as e:
            err_msg = str(e).lower()
            if "no such window" in err_msg or "session deleted" in err_msg:
                break
            else:
                time.sleep(DETECTION_INTERVAL_SECONDS)


def start_adblock_thread(driver):
    """Load models (lazily) and spawn the detection loop as a daemon thread."""
    _init_models()
    t = threading.Thread(target=ad_block_loop, args=(driver,), daemon=True)
    t.start()
    return t
