from pydantic import BaseModel, Field, model_validator


class ChunkingConfig(BaseModel):
    """Define how source documents should be recursively chunked into retrieval units."""

    enabled: bool = True
    source_dir: str = "data/raw"
    output_dir: str = "data/chunks"
    glob_pattern: str = "*.md"
    chunk_size_chars: int = Field(default=900, ge=100, le=100000)
    chunk_overlap_chars: int = Field(default=120, ge=0, le=10000)
    min_chunk_chars: int = Field(default=300, ge=1, le=10000)
    merge_forward_below_chars: int = Field(default=250, ge=1, le=10000)
    separators: list[str] = Field(
        default_factory=lambda: ["\n\n", "\n", ". ", " ", ""],
    )

    @model_validator(mode="after")
    def validate_overlap(self):
        """Ensure overlap stays smaller than the configured chunk size."""
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError("chunk_overlap_chars must be smaller than chunk_size_chars")
        if self.merge_forward_below_chars >= self.chunk_size_chars:
            raise ValueError("merge_forward_below_chars must be smaller than chunk_size_chars")
        return self
