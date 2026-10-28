from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from time import monotonic

from PIL import Image, ImageOps

from app.services.reading_types import ReadingSettings
from app.workers.jobs import ProcessingError


class TesseractOCR:
    """Run Tesseract with bounded execution time and file-based output."""

    def __init__(self, settings: ReadingSettings) -> None:
        self.settings = settings

    def __call__(self, image: Image.Image) -> str:
        width, height = image.size
        if width < 1 or height < 1:
            raise ProcessingError(
                "invalid_image",
                "The image dimensions are invalid.",
                retryable=False,
            )

        if width * height > self.settings.max_image_pixels:
            raise ProcessingError(
                "image_pixel_limit",
                "The image exceeds the configured pixel limit.",
                retryable=False,
            )

        with tempfile.TemporaryDirectory(prefix="document-ocr-") as directory:
            root = Path(directory)
            input_path = root / "input.png"
            output_base = root / "result"
            output_path = root / "result.txt"
            error_path = root / "stderr.log"

            with ImageOps.exif_transpose(image) as oriented:
                if "A" in oriented.getbands() or "transparency" in oriented.info:
                    with oriented.convert("RGBA") as rgba:
                        with Image.new("RGBA", rgba.size, "white") as background:
                            background.alpha_composite(rgba)
                            grayscale = background.convert("L")
                else:
                    grayscale = oriented.convert("L")

                try:
                    if self.settings.preprocess_images:
                        with ImageOps.autocontrast(grayscale) as prepared:
                            prepared.save(input_path, format="PNG")
                    else:
                        grayscale.save(input_path, format="PNG")
                finally:
                    grayscale.close()

            self._run(input_path, output_base, output_path, error_path)

            if not output_path.is_file():
                raise ProcessingError(
                    "ocr_output_missing",
                    "Tesseract did not produce a text result.",
                    retryable=True,
                )

            try:
                return output_path.read_text(encoding="utf-8").strip()
            except UnicodeError as error:
                raise ProcessingError(
                    "invalid_ocr_output",
                    "Tesseract returned text with an unsupported encoding.",
                    retryable=False,
                ) from error

    def _check_output_limits(
        self,
        output_path: Path,
        error_path: Path,
    ) -> None:
        for path, maximum in (
            (output_path, self.settings.max_ocr_output_bytes),
            (error_path, 1024 * 1024),
        ):
            if path.exists() and path.stat().st_size > maximum:
                raise ProcessingError(
                    "ocr_output_limit",
                    "Tesseract output exceeded the configured limit.",
                    retryable=False,
                )

    def _run(
        self,
        input_path: Path,
        output_base: Path,
        output_path: Path,
        error_path: Path,
    ) -> None:
        arguments = [
            self.settings.tesseract_cmd,
            str(input_path),
            str(output_base),
            "-l",
            self.settings.ocr_language,
            "--psm",
            str(self.settings.ocr_psm),
            "--dpi",
            str(self.settings.pdf_dpi),
            "txt",
        ]

        environment = os.environ.copy()
        environment["OMP_THREAD_LIMIT"] = "1"

        creation_flags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        )

        with error_path.open("wb") as error_output:
            try:
                process = subprocess.Popen(
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=error_output,
                    shell=False,
                    env=environment,
                    creationflags=creation_flags,
                )
            except FileNotFoundError as error:
                raise ProcessingError(
                    "ocr_unavailable",
                    "Tesseract is not installed or its executable path is invalid.",
                    retryable=False,
                ) from error
            except OSError as error:
                raise ProcessingError(
                    "ocr_start_failed",
                    "The Tesseract process could not be started.",
                    retryable=False,
                ) from error

            try:
                deadline = monotonic() + self.settings.ocr_timeout_seconds

                while process.poll() is None:
                    self._check_output_limits(output_path, error_path)

                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise ProcessingError(
                            "ocr_timeout",
                            "Tesseract exceeded the configured time limit.",
                            retryable=True,
                        )

                    try:
                        process.wait(timeout=min(0.1, remaining))
                    except subprocess.TimeoutExpired:
                        continue

                self._check_output_limits(output_path, error_path)

                if process.returncode != 0:
                    raise ProcessingError(
                        "ocr_failed",
                        "Tesseract failed. Check language data and the source image.",
                        retryable=True,
                    )
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()