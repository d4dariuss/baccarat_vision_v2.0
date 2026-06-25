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
  // Last non-null counter snapshot — captured synchronously in the MutationObserver
  // callback so the 250 ms debounce can't drop it before tick() runs.
  let _savedCounter = null;

  // ─── Session followed-picks P/L ────────────────────────────────────────────
  const session = {
    betSignalledPick: null,  // pick name when prev snapshot had confident=true
    followedPL: 0,           // cumulative P/L on BET-signalled hands
    followedHands: 0,        // hands where BET was signalled
    totalHands: 0,           // total new hands seen this session
  };

  function dumpScoreboardOnce() {
    if (dumpedScoreboard) return;
    const sb = document.querySelector('[class*="scoreBoardInfo"]')
      || document.querySelector('[class*="baccarat__history"]');
    if (sb) { dumpedScoreboard = true; api("/debug-card", "POST", { html: "SCOREBOARD:\n" + sb.outerHTML }); }
  }

  let sawGame = false;
  async function tick() {
    const liveC = readCounter();
    // Keep _savedCounter in sync so the observer value never lags a full interval.
    if (liveC) _savedCounter = liveC;
    // Fall back to the observer-captured snapshot when the Burn Card Procedure
    // screen hides the counter — prevents the last hand being silently dropped.
    const c = liveC ?? _savedCounter;
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
      session.betSignalledPick = null; // reset BET arm on new shoe
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

    // ── Win/lose sound + session P/L via balance delta ───────────────────────
    // By the time the counter increments the casino has already paid out the
    // previous hand, so readBalance() here reflects the result.  A non-zero
    // change vs the saved pre-hand balance means the player had an active bet.
    const balNow = readBalance();
    if (prevBalance && balNow && balNow.currency === prevBalance.currency && balNow.balance) {
      const delta = balNow.balance - prevBalance.balance;
      if (delta > 0) playSound("win");
      else if (delta < 0) playSound("lose");
      // Track session P/L for BET-signalled hands
      session.totalHands++;
      if (session.betSignalledPick !== null) {
        session.followedPL += delta;
        session.followedHands++;
        session.betSignalledPick = null;
      }
    }
    prevBalance = balNow || prevBalance;

    const exact = total === 1 ? readHand() : null; // try exact cards for a single hand
    const winners = [].concat(Array(dP).fill("P"), Array(dB).fill("B"), Array(dT).fill("T"));
    for (const w of winners) {
      const body = exact && exact.winner === w
        ? { winner: exact.winner, player_total: exact.player_total,
            banker_total: exact.banker_total, is_natural: exact.is_natural,
            p_pair: exact.p_pair, b_pair: exact.b_pair,
            p_suited_pair: exact.p_suited_pair, b_suited_pair: exact.b_suited_pair,
            card_values: exact.card_values }
        : { winner: w, player_total: 0, banker_total: 0 };
      lastSnapshot = await api("/hand", "POST", body);
    }
    counts = cur; lastHand = c.hand;
    render(lastSnapshot, exact, c);

    // Arm session tracker: if THIS snapshot signals BET, record it for next tick.
    session.betSignalledPick = (
      lastSnapshot && lastSnapshot.mystic && lastSnapshot.mystic.confident
    ) ? lastSnapshot.mystic.pick : null;
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

  // ─── Icon set (Lucide-style SVG, self-contained — no external CDN needed) ──
  const _ICONS = {
    target:          '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    coins:           '<circle cx="8" cy="8" r="6"/><path d="M14.5 9A6 6 0 1 1 9 14.5"/><line x1="8" y1="5.5" x2="8" y2="10.5"/>',
    "bar-chart":     '<rect x="3" y="12" width="4" height="8"/><rect x="10" y="8" width="4" height="12"/><rect x="17" y="4" width="4" height="16"/>',
    cpu:             '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M20 9h2M2 15h2M20 15h2"/>',
    wallet:          '<path d="M20 12V8H6a2 2 0 0 1 0-4h14v4"/><path d="M4 6v12a2 2 0 0 0 2 2h14v-4"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/>',
    "trending-up":   '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>',
    layers:          '<polygon points="12 2 2 7 12 12 22 7"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
    list:            '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    lock:            '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    check:           '<polyline points="20 6 9 17 4 12"/>',
    "alert-triangle":'<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "refresh-cw":    '<path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/>',
    diamond:         '<path d="M2.7 10.3a2.41 2.41 0 0 0 0 3.41l7.59 7.59a2.41 2.41 0 0 0 3.41 0l7.59-7.59a2.41 2.41 0 0 0 0-3.41l-7.59-7.59a2.41 2.41 0 0 0-3.41 0Z"/>',
  };
  function icon(name, sz = 14, col = "currentColor") {
    const p = _ICONS[name] || "";
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${sz}" height="${sz}" viewBox="0 0 24 24" fill="none" stroke="${col}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:middle;flex-shrink:0">${p}</svg>`;
  }

  const BET_COLOR = {
    banker: "#ff3d71", player: "#00b4ff", tie: "#00ff94",
    super_6: "#c97bff", b_bonus: "#ff9f00", p_bonus: "#ff9f00",
    either_pair: "#ffd700", player_pair: "#ff69b4", banker_pair: "#ff69b4",
    suited_pair: "#ff69b4",
  };
  // Colored dot representing a bet type — uses BET_COLOR so defined after it.
  function betDot(bet) {
    const c = BET_COLOR[bet] || "#b0b8d8";
    return `<svg width="7" height="7" viewBox="0 0 8 8" style="display:inline-block;vertical-align:middle;flex-shrink:0;margin-right:3px"><circle cx="4" cy="4" r="4" fill="${c}"/></svg>`;
  }
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
           `<div class="bv-bar-fill" style="width:${w}%;height:100%;background:${color};border-radius:2px;` +
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

  // ─── Animation helpers ─────────────────────────────────────────────────────

  function pingOrb(wrapEl) {
    if (!wrapEl) return;
    const orbEl = wrapEl.querySelector(".bv-orb");
    for (let i = 0; i < 2; i++) {
      setTimeout(() => {
        const r = document.createElement("div");
        r.className = "bv-ring";
        r.style.color = orbEl ? orbEl.style.color : "#00e5ff";
        wrapEl.appendChild(r);
        setTimeout(() => r.remove(), 960);
      }, i * 230);
    }
  }

  function edgeFlash(type) {
    if (!panel) return;
    panel.classList.remove("bv-flash-win", "bv-flash-lose");
    void panel.offsetWidth;
    panel.classList.add(type === "win" ? "bv-flash-win" : "bv-flash-lose");
    setTimeout(() => panel && panel.classList.remove("bv-flash-win", "bv-flash-lose"), 850);
  }

  function scanNewShoe() {
    if (!panel) return;
    const hdr = panel.querySelector("#bv-hdr");
    if (!hdr) return;
    const sw = document.createElement("div");
    sw.className = "bv-scan-wipe";
    hdr.appendChild(sw);
    setTimeout(() => sw.remove(), 700);
  }

  function floatDelta(text, color, anchorEl) {
    if (!anchorEl) return;
    const r = anchorEl.getBoundingClientRect();
    const el = document.createElement("div");
    el.className = "bv-float-delta";
    el.textContent = text;
    el.style.color = color;
    el.style.left = Math.round(r.left + r.width / 2 - 28) + "px";
    el.style.top  = Math.round(r.top - 4) + "px";
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 1200);
  }

  function sweepSection(id) {
    if (!panel) return;
    const hdr = panel.querySelector(`[data-id="${id}"]`);
    if (!hdr) return;
    const sw = document.createElement("div");
    sw.className = "bv-sweep";
    hdr.style.position = "relative";
    hdr.style.overflow = "hidden";
    hdr.appendChild(sw);
    setTimeout(() => { sw.remove(); hdr.style.overflow = ""; }, 650);
  }

  function arcSvg(pct) {
    const R = 30, C = +(2 * Math.PI * R).toFixed(1);
    const off = +(C * (1 - Math.max(0, Math.min(1, pct)))).toFixed(1);
    return `<svg width="68" height="68" viewBox="0 0 68 68"` +
      ` style="position:absolute;top:-3px;left:-3px;pointer-events:none;transform:rotate(-90deg)">` +
      `<circle cx="34" cy="34" r="${R}" fill="none" stroke="#00e5ff14" stroke-width="2.5"/>` +
      `<circle cx="34" cy="34" r="${R}" fill="none" stroke="#00e5ff" stroke-width="2.5"` +
      ` stroke-dasharray="${C}" stroke-dashoffset="${off}" stroke-linecap="round"` +
      ` style="transition:stroke-dashoffset .4s ease"/></svg>`;
  }

  // ─── Section factory ───────────────────────────────────────────────────────
  // Sections survive panel recreation — store open/closed state by id.
  const _sectionState = {};
  function makeSection(id, iconName, title, defaultOpen = true) {
    const open = _sectionState[id] !== undefined ? _sectionState[id] : defaultOpen;
    const sec = document.createElement("div");
    sec.innerHTML =
      `<div class="bvsh" data-id="${id}">` +
      `<span style="display:inline-flex;align-items:center;gap:5px;font-family:'Cinzel','Palatino Linotype',serif;` +
      `font-size:9.5px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:#5a6080">` +
      `${icon(iconName, 11, "#5a6080")}${title}</span>` +
      `<span class="bvchev" style="color:#3a3a60;font-size:9px">${open ? "▼" : "▶"}</span></div>` +
      `<div class="bvsb" id="bvsb-${id}" style="display:${open ? "block" : "none"}"></div>`;
    sec.querySelector(".bvsh").addEventListener("click", () => {
      const body = sec.querySelector(".bvsb");
      const chev = sec.querySelector(".bvchev");
      const nowOpen = body.style.display !== "none";
      body.style.display = nowOpen ? "none" : "block";
      chev.textContent = nowOpen ? "▶" : "▼";
      chev.classList.remove("bv-chev-open", "bv-chev-close");
      void chev.offsetWidth;
      chev.classList.add(nowOpen ? "bv-chev-close" : "bv-chev-open");
      setTimeout(() => chev.classList.remove("bv-chev-open", "bv-chev-close"), 380);
      _sectionState[id] = !nowOpen;
    });
    return sec;
  }

  // ─── Panel + dragging ──────────────────────────────────────────────────────
  let panel = null, statusEl = null, _minimised = false;

  // ── Animation state ────────────────────────────────────────────────────────
  let _prevConfident   = false;  // BET on/off — drives burst + title widen
  let _prevBalanceAnim = null;   // last rendered balance — drives delta float
  let _prevDragon      = false;  // dragon onset — drives shake
  let _prevHandNum     = 0;      // detects new shoe for scan wipe
  let _sectionInited   = {};     // tracks first-data sweep per section
  let _handHistory     = [];     // last 14 P/B/T outcomes for bead row

  function injectCSS() {
    if (document.getElementById("bv-css")) return;
    const s = document.createElement("style");
    s.id = "bv-css";
    // @import must be the first rule in the stylesheet.
    s.textContent = `@import url('https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;700&display=swap');

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
      #bv-hdr{position:sticky;top:0;z-index:1;padding:11px 14px 9px;
        background:#05051a;border-radius:14px 14px 0 0;
        border-bottom:1px solid #0f0f28;cursor:grab;text-align:center}
      #bv-hdr:active{cursor:grabbing}
      .bv-logo{font-family:'Cinzel','Palatino Linotype','Palatino',serif;
        font-weight:700;font-size:13px;letter-spacing:3px;
        color:#00e5ff;text-shadow:0 0 14px #00e5ff50;line-height:1.2;
        white-space:nowrap}
      #bv-status{font-size:9px;color:#3a3a60;letter-spacing:.3px;margin-top:2px;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      #bv-min{position:absolute;right:10px;top:50%;transform:translateY(-50%);
        background:none;border:none;color:#3a3a60;cursor:pointer;
        font-size:15px;line-height:1;padding:4px;flex-shrink:0}
      #bv-min:hover{color:#5a5a80}

      /* Orb — uses filter:drop-shadow so the circular glow is never square-clipped
         by overflow:hidden on ancestors. box-shadow would be clipped; this is not. */
      .bv-orb{width:62px;height:62px;border-radius:50%;
        display:flex;align-items:center;justify-content:center;
        font-family:'Cinzel','Palatino Linotype',serif;
        font-size:22px;font-weight:700;flex-shrink:0;
        transition:filter .5s,background .5s,border-color .5s}

      /* Section headers */
      .bvsh{display:flex;align-items:center;justify-content:space-between;
        padding:7px 12px;cursor:pointer;background:#09091f;user-select:none;
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
        text-overflow:ellipsis;flex:1;min-width:0;display:flex;align-items:center}
      .bv-leg-meta{font-size:10px;color:#5a6080;white-space:nowrap;
        margin-left:5px;flex-shrink:0}
      .bv-bet-tbl{width:100%;border-collapse:collapse;font-size:10px}
      .bv-bet-tbl td{padding:3px 4px;overflow:hidden;text-overflow:ellipsis;
        white-space:nowrap;max-width:80px}
      .bv-bet-tbl tr:nth-child(even) td{background:#0a0a20}
      .bv-reason{font-size:10px;color:#5a6080;padding:2px 0;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .bv-badge{display:inline-flex;align-items:center;gap:4px;
        font-family:'Cinzel','Palatino Linotype',serif;
        font-size:9px;font-weight:700;
        padding:3px 9px;border-radius:5px;letter-spacing:.9px}
      .bv-badge-bet{background:#00e5ff14;color:#00e5ff;border:1px solid #00e5ff44;
        text-shadow:0 0 8px #00e5ff80}
      .bv-badge-opt{background:#ffffff07;color:#4a5070;border:1px solid #ffffff10}
      .bv-phase{display:inline-block;font-size:9px;font-weight:700;padding:2px 8px;
        border-radius:10px;letter-spacing:.5px}
      .bv-divider{border:none;border-top:1px solid #0f0f28;margin:8px 0}

      /* ── Animations ─────────────────────────────────────────────────────── */

      /* Radar ring pings outward from orb */
      @keyframes bv-ring-out{0%{transform:scale(1);opacity:.72}100%{transform:scale(2.6);opacity:0}}
      .bv-ring{position:absolute;inset:0;border-radius:50%;border:1.5px solid currentColor;
        animation:bv-ring-out .9s ease-out forwards;pointer-events:none}

      /* Orb breathe while BET is active */
      @keyframes bv-breathe{0%,100%{transform:scale(1)}50%{transform:scale(1.055)}}
      .bv-orb-breathe{animation:bv-breathe 1.8s ease-in-out infinite}

      /* BET unlock radial burst */
      @keyframes bv-burst{0%{opacity:.85;transform:scale(1)}100%{opacity:0;transform:scale(3)}}
      .bv-burst{position:absolute;inset:0;border-radius:50%;
        background:radial-gradient(circle,#00e5ff60 0%,#c97bff20 50%,transparent 70%);
        animation:bv-burst .6s ease-out forwards;pointer-events:none}

      /* Win / lose edge flash */
      @keyframes bv-flash-win{
        0%,100%{box-shadow:0 0 32px #00e5ff10,0 12px 40px #00000099}
        35%{box-shadow:0 0 0 3px #00ff9450,0 0 52px #00ff9422,0 12px 40px #00000099}}
      @keyframes bv-flash-lose{
        0%,100%{box-shadow:0 0 32px #00e5ff10,0 12px 40px #00000099;transform:none}
        20%{transform:translateX(-2px);box-shadow:0 0 0 3px #ff3d7150,0 12px 40px #00000099}
        40%{transform:translateX(2px)}
        60%{transform:none;box-shadow:0 0 0 3px #ff3d7130,0 12px 40px #00000099}}
      .bv-flash-win{animation:bv-flash-win .75s ease-out forwards}
      .bv-flash-lose{animation:bv-flash-lose .7s ease-out forwards}

      /* New-shoe scan wipe across header */
      @keyframes bv-scan-wipe{0%{left:0;opacity:.8}100%{left:100%;opacity:0}}
      .bv-scan-wipe{position:absolute;top:0;bottom:0;width:3px;
        background:linear-gradient(180deg,transparent,#ffffff,transparent);
        pointer-events:none;animation:bv-scan-wipe .6s ease-in forwards}

      /* Title shimmer via ::after */
      .bv-logo{position:relative;display:inline-block}
      .bv-logo::after{content:'';position:absolute;top:0;left:-110%;width:55%;height:100%;
        background:linear-gradient(90deg,transparent,rgba(255,255,255,.26),transparent);
        animation:bv-logo-shimmer 9s ease-in-out infinite;pointer-events:none}
      @keyframes bv-logo-shimmer{0%,28%{left:-110%}58%,100%{left:230%}}

      /* Title letter-spacing pulse on BET first-activate */
      @keyframes bv-logo-widen{0%{letter-spacing:3px}40%{letter-spacing:7px}100%{letter-spacing:3px}}
      .bv-logo-widen{animation:bv-logo-widen .75s ease-out}

      /* BET badge glow pulse at ~1 Hz */
      @keyframes bv-badge-glow{
        0%,100%{box-shadow:0 0 5px #00e5ff28,inset 0 0 3px transparent}
        50%{box-shadow:0 0 18px #00e5ff90,0 0 30px #00e5ff28,inset 0 0 7px #00e5ff18}}
      .bv-badge-pulse{animation:bv-badge-glow 1s ease-in-out infinite}

      /* Floating balance delta */
      @keyframes bv-float{0%{transform:translateY(0);opacity:1}100%{transform:translateY(-32px);opacity:0}}
      .bv-float-delta{position:fixed;font-size:13px;font-weight:800;pointer-events:none;
        z-index:2147483648;animation:bv-float 1.1s ease-out forwards;white-space:nowrap;
        font-family:'Cinzel','Palatino Linotype',serif;letter-spacing:.5px}

      /* Dragon badge shake */
      @keyframes bv-shake{0%,100%{transform:none}
        18%{transform:translateX(-4px)}38%{transform:translateX(4px)}
        55%{transform:translateX(-3px)}72%{transform:translateX(3px)}}
      .bv-shake{animation:bv-shake .48s ease-in-out}

      /* Bead row pop-in + streak wave */
      @keyframes bv-bead-pop{0%{transform:scale(0)}62%{transform:scale(1.45)}100%{transform:scale(1)}}
      @keyframes bv-bead-wave{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}
      .bv-bead{display:inline-block;width:10px;height:10px;border-radius:50%;
        margin:2px 2px;vertical-align:middle;flex-shrink:0}
      .bv-bead-new{animation:bv-bead-pop .24s ease-out both}
      .bv-bead-wave{animation:bv-bead-wave .65s ease-in-out infinite}

      /* Section header first-data sweep */
      @keyframes bv-sweep{0%{left:-100%;opacity:.7}100%{left:210%;opacity:0}}
      .bv-sweep{position:absolute;top:0;bottom:0;left:-100%;width:55%;
        background:linear-gradient(90deg,transparent,#00e5ff1a,transparent);
        pointer-events:none;animation:bv-sweep .55s ease-out forwards}

      /* Chevron open / close bounce */
      @keyframes bv-chev-down{0%,100%{transform:translateY(0)}42%{transform:translateY(3px)}72%{transform:translateY(-1px)}}
      @keyframes bv-chev-up{0%,100%{transform:translateY(0)}42%{transform:translateY(-3px)}72%{transform:translateY(1px)}}
      .bv-chev-open{animation:bv-chev-down .32s ease-out}
      .bv-chev-close{animation:bv-chev-up .32s ease-out}

      /* Bar fill shimmer glint */
      @keyframes bv-glint{0%{left:-80%;opacity:0}15%{opacity:.45}80%{opacity:.3}100%{left:130%;opacity:0}}
      .bv-bar-fill{position:relative;overflow:hidden}
      .bv-bar-fill::after{content:'';position:absolute;top:0;bottom:0;left:-80%;width:40%;
        background:linear-gradient(90deg,transparent,rgba(255,255,255,.5),transparent);
        animation:bv-glint 2.6s ease-in-out infinite;pointer-events:none}

      /* Rotating neon border ring — slow hue cycle (30 s) */
      @keyframes bv-ring-hue{
        0%{border-color:#00e5ff28}25%{border-color:#c97bff22}
        50%{border-color:#00ff9422}75%{border-color:#ffd70018}100%{border-color:#00e5ff28}}
      #bv-border-ring{position:absolute;inset:0;border-radius:13px;
        border:1px solid #00e5ff28;pointer-events:none;z-index:0;
        animation:bv-ring-hue 30s linear infinite;
        transition:border-color 1.5s ease,box-shadow 1.5s ease}
    `;
    document.head.appendChild(s);
  }

  function ensurePanel() {
    if (panel) return;
    injectCSS();
    panel = document.createElement("div");
    panel.id = "bv-panel";
    // Animated neon border ring — sits below all content (z-index:0)
    const _bvBorderRing = document.createElement("div");
    _bvBorderRing.id = "bv-border-ring";
    panel.appendChild(_bvBorderRing);

    // ── Header (drag handle + minimize) ──
    const hdr = document.createElement("div");
    hdr.id = "bv-hdr";
    hdr.innerHTML =
      `<div class="bv-logo">BACCARAT VISION</div>` +
      `<div id="bv-status">starting…</div>` +
      `<button id="bv-min" title="Minimise">&#x2013;</button>`;
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
      panel.querySelector("#bv-min").innerHTML = _minimised ? "&#x25A1;" : "&#x2013;";
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
    const secPick    = makeSection("pick",    "target",      "Next Pick",  true);
    const secSpread  = makeSection("spread",  "coins",       "Bet Spread", true);
    const secPattern = makeSection("pattern", "bar-chart",   "Pattern",    true);
    const secModel   = makeSection("model",   "cpu",         "AI Engine",  true);
    const secBalance = makeSection("balance", "wallet",      "Balance",    true);
    const secSession = makeSection("session", "trending-up", "Session",    true);
    const secHand    = makeSection("hand",    "layers",      "Last Hand",  false);
    const secBets    = makeSection("bets",    "list",        "All Bets",   false);
    [secPick, secSpread, secPattern, secModel, secBalance, secSession, secHand, secBets]
      .forEach((s) => body.appendChild(s));

    document.body.appendChild(panel);
  }

  function setStatus(s) { ensurePanel(); if (statusEl) statusEl.textContent = s; }

  function $(id) { return panel ? panel.querySelector(`#bvsb-${id}`) : null; }

  // ─── Render helpers ────────────────────────────────────────────────────────

  function renderVoteSummary(vs) {
    if (!vs || !vs.votes || !Object.keys(vs.votes).length) return "";
    const total = Object.values(vs.votes).reduce((a, b) => a + b, 0) || 1;
    const items = Object.entries(vs.votes)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([bet, w]) => {
        const pct = (w / total * 100).toFixed(0);
        const c = BET_COLOR[bet] || "#b0b8d8";
        return `<span class="bv-chip"><span style="color:${c}">${BET_NAMES[bet] || bet}</span>` +
               `<span style="color:#4a5070"> ${pct}%</span></span>`;
      }).join("");
    const dissent = (vs.top_dissent || []).slice(0, 2).join(", ");
    return `<hr class="bv-divider">` +
      `<div style="font-size:10px;color:#4a5070;margin-bottom:3px">Expert votes (${(vs.agreement * 100).toFixed(0)}% agree)</div>` +
      `<div>${items}</div>` +
      (dissent ? `<div class="bv-reason" style="margin-top:3px">Dissent: ${dissent}</div>` : "");
  }

  function renderCalibration(cal) {
    if (!cal || !cal.length) return "";
    const rows = cal.filter((c) => c.n >= 5);
    if (rows.length < 2) return "";
    const rowHtml = rows.map((c) => {
      const diff = c.actual_rate - c.expected_rate;
      const col = Math.abs(diff) < 0.05 ? "#00ff94" : Math.abs(diff) < 0.12 ? "#ffd700" : "#ff3d71";
      return `<div class="bv-row">` +
        `<span class="bv-lbl">${c.bin_label}</span>` +
        `<span class="bv-val" style="color:${col}">${(c.actual_rate * 100).toFixed(0)}%` +
        ` <span style="color:#4a5070;font-size:9px">vs ${(c.expected_rate * 100).toFixed(0)}% (n=${c.n})</span></span></div>`;
    }).join("");
    return `<hr class="bv-divider">` +
      `<div style="font-size:10px;color:#4a5070;margin-bottom:3px">Calibration</div>` +
      rowHtml;
  }

  function renderTemplateMatch(tm) {
    if (!tm) return "";
    const col = tm.pick === "B" ? "#ff3d71" : tm.pick === "P" ? "#00b4ff" : "#00ff94";
    const name = tm.pick === "B" ? "Banker" : tm.pick === "P" ? "Player" : "Tie";
    return `<hr class="bv-divider">` +
      `<div class="bv-row" style="margin-top:2px">` +
      `<span class="bv-lbl">Template match</span>` +
      `<span class="bv-val" style="color:${col}">${name} (${(tm.confidence * 100).toFixed(0)}%)</span></div>` +
      `<div class="bv-reason">From ${tm.n_matches} similar shoes · ` +
      `B${(tm.b_pct * 100).toFixed(0)}% P${(tm.p_pct * 100).toFixed(0)}% T${(tm.t_pct * 100).toFixed(0)}%</div>`;
  }

  // ─── Render ────────────────────────────────────────────────────────────────
  function render(data, hand, counter) {
    ensurePanel();
    setStatus(`H${counter.hand} · ${counter.P}P ${counter.B}B ${counter.T}T`);
    if (_minimised) return;

    // ── New shoe scan wipe ──────────────────────────────────────────────────
    if (_prevHandNum > 5 && counter.hand <= 2) {
      _handHistory = [];
      _sectionInited = {};
      scanNewShoe();
    }
    _prevHandNum = counter.hand;

    // ── Append this hand to local bead history ──────────────────────────────
    if (hand && hand.winner) {
      _handHistory.push(hand.winner);
      if (_handHistory.length > 14) _handHistory.shift();
    }

    if (!data) {
      const el = $("pick");
      if (el) el.innerHTML = `<div style="display:flex;align-items:center;gap:5px;color:#ff9f00;font-size:11px">${icon("alert-triangle",13,"#ff9f00")} Engine offline — run the server</div>`;
      return;
    }

    const m  = data.mystic;
    const st = data.staking;
    const L  = data.learning;
    const ds = data.dynamic_spread;
    const bk = data.bankroll;
    const pat = data.pattern;
    const cur = (st && st.currency) || (bk && bk.currency) || "";

    // ── Win / lose edge flash ───────────────────────────────────────────────
    if (hand && session.betSignalledPick) {
      const winnerMap = { player: "P", banker: "B", tie: "T" };
      edgeFlash(hand.winner === winnerMap[session.betSignalledPick] ? "win" : "lose");
    }

    // ── Regime border ───────────────────────────────────────────────────────
    const _regimeColors = { Dragon:"#ff3d71", Choppy:"#00ff94", Mixed:"#ffd700", Forming:"#c97bff" };
    const _regimeCol = pat && _regimeColors[pat.personality];
    const _bRing = panel && panel.querySelector("#bv-border-ring");
    if (_bRing) {
      if (_regimeCol) {
        _bRing.style.borderColor = _regimeCol + "45";
        _bRing.style.boxShadow   = `0 0 20px ${_regimeCol}18`;
        _bRing.style.animationPlayState = "paused";
      } else {
        _bRing.style.borderColor = "";
        _bRing.style.boxShadow   = "";
        _bRing.style.animationPlayState = "";
      }
    }

    // ── Next Pick ────────────────────────────────────────────────────────── //
    const pickEl = $("pick");
    if (pickEl && m && m.pick) {
      if (!_sectionInited.pick) { sweepSection("pick"); _sectionInited.pick = true; }

      const col = m.confident ? (BET_COLOR[m.pick] || "#b0b8d8") : "#4a5070";
      const letter = m.pick === "banker" ? "B" : m.pick === "player" ? "P"
                   : m.pick === "tie"    ? "T" : m.pick[0].toUpperCase();
      // Badge includes pulse class when BET is active (drives 1 Hz glow)
      const badge = m.confident
        ? `<span class="bv-badge bv-badge-bet bv-badge-pulse">${icon("diamond",9,"#00e5ff")} BET</span>`
        : `<span class="bv-badge bv-badge-opt">WAIT</span>`;
      const stakeStr = st ? `${fmtK(st.stake)}${cur ? " " + cur : ""}` : "—";
      const vibe = m.vibe || 0;

      // Unlock arc — shown as a circular progress ring around the orb while waiting
      const showArc = !m.confident && L && (L.acts || 0) > 0;
      const arcPct  = showArc ? Math.min(1, (L.acts || 0) / (L.min_hands || 15)) : 0;

      let unlockHtml = "";
      if (L) {
        if (m.confident) {
          const proven = L.significant ? "proven edge" : "net +edge";
          unlockHtml = `<div class="bv-reason" style="display:flex;align-items:center;gap:4px;color:#00ff94;margin-top:5px">${icon("check",10,"#00ff94")} ${proven} · ${L.acts} hands graded</div>`;
        } else {
          const have = L.acts || 0, need = L.min_hands || 15;
          const pph = (L.profit_per_hand || 0) * 100;
          const why = have < need ? `${have}/${need} hands`
            : pph <= 0 ? `P/H ${pph.toFixed(1)}` : "building confidence";
          unlockHtml = `<div class="bv-reason" style="display:flex;align-items:center;gap:4px;margin-top:5px">${icon("lock",10,"#5a6080")} BET unlocks: ${why}</div>`;
        }
      }

      const reasons = (m.reasons || []).slice(0, 2)
        .map((r) => `<div class="bv-reason">• ${r}</div>`).join("");

      const orbClass = m.confident ? "bv-orb bv-orb-breathe" : "bv-orb";

      pickEl.innerHTML =
        `<div style="display:flex;align-items:center;gap:14px;padding:4px 2px 10px">` +
        // Orb wrapper — position:relative lets arc SVG and ring/burst children anchor here
        `<div id="bv-orb-wrap" style="position:relative;width:62px;height:62px;flex-shrink:0">` +
        `<div class="${orbClass}" style="width:100%;height:100%;background:${col}16;` +
        `border:2px solid ${col}50;filter:drop-shadow(0 0 14px ${col});color:${col}">${letter}</div>` +
        (showArc ? arcSvg(arcPct) : "") +
        `</div>` +
        `<div style="flex:1;min-width:0">` +
        `<div style="font-family:'Cinzel','Palatino Linotype',serif;font-size:19px;font-weight:700;` +
        `color:${col};letter-spacing:.5px;line-height:1.1;margin-bottom:6px;white-space:nowrap;` +
        `overflow:hidden;text-overflow:ellipsis">${m.pick_label.toUpperCase()}</div>` +
        `<div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap">` +
        badge +
        `<span style="font-size:12px;font-weight:700;color:#00e5ff">${stakeStr}</span>` +
        `</div></div></div>` +
        bar(vibe, col, 5) +
        `<div class="bv-row" style="margin-top:5px">` +
        `<span class="bv-lbl">Confidence</span>` +
        `<span class="bv-val" style="color:${col}">${(vibe * 100).toFixed(0)}%</span></div>` +
        unlockHtml + reasons;

      // Post-render: radar ping on every hand
      const orbWrap = pickEl.querySelector("#bv-orb-wrap");
      pingOrb(orbWrap);

      // BET first-activate: radial burst + title letter-spacing widen
      if (m.confident && !_prevConfident) {
        if (orbWrap) {
          const b = document.createElement("div");
          b.className = "bv-burst";
          orbWrap.appendChild(b);
          setTimeout(() => b.remove(), 680);
        }
        const logo = panel && panel.querySelector(".bv-logo");
        if (logo) {
          logo.classList.remove("bv-logo-widen");
          void logo.offsetWidth;
          logo.classList.add("bv-logo-widen");
          setTimeout(() => logo.classList.remove("bv-logo-widen"), 800);
        }
      }
      _prevConfident = m.confident;
    }

    // ── Bet Spread ───────────────────────────────────────────────────────── //
    const spreadEl = $("spread");
    if (spreadEl) {
      if (!_sectionInited.spread && ds && ds.legs && ds.legs.length) { sweepSection("spread"); _sectionInited.spread = true; }
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
            `<span class="bv-leg-name" style="color:${lc}">${betDot(leg.bet)}${leg.label}</span>` +
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

        // Kelly optimal stake
        if (ds.kelly_stake > 0) {
          html += `<div class="bv-row" style="margin-top:3px">` +
            `<span class="bv-lbl">Kelly optimal</span>` +
            `<span class="bv-val" style="color:#c97bff">${fmtK(ds.kelly_stake)}${cur ? " " + cur : ""}` +
            ` <span style="color:#4a5070;font-size:9px">(${(ds.kelly_fraction * 100).toFixed(2)}% bankroll)</span></span></div>`;
        }
        // Pair probability
        if (ds.pair_probs && ds.pair_probs.either_pair != null) {
          const ep = ds.pair_probs.either_pair;
          const bep = ds.pair_probs.baseline_either_pair || 0.001;
          const ratio = ep / bep;
          const pairCol = ratio >= 1.08 ? "#ffd700" : "#4a5070";
          html += `<div class="bv-row" style="margin-top:3px">` +
            `<span class="bv-lbl" style="color:${pairCol}">Pair probability</span>` +
            `<span class="bv-val" style="color:${pairCol}">${(ep * 100).toFixed(1)}%` +
            ` <span style="color:#4a5070;font-size:9px">(${ratio >= 1 ? "+" : ""}${((ratio - 1) * 100).toFixed(0)}% vs base)</span></span></div>`;
        }
        if (!ds.affordable) {
          html += `<div style="display:flex;align-items:center;gap:4px;color:#ff9f00;font-size:10px;margin-top:3px">${icon("alert-triangle",11,"#ff9f00")} scaled to fit balance</div>`;
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
        const warn = st.spread_affordable ? "" : ` <span style="display:inline-flex;align-items:center;gap:3px;color:#ff9f00">${icon("alert-triangle",11,"#ff9f00")} over balance</span>`;
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

    // ── Pattern ──────────────────────────────────────────────────────────── //
    const patEl = $("pattern");
    if (patEl && pat) {
      if (!_sectionInited.pattern) { sweepSection("pattern"); _sectionInited.pattern = true; }

      const since = pat.hands_since || {};
      const dragonBadge = pat.is_dragon
        ? ` <span class="bv-dragon-badge" style="font-size:9px;font-weight:800;color:#ff3d71;letter-spacing:.5px">DRAGON</span>`
        : "";
      const streakTxt = pat.streak_len > 1
        ? `${pat.streak_len}× ${pat.streak_side}${dragonBadge}`
        : "none";
      const chopPct = (pat.chop_score * 100).toFixed(0);
      const chopCol = pat.chop_score > 0.6 ? "#00ff94" : pat.chop_score > 0.35 ? "#ffd700" : "#ff6b6b";
      const persColor = { Dragon: "#ff3d71", Choppy: "#00ff94", Mixed: "#ffd700", Forming: "#c97bff" }[pat.personality] || "#4a5070";
      const shoes = (data.library && data.library.shoes) ? data.library.shoes : 0;

      // Animated bead row — last 14 hands, newest bead pops in, streak beads wave
      const beadCols = { P:"#00b4ff", B:"#ff3d71", T:"#00ff94" };
      const beadHtml = _handHistory.length
        ? `<div style="display:flex;flex-wrap:wrap;align-items:center;margin-top:8px;gap:0">` +
          _handHistory.map((w, i) => {
            const isNew = hand && i === _handHistory.length - 1;
            const inStreak = pat.streak_len > 1 && i >= _handHistory.length - pat.streak_len && !isNew;
            const cls = `bv-bead${isNew ? " bv-bead-new" : inStreak ? " bv-bead-wave" : ""}`;
            const wdly = inStreak ? `animation-delay:${(i % pat.streak_len) * 0.11}s;` : "";
            const col  = beadCols[w] || "#5a6080";
            return `<span class="${cls}" style="background:${col};box-shadow:0 0 5px ${col}55;${wdly}" title="${w}"></span>`;
          }).join("") + `</div>`
        : "";

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
        beadHtml +
        `<div class="bv-row" style="margin-top:4px">` +
        `<span class="bv-lbl">Last Tie / P / B</span>` +
        `<span class="bv-val">${since.T ?? "?"}h · ${since.P ?? "?"}h · ${since.B ?? "?"}h ago</span></div>` +
        (shoes ? `<div class="bv-row"><span class="bv-lbl">Shoe library</span>` +
        `<span class="bv-val" style="color:#4a5070">${shoes} logged</span></div>` : "") +
        renderTemplateMatch(data.template_match);

      // Dragon onset shake
      if (pat.is_dragon && !_prevDragon) {
        const badge = patEl.querySelector(".bv-dragon-badge");
        if (badge) {
          badge.classList.remove("bv-shake");
          void badge.offsetWidth;
          badge.classList.add("bv-shake");
          setTimeout(() => badge && badge.classList.remove("bv-shake"), 520);
        }
      }
      _prevDragon = !!pat.is_dragon;
    }

    // ── AI Engine ────────────────────────────────────────────────────────── //
    const modelEl = $("model");
    if (modelEl) {
      if (!_sectionInited.model && L && L.graded > 0) { sweepSection("model"); _sectionInited.model = true; }
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
          `text-overflow:ellipsis;white-space:nowrap">${L.verdict}</div>` +
          renderVoteSummary(data.vote_summary) +
          renderCalibration(data.calibration);
      } else {
        modelEl.innerHTML = `<div style="display:flex;align-items:center;gap:6px;color:#4a5070;font-size:11px">${icon("refresh-cw",13,"#4a5070")} Collecting data — grading every hand…</div>`;
      }
    }

    // ── Balance ──────────────────────────────────────────────────────────── //
    const balEl = $("balance");
    if (balEl) {
      if (!_sectionInited.balance && bk && bk.balance) { sweepSection("balance"); _sectionInited.balance = true; }
      if (bk && bk.currency && bk.balance) {
        // Floating delta on balance change
        if (_prevBalanceAnim !== null && bk.balance !== _prevBalanceAnim) {
          const diff = bk.balance - _prevBalanceAnim;
          const sign = diff >= 0 ? "+" : "-";
          floatDelta(`${sign}${fmtK(Math.abs(diff))}`, diff >= 0 ? "#00ff94" : "#ff3d71", balEl);
        }
        _prevBalanceAnim = bk.balance;

        const delta = (bk.balance != null && bk.shoe_start != null) ? bk.balance - bk.shoe_start : null;
        const deltaCol = delta == null ? "" : delta >= 0 ? "#00ff94" : "#ff3d71";
        balEl.innerHTML =
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
        balEl.innerHTML = `<div style="display:flex;align-items:center;gap:5px;color:#ff9f00;font-size:11px">${icon("alert-triangle",13,"#ff9f00")} Balance not detected — open the table first</div>`;
      }
    }

    // ── Session ──────────────────────────────────────────────────────────── //
    const sessEl = $("session");
    if (sessEl) {
      const followed = session.followedHands;
      const pl = session.followedPL;
      const plCol = pl >= 0 ? "#00ff94" : "#ff3d71";
      sessEl.innerHTML =
        (followed > 0
          ? `<div style="font-size:17px;font-weight:800;color:${plCol};margin-bottom:3px">` +
            `${pl >= 0 ? "▲" : "▼"} ${fmtK(Math.abs(pl))}${cur ? " " + cur : ""}</div>` +
            `<div class="bv-reason" style="margin-bottom:5px">on ${followed} BET-signalled hand${followed !== 1 ? "s" : ""}</div>`
          : `<div class="bv-reason" style="margin-bottom:5px">No BET signals recorded yet</div>`) +
        `<div class="bv-row"><span class="bv-lbl">Total new hands</span>` +
        `<span class="bv-val">${session.totalHands}</span></div>` +
        `<div class="bv-row"><span class="bv-lbl">BET signals followed</span>` +
        `<span class="bv-val">${followed}</span></div>`;
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

    // ── All Bets ─────────────────────────────────────────────────────────── //
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
          const star = r.significant ? `<span style="display:inline-flex;vertical-align:middle;margin-left:3px">${icon("check",9,"#00ff94")}</span>` : "";
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
    // Read counter synchronously here so the 250 ms debounce below can't race
    // against a subsequent "Burn Card Procedure" DOM update that hides the counter.
    const _oc = readCounter();
    if (_oc) _savedCounter = _oc;
    if (pending) return;
    pending = setTimeout(() => { pending = null; tick(); }, 250);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  setInterval(tick, 1500);
  window.__bv = { readCounter, readHand, parseCard, tick, SEL };
})();
