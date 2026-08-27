# John's Thinking Log - situational-awareness & discretionary reasoning

Our system has a mechanical gate (pos/RSI) + a deterministic context layer (`cpt_context.py`: regime,
sector, earnings, seasonality). This log captures John's QUALITATIVE reasoning from his real emails -
the discretionary "why THIS name, why NOW" judgment our gate does not model - so we keep learning his
situational-awareness thought process and can decide (advise-only) what to fold into cpt_context / the
doctrine. Adam appends here each week from John's new emails. Relates to `cpt_context.py` + the doctrine.

---

## 2026-08-14 - first pass (2 reasoning-rich emails)

### GGLL / Google entry (2026-08-12, "GOOGLE LOOKS CHEAP") - his ENTRY thought process
John's actual words, distilled:
- **He classifies the pullback: profit-taking vs broken story.** "Google rocketed $318->$380 on
  explosive earnings, then a key AI exec left and Wall Street used it as a 'take profits' moment... the
  pullback looks more like profit-taking after a major run than a fundamental change in the Google
  story." -> THE core discretionary edge our gate lacks: a low-in-channel name is only a buy if the dip
  is a *healthy pullback*, not a *broken fundamental*. Our gate sees "pos low = buy"; John also asks WHY.
- **He uses the KC MID as the value/trend line.** "Google is now trading essentially below its trend
  line (KC MID), I believe there is value at these levels." -> CONFIRMS our engine (entry at/around the
  EMA-10 mid) straight from his mouth.
- **Analogy-based judgment across names he knows.** "It reminds me of how we approach Apple when it
  trades near the bottom of its established trading range." -> he pattern-matches to a known name's range.
- **Vehicle vs thesis:** thesis on GOOGL, trade via GGLL (2x ETF). Enters "the lower end of its range"
  where "risk/reward becomes attractive."

### IREN close-out (2026-08-13, "OUT... $5,320 CC PROFITS") - his MANAGEMENT / macro thought process
- **He maps each name to its macro + sector catalysts.** IREN "got a double whammy: BTC dropped to
  ~$59,000, and the AI purge hammered the other side of IREN's business." -> per-name driver awareness
  (crypto price for IREN, AI-sector rotation). This is the kind of signal cpt_context's sector layer
  gestures at; John does it per-name with the actual catalyst.
- **He reads the BROAD-MARKET regime as backdrop.** "with the market bouncing back from an ~11% Nasdaq
  selloff, IREN finally started to move again." -> VALIDATES cpt_context's regime layer (RISK-ON/OFF) as
  real John thinking, not our invention.
- **Catalyst-aware recovery:** he waited for "Neocloud earnings exploded and the Microsoft deal" to
  rally IREN back to his cost basis before exiting. -> underwater positions are worked back TO a catalyst.
- **Leg-in / cost-basis reduction:** "I doubled down to reduce my cost from $54 to $50." (the scale-in.)
- **Exit discipline:** "my 99-delta longs needed $30.60 to break even; I put a LIMIT at $30.60, it HIT,
  I'm OUT." -> the sell-at-the-exact-number limit-order discipline (matches doctrine's "sell the mid").
- **His thesis restated:** the ITM CC gave downside + a lower cost basis, so he exits breakeven+premium
  while the buy-and-hold investor is still stuck needing $61. -> the CPT worldview in one paragraph.

## 2026-08-15 - weekly pass (1 new reasoning email; no new trade entries this week)

### 99-Delta roll mechanics (2026-08-08, "MEMBERS QUESTION - HOW ARE YOU HANDLING YOUR 99 DELTA TRADES ON EXPIRATION?") - his MANAGEMENT discipline, mechanical this time
Answering a member question, John lays out his roll procedure explicitly:
- **Why he must act:** if the 99-delta long call is left to expire ITM without closing, the market
  maker assigns him the shares - so near-expiration he is forced to choose between assignment and
  rolling. He treats this as routine, not an emergency.
- **The roll steps:** (1) buy back the near-term covered call, (2) close/sell the current 99-delta long
  call (often at a realized loss - he says that's fine, it "carries forward" into the campaign P&L,
  bookkeeping-wise), (3) buy the next 99-delta long call ~2-3 weeks out, (4) resume selling the weekly
  covered call against the new long call.
