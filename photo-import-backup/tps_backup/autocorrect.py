from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageStat


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb", buffering=0) as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sample_means(image: Image.Image) -> tuple[float, float, float]:
    sample = image.copy()
    sample.thumbnail((640, 640), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(sample)
    vals = stat.mean[:3]
    return float(vals[0]), float(vals[1]), float(vals[2])


def _apply_gentle_white_balance(image: Image.Image, event_code: str) -> Image.Image:
    # Preserve sunset warmth. For other events, only make very small channel corrections.
    if (event_code or "").upper() in {"SUN", "SUNSET"}:
        return image
    r_mean, g_mean, b_mean = _sample_means(image)
    target = max(1.0, (r_mean + g_mean + b_mean) / 3.0)
    gains = (
        _clamp(target / max(1.0, r_mean), 0.95, 1.05),
        _clamp(target / max(1.0, g_mean), 0.95, 1.05),
        _clamp(target / max(1.0, b_mean), 0.95, 1.05),
    )
    channels = image.split()
    adjusted = []
    for channel, gain in zip(channels[:3], gains):
        lut = [min(255, max(0, int(round(i * gain)))) for i in range(256)]
        adjusted.append(channel.point(lut))
    return Image.merge("RGB", adjusted)


def auto_correct_jpeg(source: Path, destination: Path, event_code: str = "") -> dict[str, Any]:
    """Create a restrained working JPEG while preserving the original's dimensions and metadata.

    The source is never modified. Orientation metadata is left intact because colour/exposure
    correction is independent of orientation and this avoids rewriting camera orientation tags.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".autocorrecting")
    if partial.exists():
        partial.unlink()

    with Image.open(source) as opened:
        if (opened.format or "").upper() not in {"JPEG", "JPG"}:
            raise ValueError(f"Not a JPEG: {source.name}")
        original_size = opened.size
        exif_bytes = opened.info.get("exif")
        icc_profile = opened.info.get("icc_profile")
        dpi = opened.info.get("dpi")
        image = opened.convert("RGB")

    # Gentle colour correction.
    image = _apply_gentle_white_balance(image, event_code)

    # Gentle exposure correction. Avoid dramatic changes so tour sets stay natural.
    sample = image.copy()
    sample.thumbnail((640, 640), Image.Resampling.BILINEAR)
    mean_luma = float(ImageStat.Stat(sample.convert("L")).mean[0])
    target_luma = 122.0
    exposure = _clamp(target_luma / max(1.0, mean_luma), 0.94, 1.08)
    if (event_code or "").upper() in {"SUN", "SUNSET"}:
        exposure = _clamp(exposure, 0.97, 1.05)
    image = ImageEnhance.Brightness(image).enhance(exposure)
    image = ImageEnhance.Contrast(image).enhance(1.04)
    image = ImageEnhance.Color(image).enhance(1.02)

    save_kwargs: dict[str, Any] = {
        "format": "JPEG",
        "quality": 95,
        "subsampling": 0,
        "optimize": False,
    }
    if exif_bytes:
        save_kwargs["exif"] = exif_bytes
    if icc_profile:
        save_kwargs["icc_profile"] = icc_profile
    if dpi:
        save_kwargs["dpi"] = dpi

    image.save(partial, **save_kwargs)

    # Validate that the corrected file is readable and has identical pixel dimensions.
    with Image.open(partial) as check:
        check.verify()
    with Image.open(partial) as check:
        if check.size != original_size:
            partial.unlink(missing_ok=True)
            raise ValueError(f"Corrected image dimensions changed: {source.name}")

    partial.replace(destination)
    return {
        "size": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "width": original_size[0],
        "height": original_size[1],
        "exposure_factor": round(exposure, 4),
    }
