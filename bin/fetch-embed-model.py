#!/usr/bin/env python3
"""Install the pinned embedding model for the semantic tier.

Downloads (or converts from a local snapshot) potion-base-8M — the Phase 0
winner — into the model directory the StaticEncoder loads from:

    ~/.nock-brain/model/
        embeddings.npy   float32 token-embedding matrix (29528 x 256)
        tokenizer.json   HuggingFace tokenizers file
        model.json       metadata: model_id, dim, source checksums

Both source files are verified against pinned SHA-256 checksums before
anything is written, so a hijacked download or tampered snapshot is rejected.
The safetensors container is parsed with the stdlib (8-byte little-endian
header length, JSON header, raw tensor bytes) — no safetensors/model2vec
dependency; numpy is the only requirement. Run with an interpreter that has
it — canonically ~/.nock-brain/venv/bin/python3 (created by
`install.sh --semantic`); bare `python3` is PATH/alias-dependent.

Usage:
    python3 fetch-embed-model.py                       # download + install
    python3 fetch-embed-model.py --from-dir SNAPSHOT   # convert local copy
    python3 fetch-embed-model.py --model-dir /custom/path
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import urllib.request
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _embed import DEFAULT_MODEL_DIR
from _store import secure_mkdir

MODEL_ID = "potion-base-8M"
HF_BASE = "https://huggingface.co/minishlab/potion-base-8M/resolve/main"
# Pinned 2026-07-11 (Phase 0 spike snapshot). A publisher-side update changes
# these hashes and the fetch fails closed until the pin is reviewed.
PINNED = {
    "model.safetensors":
        "f65d0f325faadc1e121c319e2faa41170d3fa07d8c89abd48ca5358d9a223de2",
    "tokenizer.json":
        "e67e803f624fb4d67dea1c730d06e1067e1b14d830e2c2202569e3ef0f70bb50",
}
TENSOR_NAME = "embeddings"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, name: str) -> None:
    actual = sha256_file(path)
    if actual != PINNED[name]:
        raise SystemExit(
            f"checksum mismatch for {name}: expected {PINNED[name]}, "
            f"got {actual} — refusing to install"
        )


def download(name: str, dest: Path) -> None:
    url = f"{HF_BASE}/{name}"
    if not url.startswith("https://"):  # defense in depth for the pin above
        raise SystemExit(f"refusing non-https url {url}")
    print(f"downloading {url}")
    with urllib.request.urlopen(url) as response:  # nosec B310 - pinned https
        dest.write_bytes(response.read())


def read_safetensors_matrix(path: Path):
    """Minimal stdlib safetensors reader for the single-tensor potion file."""
    import numpy as np

    with path.open("rb") as handle:
        (header_len,) = struct.unpack("<Q", handle.read(8))
        header = json.loads(handle.read(header_len))
        if TENSOR_NAME not in header:
            raise SystemExit(
                f"{path} has no tensor {TENSOR_NAME!r} "
                f"(found {sorted(k for k in header if k != '__metadata__')})"
            )
        info = header[TENSOR_NAME]
        if info.get("dtype") != "F32":
            raise SystemExit(f"expected F32 tensor, got {info.get('dtype')}")
        start, end = info["data_offsets"]
        handle.seek(8 + header_len + start)
        raw = handle.read(end - start)
    matrix = np.frombuffer(raw, dtype=np.float32).reshape(info["shape"])
    return matrix.copy()


def install(safetensors_path: Path, tokenizer_path: Path,
            model_dir: Path) -> None:
    import numpy as np

    matrix = read_safetensors_matrix(safetensors_path)
    secure_mkdir(model_dir)
    np.save(model_dir / "embeddings.npy", matrix)
    (model_dir / "embeddings.npy").chmod(0o600)
    (model_dir / "tokenizer.json").write_bytes(tokenizer_path.read_bytes())
    (model_dir / "tokenizer.json").chmod(0o600)
    meta = {
        "model_id": MODEL_ID,
        "dim": int(matrix.shape[1]),
        "vocab": int(matrix.shape[0]),
        "source_sha256": dict(PINNED),
    }
    (model_dir / "model.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")
    (model_dir / "model.json").chmod(0o600)
    print(f"installed {MODEL_ID} ({matrix.shape[0]}x{matrix.shape[1]}) "
          f"-> {model_dir}")


def run(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the pinned embedding model")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--from-dir", type=Path, default=None,
                        help="Convert a local HF snapshot instead of "
                             "downloading (needs model.safetensors + "
                             "tokenizer.json)")
    args = parser.parse_args(argv)

    try:
        import numpy  # noqa: F401
    except ImportError:
        print("fetch-embed-model: numpy is required (pip install numpy)",
              file=sys.stderr)
        return 1

    if args.from_dir:
        src_model = args.from_dir / "model.safetensors"
        src_tok = args.from_dir / "tokenizer.json"
        if not src_model.exists() or not src_tok.exists():
            print(f"{args.from_dir} lacks model.safetensors/tokenizer.json",
                  file=sys.stderr)
            return 1
    else:
        staging = args.model_dir / ".staging"
        secure_mkdir(staging)
        src_model = staging / "model.safetensors"
        src_tok = staging / "tokenizer.json"
        download("model.safetensors", src_model)
        download("tokenizer.json", src_tok)

    verify(src_model, "model.safetensors")
    verify(src_tok, "tokenizer.json")
    install(src_model, src_tok, args.model_dir)

    if not args.from_dir:
        src_model.unlink(missing_ok=True)
        src_tok.unlink(missing_ok=True)
        try:
            (args.model_dir / ".staging").rmdir()
        except OSError:
            pass
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
