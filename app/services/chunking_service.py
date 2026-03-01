import json
from pathlib import Path

from app.core.config import get_settings
from app.core.paths import resolve_project_path

settings = get_settings()


def _resolve_path(path_value: str) -> Path:
    """Resolve a configured path relative to the project root when needed."""
    return resolve_project_path(path_value)


def _normalize_separators(separators: list[str]) -> list[str]:
    """Ensure the recursive splitter always has a final character-level fallback."""
    cleaned = [separator for separator in separators if isinstance(separator, str)]
    if "" not in cleaned:
        cleaned.append("")
    return cleaned or [""]


def _fixed_window_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping fixed-size windows as the final fallback."""
    stripped = text.strip()
    if not stripped:
        return []

    if len(stripped) <= chunk_size:
        return [stripped]

    step = max(1, chunk_size - overlap)
    chunks = []
    start = 0
    while start < len(stripped):
        chunk = stripped[start : start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        if start + chunk_size >= len(stripped):
            break
        start += step
    return chunks


def _recursive_split(text: str, separators: list[str], chunk_size: int, overlap: int) -> list[str]:
    """Recursively split text by progressively smaller separators."""
    stripped = text.strip()
    if not stripped:
        return []

    if len(stripped) <= chunk_size:
        return [stripped]

    if not separators:
        return _fixed_window_split(stripped, chunk_size, overlap)

    separator = separators[0]
    if separator == "":
        return _fixed_window_split(stripped, chunk_size, overlap)

    parts = [part.strip() for part in stripped.split(separator) if part and part.strip()]
    if len(parts) <= 1:
        return _recursive_split(stripped, separators[1:], chunk_size, overlap)

    segments: list[str] = []
    current = ""
    for part in parts:
        candidate = part if not current else f"{current}{separator}{part}"
        if len(candidate) <= chunk_size:
            current = candidate
            continue

        if current:
            segments.extend(_recursive_split(current, separators[1:], chunk_size, overlap))
        current = ""

        if len(part) <= chunk_size:
            current = part
        else:
            segments.extend(_recursive_split(part, separators[1:], chunk_size, overlap))

    if current:
        segments.extend(_recursive_split(current, separators[1:], chunk_size, overlap))
    return segments


def _with_overlap(previous_chunk: str, next_chunk: str, chunk_size: int, overlap: int) -> str:
    """Prefix the next chunk with a bounded tail from the previous chunk."""
    if not previous_chunk or overlap <= 0:
        return next_chunk

    joiner = "\n\n"
    max_prefix_len = max(0, chunk_size - len(next_chunk) - len(joiner))
    if max_prefix_len <= 0:
        return next_chunk

    prefix = previous_chunk[-min(overlap, max_prefix_len) :].strip()
    if not prefix:
        return next_chunk

    combined = f"{prefix}{joiner}{next_chunk}"
    return combined if len(combined) <= chunk_size else next_chunk


def _filter_small_chunks(chunks: list[str], min_chunk_chars: int) -> list[str]:
    """Merge very small trailing chunks into the previous chunk when possible."""
    if not chunks:
        return []

    filtered: list[str] = []
    for chunk in chunks:
        if filtered and len(chunk) < min_chunk_chars:
            filtered[-1] = f"{filtered[-1]}\n\n{chunk}".strip()
            continue
        filtered.append(chunk)
    return filtered


def recursive_chunk_text(
    text: str,
    *,
    chunk_size_chars: int,
    chunk_overlap_chars: int,
    separators: list[str],
    min_chunk_chars: int,
) -> list[str]:
    """Split text into recursive chunks sized for downstream embedding and retrieval."""
    base_segments = _recursive_split(
        text,
        _normalize_separators(separators),
        chunk_size_chars,
        chunk_overlap_chars,
    )
    if not base_segments:
        return []

    chunked: list[str] = []
    previous = ""
    for segment in base_segments:
        candidate = _with_overlap(previous, segment, chunk_size_chars, chunk_overlap_chars)
        if len(candidate) > chunk_size_chars:
            windowed = _fixed_window_split(segment, chunk_size_chars, chunk_overlap_chars)
            chunked.extend(windowed)
            previous = windowed[-1] if windowed else previous
            continue
        chunked.append(candidate)
        previous = candidate

    return _filter_small_chunks(chunked, min_chunk_chars)


def build_chunk_records(
    source_path: Path,
    text: str,
    *,
    chunk_size_chars: int,
    chunk_overlap_chars: int,
    separators: list[str],
    min_chunk_chars: int,
) -> list[dict]:
    """Convert one source document into structured chunk records."""
    chunks = recursive_chunk_text(
        text,
        chunk_size_chars=chunk_size_chars,
        chunk_overlap_chars=chunk_overlap_chars,
        separators=separators,
        min_chunk_chars=min_chunk_chars,
    )
    records = []
    for index, chunk in enumerate(chunks):
        records.append(
            {
                "chunk_id": f"{source_path.stem}:{index:04d}",
                "chunk_index": index,
                "source_file": source_path.name,
                "source_path": str(source_path),
                "char_count": len(chunk),
                "content": chunk,
            }
        )
    return records


def write_chunk_file(
    source_path: Path,
    chunk_records: list[dict],
    output_dir: Path,
    *,
    chunk_size_chars: int,
    chunk_overlap_chars: int,
) -> Path:
    """Write one chunk manifest file for a source document."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{source_path.stem}.chunks.json"
    payload = {
        "source_file": source_path.name,
        "source_path": str(source_path),
        "chunk_count": len(chunk_records),
        "chunk_size_chars": chunk_size_chars,
        "chunk_overlap_chars": chunk_overlap_chars,
        "chunks": chunk_records,
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path


def chunk_source_file(source_path: Path, output_dir: Path | None = None) -> Path:
    """Chunk one configured source markdown file and persist its chunk manifest."""
    config = settings.chunking
    destination_dir = output_dir or _resolve_path(config.output_dir)
    text = source_path.read_text(encoding="utf-8")
    chunk_records = build_chunk_records(
        source_path,
        text,
        chunk_size_chars=config.chunk_size_chars,
        chunk_overlap_chars=config.chunk_overlap_chars,
        separators=config.separators,
        min_chunk_chars=config.min_chunk_chars,
    )
    return write_chunk_file(
        source_path,
        chunk_records,
        destination_dir,
        chunk_size_chars=config.chunk_size_chars,
        chunk_overlap_chars=config.chunk_overlap_chars,
    )


def chunk_configured_documents() -> list[Path]:
    """Chunk every configured source markdown document and return the output file paths."""
    config = settings.chunking
    if not config.enabled:
        return []

    source_dir = _resolve_path(config.source_dir)
    output_dir = _resolve_path(config.output_dir)
    output_paths = []
    for source_path in sorted(source_dir.glob(config.glob_pattern)):
        if source_path.is_file():
            output_paths.append(chunk_source_file(source_path, output_dir))
    return output_paths
