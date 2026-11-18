#!/usr/bin/env python
import functools
import os
import re
import shutil
import sys
import time
import typing
from argparse import ArgumentParser
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import chain
from math import floor, log10
from multiprocessing.pool import Pool
from pathlib import Path
from signal import SIGINT, signal
from subprocess import CalledProcessError, run
from tempfile import mkstemp

# If for some reason a single audio files has more than one ffmpeg stream
# (not audio channel, any number of audio channels is okay) that has audio in
# it, audio format information will be retrieved from the first audio stream.

# In theory flac tops out at 655350Hz sample rate, if you have wav files with
# sample rates that are more than that idk what to tell you.

LogLevel = typing.Literal[
    "ERROR",
    "WARNING",
    "INFO",
    "VERBOSE",
]
LOG_LEVELS: tuple[LogLevel, ...] = typing.get_args(LogLevel)


@dataclass
class Options:
    sample_directory: Path
    excluded_paths: list[re.Pattern]
    no_dry_run: bool
    jobs: int
    normalize_floats: bool
    normalize_doubles: bool
    normalize_peak_min: float
    normalize_peak_max: float
    ffmpeg_path: Path | None
    ffprobe_path: Path | None
    log_levels: tuple[LogLevel, ...]


def log(options: Options, level: LogLevel, message: typing.Any, file=sys.stderr):
    if level in options.log_levels:
        print(f"{level}: {message}", file=file)


def log_message(level: LogLevel, message: typing.Any) -> str:
    return f"{level}: {message}"


def parse_args(args: list[str]) -> Options:
    parser = ArgumentParser(
        prog="flacify",
        description="Converts all lossless uncompressed audio samples (wav, aiff) in a directory into FLAC files.",
        usage="%(prog)s [options] sample_directory",
        allow_abbrev=False,
    )
    parser.add_argument(
        "-e",
        "--exclude-path",
        type=re.compile,
        action="append",
        help="Exclude paths matching this regular expression from being processed (matches entire path including directories). Can be specified multiple times",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.process_cpu_count() - 1,
        help="Number of parallel jobs to use. Defaults to number of CPU cores minus 1",
    )
    parser.add_argument(
        "-x",
        "--no-dry-run",
        action="store_true",
        help="Proceed with converting to flac and removing corresponding wav files instead of just printing the space that would be saved",
    )
    parser.add_argument(
        "--normalize-floats",
        action="store_true",
        help="Normalize 32 bit floating point audio files with peaks under --normalize-dr-threshold to the -1.0 to 1.0 range prior to re-encoding. NOTE: this is a lossy conversion!",
    )
    parser.add_argument(
        "--normalize-doubles",
        action="store_true",
        help="Normalize 64 bit floating point audio files with peaks under --normalize-dr-threshold to the -1.0 to 1.0 range prior to re-encoding. NOTE: this is a lossy conversion!",
    )
    parser.add_argument(
        "--normalize-peak-max",
        type=float,
        default=15.0,
        help="When --normalize-floats or --normalize-doubles are specified, skip files with peaks in dB greater than this threshold. Default: 15.0",
    )
    parser.add_argument(
        "--normalize-peak-min",
        type=float,
        default=-30.0,
        help="When --normalize-floats or --normalize-doubles are specified, skip files where the maximum peaks in dB are below this threshold. Default: -30.0",
    )
    parser.add_argument(
        "--ffmpeg-path",
        type=Path,
        default=shutil.which("ffmpeg"),
        help="Path to the ffmpeg executable. Only required if it cannot be found in PATH",
    )
    parser.add_argument(
        "--ffprobe-path",
        type=Path,
        default=shutil.which("ffprobe"),
        help="Path to the ffprobe executable. Only required if it cannot be found in PATH",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=LOG_LEVELS,
        default="INFO",
        help="Maximum level of log messages to include. Defaults to INFO",
    )
    parser.add_argument(
        "sample_directory",
        type=Path,
        help="Process lossless uncompressed audio samples from this directory",
    )
    parsed_args = parser.parse_args(args)
    log_level_i = LOG_LEVELS.index(parsed_args.log_level)
    return Options(
        sample_directory=parsed_args.sample_directory,
        excluded_paths=parsed_args.exclude_path if parsed_args.exclude_path else [],
        no_dry_run=parsed_args.no_dry_run,
        normalize_floats=parsed_args.normalize_floats,
        normalize_doubles=parsed_args.normalize_doubles,
        normalize_peak_min=parsed_args.normalize_peak_min,
        normalize_peak_max=parsed_args.normalize_peak_max,
        jobs=parsed_args.jobs,
        ffmpeg_path=parsed_args.ffmpeg_path,
        ffprobe_path=parsed_args.ffprobe_path,
        log_levels=LOG_LEVELS[: log_level_i + 1],
    )


