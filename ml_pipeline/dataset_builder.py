import os
import time
import uuid
import cv2
import numpy as np
from multiprocessing import Pool
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from ultralytics import YOLO

OMNI_MODEL_PATH   = "weights/icon_detect/model.pt"
PARALLEL_WORKERS  = 4
PAGE_WAIT_SECONDS = 8
MAX_ADS_PER_SITE  = 30
MAX_SAFE_PER_SITE = 15

URLS_TO_SCRAPE = [
    "https://www.youtube.com/",
]

AD_SELECTORS = [
    '[id^="google_ads_iframe"]', '[id^="div-gpt-ad"]',
    'ins.adsbygoogle',
    'iframe[src*="doubleclick"]', 'iframe[src*="googlesyndication"]',
    'iframe[src*="googletagservices"]',
    '[data-google-query-id]',
    'iframe[id*="ad"]', 'iframe[title*="Advertisement"]',
    'iframe[aria-label*="Advertisement"]',
    '[id*="AdSlot"]',   '[id*="ad-slot"]',   '[id*="ad_slot"]',
    '[id*="adhesion"]', '[id*="dfp"]',        '[id*="prebid"]',
    '[class*="Advertisement"]', '[class*="advertisement"]',
    '[class*="banner-ad"]',     '[class*="ad-banner"]',
    '[class*="ad-unit"]',       '[class*="adUnit"]',
    '[class*="ad-wrapper"]',    '[class*="adWrapper"]',
    '[class*="ad-block"]',      '[class*="adBlock"]',
    '[class*="adhesion"]',      '[class*="dfp"]',
    '[class*="prebid"]',        '[class*="adsense"]',
    '[class*="leaderboard"]',   '[class*="mrec"]',
    '[class*="halfpage"]',
    '[data-testid*="ad"]', '[aria-label="Advertisement"]',
    '[data-ad-unit]',      '[data-ad-type]',  '[data-slot]',
    '[class*="sponsor"]',      '[class*="Sponsor"]',
    '[class*="promo-widget"]', '[class*="promoted"]',
    '[class*="native-ad"]',    '[class*="nativeAd"]',
    '.sponsored-content-wrapper',
    '.taboola-zone', '.trc_related_container', '.OUTBRAIN',
    '[id*="taboola"]', '[id*="outbrain"]',
    '.mgid-container', '[data-widget-id]',
    '.ad-container', '.ad-slot', 'amp-ad',
    '.widget-area .textwidget',
]

SAFE_SELECTORS = [
    'h1', 'h2', 'h3', 'p', 'button', 'picture',
    '[role="navigation"]', 'main img', 'article', 'nav',
    'header', 'footer', '.logo', '[role="main"]',
]

CONSENT_SELECTORS = [
    '#onetrust-accept-btn-handler',
    'button[id*="accept"]',
    'button[class*="accept"]',
    'button[aria-label*="Accept"]',
    'button[aria-label*="accept"]',
    '.css-1litn2c',
    '[data-testid="accept-button"]',
    '[class*="consent"] button',
    '[id*="consent"] button',
    '[class*="cookie"] button[class*="accept"]',
    '[id*="cookie"] button[class*="accept"]',
]

os.makedirs("dataset/ad",   exist_ok=True)
os.makedirs("dataset/safe", exist_ok=True)


def setup_browser():
    options = webdriver.ChromeOptions()
    options.page_load_strategy = 'normal'
    options.add_argument('--headless')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--mute-audio')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_argument(
        'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    )
    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)
    return driver


def dismiss_consent(driver):
    for sel in CONSENT_SELECTORS:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed():
                btn.click()
                time.sleep(1)
                return
        except Exception:
            pass


def scroll_page(driver):
    for _pass in range(2):
        total_height = driver.execute_script("return document.body.scrollHeight")
        for fraction in [0.33, 0.66, 1.0]:
            driver.execute_script(f"window.scrollTo(0, {int(total_height * fraction)});")
            time.sleep(1.5)
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)


