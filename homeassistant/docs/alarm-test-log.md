# Alarm system — live test log

**Hand-maintained. This file is never generated and never overwritten.**

`scripts/generate-alarm-doc.py` READS this file and reprints the most recent
entry in `docs/alarm-system.md`, the document that goes to the insurer. That is
the whole reason it exists: the insurer's document must carry a test date, but
it is regenerated from the device register on every run, so a date typed into
it would vanish. Type the date here instead.

Add a new row at the **top** after every live test (`docs/alarm-testing.md`
section E). Keep the date in `YYYY-MM-DD` form — the generator sorts on it.

| Date | Test | Result | By |
|---|---|---|---|
| _none yet_ | — | — | — |

<!-- Example of a completed row, for whoever writes the first real one:
| 2026-03-14 | Full live intrusion test, monitoring in test mode | Pass — siren, dispatch call received, passcode accepted | — |
-->