@dataclass(slots=True)
class Result:
    bytes_saved: int
    message: Sequence[str]


def flacify_file(options: Options, fp: Path) -> Result:
    if fp.name.startswith("."):
        return Result(0, log_message("INFO", f"Skipping hidden file {fp}"))

    message = []

    # Process exclusions
    excluded = False
    for pattern in options.excluded_paths:
        if pattern.search(str(fp)):
            excluded = True
            break
    if excluded:
        message.append(log_message("INFO", f"Skipping excluded file {fp}"))
        return Result(0, message)

    # Handle existing files
    out_fp = fp.with_suffix(".flac")
    if out_fp.exists():
        if options.no_dry_run:
            message.append(
                log_message(
                    "INFO",
                    f"Converted file {out_fp} appears to already exist; removing unconverted file {fp} and continuing",
                )
            )
            os.remove(fp)
        else:
            message.append(
                log_message(
                    "INFO",
                    f"Converted file {out_fp} appears to already exist; would remove unconverted file {fp} before continuing",
                )
            )
        return Result(0, message)

    # Retrieve audio codec information from file
    try:
        command = (
            str(options.ffprobe_path),
            "-hide_banner",
            "-show_streams",
            "-select_streams",
            "a",
            str(fp),
        )
        message.append(
            log_message("VERBOSE", f"Retrieving audio codec information for file {fp}")
        )
        message.append(log_message("VERBOSE", command))
        out = run(command, capture_output=True, check=True)
    except CalledProcessError as e:
        message.append(
            log_message(
                "VERBOSE",
                f"Failed to get codec info for file {fp}; skipping\nffprobe output: {e.stderr.decode().strip()}",
            )
        )
        return Result(0, message)

    # Parse out audio codec metadata
    sample_fmt_match = re.search(
        r"^.*sample_fmt=(.*)", out.stdout.decode(), flags=re.MULTILINE
    )
    try:
        if sample_fmt_match:
            sample_fmt = sample_fmt_match.group(1).strip()
        else:
            raise ValueError
    except ValueError:
        message.append(
            log_message(
                "WARNING",
                f"Failed to get sample format for file {fp}; skipping\nffmpeg output: {out.stdout.decode().strip()}",
            )
        )
        return Result(0, message)
    bit_depth_match = re.search(
        r"^.*bits_per_sample=(.*)", out.stdout.decode(), flags=re.MULTILINE
    )
    try:
        if bit_depth_match:
            bit_depth = int(bit_depth_match.group(1).strip())
        else:
            raise ValueError
    except ValueError:
        message.append(
            log_message(
                "WARNING",
                f"Failed to get bit depth for file {fp}; skipping\nffmpeg output: {out.stdout.decode()}",
            )
        )
        return Result(0, message)

    # Skip formats and bit depths we shouldn't process
    if sample_fmt.startswith(("s", "u")) and bit_depth not in (4, 8, 16, 24, 32):
        message.append(
            log_message(
                "INFO",
                f"Skipping unsupported integer audio sample format bit depth combination in {fp}: {sample_fmt} {bit_depth} bit",
            )
        )
        return Result(0, message)

    if not options.normalize_floats and sample_fmt.startswith("flt"):
        message.append(
            log_message(
                "INFO",
                f"Skipping 32 bit floating point audio file {fp} since --normalize-floats was not passed",
            )
        )
        return Result(0, message)
    if not options.normalize_doubles and sample_fmt.startswith("dbl"):
        message.append(
            log_message(
                "INFO",
                f"Skipping 64 bit floating point audio file {fp} since --normalize-doubles was not passed",
            )
        )
        return Result(0, message)

    # Get peak level of floating point files and add gain reduction filter
    extra_filters = []
    if (options.normalize_floats and sample_fmt.startswith("flt")) or (
        options.normalize_doubles and sample_fmt.startswith("dbl")
    ):
        astats_filter = "astats=measure_overall=Peak_level:measure_perchannel=none"
        ametadata_filter = "ametadata=print:key=lavfi.astats.Overall.Peak_level"
        try:
            command = (
                str(options.ffmpeg_path),
                "-hide_banner",
                "-i",
                str(fp),
                "-af",
                f"{astats_filter},{ametadata_filter}",
                "-f",
                "null",
                "-",
            )
            message.append(
                log_message(
                    "VERBOSE", "Checking peak amplitude of floating-point file {fp}"
                )
            )
            message.append(log_message("VERBOSE", command))
            out = run(command, capture_output=True, check=True)
        except CalledProcessError as e:
            message.append(
                log_message(
                    "WARNING",
                    f"Failed to get peak db for floating point file {fp}; skipping\nffmpeg output: {e.stderr.decode()}",
                )
            )
            return Result(0, message)
        peak_db_match = re.search(
            r"^.*Peak level dB:(.*)$", out.stderr.decode(), flags=re.MULTILINE
        )
        if peak_db_match:
            peak_db_str = peak_db_match.group(1).strip()
            peak_db = float(peak_db_str)
            if peak_db > options.normalize_peak_max:
                message.append(
                    log_message(
                        "INFO",
                        f"Skipping file {fp} with peak dB {peak_db} which is over the configured maximum threshold",
                    )
                )
                return Result(0, message)
            if peak_db < options.normalize_peak_min:
                message.append(
                    log_message(
                        "INFO",
                        f"Skipping file {fp} with peak dB {peak_db} which is under the configured minimum threshold",
                    )
                )
                return Result(0, message)
            # wack negation
            if peak_db > 0:
                gain_reduction = "-" + peak_db_str
                extra_filters.extend(("-af", f"volume={gain_reduction}dB"))
                message.append(
                    log_message(
                        "VERBOSE",
                        f"Avoiding clipping by reducing volume of {fp} by {gain_reduction}dB",
                    )
                )

    # Actually perform the conversion
    out_fd, out_fn = mkstemp(suffix=".flac")
    os.close(out_fd)
    try:
        command = (
            str(options.ffmpeg_path),
            "-hide_banner",
            "-y",
            "-i",
            str(fp),
            *extra_filters,
            out_fn,
        )
        message.append(log_message("VERBOSE", command))
        run(command, capture_output=True, check=True)
    except CalledProcessError as e:
        message.append(
            log_message(
                "WARNING",
                f"Failed to convert file {fp} to flac; skipping\nffmpeg output: {e.stderr.decode()}",
            )
        )
        return Result(0, message)

    orig_size = fp.stat().st_size
    new_size = os.stat(out_fn).st_size

    saved = orig_size - new_size
    if saved > 0:
        message.append(
            log_message(
                "INFO",
                f"Processed file {fp}; saved {format_byte_count(saved, pad=False)}",
            )
        )

        if options.no_dry_run:
            message.append(
                log_message(
                    "INFO", f"Copying temp file {out_fn} to {out_fp} and removing {fp}"
                )
            )
            shutil.copy(out_fn, out_fp)
            shutil.copystat(fp, out_fp)
            shutil.copymode(fp, out_fp)
            os.remove(fp)
        else:
            message.append(
                log_message(
                    "INFO", f"Would copy temp file {out_fn} to {out_fp} and remove {fp}"
                )
            )

        os.remove(out_fn)
        return Result(saved, message)

    message.append(
        log_message(
            "INFO", f"Would not save any space by converting file {fp}; skipped"
        )
    )
    os.remove(out_fn)
    return Result(0, message)


