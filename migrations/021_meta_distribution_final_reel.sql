-- Bind Meta distribution to an approved Final Reel when one exists.
-- Additive and forward-only: existing packages keep media_source='GENERATED_ASSET'.
-- A FINAL_REEL package still records the source generated asset for lineage and the
-- approved source media review, while the uploaded bytes and checksum come from the
-- immutable final reel. Approvals are never inherited across final reel versions.

ALTER TABLE distribution_packages ADD COLUMN media_source TEXT NOT NULL DEFAULT 'GENERATED_ASSET';
ALTER TABLE distribution_packages ADD COLUMN final_reel_asset_id TEXT REFERENCES final_reel_assets(id);
ALTER TABLE distribution_packages ADD COLUMN final_reel_review_id TEXT REFERENCES final_reel_reviews(id);

ALTER TABLE publish_jobs ADD COLUMN media_source TEXT NOT NULL DEFAULT 'GENERATED_ASSET';
ALTER TABLE publish_jobs ADD COLUMN final_reel_asset_id TEXT REFERENCES final_reel_assets(id);

CREATE INDEX idx_distribution_packages_final_reel ON distribution_packages(final_reel_asset_id,platform,version_number);