- **A structure tweak worth noting for our sizing/timing logic:** "lately I've been buying the 99 Delta
  about two weeks beyond the Covered Call expiration" - e.g. buy the Aug-21 99-delta long call, sell the
  Aug-14 covered call against it. This is a deliberate buffer (long-call expiry > short-call expiry by
  ~1-2 weeks) so he has room to manage/roll before the long leg itself is at risk. Our engine/backtest
  should confirm whether `cpt_legs.py` already assumes this offset or picks same-week expiries - if the
  latter, this is a candidate parameter to align with his live practice (advise-only, needs Yarden's ok).

## 2026-08-27 - weekly pass (BIG catch-up; 2 reasoning-rich emails read in full + 50 alert subjects)

### NAIL "CLOSED - NO ROLL" (2026-08-25) - what John's "close" REALLY means (relevant to our paper engine)
John posted "CLOSED - NO ROLL ... Down on the Position, B/E $46.50" yet "MADE $1,920 (2.09%)". Reading the
body, this is NOT a campaign loss - it is the SHORT-leg cheap-to-close rule:
- He bought back the weekly covered call for $0.08 = **-80% of its premium** (the stock fell, the CC
  decayed), banking the premium. "Down on the Position" = the shares/99-delta are underwater; the CAMPAIGN
  is net-green from accumulated CC premiums.
- He then **PAUSED** (kept the 99-delta long, expires Sep-4) rather than immediately re-writing - "take risk
  off the table, cash back in the account; maybe do another this week if it moves, else a Sep-4 cost-basis
  trade and keep milking these premiums week over week."
- **Lesson (directly supports our roll-don't-close-red fix):** John NEVER realizes a campaign loss. His
  "close" = manage the SHORT leg (buy it back cheap, bank premium, pause or roll) while the long call rides.
  Our old CASE B closed whole campaigns at a loss; the fix (roll unless the close is green) matches him.
  Net-new to model: a PAUSE state (premium banked, long alive, no active short) distinct from ROLLED.

### "The Simple Way to Understand Your 99-Delta Roll" (2026-08-27) - cost-basis carry-forward (net-new doctrine)
Extends the Aug-08 roll email with the actual bookkeeping:
- Rolling the long = sell old 99-delta (realize a loss on it), buy a new later-dated 99-delta. **Carry the
  SHARE cost basis forward:** new effective basis = old basis + the new long's TIME VALUE. Example DPST:
  $144.00 + $0.50 = $144.50; that $144.50 becomes the target exit price for selling CCs to exit the long
  side at ~breakeven.
- **"The mistake is buying the new 99-delta and forgetting the cost basis carried forward. We don't reset
  the trade because we rolled the expiration."**
- **ALWAYS roll the 99-delta before expiry** - it's deep ITM, so letting it expire assigns you the shares
  (capital you may not want tied up).
- **Gap in our engine:** `cpt_paper` models NO long-call roll or long-call expiry at all (flagged when
  shipping the CASE-B fix). This email is the spec for it: a long-call-roll event that carries cost basis +
  adds the new time value. Advise-only candidate for the next engine iteration.

### Observation (from subjects/videos, not fully read): John TRADED NVDA earnings via NVDL
John opened NVDL on 08-24 and posted a members video "NVIDIA EARNINGS TRADE - HOW I PLAYED IT USING NVDL
(2x NVDA)", then closed NVDL 08-27 for +$2,900 / 4.83% in 3 days. He does NOT blanket-avoid earnings - he
plays them via the 2x ETF. This TENSIONS our `cpt_context` "earnings-soon = avoid" flag (which we raised on
NVDL). Flag for review: John's rule may be "size/structure around earnings," not "avoid." Verify by reading
the video/body next pass before changing anything.

---

### RUNNING LEARNING SIGNAL (advise-only - do NOT wire without Yarden's ok; updated each week)
1. **The catalyst-classification gap.** John's biggest edge over our mechanical gate = judging a dip as
   *profit-taking (buyable)* vs *fundamental break (avoid)*. Our gate can't see this. Candidate: extend
   cpt_context toward a "why is it down" read (recent earnings? sector news? macro?) - we already have
   earnings proximity; the news/catalyst angle was deferred (the LLM `/why`). This is the strongest
   argument yet for that deferred layer. Still advise-only; watch for more instances first.
2. **Confirmations (already in our engine, now validated by John's own words):** entry at/around the KC
   MID; broad-market regime as backdrop; the limit-order-at-the-number exit discipline.
3. **(2026-08-15) Long-call/short-call expiry offset.** John says he now buys the 99-delta long call
   ~2-3 weeks past the covered-call expiration as standard practice, not just when rolling underwater -
   a deliberate management buffer. Check whether `cpt_legs.py` matches this offset; if not, candidate
   parameter change (advise-only, one data point so far).
4. **(2026-08-27) 99-delta long-call roll = cost-basis carry-forward.** New effective cost basis = old basis
   + the new long's time value; that number becomes the CC-selling exit target. Always roll the long BEFORE
   expiry or you get assigned the shares. Our engine models no long-call roll at all - this email is the spec
   for when we build it. Advise-only.
5. **(2026-08-27) John trades earnings via 2x ETFs; he does NOT blanket-avoid.** He played NVDA earnings
   through NVDL (+$2,900 / 3 days). Tensions our `cpt_context` "earnings-soon = avoid" flag. Candidate: soften
   from AVOID to a size/structure caution. Verify against his earnings-trade video first; advise-only.
6. **(2026-08-27) "Close" = manage the SHORT leg, never realize a campaign loss.** John's cheap-to-close +
   PAUSE on NAIL confirms it. Supports the roll-don't-close-red fix already shipped; net-new is modeling a
   PAUSE state (banked, long alive, no active short) distinct from ROLLED. Advise-only.
