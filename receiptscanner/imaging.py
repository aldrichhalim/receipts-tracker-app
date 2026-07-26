"""OpenCV pipeline turning a phone photo of a receipt into a flat B/W scan.

The stages, in order:

  load -> find the page -> crop/straighten -> blank the background
       -> normalise size -> flatten lighting -> denoise -> sharpen
       -> adaptive threshold -> despeckle -> pad

Each stage degrades gracefully: if the page cannot be found the whole frame is
used, if deskew finds no text it is skipped, and so on. A bad photo produces a
worse scan, never an exception.

Parameter choices worth knowing about, all measured against real receipt
photos by OCR keyword recall rather than picked by eye:

* Detection runs on a ~700px copy. At full resolution the printed text carries
  more edge energy than the page border and the outline is never recovered.
* The page is straightened with the minimum-area rectangle of the detected
  blob, not with a four-point perspective warp. Corners recovered from an
  approximated contour are imprecise enough that the warp shears the text and
  costs more accuracy than the perspective it corrects.
* CLAHE is off by default. On flat paper it amplifies grain, which the adaptive
  threshold then renders as speckle that OCR reads as words.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps

try:  # iPhone photos are HEIC by default; optional so the app still runs without it.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # pragma: no cover - depends on optional wheel
    HEIF_SUPPORTED = False

SUPPORTED_EXTENSIONS = [
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
]
if HEIF_SUPPORTED:
    SUPPORTED_EXTENSIONS += [".heic", ".heif"]


class ImageLoadError(RuntimeError):
    pass


@dataclass
class SourceInfo:
    """Everything we know about the photo before touching it."""

    path: Path
    width: int
    height: int
    size_bytes: int
    sha256: str
    exif_datetime: str | None = None
    camera_make: str | None = None
    camera_model: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "source_width": self.width,
            "source_height": self.height,
            "source_bytes": self.size_bytes,
            "source_sha256": self.sha256,
            "exif_datetime": self.exif_datetime,
            "camera_make": self.camera_make,
            "camera_model": self.camera_model,
        }


@dataclass
class PageDetection:
    """Where the receipt is in the frame."""

    quad: np.ndarray  # 4x2, full-resolution coordinates
    mask: np.ndarray  # full-resolution uint8, 255 on paper
    area_ratio: float
    contrast: float


@dataclass
class ScanResult:
    original_bgr: np.ndarray
    scanned: np.ndarray  # single channel, 0/255
    source: SourceInfo
    document_detected: bool = False
    deskew_angle: float = 0.0
    stages: list[str] = field(default_factory=list)


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _exif_fields(image: Image.Image) -> tuple[str | None, str | None, str | None]:
    try:
        exif = image.getexif()
    except Exception:
        return None, None, None
    if not exif:
        return None, None, None

    # 36867 DateTimeOriginal, 306 DateTime, 271 Make, 272 Model
    raw_dt = exif.get(36867) or exif.get(306)
    taken = None
    if isinstance(raw_dt, str):
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                taken = datetime.strptime(raw_dt.strip(), fmt).isoformat(sep=" ")
                break
            except ValueError:
                continue
    make = exif.get(271)
    model = exif.get(272)
    return (
        taken,
        str(make).strip() or None if make else None,
        str(model).strip() or None if model else None,
    )


def load_image(path: Path) -> tuple[np.ndarray, SourceInfo]:
    """Load via Pillow so EXIF rotation and HEIC are handled, hand back BGR."""
    path = Path(path)
    if not path.is_file():
        raise ImageLoadError(f"File not found: {path}")

    try:
        with Image.open(path) as image:
            exif_dt, make, model = _exif_fields(image)
            image = ImageOps.exif_transpose(image)
            rgb = image.convert("RGB")
            array = np.asarray(rgb)
    except ImageLoadError:
        raise
    except Exception as exc:
        raise ImageLoadError(f"Unreadable image {path.name}: {exc}") from exc

    if array.size == 0:
        raise ImageLoadError(f"Empty image: {path.name}")

    bgr = cv2.cvtColor(array, cv2.COLOR_RGB2BGR)
    info = SourceInfo(
        path=path,
        width=int(bgr.shape[1]),
        height=int(bgr.shape[0]),
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
        exif_datetime=exif_dt,
        camera_make=make,
        camera_model=model,
    )
    return bgr, info


# --------------------------------------------------------------------------
# Page detection
# --------------------------------------------------------------------------


def _order_quad(points: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    points = points.reshape(4, 2).astype(np.float32)
    ordered = np.zeros((4, 2), dtype=np.float32)
    total = points.sum(axis=1)
    ordered[0] = points[np.argmin(total)]
    ordered[2] = points[np.argmax(total)]
    diff = np.diff(points, axis=1).ravel()
    ordered[1] = points[np.argmin(diff)]
    ordered[3] = points[np.argmax(diff)]
    return ordered


def _candidates_from_mask(
    mask: np.ndarray, top_n: int = 2
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Largest blobs in `mask` as (quad, contour) pairs."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found: list[tuple[np.ndarray, np.ndarray]] = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:top_n]:
        hull = cv2.convexHull(contour)
        perimeter = cv2.arcLength(hull, True)
        if perimeter <= 0:
            continue
        # Relax the tolerance until the hull collapses to exactly four corners.
        for epsilon in np.arange(0.010, 0.10, 0.005):
            approx = cv2.approxPolyDP(hull, float(epsilon) * perimeter, True)
            if len(approx) == 4:
                found.append((approx.reshape(4, 2).astype(np.float32), contour))
                break
    return found


def _area_ratio(
    quad: np.ndarray, shape: tuple[int, int], min_ratio: float
) -> float | None:
    """Area ratio if the quad is geometrically page-like, else None."""
    height, width = shape
    image_area = float(height * width)
    if image_area <= 0:
        return None

    ordered = _order_quad(quad)
    ratio = float(cv2.contourArea(ordered)) / image_area
    if ratio < min_ratio or ratio > 0.92:
        return None

    sides = [float(np.linalg.norm(ordered[(i + 1) % 4] - ordered[i])) for i in range(4)]
    if min(sides) < 0.05 * min(height, width):
        return None

    for index in range(4):
        previous = ordered[index - 1] - ordered[index]
        following = ordered[(index + 1) % 4] - ordered[index]
        norms = np.linalg.norm(previous) * np.linalg.norm(following)
        if norms == 0:
            return None
        cosine = float(np.dot(previous, following) / norms)
        angle = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))
        if not 45.0 <= angle <= 135.0:
            return None  # too skewed to be a rectangle seen through a lens

    # A quad pinned to three or more image edges is the whole frame, not a page.
    x, y, box_width, box_height = cv2.boundingRect(ordered.astype(np.int32))
    margin = max(2, int(0.01 * max(height, width)))
    touching = sum(
        (
            x <= margin,
            y <= margin,
            x + box_width >= width - margin,
            y + box_height >= height - margin,
        )
    )
    if touching >= 3:
        return None

    return ratio


def _paper_contrast(quad: np.ndarray, gray: np.ndarray) -> float:
    """How much brighter the quad's interior is than the band just outside it.

    Paper is lighter than whatever it is lying on; a region no brighter than
    its surroundings is a piece of background, not a receipt. Ranking on area
    alone reliably picks the table the receipt is lying on.
    """
    height, width = gray.shape[:2]
    inside = np.zeros((height, width), np.uint8)
    cv2.fillPoly(inside, [_order_quad(quad).astype(np.int32)], 255)

    band = max(5, int(0.04 * max(height, width)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (band, band))
    ring = cv2.subtract(cv2.dilate(inside, kernel), inside)

    if cv2.countNonZero(inside) == 0 or cv2.countNonZero(ring) < 200:
        return -1e9
    return float(gray[inside > 0].mean() - gray[ring > 0].mean())


def _edge_candidates(small: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    median = float(np.median(gray))
    edges = cv2.Canny(gray, int(max(0, 0.66 * median)), int(min(255, 1.33 * median)))
    # A wide close bridges the gaps a shadow leaves in the page outline.
    edges = cv2.morphologyEx(
        edges, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    )
    return _candidates_from_mask(edges, top_n=4)


def _saturation_candidates(small: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """Paper is close to neutral; most surfaces it sits on are not."""
    saturation = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1]
    _, mask = cv2.threshold(saturation, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    ellipse = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, ellipse, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, ellipse, iterations=3)
    return _candidates_from_mask(mask)


def _brightness_candidates(small: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """Sweep several lightness cut-offs, keeping the biggest bright blob at each.

    No single global threshold serves every scene, so the sweep produces a
    family of nested regions and the paper-contrast score picks the cut-off
    that isolates the page: too low bleeds into the background and kills the
    contrast term, too high eats the page and kills the area term.
    """
    lightness = cv2.cvtColor(small, cv2.COLOR_BGR2LAB)[:, :, 0]
    lightness = cv2.GaussianBlur(lightness, (7, 7), 0)
    ellipse = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))

    found: list[tuple[np.ndarray, np.ndarray]] = []
    for percentile in (62, 70, 78, 85):
        threshold = float(np.percentile(lightness, percentile))
        mask = (lightness >= threshold).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, ellipse, iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, ellipse, iterations=3)
        found.extend(_candidates_from_mask(mask))
    return found


def detect_document(bgr: np.ndarray, options: dict[str, Any]) -> PageDetection | None:
    """Locate the receipt. Returns the straightening quad plus a paper mask."""
    height, width = bgr.shape[:2]
    max_dim = max(200, int(options.get("detect_max_dim", 700)))
    scale = min(1.0, max_dim / max(height, width, 1))
    small = (
        cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else bgr
    )
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    min_ratio = float(options.get("min_document_area_ratio", 0.08))
    min_contrast = float(options.get("min_page_contrast", 4.0))

    best: tuple[float, np.ndarray, float, float] | None = None
    for strategy in (_brightness_candidates, _edge_candidates, _saturation_candidates):
        try:
            candidates = strategy(small)
        except cv2.error:
            continue
        for quad, contour in candidates:
            ratio = _area_ratio(quad, small.shape[:2], min_ratio)
            if ratio is None:
                continue
            contrast = _paper_contrast(quad, gray)
            if contrast < min_contrast:
                continue
            score = contrast * (ratio**0.5)
            if best is None or score > best[0]:
                best = (score, contour, ratio, contrast)

    if best is None:
        return None

    _, contour, ratio, contrast = best

    # Straighten with the minimum-area rectangle rather than the approximated
    # corners: it cannot shear the text, only rotate and crop.
    (cx, cy), (rect_w, rect_h), angle = cv2.minAreaRect(contour)
    padding = 1.0 + float(options.get("crop_padding", 0.02))
    box = cv2.boxPoints(
        (
            (cx / scale, cy / scale),
            (rect_w * padding / scale, rect_h * padding / scale),
            angle,
        )
    ).astype(np.float32)

    mask_small = np.zeros(small.shape[:2], np.uint8)
    cv2.drawContours(mask_small, [cv2.convexHull(contour)], -1, 255, -1)
    mask = cv2.resize(mask_small, (width, height), interpolation=cv2.INTER_NEAREST)

    return PageDetection(
        quad=_order_quad(box), mask=mask, area_ratio=ratio, contrast=contrast
    )


def perspective_matrix(quad: np.ndarray) -> tuple[np.ndarray, int, int]:
    ordered = _order_quad(quad)
    tl, tr, br, bl = ordered
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    width, height = max(width, 1), max(height, 1)

    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    return (
        cv2.getPerspectiveTransform(ordered.astype(np.float32), destination),
        width,
        height,
    )


def four_point_transform(bgr: np.ndarray, quad: np.ndarray) -> np.ndarray:
    matrix, width, height = perspective_matrix(quad)
    if width < 32 or height < 32:
        return bgr
    return cv2.warpPerspective(bgr, matrix, (width, height), flags=cv2.INTER_CUBIC)


# --------------------------------------------------------------------------
# Enhancement
# --------------------------------------------------------------------------


def estimate_skew(gray: np.ndarray, max_angle: float) -> float:
    """Small-angle skew from the dominant text block orientation."""
    inverted = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    # Smear characters into lines so the min-area rect follows the baselines.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3))
    smeared = cv2.morphologyEx(inverted, cv2.MORPH_CLOSE, kernel)

    coords = cv2.findNonZero(smeared)
    if coords is None or len(coords) < 50:
        return 0.0

    angle = cv2.minAreaRect(coords)[-1]
    if angle > 45:
        angle -= 90
    elif angle < -45:
        angle += 90
    return angle if abs(angle) <= max_angle else 0.0


def rotate(image: np.ndarray, angle: float, border: int = 255) -> np.ndarray:
    height, width = image.shape[:2]
    center = (width / 2, height / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)

    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_width = int(height * sin + width * cos)
    new_height = int(height * cos + width * sin)
    matrix[0, 2] += new_width / 2 - center[0]
    matrix[1, 2] += new_height / 2 - center[1]

    return cv2.warpAffine(
        image,
        matrix,
        (new_width, new_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border,
    )


def flatten_lighting(gray: np.ndarray, kernel_size: int) -> np.ndarray:
    """Divide out the background to erase shadows and uneven exposure."""
    kernel_size = max(3, int(kernel_size) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    background = cv2.GaussianBlur(background, (0, 0), kernel_size / 3.0)
    normalized = cv2.divide(gray, background, scale=255)
    return np.clip(normalized, 0, 255).astype(np.uint8)


def denoise(gray: np.ndarray, method: str) -> np.ndarray:
    """Suppress paper grain and sensor noise before thresholding.

    Without this the adaptive threshold turns grain into speckle, and OCR reads
    the speckle as words: on the sample photos it produced ~830 "words" per
    receipt instead of ~160.
    """
    method = (method or "none").lower()
    if method == "bilateral":
        return cv2.bilateralFilter(gray, 7, 45, 45)
    if method == "median":
        return cv2.medianBlur(gray, 3)
    if method in ("nlm", "nlmeans"):
        return cv2.fastNlMeansDenoising(gray, None, 7, 7, 21)
    return gray


def unsharp(gray: np.ndarray, amount: float) -> np.ndarray:
    if amount <= 0:
        return gray
    blurred = cv2.GaussianBlur(gray, (0, 0), 1.2)
    return cv2.addWeighted(gray, 1 + amount, blurred, -amount, 0)


def remove_speckles(binary: np.ndarray, min_area: int) -> np.ndarray:
    """Drop ink blobs smaller than min_area (binary is black text on white)."""
    if min_area <= 0:
        return binary
    ink = cv2.bitwise_not(binary)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    if count <= 1:
        return binary

    keep = np.zeros(count, dtype=bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= min_area
    keep[0] = False
    cleaned_ink = np.where(keep[labels], 255, 0).astype(np.uint8)
    return cv2.bitwise_not(cleaned_ink)


def binarize(gray: np.ndarray, options: dict[str, Any]) -> np.ndarray:
    block = max(3, int(options.get("adaptive_block_size", 51)) | 1)
    constant = float(options.get("adaptive_c", 13))
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, constant
    )
    # If the page came out mostly dark the polarity is inverted (e.g. a photo of
    # a screen); flip so Tesseract always sees black text on white.
    if float(np.mean(binary)) < 110:
        binary = cv2.bitwise_not(binary)
    return binary


def scan(bgr: np.ndarray, source: SourceInfo, options: dict[str, Any]) -> ScanResult:
    stages: list[str] = []
    working = bgr
    paper_mask: np.ndarray | None = None
    detected = False

    if options.get("detect_document", True):
        detection = detect_document(bgr, options)
        if detection is not None:
            matrix, width, height = perspective_matrix(detection.quad)
            if width >= 32 and height >= 32:
                working = cv2.warpPerspective(
                    bgr, matrix, (width, height), flags=cv2.INTER_CUBIC
                )
                paper_mask = cv2.warpPerspective(
                    detection.mask, matrix, (width, height), flags=cv2.INTER_NEAREST
                )
                detected = True
                stages.append(f"page found ({detection.area_ratio:.0%} of frame)")
        if not detected:
            stages.append("no page found, using full frame")

    gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)

    if paper_mask is not None and options.get("mask_background", True):
        # Blank whatever the crop caught around the paper, so table texture is
        # never thresholded into ink. Grown slightly first: the mask traces the
        # bright area, which can sit just inside print that runs to the edge.
        grow = (
            max(3, int(float(options.get("mask_grow_ratio", 0.004)) * max(gray.shape)))
            | 1
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow))
        grown = cv2.dilate(paper_mask, kernel)
        gray = np.where(grown > 0, gray, 255).astype(np.uint8)
        stages.append("background blanked")

    angle = 0.0
    if options.get("deskew", True):
        angle = estimate_skew(gray, float(options.get("max_deskew_angle", 15.0)))
        if abs(angle) > 0.2:
            gray = rotate(gray, angle)
            stages.append(f"deskewed {angle:+.2f}deg")

    # Normalise the working size. Downscaling averages away paper grain, and
    # both directions land text at a height Tesseract handles well. Only ever
    # enlarge when the page was not found, since then the receipt is a small
    # part of the frame and shrinking would destroy the text.
    target_width = int(options.get("target_ocr_width", 1300))
    if target_width > 0 and gray.shape[1] != target_width:
        if detected or gray.shape[1] < target_width:
            factor = target_width / gray.shape[1]
            gray = cv2.resize(
                gray,
                None,
                fx=factor,
                fy=factor,
                interpolation=cv2.INTER_AREA if factor < 1 else cv2.INTER_CUBIC,
            )
            stages.append(f"resized x{factor:.2f}")

    gray = flatten_lighting(gray, options.get("shadow_kernel", 31))
    stages.append("lighting flattened")

    method = str(options.get("denoise", "bilateral"))
    if method.lower() not in ("", "none"):
        gray = denoise(gray, method)
        stages.append(f"denoised ({method})")

    clip = float(options.get("clahe_clip", 0.0))
    if clip > 0:
        grid = max(1, int(options.get("clahe_grid", 8)))
        gray = cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid)).apply(gray)
        stages.append("contrast equalised")

    gray = unsharp(gray, float(options.get("unsharp_amount", 0.8)))

    binary = binarize(gray, options)
    stages.append("adaptive threshold")

    binary = remove_speckles(binary, int(options.get("min_blob_area", 25)))
    stages.append("despeckled")

    border = int(options.get("border_px", 24))
    if border > 0:
        binary = cv2.copyMakeBorder(
            binary, border, border, border, border, cv2.BORDER_CONSTANT, value=255
        )

    return ScanResult(
        original_bgr=bgr,
        scanned=binary,
        source=source,
        document_detected=detected,
        deskew_angle=angle,
        stages=stages,
    )


def write_image(image: np.ndarray, path: Path) -> None:
    """Encode in memory then write bytes, so non-ASCII paths work everywhere."""
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower() or ".png"
    params: list[int] = []
    if suffix in (".jpg", ".jpeg"):
        params = [int(cv2.IMWRITE_JPEG_QUALITY), 95]
    elif suffix == ".png":
        params = [int(cv2.IMWRITE_PNG_COMPRESSION), 6]

    ok, buffer = cv2.imencode(suffix, image, params)
    if not ok:
        raise OSError(f"Could not encode image as {suffix}")
    path.write_bytes(buffer.tobytes())


def to_pil(image: np.ndarray) -> Image.Image:
    if image.ndim == 2:
        return Image.fromarray(image, mode="L")
    return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
