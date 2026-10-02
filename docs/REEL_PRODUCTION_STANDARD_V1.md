# REEL_PRODUCTION_STANDARD_V1

Canonical production standard for ReachOut Final Reels. Reference implementation:
**FR-8623B1425165** (composer policy `final-reel-composer-v25`). The policy object lives in
`reel_standard.py`; every new reel records `production_standard_version = REEL_PRODUCTION_STANDARD_V1`
unless a future standard supersedes it. Overrides must be explicit, recorded, reasoned, and may
never bypass the factual, rights, rendered-frame, or human-review gates.

## Architecture

Verified event → approved claim set → Content CEO decision → live Claude package → immutable
media prompt → generated video → real rights-cleared B-roll → scene plan → continuous narration
→ subtitles → music → final render → QA → `READY_FOR_REVIEW`.

Audio-first: the approved script is synthesized as **one continuous TTS file**, its waveform is
measured, phrase timestamps are derived, and the **visual timeline is built around the audio**
(never the reverse).

## Editorial rules

- Informational only. No praise, blame, campaign language, persuasion, or unsupported political
  credit. Public figures are contextual unless verified claims genuinely centre on them.
- Avoid repeating one fact in different wording; concise and understandable on first listen.
- No invented quotes, numbers, dates, or exaggerated framing.

## Narration rules

- One continuous narration file per reel. Never sentence-by-sentence TTS, scene-by-scene
  narration, or inserted silence at every cut.
- Default Telugu voice `te-IN-MohanNeural`, rate ≈ −11% to −12% (story-specific adjustment
  allowed). Calm, conversational, no anchor voice, no rushed delivery.
- `display_text` keeps figures (`2025–26`); `spoken_text` normalizes years, abbreviations,
  acronyms, percentages, currency, dates, and difficult proper nouns to spoken Telugu words.

## Visual sourcing rules

Priority: rights-cleared real local media → verified institutional/public-domain/compatible
licence media → user-provided media → locally composed factual graphics → generated AI imagery
only when authentic media is unavailable. Never infer location from appearance; store
source/location provenance. Target 7–10 meaningful beats for ~25–40 s reels.

## Compositor rules

- **No persistent base layer.** The source video/image is never a visible base behind scene
  media; it is hidden or neutral only.
- Every scene owns the full canvas for a strict contiguous `[start, end]` window. Never fade a
  scene to zero before the next scene owns the canvas.
- Transitions are **A → B** (clean cut or a 4–8 frame direct A/B dissolve). Never
  A → hidden/default asset → B. No fallback footage, placeholder, poster frame, tobacco reset,
  or interstitial.
- Still motion is varied (push, pull, pan, drift, occasional hold) with eased curves; no
  identical motion on every shot.

## Subtitle, music, public-figure

- Subtitles follow audio, may cross scene boundaries, short phrases, max 2 lines, Telugu-capable
  font, restrained shadow, no large opaque box, safe zone, never over a face.
- One continuous music bed, ~12–16 dB below narration during speech; no partisan music.
- Exactly one clean portrait when relevant; no duplicate/giant/logo-over-face treatment.

## QA gates

Mandatory (applicable): FACTUAL_QA, CONTINUOUS_NARRATION_QA, YEAR_PRONUNCIATION_QA,
NARRATION_NATURALNESS_QA, SUBTITLE_QA, GLYPH_RENDER_QA, EDITORIAL_CONTINUITY_QA,
RENDERED_FRAME_CONTINUITY_QA, RIGHTS_PROVENANCE_QA, AUDIO_MIX_QA, IG_COMPATIBILITY,
FB_COMPATIBILITY. Conditional: LOCAL_CONTEXT_QA, PUBLIC_FIGURE_QA, REFERENCE_STANDARD_QA.

`RENDERED_FRAME_CONTINUITY_QA` samples the actual MP4 (regular intervals plus around every
boundary) and fails on an unassigned asset, fallback frame, third asset at a transition, or a
duplicate public figure. The **rendered** repeated-asset count is authoritative.

## Human review

Nothing auto-publishes. Final state is `READY_FOR_REVIEW`; a human chooses APPROVE,
CHANGES_REQUIRED, or REJECT. Publishing/scheduling stays disabled unless explicitly enabled.

## Known anti-patterns (previously shipped, now forbidden)

1. Persistent base video showing between scenes (the cured-tobacco interstitial).
2. Fade-to-zero scene layers exposing the base.
3. Fixed 4-second shot blocks / slideshow timing.
4. Chunked narration with audio restarts and inserted silences.
5. Digit-by-digit year pronunciation.
6. AI visuals used by default instead of real rights-cleared media.
7. Repeated near-duplicate leaf macros or a duplicated public-figure portrait.
8. Scene changes locked to sentence endings.

## Default output

720×1280, 9:16, H.264 + AAC, 24 fps, no edit lists. Higher resolution is allowed for natively
higher-resolution sources without unnecessary upscaling.
