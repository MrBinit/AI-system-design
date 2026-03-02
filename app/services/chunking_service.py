import json
from pathlib import Path

from app.core.config import get_settings
from app.core.paths import resolve_project_path

settings = get_settings()

_DEGREE_PATTERNS = (
    ("bachelor", ("bachelor of", "b.sc.")),
    ("masters", ("master of", "m.sc.")),
    ("phd", ("phd", "doctor of")),
)


def _resolve_path(path_value: str) -> Path:
    """Resolve a configured path relative to the project root when needed."""
    return resolve_project_path(path_value)


def _normalize_text_line(text: str) -> str:
    """Collapse repeated whitespace in one line of extracted text."""
    return " ".join(text.strip().split())


def _extract_document_metadata(source_path: Path, text: str) -> dict:
    """Extract stable document-level metadata from a university source file."""
    lines = [_normalize_text_line(line) for line in text.splitlines() if _normalize_text_line(line)]
    metadata = {
        "document_id": source_path.stem,
        "document_title": lines[0] if lines else source_path.stem,
        "source_type": "university_profile",
        "university": lines[0] if lines else source_path.stem,
        "location": "",
        "city": "",
        "country": "",
        "university_type": "",
        "founded": "",
    }

    for line in lines[1:12]:
        if line.startswith("Location:"):
            value = line.split(":", 1)[1].strip()
            metadata["location"] = value
            parts = [part.strip() for part in value.split(",") if part.strip()]
            if parts:
                metadata["city"] = parts[0]
                metadata["country"] = parts[-1]
        elif line.startswith("Type:"):
            metadata["university_type"] = line.split(":", 1)[1].strip()
        elif line.startswith("Founded:"):
            metadata["founded"] = line.split(":", 1)[1].strip()

    return metadata


def _normalize_separators(separators: list[str]) -> list[str]:
    """Ensure the recursive splitter always has a final character-level fallback."""
    cleaned = [separator for separator in separators if isinstance(separator, str)]
    if "" not in cleaned:
        cleaned.append("")
    return cleaned or [""]


