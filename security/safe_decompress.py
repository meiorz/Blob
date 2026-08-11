"""Bounded decompression for externally-supplied compressed data.

SKILL.md: "Treat all externally supplied compressed data as hostile."

The core vulnerability this defends against is trusting attacker-controlled
size declarations. A zstd frame header carries an OPTIONAL frameContentSize
field; a naive decoder reads it and preallocates that many bytes. A 30 KiB
frame can declare -- and legitimately expand to -- many gigabytes. The same
pattern exists in Parquet: a page header declares uncompressed_page_size and a
naive reader allocates it before validating anything.

Rule enforced here: NEVER allocate based on a declared size. Stream, count
actual bytes produced, and abort the moment a limit is crossed.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass


class MalformedCompressedInput(Exception):
    """Input is corrupt, truncated, or otherwise not a well-formed frame.

    Distinct from DecompressionLimitExceeded on purpose: a limit breach means
    "valid but too big", whereas this means "do not trust any of these bytes".
    Callers must never treat partial output from such an input as data.
    """


class DecompressionLimitExceeded(Exception):
    """Raised when a hostile or oversized input trips a configured limit."""

    def __init__(self, limit_name: str, limit_value, observed):
        super().__init__(f"{limit_name} exceeded: limit={limit_value} observed={observed}")
        self.limit_name = limit_name
        self.limit_value = limit_value
        self.observed = observed


@dataclass(frozen=True)
class DecompressionLimits:
    max_compressed_bytes: int = 256 * 1024 * 1024      # reject oversized inputs up front
    max_output_bytes: int = 1024 * 1024 * 1024         # hard cap on produced bytes
    max_expansion_ratio: float = 100.0                 # output/input ceiling
    timeout_s: float = 30.0                            # decoder cancellation
    chunk_bytes: int = 1 << 20                         # streaming granularity

    # Archive-shaped limits. Parquet page codecs are not archives, so these are
    # NOT APPLICABLE to workload P1 and are recorded as such rather than dropped;
    # they become live if the streaming-log workload (S1) ingests archives.
    max_entries: int | None = None
    max_nesting_depth: int | None = None


def safe_zstd_decompress(data: bytes, limits: DecompressionLimits = DecompressionLimits()) -> bytes:
    """Streaming zstd decode with hard caps. Never trusts frameContentSize.

    Frame completeness is verified explicitly. An earlier version used
    ZstdDecompressor.stream_reader(), which returns whatever it can decode and
    then reports EOF -- so a TRUNCATED frame came back as a short but
    successful result. That is an unsafe partial extraction: the caller cannot
    distinguish "this is the data" from "this is the prefix an attacker chose
    to give me". Caught by tests/test_hostile_inputs.py::test_zstd_truncated_and_corrupt.
    ZstdDecompressionObj.eof is the authoritative end-of-frame signal.
    """
    import zstandard as zstd

    if not data:
        raise MalformedCompressedInput("empty input is not a valid zstd frame")
    if len(data) > limits.max_compressed_bytes:
        raise DecompressionLimitExceeded("max_compressed_bytes",
                                         limits.max_compressed_bytes, len(data))

    # PHASE 1 -- output-bounded read.
    #
    # Two rejected alternatives, both measured:
    #   * ZstdDecompressor.decompress(data, max_output_size=N) does NOT cap a
    #     frame that declares its content size; it honours the declaration and
    #     allocates it. A 32 KiB frame declaring 1 GiB decompressed fully with
    #     max_output_size=16 MiB. The cap only applies to frames with no
    #     declared size. Do not use it as a security boundary.
    #   * ZstdDecompressionObj.decompress(chunk) bounds INPUT per call, not
    #     output: one 32 KiB input chunk produced 1 GiB in a single call
    #     (~4.7 s) before any limit could be evaluated.
    # stream_reader().read(n) is the only primitive here that bounds OUTPUT.
    started = time.monotonic()
    dctx = zstd.ZstdDecompressor()
    out = io.BytesIO()
    produced = 0
    try:
        with dctx.stream_reader(io.BytesIO(data)) as reader:
            while True:
                if time.monotonic() - started > limits.timeout_s:
                    raise DecompressionLimitExceeded("timeout_s", limits.timeout_s,
                                                     time.monotonic() - started)
                chunk = reader.read(limits.chunk_bytes)
                if not chunk:
                    break
                produced += len(chunk)
                if produced > limits.max_output_bytes:
                    raise DecompressionLimitExceeded("max_output_bytes",
                                                     limits.max_output_bytes, produced)
                if produced / len(data) > limits.max_expansion_ratio:
                    raise DecompressionLimitExceeded("max_expansion_ratio",
                                                     limits.max_expansion_ratio,
                                                     produced / len(data))
                out.write(chunk)
    except zstd.ZstdError as e:
        raise MalformedCompressedInput(f"zstd decode error: {e}") from e

    # PHASE 2 -- completeness.
    #
    # stream_reader reports a clean EOF for a TRUNCATED frame, so reaching this
    # point does not mean the frame ended properly. Verify explicitly.
    # Reaching here also means output stayed under the cap, so phase 2 is
    # itself bounded.
    declared = declared_frame_content_size(data)
    if declared is not None:
        # Used ONLY as an after-the-fact consistency check. It is never used to
        # size a buffer -- that is the vulnerability, not the defence.
        if produced != declared:
            raise MalformedCompressedInput(
                f"truncated zstd frame: produced {produced} bytes, frame declares "
                f"{declared}; refusing to return partial output")
    else:
        dobj = zstd.ZstdDecompressor().decompressobj()
        try:
            pos = 0
            while pos < len(data):
                piece = data[pos:pos + limits.chunk_bytes]
                pos += len(piece)
                dobj.decompress(piece)
                if dobj.eof:
                    break
        except zstd.ZstdError as e:
            raise MalformedCompressedInput(f"zstd decode error: {e}") from e
        if not dobj.eof:
            raise MalformedCompressedInput(
                f"truncated zstd frame: input exhausted after {len(data)} compressed "
                f"bytes ({produced} produced) without reaching end of frame; "
                "refusing to return partial output")
    return out.getvalue()


def declared_frame_content_size(data: bytes) -> int | None:
    """What the frame CLAIMS it will expand to. Diagnostic only.

    Never size a buffer from this. Exposed so tests can prove the gap between
    the attacker-controlled declaration and what the guarded decoder allocates.
    """
    import zstandard as zstd
    try:
        params = zstd.get_frame_parameters(data)
        size = params.content_size
        return None if size in (0, zstd.CONTENTSIZE_UNKNOWN) else size
    except Exception:
        return None


def safe_parquet_open(path_or_buf, limits: DecompressionLimits = DecompressionLimits()):
    """Open Parquet with footer validation before any page is decompressed.

    Reading metadata first means a malformed footer fails cheaply, and the
    declared uncompressed sizes can be checked against limits BEFORE the reader
    is asked to materialize anything.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    src = pa.BufferReader(path_or_buf) if isinstance(path_or_buf, (bytes, pa.Buffer)) else path_or_buf
    pf = pq.ParquetFile(src)             # raises cleanly on malformed/truncated footer
    md = pf.metadata
    declared = 0
    for rg in range(md.num_row_groups):
        g = md.row_group(rg)
        for c in range(g.num_columns):
            declared += g.column(c).total_uncompressed_size
    if declared > limits.max_output_bytes:
        raise DecompressionLimitExceeded("max_output_bytes(declared_uncompressed)",
                                         limits.max_output_bytes, declared)
    compressed = sum(
        md.row_group(rg).column(c).total_compressed_size
        for rg in range(md.num_row_groups)
        for c in range(md.row_group(rg).num_columns)
    )
    if compressed and declared / compressed > limits.max_expansion_ratio:
        raise DecompressionLimitExceeded("max_expansion_ratio(declared)",
                                         limits.max_expansion_ratio, declared / compressed)
    return pf
