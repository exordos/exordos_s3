import pytest

from exordos_s3 import version_ref


@pytest.mark.parametrize("image", ["urn:images:1234", "https://host:8443/image.raw"])
def test_version_reference_round_trip(image):
    ref = version_ref.build_version_ref("version-id", image)
    assert version_ref.parse_disk_image(ref) == image


@pytest.mark.parametrize(
    "ref", ["", "urn:exordos:s3::image", "urn:exordos:other:uuid:image"]
)
def test_rejects_invalid_version_reference(ref):
    with pytest.raises(ValueError):
        version_ref.parse_disk_image(ref)


def test_version_insert_and_update_rebuild_reference():
    from unittest import mock
    from gcl_sdk.audit.dm import models as audit_models
    from exordos_s3.controlplane.dm import models

    version = models.S3Version(name="rustfs", image="urn:images:old")
    with (
        mock.patch.object(audit_models.AuditLogSQLStorableMixin, "insert"),
        mock.patch.object(audit_models.AuditLogSQLStorableMixin, "update"),
    ):
        version.insert()
        assert version.version_ref == version_ref.build_version_ref(
            str(version.uuid), "urn:images:old"
        )
        version.image = "https://example.org/new"
        version.update()
        assert version.version_ref == version_ref.build_version_ref(
            str(version.uuid), version.image
        )
