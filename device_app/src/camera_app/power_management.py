import asyncio
import logging

from datetime import datetime, timedelta

from typing import TYPE_CHECKING

from .app_config import value_or

if TYPE_CHECKING:
    from .application import CameraApplication

log = logging.getLogger(__name__)

POWER_STATUS_KEY = "power_status"
PING_INTERVAL_SEC = 2
PING_TIMEOUT_SEC = 2

# Always On mode: how often the watchdog checks the camera is still answering, and how
# long it gives each check. Slower than the boot ping loop on purpose — this one runs for
# the life of the app rather than for a few seconds after power-on, and a camera that has
# locked up will still be locked up in half a minute.
WATCHDOG_INTERVAL_SEC = 30
WATCHDOG_PING_TIMEOUT_SEC = 5


class CameraPowerManagement:
    def __init__(self, app: "CameraApplication"):
        self.app = app
        self.config = app.config.power
        self.tasks = []

        self._powered_on_at: datetime | None = None
        self._is_pingable = False
        self._last_ping_at: datetime | None = None
        self._ping_task: asyncio.Task | None = None

        # Always On state. `_cycling` is held across a power cycle so nothing raises the
        # pin again mid-cycle and undoes it (a snapshot request would otherwise);
        # `_suspended` latches a *deliberate* release — the device shutting down — so the
        # always-on loop doesn't immediately power the camera back up.
        self._watchdog_task: asyncio.Task | None = None
        self._cycling = False
        self._suspended = False

        self.check_release_task = asyncio.create_task(self.check_release())

    @property
    def power_is_on(self) -> bool:
        return self._powered_on_at is not None

    @property
    def always_on(self) -> bool:
        """Whether to hold power on permanently and only cycle on an unreachable camera."""
        return bool(self.config.enabled.value) and bool(
            value_or(self.config.always_on, False)
        )

    async def acquire_for(self, dt: timedelta):
        if not self.config.enabled.value:
            log.info("Power management is disabled. Skipping...")
            return

        # Somebody wants the camera, so an earlier deliberate release no longer stands.
        self._suspended = False

        if self._cycling:
            # Mid power cycle. Raising the pin now would abort the reboot we're doing
            # precisely because the camera stopped answering, and leave it locked up.
            log.info("Power cycle in progress; leaving the pin alone.")
            return

        acquired_until_ts = self.app.tag_manager.get_tag(
            f"camera_power_{self.config.pin.value}", default=0, app_key=None
        )
        acquired_until = datetime.fromtimestamp(acquired_until_ts)
        acquire_until = datetime.now() + dt

        # we can set this regardless in case the app got into some sort of funny state
        await self.app.platform_iface.set_do(self.config.pin.value, True)

        # record the boot start the first time we transition to on, and kick off
        # the ping loop so we can tell the UI when the camera is reachable.
        if self._powered_on_at is None:
            self._powered_on_at = datetime.now()
            self._is_pingable = False
            await self.publish_status()
            self._start_ping_check()

        if acquired_until > acquire_until:
            log.info(
                f"Already acquired until {acquired_until} which exceeds our timeout of {self.config.timeout.value}. "
                f"Setting platform iface on to be sure and skipping..."
            )
            await self.app.platform_iface.set_do(self.config.pin.value, True)
            return

        log.info(f"Acquiring power for camera until {acquire_until}")
        await self.app.platform_iface.set_do(self.config.pin.value, True)
        await self.app.tag_manager.set_tag(
            f"camera_power_{self.config.pin.value}", acquire_until.timestamp()
        )

    async def acquire(self):
        return await self.acquire_for(timedelta(seconds=self.config.timeout.value))

        # self.tasks.append(asyncio.create_task(self.release_at(acquire_until)))

    async def check_release(self):
        while True:
            try:
                await self.app.wait_until_ready()

                if not self.config.enabled.value:
                    log.info("Power management is disabled. Skipping...")
                    await asyncio.sleep(60)
                    continue

                if self.always_on:
                    # Nothing to release: power stays on, and the only thing that ever
                    # takes it away is the watchdog rebooting a camera that stopped
                    # answering. The "acquired until" tag is left alone deliberately, so a
                    # camera app sharing this pin without Always On still sees a hold.
                    if not (self._cycling or self._suspended):
                        await self._hold_power_on()
                    await asyncio.sleep(WATCHDOG_INTERVAL_SEC)
                    continue

                # a few prerequisites that make this possible, and desirable.
                # for simplicity, this is never restarted, nor cancelled when a camera is started. this is because:
                # 1. Time to shut off can never go backwards (ie. if you sleep until the next one and it's cancelled it will
                #    just check again and wait for the next one)
                # 2. If there's no releases scheduled it will check every 30 seconds (less than the minimum timeout)

                acquired_until_ts = self.app.tag_manager.get_tag(
                    f"camera_power_{self.config.pin.value}",
                    default=0,
                    app_key=None,
                )
                acquired_until = datetime.fromtimestamp(acquired_until_ts)
                if acquired_until_ts and acquired_until < datetime.now():
                    log.info(
                        f"Power acquired until {acquired_until} has expired. Releasing power..."
                    )
                    await self.release()
                    await self.app.tag_manager.set_tag(
                        f"camera_power_{self.config.pin.value}", 0, app_key=None
                    )
                else:
                    if acquired_until_ts == 0:
                        log.info("No acquired timestamp found. Sleeping for 60sec.")
                        await asyncio.sleep(60)
                    else:
                        log.info(f"Sleeping until acquired time: {acquired_until}")
                        await asyncio.sleep(
                            (acquired_until - datetime.now()).total_seconds()
                        )

            except asyncio.CancelledError:
                log.info("Power release check cancelled")
                break
            except Exception as e:
                log.error(f"Failed to check power release: {e}")
                await asyncio.sleep(10)

    async def release(self):
        # Only reached in Always On mode when something deliberately gives the power up —
        # the device shutting down — because the expiry check above skips releasing. Latch
        # it, or the always-on loop powers the camera straight back up.
        if self.always_on and not self._cycling:
            self._suspended = True

        log.info("Releasing power...")
        await self.app.platform_iface.set_do(self.config.pin.value, False)

        was_on = self.power_is_on
        self._stop_ping_check()
        self._powered_on_at = None
        self._is_pingable = False
        self._last_ping_at = None
        if was_on:
            await self.publish_status()

    async def publish_status(self):
        """Publish current power status to the camera channel so the UI can render
        a "powering up" overlay and hide it once the camera is reachable.
        """
        payload = {
            POWER_STATUS_KEY: {
                "enabled": self.config.enabled.value,
                "powered_on": self.power_is_on,
                "powered_on_at": self._powered_on_at.timestamp()
                if self._powered_on_at
                else None,
                "wake_delay": self.config.wake_delay.value,
                "pingable": self._is_pingable,
                "last_ping_at": self._last_ping_at.timestamp()
                if self._last_ping_at
                else None,
            }
        }
        try:
            await self.app.device_agent.update_channel_aggregate(
                self.app.app_key, payload, max_age_secs=-1
            )
        except Exception as e:
            log.warning(f"Failed to publish power status: {e}")

    async def _hold_power_on(self):
        """Keep the pin on, and keep the watchdog running. Idempotent."""
        await self.app.platform_iface.set_do(self.config.pin.value, True)

        if self._powered_on_at is None:
            log.info("Always On: powering the camera up and leaving it on.")
            self._powered_on_at = datetime.now()
            self._is_pingable = False
            await self.publish_status()

        if not (self._watchdog_task and not self._watchdog_task.done()):
            self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self):
        """Power cycle the camera when, and only when, it stops answering pings.

        This is the whole point of Always On: a camera that is working is never
        interrupted, and one that has locked up gets its power taken away — which is the
        only thing that fixes it, and something no amount of retrying over the network can
        do.

        Failures have to be consecutive. A single dropped ping means nothing (the camera is
        busy encoding, the network blipped), and cutting power on one is worse than not
        having a watchdog at all, because a power cycle costs a boot during which the
        camera really is unreachable.
        """
        failures = 0
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL_SEC)

                if self._cycling or self._suspended or self.app.engine is None:
                    continue

                try:
                    ok = bool(await self.app.engine.ping(WATCHDOG_PING_TIMEOUT_SEC))
                except Exception as e:
                    log.warning(f"Watchdog ping raised: {e}")
                    ok = False

                self._last_ping_at = datetime.now()

                if ok:
                    failures = 0
                    if not self._is_pingable:
                        self._is_pingable = True
                        log.info("Camera is answering again.")
                        await self.publish_status()
                    continue

                failures += 1
                threshold = max(1, value_or(self.config.watchdog_failures, 3))
                log.warning(
                    f"Camera did not answer a ping ({failures}/{threshold} before a "
                    f"power cycle)."
                )
                if self._is_pingable:
                    self._is_pingable = False
                    await self.publish_status()

                if failures >= threshold:
                    await self._power_cycle()
                    failures = 0
        except asyncio.CancelledError:
            pass

    async def _power_cycle(self):
        """Drop power, wait, restore it — then give the camera time to boot.

        ``_cycling`` is held for the whole thing so a snapshot arriving mid-cycle can't
        raise the pin and abort the reboot. The wake delay afterwards is not politeness:
        without it the watchdog would count the booting camera's silence as fresh failures
        and cycle it again, forever.
        """
        pin = self.config.pin.value
        off_secs = max(1, value_or(self.config.power_cycle_secs, 15))
        wake_delay = self.config.wake_delay.value

        log.warning(f"Power cycling the camera: off for {off_secs}s.")
        self._cycling = True
        try:
            await self.app.platform_iface.set_do(pin, False)
            self._powered_on_at = None
            self._is_pingable = False
            await self.publish_status()

            await asyncio.sleep(off_secs)

            await self.app.platform_iface.set_do(pin, True)
            self._powered_on_at = datetime.now()
            await self.publish_status()
            log.info(f"Camera powered back up; giving it {wake_delay}s to boot.")
        except Exception as e:
            log.error(f"Power cycle failed: {e}", exc_info=e)
        finally:
            self._cycling = False

        await asyncio.sleep(wake_delay)

    def _start_ping_check(self):
        if self._ping_task and not self._ping_task.done():
            return
        self._ping_task = asyncio.create_task(self._ping_until_reachable())

    def _stop_ping_check(self):
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        self._ping_task = None

    async def _ping_until_reachable(self):
        """Ping the camera after a power-on until it responds, then exit.

        Only runs between an off→on transition and the first successful ping —
        once the camera is reachable there's nothing for the UI to wait on, so
        we stop pinging until the next power cycle.
        """
        try:
            while True:
                if self.app.engine is None:
                    await asyncio.sleep(PING_INTERVAL_SEC)
                    continue

                try:
                    ok = bool(await self.app.engine.ping(PING_TIMEOUT_SEC))
                except Exception as e:
                    log.warning(f"Ping check raised: {e}")
                    ok = False

                self._last_ping_at = datetime.now()

                if ok:
                    self._is_pingable = True
                    await self.publish_status()
                    return

                await asyncio.sleep(PING_INTERVAL_SEC)
        except asyncio.CancelledError:
            pass