def get_labeled_boxes(driver):
    def rects_for_selectors(selectors):
        boxes = []
        for sel in selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if not el.is_displayed():
                            continue
                        loc, size = el.location, el.size
                        if size['width'] < 10 or size['height'] < 10:
                            continue
                        x1 = int(loc['x'])
                        y1 = int(loc['y'])
                        boxes.append((x1, y1, x1 + int(size['width']), y1 + int(size['height'])))
                    except Exception:
                        pass
            except Exception:
                pass
        return boxes
    return rects_for_selectors(AD_SELECTORS), rects_for_selectors(SAFE_SELECTORS)


def iou(a, b):
    xA, yA = max(a[0], b[0]), max(a[1], b[1])
    xB, yB = min(a[2], b[2]), min(a[3], b[3])
    inter  = max(0, xB - xA) * max(0, yB - yA)
    if inter == 0:
        return 0.0
    return inter / float(
        (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    )


def label_yolo_crop(yolo_box, ad_boxes, safe_boxes, threshold=0.05):
    best_ad   = max((iou(yolo_box, b) for b in ad_boxes),   default=0.0)
    best_safe = max((iou(yolo_box, b) for b in safe_boxes), default=0.0)
    if best_ad < threshold and best_safe < threshold:
        return None
    return 'ad' if best_ad >= best_safe else 'safe'


def worker(args):
    url, worker_id, progress_str = args
    driver = None
    try:
        detector = YOLO(OMNI_MODEL_PATH)
        driver   = setup_browser()

        print(f"[W{worker_id}] {progress_str} {url}")
        driver.get(url)
        time.sleep(PAGE_WAIT_SECONDS)

        dismiss_consent(driver)
        time.sleep(1)

        scroll_page(driver)

        ad_boxes, safe_boxes = get_labeled_boxes(driver)

        png_bytes = driver.get_screenshot_as_png()
        driver.quit()
        driver = None

        img = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not decode screenshot")

        results  = detector(img, conf=0.05, verbose=False)[0]
        ad_saved = safe_saved = 0

        for box in results.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            w, h = x2 - x1, y2 - y1
            if w < 50 or h < 50:
                continue
            if w / h > 5.0:
                continue

            label = label_yolo_crop((x1, y1, x2, y2), ad_boxes, safe_boxes)
            if label is None:
                continue
            if label == 'ad'   and ad_saved   >= MAX_ADS_PER_SITE:
                continue
            if label == 'safe' and safe_saved >= MAX_SAFE_PER_SITE:
                continue

            crop = img[max(0, y1):y2, max(0, x1):x2]
            if crop.size == 0:
                continue

            cv2.imwrite(f"dataset/{label}/{label}_{uuid.uuid4().hex[:8]}.png", crop)
            if label == 'ad':
                ad_saved   += 1
            else:
                safe_saved += 1

        print(f"[W{worker_id}] {url} -> {ad_saved} ads, {safe_saved} safe")
        return (url, ad_saved, safe_saved, None)

    except Exception as e:
        print(f"[W{worker_id}] {url} -> error: {e}")
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        return (url, 0, 0, str(e))


def collect_data():
    total = len(URLS_TO_SCRAPE)
    tasks = [
        (url, i % PARALLEL_WORKERS, f"[{i+1}/{total}] {(i+1)/total*100:.0f}%")
        for i, url in enumerate(URLS_TO_SCRAPE)
    ]

    time_per_site = PAGE_WAIT_SECONDS + 10
    est_parallel  = total * time_per_site / PARALLEL_WORKERS / 60
    print(f"Starting with {PARALLEL_WORKERS} parallel workers")
    print(f"{total} URLs | ~{time_per_site}s per site")
    print(f"Estimated: ~{est_parallel:.0f} min\n")

    total_ads = total_safe = total_errors = 0

    with Pool(processes=PARALLEL_WORKERS) as pool:
        for url, ads, safe, err in pool.imap_unordered(worker, tasks, chunksize=1):
            total_ads  += ads
            total_safe += safe
            if err:
                total_errors += 1

    print(f"Done.")
    print(f"Ad crops  : {total_ads}")
    print(f"Safe crops: {total_safe}")
    print(f"Errors    : {total_errors}")


if __name__ == "__main__":
    collect_data()
