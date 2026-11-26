"""A per-node public key authenticates an immutable, bounded NDJSON capture."""

import base64
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_der_public_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services.fleet_steward import NODES


def ingest_bytes(memory, key_file, node, signature, raw):
    if node not in NODES or not key_file or not Path(key_file).is_file():
        raise PermissionError("Node not enrolled")
    keys = json.loads(Path(key_file).read_text())
    try:
        public = load_der_public_key(base64.b64decode(keys[node], validate=True))
        if not isinstance(public, Ed25519PublicKey):
            raise ValueError()
        public.verify(base64.b64decode(signature, validate=True), raw)
    except (KeyError, ValueError, TypeError, InvalidSignature) as exc:
        raise PermissionError("Invalid signature") from exc
    lines = raw.splitlines()
    if not lines or len(lines) > 8001:
        raise ValueError("Invalid capture size")
    envelope = json.loads(lines[0])
    if (
        not isinstance(envelope, dict)
        or envelope.get("node") != node
        or "items" in envelope
    ):
        raise ValueError("Invalid capture identity")
    envelope["items"] = [json.loads(line) for line in lines[1:]]
    memory.ingest(envelope)
    return {"node": node, "sha256": hashlib.sha256(raw).hexdigest()}
