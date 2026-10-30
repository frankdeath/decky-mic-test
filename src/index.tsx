import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  ProgressBar,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import type { SingleDropdownOption } from "@decky/ui";
import {
  addEventListener,
  removeEventListener,
  callable,
  definePlugin,
  toaster,
  useQuickAccessVisible,
} from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { FaMicrophone } from "react-icons/fa";

interface Device {
  name: string;
  description: string;
  volume: number;
  mute: boolean;
  is_default: boolean;
}

interface AudioState {
  sinks: Device[];
  sources: Device[];
  default_sink: string | null;
  default_source: string | null;
  error: string | null;
}

interface ActionResult {
  ok: boolean;
  error?: string | null;
}

interface VolumeResult extends ActionResult {
  volume: number;
}

interface MuteResult extends ActionResult {
  mute: boolean;
}

interface TestResult {
  ok: boolean;
  peak_dbfs: number;
  rms_dbfs: number;
  duration: number;
  played: boolean;
  error?: string | null;
}

interface Level {
  rms_dbfs: number;
  peak_dbfs: number;
  clipping: boolean;
  error?: string | null;
}

type TestPhase = "idle" | "recording" | "playing";

interface TestStatus {
  phase: TestPhase;
  duration?: number;
  peak_dbfs?: number;
  rms_dbfs?: number;
  error?: string | null;
}

const getAudioState = callable<[], AudioState>("get_audio_state");
const setDefaultSink = callable<[name: string], AudioState>("set_default_sink");
const setDefaultSource = callable<[name: string], AudioState>("set_default_source");
const setSinkVolume = callable<[name: string, percent: number], VolumeResult>(
  "set_sink_volume",
);
const setSourceVolume = callable<[name: string, percent: number], VolumeResult>(
  "set_source_volume",
);
const setSinkMute = callable<[name: string, muted: boolean], MuteResult>(
  "set_sink_mute",
);
const setSourceMute = callable<[name: string, muted: boolean], MuteResult>(
  "set_source_mute",
);
const startLevelMonitor = callable<[source: string], ActionResult>(
  "start_level_monitor",
);
const stopLevelMonitor = callable<[], ActionResult>("stop_level_monitor");
const startRecording = callable<[source: string], ActionResult>("start_recording");
const stopRecording = callable<[sink: string], TestResult>("stop_recording");
const playLastRecording = callable<[sink: string], TestResult>(
  "play_last_recording",
);
const cancelRecording = callable<[], ActionResult>("cancel_recording");

function toMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function meterPercent(level: Level | null): number {
  if (!level) {
    return 0;
  }
  return Math.max(0, Math.min(100, ((level.peak_dbfs + 60) / 60) * 100));
}

