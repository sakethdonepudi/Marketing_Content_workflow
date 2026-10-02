-- Architecture 14: performance learning + hook linkage.
-- Additive. Aggregate content-level metrics only; no demographic/political profiling.

-- Extra aggregate metrics + content linkage for performance learning.
ALTER TABLE youtube_performance_snapshots ADD COLUMN engaged_views INTEGER;
ALTER TABLE youtube_performance_snapshots ADD COLUMN stayed_to_watch_pct REAL;
ALTER TABLE youtube_performance_snapshots ADD COLUMN hook_variant TEXT;
ALTER TABLE youtube_performance_snapshots ADD COLUMN topic TEXT;
ALTER TABLE youtube_performance_snapshots ADD COLUMN language TEXT;
