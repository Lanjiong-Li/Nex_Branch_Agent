"""Load the reviewed Nexo authoring semantics for immutable runtime material."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .records import canonical_bytes

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "docs/output-schemas/v2/nexo-authoring-contract.json"


def load_graph_contract() -> dict:
    """Read only for new work; resumed Runs read their archived runtime event."""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(contract, dict) or not contract.get("contract_id") or not contract.get("version"):
        raise ValueError("Nexo authoring contract requires an identity and version")
    source = contract.get("source")
    if not isinstance(source, dict) or not source.get("commit") or not source.get("files_sha256"):
        raise ValueError("Nexo authoring contract requires its reviewed source baseline")
    return {"contract": contract, "sha256": hashlib.sha256(canonical_bytes(contract)).hexdigest()}
