import asyncio
import json
import math
import os
import pwd
import shutil
import sys
import tempfile
import wave
from array import array
from typing import Any, Optional

import decky

SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLE_WIDTH = 2
CHUNK_SECONDS = 0.05
CHUNK_BYTES = int(SAMPLE_RATE * CHUNK_SECONDS) * SAMPLE_WIDTH
METER_INTERVAL_CHUNKS = 4
MIN_RECORDING_SECONDS = 0.5
TEMP_PREFIX = "mic-test-"


def _to_dbfs(value: float) -> float:
    if value <= 0:
        return -120.0
    return round(max(-120.0, 20.0 * math.log10(value / 32768.0)), 1)


def _audio_metrics(data: bytes) -> dict[str, Any]:
    usable = len(data) - (len(data) % SAMPLE_WIDTH)
    if usable <= 0:
        return {"rms_dbfs": -120.0, "peak_dbfs": -120.0, "clipping": False}

    samples = array("h")
    samples.frombytes(data[:usable])
    if sys.byteorder == "big":
        samples.byteswap()

    total = 0
    peak = 0
    for sample in samples:
        magnitude = abs(sample)
        if magnitude > peak:
            peak = magnitude
        total += sample * sample

    rms = math.sqrt(total / len(samples))
    return {
        "rms_dbfs": _to_dbfs(rms),
        "peak_dbfs": _to_dbfs(peak),
        "clipping": peak >= 32767,
    }


def _volume_percent(entry: dict[str, Any]) -> int:
    volumes = entry.get("volume") or {}
    percents = []
    if isinstance(volumes, dict):
        for channel in volumes.values():
            if not isinstance(channel, dict):
                continue
            value_percent = channel.get("value_percent")
            if isinstance(value_percent, str):
                try:
                    percents.append(float(value_percent.rstrip("%")))
                except ValueError:
                    pass
            elif "value" in channel:
                try:
                    percents.append(float(channel["value"]) / 65536.0 * 100.0)
                except (TypeError, ValueError):
                    pass
    if not percents:
        return 0
    return int(round(max(0.0, min(100.0, max(percents)))))


def _is_monitor_source(entry: dict[str, Any]) -> bool:
    name = str(entry.get("name") or "")
    return entry.get("monitor_of_sink") is not None or name.endswith(".monitor")


def _find_tool(*names: str) -> Optional[str]:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


