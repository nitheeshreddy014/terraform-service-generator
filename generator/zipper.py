"""
zipper.py
---------
Zips a generated service folder into a single .zip archive and saves it
under the `outputs/` directory so FastAPI can serve it as a file download.
"""

from __future__ import annotations

import zipfile
from pathlib import Path


def zip_service_folder(service_root: Path, output_dir: Path) -> Path:
    """
    Recursively zip *service_root* and write the archive to *output_dir*.

    Returns the Path of the created .zip file.

    Parameters
    ----------
    service_root : Path
        The top-level service folder to compress
        (e.g. /tmp/tfgen_xyz/s3).
    output_dir : Path
        Directory where the zip will be written
        (e.g. ./outputs).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    zip_name   = f"{service_root.name}.zip"
    zip_path   = output_dir / zip_name

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(service_root.rglob("*")):
            # arcname keeps the top-level folder name inside the zip
            arcname = file_path.relative_to(service_root.parent)
            zf.write(file_path, arcname)

    return zip_path
