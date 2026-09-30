import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


with tempfile.TemporaryDirectory() as directory:
    app.DB = Path(directory) / "demo.sqlite3"
    app.init()
    first = app.ingest_signal(
        url="https://government.example/reports/water-project-review",
        title="N. Chandrababu Naidu reviews Andhra Pradesh water projects",
        text="Andhra Pradesh Chief Minister Nara Chandrababu Naidu reviewed state water project progress with officials in Amaravati.",
        source_name="Government Source Fixture",
        source_type="webpage",
        content_role="item",
        item_type="news",
        publication_time="2026-09-29T14:00:00Z",
        source_metadata={"official": True},
    )
    second = app.ingest_signal(
        url="https://leader.example/updates/andhra-water-projects",
        title="Andhra Pradesh CM reviews progress of state water projects",
        text="N. Chandrababu Naidu met officials in Amaravati to review progress across Andhra Pradesh water projects.",
        source_name="Leader Source Fixture",
        source_type="rss",
        content_role="feed_item",
        item_type="news",
        publication_time="2026-09-29T15:00:00Z",
    )
    event = app.overview()["events"][0]
    print(json.dumps({
        "same_event": first["event_id"] == second["event_id"],
        "event_id": event["id"],
        "event_title": event["title"],
        "source_count": event["source_count"],
        "source_names": event["source_names"],
        "second_signal_clustered": second["clustered"],
        "cluster_score": second["cluster_score"],
    }, indent=2))
