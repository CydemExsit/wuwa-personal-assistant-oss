"""Small-ROI diagnostics with an explicit full-frame write guard."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from wuwa_assistant.models import to_jsonable

MAX_DEBUG_WIDTH = 1000
MAX_DEBUG_HEIGHT = 800


@dataclass(slots=True)
class DebugWriter:
    root: Path | None

    def _source_dir(self, source_file: str | Path) -> Path | None:
        if self.root is None:
            return None
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(source_file).stem)
        target = self.root.resolve() / stem
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_roi(
        self,
        source_file: str | Path,
        roi_name: str,
        image: NDArray[np.uint8],
        *,
        save_threshold: bool = True,
    ) -> list[str]:
        """Save a bounded crop, never a monitor-sized frame."""

        height, width = image.shape[:2]
        if width > MAX_DEBUG_WIDTH or height > MAX_DEBUG_HEIGHT:
            raise ValueError(
                f"Refusing to persist oversized debug image {width}x{height}; "
                "only small ROIs are allowed"
            )
        target_dir = self._source_dir(source_file)
        if target_dir is None:
            return []

        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", roi_name)
        original_path = target_dir / f"{safe_name}.png"
        if not cv2.imwrite(str(original_path), image):
            raise OSError(f"Could not write debug ROI: {original_path}")
        paths = [str(original_path.resolve())]

        if save_threshold:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
            _, threshold = cv2.threshold(clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            threshold_path = target_dir / f"{safe_name}_threshold.png"
            if not cv2.imwrite(str(threshold_path), threshold):
                raise OSError(f"Could not write threshold ROI: {threshold_path}")
            paths.append(str(threshold_path.resolve()))
        return paths

    def save_json(
        self,
        source_file: str | Path,
        name: str,
        payload: Any,
    ) -> str | None:
        target_dir = self._source_dir(source_file)
        if target_dir is None:
            return None
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        target = target_dir / f"{safe_name}.json"
        target.write_text(
            json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(target.resolve())
