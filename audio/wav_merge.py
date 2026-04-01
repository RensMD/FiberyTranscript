"""Merge multiple WAV segments into a single file with silence gaps."""

import logging
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Read/write in 64K-frame chunks to avoid loading entire files into memory
_CHUNK_FRAMES = 64_000


def _get_wav_format(path: Path) -> tuple[int, int, int]:
    """Return (channels, sample_width, frame_rate) for a WAV file."""
    with wave.open(str(path), "rb") as wav_file:
        return (
            wav_file.getnchannels(),
            wav_file.getsampwidth(),
            wav_file.getframerate(),
        )


def merge_wav_files(
    segments: list[Path],
    output_path: Optional[Path] = None,
    silence_seconds: float = 2.0,
) -> Path:
    """Merge multiple WAV segments into one file, inserting silence between them.

    All segments must share the same format (16kHz, 16-bit PCM).

    Args:
        segments: Ordered list of WAV file paths to merge.
        output_path: Where to write the merged file. Defaults to
            ``segments[0].parent / "merged_<stem>.wav"``.
        silence_seconds: Seconds of silence to insert between segments.

    Returns:
        Path to the merged WAV file.
    """
    if not segments:
        raise ValueError("No segments to merge")
    if len(segments) == 1:
        return segments[0]

    if output_path is None:
        output_path = segments[0].parent / f"merged_{segments[0].stem}.wav"

    # Read format from first segment
    n_channels, sampwidth, framerate = _get_wav_format(segments[0])

    for seg_path in segments[1:]:
        seg_format = _get_wav_format(seg_path)
        if seg_format != (n_channels, sampwidth, framerate):
            raise ValueError(
                "Cannot merge WAV segments with different formats: "
                f"{segments[0].name}={(n_channels, sampwidth, framerate)} "
                f"but {seg_path.name}={seg_format}"
            )

    silence_frames = int(framerate * silence_seconds)
    silence_bytes = b"\x00" * (silence_frames * n_channels * sampwidth)

    with wave.open(str(output_path), "wb") as out:
        out.setnchannels(n_channels)
        out.setsampwidth(sampwidth)
        out.setframerate(framerate)

        for i, seg_path in enumerate(segments):
            with wave.open(str(seg_path), "rb") as seg:
                while True:
                    frames = seg.readframes(_CHUNK_FRAMES)
                    if not frames:
                        break
                    out.writeframes(frames)

            # Insert silence between segments (not after the last one)
            if i < len(segments) - 1:
                out.writeframes(silence_bytes)

    logger.info(
        "Merged %d segments into %s (%.1f s silence between each)",
        len(segments), output_path.name, silence_seconds,
    )
    return output_path
