"""
ParkPi — Licence Plate Recognition
Uses OpenCV + Tesseract OCR to read plates from a Pi Camera or USB webcam.
Falls back to a simulation mode when no camera is available.
"""

import re
import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable

log = logging.getLogger("parkpi.plate")

# ── Optional imports (hardware only) ─────────────────────────────────────────
try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    log.warning("OpenCV not found — plate recognition unavailable")

try:
    import pytesseract
    HAS_TESS = True
except ImportError:
    HAS_TESS = False
    log.warning("pytesseract not found — OCR unavailable")

try:
    from picamera2 import Picamera2
    HAS_PICAM = True
except ImportError:
    HAS_PICAM = False


# ── Config ────────────────────────────────────────────────────────────────────

PLATE_PATTERN = re.compile(r"[A-Z0-9]{2,3}[-\s]?[A-Z0-9]{2,3}[-\s]?[A-Z0-9]{2,4}")
MIN_CONFIDENCE = 60     # Tesseract confidence threshold (0–100)
CAPTURE_INTERVAL = 0.5  # seconds between frames
DEBOUNCE_READS = 3      # same plate must appear N times before accepting


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class PlateRead:
    text: str
    confidence: float
    timestamp: float = field(default_factory=time.time)
    frame_path: Optional[str] = None   # saved debug image path


# ── Image processing pipeline ─────────────────────────────────────────────────

