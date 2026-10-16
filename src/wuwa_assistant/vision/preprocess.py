"""OpenCV-only ROI cropping and offline OCR preprocessing helpers."""

from __future__ import annotations

from typing import Literal, cast

import cv2
import numpy as np
from numpy.typing import NDArray

from .rois import REFERENCE_HEIGHT, REFERENCE_WIDTH, Roi

Image = NDArray[np.uint8]
ThresholdMode = Literal["otsu", "adaptive"]


def validate_image(image: Image) -> None:
    """Validate a grayscale, BGR, or BGRA uint8 OpenCV image."""

    if not isinstance(image, np.ndarray):
        raise TypeError("image must be a NumPy array")
    if image.dtype != np.uint8:
        raise TypeError(f"image dtype must be uint8, got {image.dtype}")
    if image.ndim == 2:
        return
    if image.ndim == 3 and image.shape[2] in (1, 3, 4):
        return
    raise ValueError(f"unsupported image shape {image.shape!r}; expected HxW, HxWx3, or HxWx4")


def validate_reference_frame(frame: Image) -> None:
    """Require the fixed 2560 x 1440 phase-one screenshot layout."""

    validate_image(frame)
    height, width = frame.shape[:2]
    if (width, height) != (REFERENCE_WIDTH, REFERENCE_HEIGHT):
        raise ValueError(
            f"fixed ROIs require a {REFERENCE_WIDTH}x{REFERENCE_HEIGHT} frame; got {width}x{height}"
        )


def crop_roi(
    frame: Image,
    roi: Roi,
    *,
    require_reference_size: bool = True,
    copy: bool = True,
) -> Image:
    """Crop one ROI without retaining or writing the full source frame."""

    if require_reference_size:
        validate_reference_frame(frame)
    else:
        validate_image(frame)
        height, width = frame.shape[:2]
        if roi.x2 > width or roi.y2 > height:
            raise ValueError(f"ROI {roi.name!r} exceeds the supplied {width}x{height} image")

    return cast(Image, roi.crop(frame, copy=copy))


def to_grayscale(image: Image) -> Image:
    """Return an 8-bit grayscale copy for any supported OpenCV channel layout."""

    validate_image(image)
    if image.ndim == 2:
        return image.copy()
    channels = image.shape[2]
    if channels == 1:
        return image[:, :, 0].copy()
    conversion = cv2.COLOR_BGRA2GRAY if channels == 4 else cv2.COLOR_BGR2GRAY
    return cast(Image, cv2.cvtColor(image, conversion))


def enhance_grayscale(
    image: Image,
    *,
    clip_limit: float = 2.0,
    tile_grid_size: tuple[int, int] = (8, 8),
) -> Image:
    """Apply local contrast enhancement followed by a mild unsharp mask."""

    if clip_limit <= 0:
        raise ValueError("clip_limit must be positive")
    gray = to_grayscale(image)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid_size)
    contrasted = cast(Image, clahe.apply(gray))
    blurred = cast(Image, cv2.GaussianBlur(contrasted, (0, 0), sigmaX=0.8))
    return cast(Image, cv2.addWeighted(contrasted, 1.45, blurred, -0.45, 0))


def upscale_for_ocr(image: Image, *, scale: float = 3.0) -> Image:
    """Upscale a small text ROI with a high-quality interpolation kernel."""

    validate_image(image)
    if scale < 1.0:
        raise ValueError("OCR scale must be at least 1.0")
    if scale == 1.0:
        return image.copy()
    return cast(
        Image,
        cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4),
    )


def threshold_text(image: Image, *, mode: ThresholdMode = "otsu") -> Image:
    """Create a white-foreground text mask from an already enhanced grayscale ROI."""

    gray = to_grayscale(image)
    if mode == "otsu":
        _threshold, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        return cast(Image, binary)
    if mode == "adaptive":
        # Negative C keeps pixels only when they are brighter than their local background.
        return cast(
            Image,
            cv2.adaptiveThreshold(
                gray,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,
                -3,
            ),
        )
    raise ValueError(f"unknown threshold mode {mode!r}")


def preprocess_text_roi(
    image: Image,
    *,
    scale: float = 3.0,
    threshold_mode: ThresholdMode = "otsu",
) -> Image:
    """Prepare a Traditional Chinese text ROI for an offline OCR engine."""

    enhanced = enhance_grayscale(image)
    binary = threshold_text(enhanced, mode=threshold_mode)
    return upscale_for_ocr(binary, scale=scale)


def preprocess_numeric_roi(image: Image, *, scale: float = 3.0) -> Image:
    """Prepare a numeric/percentage ROI; character whitelisting belongs to the OCR adapter."""

    return preprocess_text_roi(image, scale=scale, threshold_mode="otsu")


def low_saturation_text_mask(image: Image) -> Image:
    """Mask white and gray UI text while rejecting most of the blue star field."""

    validate_image(image)
    if image.ndim == 2 or (image.ndim == 3 and image.shape[2] == 1):
        gray = to_grayscale(image)
        return cast(Image, cv2.inRange(gray, 140, 255))
    bgr = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR) if image.shape[2] == 4 else image
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cast(
        Image,
        cv2.inRange(
            hsv,
            np.array((0, 0, 140), np.uint8),
            np.array((179, 78, 255), np.uint8),
        ),
    )


def gold_text_mask(image: Image) -> Image:
    """Mask the cream/gold echo-name text used by the fixed UI theme."""

    validate_image(image)
    if image.ndim == 2 or (image.ndim == 3 and image.shape[2] == 1):
        raise ValueError("gold text masking requires a BGR or BGRA image")
    bgr = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR) if image.shape[2] == 4 else image
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cast(
        Image,
        cv2.inRange(
            hsv,
            np.array((12, 25, 150), np.uint8),
            np.array((45, 190, 255), np.uint8),
        ),
    )


def ocr_preprocessing_variants(image: Image, *, scale: float = 3.0) -> dict[str, Image]:
    """Return independent candidates so the parser can reject disagreeing OCR results."""

    enhanced = enhance_grayscale(image)
    return {
        "enhanced_gray": upscale_for_ocr(enhanced, scale=scale),
        "otsu": upscale_for_ocr(threshold_text(enhanced, mode="otsu"), scale=scale),
        "adaptive": upscale_for_ocr(threshold_text(enhanced, mode="adaptive"), scale=scale),
        "low_saturation": upscale_for_ocr(low_saturation_text_mask(image), scale=scale),
    }


def preprocess_for_hash(image: Image) -> Image:
    """Suppress smooth animation/background changes before semantic pHash calculation."""

    enhanced = enhance_grayscale(image)
    binary = threshold_text(enhanced, mode="otsu")
    # A small opening removes isolated star particles while preserving UI glyph strokes.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    return cast(Image, cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel))
