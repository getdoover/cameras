"""Object detection as a cloud processor (AWS Lambda).

The same models and the same compliance reasoning as the on-device app — both import
``common`` — but a different shape around them:

* **Invoked, not subscribing.** The platform delivers the camera's snapshot message; we
  don't watch a channel. So there's no camera-app list in config.
* **Edits the message in place** rather than publishing a second one, so a snapshot and
  its analysis are one timeline entry. ``ProcessorDataClient`` nudges this way too: its
  anti-recursion guard blocks ``create_message`` on the invoking channel but permits
  ``update_message``.
* **Attachments just work.** In the cloud they carry real URLs, so there's none of the
  device-side dance of waiting for an upload before the image is reachable.

What this variant does **not** buy is accuracy from a bigger inference size. That was
the assumption; measurement killed it. The weights are trained at 640, and on a real
site frame 960 lost a person that 640 found. Raising the size shifts object scale away
from the training distribution, so more CPU here buys throughput, not better detection.

The genuine wins are: no device RAM/CPU budget to share with the camera apps (so
heavier *weights* become possible when we have some), attachments that resolve without
waiting on an upload, and one timeline entry per snapshot.
"""

import logging
from datetime import datetime, timezone

from common import annotate as annotate_mod
from common.detectors import anpr as anpr_mod
from common.detectors import ppe as ppe_mod
from pydoover.models import File, MessageCreateEvent, NotificationSeverity
from pydoover.processor import Application

from .app_config import ObjectDetectionProcessorConfig

log = logging.getLogger()

# Marks a message as already analysed. The invoking-channel guard means our update
# can't re-trigger us, so this is not loop protection like it is on-device -- it's
# idempotency, for a replay or a manual re-invoke.
ANALYSED_BY_KEY = "analysed_by"

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
ANNOTATED_SUFFIX = "-detected"
# Matches the camera app's convention (`<name>-thumbnail.jpg`), so its gallery treats
# our previews the same way as its own.
THUMBNAIL_SUFFIX = "-thumbnail"
# How the annotated frame is labelled in `media`. It goes in as its own view rather than
# replacing the source entry, so the unannotated frame stays browsable.
DETECTED_VIEW_SUFFIX = " (detected)"

# Loaded once per *container*, not per invocation.
#
# Lambda reuses a warm container across invocations but calls the handler (and so
# `setup`) each time, and building an onnxruntime session costs ~700ms per model. Held
# at module scope so only a cold start pays for it; the detectors are stateless between
# frames, so sharing them is safe.
_DETECTORS: dict = {}


