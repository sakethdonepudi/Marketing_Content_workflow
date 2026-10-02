-- Architecture 06D: live Claude package provenance and xAI video lineage.
-- Forward-only, additive columns; existing rows keep NULL for fields they never had.

ALTER TABLE production_jobs ADD COLUMN request_snapshot_json TEXT;
ALTER TABLE production_jobs ADD COLUMN request_snapshot_hash TEXT;

ALTER TABLE render_jobs ADD COLUMN generation_mode TEXT
  CHECK(generation_mode IS NULL OR generation_mode IN ('IMAGE','TEXT_TO_VIDEO','IMAGE_TO_VIDEO','REFERENCE_TO_VIDEO'));
ALTER TABLE render_jobs ADD COLUMN requested_aspect_ratio TEXT;
ALTER TABLE render_jobs ADD COLUMN requested_duration_seconds REAL;
ALTER TABLE render_jobs ADD COLUMN requested_resolution TEXT;
ALTER TABLE render_jobs ADD COLUMN source_asset_id TEXT REFERENCES generated_assets(id);
ALTER TABLE render_jobs ADD COLUMN source_asset_checksum TEXT;

ALTER TABLE generated_assets ADD COLUMN codec TEXT;
ALTER TABLE generated_assets ADD COLUMN has_audio INTEGER CHECK(has_audio IS NULL OR has_audio IN (0,1));

CREATE INDEX idx_render_jobs_source_asset ON render_jobs(source_asset_id);
