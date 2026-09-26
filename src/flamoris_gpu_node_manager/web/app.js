"use strict";

const elements = {
  connection: document.querySelector("#connection"),
  currentRuntime: document.querySelector("#current-runtime"),
  refreshed: document.querySelector("#refreshed"),
  telemetry: document.querySelector("#telemetry"),
  telemetryNote: document.querySelector("#telemetry-note"),
  transition: document.querySelector("#transition"),
  runtimes: document.querySelector("#runtimes"),
  notice: document.querySelector("#notice"),
};

const nodeName = document.querySelector("#node-name").textContent;

let requestInFlight = false;
let pollTimer;

const stateLabels = {
  OFF: "OFF",
  STOPPING: "STOPPING",
  WAITING_FOR_GPU: "WAITING FOR GPU",
  STARTING: "STARTING",
  READY: "READY",
  FAILED: "FAILED",
};

const stepLabels = {
  inspect: "Inspecting runtime state",
  stop: "Stopping current runtime",
  "resource-release": "Waiting for GPU release",
  start: "Starting target runtime",
  "health-check": "Checking target health",
};

function text(tag, className, value) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  return element;
}

function setConnection(online) {
  elements.connection.className = `connection ${online ? "online" : "offline"}`;
  elements.connection.replaceChildren(
    text("span", "connection-dot", ""),
    text("span", "", `${nodeName} · ${online ? "ONLINE" : "OFFLINE"}`),
  );
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
}

function formatMetric(value, suffix, digits = 0) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(digits)}${suffix}`;
}

function metricCard(label, value, detail, progress) {
  const card = document.createElement("article");
  card.className = "metric";
  card.append(text("p", "metric-label", label), text("p", "metric-value", value));
  if (progress !== null) {
    const track = document.createElement("progress");
    track.className = "meter";
    track.max = 100;
    track.value = Math.max(0, Math.min(100, progress));
    card.append(track);
  }
  card.append(text("p", "metric-detail", detail));
  return card;
}

function renderTelemetry(telemetry) {
  const used = telemetry.vram_used_bytes;
  const total = telemetry.vram_total_bytes;
  const vramPercent = used !== null && total ? (used / total) * 100 : null;
  elements.telemetry.replaceChildren(
    metricCard(
      "VRAM",
      used === null || used === undefined ? "—" : formatBytes(used),
      total ? `${formatBytes(total)} total` : "Total unavailable",
      vramPercent,
    ),
    metricCard(
      "GPU LOAD",
      formatMetric(telemetry.gpu_utilization_percent, "%"),
      "Current engine utilization",
      telemetry.gpu_utilization_percent ?? null,
    ),
    metricCard(
      "TEMPERATURE",
      formatMetric(telemetry.temperature_celsius, "°C", 1),
      "GPU edge sensor",
      null,
    ),
    metricCard(
      "POWER",
      formatMetric(telemetry.power_watts, " W", 1),
      "Board power when exposed",
      null,
    ),
  );
  elements.telemetryNote.textContent = telemetry.available
    ? (telemetry.error || "Live from AMDGPU sysfs")
    : (telemetry.error || "Telemetry unavailable");
}

function renderTransition(status) {
  const transition = status.transition;
  const active = Boolean(transition && !transition.failure_reason);
  const failure = transition?.failure_reason
    || status.runtimes.find((runtime) => runtime.failure_reason)?.failure_reason;

  elements.transition.className = `transition ${failure ? "failure" : active ? "active" : "stable"}`;
  const indicator = text("span", "transition-indicator", "");
  const copy = document.createElement("div");
  copy.append(
    text(
      "p",
      "transition-title",
      failure ? "Last transition failed" : active ? (stepLabels[transition.step] || transition.step) : "System stable",
    ),
    text(
      "p",
      "transition-detail",
      failure
        ? failure
        : active
          ? `${transition.from_runtime || "idle"} → ${transition.target_runtime || "off"}`
          : "No runtime transition is in progress",
    ),
  );
  elements.transition.replaceChildren(indicator, copy);
  return active;
}

function runtimeCard(runtime, transitionActive) {
  const status = runtime.status;
  const card = document.createElement("article");
  card.className = `runtime-card state-${status.state.toLowerCase()}`;
  card.dataset.runtimeId = runtime.id;

  const heading = document.createElement("div");
  heading.className = "runtime-heading";
  const titleGroup = document.createElement("div");
  titleGroup.append(
    text("h3", "runtime-name", runtime.display_name),
    text("p", "runtime-id", runtime.id),
  );
  heading.append(titleGroup, text("span", "state-badge", stateLabels[status.state] || status.state));

  const service = text("p", "runtime-service", runtime.service);
  const failure = text("p", "runtime-failure", status.failure_reason || "");
  failure.hidden = !status.failure_reason;

  const button = document.createElement("button");
  const isReady = status.state === "READY";
  button.type = "button";
  button.textContent = isReady ? "Stop" : runtime.enabled ? "Activate" : "Unavailable";
  button.className = isReady ? "action stop" : "action activate";
  button.disabled = requestInFlight || transitionActive || (!runtime.enabled && !isReady);
  button.addEventListener("click", () => mutate(runtime.id, isReady ? "stop" : "activate"));

  card.append(heading, service, failure, button);
  return card;
}

function render(status, runtimes) {
  setConnection(true);
  elements.currentRuntime.textContent = status.current_runtime
    ? runtimes.find((item) => item.id === status.current_runtime)?.display_name || status.current_runtime
    : "No active runtime";
  elements.refreshed.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  renderTelemetry(status.telemetry);
  const transitionActive = renderTransition(status);
  elements.runtimes.replaceChildren(
    ...runtimes.map((runtime) => runtimeCard(runtime, transitionActive)),
  );
}

async function readJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error?.message || `Request failed (${response.status})`);
  }
  return payload;
}

function apiUrl(path) {
  return new URL(`./api/${path}`, document.baseURI);
}

async function refresh() {
  try {
    const [status, runtimes] = await Promise.all([
      readJson(apiUrl("status")),
      readJson(apiUrl("runtimes")),
    ]);
    render(status, runtimes);
  } catch (error) {
    setConnection(false);
    elements.notice.textContent = error.message;
  } finally {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refresh, 1000);
  }
}

async function mutate(runtimeId, action) {
  if (requestInFlight) return;
  requestInFlight = true;
  elements.notice.textContent = "Request sent. Waiting for manager authority…";
  elements.runtimes.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });
  try {
    await readJson(apiUrl(`runtimes/${encodeURIComponent(runtimeId)}/${action}`), {
      method: "POST",
      headers: { "X-GPU-Node-Manager-Intent": "runtime-mutation" },
    });
    elements.notice.textContent = "";
  } catch (error) {
    elements.notice.textContent = error.message;
  } finally {
    requestInFlight = false;
    await refresh();
  }
}

refresh();
