"""LDPlayer/ADB adapter for the provider-independent screenshot contract.

Converts the existing verified ADB screenshot pipeline (VerifiedTarget -> AdbSession) into a generic
`ScreenshotProvider`. The generic layers (providers, vision) receive only `Screenshot`s whose provenance is
`FrameSource("ldplayer-adb", <logical id>)`; the ADB serial appears only as informational `detail` and is never
accepted as input anywhere in the generic contract.

Safety: the adapter can only be built from a `VerifiedTarget`; every capture goes through the AdbSession gate
(emergency stop, proof freshness, device online, serial unchanged) and the PNG/size checks.
"""
from __future__ import annotations

from app.adb.client import AdbClient
from app.adb.session import AdbSession
from app.core.cancel import EmergencyStop
from app.core.errors import TargetNotVerified
from app.instances.mapping import MappingService, VerifiedTarget
from app.ldplayer.ids import PROVIDER_ID  # noqa: F401  (re-exported)
from app.providers.base import Screenshot, ScreenshotProvider, TargetIdentity
from app.providers.target import Capability, require_capability


class LDPlayerScreenshotProvider(ScreenshotProvider):
    def __init__(self, target: VerifiedTarget, adb: AdbClient, mapping: MappingService, stop: EmergencyStop, *,
                 max_age_s: float = 60.0) -> None:
        if not isinstance(target, VerifiedTarget):
            raise TargetNotVerified("The LDPlayer screenshot provider can only be created from a VerifiedTarget.",
                                    reason_code="not_verified_target")
        require_capability(target, Capability.SCREENSHOT)  # the target must declare it, not merely be LDPlayer's
        self._session = AdbSession(target, adb, mapping, stop, max_age_s=max_age_s)
        self._identity: TargetIdentity = target.identity

    @property
    def identity(self) -> TargetIdentity:
        return self._identity

    def capture(self) -> Screenshot:
        shot = self._session.screenshot()  # runs the full safety gate and PNG/size validation
        return self._issue_screenshot(shot.png, shot.width, shot.height, detail=f"adb {shot.serial}")
