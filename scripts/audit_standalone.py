"""Write a review-oriented inventory for a built Windows standalone package."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath
from typing import Any


RUNTIME_BINARY_SUFFIXES = {".dll", ".pyd", ".exe", ".so", ".dylib"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink() or path.is_junction():
            relative = path.relative_to(root)
            raise ValueError(f"linked path is not allowed in an audit input: {relative}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError(f"audit input path escapes its root: {path.relative_to(root)}")
        if path.is_file():
            files.append(path)
    return files


def _license_metadata(site_packages: Path) -> list[dict[str, Any]]:
    distributions: list[dict[str, Any]] = []
    for dist_info in sorted(site_packages.glob("*.dist-info"), key=lambda path: path.name.casefold()):
        if dist_info.is_symlink() or dist_info.is_junction():
            raise ValueError(f"linked distribution metadata is not allowed: {dist_info.name}")
        metadata_path = dist_info / "METADATA"
        if not metadata_path.is_file():
            continue
        if metadata_path.is_symlink() or metadata_path.is_junction():
            raise ValueError(f"linked package METADATA is not allowed: {metadata_path}")
        message = BytesParser(policy=default).parsebytes(metadata_path.read_bytes())
        declarations = [
            {"field": field, "value": value}
            for field in ("License-Expression", "License")
            for value in message.get_all(field, [])
            if str(value).strip()
        ]
        license_files: list[dict[str, Any]] = []
        missing_license_files: list[str] = []
        for declared in message.get_all("License-File", []):
            relative = PurePosixPath(str(declared))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe License-File path in {metadata_path}: {declared}")
            candidates = [
                dist_info / "licenses" / Path(*relative.parts),
                dist_info / Path(*relative.parts),
            ]
            target = next((candidate for candidate in candidates if candidate.is_file()), None)
            if target is None:
                missing_license_files.append(str(declared))
                continue
            resolved = target.resolve()
            if not resolved.is_relative_to(dist_info.resolve()):
                raise ValueError(f"License-File resolves outside its distribution: {metadata_path}: {declared}")
            license_files.append(_file_record(resolved, site_packages))
        distributions.append(
            {
                "name": str(message.get("Name", dist_info.name.removesuffix(".dist-info"))),
                "version": str(message.get("Version", "")),
                "license_declarations": declarations,
                "license_files": license_files,
                "missing_license_files": sorted(missing_license_files),
            }
        )
    return distributions


def build_manifest(
    deployment: Path,
    site_packages: Path,
    *,
    python_version: str,
    pyside_version: str,
    nuitka_version: str,
) -> dict[str, Any]:
    deployment = deployment.resolve(strict=True)
    site_packages = site_packages.resolve(strict=True)
    if not deployment.is_dir() or not site_packages.is_dir():
        raise ValueError("deployment and site-packages must be directories")

    files = sorted(_regular_files(deployment), key=lambda path: path.as_posix().casefold())
    executables = [path for path in files if path.name.casefold() == "scidev_client.exe"]
    qml_files = [path for path in files if path.name.casefold() == "developmenttree.qml"]
    if len(executables) != 1:
        raise ValueError(f"expected exactly one scidev_client.exe, found {len(executables)}")
    if not qml_files:
        raise ValueError("standalone package is missing DevelopmentTree.qml")

    runtime_binaries = [
        _file_record(path, deployment)
        for path in files
        if path.suffix.casefold() in RUNTIME_BINARY_SUFFIXES
    ]
    if not any(path["path"].casefold().endswith("qt6core.dll") for path in runtime_binaries):
        raise ValueError("standalone package is missing its Qt6Core runtime library")

    distribution_licenses = _license_metadata(site_packages)
    missing_license_files = [
        {"distribution": package["name"], "files": package["missing_license_files"]}
        for package in distribution_licenses
        if package["missing_license_files"]
    ]
    if missing_license_files:
        raise ValueError(f"installed distribution metadata references missing license files: {missing_license_files}")

    return {
        "package": "SciDevHarness-Windows-x64",
        "python": python_version,
        "pyside6_distribution": "PySide6-Essentials",
        "pyside6": pyside_version,
        "nuitka": nuitka_version,
        "mode": "standalone",
        "executable": _file_record(executables[0], deployment),
        "qml_files": [path.relative_to(deployment).as_posix() for path in qml_files],
        "runtime_binaries": runtime_binaries,
        "build_environment_license_metadata": distribution_licenses,
        "license_review": {
            "status": "manual-review-required",
            "note": "This inventory is evidence for review, not legal clearance or a complete license conclusion.",
        },
        "bundled_file_count": len(files),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--pyside-version", required=True)
    parser.add_argument("--nuitka-version", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(
            args.deployment,
            args.site_packages,
            python_version=args.python_version,
            pyside_version=args.pyside_version,
            nuitka_version=args.nuitka_version,
        )
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"Standalone package audit failed: {exc}", file=sys.stderr)
        return 1
    print(f"Standalone audit manifest: {output}")
    print(
        f"Runtime binaries: {len(manifest['runtime_binaries'])}; "
        f"build distributions: {len(manifest['build_environment_license_metadata'])}"
    )
    print(f"License status: {manifest['license_review']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
