/* Live figures for the browser pages.
 *
 * A plain static file: nothing on the server renders it, so it carries no
 * template markup. The endpoint and the interval arrive as `data-` attributes
 * on <body>, which is how the server stays in charge of both.
 *
 * Every value that came from the network is written with textContent. A station
 * label is a URL that somebody typed, so building HTML from it would be an
 * injection hole in a page that is otherwise entirely read-only.
 */
"use strict";

(function () {
  const body = document.body;
  const endpoint = body.dataset.metricsUrl || "/metrics.json";
  const intervalMs = Number(body.dataset.refreshMs) || 1000;

  let snapshot = null;
  let failures = 0;
  let inFlight = false;

  /* ------------------------------------------------------------- formatting */

  const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

  function bytes(value) {
    let n = Number(value) || 0;
    let unit = 0;
    while (n >= 1024 && unit < BYTE_UNITS.length - 1) {
      n /= 1024;
      unit += 1;
    }
    // 1024-based, to match the `rss_mb` that /health has always reported.
    return (unit === 0 ? String(n) : n.toFixed(n < 10 ? 1 : 0)) + " " + BYTE_UNITS[unit];
  }

  function duration(value) {
    const total = Math.max(0, Math.floor(Number(value) || 0));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    if (days) return days + "d " + hours + "h";
    if (hours) return hours + "h " + minutes + "m";
    if (minutes) return minutes + "m " + seconds + "s";
    return seconds + "s";
  }

  function percent(value) {
    return (Number(value) || 0).toFixed(1) + "%";
  }

  function millis(value) {
    const ms = (Number(value) || 0) * 1000;
    return (ms < 10 ? ms.toFixed(1) : Math.round(ms)) + " ms";
  }

  function count(value) {
    return (Number(value) || 0).toLocaleString();
  }

  const FORMATTERS = { bytes, duration, percent, millis, count };
  const PLACEHOLDER = "\u2013"; // en dash: reads as "no reading yet"

  /* ----------------------------------------------------------------- reading */

  function lookup(path) {
    if (!snapshot) return undefined;
    return path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), snapshot);
  }

  /* ---------------------------------------------------------------- painting */

  function paintMetrics() {
    document.querySelectorAll("[data-metric]").forEach((element) => {
      const raw = lookup(element.dataset.metric);
      if (raw === undefined || raw === null) {
        element.textContent = PLACEHOLDER;
        return;
      }
      const format = FORMATTERS[element.dataset.format];
      element.textContent = format ? format(raw) : String(raw);
    });
  }

  /* What the pill says, as one pure function so the suite can drive every
   * state without a browser (tests/test_web_ui.py runs this file's copy of it
   * under Node).
   *
   * `status` alone cannot answer "is this live?". The snapshot reports "ok"
   * for an idle service too - correctly, because idle is healthy and not
   * degraded - so the old `status === "ok" -> "live"` claimed "live" on any
   * install with no radio attached, and "nothing streaming" could never
   * appear: it lived only on the `degraded` branch, which needs a session to
   * be wrong. The session count is what tells idle from live; `status` then
   * separates "streaming" from "streaming, but somebody is hearing silence".
   *
   * `failures` outranks any snapshot: stale numbers are worse than none, so a
   * failed poll must not say "live" while nothing is arriving. That state is
   * reachable even though the page is served by the same process - it appears
   * the moment the service is stopped or dies (watchdog, restart, network)
   * while the page is already open.
   */
  function statusFor(snap, failures) {
    if (failures) return { state: "down", text: "no contact" };
    if (!snap) return { state: "unknown", text: "connecting\u2026" };
    const sessions = Number((snap.gauges || {}).sessions) || 0;
    if (sessions === 0) return { state: "idle", text: "nothing streaming" };
    if (snap.status === "ok") return { state: "ok", text: "live" };
    return { state: "degraded", text: "nothing streaming" };
  }

  function paintStatus() {
    const pill = document.getElementById("live-state");
    if (!pill) return;

    const shown = statusFor(snapshot, failures);
    const hint = "Reading " + endpoint + " every " + intervalMs / 1000 + "s.";

    // `data-tooltip`, never `title` - the same rule as every other description
    // on this page: the native tooltip is delayed about a second and drawn by
    // the browser, and the `cursor: help` that hints at it renders as a literal
    // question mark. The bubble says why the pill reads what it reads.
    const TIP = {
      unknown: "Waiting for the first reading from " + endpoint + ".",
      down: "Could not read " + endpoint + " - is the service running?",
      ok: hint,
      idle: "The service is up, but no station is streaming right now. " + hint,
      degraded:
        "Sessions exist but their listeners have nothing to play - see the " +
        "table below. " + hint,
    };

    pill.hidden = false;
    pill.dataset.state = shown.state;
    pill.textContent = shown.text;
    pill.dataset.tooltip = TIP[shown.state] || hint;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function engineLabel(pipeline) {
    const mode = pipeline.mode || "starting";
    const kind = pipeline.detected_kind || pipeline.detected_codec;
    if (mode === "unknown") return "starting\u2026";
    return kind ? mode + " \u00b7 " + kind : mode;
  }

  /* Seconds of audio buffered for this station's listeners.
   *
   * Taken straight from the server rather than derived here from a chunk count.
   * It used to be computed in this file as `queued_chunks * chunk_bytes / rate`,
   * where `queued_chunks` is the TOTAL across listeners - while starvation used
   * the MINIMUM. With two connections present the page could therefore show a
   * healthy buffer beside a climbing starvation figure, which is exactly the
   * contradiction that was reported. One figure, one place, one aggregation.
   */
  function bufferedSeconds(detail) {
    const seconds = detail.buffered_seconds;
    if (seconds === undefined || seconds === null) return PLACEHOLDER;
    return Number(seconds).toFixed(1) + "s";
  }

  function streamRow(detail) {
    const pipeline = detail.pipeline || {};
    const session = detail.session || {};
    const subscribers = detail.subscribers || [];
    const dropped = subscribers.reduce((total, sub) => total + (sub.dropped || 0), 0);

    const row = element("tr");
    if (detail.fatal_error) {
      row.className = "row-dead";
      row.dataset.tooltip = detail.fatal_error;
    } else if (detail.stalled) {
      // A station whose listeners have nothing buffered is a problem an operator
      // must be able to see, not infer from an idle counter climbing. Judged from
      // the listeners' side, so the normal gap between segments does not trip it.
      row.className = "row-stalled";
      row.dataset.tooltip =
        "Listeners have had nothing buffered for " + duration(detail.starved_for) +
        " - long enough to be heard as silence";
    }

    const station = element("td");
    station.appendChild(element("div", "station", detail.label || detail.key || detail.id));
    station.appendChild(
      element(
        "div",
        "muted small",
        "#" + detail.id +
          " \u00b7 source " + ((pipeline.source_index || 0) + 1) + "/" + detail.sources +
          (pipeline.failovers ? " \u00b7 " + pipeline.failovers + " failovers" : "")
      )
    );
    row.appendChild(station);

    row.appendChild(element("td", "num", count(detail.listeners)));
    row.appendChild(element("td", "num", dropped ? count(dropped) : "\u2013"));
    row.appendChild(element("td", null, engineLabel(pipeline)));
    row.appendChild(element("td", "num", bufferedSeconds(detail)));
    // `bytes_sent`, not `bytes_out`: the latter counts what the pipeline
    // *produced*, which includes audio for a warm station nobody is listening to
    // and audio dropped for a listener that fell behind. The column and the
    // "audio sent" tile must mean the same thing or one of them is lying.
    row.appendChild(element("td", "num", bytes(session.bytes_sent)));
    row.appendChild(element("td", "num", count(session.restarts)));
    row.appendChild(element("td", "num", count(pipeline.failures)));
    row.appendChild(element("td", "num", duration(detail.starved_for)));
    row.appendChild(element("td", "num", duration(pipeline.idle)));
    row.appendChild(element("td", "num", duration(session.uptime)));
    return row;
  }

  function paintStreams() {
    const tbody = document.getElementById("streams");
    if (!tbody || !snapshot) return;
    const details = (snapshot.streams && snapshot.streams.details) || [];

    tbody.textContent = "";
    if (!details.length) {
      const cell = element("td", "empty", "Nothing is playing right now.");
      cell.colSpan = 11;
      tbody.appendChild(element("tr")).appendChild(cell);
      return;
    }
    details.forEach((detail) => tbody.appendChild(streamRow(detail)));
  }

  function paintAll() {
    paintMetrics();
    paintStatus();
    paintStreams();
  }

  /* --------------------------------------------------------------- tooltips */

  /* One shared element, positioned with `fixed` coordinates.
   *
   * The obvious implementation is a `title` attribute, and that is what this page
   * used first. It was wrong twice over: the browser delays its tooltip by around
   * a second and styles it itself, and `cursor: help` - the usual hint that one is
   * there - renders as a literal question mark, so the page appears to offer a "?"
   * and nothing else.
   *
   * A CSS-only tooltip (`::after` on the element) is the other obvious option and
   * is worse still here: the stations table scrolls horizontally, and a tooltip
   * inside an `overflow: auto` box is clipped by it. A single fixed-position node
   * has neither problem, and lets the bubble flip when it would leave the screen.
   *
   * `data-tooltip`, not `title`, so the native tooltip cannot appear as well.
   */
  const tip = { node: null, owner: null };

  function tipNode() {
    if (!tip.node) {
      const node = element("div");
      node.id = "tooltip";
      node.setAttribute("role", "tooltip");
      // content is set with textContent, never innerHTML - the same rule as
      // everywhere else in this file.
      document.body.appendChild(node);
      tip.node = node;
    }
    return tip.node;
  }

  function placeTip(owner, node) {
    const margin = 8;
    const anchor = owner.getBoundingClientRect();
    const box = node.getBoundingClientRect();

    // Above by default, below when there is no room - which is what keeps a
    // tooltip on the first row of the table (and on the tiles) on screen.
    let top = anchor.top - box.height - margin;
    if (top < margin) top = anchor.bottom + margin;

    // Clamped to the viewport, so a tooltip on the rightmost column does not
    // hang off the edge. Left-aligned to its trigger while it fits.
    let left = anchor.left;
    const widest = window.innerWidth - box.width - margin;
    if (left > widest) left = Math.max(margin, widest);

    node.style.top = Math.round(top) + "px";
    node.style.left = Math.round(left) + "px";
  }

  function showTip(owner) {
    const text = owner.dataset.tooltip;
    if (!text) return;
    const node = tipNode();
    node.textContent = text;
    node.dataset.open = "true";
    tip.owner = owner;
    placeTip(owner, node);
  }

  function hideTip() {
    if (tip.node) tip.node.dataset.open = "false";
    tip.owner = null;
  }

  // Delegated, so rows built later by `paintStreams` are covered without
  // rebinding anything on every poll.
  document.addEventListener("pointerover", (event) => {
    const owner = event.target.closest("[data-tooltip]");
    if (owner) showTip(owner);
    else hideTip();
  });

  document.addEventListener("pointerout", (event) => {
    const owner = event.target.closest("[data-tooltip]");
    // `relatedTarget` is where the pointer went. Moving onto a child of the same
    // tile must not dismiss the tooltip.
    if (owner && !owner.contains(event.relatedTarget)) hideTip();
  });

  // Keyboard and touch: `focus-visible`/tap focus lands here, which is the only
  // way a phone gets a tooltip at all - there is no hover on a touch screen.
  document.addEventListener("focusin", (event) => {
    const owner = event.target.closest("[data-tooltip]");
    if (owner) showTip(owner);
  });
  document.addEventListener("focusout", hideTip);

  // A tooltip pointing at where its trigger *was* is worse than none.
  window.addEventListener("scroll", hideTip, { passive: true });
  window.addEventListener("resize", hideTip);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideTip();
  });

  /* ---------------------------------------------------------------- polling */

  async function tick() {
    // A hidden tab is a browser nobody is looking at; on a Raspberry Pi the
    // polite thing is to stop asking. The tab polls once more when it returns.
    if (document.hidden || inFlight) return;
    inFlight = true;
    try {
      const response = await fetch(endpoint, { cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      snapshot = await response.json();
      failures = 0;
      paintAll();
    } catch (error) {
      failures += 1;
      // Once, and then rarely: a Pi left overnight with the service stopped
      // should not fill a console with one line per second.
      if (failures === 1 || failures % 60 === 0) {
        console.warn("radioconv: cannot read " + endpoint + " (" + error + ")");
      }
      paintStatus();
    } finally {
      inFlight = false;
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) tick();
  });

  paintAll();
  tick();
  setInterval(tick, intervalMs);
})();