def preprocess(frame) -> "np.ndarray":
    """
    Convert camera frame into a binary image optimised for OCR.
    Steps: greyscale → bilateral filter → adaptive threshold → dilate.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    filtered = cv2.bilateralFilter(gray, 11, 17, 17)
    thresh = cv2.adaptiveThreshold(
        filtered, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        19, 9
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    return cv2.dilate(thresh, kernel, iterations=1)


def find_plate_candidates(frame) -> list:
    """
    Return list of (x, y, w, h) rectangles that look like plates.
    Filters by aspect ratio (2:1 – 6:1) and minimum area.
    """
    processed = preprocess(frame)
    contours, _ = cv2.findContours(
        processed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )
    candidates = []
    for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:30]:
        x, y, w, h = cv2.boundingRect(cnt)
        if h == 0:
            continue
        ratio = w / h
        area = w * h
        if 1.8 < ratio < 6.5 and area > 1500:
            candidates.append((x, y, w, h))
    return candidates


def ocr_region(gray_region) -> tuple[str, float]:
    """
    Run Tesseract on a greyscale image region.
    Returns (plate_text, confidence).
    """
    # Upscale small regions for better OCR accuracy
    h, w = gray_region.shape
    if w < 200:
        scale = 200 / w
        gray_region = cv2.resize(
            gray_region, (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_CUBIC
        )

    config = "--oem 3 --psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
    data = pytesseract.image_to_data(
        gray_region, config=config,
        output_type=pytesseract.Output.DICT
    )
    texts, confs = [], []
    for txt, conf in zip(data["text"], data["conf"]):
        txt = txt.strip()
        conf = int(conf)
        if txt and conf > 0:
            texts.append(txt)
            confs.append(conf)

    if not texts:
        return "", 0.0

    combined = "".join(texts).upper().replace(" ", "")
    avg_conf = sum(confs) / len(confs)
    return combined, avg_conf


def read_plate_from_frame(frame) -> Optional[PlateRead]:
    """
    Full pipeline: camera frame → PlateRead or None.
    """
    if not HAS_CV2 or not HAS_TESS:
        return None

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    candidates = find_plate_candidates(frame)

    for x, y, w, h in candidates:
        # Crop with padding
        pad = 6
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(frame.shape[1], x + w + pad)
        y2 = min(frame.shape[0], y + h + pad)
        region = gray[y1:y2, x1:x2]

        text, conf = ocr_region(region)

        if conf < MIN_CONFIDENCE:
            continue

        match = PLATE_PATTERN.search(text)
        if not match:
            continue

        clean = match.group(0).replace(" ", "").upper()
        log.debug("Plate candidate: %s (conf %.0f%%)", clean, conf)
        return PlateRead(text=clean, confidence=conf)

    return None


# ── Camera reader ─────────────────────────────────────────────────────────────

class PlateRecognizer:
    """
    Background thread that continuously captures frames and calls
    on_plate_detected(plate_text) whenever a plate is confirmed.
    """

    def __init__(
        self,
        on_plate_detected: Callable[[str], None],
        camera_index: int = 0,
        use_picamera: bool = True,
        save_debug_frames: bool = False,
        debug_dir: str = "/tmp/parkpi_frames",
    ):
        self.on_plate_detected = on_plate_detected
        self.camera_index = camera_index
        self.use_picamera = use_picamera and HAS_PICAM
        self.save_debug = save_debug_frames
        self.debug_dir = debug_dir
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._recent: dict[str, int] = {}   # plate → consecutive read count

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Plate recognizer started (picam=%s)", self.use_picamera)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    # ── internal ──────────────────────────────────────────────────────────────

    def _loop(self):
        if self.use_picamera:
            self._loop_picamera()
        else:
            self._loop_opencv()

    def _loop_picamera(self):
        cam = Picamera2()
        config = cam.create_preview_configuration(
            main={"size": (1280, 720), "format": "RGB888"}
        )
        cam.configure(config)
        cam.start()
        try:
            while self._running:
                frame = cam.capture_array()
                # picamera2 returns RGB — convert to BGR for OpenCV
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                self._process(frame_bgr)
                time.sleep(CAPTURE_INTERVAL)
        finally:
            cam.stop()

    def _loop_opencv(self):
        cap = cv2.VideoCapture(self.camera_index)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        if not cap.isOpened():
            log.error("Could not open camera %d", self.camera_index)
            return
        try:
            while self._running:
                ok, frame = cap.read()
                if not ok:
                    log.warning("Camera read failed — retrying")
                    time.sleep(1)
                    continue
                self._process(frame)
                time.sleep(CAPTURE_INTERVAL)
        finally:
            cap.release()

    def _process(self, frame):
        result = read_plate_from_frame(frame)
        if result is None:
            self._recent.clear()
            return

        plate = result.text
        self._recent[plate] = self._recent.get(plate, 0) + 1

        # Debounce: only fire callback after N consistent reads
        if self._recent[plate] >= DEBOUNCE_READS:
            log.info("Plate confirmed: %s (conf %.0f%%)", plate, result.confidence)
            self._recent.clear()
            if self.save_debug:
                self._save_debug(frame, plate)
            try:
                self.on_plate_detected(plate)
            except Exception as e:
                log.error("on_plate_detected error: %s", e)

    def _save_debug(self, frame, plate: str):
        import os
        os.makedirs(self.debug_dir, exist_ok=True)
        ts = int(time.time())
        path = f"{self.debug_dir}/{plate}_{ts}.jpg"
        if HAS_CV2:
            cv2.imwrite(path, frame)
        log.debug("Debug frame saved: %s", path)


# ── Simulation mode ────────────────────────────────────────────────────────────

class SimulatedRecognizer:
    """
    Fires fake plate detections on a timer — for testing without hardware.
    Usage: SimulatedRecognizer(callback, plates=["MAR12AB","CAR77XZ"], interval=10)
    """

    def __init__(
        self,
        on_plate_detected: Callable[[str], None],
        plates: list[str] = None,
        interval: float = 15.0,
    ):
        self.callback = on_plate_detected
        self.plates = plates or ["MAR12AB", "CAR77XZ", "SAF44TT", "RBA99MK"]
        self.interval = interval
        self._idx = 0
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Simulated plate recognizer started (%.0fs interval)", self.interval)

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            time.sleep(self.interval)
            plate = self.plates[self._idx % len(self.plates)]
            self._idx += 1
            log.info("[SIM] Plate detected: %s", plate)
            try:
                self.callback(plate)
            except Exception as e:
                log.error("Sim callback error: %s", e)
