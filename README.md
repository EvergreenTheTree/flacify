flacify
=======

Tool for converting all lossless uncompressed audio samples (WAV, AIFF) in a
directory into FLAC files. Intended to be used on large sample libraries.
Anecdotally, on my sample library of around 80 GB it was able to save about
27 GB with floating point normalization enabled.

This tool should work on all platforms where python exists and `ffmpeg` and
`ffprobe` are available, but I have only tested it on Linux.

**NOTICES**:
- **MAKE A BACKUP FIRST**. I make _no_ guarantees about what will happen to your sample library.
- This may take a while. On my sample library of around 80 GB and computer with a Ryzen 3700x (16 cores) + 16 GB of RAM it takes around 7 minutes with normalization enabled.
- By default this tool simply reports how much space would be saved and does not perform the conversion. Run it in this mode first to make sure there aren't any consequential ERROR or WARNING messages, then run it with the `-x` / `--no-dry-run` option.
- The `--normalize` options cause a lossy conversion to take place (floating point 32 bit WAV to 24 bit signed integer FLAC). In practice the distortion caused by this is well below the limit of human hearing, but it does lose information, hence why it is not the default.
- Not all programs support FLACs (including DAWs which would make this program useless to you). If you have things like impulse responses or multisampled instruments in your sample directory you will likely want to exclude them with the `-e` option.

If you wish to save a log of what happened, redirect stderr to a file (e.g. `flacify ... 2>log` in bash).
The log levels roughly have the following meaning:
- `ERROR`: The program cannot continue and must quit immediately. Currently this only happens when there are ffmpeg installation problems.
- `WARNING`: Something went wrong with an individual file.
- `INFO`: Generally used to output a message for each file processed. This is the default.
- `VERBOSE`: Includes ffmpeg version info, every command run, and extra details about each action taken.

