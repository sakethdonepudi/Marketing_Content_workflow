"""Storage self-test — WRITE / READ / VERIFY / DELETE through the configured adapter.

Safe by design: keys always live under `<env>/self-test/<uuid>.txt` and never overwrite a
business key. Cleanup runs in a finally block. Output never prints credentials.

Exit codes: 0 = full PASS, 1 = configuration/runtime failure.
"""

import os
import sys
import uuid

# Allow running as `python3 tools/storage_self_test.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _storage(root=None):
    import media_storage
    return media_storage, media_storage.media_storage_from_env(root or os.environ.get("RENDER_STORAGE_ROOT", "."))


def run(root=None, storage=None):
    """Run the self-test. Returns a report dict; never raises (failures are captured)."""
    import media_storage
    report = {"environment": os.environ.get("APP_ENV", "development"),
              "backend": os.environ.get("STORAGE_BACKEND", "local"),
              "bucket_configured": bool(os.environ.get("S3_BUCKET")), "key_prefix": None,
              "write": "FAIL", "read": "FAIL", "byte_verification": "FAIL", "delete": "FAIL",
              "cleanup_attempted": False, "cleanup_verified": "SKIPPED", "error": None}
    try:
        media_storage.assert_storage_configuration()
    except ValueError as error:
        report["error"] = str(error)
        return report
    if storage is None:
        try:
            _, storage = _storage(root)
        except Exception as error:  # noqa: BLE001 - construction failure is a config failure
            report["error"] = f"storage construction failed: {type(error).__name__}"
            return report
    prefix = media_storage.self_test_prefix()
    payload = f"reachout-self-test-{uuid.uuid4().hex}".encode()
    key = f"{prefix}/{uuid.uuid4().hex}.txt"
    storage_uri = f"s3://{os.environ.get('S3_BUCKET')}/{key}" if report["backend"] == "s3" else f"local://{key}"
    report["key_prefix"] = prefix
    written = None
    try:
        storage.save_at(storage_uri, payload)
        report["write"] = "PASS"
        fetched = storage.get(storage_uri)
        report["read"] = "PASS"
        report["byte_verification"] = "PASS" if fetched == payload else "FAIL"
    except Exception as error:  # noqa: BLE001 - safe failure text
        report["error"] = f"self-test failed: {type(error).__name__}"
    finally:
        if report["write"] == "PASS":
            report["cleanup_attempted"] = True
            try:
                storage.delete(storage_uri)
                report["delete"] = "PASS"
                # Verify the artifact is gone where the adapter can check.
                try:
                    report["cleanup_verified"] = "PASS" if not storage.exists(storage_uri) else "FAIL"
                except Exception:  # noqa: BLE001
                    report["cleanup_verified"] = "SKIPPED"
            except Exception:  # noqa: BLE001 - cleanup best-effort
                report["delete"] = "FAIL"
    return report


def main(argv):
    root = argv[1] if len(argv) > 1 else None
    report = run(root=root)
    for key in ("environment", "backend", "bucket_configured", "key_prefix", "write", "read",
                "byte_verification", "delete", "cleanup_attempted", "cleanup_verified"):
        print(f"{key}: {report[key]}")
    if report["error"]:
        print("error:", report["error"])
    ok = all(report[k] == "PASS" for k in ("write", "read", "byte_verification", "delete"))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
