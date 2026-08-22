# 11 — Voice and local control

> Set up Assist with a local pipeline: local wake word, local STT/TTS, and only
> fall back to a cloud LLM for conversational queries that the intent system
> can't handle. Define custom intents for the things I'll actually say, and
> expose only a curated entity set to the assistant — not everything.
> Document the exposure list in `docs/decisions.md` and explain the privacy
> tradeoff of each fallback you configure.

## Hooks that already exist

- `script.notify_person` has a `tts_service:` variable, empty by default.
  Fill it once a TTS engine exists and `critical` alerts start speaking aloud
  on every media player labelled `announce`. Until then TTS is skipped silently
  and the push still goes out.
- `script.set_area_mood` (`packages/lighting.yaml`) takes an area and a mood, so
  "set the kitchen to movie" needs an intent, not new automation logic.
- `script.climate_boost` (`packages/climate.yaml`) is the same shape for "make
  it colder in here".

## Exposure

Default Assist exposure is **everything**, which means a voice assistant can
unlock doors. Curate it, and record in `docs/decisions.md`:

- Which entities are exposed and why.
- Which are deliberately not — locks, the alarm panel, and anything in
  `security.yaml` should need a confirmation at minimum.
- For each cloud fallback: what leaves the house, to whom, and when.
