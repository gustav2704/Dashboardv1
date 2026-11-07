"""Recover chart inputs for exact EX5 hits rejected by stale catalog metadata."""

from __future__ import annotations

import json
import sys
from pathlib import Path, PureWindowsPath


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "scripts"))

from app.db import session  # noqa: E402
from app.ssh_bridge import SSHBridge  # noqa: E402
from forensic_retry_filtered import _filtered_profiles  # noqa: E402
from forensic_selected_vps_identity import CACHE_ROOT, _load_targets, _sha256  # noqa: E402


ARTIFACT_REPORT = ROOT / "reports" / "forensic_unresolved_vps_artifacts_2026-09-06.json"
OUTPUT_REPORT = ROOT / "reports" / "forensic_relaxed_missing_profiles_2026-09-06.json"
TARGET_IDS = {391, 406, 575, 576, 577}


def main() -> None:
    artifacts = json.loads(ARTIFACT_REPORT.read_text(encoding="utf-8"))
    targets = _load_targets()
    selected = {sid: targets[sid] for sid in TARGET_IDS}
    locations: dict[tuple[int, str], dict[str, object]] = {}
    for sid in TARGET_IDS:
        for item in artifacts["matches"].get(str(sid), []):
            locations[(int(item["terminal_id"]), str(item["data_dir"]).casefold())] = item
    output = {"matches": {str(sid): [] for sid in sorted(TARGET_IDS)}, "roots": []}
    for (terminal_id, _), location in locations.items():
        root = str(location["data_dir"])
        with session() as conn:
            terminal_row = conn.execute("SELECT * FROM terminals WHERE id=?", (terminal_id,)).fetchone()
        if not terminal_row:
            continue
        bridge = SSHBridge(dict(terminal_row))
        profiles = _filtered_profiles(bridge, root, selected, terminal_id)
        record = {"terminal_id": terminal_id, "data_dir": root, "profiles": len(profiles)}
        output["roots"].append(record)
        for sid in TARGET_IDS:
            expected = {name.casefold() for name in targets[sid]["ex5_names"]}
            for profile in profiles:
                if str(profile["expert_name"]).casefold() not in expected:
                    continue
                output["matches"][str(sid)].append({
                    "terminal_id": terminal_id,
                    "data_dir": root,
                    "profile_path": profile["path"],
                    "expert_name": profile["expert_name"],
                    "symbol": profile["symbol"],
                    "timeframe": profile["timeframe"],
                    "magic": profile["magic"],
                    "comment": profile["comment"],
                    "inputs": profile["inputs"],
                })
        print(f"RELAXED_ROOT_DONE terminal={terminal_id} profiles={len(profiles)} root={root}", flush=True)

    # Download each unique exact EX5/MQ5 hit so later identity decisions use a
    # local hash-verified artifact and never depend on a disappearing VPS file.
    for sid in TARGET_IDS:
        seen: set[str] = set()
        for item in artifacts["matches"].get(str(sid), []):
            digest = str(item["sha256"]).casefold()
            if digest in seen:
                continue
            seen.add(digest)
            with session() as conn:
                terminal_row = conn.execute(
                    "SELECT * FROM terminals WHERE id=?", (int(item["terminal_id"]),)
                ).fetchone()
            if not terminal_row:
                continue
            destination = CACHE_ROOT / str(sid) / PureWindowsPath(str(item["path"])).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            SSHBridge(dict(terminal_row)).download_path(str(item["path"]), destination)
            if _sha256(destination) != digest:
                destination.unlink(missing_ok=True)
                raise RuntimeError(f"Downloaded artifact hash mismatch for strategy {sid}")
    OUTPUT_REPORT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "RELAXED_FINISHED " + " ".join(
            f"{sid}={len(output['matches'][str(sid)])}" for sid in sorted(TARGET_IDS)
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
