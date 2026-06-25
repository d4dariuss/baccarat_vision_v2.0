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
  // Returns true while the extension context is alive.
  function _ctxOk() { try { return !!chrome.runtime?.id; } catch (_) { return false; } }

  function api(path, method = "GET", body = null) {
    // /probe selects the slot — don't inject a slot into that call itself.
    const slottedPath = path === "/probe" ? path : path + `?slot=${assignedSlot}`;
    return new Promise((resolve) => {
      if (!_ctxOk()) { resolve(null); return; }
      try {
        chrome.runtime.sendMessage({ type: "api", path: slottedPath, method, body }, (resp) => {
          try {
            if (chrome.runtime.lastError || !resp) return resolve(null);
            resolve(resp.ok ? resp.data : null);
          } catch (_) { resolve(null); }
        });
      } catch (_) { resolve(null); }
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
      if (!_ctxOk()) { r(null); return; }
      try {
        chrome.runtime.sendMessage({ type: "getBalance" }, (resp) => {
          try { r(resp && resp.ok ? resp.data : null); } catch (_) { r(null); }
        });
      } catch (_) { r(null); }
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
  // Fingerprint of the last hand we posted to /hand — used to avoid double-posting
  // the final hand of a shoe when the counter doesn't update for it (Iconic21 bug).
  let _lastHandFingerprint = null;

  // ─── Session followed-picks P/L ────────────────────────────────────────────
  const session = {
    betSignalledPick: null,
    followedPL: 0,
    followedHands: 0,
    totalHands: 0,
    wins: 0,
    losses: 0,
  };

  // Per-shoe session history — persisted across page refreshes via chrome.storage
  let _shoeHistory = [];  // [{shoe, wins, losses, pl, hands, ts}]
  let _shoeNum = 0;       // incremented on each new shoe
  const _SESS_KEY = "bv_session_v1";
  const _SESS_TTL = 8 * 3600 * 1000; // 8 hours — discard stale data from prior day

  function _saveSession() {
    if (!_ctxOk()) return;
    try {
      chrome.storage.local.set({ [_SESS_KEY]: {
        shoeHistory: _shoeHistory,
        shoeNum: _shoeNum,
        current: { wins: session.wins, losses: session.losses,
                   followedPL: session.followedPL, followedHands: session.followedHands },
        ts: Date.now(),
      }});
    } catch (_) {}
  }

  function _loadSession() {
    if (!_ctxOk()) return;
    try {
      chrome.storage.local.get(_SESS_KEY, (data) => {
        const d = data && data[_SESS_KEY];
        if (!d || !d.ts || Date.now() - d.ts > _SESS_TTL) return;
        _shoeHistory = d.shoeHistory || [];
        _shoeNum = d.shoeNum || 0;
        if (d.current) {
          session.wins = d.current.wins || 0;
          session.losses = d.current.losses || 0;
          session.followedPL = d.current.followedPL || 0;
          session.followedHands = d.current.followedHands || 0;
        }
      });
    } catch (_) {}
  }

  function _archiveCurrentShoe() {
    if (session.wins + session.losses > 0 || session.followedHands > 0) {
      _shoeHistory.push({
        shoe: _shoeNum,
        wins: session.wins, losses: session.losses,
        pl: session.followedPL, hands: session.followedHands,
        ts: Date.now(),
      });
      // Keep only last 20 shoes to cap storage size
      if (_shoeHistory.length > 20) _shoeHistory.shift();
    }
    _shoeNum++;
    session.wins = 0; session.losses = 0;
    session.followedPL = 0; session.followedHands = 0;
    _saveSession();
  }

  // Read actual bets placed on the table from the game DOM (best-effort)
  function readPlacedBets() {
    const bets = {};
    // Try data-locator attribute patterns used by Spinquest/Iconic21
    const tryLocator = (key, ...locs) => {
      for (const loc of locs) {
        const el = document.querySelector(`[data-locator="${loc}"]`)
          || document.querySelector(`[data-locator*="${loc}"]`);
        if (!el) continue;
        const amt = parseAmount(el.textContent);
        if (amt > 0) { bets[key] = amt; return; }
      }
    };
    tryLocator("banker",      "gameline", "banker", "main-bet", "gameline-bet");
    tryLocator("player",      "player", "player-bet");
    tryLocator("tie",         "tie", "tie-bet");
    tryLocator("super_6",     "super6", "super-6", "superhot6");
    tryLocator("b_bonus",     "bbonus", "b-bonus", "banker-bonus");
    tryLocator("p_bonus",     "pbonus", "p-bonus", "player-bonus");
    tryLocator("either_pair", "either-pair", "anypair", "pair");

    // Fallback: class-based bet amount elements
    if (!Object.keys(bets).length) {
      for (const el of document.querySelectorAll(
          '[class*="betAmount"],[class*="bet-amount"],[class*="totalBet"],[class*="wager__amount"]')) {
        const amt = parseAmount(el.textContent);
        if (!amt) continue;
        const anc = el.closest('[data-locator]') || el.closest('[data-bet]');
        if (!anc) continue;
        const hint = (anc.getAttribute('data-locator') || anc.getAttribute('data-bet') || '').toLowerCase();
        if (hint.includes('banker') || hint.includes('gameline')) bets.banker = (bets.banker || 0) + amt;
        else if (hint.includes('player')) bets.player = (bets.player || 0) + amt;
        else if (hint.includes('tie')) bets.tie = (bets.tie || 0) + amt;
        else if (hint.includes('super6') || hint.includes('super-6')) bets.super_6 = (bets.super_6 || 0) + amt;
        else if (hint.includes('bonus') && (hint.includes('b') || hint.includes('banker'))) bets.b_bonus = (bets.b_bonus || 0) + amt;
        else if (hint.includes('bonus')) bets.p_bonus = (bets.p_bonus || 0) + amt;
        else if (hint.includes('pair')) bets.either_pair = (bets.either_pair || 0) + amt;
      }
    }
    return Object.keys(bets).length ? bets : null;
  }

  function dumpScoreboardOnce() {
    if (dumpedScoreboard) return;
    const sb = document.querySelector('[class*="scoreBoardInfo"]')
      || document.querySelector('[class*="baccarat__history"]');
    if (sb) { dumpedScoreboard = true; api("/debug-card", "POST", { html: "SCOREBOARD:\n" + sb.outerHTML }); }
  }

  // One-time dump of bet areas so we can identify DOM selectors for readPlacedBets()
  let _dumpedBetArea = false;
  function dumpBetAreaOnce() {
    if (_dumpedBetArea) return;
    // Try to find any element containing bet amounts
    const candidates = [
      document.querySelector('[class*="bettingArea"],[class*="betArea"],[class*="gameLine"],[class*="game-line"]'),
      document.querySelector('[class*="chipTray"],[class*="chip-tray"],[class*="betPanel"]'),
      document.querySelector('[class*="totalBet"],[class*="total-bet"],[class*="wager"]'),
      document.querySelector('[data-locator*="gameline"],[data-locator*="bet"]'),
    ].filter(Boolean);
    if (candidates.length) {
      _dumpedBetArea = true;
      const html = candidates.map(el => `[${el.className||el.getAttribute('data-locator')}]:\n${el.outerHTML.slice(0, 800)}`).join("\n\n");
      api("/debug-card", "POST", { html: "BET_AREA:\n" + html });
    }
  }

  let sawGame = false;
  let _tickRunning = false; // prevents observer + interval from double-counting the same hand
  async function tick() {
    if (_tickRunning) return;
    if (!_ctxOk()) return; // Extension reloaded — stop silently
    _tickRunning = true;
    try { await _tick(); } finally { _tickRunning = false; }
  }
  async function _tick() {
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
    dumpBetAreaOnce();
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
          // New shoe (or fresh slot) — archive any prior shoe data, then full reset.
          _archiveCurrentShoe();
          lastSnapshot = await api("/reset", "POST", { burn_cards: 10, hands: sum(cur) });
          setStatus(`slot ${assignedSlot} — new shoe at hand ${c.hand}`);
        }
      } else {
        // Server offline or probe failed — archive prior data and reset.
        _archiveCurrentShoe();
        lastSnapshot = await api("/reset", "POST", { burn_cards: 10, hands: sum(cur) });
        setStatus(`synced at hand ${c.hand}`);
      }
      counts = cur; lastHand = c.hand;
      render(lastSnapshot, null, c);
      return;
    }
    if (sum(cur) < sum(counts) || c.hand < lastHand) {
      // The casino counter reset -> a new shoe.
      // Iconic21 does NOT update the road totals for the final hand of a shoe.
      // The cards are still visible on the table when this fires, so try to
      // capture and post that missed hand before archiving the old shoe.
      const lastHandCards = readHand();
      if (lastHandCards) {
        const fp = `${lastHandCards.winner}${lastHandCards.player_total}${lastHandCards.banker_total}`;
        if (fp !== _lastHandFingerprint) {
          // This hand wasn't posted yet — post it now before the reset.
          await api("/hand", "POST", {
            winner: lastHandCards.winner,
            player_total: lastHandCards.player_total,
            banker_total: lastHandCards.banker_total,
            is_natural: lastHandCards.is_natural,
            p_pair: lastHandCards.p_pair,
            b_pair: lastHandCards.b_pair,
            p_suited_pair: lastHandCards.p_suited_pair,
            b_suited_pair: lastHandCards.b_suited_pair,
            card_values: lastHandCards.card_values,
          });
          _lastHandFingerprint = fp;
        }
      }
      prevBalance = null; // discard stale reading so first hand of new shoe is clean
      session.betSignalledPick = null; // reset BET arm on new shoe
      _lastHandFingerprint = null; // reset fingerprint for new shoe
      _archiveCurrentShoe(); // persist shoe stats before reset
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
        // Fire edge flash + win/loss count here (balance-detected path).
        // render() checks betSignalledPick too but it will be null by then,
        // so that block acts as fallback only when balance is undetectable.
        if (delta > 0) {
          session.wins++;
          edgeFlash("win");
        } else if (delta < 0) {
          session.losses++;
          edgeFlash("lose");
        }
        // Still set flag so render() skips the "no-bet" subtle prediction ping
        _lastHandBetFollowed = true;
        _lastHandBetWon = delta > 0;
        session.betSignalledPick = null;
        _saveSession(); // persist after each bet outcome
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
    // Track fingerprint so the shoe-end rescue doesn't double-post this hand.
    if (exact) _lastHandFingerprint = `${exact.winner}${exact.player_total}${exact.banker_total}`;
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
    banker: "#ff453a", player: "#0a84ff", tie: "#30d158",
    super_6: "#bf5af2", b_bonus: "#ff9f0a", p_bonus: "#ff9f0a",
    either_pair: "#ffd60a", player_pair: "#ff375f", banker_pair: "#ff375f",
    suited_pair: "#ff375f",
  };
  // Colored dot representing a bet type — uses BET_COLOR so defined after it.
  function betDot(bet) {
    const c = BET_COLOR[bet] || "rgba(235,235,245,0.4)";
    return `<svg width="7" height="7" viewBox="0 0 8 8" style="display:inline-block;vertical-align:middle;flex-shrink:0;margin-right:3px"><circle cx="4" cy="4" r="4" fill="${c}"/></svg>`;
  }
  const BET_NAMES = {
    player: "Player", banker: "Banker", super_6: "Super 6", tie: "Tie",
    player_pair: "P Pair", banker_pair: "B Pair", either_pair: "Either Pair",
    suited_pair: "Suited", p_bonus: "P Bonus", b_bonus: "B Bonus",
  };

  function pnl(x, dec = 1) {
    const c = x >= 0 ? "#30d158" : "#ff453a";
    return `<span style="color:${c};font-weight:500">${x >= 0 ? "+" : ""}${x.toFixed(dec)}</span>`;
  }
  function pnlFmt(x) {
    const c = x >= 0 ? "#30d158" : "#ff453a";
    return `<span style="color:${c};font-weight:500">${x >= 0 ? "+" : "-"}${fmtK(Math.abs(x))}</span>`;
  }
  function bar(pct, color, h = 3) {
    const w = Math.max(0, Math.min(100, pct * 100)).toFixed(1);
    return `<div style="height:${h}px;background:rgba(255,255,255,0.08);border-radius:2px;overflow:hidden;margin:3px 0">` +
           `<div class="bv-bar-fill" style="width:${w}%;height:100%;background:${color};border-radius:2px;` +
           `transition:width .35s ease"></div></div>`;
  }

  const SUIT_DISP = { s: "♠", h: "♥", d: "♦", c: "♣" };
  function cardHtml(c) {
    const red = c.suit === "h" || c.suit === "d";
    const col = red ? "#ff453a" : "rgba(235,235,245,0.82)";
    return `<span style="display:inline-block;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.13);border-radius:5px;` +
           `padding:2px 7px;margin:0 2px;font-size:13px;font-family:'JetBrains Mono',monospace;font-weight:600;color:${col}">` +
           `${c.rank}${SUIT_DISP[c.suit] || ""}</span>`;
  }

  // ─── Animation helpers ─────────────────────────────────────────────────────

  function pingOrb(wrapEl, count = 2, color = null) {
    if (!wrapEl) return;
    const col = color || wrapEl.dataset.col || "#0a84ff";
    for (let i = 0; i < count; i++) {
      setTimeout(() => {
        // Re-query the ring in case innerHTML was replaced between ticks
        const target = wrapEl.isConnected ? wrapEl
          : (panel && panel.querySelector("#bv-pick-ring"));
        if (!target) return;
        const r = document.createElement("div");
        r.className = "bv-ring";
        r.style.color = col;
        target.appendChild(r);
        setTimeout(() => r.remove(), 750);
      }, i * 180);
    }
  }

  function subtlePing(wrapEl, col = "rgba(48,209,88,0.35)") {
    if (!wrapEl) return;
    // Re-query in case the ring was replaced
    const target = wrapEl.isConnected ? wrapEl
      : (panel && panel.querySelector("#bv-pick-ring"));
    if (!target) return;
    const r = document.createElement("div");
    r.className = "bv-ring";
    r.style.color = col;
    r.style.opacity = "0.45";
    target.appendChild(r);
    setTimeout(() => r.remove(), 750);
  }

  function pickRingPing(col, count) {
    const ring = panel && panel.querySelector("#bv-pick-ring");
    if (ring) pingOrb(ring, count, col);
  }

  function edgeFlash(type) {
    if (!panel) return;
    panel.classList.remove("bv-flash-win", "bv-flash-lose");
    void panel.offsetWidth;
    panel.classList.add(type === "win" ? "bv-flash-win" : "bv-flash-lose");
    setTimeout(() => panel && panel.classList.remove("bv-flash-win", "bv-flash-lose"), 750);
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
    setTimeout(() => el.remove(), 1100);
  }

  function sweepSection(id) {
    if (!panel) return;
    // Works on both old header elements [data-id] and new zone elements [#bvsb-*]
    const el = panel.querySelector(`[data-id="${id}"]`) || panel.querySelector(`#bvsb-${id}`);
    if (!el) return;
    const sw = document.createElement("div");
    sw.className = "bv-sweep";
    if (window.getComputedStyle(el).position === "static") el.style.position = "relative";
    el.style.overflow = "hidden";
    el.appendChild(sw);
    setTimeout(() => { sw.remove(); el.style.overflow = ""; }, 600);
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
  // Section open/close state — persisted so reloads remember user's choices.
  const _SECSTATE_KEY = "bv_secstate_v1";
  const _sectionState = {};
  let _secStateLoaded = false;

  function _saveSectionState() {
    if (!_ctxOk()) return;
    try { chrome.storage.local.set({ [_SECSTATE_KEY]: _sectionState }); } catch (_) {}
  }

  function _loadSectionState(cb) {
    if (!_ctxOk()) { cb(); return; }
    try {
      chrome.storage.local.get(_SECSTATE_KEY, (d) => {
        if (d && d[_SECSTATE_KEY]) Object.assign(_sectionState, d[_SECSTATE_KEY]);
        _secStateLoaded = true;
        cb();
      });
    } catch (_) { _secStateLoaded = true; cb(); }
  }

  // _secHide / _secShow: use inline setProperty AND a watchdog so even game JS
  // that calls element.style.display = 'block' gets immediately reversed.
  function _secHide(body) {
    body.classList.add("bv-hidden");
    body.style.setProperty("display", "none", "important");
    // Watchdog: game JS can overwrite setProperty; MO fires and re-hides.
    if (!body._bvWatcher) {
      body._bvWatcher = new MutationObserver(() => {
        if (body.classList.contains("bv-hidden") && body.style.display !== "none") {
          body.style.setProperty("display", "none", "important");
        }
      });
      body._bvWatcher.observe(body, { attributes: true, attributeFilter: ["style"] });
    }
  }
  function _secShow(body) {
    if (body._bvWatcher) { body._bvWatcher.disconnect(); body._bvWatcher = null; }
    body.classList.remove("bv-hidden");
    body.style.removeProperty("display");
  }

  function makeSection(id, title, defaultOpen = true) {
    const open = _sectionState[id] !== undefined ? _sectionState[id] : defaultOpen;
    const sec = document.createElement("div");
    sec.className = "bv-section";
    sec.innerHTML =
      `<div class="bvsh" data-id="${id}">` +
        `<span class="bvsh-label">${title}</span>` +
        `<span id="bvsecv-${id}" class="bvsh-value"></span>` +
        `<span class="bvchev" style="transform:rotate(${open ? 90 : 0}deg)">›</span>` +
      `</div>` +
      `<div class="bvsb" id="bvsb-${id}"></div>`;
    const body = sec.querySelector(".bvsb");
    if (!open) _secHide(body);
    sec.querySelector(".bvsh").addEventListener("click", () => {
      const chev = sec.querySelector(".bvchev");
      const nowOpen = !body.classList.contains("bv-hidden");
      nowOpen ? _secHide(body) : _secShow(body);
      chev.style.transform = `rotate(${nowOpen ? 0 : 90}deg)`;
      _sectionState[id] = !nowOpen;
      _saveSectionState();
    });
    return sec;
  }

  // ─── Panel + dragging ──────────────────────────────────────────────────────
  let panel = null, statusEl = null, _minimised = false;

  // ── Animation state ────────────────────────────────────────────────────────
  let _prevConfident       = false;  // BET on/off — drives burst + title widen
  let _prevBalanceAnim     = null;   // last rendered balance — drives delta float
  let _prevDragon          = false;  // dragon onset — drives shake
  let _prevHandNum         = 0;      // detects new shoe for scan wipe
  let _prevHandWinner      = null;   // last rendered hand winner — detects new hand in render
  let _lastHandBetFollowed = false;  // set in tick when a bet-following hand resolves
  let _lastHandBetWon      = false;  // outcome of that followed-bet hand
  let _sectionInited       = {};     // tracks first-data sweep per section
  let _handHistory         = [];     // last 14 P/B/T outcomes for bead row

  function injectCSS() {
    if (document.getElementById("bv-css")) return;
    const s = document.createElement("style");
    s.id = "bv-css";
    s.textContent = `@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

      /* ── Panel shell ──────────────────────────────────────────────────── */
      #bv-panel{position:fixed;top:12px;right:12px;width:280px;
        max-height:calc(100vh - 24px);overflow-y:auto;overflow-x:hidden;
        background:#1c1c1e !important;
        border:1px solid rgba(255,255,255,0.1);border-radius:16px;
        font-family:"Inter",-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
        font-size:12px;color:rgba(235,235,245,0.6);
        z-index:2147483647;
        transform:translateZ(0);will-change:transform;isolation:isolate;
        contain:paint;
        box-shadow:0 24px 60px rgba(0,0,0,0.65),0 0 0 0.5px rgba(255,255,255,0.04);
        box-sizing:border-box;pointer-events:auto !important;}
      /* Prevent game CSS making any panel child transparent */
      #bv-panel *{box-sizing:border-box;max-width:100%;background-color:transparent}
      /* Section bodies that are explicit containers need opaque bg */
      #bv-panel .bvsb,#bv-panel #bv-body{background:#1c1c1e !important}
      /* Reset game-level table/div styles that can bleed in */
      #bv-panel table{border-collapse:collapse;border-spacing:0;border:none}
      #bv-panel td,#bv-panel th{border:none}
      #bv-panel::-webkit-scrollbar{width:3px}
      #bv-panel::-webkit-scrollbar-track{background:transparent}
      #bv-panel::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.12);border-radius:2px}

      /* ── Header ───────────────────────────────────────────────────────── */
      #bv-hdr{position:sticky;top:0;z-index:1;
        display:flex;align-items:center;gap:8px;padding:11px 14px;
        background:#1c1c1e;
        border-bottom:1px solid rgba(255,255,255,0.06);border-radius:15px 15px 0 0;}
      #bv-grip{color:rgba(235,235,245,0.2);font-size:15px;line-height:1;
        cursor:grab;flex-shrink:0;user-select:none;letter-spacing:-0.5px}
      #bv-grip:active{cursor:grabbing}
      #bv-wordmark-wrap{flex:1;display:flex;flex-direction:column;gap:0;min-width:0}
      #bv-wordmark{font-size:11px;font-weight:600;letter-spacing:0.04em;
        color:rgba(235,235,245,0.85);white-space:nowrap}
      #bv-status{font-size:9px;color:rgba(235,235,245,0.2);white-space:nowrap;
        overflow:hidden;text-overflow:ellipsis;margin-top:1px}
      #bv-live-wrap{display:flex;align-items:center;gap:5px;flex-shrink:0}
      #bv-live-dot{width:6px;height:6px;border-radius:50%;background:#30d158;
        box-shadow:0 0 0 2px rgba(48,209,88,0.2);flex-shrink:0}
      #bv-live-label{font-size:10px;font-weight:500;letter-spacing:0.06em;
        color:rgba(235,235,245,0.3)}
      #bv-min{background:none;border:none;color:rgba(235,235,245,0.25);cursor:pointer;
        font-size:17px;line-height:1;padding:2px 0 2px 6px;flex-shrink:0}
      #bv-min:hover{color:rgba(235,235,245,0.6)}
      #bv-shoe-bar-track{position:absolute;left:0;right:0;bottom:0;height:1.5px;
        background:rgba(255,255,255,0.05)}
      #bv-shoe-bar-fill{height:100%;
        background:linear-gradient(90deg,#0a84ff 0%,#30d158 100%);
        border-radius:0 1px 1px 0;transition:width 0.5s ease;width:0}

      /* ── Collapsible sections ─────────────────────────────────────────── */
      .bv-section{border-bottom:1px solid rgba(255,255,255,0.06)}
      .bvsh{display:flex;align-items:center;padding:11px 16px;cursor:pointer;
        user-select:none;transition:background 0.15s ease;gap:6px}
      .bvsh:hover{background:rgba(255,255,255,0.03)}
      .bvsh-label{font-size:10px;font-weight:700;letter-spacing:0.08em;
        color:rgba(235,235,245,0.48);text-transform:uppercase;flex-shrink:0}
      .bvsh-value{font-family:"JetBrains Mono",monospace;font-size:11px;
        font-weight:600;color:rgba(235,235,245,0.55);flex:1;min-width:0;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap;letter-spacing:0;
        padding-left:6px}
      .bvchev{font-size:13px;color:rgba(235,235,245,0.25);
        transition:transform 0.22s ease;display:inline-block;flex-shrink:0;margin-left:auto}
      .bvsb{display:block;padding:10px 16px 14px}
      #bv-panel .bvsb.bv-hidden{display:none !important;padding:0;margin:0;height:0;overflow:hidden}
      #bv-panel #bv-body.bv-hidden{display:none !important}
      #bvsb-session{padding:0}

      /* ── Pick zone ────────────────────────────────────────────────────── */
      #bv-pick-name{font-size:30px;font-weight:700;letter-spacing:-0.025em;
        color:#ffffff;line-height:1;margin-bottom:5px;transition:color 0.3s ease}
      #bv-confidence-num{font-family:"JetBrains Mono",monospace;
        transition:color 0.3s ease}
      #bv-pick-stake{font-family:"JetBrains Mono",monospace;
        font-size:18px;font-weight:600;color:rgba(235,235,245,0.85);
        transition:color 0.3s ease;text-align:right}
      /* Anchor for radar-ring animation on new hand */
      #bv-pick-ring{position:relative;width:40px;height:40px;
        border-radius:50%;flex-shrink:0}

      /* ── Badges ───────────────────────────────────────────────────────── */
      .bv-badge{display:inline-flex;align-items:center;gap:4px;
        font-size:10px;font-weight:600;letter-spacing:0.04em;
        padding:3px 9px;border-radius:6px}
      .bv-badge-bet{border:1px solid transparent}
      .bv-badge-opt{background:rgba(235,235,245,0.06);color:rgba(235,235,245,0.3);
        border:1px solid rgba(235,235,245,0.1)}

      /* ── Allocation zone ──────────────────────────────────────────────── */
      #bvsb-spread{transition:opacity 0.3s ease;padding-top:2px}
      #bvsb-spread.bv-locked{opacity:0.38}
      .bv-leg-row{display:flex;align-items:center;
        padding:6px 0 6px 12px;
        border-left:3px solid transparent;min-width:0;gap:8px;
        border-radius:0 4px 4px 0}
      .bv-leg-name{font-size:11px;font-weight:600;color:rgba(235,235,245,0.9);
        flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .bv-leg-amt{font-family:"JetBrains Mono",monospace;font-size:11px;font-weight:500;
        color:rgba(235,235,245,0.85);white-space:nowrap;flex-shrink:0}
      .bv-leg-ev{font-family:"JetBrains Mono",monospace;font-size:9px;
        white-space:nowrap;flex-shrink:0;opacity:0.6}
      #bv-kelly-track{height:2px;background:rgba(255,255,255,0.07);
        border-radius:1px;overflow:hidden;margin-top:10px}
      #bv-kelly-fill{height:100%;border-radius:1px;
        transition:width 0.4s ease,background 0.4s ease}

      /* ── Session strip ────────────────────────────────────────────────── */
      .bv-session-grid{display:grid;grid-template-columns:1fr 1fr;gap:0}
      .bv-session-cell{padding:10px 16px}
      .bv-session-cell + .bv-session-cell{text-align:right}
      .bv-micro-lbl{font-size:9px;font-weight:600;letter-spacing:0.08em;
        color:rgba(235,235,245,0.2);text-transform:uppercase;margin-bottom:3px}
      #bv-session-pl{font-family:"JetBrains Mono",monospace;
        font-size:16px;font-weight:600;line-height:1;transition:color 0.3s ease}
      #bv-session-meta{font-size:10px;color:rgba(235,235,245,0.3);margin-top:4px;
        display:flex;gap:5px;align-items:center;flex-wrap:wrap}
      #bv-balance-num{font-family:"JetBrains Mono",monospace;
        font-size:19px;font-weight:700;color:rgba(235,235,245,0.92);line-height:1}
      #bv-balance-meta{font-size:10px;color:rgba(235,235,245,0.3);margin-top:3px}

      /* ── Shoe zone ────────────────────────────────────────────────────── */
      .bv-regime-badge{display:inline-block;font-size:10px;font-weight:600;
        padding:3px 10px;border-radius:6px;letter-spacing:0.02em;margin-bottom:10px}

      /* ── Model section ────────────────────────────────────────────────── */
      #bvsb-model{padding-top:4px}

      /* ── Shared primitives ────────────────────────────────────────────── */
      .bv-row{display:flex;justify-content:space-between;align-items:center;
        padding:3px 0;min-width:0}
      .bv-lbl{color:rgba(235,235,245,0.35);font-size:11px;white-space:nowrap;
        overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
      .bv-val{font-family:"JetBrains Mono",monospace;font-size:11px;font-weight:500;
        white-space:nowrap;margin-left:8px;flex-shrink:0;color:rgba(235,235,245,0.85)}
      .bv-divider{border:none;border-top:1px solid rgba(255,255,255,0.06);margin:8px 0}
      .bv-reason{font-size:10px;color:rgba(235,235,245,0.35);
        padding:2px 0 2px 8px;margin-top:4px;
        border-left:2px solid rgba(255,255,255,0.1);
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .bv-chip{display:inline-flex;align-items:center;gap:4px;
        background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.08);
        border-radius:6px;padding:3px 8px;font-size:10px;white-space:nowrap;margin:2px 3px 2px 0}
      .bv-phase{display:inline-block;font-size:10px;font-weight:500;padding:2px 7px;border-radius:5px}
      .bv-bet-tbl{width:100%;border-collapse:collapse;font-size:10px}
      .bv-bet-tbl td{padding:4px 4px}
      .bv-bet-tbl tr:nth-child(even) td{background:rgba(255,255,255,0.03)}

      /* ── Animations ───────────────────────────────────────────────────── */

      /* Radar ring from pick anchor */
      @keyframes bv-ring-out{0%{transform:scale(1);opacity:.5}100%{transform:scale(2.4);opacity:0}}
      .bv-ring{position:absolute;inset:0;border-radius:50%;
        border:1px solid currentColor;
        animation:bv-ring-out .7s ease-out forwards;pointer-events:none}

      /* BET badge breathe */
      @keyframes bv-breathe{0%,100%{opacity:1}50%{opacity:.75}}
      .bv-orb-breathe{animation:bv-breathe 2.5s ease-in-out infinite}

      /* BET first-activate burst (blue radial) */
      @keyframes bv-burst{0%{opacity:.55;transform:scale(1)}100%{opacity:0;transform:scale(2.8)}}
      .bv-burst{position:absolute;inset:-8px;border-radius:50%;
        background:radial-gradient(circle,rgba(10,132,255,0.3) 0%,transparent 70%);
        animation:bv-burst .55s ease-out forwards;pointer-events:none}

      /* Win/lose edge flash */
      @keyframes bv-flash-win{
        0%,100%{box-shadow:0 24px 60px rgba(0,0,0,0.65);transform:none}
        18%{transform:translateY(-2px);box-shadow:0 0 0 2px rgba(48,209,88,0.55),0 24px 60px rgba(0,0,0,0.65)}
        40%{transform:translateY(1.5px)}58%{transform:none}}
      @keyframes bv-flash-lose{
        0%,100%{box-shadow:0 24px 60px rgba(0,0,0,0.65);transform:none}
        18%{transform:translateX(-1.5px);box-shadow:0 0 0 2px rgba(255,69,58,0.5),0 24px 60px rgba(0,0,0,0.65)}
        40%{transform:translateX(1.5px)}58%{transform:none}}
      .bv-flash-win{animation:bv-flash-win .55s ease-out forwards}
      .bv-flash-lose{animation:bv-flash-lose .55s ease-out forwards}

      /* New-shoe scan wipe */
      @keyframes bv-scan-wipe{0%{left:0;opacity:.6}100%{left:100%;opacity:0}}
      .bv-scan-wipe{position:absolute;top:0;bottom:0;width:2px;
        background:linear-gradient(180deg,transparent,rgba(255,255,255,0.6),transparent);
        pointer-events:none;animation:bv-scan-wipe .5s ease-in forwards}

      /* BET badge glow — color driven by --bc1/--bc2 CSS vars set inline on the element */
      @keyframes bv-badge-glow{
        0%,100%{box-shadow:none}
        50%{box-shadow:0 0 10px var(--bc1,rgba(10,132,255,0.45)),0 0 18px var(--bc2,rgba(10,132,255,0.2))}}
      .bv-badge-pulse{animation:bv-badge-glow 1.3s ease-in-out infinite}

      /* Balance delta float */
      @keyframes bv-float{0%{transform:translateY(0);opacity:1}100%{transform:translateY(-28px);opacity:0}}
      .bv-float-delta{position:fixed;font-family:"JetBrains Mono",monospace;
        font-size:13px;font-weight:600;pointer-events:none;
        z-index:2147483648;animation:bv-float 1s ease-out forwards;white-space:nowrap}

      /* Dragon badge shake */
      @keyframes bv-shake{0%,100%{transform:none}
        18%{transform:translateX(-3px)}38%{transform:translateX(3px)}
        55%{transform:translateX(-2px)}72%{transform:translateX(2px)}}
      .bv-shake{animation:bv-shake .42s ease-in-out}

      /* Bead pop-in + streak wave */
      @keyframes bv-bead-pop{0%{transform:scale(0)}60%{transform:scale(1.35)}100%{transform:scale(1)}}
      @keyframes bv-bead-wave{0%,100%{transform:translateY(0)}50%{transform:translateY(-2.5px)}}
      .bv-bead{display:inline-block;width:11px;height:11px;border-radius:50%;
        margin:2px 3px;vertical-align:middle;flex-shrink:0}
      .bv-bead-new{animation:bv-bead-pop .22s ease-out both}
      .bv-bead-wave{animation:bv-bead-wave .65s ease-in-out infinite}

      /* Zone first-data sweep */
      @keyframes bv-sweep{0%{left:-100%;opacity:.5}100%{left:210%;opacity:0}}
      .bv-sweep{position:absolute;top:0;bottom:0;left:-100%;width:55%;
        background:linear-gradient(90deg,transparent,rgba(255,255,255,0.05),transparent);
        pointer-events:none;animation:bv-sweep .5s ease-out forwards}

      /* Chevron bounce */
      @keyframes bv-chev-down{0%,100%{transform:rotate(0)}42%{transform:rotate(90deg) translateX(1px)}100%{transform:rotate(90deg)}}
      @keyframes bv-chev-up{0%{transform:rotate(90deg)}100%{transform:rotate(0)}}
      .bv-chev-open{animation:bv-chev-down .25s ease-out forwards}
      .bv-chev-close{animation:bv-chev-up .25s ease-out forwards}

      /* Bar shimmer — subtle */
      @keyframes bv-glint{0%{left:-80%;opacity:0}20%{opacity:.2}80%{opacity:.15}100%{left:130%;opacity:0}}
      .bv-bar-fill{position:relative;overflow:hidden}
      .bv-bar-fill::after{content:'';position:absolute;top:0;bottom:0;left:-80%;width:40%;
        background:linear-gradient(90deg,transparent,rgba(255,255,255,.3),transparent);
        animation:bv-glint 3s ease-in-out infinite;pointer-events:none}

      /* Border ring — subtle opacity breathe */
      @keyframes bv-ring-hue{0%,100%{border-color:rgba(255,255,255,0.08)}
        50%{border-color:rgba(255,255,255,0.15)}}
      #bv-border-ring{position:absolute;inset:0;border-radius:15px;
        border:1px solid rgba(255,255,255,0.08);pointer-events:none;z-index:0;
        animation:bv-ring-hue 10s ease-in-out infinite;
        transition:border-color 1s ease,box-shadow 1s ease}

      /* Pick zone fade-pulse on BET activate */
      @keyframes bv-pick-pulse{0%{opacity:1}25%{opacity:.7}100%{opacity:1}}
      .bv-pick-pulse{animation:bv-pick-pulse 0.4s ease-out}
    `;
    document.head.appendChild(s);
  }

  function ensurePanel() {
    if (panel) return;
    injectCSS();
    panel = document.createElement("div");
    panel.id = "bv-panel";

    // Subtle animated border ring
    const borderRing = document.createElement("div");
    borderRing.id = "bv-border-ring";
    panel.appendChild(borderRing);

    // ── Header ──────────────────────────────────────────────────────────────
    const hdr = document.createElement("div");
    hdr.id = "bv-hdr";
    hdr.innerHTML =
      `<div id="bv-grip">⠿</div>` +
      `<div id="bv-wordmark-wrap">` +
        `<div id="bv-wordmark">BACCARAT VISION</div>` +
        `<div id="bv-status">starting…</div>` +
      `</div>` +
      `<div id="bv-live-wrap">` +
        `<div id="bv-live-dot"></div>` +
        `<div id="bv-live-label">LIVE</div>` +
      `</div>` +
      `<button id="bv-min" title="Minimise">&#x2013;</button>` +
      `<div id="bv-shoe-bar-track"><div id="bv-shoe-bar-fill"></div></div>`;
    panel.appendChild(hdr);
    statusEl = panel.querySelector("#bv-status");

    // ── Body ────────────────────────────────────────────────────────────────
    const body = document.createElement("div");
    body.id = "bv-body";

    // Collapsible sections — IDs match the #bvsb-{id} lookup used by $()
    const secPick    = makeSection("pick",    "Next Pick",           true);
    const secAlloc   = makeSection("alloc",   "Allocation",          true);
    const secSession = makeSection("session", "Session",             false); // default collapsed
    const secPattern = makeSection("pattern", "Last Hand & Pattern", true);
    const secModel   = makeSection("model",   "AI Model",            false);

    // #bvsb-spread lives inside the alloc section body so spread render + bv-locked still work
    const spreadInner = document.createElement("div");
    spreadInner.id = "bvsb-spread";
    secAlloc.querySelector(".bvsb").appendChild(spreadInner);

    [secPick, secAlloc, secSession, secPattern, secModel].forEach(s => body.appendChild(s));
    panel.appendChild(body);

    // ── Minimise toggle ──────────────────────────────────────────────────────
    panel.querySelector("#bv-min").addEventListener("click", (e) => {
      e.stopPropagation();
      _minimised = !_minimised;
      _minimised ? _secHide(body) : _secShow(body);
      panel.querySelector("#bv-min").innerHTML = _minimised ? "&#x25A1;" : "&#x2013;";
    });

    // ── Drag ────────────────────────────────────────────────────────────────
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

    document.body.appendChild(panel);
  }

  function setStatus(s, shoePct) {
    ensurePanel();
    if (statusEl) statusEl.textContent = s;
    if (shoePct !== undefined) {
      const fill = panel.querySelector("#bv-shoe-bar-fill");
      if (fill) fill.style.width = Math.max(0, Math.min(100, shoePct * 100)).toFixed(1) + "%";
    }
  }

  function $(id) { return panel ? panel.querySelector(`#bvsb-${id}`) : null; }

  // ─── Render helpers ────────────────────────────────────────────────────────

  function renderVoteSummary(vs) {
    if (!vs || !vs.votes || !Object.keys(vs.votes).length) return "";
    const total = Object.values(vs.votes).reduce((a, b) => a + b, 0) || 1;
    const items = Object.entries(vs.votes)
      .sort((a, b) => b[1] - a[1]).slice(0, 3)
      .map(([bet, w]) => {
        const pct = (w / total * 100).toFixed(0);
        const c = BET_COLOR[bet] || "rgba(235,235,245,0.6)";
        return `<span class="bv-chip"><span style="color:${c}">${BET_NAMES[bet] || bet}</span>` +
               `<span style="color:rgba(235,235,245,0.3)"> ${pct}%</span></span>`;
      }).join("");
    const dissent = (vs.top_dissent || []).slice(0, 2).join(", ");
    return `<hr class="bv-divider">` +
      `<div style="font-size:10px;color:rgba(235,235,245,0.3);margin-bottom:3px">Experts · ${(vs.agreement * 100).toFixed(0)}% agree</div>` +
      `<div>${items}</div>` +
      (dissent ? `<div class="bv-reason" style="margin-top:3px">Dissent: ${dissent}</div>` : "");
  }

  function renderCalibration(cal) {
    if (!cal || !cal.length) return "";
    const rows = cal.filter((c) => c.n >= 5);
    if (rows.length < 2) return "";
    const rowHtml = rows.map((c) => {
      const diff = c.actual_rate - c.expected_rate;
      const col = Math.abs(diff) < 0.05 ? "#30d158" : Math.abs(diff) < 0.12 ? "#ffd60a" : "#ff453a";
      return `<div class="bv-row">` +
        `<span class="bv-lbl">${c.bin_label}</span>` +
        `<span class="bv-val" style="color:${col}">${(c.actual_rate * 100).toFixed(0)}%` +
        ` <span style="color:rgba(235,235,245,0.25);font-size:9px">vs ${(c.expected_rate * 100).toFixed(0)}% (n=${c.n})</span></span></div>`;
    }).join("");
    return `<hr class="bv-divider">` +
      `<div style="font-size:10px;color:rgba(235,235,245,0.3);margin-bottom:3px">Calibration</div>` + rowHtml;
  }

  function renderTemplateMatch(tm) {
    if (!tm) return "";
    const col = tm.pick === "B" ? "#ff453a" : tm.pick === "P" ? "#0a84ff" : "#30d158";
    const name = tm.pick === "B" ? "Banker" : tm.pick === "P" ? "Player" : "Tie";
    return `<hr class="bv-divider">` +
      `<div class="bv-row" style="margin-top:2px">` +
      `<span class="bv-lbl">Template match</span>` +
      `<span class="bv-val" style="color:${col}">${name} (${(tm.confidence * 100).toFixed(0)}%)</span></div>` +
      `<div class="bv-reason">From ${tm.n_matches} similar shoes · ` +
      `B${(tm.b_pct * 100).toFixed(0)}% P${(tm.p_pct * 100).toFixed(0)}% T${(tm.t_pct * 100).toFixed(0)}%</div>`;
  }

  // ─── Session-aware bet spread suggestion ──────────────────────────────────
  // vibe: normalized 0-1 model confidence (m.vibe, already normalized by caller)
  function calcSuggestedSpread(bk, st, sess, shoeHistory, vibe) {
    if (!bk || !bk.balance || !st || !st.spread_legs || !st.spread_legs.length) return null;
    const balance = bk.balance;
    const conf = (typeof vibe === "number" && vibe > 0) ? vibe : 0.5;

    // Base: 15% of balance per hand (conservative starting point)
    let pct = 0.15;

    // Model confidence modifier
    if (conf >= 0.70) pct = 0.22;
    else if (conf >= 0.65) pct = 0.19;
    else if (conf < 0.52) pct = 0.10;
    else if (conf < 0.48) pct = 0.07;

    // Session win rate modifier (requires at least 5 tracked hands)
    const totalHands = sess.wins + sess.losses;
    if (totalHands >= 5) {
      const wr = sess.wins / totalHands;
      if (wr >= 0.58)      pct *= 1.15;  // strong winning session
      else if (wr >= 0.53) pct *= 1.07;  // slightly positive
      else if (wr <= 0.40) pct *= 0.70;  // losing badly
      else if (wr <= 0.45) pct *= 0.85;  // slightly below break-even
    }

    // Recent shoe trend (last 2 completed shoes)
    const recent = shoeHistory.slice(-2);
    if (recent.length >= 1) {
      const recentPL = recent.reduce((s, sh) => s + sh.pl, 0);
      if (recentPL < -balance * 0.04)      pct *= 0.80;  // significant drawdown
      else if (recentPL < -balance * 0.01) pct *= 0.90;  // mild loss trend
      else if (recentPL > balance * 0.04)  pct *= 1.10;  // winning trend
    }

    // Clamp to sensible range: 5% – 25% of balance
    pct = Math.max(0.05, Math.min(0.25, pct));

    const suggestedTotal = Math.round(balance * pct);

    // Scale server's spread legs proportionally to our suggested total
    const serverTotal = st.spread_total > 0
      ? st.spread_total
      : st.spread_legs.reduce((s, g) => s + (g.stake || 0), 0);
    const scale = serverTotal > 0 ? suggestedTotal / serverTotal : 1;

    return {
      total: suggestedTotal,
      pct,
      legs: st.spread_legs.map(g => ({
        ...g,
        stake: Math.round((g.stake || 0) * scale),
      })),
    };
  }

  // ─── Render ────────────────────────────────────────────────────────────────
  function render(data, hand, counter) {
    ensurePanel();
    const shoePct = counter.hand / 72;
    const shoePctStr = (shoePct * 100).toFixed(0);
    setStatus(`Hand ${counter.hand}/72 · ${counter.P}P ${counter.B}B ${counter.T}T · ${shoePctStr}%`, shoePct);
    if (_minimised) return;

    // ── New shoe scan wipe ──────────────────────────────────────────────────
    if (_prevHandNum > 5 && counter.hand <= 2) {
      _handHistory = [];
      _sectionInited = {};
      _prevHandWinner = null;
      _lastHandBetFollowed = false;
      scanNewShoe();
    }
    _prevHandNum = counter.hand;

    // ── Append this hand to local bead history ──────────────────────────────
    if (hand && hand.winner) {
      _handHistory.push(hand.winner);
      if (_handHistory.length > 14) _handHistory.shift();
    }
    // Post-verify: bead history must not exceed total hands played in the shoe.
    // If the observer + interval fired concurrently before the _tickRunning guard
    // was fully in place, spurious extra beads can appear — trim from the newest end.
    const _maxBeads = Math.min(counter.hand, 14);
    while (_handHistory.length > _maxBeads) _handHistory.pop();

    if (!data) {
      const el = $("pick");
      if (el) el.innerHTML = `<div style="display:flex;align-items:center;gap:5px;color:#ff9f0a;font-size:11px">${icon("alert-triangle",13,"#ff9f0a")} Engine offline — run the server</div>`;
      return;
    }

    const m  = data.mystic;
    const st = data.staking;
    const L  = data.learning;
    const ds = data.dynamic_spread;
    const bk = data.bankroll;
    const pat = data.pattern;
    const cur = (st && st.currency) || (bk && bk.currency) || "";

    // ── Win / lose edge flash (fallback — only runs when balance delta undetectable) //
    if (hand && session.betSignalledPick) {
      const winnerMap = { player: "P", banker: "B", tie: "T" };
      const isWin = hand.winner === winnerMap[session.betSignalledPick];
      edgeFlash(isWin ? "win" : "lose");
      _lastHandBetFollowed = true;
      if (isWin) session.wins++; else session.losses++;
    }

    // ── Regime border ───────────────────────────────────────────────────────
    const _regimeColors = { Dragon:"#ff453a", Choppy:"#30d158", Mixed:"#ffd60a", Forming:"#bf5af2" };
    const _regimeCol = pat && _regimeColors[pat.personality];
    const _bRing = panel && panel.querySelector("#bv-border-ring");
    if (_bRing) {
      if (_regimeCol) {
        _bRing.style.borderColor = _regimeCol + "55";
        _bRing.style.boxShadow   = `0 0 16px ${_regimeCol}20`;
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

      const col = m.confident ? (BET_COLOR[m.pick] || "rgba(235,235,245,0.85)") : "rgba(235,235,245,0.22)";
      // Normalize vibe: API sometimes returns 0-100 scale instead of 0-1
      const vibe = (() => { const v = m.vibe || 0; return v > 1 ? v / 100 : v; })();
      const pickName = (m.pick_label || m.pick).toUpperCase();
      const badge = m.confident
        ? `<span class="bv-badge bv-badge-bet bv-badge-pulse" style="color:${col};background:${col}1a;border-color:${col}40;--bc1:${col}73;--bc2:${col}33">${icon("diamond",9,col)} BET</span>`
        : `<span class="bv-badge bv-badge-opt">WAIT</span>`;
      const stakeStr = (st && st.stake) ? `${fmtK(st.stake)}${cur ? " " + cur : ""}` : "";

      let unlockHtml = "";
      if (L) {
        if (m.confident) {
          const proven = L.significant ? "proven edge" : "net +edge";
          unlockHtml = `<div style="display:flex;align-items:center;gap:4px;color:#30d158;` +
            `font-size:11px;font-weight:500;margin-top:6px">` +
            `${icon("check",11,"#30d158")} ${proven} · ${L.acts} hands</div>`;
        } else {
          const have = L.acts || 0, need = L.min_hands || 15;
          const pph = (L.profit_per_hand || 0) * 100;
          const why = have < need ? `${have}/${need} hands` : pph <= 0 ? `P/H ${pph.toFixed(1)}` : "building confidence";
          unlockHtml = `<div style="display:flex;align-items:center;gap:4px;` +
            `color:rgba(235,235,245,0.4);font-size:11px;margin-top:6px">` +
            `${icon("lock",11,"rgba(235,235,245,0.3)")} BET unlocks: ${why}</div>`;
        }
      }

      const topReason = (m.reasons || [])[0];
      const reasonHtml = topReason
        ? `<div style="font-size:11px;color:rgba(235,235,245,0.5);padding:3px 0 3px 10px;` +
          `margin-top:4px;border-left:2px solid ${col}50;overflow:hidden;text-overflow:ellipsis;` +
          `white-space:nowrap">${topReason.length > 58 ? topReason.slice(0, 58) + "…" : topReason}</div>`
        : "";

      pickEl.innerHTML =
        `<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px">` +
        `<div style="flex:1;min-width:0">` +
        `<div id="bv-pick-name" style="color:${col}">${pickName}</div>` +
        `<div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:6px">` +
        badge +
        `<span id="bv-confidence-num" style="color:${col};background:${col}1a;padding:2px 8px;` +
        `border-radius:6px;font-size:11px;font-weight:600;letter-spacing:0.02em">` +
        `${(vibe * 100).toFixed(0)}%</span>` +
        `</div>` +
        unlockHtml + reasonHtml +
        `</div>` +
        `<div style="flex-shrink:0;text-align:right">` +
        (stakeStr ? `<div id="bv-pick-stake">${stakeStr}</div>` : "") +
        `<div id="bv-pick-ring" style="position:relative;width:36px;height:36px;margin:${stakeStr ? "6px" : "0"} 0 0 auto;border-radius:50%"></div>` +
        `</div>` +
        `</div>` +
        `<div style="margin-top:9px">${bar(vibe, col, 2)}</div>`;

      // Pick ring — color kept current; animation fires contextually below
      const ringEl = pickEl.querySelector("#bv-pick-ring");
      if (ringEl) ringEl.dataset.col = col;

      // BET first-activate: burst (pick color, not blue) + pick-name fade pulse
      if (m.confident && !_prevConfident) {
        if (ringEl) {
          const b = document.createElement("div");
          b.className = "bv-burst";
          b.style.background = `radial-gradient(circle,${col}4d 0%,transparent 70%)`;
          ringEl.appendChild(b);
          setTimeout(() => b.remove(), 650);
        }
        const nameEl = pickEl.querySelector("#bv-pick-name");
        if (nameEl) {
          nameEl.classList.remove("bv-pick-pulse");
          void nameEl.offsetWidth;
          nameEl.classList.add("bv-pick-pulse");
          setTimeout(() => nameEl.classList.remove("bv-pick-pulse"), 450);
        }
      }
      _prevConfident = m.confident;

      // Section header: show pick name + state; colored left border when BET active
      const pickSecVal = panel && panel.querySelector("#bvsecv-pick");
      if (pickSecVal) {
        pickSecVal.textContent = m.confident ? pickName : "waiting…";
        pickSecVal.style.color = m.confident ? col : "rgba(235,235,245,0.2)";
      }
      const pickSection = pickEl && pickEl.closest(".bv-section");
      if (pickSection) {
        pickSection.style.boxShadow = m.confident
          ? `inset 3px 0 0 ${col}, inset 0 0 0 0 transparent`
          : "";
        pickSection.style.transition = "box-shadow 0.4s ease";
      }
    }

    // ── Track last hand winner (for future state tracking) ──────────────── //
    if (hand && hand.winner && hand.winner !== _prevHandWinner) {
      _lastHandBetFollowed = false;
      _prevHandWinner = hand.winner;
    }

    // ── Bet Spread ───────────────────────────────────────────────────────── //
    const spreadEl = $("spread");
    if (spreadEl) {
      // Toggle locked appearance when no BET signal
      spreadEl.classList.toggle("bv-locked", !(m && m.confident));
      if (!_sectionInited.spread && ds && ds.legs && ds.legs.length) { sweepSection("spread"); _sectionInited.spread = true; }
      let html = "";

      // Dynamic spread (probability-driven, has EV)
      if (ds && ds.legs && ds.legs.length) {
        const phaseColor = { early: "rgba(235,235,245,0.3)", mid: "#ffd60a", late: "#30d158" }[ds.phase] || "rgba(235,235,245,0.3)";
        const phaseDot  = { early: "○", mid: "◑", late: "●" }[ds.phase] || "○";
        const phaseLabel = { early: "Early shoe", mid: "Mid shoe", late: "Late shoe" }[ds.phase] || ds.phase;
        const multCol  = ds.multiplier > 3 ? "#30d158" : ds.multiplier > 1.5 ? "#ffd60a" : "rgba(235,235,245,0.3)";
        const multNote = ds.multiplier <= 1 ? "hold" : ds.multiplier >= 4 ? "push" : "raise";

        html += `<div class="bv-row" style="margin-bottom:7px">` +
          `<span class="bv-phase" style="background:${phaseColor}20;color:${phaseColor};border:1px solid ${phaseColor}40">` +
          `${phaseDot} ${phaseLabel}</span>` +
          `<span style="font-family:'JetBrains Mono',monospace;font-size:11px;color:${multCol};font-weight:600">${ds.multiplier}× — ${multNote}</span>` +
          `</div>`;

        // Signal bars
        const sigs = [
          ["Position", ds.composition_signal, "#0a84ff"],
          ["AI Model", ds.learner_signal,     "#30d158"],
          ["Streak",   ds.pattern_signal,     "#ffd60a"],
          ["Combined", ds.signal,             "#bf5af2"],
        ];
        html += sigs.map(([lbl, v, c]) =>
          `<div class="bv-row"><span class="bv-lbl" style="min-width:52px">${lbl}</span>` +
          `<div style="flex:1;margin:0 6px">${bar(v, c, 3)}</div>` +
          `<span class="bv-val" style="color:${c};min-width:28px;text-align:right">${(v * 100).toFixed(0)}%</span></div>`
        ).join("");

        html += `<div style="margin-top:10px;padding-top:8px">`;
        ds.legs.forEach((leg) => {
          const lc = BET_COLOR[leg.bet] || "rgba(235,235,245,0.4)";
          // Only show EV label when it's meaningfully positive or unusually negative
          const showEv = leg.ev > 0 || (leg.ev / (leg.stake || 1)) < -0.05;
          const evCol = leg.ev >= 0 ? "#30d158" : "#ff453a";
          const evTxt = showEv ? `EV ${leg.ev >= 0 ? "+" : ""}${fmtK(leg.ev)}` : "";
          html +=
            `<div class="bv-leg-row" style="border-left-color:${lc}">` +
            `<span class="bv-leg-name">${betDot(leg.bet)}${leg.label}</span>` +
            `<span class="bv-leg-amt">${fmtK(leg.stake)}${cur ? " " + cur : ""}</span>` +
            (evTxt ? `<span class="bv-leg-ev" style="color:${evCol}">${evTxt}</span>` : "") +
            `</div>`;
        });
        const totalEvCol = ds.total_ev >= 0 ? "#30d158" : "#ff453a";
        html += `</div>` +
          `<div class="bv-row" style="margin-top:6px">` +
          `<span class="bv-lbl">Total at risk</span>` +
          `<span class="bv-val" style="color:rgba(235,235,245,0.85)">${fmtK(ds.total_stake)} ${cur}</span></div>` +
          `<div class="bv-row">` +
          `<span class="bv-lbl">Total EV</span>` +
          `<span class="bv-val" style="color:${totalEvCol}">${ds.total_ev >= 0 ? "+" : ""}${fmtK(ds.total_ev)} ${cur}</span></div>`;

        // Kelly gauge
        if (ds.kelly_stake > 0) {
          const kellyPct = Math.min(1, ds.kelly_fraction || 0);
          const kellyCol = kellyPct > 0.15 ? "#ff453a" : kellyPct > 0.08 ? "#ffd60a" : "#30d158";
          html += `<div id="bv-kelly-track"><div id="bv-kelly-fill" style="width:${(kellyPct * 100).toFixed(1)}%;background:${kellyCol}"></div></div>` +
            `<div class="bv-row" style="margin-top:1px">` +
            `<span class="bv-lbl">Kelly</span>` +
            `<span class="bv-val" style="color:#bf5af2">${fmtK(ds.kelly_stake)}${cur ? " " + cur : ""} · ${(ds.kelly_fraction * 100).toFixed(2)}%</span></div>`;
        }
        // Pair probability
        if (ds.pair_probs && ds.pair_probs.either_pair != null) {
          const ep = ds.pair_probs.either_pair;
          const bep = ds.pair_probs.baseline_either_pair || 0.001;
          const ratio = ep / bep;
          const pairCol = ratio >= 1.08 ? "#ffd60a" : "rgba(235,235,245,0.3)";
          html += `<div class="bv-row" style="margin-top:2px">` +
            `<span class="bv-lbl" style="color:${pairCol}">Pair probability</span>` +
            `<span class="bv-val" style="color:${pairCol}">${(ep * 100).toFixed(1)}% ` +
            `<span style="color:rgba(235,235,245,0.25);font-size:9px">(${ratio >= 1 ? "+" : ""}${((ratio - 1) * 100).toFixed(0)}% vs base)</span></span></div>`;
        }
        if (!ds.affordable) {
          html += `<div style="display:flex;align-items:center;gap:4px;color:#ff9f0a;font-size:10px;margin-top:3px">${icon("alert-triangle",11,"#ff9f0a")} scaled to fit balance</div>`;
        }
      }

      // Pattern side-bet signals
      if (st && st.side_bets && st.side_bets.length) {
        html += `<div style="margin-top:8px;font-size:10px;color:rgba(235,235,245,0.3);text-transform:uppercase;letter-spacing:.5px">Pattern signals</div>`;
        st.side_bets.forEach((sb) => {
          const lc = BET_COLOR[sb.bet] || "rgba(235,235,245,0.4)";
          html += `<div class="bv-row">` +
            `<span class="bv-leg-name" style="color:${lc}">+ ${sb.label}</span>` +
            `<span class="bv-leg-amt">${fmtK(sb.stake)}${cur ? " " + cur : ""}</span></div>`;
        });
      }

      // Actual placed bets (read from DOM) — preferred over server preset
      const actualBets = readPlacedBets();
      if (actualBets && Object.keys(actualBets).length) {
        const actualTotal = Object.values(actualBets).reduce((s, v) => s + v, 0);
        const warn = bk && bk.balance && actualTotal > bk.balance
          ? ` <span style="display:inline-flex;align-items:center;gap:3px;color:#ff9f0a">${icon("alert-triangle",11,"#ff9f0a")} over balance</span>`
          : "";
        html += `<div style="margin-top:6px;padding-top:5px">` +
          `<div style="font-size:10px;color:rgba(235,235,245,0.3);text-transform:uppercase;` +
          `letter-spacing:.5px;margin-bottom:4px">Placed · ${fmtK(actualTotal)} ${cur}${warn}</div>` +
          `<div style="display:flex;flex-wrap:wrap">` +
          Object.entries(actualBets).map(([bet, amt]) => {
            const lc = BET_COLOR[bet] || "rgba(235,235,245,0.4)";
            const label = BET_NAMES[bet] || bet;
            return `<span class="bv-chip"><span style="color:${lc}">${label}</span>` +
              `<span style="color:rgba(235,235,245,0.25)"> ${fmtK(amt)}</span></span>`;
          }).join("") + `</div></div>`;
      } else if (st && st.spread_legs && st.spread_legs.length) {
        // Fallback: session-aware suggestion (DOM bet reading not yet available)
        const normVibe = m && m.vibe ? (m.vibe > 1 ? m.vibe / 100 : m.vibe) : 0.5;
        const sugg = calcSuggestedSpread(bk, st, session, _shoeHistory, normVibe);
        if (sugg) {
          const pctLabel = (sugg.pct * 100).toFixed(0) + "%";
          html += `<div style="margin-top:6px;padding-top:5px">` +
            `<div style="font-size:10px;color:rgba(235,235,245,0.3);text-transform:uppercase;` +
            `letter-spacing:.5px;margin-bottom:4px">Suggested · ${fmtK(sugg.total)} ${cur}` +
            `<span style="font-size:9px;color:rgba(235,235,245,0.18);margin-left:5px">${pctLabel} of balance</span></div>` +
            `<div style="display:flex;flex-wrap:wrap">` +
            sugg.legs.map((g) => {
              const lc = BET_COLOR[g.bet] || "rgba(235,235,245,0.4)";
              const amt = g.stake ? fmtK(g.stake) : "—";
              return `<span class="bv-chip"><span style="color:${lc}">${g.label}</span>` +
                `<span style="color:rgba(235,235,245,0.25)"> ${amt}</span></span>`;
            }).join("") + `</div></div>`;
        }
      }

      if (!html) html = `<div style="color:rgba(235,235,245,0.25);font-size:10px">No spread data yet</div>`;
      spreadEl.innerHTML = html;
      // Update allocation section header total
      const allocTotal = panel && panel.querySelector("#bvsecv-alloc");
      if (allocTotal) allocTotal.textContent = (ds && ds.total_stake) ? `${fmtK(ds.total_stake)} ${cur}` : "";
    }

    // ── Session + Balance strip ───────────────────────────────────────────── //
    const sessEl = $("session");
    if (sessEl) {
      if (!_sectionInited.session && session.followedHands > 0) { sweepSection("session"); _sectionInited.session = true; }
      const followed = session.followedHands;
      const pl = session.followedPL;
      const plCol = pl >= 0 ? "#30d158" : "#ff453a";

      // Balance + delta
      let balNum = "—", balMeta = "";
      if (bk && bk.balance) {
        // Floating delta on balance change
        if (_prevBalanceAnim !== null && bk.balance !== _prevBalanceAnim) {
          const diff = bk.balance - _prevBalanceAnim;
          const sign = diff >= 0 ? "+" : "-";
          floatDelta(`${sign}${fmtK(Math.abs(diff))}`, diff >= 0 ? "#30d158" : "#ff453a", sessEl);
        }
        _prevBalanceAnim = bk.balance;
        balNum = `${fmtK(bk.balance)}${cur ? " " + cur : ""}`;
        if (bk.shoe_start != null) {
          const delta = bk.balance - bk.shoe_start;
          const dc = delta > 0 ? "rgba(48,209,88,0.7)" : delta < 0 ? "rgba(255,69,58,0.55)" : "rgba(235,235,245,0.2)";
          balMeta = delta !== 0
            ? `<span style="color:${dc};font-size:11px">${delta >= 0 ? "▲" : "▼"} ${fmtK(Math.abs(delta))} shoe</span>`
            : "";
        }
      }

      // Totals across all shoes this session (including archived shoes)
      const allWins   = _shoeHistory.reduce((s, sh) => s + sh.wins, 0)   + session.wins;
      const allLosses = _shoeHistory.reduce((s, sh) => s + sh.losses, 0) + session.losses;
      const allPL     = _shoeHistory.reduce((s, sh) => s + sh.pl, 0)     + session.followedPL;
      const allHands  = _shoeHistory.reduce((s, sh) => s + sh.hands, 0)  + session.followedHands;
      const allPLCol  = allPL >= 0 ? "#30d158" : "#ff453a";

      const plDisplay = allHands > 0 && allPL !== 0
        ? `<span style="color:${allPLCol}">${allPL > 0 ? "+" : ""}${fmtK(allPL)}${cur ? " " + cur : ""}</span>`
        : `<span style="color:rgba(235,235,245,0.2)">—</span>`;

      const wHtml = `<span style="color:#30d158;font-weight:700">${allWins}W</span>`;
      const lHtml = `<span style="color:#ff453a;font-weight:700">${allLosses}L</span>`;

      // Shoe log: archived shoes + current shoe row
      const hasShoeData = _shoeHistory.length > 0 || session.wins > 0 || session.losses > 0 || session.followedHands > 0;
      let shoeLogHtml = "";
      if (hasShoeData) {
        const shoeRows = _shoeHistory.slice(-4).map((sh) => {
          const plC = sh.pl !== 0 ? (sh.pl > 0 ? "#30d158" : "#ff453a") : "rgba(235,235,245,0.25)";
          const plS = sh.pl !== 0 ? `${sh.pl > 0 ? "+" : ""}${fmtK(sh.pl)}${cur ? " " + cur : ""}` : "—";
          return `<div style="display:flex;justify-content:space-between;font-size:10px;padding:1px 0">` +
            `<span style="color:rgba(235,235,245,0.25)">Shoe ${sh.shoe + 1}</span>` +
            `<span style="font-family:'JetBrains Mono',monospace;color:rgba(235,235,245,0.35)">${sh.wins}W&nbsp;${sh.losses}L</span>` +
            `<span style="font-family:'JetBrains Mono',monospace;color:${plC}">${plS}</span>` +
            `</div>`;
        });
        // Current shoe row (highlighted)
        const curPlC = session.followedPL !== 0 ? (session.followedPL > 0 ? "#30d158" : "#ff453a") : "rgba(235,235,245,0.25)";
        const curPlS = session.followedPL !== 0 ? `${session.followedPL > 0 ? "+" : ""}${fmtK(session.followedPL)}${cur ? " " + cur : ""}` : "—";
        shoeRows.push(
          `<div style="display:flex;justify-content:space-between;font-size:10px;padding:1px 0">` +
          `<span style="color:rgba(235,235,245,0.55);font-weight:600">Shoe ${_shoeNum + 1}</span>` +
          `<span style="font-family:'JetBrains Mono',monospace;color:rgba(235,235,245,0.55)">${session.wins}W&nbsp;${session.losses}L</span>` +
          `<span style="font-family:'JetBrains Mono',monospace;color:${curPlC}">${curPlS}</span>` +
          `</div>`
        );
        shoeLogHtml = `<div style="padding:6px 16px 10px">` +
          `<div style="font-size:9px;font-weight:600;letter-spacing:0.08em;color:rgba(235,235,245,0.2);text-transform:uppercase;margin-bottom:5px">Shoes</div>` +
          shoeRows.join("") + `</div>`;
      }

      sessEl.innerHTML =
        `<div class="bv-session-grid">` +
          `<div class="bv-session-cell">` +
            `<div class="bv-micro-lbl">Balance</div>` +
            `<div id="bv-balance-num">${balNum}</div>` +
            `<div id="bv-balance-meta">${balMeta || "&nbsp;"}</div>` +
          `</div>` +
          `<div class="bv-session-cell">` +
            `<div class="bv-micro-lbl">Session P/L</div>` +
            `<div id="bv-session-pl">${plDisplay}</div>` +
            `<div id="bv-session-meta">${wHtml} <span style="color:rgba(235,235,245,0.25)">·</span> ${lHtml} <span style="color:rgba(235,235,245,0.25)">·</span> ${allHands} sig</div>` +
          `</div>` +
        `</div>` +
        shoeLogHtml;

      // Section header: show balance for quick glance when collapsed
      const sessSecVal = panel && panel.querySelector("#bvsecv-session");
      if (sessSecVal) {
        sessSecVal.textContent = bk && bk.balance ? fmtK(bk.balance) + (cur ? " " + cur : "") : "";
      }
    }

    // ── Pattern / Shoe + Last Hand cards ─────────────────────────────────── //
    const patEl = $("pattern");
    if (patEl) {
      if (!_sectionInited.pattern && (pat || (hand && hand.detail))) {
        sweepSection("pattern"); _sectionInited.pattern = true;
      }

      // ── Last hand card display (always visible) ──────────────────────────
      let lastCardHtml = "";
      if (hand && hand.detail) {
        const pc = hand.detail.player.map(c => cardHtml(c)).join("");
        const bc = hand.detail.banker.map(c => cardHtml(c)).join("");
        const wname = { P: "Player", B: "Banker", T: "Tie" }[hand.winner] || hand.winner;
        const wcol = BET_COLOR[hand.winner === "P" ? "player" : hand.winner === "B" ? "banker" : "tie"] || "rgba(235,235,245,0.4)";
        const extras = [
          hand.is_natural ? "Natural" : null,
          hand.p_pair    ? "P Pair"  : null,
          hand.b_pair    ? "B Pair"  : null,
        ].filter(Boolean).join(" · ");
        lastCardHtml =
          `<div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);` +
          `border-top:2px solid ${wcol}50;border-radius:10px;padding:10px 12px;margin-bottom:10px">` +
          `<div class="bv-row" style="margin-bottom:5px">` +
          `<span class="bv-lbl" style="min-width:62px">Player <span style="font-family:'JetBrains Mono',monospace;color:rgba(235,235,245,0.7)">${hand.player_total}</span></span>` +
          `<span style="display:flex;gap:3px;flex-wrap:wrap;justify-content:flex-end">${pc}</span></div>` +
          `<div class="bv-row" style="margin-bottom:8px">` +
          `<span class="bv-lbl" style="min-width:62px">Banker <span style="font-family:'JetBrains Mono',monospace;color:rgba(235,235,245,0.7)">${hand.banker_total}</span></span>` +
          `<span style="display:flex;gap:3px;flex-wrap:wrap;justify-content:flex-end">${bc}</span></div>` +
          `<div>` +
          `<span style="font-size:12px;font-weight:700;color:${wcol}">${wname} wins</span>` +
          (extras ? `<div style="font-size:10px;color:rgba(235,235,245,0.3);margin-top:3px">${extras}</div>` : "") +
          `</div></div>`;
      }

      // ── Pattern / shoe data ──────────────────────────────────────────────
      let patHtml = "";
      if (pat) {
        const since = pat.hands_since || {};
        const persColor = { Dragon:"#ff453a", Choppy:"#30d158", Mixed:"#ffd60a", Forming:"#bf5af2" }[pat.personality] || "rgba(235,235,245,0.3)";
        const chopPct = (pat.chop_score * 100).toFixed(0);
        const chopCol = pat.chop_score === 0
          ? "rgba(235,235,245,0.2)"
          : pat.chop_score > 0.6 ? "#30d158"
          : pat.chop_score > 0.35 ? "#ffd60a"
          : "#ff453a";
        const dragonBadge = pat.is_dragon
          ? ` <span class="bv-dragon-badge" style="font-size:9px;font-weight:700;color:#ff453a;letter-spacing:.04em">DRAGON</span>`
          : "";
        const streakTxt = pat.streak_len > 1
          ? `${pat.streak_len}× ${pat.streak_side}${dragonBadge}`
          : "—";

        const beadCols = { P:"#0a84ff", B:"#ff453a", T:"#30d158" };
        const beadHtml = _handHistory.length
          ? `<div style="display:flex;flex-wrap:wrap;align-items:center;margin-top:8px">` +
            _handHistory.map((w, i) => {
              const isNew = hand && i === _handHistory.length - 1;
              const inStreak = pat.streak_len > 1 && i >= _handHistory.length - pat.streak_len && !isNew;
              const cls = `bv-bead${isNew ? " bv-bead-new" : inStreak ? " bv-bead-wave" : ""}`;
              const wdly = inStreak ? `animation-delay:${(i % pat.streak_len) * 0.11}s;` : "";
              const bc2  = beadCols[w] || "rgba(235,235,245,0.3)";
              return `<span class="${cls}" style="background:${bc2};${wdly}" title="${w}"></span>`;
            }).join("") + `</div>`
          : "";

        const regimeBadgeBg = { Dragon:"rgba(255,69,58,0.12)", Choppy:"rgba(48,209,88,0.1)", Mixed:"rgba(255,214,10,0.1)", Forming:"rgba(191,90,242,0.1)" }[pat.personality] || "rgba(255,255,255,0.06)";
        const regimeBadgeBorder = { Dragon:"rgba(255,69,58,0.3)", Choppy:"rgba(48,209,88,0.25)", Mixed:"rgba(255,214,10,0.25)", Forming:"rgba(191,90,242,0.25)" }[pat.personality] || "rgba(255,255,255,0.08)";

        patHtml =
          `<div class="bv-regime-badge" style="background:${regimeBadgeBg};color:${persColor};border:1px solid ${regimeBadgeBorder}">` +
          `${pat.personality}</div>` +
          beadHtml +
          `<div class="bv-row" style="margin-top:6px">` +
          `<span class="bv-lbl">Streak</span>` +
          `<span class="bv-val">${streakTxt}</span></div>` +
          `<div class="bv-row">` +
          `<span class="bv-lbl">Chop score</span>` +
          `<span class="bv-val" style="color:${chopCol}">${chopPct}%</span></div>` +
          bar(pat.chop_score, chopCol, 3) +
          `<div class="bv-row" style="margin-top:4px">` +
          `<span class="bv-lbl">Last T / P / B</span>` +
          `<span class="bv-val">${since.T ?? "?"}h · ${since.P ?? "?"}h · ${since.B ?? "?"}h ago</span></div>`;

        if (pat.is_dragon && !_prevDragon) {
          setTimeout(() => {
            const badge = patEl.querySelector(".bv-dragon-badge");
            if (badge) {
              badge.classList.remove("bv-shake"); void badge.offsetWidth;
              badge.classList.add("bv-shake");
              setTimeout(() => badge && badge.classList.remove("bv-shake"), 500);
            }
          }, 0);
        }
        _prevDragon = !!pat.is_dragon;
      }

      patEl.innerHTML = lastCardHtml + patHtml;

      // Section header: show regime + streak when collapsed
      const patSecVal = panel && panel.querySelector("#bvsecv-pattern");
      if (patSecVal && pat) {
        const persColor2 = { Dragon:"#ff453a", Choppy:"#30d158", Mixed:"#ffd60a", Forming:"#bf5af2" }[pat.personality] || "rgba(235,235,245,0.3)";
        const streakSuffix = pat.streak_len > 1 ? ` · ${pat.streak_len}× ${pat.streak_side}` : "";
        patSecVal.textContent = (pat.personality || "") + streakSuffix;
        patSecVal.style.color = persColor2;
      }
    }

    // ── AI Model (collapsible) ───────────────────────────────────────────── //
    const modelEl = $("model");
    const modelVerdict = panel && panel.querySelector("#bvsecv-model");
    if (modelEl) {
      if (!_sectionInited.model && L && L.graded > 0) { sweepSection("model"); _sectionInited.model = true; }
      if (L && L.graded > 0) {
        const edge = (L.accuracy - L.baseline_accuracy) * 100;
        const edgeCol = edge >= 0 ? "#30d158" : "#ff453a";
        const accPct = L.accuracy || 0;
        const pl = L.profit || 0;
        const plCol = pl >= 0 ? "#30d158" : "#ff453a";
        const vcol = L.significant ? "#30d158" : L.actionable ? "#ffd60a" : "rgba(235,235,245,0.3)";
        const shoes = (data.library && data.library.shoes) || 0;

        if (modelVerdict) modelVerdict.textContent = L.verdict || "";

        modelEl.innerHTML =
          `<div class="bv-row">` +
          `<span class="bv-lbl">Accuracy</span>` +
          `<span class="bv-val">${(accPct * 100).toFixed(1)}%` +
          ` <span style="color:${edgeCol};font-size:10px">(${edge >= 0 ? "+" : ""}${edge.toFixed(1)} vs base)</span></span></div>` +
          bar(accPct, edgeCol, 3) +
          `<div class="bv-row">` +
          `<span class="bv-lbl">P/H · recent</span>` +
          `<span class="bv-val">${pnl(L.profit_per_hand * 100, 1)}/100 · ${(L.recent_accuracy * 100).toFixed(0)}%</span></div>` +
          `<div class="bv-row">` +
          `<span class="bv-lbl">Model P/L</span>` +
          `<span class="bv-val" style="color:${plCol}">${pl >= 0 ? "+" : ""}${pl.toFixed(1)}u</span></div>` +
          `<div class="bv-row">` +
          `<span class="bv-lbl">Top expert</span>` +
          `<span class="bv-val" style="color:#bf5af2;overflow:hidden;text-overflow:ellipsis;max-width:130px">${L.best_expert}</span></div>` +
          `<div style="margin-top:3px;font-size:10px;color:${vcol};overflow:hidden;` +
          `text-overflow:ellipsis;white-space:nowrap">${L.verdict}</div>` +
          renderVoteSummary(data.vote_summary) +
          renderCalibration(data.calibration) +
          renderTemplateMatch(data.template_match) +
          // Bet stats table
          (L.bets ? (() => {
            const richHands = (data.vision && data.vision.length) ? data.vision[0].n : 0;
            return `<hr class="bv-divider">` +
              `<div style="color:rgba(235,235,245,0.25);font-size:10px;margin-bottom:4px">${richHands} card-hands · ${shoes} shoes</div>` +
              `<table class="bv-bet-tbl"><thead><tr>` +
              `<td style="color:rgba(235,235,245,0.3)">Bet</td><td style="color:rgba(235,235,245,0.3)">Hit%</td>` +
              `<td style="color:rgba(235,235,245,0.3)">Hands</td><td style="color:rgba(235,235,245,0.3)">/100</td></tr></thead><tbody>` +
              L.bets.map((r) => {
                const star = r.significant ? `<span style="display:inline-flex;vertical-align:middle;margin-left:3px">${icon("check",9,"#30d158")}</span>` : "";
                const pc = (r.hit * 100).toFixed(0);
                return `<tr><td style="color:${BET_COLOR[r.bet] || "rgba(235,235,245,0.4)"}">${BET_NAMES[r.bet] || r.bet}${star}</td>` +
                  `<td>${pc}%</td><td>${r.n}</td><td>${pnl(r.per100)}</td></tr>`;
              }).join("") + `</tbody></table>`;
          })() : "");
      } else {
        if (modelVerdict) modelVerdict.textContent = "collecting data…";
        modelEl.innerHTML = `<div style="display:flex;align-items:center;gap:6px;color:rgba(235,235,245,0.3);font-size:11px">${icon("refresh-cw",13,"rgba(235,235,245,0.25)")} Grading every hand — check back soon</div>`;
      }
    }
  }

  // ----- boot ---------------------------------------------------------------
  log("Baccarat Vision content script loaded in", location.href);
  _loadSession(); // restore session stats from prior page load / refresh
  // Load persisted section open/close state before first render so sections
  // start in the user's last-chosen state rather than always defaulting open.
  _loadSectionState(() => {
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
  });
})();