class ObjectDetectionProcessor(Application):
    config: ObjectDetectionProcessorConfig
    config_cls = ObjectDetectionProcessorConfig

    def _detectors(self):
        """The PPE / ANPR detectors, built once per warm container."""
        ppe_wanted = self.config.ppe.enabled.value
        anpr_wanted = self.config.anpr.enabled.value

        # Key on the settings that shape a model's construction, so a config change
        # rebuilds rather than silently reusing a detector built for the old value.
        key = (
            ppe_wanted,
            anpr_wanted,
            self.config.ppe.confidence.value,
            self.config.ppe.require_hard_hat.value,
            self.config.ppe.require_high_vis.value,
            self.config.anpr.confidence.value,
            self.config.anpr.min_plate_chars.value,
        )
        if _DETECTORS.get("key") != key:
            _DETECTORS.clear()
            _DETECTORS["key"] = key
            _DETECTORS["ppe"] = ppe_mod.load(self.config.ppe) if ppe_wanted else None
            _DETECTORS["anpr"] = (
                anpr_mod.load(self.config.anpr) if anpr_wanted else None
            )
            log.info(
                f"Built detectors (cold start): ppe={_DETECTORS['ppe'] is not None} "
                f"anpr={_DETECTORS['anpr'] is not None}"
            )
        return _DETECTORS["ppe"], _DETECTORS["anpr"]

    async def on_message_create(self, event: MessageCreateEvent):
        message = event.message
        payload = message.data or {}
        channel = event.channel.name

        if ANALYSED_BY_KEY in payload:
            return

        reason = payload.get("reason")
        wanted_by_camera = payload.get("object_detection")
        if wanted_by_camera is False:
            log.info(f"'{channel}' snapshot not marked for object detection.")
            return

        wanted = self.config.wanted_reasons
        if wanted_by_camera is not True and wanted and reason not in wanted:
            log.info(f"Ignoring '{channel}' snapshot (reason={reason}).")
            return

        ppe, anpr = self._detectors()
        if not (ppe or anpr):
            log.warning("No detectors enabled or their weights failed to load.")
            return

        targets = self._image_attachments(payload, message.attachments)
        if not targets:
            log.info(f"No analysable image on '{channel}' message {message.id}.")
            return

        # One frame per message in practice; a PTZ camera contributing several presets
        # is analysed in order and the findings merged under their view names.
        findings, files, media, summaries = {}, [], [], []
        for name, attachment in targets:
            result = await self._analyse(attachment, ppe, anpr, name)
            if result is None:
                continue
            findings[name] = result["findings"]
            summaries.append(result["summary"])
            files.extend(result["files"])
            if result["media"]:
                media.append(result["media"])

        if not findings:
            return

        await self._publish(
            channel, message, payload, findings, files, media, summaries
        )

    async def _analyse(self, attachment, ppe, anpr, name):
        try:
            data = await self.api.fetch_message_attachment(attachment)
        except Exception as e:
            log.warning(f"Couldn't download '{attachment.filename}': {e}", exc_info=e)
            return None

        image = annotate_mod.decode(data)
        if image is None:
            log.warning(f"Couldn't decode '{attachment.filename}' as an image.")
            return None

        size = self.config.inference_size.value
        ppe_result = anpr_result = None
        if ppe:
            try:
                ppe_result = ppe.analyse(image, size)
                for person in ppe_result.people:
                    person.missing = person.violations(
                        self.config.ppe.require_hard_hat.value,
                        self.config.ppe.require_high_vis.value,
                    )
            except Exception as e:
                log.error(f"PPE inference failed: {e}", exc_info=e)
        if anpr:
            try:
                anpr_result = anpr.analyse(image, size)
            except Exception as e:
                log.error(f"Plate inference failed: {e}", exc_info=e)

        view = {}
        if ppe_result is not None:
            view["ppe"] = ppe_result.to_dict()
        if anpr_result is not None:
            view["anpr"] = anpr_result.to_dict()

        files, media_entry = [], None
        if self.config.annotate.value:
            try:
                drawn = annotate_mod.annotate(image, ppe_result, anpr_result)
                filename = self._annotated_filename(attachment.filename)
                thumb_name = f"{filename.rsplit('.', 1)[0]}{THUMBNAIL_SUFFIX}.jpg"
                files.append(
                    File(
                        filename=filename,
                        content_type="image/jpeg",
                        size=0,
                        data=annotate_mod.encode_jpeg(drawn),
                    )
                )
                files.append(
                    File(
                        filename=thumb_name,
                        content_type="image/jpeg",
                        size=0,
                        data=annotate_mod.encode_thumbnail_jpeg(drawn),
                    )
                )
                # Same shape as the camera app's own media entries, so a gallery renders
                # this without special-casing us. Named as its own view rather than
                # replacing the source entry, so the original frame stays browsable.
                media_entry = {
                    "name": f"{name}{DETECTED_VIEW_SUFFIX}",
                    "file": filename,
                    "thumbnail": thumb_name,
                }
            except Exception as e:
                log.warning(f"Couldn't annotate the image: {e}", exc_info=e)

        violators = list(ppe_result.violators) if ppe_result else []
        plates = anpr_result.read_plates if anpr_result else []
        return {
            "findings": view,
            "files": files,
            "media": media_entry,
            "summary": self._summarise(violators, plates),
        }

    async def _publish(
        self, channel, message, payload, findings, files, media, summaries
    ):
        """Merge the findings into the original message and attach the annotation.

        `replace_data=False` so the camera's own payload survives, and
        `clear_attachments=False` so the original snapshot stays alongside the
        annotated copy rather than being replaced by it.
        """
        detail = {
            ANALYSED_BY_KEY: self.app_key,
            "analysed_at": datetime.now(tz=timezone.utc).isoformat(),
            "findings": findings,
            "summary": "; ".join(s for s in summaries if s) or "nothing detected",
        }
        if media:
            # Send the *whole* list, camera entries included. A merge patch replaces a
            # list wholesale rather than appending to it, so sending only our entries
            # would drop the original snapshot out of the gallery -- attached, but
            # invisible. Rebuilt here so the result is right either way.
            detail["media"] = self._merged_media(payload, media)
        try:
            await self.api.update_message(
                channel_name=channel,
                message_id=message.id,
                data=detail,
                replace_data=False,
                files=files or None,
                clear_attachments=False,
            )
        except Exception as e:
            log.error(f"Failed to update message {message.id}: {e}", exc_info=e)
            return

        log.info(f"Updated message {message.id} on '{channel}': {detail['summary']}")
        await self._notify(channel, findings)

    async def _notify(self, channel, findings):
        violations = [
            v
            for view in findings.values()
            for v in view.get("ppe", {}).get("violations", [])
        ]
        plates = [
            p["plate"]
            for view in findings.values()
            for p in view.get("anpr", {}).get("plates", [])
            if p.get("plate")
        ]

        if violations and self.config.ppe.notify_on_violation.value:
            missing = sorted({m for v in violations for m in v.get("missing", [])})
            pretty = " and ".join(m.replace("_", " ") for m in missing)
            who = "someone" if len(violations) == 1 else f"{len(violations)} people"
            await self.send_notification(
                f"{channel} detected {who} without {pretty}.",
                severity=NotificationSeverity.Warn,
                topic="ppe_event",
            )

        if plates and self.config.anpr.notify_on_plate.value:
            await self.send_notification(
                f"{channel} read plate(s) {', '.join(plates)}.",
                severity=NotificationSeverity.Info,
                topic="anpr_event",
            )

    @staticmethod
    def _merged_media(payload: dict, new_entries: list) -> list:
        """The camera's media entries plus ours, without duplicating on a re-run.

        Keyed by filename so re-analysing a message replaces our previous entry rather
        than appending a second copy of it.
        """
        existing = [e for e in (payload.get("media") or []) if isinstance(e, dict)]
        ours = {e["file"] for e in new_entries}
        return [e for e in existing if e.get("file") not in ours] + new_entries

    @classmethod
    def _image_attachments(cls, payload: dict, attachments: list) -> list:
        """Pick the full-size images out of a snapshot message.

        Mirrors the on-device app: the camera's ``media`` list says which attachment is
        full-size and which is its 640x360 thumbnail, and analysing the thumbnail too
        would double the cost for a worse answer. Also skips anything we've already
        attached ourselves, so a re-invoke doesn't analyse its own annotation.
        """
        by_filename = {a.filename: a for a in attachments or []}

        media = payload.get("media")
        if not isinstance(media, list):
            return [
                (a.filename, a)
                for a in attachments or []
                if cls._is_image(a.filename) and ANNOTATED_SUFFIX not in a.filename
            ]

        targets = []
        for entry in media:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("file")
            attachment = by_filename.get(filename)
            if attachment is None or not cls._is_image(filename):
                continue
            if ANNOTATED_SUFFIX in filename:
                continue
            targets.append((entry.get("name") or filename, attachment))
        return targets

    @staticmethod
    def _is_image(filename: str) -> bool:
        return bool(filename) and filename.lower().endswith(IMAGE_SUFFIXES)

    @staticmethod
    def _annotated_filename(filename: str) -> str:
        stem, _, _suffix = filename.rpartition(".")
        return f"{stem or filename}{ANNOTATED_SUFFIX}.jpg"

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
        return "; ".join(parts)
