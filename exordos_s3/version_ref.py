# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""S3 version references, compatible with the observability URN format."""


def build_version_ref(uuid: str, image: str) -> str:
    image = image.removeprefix("urn:images:")
    return f"urn:exordos:s3:{uuid}:{image}"


def parse_disk_image(version_ref: str) -> str:
    parts = version_ref.split(":", 4)
    if len(parts) != 5 or parts[:3] != ["urn", "exordos", "s3"] or not all(parts[3:]):
        raise ValueError(f"Invalid S3 version reference: {version_ref!r}")
    image = parts[4]
    return image if image.startswith(("http://", "https://")) else f"urn:images:{image}"
