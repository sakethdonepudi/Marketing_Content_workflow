"""Canonical Final Reel production standard.

REEL_PRODUCTION_STANDARD_V1 codifies the architecture and QA behaviour of the reference
reel FR-8623B1425165 so every future reel inherits the same content, audio, visual, editing,
rights, and review standards by default. It is an explicit policy object, not tribal
knowledge. Overrides are allowed only when explicit, recorded, and reasoned, and they may
never bypass the factual, rights, rendered-frame, or human-review gates.
"""

PRODUCTION_STANDARD_VERSION = "REEL_PRODUCTION_STANDARD_V1"
REFERENCE_REEL_ID = "FR-8623B1425165"
REFERENCE_COMPOSER_POLICY = "final-reel-composer-v25"

# QA gates every reel must pass before READY_FOR_REVIEW (where applicable).
MANDATORY_QA_GATES = (
    "FACTUAL_QA",
    "CONTINUOUS_NARRATION_QA",
    "YEAR_PRONUNCIATION_QA",
    "NARRATION_NATURALNESS_QA",
    "SUBTITLE_QA",
    "GLYPH_RENDER_QA",
    "EDITORIAL_CONTINUITY_QA",
    "RENDERED_FRAME_CONTINUITY_QA",
    "RIGHTS_PROVENANCE_QA",
    "AUDIO_MIX_QA",
    "IG_COMPATIBILITY",
    "FB_COMPATIBILITY",
)
CONDITIONAL_QA_GATES = ("LOCAL_CONTEXT_QA", "PUBLIC_FIGURE_QA", "REFERENCE_STANDARD_QA")

# Gates that may never be bypassed even with an explicit override.
NON_BYPASSABLE_GATES = ("FACTUAL_QA", "RIGHTS_PROVENANCE_QA", "RENDERED_FRAME_CONTINUITY_QA", "HUMAN_REVIEW")

STANDARD = {
    "version": PRODUCTION_STANDARD_VERSION,
    "reference_reel_id": REFERENCE_REEL_ID,
    "reference_composer_policy": REFERENCE_COMPOSER_POLICY,
    "output": {"width": 720, "height": 1280, "aspect": "9:16", "codec": "h264+aac", "fps": 24,
               "edit_lists": False,
               "note": "Higher output is allowed for natively higher-resolution sources; no upscaling."},
    "narration": {
        "architecture": "one continuous file per reel",
        "forbidden": ["sentence-by-sentence TTS files", "scene-by-scene narration",
                      "audio restarts between visuals", "inserted silence at every cut"],
        "default_voice": "te-IN-MohanNeural",
        "default_rate_percent": -11,
        "rate_range_percent": [-12, -11],
        "audio_first": True,
        "spoken_display_split": True,
    },
    "normalization": {
        "spoken_display_split": True,
        "examples": {"2025": "రెండు వేల ఇరవై ఐదు", "2026": "రెండు వేల ఇరవై ఆరు",
                     "2025–26": "రెండు వేల ఇరవై ఐదు - ఇరవై ఆరు"},
        "applies_to": ["years", "abbreviations", "acronyms", "percentages", "currency", "dates", "proper nouns"],
    },
    "visual_sourcing": {
        "priority": ["rights-cleared real local media", "verified institutional/public-domain/compatible-license media",
                     "user-provided media", "locally composed factual graphics", "generated AI imagery only when unavailable"],
        "default_is_ai": False,
        "never_infer_location": True,
        "store_provenance": True,
    },
    "visual_diversity": {"target_beats": [7, 10], "target_duration_seconds": [25, 40],
                         "forbidden": ["repeated leaf closeups", "repeated buildings", "same portrait multiple times",
                                       "adjacent near-duplicate assets", "default background returning between scenes"]},
    "compositor": {
        "persistent_base_layer": False,
        "scene_window": "strict contiguous [start,end]; layer valid for the whole window",
        "fade_to_zero_before_next_scene": False,
        "transitions": ["clean cut", "occasional 4-8 frame A/B dissolve"],
        "forbidden": ["fallback footage", "source video", "placeholder image", "tobacco reset", "poster frame",
                      "interstitial image", "fade/restart audio at cuts"],
    },
    "editing": {"cuts": ["editorial cut", "J-cut", "L-cut"], "ken_burns": True, "varied_shot_duration": True,
                "shot_duration_seconds": [2, 5], "public_figure_seconds": [2, 2.5],
                "avoid": ["slideshow timing", "identical shot lengths", "scene changes locked to sentence endings"]},
    "public_figure": {"single_portrait": True, "duplicate_portrait": False, "giant_background_face": False,
                      "logo_over_face": False, "contextual_only": True},
    "subtitles": {"source": "audio", "cross_scene_boundaries": True, "max_lines": 2, "short_phrases": True,
                  "large_opaque_box": False, "font_script": "Telugu", "safe_zone": True, "face_overlap": False},
    "music": {"continuous_bed": True, "restart_per_shot": False, "duck_db_below_narration": [12, 16],
              "partisan_music": False},
    "rights": {"reusable_statuses": ["VERIFIED_REUSE", "ATTRIBUTION_REQUIRED", "USER_PROVIDED"],
               "unknown_allowed": False,
               "record_fields": ["source_url", "publisher", "license", "attribution_required", "location",
                                 "content_hash", "usage_scope", "ingested_at", "rights_status"]},
    "qa": {"mandatory": list(MANDATORY_QA_GATES), "conditional": list(CONDITIONAL_QA_GATES),
           "non_bypassable": list(NON_BYPASSABLE_GATES)},
    "review": {"auto_publish": False, "final_state": "READY_FOR_REVIEW",
               "human_options": ["APPROVE", "CHANGES_REQUIRED", "REJECT"],
               "publishing_enabled": False},
}


