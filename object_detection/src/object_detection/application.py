"""Object detection over the camera apps' published snapshots.

The app is entirely event-driven: it subscribes to each configured camera app's
channel, and every time that camera publishes a snapshot message it fetches the
attached image, runs the enabled detectors, and **edits that message in place** with
the findings and an annotated copy — so a frame and its analysis are one timeline entry
rather than two a reader has to pair up.

Running here rather than in the camera app is deliberate: the models are shared, so
one instance can serve every camera on a Doovit and load one copy of each model,
which matters on a device with well under a gigabyte of RAM to spare.

The inference itself lives in ``common``, shared verbatim with the cloud processor
variant (``object_detection_processor``) so the two can't drift apart.
"""

import asyncio
import logging
from datetime import datetime, timezone

from common import annotate as annotate_mod
from common.detectors import anpr as anpr_mod
from common.detectors import ppe as ppe_mod
from pydoover.docker import Application
from pydoover.models import (
    EventSubscription,
    File,
    MessageCreateEvent,
    NotificationSeverity,
)

from .app_config import ObjectDetectionConfig
from .app_tags import ObjectDetectionTags

log = logging.getLogger()

# The camera app's `camera_event` channel -- the hook doover automations subscribe
# to. We publish onto it with our own kinds so an automation can act on a PPE
# violation or a plate read exactly as it does for the camera's own events.
CAMERA_EVENT_CHANNEL = "camera_event"

# Marks a message as our own output. This app publishes into the very channel it
# subscribes to, so without a marker every result we publish would come straight
# back as a new snapshot to analyse -- an endless loop that also costs a model run
# each time round. Checked before anything else in the handler.
ANALYSED_BY_KEY = "analysed_by"

# Extensions we can decode. Video snapshots (the camera app's "Video" mode) land on
# the same channel and can't be run through an image decoder.
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

ANNOTATED_SUFFIX = "-detected"
# Matches the camera app's convention (`<name>-thumbnail.jpg`), so its gallery treats our
# previews the same way as its own.
THUMBNAIL_SUFFIX = "-thumbnail"
# How the annotated frame is labelled in `media` -- its own view rather than replacing
# the source entry, so the unannotated frame stays browsable.
DETECTED_VIEW_SUFFIX = " (detected)"

# How long to wait after a snapshot message before re-reading it for its attachments.
# The device agent only knows the attachment URLs once its upload of the files has
# completed, so asking immediately gets nothing back. One second covers the local
# queue-and-upload on a healthy link; if it hasn't landed by then we log and skip the
# frame rather than block the event stream waiting for it.
ATTACHMENT_WAIT_SEC = 1


