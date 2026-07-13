#!/usr/bin/env python3
"""Copy a MemPalace from a Chroma palace into Qdrant, reusing the precomputed
embeddings (no re-embedding, exact-fidelity vectors).

Reads each collection via collection.get(include=[documents,metadatas,embeddings])
from the Chroma source and re-adds into the Qdrant-backed palace with the same
ids/metadata/vectors. The only REQUIRED collection is mempalace_drawers;
mempalace_closets is a derived index and may legitimately be absent.

Usage:
  MEMPALACE_QDRANT_URL=http://127.0.0.1:6333 \
  python migrate_chroma_to_qdrant.py SRC_PALACE_PATH DEST_PALACE_PATH
"""
import sys
from mempalace.palace import get_collection

BATCH = 500
REQUIRED = "mempalace_drawers"
COLLECTIONS = [REQUIRED, "mempalace_closets"]


def migrate_collection(src_path, dst_path, name):
    """Return (src_count, dst_count), or None if the source collection is absent."""
    try:
        src = get_collection(src_path, collection_name=name, create=False, backend="chroma")
        total = src.count()
    except Exception as exc:  # noqa: BLE001
        cls = type(exc).__name__
        if "NotInitialized" in cls or "NotFound" in cls or "PalaceNotFound" in cls:
            return None
        raise
    dst = get_collection(dst_path, collection_name=name, create=True, backend="qdrant")
    moved = 0
    offset = 0
    while offset < total:
        batch = src.get(limit=BATCH, offset=offset,
                        include=["documents", "metadatas", "embeddings"])
        ids = batch["ids"]
        if not ids:
            break
        # upsert (not add): skips the duplicate-precheck that fails before the
        # qdrant collection is created, and is idempotent across re-runs.
        dst.upsert(ids=ids, documents=batch.get("documents"),
                   metadatas=batch.get("metadatas"), embeddings=batch.get("embeddings"))
        moved += len(ids)
        offset += len(ids)
        print(f"  [{name}] {moved}/{total}")
    return total, dst.count()


def main():
    src_path, dst_path = sys.argv[1], sys.argv[2]
    print(f"SRC (chroma): {src_path}\nDST (qdrant): {dst_path}")
    drawers_ok = False
    all_present_match = True
    for name in COLLECTIONS:
        r = migrate_collection(src_path, dst_path, name)
        if r is None:
            print(f"== {name}: assente nella sorgente, salto (ok)")
            continue
        src_n, dst_n = r
        match = src_n == dst_n
        if not match:
            all_present_match = False
        if name == REQUIRED and match and src_n > 0:
            drawers_ok = True
        print(f"== {name}: src={src_n} dst={dst_n} → {'OK' if match else 'MISMATCH'}")
    ok = drawers_ok and all_present_match
    print("RISULTATO:", "PARITÀ OK" if ok else "DA CONTROLLARE")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