class StandardOverrideError(ValueError):
    pass


def validate_override(override):
    """An override must be explicit, recorded, reasoned, and may not bypass core gates."""
    if not isinstance(override, dict):
        raise StandardOverrideError("An override must be an explicit mapping.")
    reason = str(override.get("reason") or "").strip()
    if not reason:
        raise StandardOverrideError("An override must include a reason.")
    bypassed = set(override.get("bypass") or [])
    forbidden = bypassed & set(NON_BYPASSABLE_GATES)
    if forbidden:
        raise StandardOverrideError("An override may not bypass: " + ", ".join(sorted(forbidden)))
    return {"recorded": True, "reason": reason, "bypass": sorted(bypassed)}


def active_standard():
    """The active production standard every new reel inherits by default."""
    return dict(STANDARD)


def reference_standard_qa(reel):
    """Compare a reel structurally against the reference implementation (not pixel similarity)."""
    checks = []
    def check(name, ok, note=""):
        checks.append({"check": name, "status": "PASS" if ok else "FLAG", "note": note})
    continuous = reel.get("continuous_narration_qa") or {}
    editorial = reel.get("editorial_continuity_qa") or {}
    rendered = reel.get("rendered_frame_continuity_qa") or {}
    rights = reel.get("rights_provenance_qa") or {}
    local = reel.get("local_context_qa") or {}
    figure = reel.get("public_figure_qa") or {}
    check("continuous_audio", continuous.get("status", "N/A") in ("PASS", "N/A"))
    check("visual_continuity", rendered.get("status", "N/A") in ("PASS", "N/A")
          and not (rendered.get("third_asset_transition_count") or 0))
    check("no_hidden_fallback", not any(b.get("kind") == "FOOTAGE" for b in (reel.get("composition_manifest") or {}).get("beats", [])))
    check("scene_diversity", (editorial.get("distinct_visuals") or 0) >= 7)
    check("subtitle_cleanliness", (reel.get("subtitle_qa") or {}).get("status", "N/A") in ("PASS", "N/A"))
    check("rights_provenance", rights.get("status", "N/A") in ("PASS", "N/A"))
    check("local_authenticity", (local.get("real_ap_percent") or 0) >= 60 or local.get("status", "N/A") == "N/A")
    check("render_correctness", (rendered.get("rendered_repeated_asset_count") or 0) == 0)
    check("music_narration_balance", (reel.get("audio_qa") or {}).get("status", "N/A") in ("PASS", "N/A"))
    check("public_figure_restraint", figure.get("status", "N/A") in ("PASS", "N/A"))
    check("technical_compatibility",
          (reel.get("instagram_compatibility") or {}).get("compliant", True)
          and (reel.get("facebook_compatibility") or {}).get("compliant", True))
    failed = [c for c in checks if c["status"] == "FLAG"]
    return {"status": "PASS" if not failed else "FLAG", "checks": checks,
            "reference_reel_id": REFERENCE_REEL_ID, "standard_version": PRODUCTION_STANDARD_VERSION}