class ObjectDetectionApplication(Application):
    config: ObjectDetectionConfig
    tags: ObjectDetectionTags

    config_cls = ObjectDetectionConfig
    tags_cls = ObjectDetectionTags

    async def setup(self):
        self.ppe = None
        self.anpr = None

        if self.config.ppe.enabled.value:
            self.ppe = ppe_mod.load(self.config.ppe)
        if self.config.anpr.enabled.value:
            self.anpr = anpr_mod.load(self.config.anpr)

        if not (self.ppe or self.anpr):
            log.warning(
                "No detectors are enabled (or none could load their weights) -- "
                "snapshots will be ignored."
            )

        # One model run at a time. Concurrent runs on a 4-core CM4 shared with the
        # camera apps would multiply peak RAM by the number of cameras that happened
        # to snapshot together, which is exactly when they all fire (the schedule).
        self._inference_lock = asyncio.Lock()

        keys = self.config.watched_app_keys
        if not keys:
            log.warning("No camera apps configured; nothing to subscribe to.")
        for key in keys:
            log.info(f"Subscribing to snapshots from '{key}'.")
            self.device_agent.add_event_callback(
                key, self.on_camera_message, EventSubscription.message_create
            )

    async def main_loop(self):
        # Everything happens in the subscription callbacks; the loop only exists to
        # surface that the app is alive and what it has done.
        log.info(
            f"Watching {len(self.config.watched_app_keys)} camera app(s). "
            f"Analysed {self.tags.analysed_count.value} snapshot(s), "
            f"{self.tags.violation_count.value} PPE violation(s)."
        )

    # -- ingest ---------------------------------------------------------------

    async def on_camera_message(self, event: MessageCreateEvent):
        try:
            await self._handle_camera_message(event)
        except Exception as e:
            # A subscription callback that raises kills the stream for that channel,
            # taking every future snapshot with it. One bad frame must not do that.
            log.error(f"Failed to process camera message: {e}", exc_info=e)

    async def _handle_camera_message(self, event: MessageCreateEvent):
        message = event.message
        payload = message.data or {}
        app_key = event.channel.name

        if ANALYSED_BY_KEY in payload:
            # Our own result coming back round. See ANALYSED_BY_KEY.
            return

        reason = payload.get("reason")

        # The camera app sets this on its motion snapshots to say whether it wants them
        # analysed (its "Motion Snapshot Config > Object Detection" setting). It's
        # authoritative in both directions and overrides the reason filter: the camera
        # is the thing that knows whether this particular frame was captured to be
        # analysed, and honouring only the True case would leave no way to opt a single
        # camera out.
        wanted_by_camera = payload.get("object_detection")
        if wanted_by_camera is False:
            log.debug(f"'{app_key}' snapshot not marked for object detection.")
            return

        wanted = self.config.wanted_reasons
        if wanted_by_camera is not True and wanted and reason not in wanted:
            log.debug(f"Ignoring '{app_key}' snapshot (reason={reason}).")
            return

        if not (self.ppe or self.anpr):
            return

        message = await self._await_attachments(app_key, message)
        targets = self._image_attachments(payload, message.attachments)
        if not targets:
            # Say so rather than returning quietly. A snapshot message whose payload
            # names media but carries no attachments used to look identical to "no
            # snapshots are arriving", which is how this went unnoticed.
            if isinstance(payload.get("media"), list) and payload["media"]:
                log.warning(
                    f"'{app_key}' published {len(payload['media'])} media item(s) but "
                    f"the message still carries no attachments, so there is nothing to "
                    f"analyse. Named: "
                    f"{[m.get('file') for m in payload['media'] if isinstance(m, dict)]}"
                )
            return

        log.info(
            f"Analysing {len(targets)} image(s) from '{app_key}' (reason={reason})."
        )
        for name, attachment in targets:
            await self._analyse_attachment(app_key, message, name, attachment, reason)

    async def _await_attachments(self, app_key: str, message):
        """Re-read the message so it carries its attachments.

        A ``MessageCreate`` event never has them: the publishing app hands its files to
        the device agent, which queues them for upload and mints no local URLs, so the
        event (and the agent's cached copy) lists none. The agent fills them in on
        ``GetMessage`` once the upload lands, so the sequence is: wait for the upload,
        then ask again.

        Returns the original message unchanged if the re-read fails or still has
        nothing — the caller logs that case, and a frame we can't reach is not worth
        raising over.
        """
        await asyncio.sleep(ATTACHMENT_WAIT_SEC)
        try:
            refetched = await self.device_agent.fetch_message(app_key, message.id)
        except Exception as e:
            log.warning(
                f"Couldn't re-read message {message.id} on '{app_key}' for its "
                f"attachments: {e}",
                exc_info=e,
            )
            return message

        if refetched is None or not refetched.attachments:
            return message
        return refetched

    @classmethod
    def _image_attachments(cls, payload: dict, attachments: list) -> list:
        """Pick the full-size images out of a snapshot message.

        The camera app publishes a ``media`` list naming which attachment is the
        full-size file and which is its thumbnail; the thumbnail is the same scene at
        640x360, so running the models over it as well would double the CPU cost to
        produce a worse answer. Where there's no ``media`` list (an older camera app,
        or another publisher) every image attachment is analysed.
        """
        by_filename = {a.filename: a for a in attachments or []}

        media = payload.get("media")
        if not isinstance(media, list):
            return [
                (a.filename, a) for a in attachments or [] if cls._is_image(a.filename)
            ]

        targets = []
        for entry in media:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("file")
            attachment = by_filename.get(filename)
            if attachment is None or not cls._is_image(filename):
                continue
            targets.append((entry.get("name") or filename, attachment))
        return targets

    @staticmethod
    def _is_image(filename: str) -> bool:
        return bool(filename) and filename.lower().endswith(IMAGE_SUFFIXES)

    async def _analyse_attachment(self, app_key, message, name, attachment, reason):
        try:
            file = await self.device_agent.fetch_message_attachment(attachment)
        except Exception as e:
            log.warning(
                f"Couldn't fetch '{attachment.filename}' from '{app_key}': {e}",
                exc_info=e,
            )
            return

        image = annotate_mod.decode(file.data)
        if image is None:
            log.warning(f"Couldn't decode '{attachment.filename}' as an image.")
            return

        async with self._inference_lock:
            ppe_result, anpr_result = await asyncio.to_thread(self._run_models, image)

        await self._publish_result(
            app_key, message, name, attachment, reason, image, ppe_result, anpr_result
        )

    def _run_models(self, image):
        """Run every enabled detector. Blocking -- executed in a worker thread."""
        size = self.config.inference_size.value

        ppe_result = anpr_result = None
        if self.ppe:
            try:
                ppe_result = self.ppe.analyse(image, size)
            except Exception as e:
                log.error(f"PPE inference failed: {e}", exc_info=e)
        if self.anpr:
            try:
                anpr_result = self.anpr.analyse(image, size)
            except Exception as e:
                log.error(f"Plate inference failed: {e}", exc_info=e)
        return ppe_result, anpr_result

    # -- publish --------------------------------------------------------------

    async def _publish_result(
        self, app_key, message, name, attachment, reason, image, ppe_result, anpr_result
    ):
        findings = {}
        if ppe_result is not None:
            findings["ppe"] = ppe_result.to_dict()
        if anpr_result is not None:
            findings["anpr"] = anpr_result.to_dict()

        violators = list(ppe_result.violators) if ppe_result else []
        plates = anpr_result.read_plates if anpr_result else []
        found_anything = bool(
            violators
            or plates
            or (ppe_result and ppe_result.people)
            or (anpr_result and anpr_result.plates)
        )

        await self.tags.analysed_count.set(self.tags.analysed_count.value + 1)

        if not found_anything and not self.config.publish_clean_results.value:
            log.info(f"Nothing detected in '{attachment.filename}'.")
            return

        payload = {
            ANALYSED_BY_KEY: self.app_key,
            "analysed_at": datetime.now(tz=timezone.utc).isoformat(),
            "findings": {name: findings},
            "summary": self._summarise(violators, plates),
        }

        files = []
        if self.config.annotate.value:
            try:
                annotated = annotate_mod.annotate(image, ppe_result, anpr_result)
                filename = self._annotated_filename(attachment.filename)
                thumb_name = f"{filename.rsplit('.', 1)[0]}{THUMBNAIL_SUFFIX}.jpg"
                files.append(
                    File(
                        filename=filename,
                        content_type="image/jpeg",
                        size=0,
                        data=annotate_mod.encode_jpeg(annotated),
                    )
                )
                files.append(
                    File(
                        filename=thumb_name,
                        content_type="image/jpeg",
                        size=0,
                        data=annotate_mod.encode_thumbnail_jpeg(annotated),
                    )
                )
                # Put the annotated frame in `media` too, or a gallery driven off that
                # list never shows it -- the attachment would be there but invisible.
                # The whole list is rebuilt, camera entries included: a merge patch
                # replaces a list rather than appending, so sending only ours would drop
                # the original snapshot from the gallery.
                entry = {
                    "name": f"{name}{DETECTED_VIEW_SUFFIX}",
                    "file": filename,
                    "thumbnail": thumb_name,
                }
                existing = [
                    e
                    for e in (message.data or {}).get("media") or []
                    if isinstance(e, dict) and e.get("file") != filename
                ]
                payload["media"] = existing + [entry]
            except Exception as e:
                log.warning(f"Couldn't annotate the image: {e}", exc_info=e)

        # Edit the camera's own snapshot message rather than publishing a second one, so
        # a frame and its analysis are one timeline entry instead of two that a reader
        # has to pair up. Matches the cloud processor, which has to work this way: its
        # anti-recursion guard blocks create_message on the invoking channel.
        #
        # replace_data=False keeps the camera's payload (reason, media, night);
        # clear_attachments=False keeps the original snapshot beside the annotated copy.
        try:
            await self.device_agent.update_message(
                app_key,
                message.id,
                payload,
                files=files,
                replace_data=False,
                clear_attachments=False,
            )
        except Exception as e:
            log.error(
                f"Failed to update message {message.id} on '{app_key}': {e}", exc_info=e
            )

        await self._publish_events(app_key, violators, plates)
        await self._notify(app_key, violators, plates)

    @staticmethod
    def _annotated_filename(filename: str) -> str:
        stem, _, _suffix = filename.rpartition(".")
        if not stem:
            return f"{filename}{ANNOTATED_SUFFIX}.jpg"
        return f"{stem}{ANNOTATED_SUFFIX}.jpg"

    @staticmethod
    def _summarise(violators, plates) -> str:
        parts = []
        if violators:
            missing = sorted({m for v in violators for m in v.missing})
            pretty = ", ".join(m.replace("_", " ") for m in missing)
            parts.append(
                f"{len(violators)} "
                f"{'person' if len(violators) == 1 else 'people'} missing {pretty}"
            )
        if plates:
            parts.append(f"plate(s) {', '.join(p.text for p in plates)}")
        return "; ".join(parts) or "nothing detected"

    async def _publish_events(self, app_key, violators, plates):
        """Publish structured events for automations, mirroring `camera_event`."""
        now = datetime.now(tz=timezone.utc).isoformat()

        for plate in plates:
            await self.tags.last_plate.set(plate.text)
            await self._publish_camera_event(
                "anpr",
                app_key,
                timestamp=now,
                plate=plate.text,
                confidence=plate.detection.confidence,
            )

        if not violators:
            return

        await self.tags.violation_count.set(self.tags.violation_count.value + 1)
        # Epoch milliseconds, matching the camera app's tag of the same name. Its
        # naive `datetime.now()` yields the same epoch value as this, since
        # `timestamp()` reads a naive datetime as local time -- being explicit about
        # the zone just removes the ambiguity for the reader.
        await self.tags.last_ppe_violation.set(
            int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        )
        await self._publish_camera_event(
            "ppe_violation",
            app_key,
            timestamp=now,
            count=len(violators),
            missing=sorted({m for v in violators for m in v.missing}),
        )

    async def _publish_camera_event(self, kind: str, app_key: str, **extra):
        payload = {
            "kind": kind,
            # The camera the finding came from, not this app -- an automation cares
            # which camera saw it.
            "app_key": app_key,
            "detected_by": self.app_key,
            **extra,
        }
        try:
            await self.create_message(CAMERA_EVENT_CHANNEL, payload)
        except Exception as e:
            log.warning(f"Failed to publish {kind} event: {e}", exc_info=e)

    async def _notify(self, app_key, violators, plates):
        if violators and self.config.ppe.notify_on_violation.value:
            missing = sorted({m for v in violators for m in v.missing})
            pretty = " and ".join(m.replace("_", " ") for m in missing)
            who = "someone" if len(violators) == 1 else f"{len(violators)} people"
            await self.send_notification(
                f"{app_key} detected {who} without {pretty}.",
                severity=NotificationSeverity.Warn,
                topic="ppe_event",
            )

        if plates and self.config.anpr.notify_on_plate.value:
            await self.send_notification(
                f"{app_key} read plate(s) {', '.join(p.text for p in plates)}.",
                severity=NotificationSeverity.Info,
                topic="anpr_event",
            )