function Content() {
  const [state, setState] = useState<AudioState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [meterEnabled, setMeterEnabled] = useState(true);
  const [level, setLevel] = useState<Level | null>(null);
  const [phase, setPhase] = useState<TestPhase>("idle");
  const [recordSeconds, setRecordSeconds] = useState(0);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [hasLastClip, setHasLastClip] = useState(false);
  const [starting, setStarting] = useState(false);
  const quickAccessVisible = useQuickAccessVisible();
  const quickAccessVisibleRef = useRef(quickAccessVisible);
  quickAccessVisibleRef.current = quickAccessVisible;

  const adjustingUntilRef = useRef(0);
  const meterDesiredRef = useRef(false);
  const sinkVolumeTimer = useRef<number | undefined>(undefined);
  const sourceVolumeTimer = useRef<number | undefined>(undefined);

  const reportError = (message: string) => {
    setError(message);
    toaster.toast({ title: "Mic Test", body: message });
  };

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (Date.now() < adjustingUntilRef.current) {
        return;
      }
      try {
        const next = await getAudioState();
        if (cancelled) {
          return;
        }
        setState(next);
        if (next.error) {
          setError(next.error);
        }
      } catch (caught) {
        if (!cancelled) {
          setError(toMessage(caught));
        }
      }
    };

    load();
    const interval = window.setInterval(load, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    const levelListener = addEventListener<[incoming: Level]>(
      "mic_level",
      (incoming) => {
        if (incoming.error) {
          setError(incoming.error);
          return;
        }
        setLevel(incoming);
      },
    );
    const statusListener = addEventListener<[status: TestStatus]>(
      "mic_test_status",
      (status) => {
        setPhase(status.phase);
        if (status.error) {
          setError(status.error);
        }
      },
    );

    return () => {
      removeEventListener("mic_level", levelListener);
      removeEventListener("mic_test_status", statusListener);
    };
  }, []);

  const sinks = state?.sinks ?? [];
  const sources = state?.sources ?? [];
  const defaultSinkName = state?.default_sink ?? null;
  const defaultSourceName = state?.default_source ?? null;
  const selectedSink =
    sinks.find((device) => device.name === defaultSinkName) ??
    sinks[0] ??
    null;
  const selectedSource =
    sources.find((device) => device.name === defaultSourceName) ??
    sources[0] ??
    null;
  const selectedSinkName = selectedSink?.name ?? null;
  const selectedSourceName = selectedSource?.name ?? null;

  useEffect(() => {
    const desired =
      meterEnabled && quickAccessVisible && Boolean(selectedSourceName);
    meterDesiredRef.current = desired;
    if (!desired || !selectedSourceName) {
      setLevel(null);
      stopLevelMonitor().catch(() => undefined);
      return;
    }

    let cancelled = false;
    startLevelMonitor(selectedSourceName)
      .then((result) => {
        if (cancelled) {
          if (!meterDesiredRef.current) {
            stopLevelMonitor().catch(() => undefined);
          }
          return;
        }
        if (!result.ok) {
          setError(result.error ?? "Could not start the level meter.");
        }
      })
      .catch((caught) => {
        if (!cancelled) {
          setError(toMessage(caught));
        }
      });

    return () => {
      cancelled = true;
    };
  }, [meterEnabled, quickAccessVisible, selectedSourceName]);

  useEffect(() => {
    if (!quickAccessVisible && phase === "recording") {
      cancelRecording().catch(() => undefined);
      setPhase("idle");
    }
  }, [quickAccessVisible, phase]);

  useEffect(() => {
    return () => {
      meterDesiredRef.current = false;
      stopLevelMonitor().catch(() => undefined);
      cancelRecording().catch(() => undefined);
      if (sinkVolumeTimer.current !== undefined) {
        window.clearTimeout(sinkVolumeTimer.current);
      }
      if (sourceVolumeTimer.current !== undefined) {
        window.clearTimeout(sourceVolumeTimer.current);
      }
    };
  }, []);

  useEffect(() => {
    if (phase !== "recording") {
      setRecordSeconds(0);
      return;
    }
    const started = Date.now();
    const interval = window.setInterval(() => {
      setRecordSeconds((Date.now() - started) / 1000);
    }, 200);
    return () => window.clearInterval(interval);
  }, [phase]);

  const handleSinkChange = async (option: SingleDropdownOption) => {
    const name = option.data as string;
    if (!name) {
      return;
    }
    setError(null);
    setState((previous) =>
      previous ? { ...previous, default_sink: name } : previous,
    );
    try {
      const next = await setDefaultSink(name);
      setState(next);
      if (next.error) {
        reportError(next.error);
      }
    } catch (caught) {
      reportError(toMessage(caught));
    }
  };

  const handleSourceChange = async (option: SingleDropdownOption) => {
    const name = option.data as string;
    if (!name) {
      return;
    }
    setError(null);
    setState((previous) =>
      previous ? { ...previous, default_source: name } : previous,
    );
    try {
      const next = await setDefaultSource(name);
      setState(next);
      if (next.error) {
        reportError(next.error);
      }
    } catch (caught) {
      reportError(toMessage(caught));
    }
  };

  const handleSinkVolume = (value: number) => {
    if (!selectedSinkName) {
      return;
    }
    const name = selectedSinkName;
    adjustingUntilRef.current = Date.now() + 1200;
    setState((previous) =>
      previous
        ? {
            ...previous,
            sinks: previous.sinks.map((device) =>
              device.name === name ? { ...device, volume: value } : device,
            ),
          }
        : previous,
    );

    if (sinkVolumeTimer.current !== undefined) {
      window.clearTimeout(sinkVolumeTimer.current);
    }
    sinkVolumeTimer.current = window.setTimeout(() => {
      setSinkVolume(name, value)
        .then((result) => {
          if (!result.ok) {
            reportError(result.error ?? "Could not set output volume.");
          }
        })
        .catch((caught) => reportError(toMessage(caught)));
    }, 150);
  };

  const handleSourceVolume = (value: number) => {
    if (!selectedSourceName) {
      return;
    }
    const name = selectedSourceName;
    adjustingUntilRef.current = Date.now() + 1200;
    setState((previous) =>
      previous
        ? {
            ...previous,
            sources: previous.sources.map((device) =>
              device.name === name ? { ...device, volume: value } : device,
            ),
          }
        : previous,
    );

    if (sourceVolumeTimer.current !== undefined) {
      window.clearTimeout(sourceVolumeTimer.current);
    }
    sourceVolumeTimer.current = window.setTimeout(() => {
      setSourceVolume(name, value)
        .then((result) => {
          if (!result.ok) {
            reportError(result.error ?? "Could not set input volume.");
          }
        })
        .catch((caught) => reportError(toMessage(caught)));
    }, 150);
  };

  const handleSinkMute = async (muted: boolean) => {
    if (!selectedSinkName) {
      return;
    }
    const name = selectedSinkName;
    adjustingUntilRef.current = Date.now() + 1200;
    setState((previous) =>
      previous
        ? {
            ...previous,
            sinks: previous.sinks.map((device) =>
              device.name === name ? { ...device, mute: muted } : device,
            ),
          }
        : previous,
    );
    try {
      const result = await setSinkMute(name, muted);
      if (!result.ok) {
        reportError(result.error ?? "Could not change output mute.");
      }
    } catch (caught) {
      reportError(toMessage(caught));
    }
  };

  const handleSourceMute = async (muted: boolean) => {
    if (!selectedSourceName) {
      return;
    }
    const name = selectedSourceName;
    adjustingUntilRef.current = Date.now() + 1200;
    setState((previous) =>
      previous
        ? {
            ...previous,
            sources: previous.sources.map((device) =>
              device.name === name ? { ...device, mute: muted } : device,
            ),
          }
        : previous,
    );
    try {
      const result = await setSourceMute(name, muted);
      if (!result.ok) {
        reportError(result.error ?? "Could not change input mute.");
      }
    } catch (caught) {
      reportError(toMessage(caught));
    }
  };

  const handleTest = async () => {
    if (starting) {
      return;
    }

    if (phase === "playing") {
      return;
    }

    if (phase === "recording") {
      setStarting(true);
      setPhase("playing");
      try {
        const result = await stopRecording(selectedSinkName ?? "");
        setTestResult(result);
        setHasLastClip((previous) => previous || result.duration > 0);
        if (!result.ok) {
          reportError(result.error ?? "The microphone test failed.");
        }
        setPhase("idle");
      } catch (caught) {
        reportError(toMessage(caught));
        setPhase("idle");
      } finally {
        setStarting(false);
      }
      return;
    }

    if (!selectedSourceName) {
      reportError("No microphone is available.");
      return;
    }

    setStarting(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await startRecording(selectedSourceName);
      if (result.ok) {
        setPhase("recording");
        if (!quickAccessVisibleRef.current) {
          await cancelRecording().catch(() => undefined);
          setPhase("idle");
        }
      } else {
        reportError(result.error ?? "Could not start recording.");
      }
    } catch (caught) {
      reportError(toMessage(caught));
    } finally {
      setStarting(false);
    }
  };

  const handleReplay = async () => {
    if (starting || phase === "playing" || !hasLastClip) {
      return;
    }
    setStarting(true);
    setPhase("playing");
    try {
      const result = await playLastRecording(selectedSinkName ?? "");
      if (!result.ok) {
        reportError(result.error ?? "Could not play the last recording.");
      }
      setPhase("idle");
    } catch (caught) {
      reportError(toMessage(caught));
      setPhase("idle");
    } finally {
      setStarting(false);
    }
  };

  if (!state) {
    return (
      <PanelSection title="Mic Test" spinner>
        <PanelSectionRow>
          <Field label="Loading audio devices..." />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const recording = phase === "recording";
  const playing = phase === "playing";
  const progress = meterPercent(level);

  return (
    <>
      {error && (
        <PanelSection>
          <PanelSectionRow>
            <Field
              label={<span style={{ color: "#ff6b6b" }}>{error}</span>}
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Output">
        <PanelSectionRow>
          <DropdownItem
            key={`sink-${selectedSinkName ?? "none"}`}
            label="Output device"
            rgOptions={sinks.map((device) => ({
              data: device.name,
              label: device.description,
            }))}
            selectedOption={selectedSinkName}
            disabled={sinks.length === 0}
            onChange={(option) => {
              void handleSinkChange(option);
            }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Output volume"
            value={selectedSink?.volume ?? 0}
            min={0}
            max={100}
            step={1}
            showValue
            valueSuffix="%"
            disabled={!selectedSink}
            onChange={handleSinkVolume}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Mute output"
            checked={selectedSink?.mute ?? false}
            disabled={!selectedSink}
            onChange={(muted) => {
              void handleSinkMute(muted);
            }}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Input">
        <PanelSectionRow>
          <DropdownItem
            key={`source-${selectedSourceName ?? "none"}`}
            label="Input device"
            rgOptions={sources.map((device) => ({
              data: device.name,
              label: device.description,
            }))}
            selectedOption={selectedSourceName}
            disabled={sources.length === 0 || recording}
            onChange={(option) => {
              void handleSourceChange(option);
            }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Input volume"
            value={selectedSource?.volume ?? 0}
            min={0}
            max={100}
            step={1}
            showValue
            valueSuffix="%"
            disabled={!selectedSource}
            onChange={handleSourceVolume}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Mute input"
            checked={selectedSource?.mute ?? false}
            disabled={!selectedSource}
            onChange={(muted) => {
              void handleSourceMute(muted);
            }}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Live input level">
        <PanelSectionRow>
          <ToggleField
            label="Live level meter"
            description={
              selectedSource?.description ?? "No microphone is available"
            }
            checked={meterEnabled}
            onChange={(enabled) => setMeterEnabled(enabled)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="Input level"
            description={
              level
                ? `${level.clipping ? "CLIPPING - " : ""}Peak ${level.peak_dbfs.toFixed(1)} dBFS - RMS ${level.rms_dbfs.toFixed(1)} dBFS`
                : meterEnabled
                  ? "Waiting for audio..."
                  : "Meter is off"
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ProgressBar nProgress={progress} focusable={false} />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Microphone test">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={starting || playing || (!recording && !selectedSource)}
            onClick={() => {
              void handleTest();
            }}
          >
            {recording
              ? `Stop & play back (${recordSeconds.toFixed(1)}s)`
              : playing
                ? "Playing..."
                : "Start test recording"}
          </ButtonItem>
        </PanelSectionRow>
        {testResult && (
          <PanelSectionRow>
            <Field
              label={`Peak ${testResult.peak_dbfs.toFixed(1)} dBFS - RMS ${testResult.rms_dbfs.toFixed(1)} dBFS`}
              description={`Clip length: ${testResult.duration.toFixed(1)}s${
                testResult.error ? ` - ${testResult.error}` : ""
              }`}
            />
          </PanelSectionRow>
        )}
        {hasLastClip && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={starting || playing || !selectedSink}
              onClick={() => {
                void handleReplay();
              }}
            >
              Play last recording
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  return {
    name: "Mic Test",
    titleView: <div className={staticClasses.Title}>Mic Test</div>,
    content: <Content />,
    icon: <FaMicrophone />,
    onDismount() {
      stopLevelMonitor().catch(() => undefined);
      cancelRecording().catch(() => undefined);
    },
  };
});