class Plugin:
    async def _main(self):
        self.env = self._build_env()
        self.runtime_dir = decky.DECKY_PLUGIN_RUNTIME_DIR
        os.makedirs(self.runtime_dir, exist_ok=True)

        self.pactl = _find_tool("pactl")
        self.capture_tool = _find_tool("parec", "pw-record", "arecord")
        self.play_tool = _find_tool("paplay", "pw-play", "aplay")

        self._meter_lock = asyncio.Lock()
        self._meter_task: Optional[asyncio.Task] = None
        self._meter_proc: Optional[asyncio.subprocess.Process] = None
        self._rec_lock = asyncio.Lock()
        self._rec_proc: Optional[asyncio.subprocess.Process] = None
        self._rec_file = None
        self._rec_path: Optional[str] = None
        self._rec_started = 0.0
        self._rec_playing = False
        self._last_wav: Optional[str] = None
        self._last_metrics: Optional[dict[str, Any]] = None

        self._cleanup_temp_files()
        decky.logger.info(
            "Mic Test loaded (pactl=%s, capture=%s, playback=%s)",
            self.pactl,
            self.capture_tool,
            self.play_tool,
        )

    async def _unload(self):
        await self.stop_level_monitor()
        await self.cancel_recording()
        self._cleanup_temp_files()
        decky.logger.info("Mic Test unloaded")

    async def _uninstall(self):
        await self.stop_level_monitor()
        await self.cancel_recording()
        self._cleanup_temp_files()
        decky.logger.info("Mic Test uninstalled")

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        try:
            uid = pwd.getpwnam(decky.DECKY_USER).pw_uid
        except KeyError:
            uid = os.getuid()
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        env["HOME"] = decky.DECKY_USER_HOME
        return env

    def _cleanup_temp_files(self):
        try:
            entries = os.listdir(self.runtime_dir)
        except OSError:
            return
        for entry in entries:
            if entry.startswith(TEMP_PREFIX) and entry.endswith((".raw", ".wav")):
                try:
                    os.remove(os.path.join(self.runtime_dir, entry))
                except OSError:
                    pass
        self._last_wav = None
        self._last_metrics = None

    async def _pactl(self, *args: str) -> Optional[str]:
        if self.pactl is None:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.pactl,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
            stdout, stderr = await proc.communicate()
        except (OSError, ValueError) as exc:
            decky.logger.warning("pactl %s failed to start: %s", " ".join(args), exc)
            return None

        if proc.returncode != 0:
            decky.logger.warning(
                "pactl %s failed (%s): %s",
                " ".join(args),
                proc.returncode,
                stderr.decode(errors="replace").strip(),
            )
            return None
        return stdout.decode(errors="replace")

    async def _pactl_json(self, kind: str) -> Optional[list[Any]]:
        output = await self._pactl("-f", "json", "list", kind)
        if output is None:
            return None
        try:
            parsed = json.loads(output)
        except ValueError:
            return None
        return parsed if isinstance(parsed, list) else None

    async def _get_short_volume(self, prefix: str, name: str) -> int:
        output = await self._pactl(f"get-{prefix}-volume", name) or ""
        for token in output.replace("/", " ").split():
            if token.endswith("%"):
                try:
                    return int(round(float(token.rstrip("%"))))
                except ValueError:
                    pass
        return 0

    async def _get_short_mute(self, prefix: str, name: str) -> bool:
        output = await self._pactl(f"get-{prefix}-mute", name) or ""
        return "yes" in output.lower()

    async def _list_devices(self, kind: str) -> Optional[list[dict[str, Any]]]:
        raw_entries = await self._pactl_json(kind)
        if raw_entries is not None:
            devices = []
            for entry in raw_entries:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "")
                if not name:
                    continue
                devices.append(
                    {
                        "name": name,
                        "description": str(entry.get("description") or name),
                        "volume": _volume_percent(entry),
                        "mute": bool(entry.get("mute", False)),
                        "is_monitor": _is_monitor_source(entry),
                    }
                )
            return devices

        short = await self._pactl("list", "short", kind)
        if short is None:
            return None

        prefix = "sink" if kind == "sinks" else "source"
        devices = []
        for line in short.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not parts[1]:
                continue
            name = parts[1]
            devices.append(
                {
                    "name": name,
                    "description": name,
                    "volume": await self._get_short_volume(prefix, name),
                    "mute": await self._get_short_mute(prefix, name),
                    "is_monitor": name.endswith(".monitor"),
                }
            )
        return devices

    async def _get_default(self, kind: str) -> Optional[str]:
        output = await self._pactl(f"get-default-{kind}")
        if output and output.strip():
            return output.strip()

        info = await self._pactl("info") or ""
        prefix = "Default Sink:" if kind == "sink" else "Default Source:"
        for line in info.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip() or None
        return None

    async def get_audio_state(self) -> dict[str, Any]:
        empty_state: dict[str, Any] = {
            "sinks": [],
            "sources": [],
            "default_sink": None,
            "default_source": None,
            "error": None,
        }
        if self.pactl is None:
            empty_state["error"] = "pactl was not found on this system."
            return empty_state

        sinks_raw, sources_raw, default_sink, default_source = await asyncio.gather(
            self._list_devices("sinks"),
            self._list_devices("sources"),
            self._get_default("sink"),
            self._get_default("source"),
        )

        error = None
        if sinks_raw is None:
            error = "Could not list audio outputs."
            sinks_raw = []
        if sources_raw is None:
            error = "Could not list audio inputs."
            sources_raw = []

        sinks = []
        for device in sinks_raw:
            if device.get("is_monitor"):
                continue
            sinks.append(
                {
                    "name": device["name"],
                    "description": device["description"],
                    "volume": device["volume"],
                    "mute": device["mute"],
                    "is_default": device["name"] == default_sink,
                }
            )

        sources = []
        for device in sources_raw:
            if device.get("is_monitor"):
                continue
            sources.append(
                {
                    "name": device["name"],
                    "description": device["description"],
                    "volume": device["volume"],
                    "mute": device["mute"],
                    "is_default": device["name"] == default_source,
                }
            )

        sinks.sort(key=lambda item: (not item["is_default"], item["description"].lower()))
        sources.sort(key=lambda item: (not item["is_default"], item["description"].lower()))

        return {
            "sinks": sinks,
            "sources": sources,
            "default_sink": default_sink,
            "default_source": default_source,
            "error": error,
        }

    async def set_default_sink(self, name: str) -> dict[str, Any]:
        error = None
        if not name:
            error = "No output device selected."
        elif self.pactl is None:
            error = "pactl was not found on this system."
        else:
            output = await self._pactl("set-default-sink", name)
            if output is None:
                error = f"Could not switch the output to {name}."
            else:
                streams = await self._pactl("list", "short", "sink-inputs") or ""
                for line in streams.splitlines():
                    parts = line.split("\t")
                    if parts and parts[0].isdigit():
                        await self._pactl("move-sink-input", parts[0], name)

        state = await self.get_audio_state()
        if error:
            state["error"] = error
        else:
            # WirePlumber applies default changes asynchronously, so the state
            # read immediately afterwards may still show the old default.
            state["default_sink"] = name
            for device in state["sinks"]:
                device["is_default"] = device["name"] == name
        return state

    async def set_default_source(self, name: str) -> dict[str, Any]:
        error = None
        if not name:
            error = "No input device selected."
        elif self.pactl is None:
            error = "pactl was not found on this system."
        else:
            output = await self._pactl("set-default-source", name)
            if output is None:
                error = f"Could not switch the input to {name}."

        state = await self.get_audio_state()
        if error:
            state["error"] = error
        else:
            state["default_source"] = name
            for device in state["sources"]:
                device["is_default"] = device["name"] == name
        return state

    async def set_sink_volume(self, name: str, percent: float) -> dict[str, Any]:
        value = max(0, min(100, int(round(percent))))
        output = await self._pactl("set-sink-volume", name, f"{value}%")
        return {
            "ok": output is not None,
            "volume": value,
            "error": None if output is not None else "Could not set output volume.",
        }

    async def set_source_volume(self, name: str, percent: float) -> dict[str, Any]:
        value = max(0, min(100, int(round(percent))))
        output = await self._pactl("set-source-volume", name, f"{value}%")
        return {
            "ok": output is not None,
            "volume": value,
            "error": None if output is not None else "Could not set input volume.",
        }

    async def set_sink_mute(self, name: str, muted: bool) -> dict[str, Any]:
        output = await self._pactl("set-sink-mute", name, "1" if muted else "0")
        return {
            "ok": output is not None,
            "mute": bool(muted),
            "error": None if output is not None else "Could not change output mute.",
        }

    async def set_source_mute(self, name: str, muted: bool) -> dict[str, Any]:
        output = await self._pactl("set-source-mute", name, "1" if muted else "0")
        return {
            "ok": output is not None,
            "mute": bool(muted),
            "error": None if output is not None else "Could not change input mute.",
        }

    def _capture_command(self, source: str) -> Optional[list[str]]:
        if self.capture_tool is None:
            return None
        tool_name = os.path.basename(self.capture_tool)
        if tool_name == "parec":
            return [
                self.capture_tool,
                f"--device={source}",
                "--format=s16le",
                f"--rate={SAMPLE_RATE}",
                f"--channels={CHANNELS}",
                "--latency-msec=20",
            ]
        if tool_name in ("pw-record", "pw-cat"):
            return [
                self.capture_tool,
                f"--target={source}",
                "--format=s16",
                f"--rate={SAMPLE_RATE}",
                f"--channels={CHANNELS}",
                "-",
            ]
        if tool_name == "arecord":
            return [
                self.capture_tool,
                "-q",
                "-f",
                "S16_LE",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(CHANNELS),
                "-t",
                "raw",
            ]
        return None

    def _playback_command(self, sink: str, wav_path: str) -> Optional[list[str]]:
        if self.play_tool is None:
            return None
        tool_name = os.path.basename(self.play_tool)
        if tool_name == "paplay":
            return [self.play_tool, f"--device={sink}", wav_path]
        if tool_name in ("pw-play", "pw-cat"):
            return [self.play_tool, f"--target={sink}", wav_path]
        if tool_name == "aplay":
            return [self.play_tool, "-q", wav_path]
        return None

    async def start_level_monitor(self, source: str) -> dict[str, Any]:
        if not source:
            return {"ok": False, "error": "No input device selected."}
        if self._capture_command(source) is None:
            return {"ok": False, "error": "No audio capture tool found."}

        async with self._meter_lock:
            await self._stop_level_monitor_locked()
            try:
                self._meter_task = asyncio.get_running_loop().create_task(
                    self._meter_loop(source)
                )
            except RuntimeError as exc:
                return {"ok": False, "error": f"Could not start level monitor: {exc}"}
        return {"ok": True}

    async def stop_level_monitor(self) -> dict[str, Any]:
        async with self._meter_lock:
            await self._stop_level_monitor_locked()
        return {"ok": True}

    async def _stop_level_monitor_locked(self):
        task = self._meter_task
        self._meter_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                decky.logger.debug("Level monitor task ended: %s", exc)

        proc = self._meter_proc
        self._meter_proc = None
        if proc is not None:
            await self._terminate(proc)

    async def _meter_loop(self, source: str):
        proc = None
        try:
            command = self._capture_command(source)
            if command is None:
                await decky.emit(
                    "mic_level",
                    {"rms_dbfs": -120.0, "peak_dbfs": -120.0, "clipping": False,
                     "error": "No audio capture tool found."},
                )
                return

            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
            )
            self._meter_proc = proc

            buffer = bytearray()
            chunks_since_emit = 0
            while True:
                data = await proc.stdout.read(CHUNK_BYTES)
                if not data:
                    break
                buffer.extend(data)
                while len(buffer) >= CHUNK_BYTES:
                    chunk = bytes(buffer[:CHUNK_BYTES])
                    del buffer[:CHUNK_BYTES]
                    chunks_since_emit += 1
                    if chunks_since_emit >= METER_INTERVAL_CHUNKS:
                        chunks_since_emit = 0
                        await decky.emit("mic_level", _audio_metrics(chunk))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            decky.logger.warning("Level monitor failed: %s", exc)
            await decky.emit(
                "mic_level",
                {"rms_dbfs": -120.0, "peak_dbfs": -120.0, "clipping": False,
                 "error": str(exc)},
            )
        finally:
            if proc is not None:
                await self._terminate(proc)
            if self._meter_proc is proc:
                self._meter_proc = None
            if self._meter_task is asyncio.current_task():
                self._meter_task = None

    async def start_recording(self, source: str) -> dict[str, Any]:
        if not source:
            return {"ok": False, "error": "No input device selected."}

        command = self._capture_command(source)
        if command is None:
            return {"ok": False, "error": "No audio capture tool found."}

        os.makedirs(self.runtime_dir, exist_ok=True)
        async with self._rec_lock:
            if self._rec_proc is not None:
                return {"ok": False, "error": "A recording is already in progress."}
            if self._rec_playing:
                return {"ok": False, "error": "Audio playback is already in progress."}

            try:
                fd, raw_path = tempfile.mkstemp(
                    prefix=TEMP_PREFIX, suffix=".raw", dir=self.runtime_dir
                )
                raw_file = os.fdopen(fd, "wb")
            except OSError as exc:
                return {"ok": False, "error": f"Could not create a temporary file: {exc}"}

            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=raw_file,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=self.env,
                )
            except (OSError, ValueError) as exc:
                raw_file.close()
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
                return {"ok": False, "error": f"Could not start recording: {exc}"}

            self._rec_proc = proc
            self._rec_file = raw_file
            self._rec_path = raw_path
            self._rec_started = asyncio.get_running_loop().time()
            await decky.emit("mic_test_status", {"phase": "recording"})
        return {"ok": True}

    async def stop_recording(self, sink: str) -> dict[str, Any]:
        async with self._rec_lock:
            proc = self._rec_proc
            raw_file = self._rec_file
            raw_path = self._rec_path
            started = self._rec_started

            self._rec_proc = None
            self._rec_file = None
            self._rec_path = None
            self._rec_started = 0.0

            if proc is None or raw_file is None or raw_path is None:
                return {
                    "ok": False,
                    "peak_dbfs": -120.0,
                    "rms_dbfs": -120.0,
                    "duration": 0.0,
                    "played": False,
                    "error": "No recording is in progress.",
                }

            await self._terminate(proc)
            try:
                raw_file.close()
            except OSError:
                pass

            elapsed = max(0.0, asyncio.get_running_loop().time() - started)
            try:
                raw_size = os.path.getsize(raw_path)
            except OSError:
                raw_size = 0

            if elapsed < MIN_RECORDING_SECONDS or raw_size < SAMPLE_RATE * SAMPLE_WIDTH // 2:
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
                await decky.emit("mic_test_status", {"phase": "idle"})
                return {
                    "ok": False,
                    "peak_dbfs": -120.0,
                    "rms_dbfs": -120.0,
                    "duration": round(elapsed, 2),
                    "played": False,
                    "error": "Recording was too short. Please try again.",
                }

            wav_path = os.path.splitext(raw_path)[0] + ".wav"
            try:
                metrics, duration = self._write_wav(raw_path, wav_path)
            except (OSError, wave.Error) as exc:
                decky.logger.warning("Could not finalize recording: %s", exc)
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
                await decky.emit("mic_test_status", {"phase": "idle"})
                return {
                    "ok": False,
                    "peak_dbfs": -120.0,
                    "rms_dbfs": -120.0,
                    "duration": round(elapsed, 2),
                    "played": False,
                    "error": f"Could not finalize recording: {exc}",
                }
            finally:
                try:
                    os.remove(raw_path)
                except OSError:
                    pass

            self._set_last_recording(wav_path, metrics, duration)
            self._rec_playing = True
            await decky.emit(
                "mic_test_status",
                {
                    "phase": "playing",
                    "duration": duration,
                    "peak_dbfs": metrics["peak_dbfs"],
                    "rms_dbfs": metrics["rms_dbfs"],
                },
            )

        playback_error: Optional[str] = "Playback was cancelled."
        try:
            playback_error = await self._play_clip(wav_path, sink, duration)
        finally:
            async with self._rec_lock:
                self._rec_playing = False
                await decky.emit(
                    "mic_test_status",
                    {
                        "phase": "idle",
                        "duration": duration,
                        "peak_dbfs": metrics["peak_dbfs"],
                        "rms_dbfs": metrics["rms_dbfs"],
                        "error": playback_error,
                    },
                )

        return {
            "ok": playback_error is None,
            "peak_dbfs": metrics["peak_dbfs"],
            "rms_dbfs": metrics["rms_dbfs"],
            "duration": duration,
            "played": playback_error is None,
            "error": playback_error,
        }

    async def play_last_recording(self, sink: str) -> dict[str, Any]:
        async with self._rec_lock:
            if self._rec_proc is not None:
                return {
                    "ok": False,
                    "peak_dbfs": -120.0,
                    "rms_dbfs": -120.0,
                    "duration": 0.0,
                    "played": False,
                    "error": "A recording is already in progress.",
                }
            if self._rec_playing:
                return {
                    "ok": False,
                    "peak_dbfs": -120.0,
                    "rms_dbfs": -120.0,
                    "duration": 0.0,
                    "played": False,
                    "error": "Audio playback is already in progress.",
                }

            wav_path = self._last_wav
            metrics = self._last_metrics
            if wav_path is None or metrics is None or not os.path.exists(wav_path):
                return {
                    "ok": False,
                    "peak_dbfs": -120.0,
                    "rms_dbfs": -120.0,
                    "duration": 0.0,
                    "played": False,
                    "error": "There is no recording to play yet.",
                }

            duration = float(metrics["duration"])
            self._rec_playing = True
            await decky.emit(
                "mic_test_status",
                {
                    "phase": "playing",
                    "duration": duration,
                    "peak_dbfs": metrics["peak_dbfs"],
                    "rms_dbfs": metrics["rms_dbfs"],
                },
            )

        playback_error: Optional[str] = "Playback was cancelled."
        try:
            playback_error = await self._play_clip(wav_path, sink, duration)
        finally:
            async with self._rec_lock:
                self._rec_playing = False
                await decky.emit(
                    "mic_test_status",
                    {
                        "phase": "idle",
                        "duration": duration,
                        "peak_dbfs": metrics["peak_dbfs"],
                        "rms_dbfs": metrics["rms_dbfs"],
                        "error": playback_error,
                    },
                )

        return {
            "ok": playback_error is None,
            "peak_dbfs": metrics["peak_dbfs"],
            "rms_dbfs": metrics["rms_dbfs"],
            "duration": duration,
            "played": playback_error is None,
            "error": playback_error,
        }

    async def cancel_recording(self) -> dict[str, Any]:
        async with self._rec_lock:
            proc = self._rec_proc
            raw_file = self._rec_file
            raw_path = self._rec_path

            if proc is None and raw_file is None and raw_path is None:
                return {"ok": True}

            self._rec_proc = None
            self._rec_file = None
            self._rec_path = None
            self._rec_started = 0.0

            if proc is not None:
                await self._terminate(proc)
            if raw_file is not None:
                try:
                    raw_file.close()
                except OSError:
                    pass
            if raw_path is not None:
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
            await decky.emit("mic_test_status", {"phase": "idle"})
        return {"ok": True}

    def _write_wav(self, raw_path: str, wav_path: str) -> tuple[dict[str, Any], float]:
        with open(raw_path, "rb") as raw_file:
            data = raw_file.read()
        usable = len(data) - (len(data) % SAMPLE_WIDTH)
        data = data[:usable]

        with wave.open(wav_path, "wb") as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(data)

        metrics = _audio_metrics(data)
        duration = len(data) / float(SAMPLE_RATE * SAMPLE_WIDTH)
        return metrics, round(duration, 2)

    def _set_last_recording(
        self, wav_path: str, metrics: dict[str, Any], duration: float
    ):
        old_path = self._last_wav
        if old_path and old_path != wav_path:
            try:
                os.remove(old_path)
            except OSError:
                pass
        self._last_wav = wav_path
        self._last_metrics = {
            "peak_dbfs": metrics["peak_dbfs"],
            "rms_dbfs": metrics["rms_dbfs"],
            "duration": duration,
        }

    async def _play_clip(
        self, wav_path: str, sink: str, duration: float
    ) -> Optional[str]:
        if not sink:
            return "No output device selected."
        command = self._playback_command(sink, wav_path)
        if command is None:
            return "No audio playback tool found."

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except (OSError, ValueError) as exc:
            return f"Could not start playback: {exc}"

        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=max(15.0, duration + 10.0)
            )
        except asyncio.TimeoutError:
            await self._terminate(proc)
            return "Playback timed out."
        except Exception as exc:
            await self._terminate(proc)
            return f"Playback failed: {exc}"

        if proc.returncode != 0:
            message = stderr.decode(errors="replace").strip()
            return message or f"Playback failed ({proc.returncode})."
        return None

    async def _terminate(self, proc: asyncio.subprocess.Process):
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        except Exception:
            pass
