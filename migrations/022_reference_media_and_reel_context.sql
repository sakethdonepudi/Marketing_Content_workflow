-- Reference media ingest (rights-cleared public-figure photos and party logos) and
-- optional contextual Final Reel inputs. Additive and forward-only.
--
-- uploaded_media_assets is a workspace-level, rights-tracked store. Only rows with
-- rights_status='VERIFIED' may be bound into a Final Reel. The bytes live in the
-- controlled media storage; the database holds provenance, checksum, and audit fields.

CREATE TABLE uploaded_media_assets(
  id TEXT PRIMARY KEY,
  asset_type TEXT NOT NULL CHECK(asset_type IN ('PUBLIC_FIGURE_PHOTO','PARTY_LOGO','OTHER')),
  label TEXT NOT NULL,
  identity_subject TEXT,
  storage_uri TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  width INTEGER,
  height INTEGER,
  file_size INTEGER NOT NULL CHECK(file_size > 0),
  checksum_sha256 TEXT NOT NULL UNIQUE,
  source_name TEXT NOT NULL,
  source_url TEXT,
  license_note TEXT NOT NULL,
  rights_status TEXT NOT NULL CHECK(rights_status IN ('VERIFIED','RESTRICTED','UNKNOWN')),
  uploader TEXT NOT NULL,
  reviewer TEXT,
  uploaded_at TEXT NOT NULL,
  rights_reviewed_at TEXT,
  UNIQUE(checksum_sha256)
);
CREATE INDEX idx_uploaded_media_type ON uploaded_media_assets(asset_type,rights_status);

ALTER TABLE final_reel_assets ADD COLUMN cbn_asset_id TEXT REFERENCES uploaded_media_assets(id);
ALTER TABLE final_reel_assets ADD COLUMN tdp_asset_id TEXT REFERENCES uploaded_media_assets(id);
ALTER TABLE final_reel_assets ADD COLUMN public_figure_qa_json TEXT;
ALTER TABLE final_reel_assets ADD COLUMN composition_manifest_json TEXT;

CREATE TRIGGER uploaded_media_assets_immutable_update BEFORE UPDATE ON uploaded_media_assets BEGIN
  SELECT RAISE(ABORT,'uploaded media assets are immutable');
END;
CREATE TRIGGER uploaded_media_assets_immutable_delete BEFORE DELETE ON uploaded_media_assets BEGIN
  SELECT RAISE(ABORT,'uploaded media assets are immutable');
END;
