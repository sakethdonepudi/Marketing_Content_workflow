-- Storage + provenance columns for ingested media candidates. Additive/forward-only.
ALTER TABLE media_candidates ADD COLUMN storage_uri TEXT;
ALTER TABLE media_candidates ADD COLUMN mime_type TEXT;
ALTER TABLE media_candidates ADD COLUMN width INTEGER;
ALTER TABLE media_candidates ADD COLUMN height INTEGER;
ALTER TABLE media_candidates ADD COLUMN file_size INTEGER;
ALTER TABLE media_candidates ADD COLUMN attribution_text TEXT;
