/* Baccarat Vision — content script (runs inside the game iframe).
 *
 * Reads the live game DOM (counter, cards, result), sends each completed hand to
 * the local engine server, and renders an overlay with the predictions.
 *
 * Class names are hashed CSS modules (e.g. `scoreBoardInfo__totalItem-UDdHPq`),
 * but the readable prefix is stable, so we match with [class*="prefix"]. The
 * one thing that needs live confirmation is how a card encodes its rank/suit —
 * parseCard() tries the usual encodings and logs the raw element (see DEBUG).
 */
(() => {
  "use strict";
  const DEBUG = false;
  const log = (...a) => DEBUG && console.log("%c[BV]", "color:#9ad", ...a);

  // ----- selectors (class-prefix, robust to the hash suffix) ----------------
  const SEL = {
    counterItem: '[class*="scoreBoardInfo__totalItem"]',
    cardsContainer: '[class*="baccaratCardsStack__cards-"]',
    card: '[class*="baccaratCardsStack__card-"]',
    score: '[class*="baccaratCardsStack__score"]',
  };
  const SUIT_SYM = { "♠": "s", "♥": "h", "♦": "d", "♣": "c" };

  // ----- slot tracking (dual-table support) -----------------------------------
  // Each content script instance gets a slot assigned by the /probe endpoint on
  // first contact. All subsequent requests include ?slot=X so two tables never
  // share state. Slot defaults to "A" until /probe responds.
  let assignedSlot = "A";

  // ----- engine API (via background relay; dodges https->localhost block) ----
  function api(path, method = "GET", body = null) {
    // /probe selects the slot — don't inject a slot into that call itself.
    const slottedPath = path === "/probe" ? path : path + `?slot=${assignedSlot}`;
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type: "api", path: slottedPath, method, body }, (resp) => {
        if (chrome.runtime.lastError || !resp) return resolve(null);
        resolve(resp.ok ? resp.data : null);
      });
    });
  }

  // ----- helpers ------------------------------------------------------------
  const RANK_VALUE = { A: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9,
    10: 0, T: 0, J: 0, Q: 0, K: 0 };
  function rankToValue(r) {
    return RANK_VALUE[String(r).toUpperCase()];
  }
  function intsIn(s) {
    return (String(s).match(/\d+/g) || []).map(Number);
  }
  function parseAmount(s) {
    const m = String(s).replace(/,/g, "").trim().match(/([\d.]+)\s*([KMB])?/i);
    if (!m) return 0;
    let v = parseFloat(m[1]);
    const suf = (m[2] || "").toUpperCase();
    if (suf === "K") v *= 1e3; else if (suf === "M") v *= 1e6; else if (suf === "B") v *= 1e9;
    return Math.round(v);
  }

  function _currencyNear(el) {
    let n = el;
    for (let i = 0; i < 3 && n; i++) {
      const m = (n.textContent || "").match(/\b(GC|SC)\b/i);
      if (m) return m[1].toUpperCase();
      n = n.parentElement;
    }
    return "";
  }
  // Read balance + currency (GC/SC). Prefer the stable data-locator, then the
  // class element, then any "GC/SC <number>" labelled Coins/Balance.
  function readBalance() {
    const el = document.querySelector('[data-locator="balance-amount"]')
      || document.querySelector('[class*="balance__value"]');
    if (el) {
      const t = el.textContent || "";
      const num = t.match(/[\d.,]{1,}/);
      if (num && parseAmount(num[0]) > 0) {
        const titleEl = document.querySelector('[data-locator="balance-title"]');
        const cm = (t.match(/\b(GC|SC)\b/i) || [])[1]
          || (titleEl && (titleEl.textContent.match(/\b(GC|SC)\b/i) || [])[1])
          || _currencyNear(el);
        return { currency: (cm || "").toUpperCase(), balance: parseAmount(num[0]) };
      }
    }
    const text = document.body ? document.body.innerText || "" : "";
    let best = null;
    for (const m of text.matchAll(/\b(GC|SC)\s*([\d,]+)/gi)) {
      const amt = parseAmount(m[2]);
      const ctx = text.slice(Math.max(0, m.index - 14), m.index).toLowerCase();
      const labelled = /coin|balance|wallet/.test(ctx);
      if (!best || labelled || amt > best.balance) {
        best = { currency: m[1].toUpperCase(), balance: amt };
        if (labelled) break;
      }
    }
    return best;
  }
  // Chip denominations are encoded in the locator itself: chip-100, chip-2500…
  function readChips() {
    const denoms = [];
    for (const el of document.querySelectorAll('[data-locator^="chip-"]')) {
      const m = (el.getAttribute("data-locator") || "").match(/^chip-(\d+)$/);
      if (m) denoms.push(parseInt(m[1], 10));
    }
    if (denoms.length) return [...new Set(denoms)].sort((a, b) => a - b);
    const els = [...document.querySelectorAll('[class*="chip__value"]')];
    return [...new Set(els.map((e) => parseAmount(e.textContent)).filter((v) => v > 0))]
      .sort((a, b) => a - b);
  }
  function relayBalance() {
    const b = readBalance();
    if (b && b.balance) { try { chrome.runtime.sendMessage({ type: "setBalance", data: b }); } catch (e) {} }
  }
  function getRelayedBalance() {
    return new Promise((r) => {
      try { chrome.runtime.sendMessage({ type: "getBalance" }, (resp) => r(resp && resp.ok ? resp.data : null)); }
      catch (e) { r(null); }
    });
  }
  let lastCtx = "";
  async function pushContext() {
    let bal = readBalance();
    if (bal && bal.balance) { try { chrome.runtime.sendMessage({ type: "setBalance", data: bal }); } catch (e) {} }
    if (!bal || !bal.balance) bal = await getRelayedBalance();  // from the other frame
    const denoms = readChips();
    if (!bal && !denoms.length) return;
    const sig = JSON.stringify([bal, denoms]);
    if (sig === lastCtx) return;
    lastCtx = sig;
    await api("/context", "POST", {
      currency: bal ? bal.currency : "", balance: bal ? bal.balance : 0, denoms,
    });
  }

  const seenSamples = new Set();
  function captureSample(el) {
    // Send a few unrecognised card elements to the server (file) so we can
    // decode the format without the user opening DevTools.
    const html = el.outerHTML || "";
    if (!html || seenSamples.has(html) || seenSamples.size >= 10) return;
    seenSamples.add(html);
    api("/debug-card", "POST", { html });
  }

  // Parse a card element -> {rank, suit, value}. The value lives in a child's
  // data-locator, formatted "{suit}-{rank}", e.g. "♣-10", "♥-J", "♠-9".
  function parseCard(el) {
    let loc = el.getAttribute && el.getAttribute("data-locator");
    if (!loc || !/[♠♥♦♣]/.test(loc)) {
      loc = null;
      const nodes = el.querySelectorAll ? el.querySelectorAll("[data-locator]") : [];
      for (const n of nodes) {
        const v = n.getAttribute("data-locator");
        if (v && /[♠♥♦♣]/.test(v)) { loc = v; break; }
      }
    }
    if (loc) {
      const m = loc.match(/([♠♥♦♣]).*?(10|[2-9]|[AJQKT])/i);
      if (m) {
        const suit = SUIT_SYM[m[1]];
        const rank = m[2].toUpperCase();
        const value = rankToValue(rank);
        if (value !== undefined) return { rank, suit, value };
      }
    }
    captureSample(el);
    if (DEBUG) log("parseCard: could not decode — sample sent to server.", el);
    return null;
  }

  // Read the shoe counter -> {hand, P, B, T} (best effort).
  function readCounter() {
    const items = document.querySelectorAll(SEL.counterItem);
    if (!items.length) return null;
    const nums = [];
    items.forEach((it) => {
      const n = intsIn(it.textContent);
      if (n.length) nums.push(n[0]);
    });
    if (nums.length < 3) return null;
    // Layout is hand, P, B, T (or P, B, T with hand = sum). Normalise.
    let hand, p, b, t;
    if (nums.length >= 4) [hand, p, b, t] = nums;
    else { [p, b, t] = nums; hand = p + b + t; }
    return { hand, P: p, B: b, T: t };
  }

  const total2 = (cards) => (cards.length >= 2 ? (cards[0].value + cards[1].value) % 10 : null);
  const handTotal = (cards) => cards.reduce((s, c) => (s + c.value) % 10, 0);

  // Read the current hand's cards + scores (Player = leftmost, Banker = rightmost).
  function readHand() {
    const conts = [...document.querySelectorAll(SEL.cardsContainer)].map((cont) => {
      const cards = [...cont.querySelectorAll(SEL.card)].map(parseCard).filter(Boolean);
      const root = cont.parentElement;
      const scoreEl = root && root.querySelector(SEL.score);
      const score = scoreEl ? (intsIn(scoreEl.textContent)[0] ?? null) : null;
      return { cards, score, left: cont.getBoundingClientRect().left };
    }).filter((s) => s.cards.length >= 2).sort((a, b) => a.left - b.left);

    if (conts.length < 2) return null;
    const player = conts[0];
    const banker = conts[conts.length - 1];
    const pTot = player.score ?? handTotal(player.cards);
    const bTot = banker.score ?? handTotal(banker.cards);
    const winner = pTot > bTot ? "P" : bTot > pTot ? "B" : "T";
    const pc = player.cards, bc = banker.cards;
    const suited = (cs) => cs[0].rank === cs[1].rank && cs[0].suit === cs[1].suit;
    return {
      winner,
      player_total: pTot,
      banker_total: bTot,
      is_natural: [total2(pc), total2(bc)].some((t) => t === 8 || t === 9),
      p_pair: pc[0].rank === pc[1].rank,
      b_pair: bc[0].rank === bc[1].rank,
      p_suited_pair: suited(pc),
      b_suited_pair: suited(bc),
      card_values: [...pc, ...bc].map((c) => c.value),
      detail: { player: pc, banker: bc },
    };
  }

  // ----- new-hand detection + posting (counter-delta reconciliation) --------
  // Mirrors the desktop pipeline: the counter is the source of truth. We add
  // exactly the hands its P/B/T counts say have happened (so missed frames are
  // caught up), with exact card values when they decode, else winner-only
  // (estimated composition). Works fully on the counter alone.
  let counts = null; // [P, B, T] from last good read
  let lastHand = 0;
  let lastSnapshot = null;
  let dumpedScoreboard = false;
  let prevBalance = null; // balance snapshot used to detect bet win/loss

  function dumpScoreboardOnce() {
    if (dumpedScoreboard) return;
    const sb = document.querySelector('[class*="scoreBoardInfo"]')
      || document.querySelector('[class*="baccarat__history"]');
    if (sb) { dumpedScoreboard = true; api("/debug-card", "POST", { html: "SCOREBOARD:\n" + sb.outerHTML }); }
  }

  let sawGame = false;
  async function tick() {
    const c = readCounter();
    const isGame = !!c || !!document.querySelector('[class*="scoreBoardInfo"],[class*="baccaratCardsStack"]');
    if (isGame) sawGame = true;
    if (!sawGame) return;  // overlay + pipeline run only in the game frame
    pushContext();         // keep the server's balance/chips fresh
    if (!c) { setStatus("looking for the counter…"); return; }
    dumpScoreboardOnce();
    const cur = [c.P, c.B, c.T];
    const sum = (a) => a[0] + a[1] + a[2];

    if (counts === null) {
      // First read: probe the server to see if this shoe is already tracked
      // (handles page refreshes and tab-switching between two tables).
      const probe = await api("/probe", "POST", {
        total: sum(cur), player_wins: cur[0], banker_wins: cur[1], ties: cur[2],
      });
      if (probe && probe.slot) {
        assignedSlot = probe.slot;
        if (probe.match) {
          // Server already tracks this shoe — fill only the small refresh gap.
          if (probe.gap > 0) {
            await api("/catch_up", "POST", { hands: probe.gap });
          }
          lastSnapshot = await api("/snapshot");
          setStatus(`resumed slot ${assignedSlot} at hand ${c.hand}`);
        } else {
          // New shoe (or fresh slot) — full reset.
          lastSnapshot = await api("/reset", "POST", { burn_cards: 10, hands: sum(cur) });
          setStatus(`slot ${assignedSlot} — new shoe at hand ${c.hand}`);
        }
      } else {
        // Server offline or probe failed — fall back to a plain reset.
        lastSnapshot = await api("/reset", "POST", { burn_cards: 10, hands: sum(cur) });
        setStatus(`synced at hand ${c.hand}`);
      }
      counts = cur; lastHand = c.hand;
      render(lastSnapshot, null, c);
      return;
    }
    if (sum(cur) < sum(counts) || c.hand < lastHand) {
      // The casino counter reset -> a new shoe. The DOM counter is reliable
      // (unlike OCR), so archive the finished shoe to the library and start fresh.
      prevBalance = null; // discard stale reading so first hand of new shoe is clean
      lastSnapshot = await api("/reset", "POST", { burn_cards: 10, hands: sum(cur) });
      counts = cur; lastHand = c.hand;
      setStatus(`new shoe at hand ${c.hand} — previous shoe archived`);
      render(lastSnapshot, null, c);
      return;
    }

    const dP = Math.max(0, cur[0] - counts[0]);
    const dB = Math.max(0, cur[1] - counts[1]);
    const dT = Math.max(0, cur[2] - counts[2]);
    const total = dP + dB + dT;
    if (total === 0) return;
    // Guard against a single OCR/DOM misread inflating a count: a real gap at
    // 1.5s polling is tiny. An implausible jump -> rebaseline, add no hands.
    if (total > 6 || dP > 4 || dB > 4 || dT > 4) {
      counts = cur; lastHand = c.hand;
      setStatus(`hand ${c.hand} — ignored implausible jump`);
      return;
    }

    // ── Win/lose sound via balance delta ─────────────────────────────────────
    // By the time the counter increments the casino has already paid out the
    // previous hand, so readBalance() here reflects the result.  A non-zero
    // change vs the saved pre-hand balance means the player had an active bet.
    const balNow = readBalance();
    if (prevBalance && balNow && balNow.currency === prevBalance.currency && balNow.balance) {
      const delta = balNow.balance - prevBalance.balance;
      if (delta > 0) playSound("win");
      else if (delta < 0) playSound("lose");
      // delta === 0 → no bet placed (or exact push on Tie), no sound
    }
    prevBalance = balNow || prevBalance;

    const exact = total === 1 ? readHand() : null; // try exact cards for a single hand
    const winners = [].concat(Array(dP).fill("P"), Array(dB).fill("B"), Array(dT).fill("T"));
    for (const w of winners) {
      const body = exact && exact.winner === w
        ? exact
        : { winner: w, player_total: 0, banker_total: 0 };
      lastSnapshot = await api("/hand", "POST", body);
    }
    counts = cur; lastHand = c.hand;
    render(lastSnapshot, exact, c);
  }

  // ─── Sound effects ─────────────────────────────────────────────────────────
  // File lists are loaded once from sounds/index.json (a web_accessible_resource).
  // To add new sounds: drop MP3s into extension/sounds/win/ or sounds/lose/,
  // add their paths to sounds/index.json, then reload the extension.
  // No code changes needed.
  let _soundIndex = { win: [], lose: [] };
  fetch(chrome.runtime.getURL("sounds/index.json"))
    .then((r) => r.json())
    .then((data) => { _soundIndex = data; })
    .catch(() => {});

  let _sfx = null; // currently playing Audio element (stopped before next play)

  // Per-type shuffle queues. Each queue is a pre-shuffled copy of the pool.
  // Sounds are drawn from the front; when empty the pool is reshuffled and
  // the last-played file is moved to the back so it can't repeat immediately.
  const _queues   = { win: [], lose: [] };
  const _lastPlay = { win: null, lose: null };

  function _shuffle(arr) {
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
  }

  function _nextFile(type) {
    const pool = _soundIndex[type] || [];
    if (!pool.length) return null;
    if (!_queues[type].length) {
      // Refill: shuffle a fresh copy, then push the last-played to the back
      // so it is guaranteed not to repeat at the seam between batches.
      _queues[type] = _shuffle([...pool]);
      const last = _lastPlay[type];
      if (last && _queues[type][0] === last && _queues[type].length > 1) {
        _queues[type].push(_queues[type].shift()); // rotate it to the back
      }
    }
    return _queues[type].shift();
  }

  function playSound(type) {
    const file = _nextFile(type);
    if (!file) return;
    _lastPlay[type] = file;
    try {
      if (_sfx) { _sfx.pause(); _sfx.currentTime = 0; }
      _sfx = new Audio(chrome.runtime.getURL(file));
      _sfx.volume = 0.65;
      _sfx.play().catch(() => {});
    } catch (_) {}
  }

  // ─── overlay UI ────────────────────────────────────────────────────────────

  const fmt  = (n) => Math.round(n).toLocaleString();
  const fmtK = (n) => Math.abs(n) >= 1e6 ? (n / 1e6).toFixed(1) + "M"
                    : Math.abs(n) >= 1e3 ? (n / 1e3).toFixed(0) + "K"
                    : Math.round(n).toString();

  const BET_COLOR = {
    banker: "#ff3d71", player: "#00b4ff", tie: "#00ff94",
    super_6: "#c97bff", b_bonus: "#ff9f00", p_bonus: "#ff9f00",
    either_pair: "#ffd700", player_pair: "#ff69b4", banker_pair: "#ff69b4",
    suited_pair: "#ff69b4",
  };
  const BET_ICON = {
    banker: "🔴", player: "🔵", tie: "🟢", super_6: "🟣",
    b_bonus: "🟠", p_bonus: "🟠", either_pair: "🟡",
    player_pair: "🩷", banker_pair: "🩷", suited_pair: "💎",
  };
  const BET_NAMES = {
    player: "Player", banker: "Banker", super_6: "Super 6", tie: "Tie",
    player_pair: "P Pair", banker_pair: "B Pair", either_pair: "Either Pair",
    suited_pair: "Suited", p_bonus: "P Bonus", b_bonus: "B Bonus",
  };

  function pnl(x, dec = 1) {
    const c = x >= 0 ? "#00ff94" : "#ff3d71";
    return `<span style="color:${c};font-weight:600">${x >= 0 ? "+" : ""}${x.toFixed(dec)}</span>`;
  }
  function pnlFmt(x) {
    const c = x >= 0 ? "#00ff94" : "#ff3d71";
    return `<span style="color:${c};font-weight:600">${x >= 0 ? "+" : "-"}${fmtK(Math.abs(x))}</span>`;
  }
  function bar(pct, color, h = 3) {
    const w = Math.max(0, Math.min(100, pct * 100)).toFixed(1);
    return `<div style="height:${h}px;background:#1a1a35;border-radius:2px;overflow:hidden;margin:2px 0">` +
           `<div style="width:${w}%;height:100%;background:${color};border-radius:2px;` +
           `box-shadow:0 0 6px ${color}80;transition:width .3s"></div></div>`;
  }

  const SUIT_DISP = { s: "♠", h: "♥", d: "♦", c: "♣" };
  function cardHtml(c) {
    const red = c.suit === "h" || c.suit === "d";
    const col = red ? "#ff6b6b" : "#c0c8e0";
    return `<span style="display:inline-block;background:#12122e;border:1px solid #2a2a55;border-radius:3px;` +
           `padding:1px 5px;margin:0 1px;font-size:11px;font-family:monospace;color:${col}">` +
           `${c.rank}${SUIT_DISP[c.suit] || ""}</span>`;
  }

  // ─── Section factory ───────────────────────────────────────────────────────
  // Sections survive panel recreation — store open/closed state by id.
  const _sectionState = {};
  function makeSection(id, icon, title, defaultOpen = true) {
    const open = _sectionState[id] !== undefined ? _sectionState[id] : defaultOpen;
    const sec = document.createElement("div");
    // border-top and background come from .bvsh CSS class — no inline styles needed
    sec.innerHTML =
      `<div class="bvsh" data-id="${id}">` +
      `<span style="font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#5a6080">` +
      `${icon} ${title}</span>` +
      `<span class="bvchev" style="color:#3a3a60;font-size:9px">${open ? "▼" : "▶"}</span></div>` +
      `<div class="bvsb" id="bvsb-${id}" style="display:${open ? "block" : "none"}"></div>`;
    sec.querySelector(".bvsh").addEventListener("click", () => {
      const body = sec.querySelector(".bvsb");
      const chev = sec.querySelector(".bvchev");
      const nowOpen = body.style.display !== "none";
      body.style.display = nowOpen ? "none" : "block";
      chev.textContent = nowOpen ? "▶" : "▼";
      _sectionState[id] = !nowOpen;
    });
    return sec;
  }

  // ─── Panel + dragging ──────────────────────────────────────────────────────
  let panel = null, statusEl = null, _minimised = false;

  function injectCSS() {
    if (document.getElementById("bv-css")) return;
    const s = document.createElement("style");
    s.id = "bv-css";
    s.textContent = `
      #bv-panel{position:fixed;top:12px;right:12px;width:284px;max-height:calc(100vh - 24px);
        overflow-y:auto;overflow-x:hidden;background:#07071a;
        border:1px solid #00e5ff22;border-radius:14px;
        font-family:'Segoe UI',system-ui,sans-serif;font-size:12px;color:#b0b8d8;
        z-index:2147483647;box-shadow:0 0 32px #00e5ff10,0 12px 40px #00000099;
        box-sizing:border-box;pointer-events:auto !important;}
      #bv-panel *{box-sizing:border-box;max-width:100%}
      #bv-panel::-webkit-scrollbar{width:3px}
      #bv-panel::-webkit-scrollbar-track{background:#07071a}
      #bv-panel::-webkit-scrollbar-thumb{background:#1e1e40;border-radius:2px}
      #bv-hdr{display:flex;align-items:center;justify-content:space-between;
        padding:10px 12px;background:#05051a;border-radius:14px 14px 0 0;
        border-bottom:1px solid #0f0f28;cursor:grab;position:sticky;top:0;z-index:1}
      #bv-hdr:active{cursor:grabbing}

      /* Orb — uses filter:drop-shadow so the circular glow is never square-clipped
         by overflow:hidden on ancestors. box-shadow would be clipped; this is not. */
      .bv-orb{width:62px;height:62px;border-radius:50%;
        display:flex;align-items:center;justify-content:center;
        font-size:24px;font-weight:900;flex-shrink:0;
        transition:filter .5s,background .5s,border-color .5s}

      /* Section headers (via CSS class — no inline styles needed) */
      .bvsh{display:flex;align-items:center;justify-content:space-between;
        padding:8px 12px;cursor:pointer;background:#09091f;user-select:none;
        border-top:1px solid #0f0f28}
      .bvsh:hover{background:#0c0c24}
      .bvsb{padding:10px 12px}

      .bv-row{display:flex;justify-content:space-between;align-items:center;
        padding:3px 0;min-width:0}
      .bv-lbl{color:#4a5070;font-size:10px;white-space:nowrap;
        overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
      .bv-val{font-size:11px;font-weight:600;white-space:nowrap;
        margin-left:6px;flex-shrink:0}
      .bv-chip{display:inline-flex;align-items:center;gap:3px;
        background:#10102a;border:1px solid #20205a;border-radius:5px;
        padding:3px 7px;font-size:10px;white-space:nowrap;margin:2px 2px 2px 0}
      .bv-leg-row{display:flex;align-items:center;justify-content:space-between;
        padding:4px 0;border-bottom:1px solid #0d0d22;min-width:0}
      .bv-leg-row:last-child{border-bottom:none}
      .bv-leg-name{font-size:11px;font-weight:600;white-space:nowrap;overflow:hidden;
        text-overflow:ellipsis;flex:1;min-width:0}
      .bv-leg-meta{font-size:10px;color:#5a6080;white-space:nowrap;
        margin-left:5px;flex-shrink:0}
      .bv-bet-tbl{width:100%;border-collapse:collapse;font-size:10px}
      .bv-bet-tbl td{padding:3px 4px;overflow:hidden;text-overflow:ellipsis;
        white-space:nowrap;max-width:80px}
      .bv-bet-tbl tr:nth-child(even) td{background:#0a0a20}
      .bv-reason{font-size:10px;color:#5a6080;padding:2px 0;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .bv-badge{display:inline-flex;align-items:center;font-size:9px;font-weight:800;
        padding:3px 9px;border-radius:5px;letter-spacing:.9px}
      .bv-badge-bet{background:#00e5ff14;color:#00e5ff;border:1px solid #00e5ff44;
        text-shadow:0 0 8px #00e5ff80}
      .bv-badge-opt{background:#ffffff07;color:#4a5070;border:1px solid #ffffff10}
      .bv-phase{display:inline-block;font-size:9px;font-weight:700;padding:2px 8px;
        border-radius:10px;letter-spacing:.5px}
      .bv-divider{border:none;border-top:1px solid #0f0f28;margin:8px 0}
    `;
    document.head.appendChild(s);
  }

  function ensurePanel() {
    if (panel) return;
    injectCSS();
    panel = document.createElement("div");
    panel.id = "bv-panel";

    // ── Header (drag handle + minimize) ──
    const hdr = document.createElement("div");
    hdr.id = "bv-hdr";
    hdr.innerHTML =
      `<div style="display:flex;align-items:center;gap:6px;min-width:0">` +
      `<span style="color:#00e5ff;font-weight:800;font-size:11px;letter-spacing:1.5px;` +
      `text-shadow:0 0 10px #00e5ff;white-space:nowrap">🎯 BV</span>` +
      `<span id="bv-status" style="color:#3a3a60;font-size:10px;overflow:hidden;` +
      `text-overflow:ellipsis;white-space:nowrap">starting…</span></div>` +
      `<button id="bv-min" title="Minimise" style="background:none;border:none;` +
      `color:#3a3a60;cursor:pointer;font-size:13px;padding:0 0 0 6px;flex-shrink:0">_</button>`;
    panel.appendChild(hdr);
    statusEl = panel.querySelector("#bv-status");

    // ── Minimise toggle ──
    const body = document.createElement("div");
    body.id = "bv-body";
    panel.appendChild(body);
    panel.querySelector("#bv-min").addEventListener("click", (e) => {
      e.stopPropagation();
      _minimised = !_minimised;
      body.style.display = _minimised ? "none" : "block";
      panel.querySelector("#bv-min").textContent = _minimised ? "□" : "_";
    });

    // ── Dragging ──
    let ox = 0, oy = 0, dragging = false;
    hdr.addEventListener("mousedown", (e) => {
      if (e.target.id === "bv-min") return;
      dragging = true;
      const r = panel.getBoundingClientRect();
      ox = e.clientX - r.left; oy = e.clientY - r.top;
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const x = Math.max(0, Math.min(window.innerWidth - panel.offsetWidth, e.clientX - ox));
      const y = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - oy));
      panel.style.left = x + "px"; panel.style.top = y + "px";
      panel.style.right = "auto";
    });
    document.addEventListener("mouseup", () => { dragging = false; });

    // ── Sections (in body div) ──
    const secPick    = makeSection("pick",    "🎯", "Next Pick",    true);
    const secSpread  = makeSection("spread",  "💰", "Bet Spread",   true);
    const secPattern = makeSection("pattern", "📊", "Pattern",      true);
    const secModel   = makeSection("model",   "🧠", "AI Engine",    true);
    const secBalance = makeSection("balance", "🏦", "Balance",      true);
    const secHand    = makeSection("hand",    "🃏", "Last Hand",    false);
    const secBets    = makeSection("bets",    "📋", "All Bets",     false);
    [secPick, secSpread, secPattern, secModel, secBalance, secHand, secBets]
      .forEach((s) => body.appendChild(s));

    document.body.appendChild(panel);
  }

  function setStatus(s) { ensurePanel(); if (statusEl) statusEl.textContent = s; }

  function $(id) { return panel ? panel.querySelector(`#bvsb-${id}`) : null; }

  // ─── Render ────────────────────────────────────────────────────────────────
  function render(data, hand, counter) {
    ensurePanel();
    setStatus(`H${counter.hand} · ${counter.P}P ${counter.B}B ${counter.T}T`);
    if (_minimised) return;

    if (!data) {
      const el = $("pick");
      if (el) el.innerHTML = `<div style="color:#ff9f00;font-size:11px">⚠ Engine offline — run the server</div>`;
      return;
    }

    const m  = data.mystic;
    const st = data.staking;
    const L  = data.learning;
    const ds = data.dynamic_spread;
    const bk = data.bankroll;
    const pat = data.pattern;
    const cur = (st && st.currency) || (bk && bk.currency) || "";

    // ── 🎯 Next Pick ─────────────────────────────────────────────────────── //
    const pickEl = $("pick");
    if (pickEl && m && m.pick) {
      const col = m.confident ? (BET_COLOR[m.pick] || "#b0b8d8") : "#4a5070";
      // Single letter inside the orb — large and unmistakable
      const letter = m.pick === "banker" ? "B" : m.pick === "player" ? "P"
                   : m.pick === "tie"    ? "T" : m.pick[0].toUpperCase();
      const badge = m.confident
        ? `<span class="bv-badge bv-badge-bet">✦ BET</span>`
        : `<span class="bv-badge bv-badge-opt">WAIT</span>`;
      const stakeStr = st ? `${fmtK(st.stake)}${cur ? " " + cur : ""}` : "—";
      const vibe = m.vibe || 0;

      let unlockHtml = "";
      if (L) {
        if (m.confident) {
          const proven = L.significant ? "✓ proven edge" : "✓ net +edge";
          unlockHtml = `<div class="bv-reason" style="color:#00ff94;margin-top:5px">${proven} · ${L.acts} hands graded</div>`;
        } else {
          const have = L.acts || 0, need = L.min_hands || 15;
          const pph = (L.profit_per_hand || 0) * 100;
          const why = have < need ? `${have}/${need} hands`
            : pph <= 0 ? `P/H ${pph.toFixed(1)}` : "building confidence";
          unlockHtml = `<div class="bv-reason" style="margin-top:5px">🔒 BET unlocks: ${why}</div>`;
        }
      }

      const reasons = (m.reasons || []).slice(0, 2)
        .map((r) => `<div class="bv-reason">• ${r}</div>`).join("");

      // Orb uses filter:drop-shadow — this creates a circular glow that is NOT
      // clipped by overflow:hidden on any ancestor (unlike box-shadow or text-shadow).
      pickEl.innerHTML =
        `<div style="display:flex;align-items:center;gap:14px;padding:4px 2px 10px">` +
        `<div class="bv-orb" style="background:${col}16;border:2px solid ${col}50;` +
        `filter:drop-shadow(0 0 14px ${col});color:${col}">${letter}</div>` +
        `<div style="flex:1;min-width:0">` +
        `<div style="font-size:19px;font-weight:800;color:${col};letter-spacing:.5px;` +
        `line-height:1.1;margin-bottom:6px;white-space:nowrap;overflow:hidden;` +
        `text-overflow:ellipsis">${m.pick_label.toUpperCase()}</div>` +
        `<div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap">` +
        badge +
        `<span style="font-size:12px;font-weight:700;color:#00e5ff">${stakeStr}</span>` +
        `</div></div></div>` +
        bar(vibe, col, 5) +
        `<div class="bv-row" style="margin-top:5px">` +
        `<span class="bv-lbl">Confidence</span>` +
        `<span class="bv-val" style="color:${col}">${(vibe * 100).toFixed(0)}%</span></div>` +
        unlockHtml + reasons;
    }

    // ── 💰 Bet Spread ────────────────────────────────────────────────────── //
    const spreadEl = $("spread");
    if (spreadEl) {
      let html = "";

      // Dynamic spread (probability-driven, has EV)
      if (ds && ds.legs && ds.legs.length) {
        const phaseColor = { early: "#4a5070", mid: "#ffd700", late: "#00ff94" }[ds.phase] || "#4a5070";
        const phaseDot  = { early: "○", mid: "◑", late: "●" }[ds.phase] || "○";
        const phaseLabel = { early: "Early shoe", mid: "Mid shoe", late: "Late shoe" }[ds.phase] || ds.phase;
        const multCol  = ds.multiplier > 3 ? "#00ff94" : ds.multiplier > 1.5 ? "#ffd700" : "#4a5070";
        const multNote = ds.multiplier <= 1 ? "hold" : ds.multiplier >= 4 ? "push" : "raise";

        html += `<div class="bv-row" style="margin-bottom:7px">` +
          `<span class="bv-phase" style="background:${phaseColor}20;color:${phaseColor};border:1px solid ${phaseColor}40">` +
          `${phaseDot} ${phaseLabel}</span>` +
          `<span style="font-size:11px;color:${multCol};font-weight:700">${ds.multiplier}× — ${multNote}</span>` +
          `</div>`;

        // Signal bars — human-readable labels
        const sigs = [
          ["Position", ds.composition_signal, "#00b4ff"],
          ["AI Model", ds.learner_signal,     "#00ff94"],
          ["Streak",   ds.pattern_signal,     "#ffd700"],
          ["Combined", ds.signal,             "#c97bff"],
        ];
        html += sigs.map(([lbl, v, c]) =>
          `<div class="bv-row"><span class="bv-lbl" style="min-width:52px">${lbl}</span>` +
          `<div style="flex:1;margin:0 6px">${bar(v, c, 3)}</div>` +
          `<span class="bv-val" style="color:${c};min-width:28px;text-align:right">${(v * 100).toFixed(0)}%</span></div>`
        ).join("");

        html += `<div style="margin-top:6px;border-top:1px solid #12122e;padding-top:6px">`;
        ds.legs.forEach((leg) => {
          const lc = BET_COLOR[leg.bet] || "#b0b8d8";
          const evCol = leg.ev >= 0 ? "#00ff94" : "#ff6b6b";
          const evTxt = `EV ${leg.ev >= 0 ? "+" : ""}${fmtK(leg.ev)}`;
          html +=
            `<div class="bv-leg-row">` +
            `<span class="bv-leg-name" style="color:${lc}">${BET_ICON[leg.bet] || ""} ${leg.label}</span>` +
            `<span class="bv-leg-meta">${fmtK(leg.stake)}${cur ? " " + cur : ""}</span>` +
            `<span class="bv-leg-meta" style="color:${evCol};margin-left:4px">${evTxt}</span>` +
            `</div>`;
        });
        const totalEvCol = ds.total_ev >= 0 ? "#00ff94" : "#ff6b6b";
        html += `</div>` +
          `<div class="bv-row" style="margin-top:4px;border-top:1px solid #1a1a35;padding-top:4px">` +
          `<span class="bv-lbl" style="color:#b0b8d8;font-weight:700">Total at risk</span>` +
          `<span class="bv-val" style="color:#00e5ff">${fmtK(ds.total_stake)} ${cur}</span></div>` +
          `<div class="bv-row">` +
          `<span class="bv-lbl">Total EV</span>` +
          `<span class="bv-val" style="color:${totalEvCol}">${ds.total_ev >= 0 ? "+" : ""}${fmtK(ds.total_ev)} ${cur}</span></div>`;

        if (!ds.affordable) {
          html += `<div style="color:#ff9f00;font-size:10px;margin-top:3px">⚠ scaled to fit balance</div>`;
        }
      }

      // Pattern side-bet signals (additive to spread)
      if (st && st.side_bets && st.side_bets.length) {
        html += `<div style="margin-top:6px;border-top:1px solid #12122e;padding-top:5px;` +
          `font-size:10px;color:#4a5070;text-transform:uppercase;letter-spacing:.5px">Pattern signals</div>`;
        st.side_bets.forEach((s) => {
          const lc = BET_COLOR[s.bet] || "#b0b8d8";
          html += `<div class="bv-row">` +
            `<span class="bv-leg-name" style="color:${lc}">+ ${s.label}</span>` +
            `<span class="bv-leg-meta">${fmtK(s.stake)}${cur ? " " + cur : ""}</span></div>`;
        });
      }

      // Preset layout (always shown as reference)
      if (st && st.spread_legs && st.spread_legs.length) {
        const warn = st.spread_affordable ? "" : ` <span style="color:#ff9f00">⚠ over balance</span>`;
        html += `<div style="margin-top:6px;border-top:1px solid #12122e;padding-top:5px">` +
          `<div style="font-size:10px;color:#4a5070;text-transform:uppercase;` +
          `letter-spacing:.5px;margin-bottom:4px">Preset · ${fmtK(st.spread_total)} ${cur}${warn}</div>` +
          `<div style="display:flex;flex-wrap:wrap">` +
          st.spread_legs.map((g) => {
            const lc = BET_COLOR[g.bet] || "#b0b8d8";
            return `<span class="bv-chip"><span style="color:${lc}">${g.label}</span>` +
              `<span style="color:#3a3a60">${g.units}u</span></span>`;
          }).join("") + `</div></div>`;
      }

      if (!html) html = `<div style="color:#4a5070;font-size:10px">No spread data yet</div>`;
      spreadEl.innerHTML = html;
    }

    // ── 📊 Pattern ───────────────────────────────────────────────────────── //
    const patEl = $("pattern");
    if (patEl && pat) {
      const since = pat.hands_since || {};
      const streakTxt = pat.streak_len > 1
        ? `${pat.streak_len}× ${pat.streak_side}${pat.is_dragon ? " 🐉" : ""}`
        : "none";
      const chopPct = (pat.chop_score * 100).toFixed(0);
      const chopCol = pat.chop_score > 0.6 ? "#00ff94" : pat.chop_score > 0.35 ? "#ffd700" : "#ff6b6b";
      const persColor = { Dragon: "#ff3d71", Choppy: "#00ff94", Mixed: "#ffd700", Forming: "#c97bff" }[pat.personality] || "#4a5070";
      const shoes = (data.library && data.library.shoes) ? data.library.shoes : 0;

      patEl.innerHTML =
        `<div class="bv-row">` +
        `<span class="bv-lbl">Personality</span>` +
        `<span class="bv-val" style="color:${persColor}">${pat.personality}</span></div>` +
        `<div class="bv-row">` +
        `<span class="bv-lbl">Streak</span>` +
        `<span class="bv-val">${streakTxt}</span></div>` +
        `<div class="bv-row">` +
        `<span class="bv-lbl">Chop score</span>` +
        `<span class="bv-val" style="color:${chopCol}">${chopPct}%</span></div>` +
        bar(pat.chop_score, chopCol, 3) +
        `<div class="bv-row" style="margin-top:4px">` +
        `<span class="bv-lbl">Last Tie / P / B</span>` +
        `<span class="bv-val">${since.T ?? "?"}h · ${since.P ?? "?"}h · ${since.B ?? "?"}h ago</span></div>` +
        (shoes ? `<div class="bv-row"><span class="bv-lbl">Shoe library</span>` +
        `<span class="bv-val" style="color:#4a5070">${shoes} logged</span></div>` : "");
    }

    // ── 🧠 AI Engine ─────────────────────────────────────────────────────── //
    const modelEl = $("model");
    if (modelEl) {
      if (L && L.graded > 0) {
        const edge = (L.accuracy - L.baseline_accuracy) * 100;
        const edgeCol = edge >= 0 ? "#00ff94" : "#ff3d71";
        const accPct = L.accuracy || 0;
        const pl = L.profit || 0;
        const plCol = pl >= 0 ? "#00ff94" : "#ff3d71";
        const vcol = L.significant ? "#00ff94" : L.actionable ? "#ffd700" : "#4a5070";

        modelEl.innerHTML =
          `<div class="bv-row">` +
          `<span class="bv-lbl">Accuracy</span>` +
          `<span class="bv-val">${(accPct * 100).toFixed(1)}%` +
          ` <span style="color:${edgeCol};font-size:10px">(${edge >= 0 ? "+" : ""}${edge.toFixed(1)} vs base)</span></span></div>` +
          bar(accPct, edgeCol >= 0 ? "#00ff94" : "#ff3d71", 3) +
          `<div class="bv-row">` +
          `<span class="bv-lbl">P/H · recent</span>` +
          `<span class="bv-val">${pnl(L.profit_per_hand * 100, 1)}/100 · ${(L.recent_accuracy * 100).toFixed(0)}%</span></div>` +
          `<div class="bv-row">` +
          `<span class="bv-lbl">Model P/L</span>` +
          `<span class="bv-val" style="color:${plCol}">${pl >= 0 ? "+" : ""}${pl.toFixed(1)}u (from 100u)</span></div>` +
          `<div class="bv-row">` +
          `<span class="bv-lbl">Top expert</span>` +
          `<span class="bv-val" style="color:#c97bff;overflow:hidden;text-overflow:ellipsis;max-width:130px">${L.best_expert}</span></div>` +
          `<div style="margin-top:3px;font-size:10px;color:${vcol};overflow:hidden;` +
          `text-overflow:ellipsis;white-space:nowrap">${L.verdict}</div>`;
      } else {
        modelEl.innerHTML = `<div style="color:#4a5070;font-size:11px">🔄 Collecting data — grading every hand…</div>`;
      }
    }

    // ── 🏦 Balance ───────────────────────────────────────────────────────── //
    const balEl = $("balance");
    if (balEl) {
      if (bk && bk.currency && bk.balance) {
        const delta = (bk.balance != null && bk.shoe_start != null) ? bk.balance - bk.shoe_start : null;
        const deltaCol = delta == null ? "" : delta >= 0 ? "#00ff94" : "#ff3d71";
        balEl.innerHTML =
          // Large balance headline
          `<div style="font-size:20px;font-weight:800;color:#00e5ff;letter-spacing:.3px;` +
          `margin-bottom:2px">${fmtK(bk.balance)} <span style="font-size:12px;` +
          `font-weight:600;color:#3a5070">${bk.currency}</span></div>` +
          (delta != null
            ? `<div style="font-size:12px;font-weight:700;color:${deltaCol}">` +
              `${delta >= 0 ? "▲" : "▼"} ${fmtK(Math.abs(delta))} this shoe</div>`
            : "") +
          (bk.suggested_pnl != null && bk.suggested_pnl !== 0
            ? `<div class="bv-row" style="margin-top:4px"><span class="bv-lbl">Suggested P/L</span>` +
              `<span class="bv-val">${pnlFmt(bk.suggested_pnl)} ${bk.currency}</span></div>`
            : "");
      } else {
        balEl.innerHTML = `<div style="color:#ff9f00;font-size:11px">⚠ Balance not detected — open the table first</div>`;
      }
    }

    // ── 🃏 Last Hand ─────────────────────────────────────────────────────── //
    const handEl = $("hand");
    if (handEl && hand && hand.detail) {
      const p = hand.detail.player.map(cardHtml).join("");
      const b = hand.detail.banker.map(cardHtml).join("");
      const wname = { P: "Player", B: "Banker", T: "Tie" }[hand.winner] || hand.winner;
      const wcol = BET_COLOR[hand.winner === "P" ? "player" : hand.winner === "B" ? "banker" : "tie"] || "#b0b8d8";
      handEl.innerHTML =
        `<div class="bv-row" style="margin-bottom:4px">` +
        `<span class="bv-lbl">Player ${hand.player_total}</span><span>${p}</span></div>` +
        `<div class="bv-row" style="margin-bottom:6px">` +
        `<span class="bv-lbl">Banker ${hand.banker_total}</span><span>${b}</span></div>` +
        `<div style="font-size:13px;font-weight:700;color:${wcol};text-shadow:0 0 8px ${wcol}">` +
        `▶ ${wname} wins${hand.is_natural ? " · Natural" : ""}` +
        `${hand.p_pair ? " · P Pair" : ""}${hand.b_pair ? " · B Pair" : ""}</div>`;
    } else if (handEl) {
      handEl.innerHTML = `<div style="color:#4a5070;font-size:10px">Waiting for next hand…</div>`;
    }

    // ── 📋 All Bets ──────────────────────────────────────────────────────── //
    const betsEl = $("bets");
    if (betsEl && L && L.bets) {
      const richHands = (data.vision && data.vision.length) ? data.vision[0].n : 0;
      const shoes = (data.library && data.library.shoes) || 0;
      betsEl.innerHTML =
        `<div style="color:#4a5070;font-size:10px;margin-bottom:4px">` +
        `${richHands} card-hands · ${shoes} shoes logged</div>` +
        `<table class="bv-bet-tbl"><thead><tr>` +
        `<td style="color:#4a5070">Bet</td><td style="color:#4a5070">Hit%</td>` +
        `<td style="color:#4a5070">Hands</td><td style="color:#4a5070">/100</td></tr></thead><tbody>` +
        L.bets.map((r) => {
          const star = r.significant ? " ✓" : "";
          const pc = (r.hit * 100).toFixed(0);
          const pcol = r.significant ? "#00ff94" : r.per100 > 0 ? "#ffd700" : "#b0b8d8";
          return `<tr><td style="color:${BET_COLOR[r.bet] || "#b0b8d8"}">${BET_NAMES[r.bet] || r.bet}${star}</td>` +
            `<td>${pc}%</td><td>${r.n}</td>` +
            `<td>${pnl(r.per100)}</td></tr>`;
        }).join("") + `</tbody></table>`;
    }
  }

  // ----- boot ---------------------------------------------------------------
  log("Baccarat Vision content script loaded in", location.href);
  relayBalance();
  setInterval(relayBalance, 1500);
  let pending = null;
  const observer = new MutationObserver(() => {
    if (pending) return;
    pending = setTimeout(() => { pending = null; tick(); }, 250);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  setInterval(tick, 1500);
  window.__bv = { readCounter, readHand, parseCard, tick, SEL };
})();
