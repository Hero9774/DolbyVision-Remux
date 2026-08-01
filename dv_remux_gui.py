"""
dv_remux_gui.py  v5.9.1
=======================
GUI tool: Dolby Vision MKV → MP4 remux + SRT subtitle extraction
For Jellyfin / LG TV

New in v5.9.1 (bug fix release, no new features):
  • Fixed three ways files could be destroyed:
    - A copy/move whose source and target resolved to the same path
      truncated the file to zero bytes and then deleted it (possible when
      the local working folder was set to the source folder).
    - The Dolby Vision box injection assumed moov was the last top-level
      box but never checked; on a file that was already faststarted it
      overwrote mdat and truncated the video away – while returning success.
    - The faststart step deleted the original before renaming the temporary
      file; a failed rename (common on Windows when a virus scanner or the
      Explorer briefly holds the new file) then removed the only remaining
      copy. Now an atomic os.replace().
  • The three remux workers are wrapped in try/except/finally, so the GUI
    always receives its completion signal. Previously an unexpected
    exception killed the thread silently and Start stayed disabled forever.
    The summary and the log file are now written after the workers' own
    cleanup, so the "original restored" messages reach the log file too.
  • Closing the window during a run no longer kills the worker instantly;
    it waits for the rollback and for the original MKV to be moved back.
  • The converter no longer sends two completion signals / writes its log
    file twice.
  • Simulation mode no longer creates directories, and it now verifies that
    ffprobe exists – without it every movie was silently reported as
    "not Dolby Vision".
  • P5→P8 now converts DTS to E-AC3 as well, warns about anamorphic
    material it cannot correct, and cleans up a half-written MP4 on failure.
  • dovi_tool is now found on Linux/macOS too (no hardcoded .exe).
  • The config file is written atomically and a damaged one is kept as .bak
    instead of being silently discarded.

New in v5.9.0:
  • Fixed: duplicate Dolby Vision configuration box. Current ffmpeg versions
    write a correct dvvC box themselves when muxing HEVC DV profile 8, but
    the presence check only looked for "dvcC" – so a second configuration
    box was injected into the same sample entry. Both names now count.
  • Fixed: the injected record was 8 bytes of payload instead of the
    required 24 (box 16 instead of 32 bytes) and was therefore malformed.
    It now matches byte for byte what ffmpeg writes, and it is named dvvC
    in hvc1/hev1 entries and dvcC in dvh1/dvhe entries, as the Dolby
    ISOBMFF specification requires.
  • Anamorphic sources (sample aspect ratio ≠ 1:1, e.g. 3840x2080 with SAR
    481:480) are corrected with -aspect, which rewrites only the container's
    pasp box – ffprobe, and therefore Jellyfin, gives it precedence over the
    VUI. Jellyfin otherwise refuses direct playback with "anamorphic video is
    not supported". Only applied when the deviation is at most 1 %; stronger
    anamorphic material (e.g. PAL DVD with SAR 16:15) is left untouched with a
    warning, since correcting it there would visibly distort the picture.
    NOTE: do not replace this with "-bsf:v hevc_metadata=sample_aspect_ratio
    =1/1". That filter re-serializes VPS/SPS/PPS, after which the Dolby Vision
    RPU no longer matches the parameter sets: ffmpeg still decodes the file,
    but the picture falls apart into colour smears on the TV (verified on an
    LG G4). The container route leaves every parameter set and RPU untouched.
  • DTS → E-AC3 (new toggle, on by default): ffmpeg can only store DTS inside
    MP4 as an mp4a sample entry with an esds descriptor – a proper dtsc entry
    is rejected by the muxer. Hardware players do not recognize that and
    refuse the entire file, even models that do support DTS (verified on an
    LG G4: the same file with E-AC3 plays fine, including Dolby Vision).
    Affected tracks are therefore converted to E-AC3 640k; every other audio
    track is still copied unchanged, as is the video.
  • Video converter (sub-program, button "🎬 Video converter" in the main
    window): scans a folder for video files, determines the resolution of
    each one and converts everything that is not already an MP4 into an
    LG-TV-friendly MP4 – in its own window with its own queues, stop event
    and log, so it runs independently of the remux run. All paths (source,
    working folder, backup folder, ffmpeg) are shared with the main window.
    MKV files are ignored here; they belong to the remux part.
    Uses the AMD AMF hardware encoder (hevc_amf / h264_amf) when available;
    availability is verified with a real one-second test encode at start
    instead of trusting "ffmpeg -encoders", and falls back to libx265/libx264
    per file if the hardware encoder refuses a source.
    Output profile: HEVC Main + hvc1 tag by default (H.264 High selectable),
    yuv420p, AAC audio (copy when the source is already AAC),
    -movflags +faststart and a fixed 2-second keyframe interval so seeking
    and fast-forward work on the TV. Rate control uses a target bitrate
    (peak VBR, 4 Mbit/s for 1080p25 HEVC at "medium", scaled by resolution
    and frame rate) rather than a constant quantizer, under which noisy old
    sources can end up larger than the original.
    The source resolution is preserved; anamorphic material (SAR ≠ 1:1,
    typical for MPEG/DVD/WMV) is rescaled to square pixels, and interlaced
    sources are deinterlaced (yadif), since AMF only encodes progressively.
    Like the remux pipeline, the conversion runs in the local working folder:
    the original is moved there, converted, only the finished MP4 is moved
    back to the source folder, and the original is deleted afterwards. If
    anything fails – encode error, failed transfer back, stop button – the
    original is moved back to its source location automatically.
    Existing MP4 targets and unknown file types are skipped and logged.

New in v5.8.0:
  • Multilingual support (German/English): dropdown in the title bar,
    switching the language takes effect immediately across the whole GUI
    (live re-render of all labels/buttons/dialogs) as well as on all
    log/error messages of the worker threads. All texts live in
    lang/de.json and lang/en.json (identical keys in both files, enforced
    by a startup check). Written .log files follow the language active at
    the time each line was produced. The language selection is stored in
    the config (key "sprache"). New central helper t(key, **kwargs) with a
    fallback chain (current language → default language → the key itself) and robust
    behaviour on missing format placeholders (no crash, unformatted text as
    fallback).
  • Code split into the package dv_remux/ (konstanten, config, sprache,
    mkv_analyse, mp4_binary, dateioperationen, pipeline, worker, gui) –
    a pure structural refactor with no behaviour change, so that future
    changes only need to touch the relevant module instead of the whole
    script. This script is now just the entry point.

New in v5.7.0:
  • Local working copy for NAS sources (optional, via toggle + path field,
    default working folder = Windows standard temp):
    The complete remux/P5→P8/dvcC/faststart pipeline runs in a local
    working folder instead of directly on the NAS, and only the finished
    MP4 is transferred back to the NAS target folder at the end. This
    dramatically reduces NAS network traffic, since faststart previously
    copied the complete file again NAS-internally. Before starting, the
    free disk space in the working folder is checked (buffer factor 2.3×
    source size); if there is too little space, the movie is skipped
    instead of aborting. If the transfer of the finished MP4 back to the
    NAS fails, it stays available locally (manually retrievable) instead of
    being lost. movie.nfo stays unchanged directly on the NAS, as do all
    extracted .srt files.
  • The original MKV is safely MOVED (not copied) NAS→working folder
    (prevents Jellyfin from seeing MKV+MP4 as duplicates in the same NAS
    folder during processing): the copy is first created completely and
    verified by file size, and the NAS original is only deleted afterwards
    – no data loss on abort/error during the copy operation. If any
    subsequent step fails (remux error, failed back-transfer, abort via
    stop button), the original MKV is automatically moved back to its NAS
    origin before the run ends.
  • The existing "Move MKV" toggle is now called "Create backup" (on =
    original is backed up, off = original is deleted without backup – same
    function, clearer label); the associated "MKV target" is now called
    "Backup folder". With the local working copy enabled, this backup step
    runs from the working folder instead of directly from the NAS.

New in v5.6.0:
  • CMv4.0 metadata on P5→P8 conversion (dovi_tool ≥ 2.3.3):
    Standard CMv4.0 extension metadata (L3, L9, L11, L254) is now added
    automatically to the converted RPU. This improves tone mapping on
    devices that support CMv4.0 (e.g. newer LG models).
    Implemented via dovi_tool --edit-config with add_cmv4_default_metadata=true,
    combined into step 2 of the existing 5-step pipeline.
    On older dovi_tool versions (<2.3.3) the option is silently ignored;
    the conversion still completes correctly.

New in v3:
  • NFO update: original_filename .mkv→.mp4, subtitle entries are replaced
    by the actually extracted SRT files
  • Backup of the original NFO as movie.nfo.bak before every change
  • XML structure, comments and tinyMediaManager metadata are preserved

New in v5.0.1:
  • TrueHD fallback: MKVs with a TrueHD Atmos track (not MP4-compatible)
    are automatically retried without the TrueHD stream – the EAC3 track
    is preserved. No manual intervention needed.

New in v5.0.2:
  • Close button (✖) + X button with a safety prompt when a process is
    running; config is saved on exit.
  • Autoscroll toggle in the log area: can be disabled to scroll through
    the log while a process is running.

New in v5.0.3:
  • ✕ button in the top right of the title bar (close protection).
  • Info button (ℹ) with GitHub link and contact address.
  • Hint texts anonymized (no real movie name as an example).
  • Secondary buttons made uniformly larger.

New in v5.3.0:
  • dvcC box (Dolby Vision Configuration Record) injected directly into the
    MP4: without dvcC, Jellyfin / LG TV does not recognize Dolby Vision in
    the MP4 container. The box is inserted after the mux via a Python binary
    patch (no faststart shift needed, since moov is at the end of the file).
    ffprobe then shows: dv_profile=8, dv_level=<auto>,
    dv_bl_signal_compatibility_id=1
  • 5-step pipeline for P5→P8: [1/5] HEVC, [2/5] dovi_tool P5→P8,
    [3/5] MP4 (no faststart), [4/5] inject dvcC, [5/5] faststart + mp42
  • Duplicate subtitles are skipped (SKIP entry in the log)

New in v5.2.0:
  • DV Profile 5 → Profile 8 conversion via dovi_tool (tools/dovi_tool.exe):
    Profile-5 MKVs (ICtCp/IPT-PQ-C2, typical for WEB-DL releases) cause a
    blue tint on devices without a native DV decoder. The tool converts the
    RPU automatically to Profile 8 (HDR10-compatible) – 3-step pipeline:
      1. extract HEVC  2. dovi_tool P5→P8  3. assemble MP4
    If dovi_tool is missing: warning in the log, normal remux continues.

New in v5.1.0:
  • old-MKV target selectable: "In movie folder" (as before) or "Global
    folder" – with a global folder all old MKVs go directly into the chosen
    path, no more "old MKV" subfolder in the source directory.

Requirements:
  - Python 3.8+  (tkinter is included with Python)
  - ffmpeg + ffprobe (https://ffmpeg.org/download.html)
    In simulation mode ffmpeg/ffprobe are NOT required.
"""

from dv_remux.konstanten import stelle_ordner_sicher
from dv_remux.sprache import sprachen_konsistenz_pruefen
from dv_remux.gui import App

if __name__ == "__main__":
    stelle_ordner_sicher()
    sprachen_konsistenz_pruefen()
    app = App()
    app.mainloop()