def format_byte_count(byte_count: int, pad=True) -> str:
    suffixes = [
        ("G", 1 << 30),
        ("M", 1 << 20),
        ("K", 1 << 10),
        ("B", 1),
    ]

    for suffix, size in suffixes:
        if byte_count > size:
            if pad:
                return f"{byte_count / size:>8.2f} {suffix}"
            else:
                return f"{byte_count / size:.2f} {suffix}"

    return "0"


def initialize_worker():
    # Catch ctrl+C to finish work first
    signal(SIGINT, lambda _, __: None)


def main():
    options = parse_args(sys.argv[1:])
    if options.ffmpeg_path is None:
        log(
            options,
            "ERROR",
            "could not automatically determine ffmpeg path, please pass it manually with --ffmpeg-path",
        )
        return 1
    else:
        try:
            command = (options.ffmpeg_path, "-version")
            log(options, "VERBOSE", command)
            out = run(command, capture_output=True, check=True)
            log(options, "VERBOSE", out.stdout.decode().strip())
        except CalledProcessError as e:
            log(options, "ERROR", f"Attempt to run ffmpeg failed\noutput: {e.stderr.decode()}")
    if options.ffprobe_path is None:
        print(
            "ERROR",
            "could not automatically determine ffprobe path, please pass it manually with --ffprobe-path",
        )
        return 1
    else:
        try:
            command = (options.ffprobe_path, "-version")
            log(options, "VERBOSE", command)
            out = run(command, capture_output=True, check=True)
            log(options, "VERBOSE", out.stdout.decode().strip())
        except CalledProcessError as e:
            log(options, "ERROR", f"Attempt to run ffprobe failed\noutput: {e.stderr.decode()}")

    lossless_file_extensions = ("wav", "aif")
    lossless_file_paths = list(
        chain.from_iterable(
            options.sample_directory.rglob("*." + ext, case_sensitive=False)
            for ext in lossless_file_extensions
        )
    )
    cols, _ = shutil.get_terminal_size()
    n_processed = 0
    n_total = len(lossless_file_paths)
    bytes_saved_total = 0
    pad = floor(log10(len(lossless_file_paths))) + 1
    last_status_message = f"\r{n_processed:>{pad}}/{n_total}: {format_byte_count(bytes_saved_total)} {'' if options.no_dry_run else 'would be '}saved"
    last_status_time = float("-inf")
    try:
        with Pool(processes=options.jobs, initializer=initialize_worker) as pool:
            for result in pool.imap_unordered(
                functools.partial(flacify_file, options),
                lossless_file_paths,
                chunksize=16,
            ):
                n_processed += 1
                bytes_saved_total += result.bytes_saved

                if result.message:
                    lines = [
                        l for l in result.message if l.startswith(options.log_levels)
                    ]
                    if sys.stderr.isatty() and sys.stdout.isatty() and len(lines) > 0:
                        # Clear last status message
                        print("\r" + " " * cols, end="\r")

                    for line in lines:
                        print(line, file=sys.stderr)

                    if sys.stderr.isatty() and sys.stdout.isatty() and len(lines) > 0:
                        # Keep last status message at the bottom
                        print(last_status_message, end="")

                now = time.time()
                # Update status every second or on last iteration
                if sys.stdout.isatty() and (
                    now - last_status_time > 1 or n_processed == n_total
                ):
                    last_status_time = now
                    last_status_message = f"\r{n_processed:>{pad}}/{n_total}: {format_byte_count(bytes_saved_total)} {'' if options.no_dry_run else 'would be '}saved"
                    print(last_status_message, end="")
    except KeyboardInterrupt:
        return 130

    print()
    if options.no_dry_run:
        print(f"Saved {format_byte_count(bytes_saved_total, pad=False)}!")
    else:
        print(f"Would save {format_byte_count(bytes_saved_total, pad=False)}!")

    return 0


if __name__ == "__main__":
    sys.exit(main())