def _fixed_window_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping windows, snapping cuts to whitespace when possible."""
    stripped = text.strip()
    if not stripped:
        return []

    if len(stripped) <= chunk_size:
        return [stripped]

    chunks = []
    start = 0
    while start < len(stripped):
        end = min(start + chunk_size, len(stripped))
        if end < len(stripped):
            boundary = max(
                stripped.rfind("\n", start + 1, end),
                stripped.rfind(" ", start + 1, end),
            )
            if boundary > start:
                end = boundary

        chunk = stripped[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(stripped):
            break

        next_start = max(0, end - overlap)
        if (
            next_start > 0
            and next_start < len(stripped)
            and not stripped[next_start - 1].isspace()
            and not stripped[next_start].isspace()
        ):
            while next_start < len(stripped) and not stripped[next_start].isspace():
                next_start += 1
            while next_start < len(stripped) and stripped[next_start].isspace():
                next_start += 1
        start = next_start
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


def _is_heading_marker(paragraph: str) -> bool:
    """Detect explicit markdown heading markers."""
    return paragraph.startswith("# ")


def _is_heading_like_paragraph(paragraph: str) -> bool:
    """Detect plain-text section headings commonly found in the source documents."""
    normalized = " ".join(paragraph.strip().split())
    if not normalized:
        return False
    if _is_heading_marker(normalized):
        return True
    if "\n" in normalized:
        return False
    if len(normalized) > 120:
        return False

    lowered = normalized.lower()
    explicit_headings = {
        "university overview",
        "program description",
        "program overview",
        "admission requirements",
        "faculty highlights",
        "research environment",
        "research infrastructure",
        "international opportunities",
        "career outcomes",
        "core courses",
        "electives",
        "thesis",
        "focus areas",
        "research areas",
        "research focus",
    }
    if lowered in explicit_headings:
        return True

    if lowered.startswith(("bachelor of ", "master of ", "phd in ", "doctor of ")):
        return True

    if lowered.endswith((" lab", " group")) and len(normalized.split()) <= 8:
        return True

    words = normalized.replace(":", "").split()
    if len(words) > 12:
        return False
    return normalized.endswith(":")


def _extract_section_heading(chunk: str) -> str:
    """Extract the most likely section heading associated with a chunk."""
    paragraphs = [paragraph.strip() for paragraph in chunk.split("\n\n") if paragraph.strip()]
    for paragraph in paragraphs[:3]:
        if _is_heading_like_paragraph(paragraph):
            return _normalize_text_line(paragraph)
    return ""


def _extract_degree_level(chunk: str) -> str:
    """Infer the academic level represented by a chunk."""
    lowered = chunk.lower()
    for degree_level, patterns in _DEGREE_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return degree_level
    return ""


def _infer_entity_type(chunk: str, section_heading: str, degree_level: str) -> str:
    """Classify a chunk into a coarse retrieval entity type."""
    lowered = chunk.lower()
    heading_lower = section_heading.lower()

    if degree_level:
        return "program"
    if "prof. dr." in lowered or heading_lower == "faculty highlights":
        return "faculty"
    if heading_lower.endswith((" lab", " group")) or "research lab" in lowered or "research group" in lowered:
        return "lab"
    if heading_lower in {"admission requirements", "program description", "program overview", "core courses", "electives", "thesis"}:
        return "program_section"
    return "university_section"


def _build_chunk_metadata(chunk: str, document_metadata: dict) -> dict:
    """Build per-chunk metadata used for filtering and retrieval later."""
    section_heading = _extract_section_heading(chunk)
    degree_level = _extract_degree_level(chunk)
    return {
        "document_id": document_metadata["document_id"],
        "document_title": document_metadata["document_title"],
        "source_type": document_metadata["source_type"],
        "university": document_metadata["university"],
        "location": document_metadata["location"],
        "city": document_metadata["city"],
        "country": document_metadata["country"],
        "section_heading": section_heading,
        "degree_level": degree_level,
        "entity_type": _infer_entity_type(chunk, section_heading, degree_level),
    }


def _split_structural_sections(text: str) -> list[str]:
    """Split a document into semantic sections before recursive chunking."""
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    if not paragraphs:
        return []

    sections: list[str] = []
    current: list[str] = []
    for paragraph in paragraphs:
        if _is_heading_like_paragraph(paragraph):
            if current:
                sections.append("\n\n".join(current).strip())
                current = []
            current.append(paragraph)
            continue
        current.append(paragraph)

    if current:
        sections.append("\n\n".join(current).strip())
    return sections


def _extract_overlap_prefix(previous_chunk: str, overlap: int) -> str:
    """Extract a clean overlap prefix that starts on a whitespace boundary."""
    if not previous_chunk or overlap <= 0:
        return ""

    start = max(0, len(previous_chunk) - overlap)
    if (
        start > 0
        and start < len(previous_chunk)
        and not previous_chunk[start - 1].isspace()
        and not previous_chunk[start].isspace()
    ):
        while start < len(previous_chunk) and not previous_chunk[start].isspace():
            start += 1
        while start < len(previous_chunk) and previous_chunk[start].isspace():
            start += 1
    return previous_chunk[start:].strip()


def _with_overlap(previous_chunk: str, next_chunk: str, chunk_size: int, overlap: int) -> str:
    """Prefix the next chunk with a bounded tail from the previous chunk."""
    if not previous_chunk or overlap <= 0:
        return next_chunk

    joiner = "\n\n"
    max_prefix_len = max(0, chunk_size - len(next_chunk) - len(joiner))
    if max_prefix_len <= 0:
        return next_chunk

    prefix = _extract_overlap_prefix(previous_chunk, min(overlap, max_prefix_len))
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


def _merge_tiny_chunks_forward(
    chunks: list[str],
    *,
    merge_below_chars: int,
    chunk_size_chars: int,
) -> list[str]:
    """Merge tiny chunks into the next chunk when the combined size still fits."""
    if not chunks:
        return []

    merged: list[str] = []
    index = 0
    joiner = "\n\n"
    while index < len(chunks):
        current = chunks[index]
        if (
            len(current) < merge_below_chars
            and index + 1 < len(chunks)
        ):
            combined = f"{current}{joiner}{chunks[index + 1]}".strip()
            if len(combined) <= chunk_size_chars:
                merged.append(combined)
                index += 2
                continue

        merged.append(current)
        index += 1
    return merged


def recursive_chunk_text(
    text: str,
    *,
    chunk_size_chars: int,
    chunk_overlap_chars: int,
    separators: list[str],
    min_chunk_chars: int,
    merge_forward_below_chars: int = 250,
) -> list[str]:
    """Split text into recursive chunks sized for downstream embedding and retrieval."""
    sections = _split_structural_sections(text)
    if not sections:
        return []

    final_chunks: list[str] = []
    normalized_separators = _normalize_separators(separators)

    for section in sections:
        base_segments = _recursive_split(
            section,
            normalized_separators,
            chunk_size_chars,
            chunk_overlap_chars,
        )
        if not base_segments:
            continue

        section_chunks: list[str] = []
        previous = ""
        for segment in base_segments:
            candidate = _with_overlap(previous, segment, chunk_size_chars, chunk_overlap_chars)
            if len(candidate) > chunk_size_chars:
                windowed = _fixed_window_split(segment, chunk_size_chars, chunk_overlap_chars)
                section_chunks.extend(windowed)
                previous = windowed[-1] if windowed else previous
                continue
            section_chunks.append(candidate)
            previous = candidate

        section_chunks = _merge_tiny_chunks_forward(
            section_chunks,
            merge_below_chars=merge_forward_below_chars,
            chunk_size_chars=chunk_size_chars,
        )
        final_chunks.extend(_filter_small_chunks(section_chunks, min_chunk_chars))

    return final_chunks


def build_chunk_records(
    source_path: Path,
    text: str,
    *,
    chunk_size_chars: int,
    chunk_overlap_chars: int,
    separators: list[str],
    min_chunk_chars: int,
    merge_forward_below_chars: int = 250,
    document_metadata: dict | None = None,
) -> list[dict]:
    """Convert one source document into structured chunk records."""
    resolved_document_metadata = document_metadata or _extract_document_metadata(source_path, text)
    chunks = recursive_chunk_text(
        text,
        chunk_size_chars=chunk_size_chars,
        chunk_overlap_chars=chunk_overlap_chars,
        separators=separators,
        min_chunk_chars=min_chunk_chars,
        merge_forward_below_chars=merge_forward_below_chars,
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
                "metadata": _build_chunk_metadata(chunk, resolved_document_metadata),
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
    document_metadata: dict | None = None,
) -> Path:
    """Write one chunk manifest file for a source document."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{source_path.stem}.chunks.json"
    payload = {
        "source_file": source_path.name,
        "source_path": str(source_path),
        "document_metadata": document_metadata or {},
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
    document_metadata = _extract_document_metadata(source_path, text)
    chunk_records = build_chunk_records(
        source_path,
        text,
        chunk_size_chars=config.chunk_size_chars,
        chunk_overlap_chars=config.chunk_overlap_chars,
        separators=config.separators,
        min_chunk_chars=config.min_chunk_chars,
        merge_forward_below_chars=config.merge_forward_below_chars,
        document_metadata=document_metadata,
    )
    return write_chunk_file(
        source_path,
        chunk_records,
        destination_dir,
        chunk_size_chars=config.chunk_size_chars,
        chunk_overlap_chars=config.chunk_overlap_chars,
        document_metadata=document_metadata,
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
