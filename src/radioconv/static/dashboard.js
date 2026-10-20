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

  /* A table cell that also knows its own name, and what it counts.
   *
   * On a phone the station table is laid out as one card per station (see the
   * narrow-screen block in dashboard.css) and the header row is hidden - so
   * each cell prints `data-label` in front of its value, because "8.9s" alone
   * on a line says nothing, and carries the column's description, because the
   * explanation is otherwise attached to a heading nobody can see or tap.
   *
   * Both come from the heading itself rather than being repeated here: the
   * header row is the one place that says what a column means, and a second
   * copy in this file is the copy nobody re-reads.
   *
   * `role` is set explicitly for the same reason the template sets it: the card
   * layout uses `display: block`, which otherwise removes the table from the
   * accessibility tree and leaves a screen reader with a pile of loose text.
   */
  let headings = null;

  function heading(index) {
    if (!headings) {
      headings = Array.from(document.querySelectorAll(".stations thead th")).map((th) => ({
        label: th.textContent.trim(),
        tip: th.dataset.tooltip || "",
      }));
    }
    return headings[index];
  }

  function cell(index, className, text, key) {
    const node = element("td", className, text);
    const column = heading(index);
    if (column) {
      node.dataset.label = column.label;
      // The Station cell is the card's title, and it is where a stalled or dead
      // row states its problem (see streamRow) - a description of its own would
      // sit on top of that.
      if (column.tip && index > 0) {
        node.dataset.tooltip = column.tip;
        // Focusable, like every other description on the page. A cell that
        // carries a description and cannot be reached is a description that
        // only a mouse can read - and the point of the table on a phone is
        // that it can be read without one.
        node.tabIndex = 0;
      }
    }
    // The key names this cell in a way that survives the table being rebuilt
    // every second: a station and a column, not a DOM node (see restoreTip).
    if (key) node.dataset.tipKey = key;
    node.setAttribute("role", "cell");
    return node;
  }

  /* Whether ffmpeg is running, and nothing else.
   *
   * The codec used to be appended here as `transcode \u00b7 mp3`, taken from
   * `detected_kind` (the SOURCE kind) with `detected_codec` (the OUTPUT kind)
   * as a fallback. The same slot therefore meant two different things: the
   * same station with an ADTS-in-TS source read `transcode \u00b7 adts`, while
   * one serving a bare `.aac` segment read `transcode \u00b7 mp3`, and neither
   * said what was actually happening. A listener only needs the engine.
   */
  function engineLabel(pipeline) {
    const mode = pipeline.mode || "starting";
    if (mode === "unknown") return "starting\u2026";
    return mode;
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
    row.setAttribute("role", "row");
    // The key names this station's row in a way that survives the table being
    // rebuilt every second (see restoreTip). The session id is unique for as
    // long as the station exists.
    const key = detail.id || detail.key || "";
    if (key) row.dataset.tipKey = "row:" + key;
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

    // No label of its own: on a phone this cell is the card's title, and the
    // place a stalled or dead row explains itself (see above).
    const station = cell(0, "station-cell", undefined, key ? "cell:" + key + ":0" : null);
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

    row.appendChild(cell(1, "num", count(detail.listeners), key ? "cell:" + key + ":1" : null));
    row.appendChild(cell(2, "num", dropped ? count(dropped) : "\u2013", key ? "cell:" + key + ":2" : null));
    row.appendChild(cell(3, null, engineLabel(pipeline), key ? "cell:" + key + ":3" : null));
    row.appendChild(cell(4, "num", bufferedSeconds(detail), key ? "cell:" + key + ":4" : null));
    // `bytes_sent`, not `bytes_out`: the latter counts what the pipeline
    // *produced*, which includes audio for a warm station nobody is listening to
    // and audio dropped for a listener that fell behind. The column and the
    // "audio sent" tile must mean the same thing or one of them is lying.
    row.appendChild(cell(5, "num", bytes(session.bytes_sent), key ? "cell:" + key + ":5" : null));
    row.appendChild(cell(6, "num", count(session.restarts), key ? "cell:" + key + ":6" : null));
    row.appendChild(cell(7, "num", count(pipeline.failures), key ? "cell:" + key + ":7" : null));
    row.appendChild(cell(8, "num", duration(detail.starved_for), key ? "cell:" + key + ":8" : null));
    row.appendChild(cell(9, "num", duration(pipeline.idle), key ? "cell:" + key + ":9" : null));
    row.appendChild(cell(10, "num", duration(session.uptime), key ? "cell:" + key + ":10" : null));
    return row;
  }

  function paintStreams() {
    const tbody = document.getElementById("streams");
    if (!tbody || !snapshot) return;
    const details = (snapshot.streams && snapshot.streams.details) || [];

    // The table is rebuilt from scratch every second, so anything a reader has
    // the focus in - or a description they are reading - is about to be
    // destroyed. Remember both, and put them back (see restoreTip).
    //
    // The pointer's element is destroyed too, so a mouse resting over the
    // table ends up pointing at nothing. The browser fires a fresh
    // `pointerover` when the element under the mouse is replaced, so the
    // description is restored the same way - by key, against the new markup.
    const key = focusedKey();
    const wasOpen = tipSource.focus ? tipSource.focus.dataset.tipKey : null;
    const wasHovered = tipSource.pointer ? tipSource.pointer.dataset.tipKey : null;

    repainting = true;
    tbody.textContent = "";
    if (!details.length) {
      const row = element("tr");
      row.setAttribute("role", "row");
      const placeholder = element("td", "empty", "Nothing is playing right now.");
      placeholder.colSpan = 11;
      placeholder.setAttribute("role", "cell");
      row.appendChild(placeholder);
      tbody.appendChild(row);
    } else {
      details.forEach((detail) => tbody.appendChild(streamRow(detail)));
    }
    repainting = false;

    // The focus first: it is what `focusout` reports when the old cell is
    // removed, and the description is put back against the element that holds
    // it.
    if (key) restoreFocus(key);
    if (wasOpen) restoreTip(wasOpen);
    if (wasHovered) restoreHover(wasHovered);
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
  const tip = { node: null };

  /* What is currently showing a description, and why.
   *
   * Two reasons, kept apart: a pointer is over something, or something has the
   * focus. They are not the same thing and they do not end at the same time - a
   * finger that lifts leaves the focus behind, and a mouse that moves away
   * leaves the focus where it was. A single "who owns the tooltip" variable
   * cannot tell those apart, and guessing wrong is what made the description
   * disappear the moment a tap ended.
   */
  const tipSource = { pointer: null, focus: null };

  /* Whether the stations table is being rebuilt at this moment.
   *
   * Rebuilding it replaces the element a reader has the focus in, and the
   * browser reports that as a `focusout` - which would close a description that
   * had nothing wrong with it, once a second, for as long as the table was on
   * screen. The rebuild puts the description back afterwards (see
   * `restoreTip`), so the close is suppressed while it runs.
   */
  let repainting = false;

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

  /* The element a description should point at, if any.
   *
   * The pointer wins over the focus: it is the one the reader is pointing at
   * right now. But a pointer only counts when it is a mouse - a finger is "over"
   * an element only while it is on the screen, and it is gone the moment it
   * lifts, so a touch pointer is not a reason to show anything by itself.
   *
   * An element that has been removed from the page cannot be pointed at. The
   * stations table is rebuilt every second, so a description whose trigger was
   * a cell is briefly attached to nothing - which is why the rebuild puts the
   * description back explicitly (see restoreTip).
   */
  function tipOwner() {
    if (tipSource.pointer && tipSource.pointer.isConnected) return tipSource.pointer;
    if (tipSource.focus && tipSource.focus.isConnected) return tipSource.focus;
    return null;
  }

  /* Draw the description for whichever element owns it, or hide the bubble.
   *
   * The bubble is the one place that decides what is on screen, so that the
   * events do not have to agree with each other: a pointer leaving, a focus
   * arriving and the table being rebuilt all end up here.
   */
  function renderTip() {
    const owner = tipOwner();
    if (!owner || !owner.dataset.tooltip) {
      if (tip.node) tip.node.dataset.open = "false";
      return;
    }
    const node = tipNode();
    node.textContent = owner.dataset.tooltip;
    node.dataset.open = "true";
    placeTip(owner, node);
  }

  /* Move a description to where its trigger is now.
   *
   * A description left where its trigger *was* is worse than none - but one
   * that follows its trigger is neither. The page scrolls, the stations table
   * scrolls sideways, and neither is a reason to throw the text away. It is
   * closed only when the trigger has gone off the screen, or out of the page.
   */
  function refreshTip() {
    const owner = tipOwner();
    if (!owner) {
      // The trigger is gone for good - the station was reaped, or the table was
      // rebuilt without it. Drop the sources that point at nothing, and keep
      // the one that is still there.
      if (tipSource.pointer && !tipSource.pointer.isConnected) tipSource.pointer = null;
      if (tipSource.focus && !tipSource.focus.isConnected) tipSource.focus = null;
      renderTip();
      return;
    }
    const box = owner.getBoundingClientRect();
    const margin = 8;
    if (box.bottom < margin || box.top > window.innerHeight - margin) {
      // The trigger has scrolled off the screen. A description floating over
      // whatever is there now would be pointing at the wrong thing.
      if (tipSource.pointer === owner) tipSource.pointer = null;
      if (tipSource.focus === owner) tipSource.focus = null;
    }
    renderTip();
  }

  /* What an event does to a description, as a pure function.
   *
   * The rules *are* the bug, so they are written as rules and tested as rules
   * (see tests/test_web_ui.py). The handlers below do nothing but find the
   * element and ask.
   *
   *   type        the event type
   *   pointerType "mouse", "touch", or "" for a focus event
   *   hasTip      whether the element under the pointer carries a description
   *   leaves      whether the pointer left the element that had the description
   *   nextHasTip  whether focus is moving to another element that has one
   *   rebuilding  whether the stations table is being rebuilt right now
   *
   * The answer is one of "show", "hide", "hide-all" or "none". "hide" closes
   * the source the event came from - a mouse leaving does not take the focus
   * away from the element a keyboard reader is on, and vice versa - while
   * "hide-all" closes both, which is what a deliberate tap on something
   * without a description means: the reader asked for something else.
   */
  function tipAction({
    type,
    pointerType = "",
    hasTip = false,
    leaves = false,
    nextHasTip = false,
    rebuilding = false,
  }) {
    // A touch pointer is "over" an element only while a finger is on it, and
    // it reports "out" the instant that finger lifts - with no relatedTarget,
    // because the pointer is destroyed along with the touch. Read as "the
    // pointer left", that closed the description the moment the tap ended: a
    // tap showed the text for a single frame, and the only way to read it was
    // to hold a finger down. So on a touch screen only `pointerdown` means
    // anything, and `pointerout` means nothing at all.
    if (pointerType === "touch") {
      if (type === "pointerdown") return hasTip ? "show" : "hide-all";
      return "none";
    }

    // A mouse: over is a hover, out is a departure.
    if (type === "pointerover") return hasTip ? "show" : "hide";
    if (type === "pointerout") return leaves ? "hide" : "none";

    // Focus. `focusin` is how a keyboard - and a phone, which has no hover at
    // all - reaches a description.
    if (type === "focusin") return hasTip ? "show" : "none";
    if (type === "focusout") {
      // The table is being rebuilt: the description is about to be put back
      // (see restoreTip), and closing it now would make it flicker.
      if (rebuilding) return "none";
      // Focus is moving to another described element, which `focusin` is about
      // to show.
      if (nextHasTip) return "none";
      return "hide";
    }

    return "none";
  }

  /* Apply an answer to the source it came from.
   *
   * `source` is "pointer" for a pointer event and "focus" for a focus event.
   * The two are kept apart on purpose: a mouse moving away does not take the
   * focus away from the element a keyboard reader is on, and a keyboard reader
   * tabbing away does not take the mouse away from the figure it is over.
   */
  function applyTip(action, owner, source) {
    if (action === "show" && owner && owner.dataset.tooltip) {
      tipSource[source] = owner;
    } else if (action === "hide") {
      tipSource[source] = null;
    } else if (action === "hide-all") {
      tipSource.pointer = null;
      tipSource.focus = null;
    }
    renderTip();
  }

  // Delegated, so rows built later by `paintStreams` are covered without
  // rebinding anything on every poll.
  document.addEventListener("pointerover", (event) => {
    const owner = event.target.closest("[data-tooltip]");
    applyTip(
      tipAction({ type: event.type, pointerType: event.pointerType, hasTip: !!owner }),
      owner,
      "pointer"
    );
  });

  document.addEventListener("pointerout", (event) => {
    const owner = event.target.closest("[data-tooltip]");
    // `relatedTarget` is where the pointer went. Moving onto a child of the same
    // tile must not dismiss the tooltip.
    const leaves = !!owner && !owner.contains(event.relatedTarget);
    applyTip(
      tipAction({
        type: event.type,
        pointerType: event.pointerType,
        hasTip: !!owner,
        leaves: leaves,
      }),
      owner,
      "pointer"
    );
  });

  // A tap. On a touch screen this is the only way to reach any of this, so it
  // is the event that decides what a finger on the screen means.
  document.addEventListener("pointerdown", (event) => {
    const owner = event.target.closest("[data-tooltip]");
    applyTip(
      tipAction({ type: event.type, pointerType: event.pointerType, hasTip: !!owner }),
      owner,
      "pointer"
    );
  });

  // Keyboard and touch: `focus-visible`/tap focus lands here, which is the only
  // way a phone gets a tooltip at all - there is no hover on a touch screen.
  document.addEventListener("focusin", (event) => {
    const owner = event.target.closest("[data-tooltip]");
    applyTip(tipAction({ type: event.type, hasTip: !!owner }), owner, "focus");
  });

  document.addEventListener("focusout", (event) => {
    const next = event.relatedTarget;
    applyTip(
      tipAction({
        type: event.type,
        nextHasTip: !!(next && next.closest && next.closest("[data-tooltip]")),
        rebuilding: repainting,
      }),
      null,
      "focus"
    );
  });

  // A description that stays where its trigger was is worse than none; one
  // that follows its trigger is neither. The table scrolls sideways and the
  // page scrolls, and neither is a reason to throw the text away. Capturing,
  // so the table's own scroller is caught as well as the page.
  document.addEventListener("scroll", refreshTip, { passive: true, capture: true });
  window.addEventListener("resize", refreshTip);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      tipSource.pointer = null;
      tipSource.focus = null;
      renderTip();
    }
  });

  /* The key of the element that has the focus, if it is one of ours.
   *
   * Only the stations table is rebuilt, so only a focus inside it has to be
   * put back.
   */
  function focusedKey() {
    const active = document.activeElement;
    if (!active || !active.closest) return null;
    if (!active.closest("#streams")) return null;
    return active.dataset.tipKey || null;
  }

  /* Put a description back after the stations table has been rebuilt.
   *
   * The key names a station and a column, so the description can be found
   * again in the new markup. If the station has gone there is nothing to point
   * at, and the bubble closes.
   */
  function restoreTip(key) {
    if (!key) return;
    const owner = document.querySelector('[data-tip-key="' + key + '"]');
    if (owner) {
      tipSource.focus = owner;
      renderTip();
    } else {
      tipSource.focus = null;
      renderTip();
    }
  }

  /* Put a *hovered* description back after the stations table was rebuilt.
   *
   * The mouse is still resting on the same place, but the cell it was over
   * has been replaced - so the description would be pointing at a node that
   * is no longer in the page. The browser fires a fresh `pointerover` when
   * the element under the mouse is replaced, but that is not guaranteed for
   * every browser, and the description should not depend on it.
   */
  function restoreHover(key) {
    if (!key) return;
    const owner = document.querySelector('[data-tip-key="' + key + '"]');
    if (owner) {
      tipSource.pointer = owner;
      renderTip();
    } else {
      tipSource.pointer = null;
      renderTip();
    }
  }

  /* Give the focus back to the cell it was in.
   *
   * Without this a keyboard reader is thrown back to the top of the page every
   * second, which makes the stations table unusable without a mouse. The focus
   * only goes back to something that is on screen, so a reader who has scrolled
   * away from the table is not yanked back to it.
   */
  function restoreFocus(key) {
    if (!key) return;
    const owner = document.querySelector('[data-tip-key="' + key + '"]');
    if (!owner) return;
    const box = owner.getBoundingClientRect();
    if (box.bottom <= 0 || box.top >= window.innerHeight) return;
    owner.focus({ preventScroll: true });
  }

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
