#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Acquisitore VHS - digitalizzazione di videocassette per Linux.

Interfaccia grafica leggera (Tkinter) attorno a ffmpeg per acquisire
video e audio da schede di acquisizione USB (EasyCAP, UTV007, EM28xx,
MS2109, stk1160 ...) viste dal sistema come webcam (Video4Linux2).

Dipendenze: python3 (>= 3.6), python3-tk, ffmpeg.
Facoltativo: v4l-utils (v4l2-ctl) per ingressi, standard TV e regolazioni
immagine; pulseaudio-utils (pactl) per le sorgenti PulseAudio/PipeWire.
"""

import argparse
import array
import collections
import datetime
import glob
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

APP_NAME = "Acquisitore VHS"
VERSION = "1.0.0"

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "acquisitore")
CONFIG_FILE = os.path.join(CONFIG_DIR, "impostazioni.json")

TEST_SOURCE = "test"
NO_AUDIO = "none"
CARD_AUDIO = "card"   # audio della scheda di acquisizione stessa (rilevato a ogni avvio)


def default_output_dir():
    for name in ("Video", "Videos", "Filmati"):
        p = os.path.expanduser("~/" + name)
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


DEFAULTS = {
    "video_device": "",
    "video_device_manual": False,
    "video_input": "",
    "standard": "PAL",
    "input_format": "",
    "video_size": "720x576",
    "framerate": "25",
    "audio_device": CARD_AUDIO,
    "audio_channels": "auto",
    "audio_rate": "auto",
    "audio_monitor": False,
    "output_dir": default_output_dir(),
    "name_template": "Cassetta_%Y-%m-%d_%H-%M",
    "preset": "mjpeg",
    "container": "mkv",
    "deinterlace": "none",
    "head_mask": False,
    "aspect": "4:3",
    "max_minutes": "0",
    "auto_stop_nosignal": True,
    "nosignal_minutes": "2",
    "preview_size": "small",
    "preview_while_recording": True,
    "controls": {},
    "conv_crf": "22",
    "conv_speed": "veryfast",
    "conv_deinterlace": True,
    "split_minutes": "0",
    "auto_recover": True,
}

SPLIT_VALUES = collections.OrderedDict([
    ("0", "Un unico file"), ("30", "Ogni 30 minuti"), ("60", "Ogni ora"),
])

STANDARDS = ["Automatico", "PAL", "NTSC", "SECAM", "PAL-M", "PAL-Nc", "NTSC-JP"]

FPS_VALUES = collections.OrderedDict([
    ("25", "25"), ("29.97", "30000/1001"), ("30", "30"),
    ("50", "50"), ("59.94", "60000/1001"), ("15", "15"),
])

DEFAULT_SIZES = ["720x576", "720x480", "704x576", "640x480", "352x288", "320x240"]

# Preset di codifica: (etichetta, argomenti video, argomenti audio, encoder richiesto)
PRESETS = collections.OrderedDict([
    ("mjpeg", ("MJPEG alta qualità - poca CPU, file grandi (consigliato)",
               ["-c:v", "mjpeg", "-q:v", "3", "-pix_fmt", "yuvj422p"],
               ["-c:a", "pcm_s16le"], "mjpeg")),
    ("x264fast", ("H.264 veloce - CPU media, file medi",
                  ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                   "-pix_fmt", "yuv420p"],
                  ["-c:a", "aac", "-b:a", "192k"], "libx264")),
    ("x264", ("H.264 compatto - CPU alta, file piccoli",
              ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p"],
              ["-c:a", "aac", "-b:a", "192k"], "libx264")),
    ("ffv1", ("FFV1 senza perdita - archivio, file enormi",
              ["-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "4",
               "-slicecrc", "1"],
              ["-c:a", "flac"], "ffv1")),
    ("copy", ("Copia diretta (solo se la scheda invia MJPEG/H.264)",
              ["-c:v", "copy"], ["-c:a", "pcm_s16le"], None)),
])

DEINTERLACE = collections.OrderedDict([
    ("none", "Nessuno (consigliato per l'archivio)"),
    ("yadif", "yadif (veloce)"),
    ("bwdif", "bwdif (qualità migliore, più CPU)"),
])

MAX_DURATIONS = collections.OrderedDict([
    ("0", "Nessun limite"), ("30", "30 minuti"), ("60", "1 ora (E-60)"),
    ("95", "1 ora 35 (E-90)"), ("125", "2 ore 5 (E-120)"),
    ("185", "3 ore 5 (E-180)"), ("245", "4 ore 5 (E-240)"),
    ("365", "6 ore 5 (LP E-180)"),
])

PREVIEW_SIZES = {"small": 384, "large": 576}


# --------------------------------------------------------------------------
# Utilità di sistema
# --------------------------------------------------------------------------

def run_cmd(args, timeout=10):
    """Esegue un comando e restituisce (codice, output combinato)."""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, universal_newlines=True,
                           errors="replace", timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def read_file(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def fmt_hms(seconds):
    seconds = max(0, int(seconds))
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds // 60) % 60, seconds % 60)


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.2f %s") % (n, unit)
        n /= 1024.0


def usb_parent(sys_path):
    """Percorso del dispositivo USB fisico a cui appartiene un nodo sysfs."""
    try:
        real = os.path.realpath(sys_path)
    except OSError:
        return None
    # .../usb1/1-1/1-1:1.0  -> .../usb1/1-1
    while real and real != "/":
        base = os.path.basename(real)
        if re.match(r"^\d+-[\d.]+$", base):
            return real
        real = os.path.dirname(real)
    return None


def filesystem_type(path):
    """Tipo di filesystem (es. 'ext4', 'vfat') della cartella indicata."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return ""
    best, fstype = "", ""
    for line in read_file("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = parts[1].replace("\\040", " ")
        if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
            best, fstype = mnt, parts[2]
    return fstype


class SleepInhibitor(object):
    """Impedisce la sospensione del PC durante la registrazione."""

    def __init__(self):
        self.proc = None

    def start(self):
        exe = shutil.which("systemd-inhibit")
        if not exe or self.proc:
            return False
        try:
            self.proc = subprocess.Popen(
                [exe, "--what=sleep:idle:handle-lid-switch", "--who=" + APP_NAME,
                 "--why=Registrazione di una videocassetta in corso", "--mode=block",
                 "sleep", "2147483647"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            return True
        except OSError:
            self.proc = None
            return False

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.proc = None


class FFmpegInfo(object):
    """Rileva versione, encoder e dispositivi supportati da ffmpeg."""

    def __init__(self):
        self.path = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        self.version = (0, 0)
        self.version_str = "non trovato"
        self.encoders = set()
        self.demuxers = set()
        self.muxers = set()
        if not self.path:
            return
        _, out = run_cmd([self.path, "-hide_banner", "-version"])
        first = out.splitlines()[0] if out else ""
        self.version_str = first.replace("ffmpeg version", "").split("Copyright")[0].strip()
        m = re.search(r"version\s+n?(\d+)\.(\d+)", first)
        self.version = (int(m.group(1)), int(m.group(2))) if m else (99, 0)
        _, out = run_cmd([self.path, "-hide_banner", "-encoders"])
        for line in out.splitlines():
            m = re.match(r"^\s*[VAS][\w.]{5}\s+(\S+)", line)
            if m:
                self.encoders.add(m.group(1))
        _, out = run_cmd([self.path, "-hide_banner", "-devices"])
        for line in out.splitlines():
            m = re.match(r"^\s*([D ])([E ])\s+(\S+)", line)
            if m:
                for name in m.group(3).split(","):
                    if m.group(1) == "D":
                        self.demuxers.add(name)
                    if m.group(2) == "E":
                        self.muxers.add(name)

    def fps_mode_args(self):
        if self.version >= (5, 1):
            return ["-fps_mode", "cfr"]
        return ["-vsync", "cfr"]


# --------------------------------------------------------------------------
# Rilevamento dispositivi
# --------------------------------------------------------------------------

class VideoDevice(object):
    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.formats = collections.OrderedDict()   # nome ffmpeg -> (descrizione, [dimensioni])
        self.inputs = []                            # [(indice, nome)]
        self.usb = None
        self.driver = ""

    @property
    def is_uvc(self):
        """Dispositivo UVC (webcam o schede tipo MS2109/MS210x): niente standard TV."""
        return self.driver == "uvcvideo"

    def capture_score(self, audio_devices=None):
        """Quanto è probabile che sia una scheda di acquisizione da videocassetta."""
        if self.path == TEST_SOURCE or not self.formats:
            return -100
        score = 0
        sizes = set()
        for _, (_, sz) in self.formats.items():
            sizes.update(sz)
        if "720x576" in sizes or "720x480" in sizes:
            score += 5
        nm = self.name.lower()
        if any(k in nm for k in ("ms210", "ms2109", "macrosilicon", "easycap", "em28",
                                 "stk1160", "utv007", "usbtv", "cx231", "av to usb",
                                 "video capture", "grabber", "usb video", "capture")):
            score += 6
        if any(k in nm for k in ("easycamera", "webcam", "integrated", "facetime",
                                 "hd camera", "hd webcam", "ir camera", "hp ", "lenovo",
                                 "chicony", "realtek", "camera")):
            score -= 8
        if not self.is_uvc and self.driver:
            score += 4   # em28xx, stk1160, usbtv, cx231xx... sono schede di acquisizione
        if audio_devices and self.usb:
            if any(a[2] == self.usb for a in audio_devices if a[2]):
                score += 3
        return score

    @property
    def label(self):
        return "%s  (%s)" % (self.name, self.path)


def probe_video_formats(ff, dev):
    _, out = run_cmd([ff.path, "-hide_banner", "-f", "v4l2", "-list_formats", "all",
                      "-i", dev], timeout=8)
    formats = collections.OrderedDict()
    for line in out.splitlines():
        m = re.search(r"\]\s*(Raw|Compressed)\s*:(.*)$", line)
        if not m:
            continue
        parts = re.split(r"\s+:(?:\s+|$)", m.group(2).strip())
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        desc = parts[1].strip()
        sizes_txt = parts[2] if len(parts) > 2 else ""
        if not name or name.lower() == "unsupported":
            continue
        sizes = []
        for w, h in re.findall(r"(?<![\d{,])(\d+)x(\d+)(?![\d}])", sizes_txt):
            s = "%sx%s" % (w, h)
            if s not in sizes:
                sizes.append(s)
        formats[name] = (desc, sizes)
    return formats


def v4l2_inputs(dev):
    if not shutil.which("v4l2-ctl"):
        return []
    _, out = run_cmd(["v4l2-ctl", "-d", dev, "--list-inputs"])
    return [(int(a), b.strip()) for a, b in
            re.findall(r"Input\s*:\s*(\d+)\s*\n\s*Name\s*:\s*(.+)", out)]


def v4l2_controls(dev):
    """Restituisce [(nome, tipo, min, max, step, default, valore)]."""
    if not shutil.which("v4l2-ctl"):
        return []
    _, out = run_cmd(["v4l2-ctl", "-d", dev, "--list-ctrls"])
    res = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\w+)\s+0x[0-9a-f]+\s+\((int|bool)\)\s*:(.*)$", line)
        if not m:
            continue
        name, typ, rest = m.groups()
        vals = dict(re.findall(r"(\w+)=(-?\d+)", rest))
        try:
            if typ == "int":
                res.append((name, typ, int(vals["min"]), int(vals["max"]),
                            int(vals.get("step", 1)) or 1, int(vals.get("default", 0)),
                            int(vals["value"])))
            else:
                res.append((name, typ, 0, 1, 1, int(vals.get("default", 0)),
                            int(vals["value"])))
        except (KeyError, ValueError):
            pass
    return res


def list_video_devices(ff):
    devices = []
    for path in sorted(glob.glob("/dev/video*"), key=natural_key):
        node = os.path.basename(path)
        sysdir = "/sys/class/video4linux/" + node
        name = read_file(sysdir + "/name").strip() or node
        d = VideoDevice(path, name)
        d.usb = usb_parent(sysdir + "/device")
        try:
            d.driver = os.path.basename(os.path.realpath(sysdir + "/device/driver"))
        except OSError:
            d.driver = ""
        if ff.path:
            d.formats = probe_video_formats(ff, path)
        d.inputs = v4l2_inputs(path)
        devices.append(d)
    # Prima i dispositivi che espongono formati video (le schede creano spesso
    # anche un nodo "metadata" inutilizzabile).
    capt = [d for d in devices if d.formats]
    other = [d for d in devices if not d.formats]
    return capt + other


def list_audio_devices(ff):
    """Restituisce [(valore, etichetta, percorso_usb, info_scheda_alsa)]."""
    res = [(CARD_AUDIO, "Audio della scheda di acquisizione (automatico)", None, None)]
    if "alsa" in ff.demuxers:
        cards = {}
        for m in re.finditer(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(\S+)\s*-\s*(.*)$",
                             read_file("/proc/asound/cards"), re.M):
            cards[m.group(1)] = (m.group(2), m.group(3), m.group(4).strip())
        for line in read_file("/proc/asound/pcm").splitlines():
            m = re.match(r"^(\d+)-(\d+):\s*([^:]*?)\s*:.*capture", line)
            if not m:
                continue
            num, devn, pcmname = str(int(m.group(1))), int(m.group(2)), m.group(3)
            cid, driver, longname = cards.get(num, (num, "", "Scheda " + num))
            value = "alsa:plughw:CARD=%s,DEV=%d" % (cid, devn)
            label = "%s - %s  (ALSA hw:%s,%d)" % (longname, pcmname, num, devn)
            info = {"num": num, "id": cid, "driver": driver, "name": longname}
            res.append((value, label, usb_parent("/sys/class/sound/card%s/device" % num),
                        info))
        res.append(("alsa:default", "ALSA predefinito", None, None))
    if "pulse" in ff.demuxers and shutil.which("pactl"):
        _, out = run_cmd(["pactl", "list", "short", "sources"], timeout=5)
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) >= 2 and not cols[1].endswith(".monitor"):
                res.append(("pulse:" + cols[1], "PulseAudio/PipeWire: " + cols[1], None,
                            None))
    res.append((TEST_SOURCE, "Segnale di prova (tono 440 Hz)", None, None))
    res.append((NO_AUDIO, "Nessun audio", None, None))
    return res


# Driver audio tipici delle schede di acquisizione video
CAPTURE_AUDIO_DRIVERS = ("usb-audio", "em28xx", "stk1160", "cx231xx", "usbtv", "go7007",
                         "tm6000", "au0828", "hdpvr", "saa7134", "cx88", "bt87x")


def find_card_audio(audio_devices, video_dev):
    """Trova l'ingresso audio che appartiene alla scheda di acquisizione video.

    1) stessa periferica USB fisica del video (metodo sicuro);
    2) altrimenti una scheda audio con un driver tipico delle schede di acquisizione;
    3) altrimenti una scheda audio USB qualsiasi.
    """
    alsa = [a for a in audio_devices if a[3]]
    if video_dev is not None and video_dev.usb:
        for a in alsa:
            if a[2] and a[2] == video_dev.usb:
                return a
    for a in alsa:
        drv = a[3]["driver"].lower()
        if any(drv.startswith(x) for x in CAPTURE_AUDIO_DRIVERS):
            return a
    for a in alsa:
        if a[2]:
            return a
    return None


def alsa_native_format(card_num):
    """(canali, frequenza) nativi dell'ingresso audio USB, se dichiarati dal driver."""
    txt = read_file("/proc/asound/card%s/stream0" % card_num)
    i = txt.find("Capture:")
    if i < 0:
        return None
    sec = txt[i:]
    m_ch = re.search(r"Channels:\s*(\d+)", sec)
    m_rt = re.search(r"Rates:\s*(.+)", sec)
    if not m_ch or not m_rt:
        return None
    ch = int(m_ch.group(1))
    nums = [int(x) for x in re.findall(r"\d+", m_rt.group(1))]
    if not nums:
        return None
    if "continuous" in m_rt.group(1) and len(nums) >= 2:
        rate = 48000 if nums[0] <= 48000 <= nums[1] else nums[1]
    elif 48000 in nums:
        rate = 48000
    elif 44100 in nums:
        rate = 44100
    else:
        rate = max(nums)
    return ch, rate


def alsa_capture_controls(card_id):
    """Controlli di registrazione del mixer ALSA: [(nome, percentuale, ha_interruttore, attivo)]."""
    if not shutil.which("amixer"):
        return []
    rc, out = run_cmd(["amixer", "-c", card_id], timeout=5)
    if rc != 0:
        return []
    res = []
    for block in re.split(r"^Simple mixer control ", out, flags=re.M)[1:]:
        m = re.match(r"'(.+?)',(\d+)", block)
        mc = re.search(r"Capabilities:(.*)", block)
        if not m or not mc:
            continue
        caps = mc.group(1).split()
        if "cvolume" not in caps and "cswitch" not in caps:
            continue
        name = m.group(1) if m.group(2) == "0" else "%s,%s" % (m.group(1), m.group(2))
        mp = re.search(r"Capture \d+ \[(\d+)%\]", block)
        pct = int(mp.group(1)) if mp and "cvolume" in caps else None
        on = "[off]" not in block
        res.append((name, pct, "cswitch" in caps, on))
    return res


def alsa_set_capture(card_id, name, percent=None):
    args = ["amixer", "-q", "-c", card_id, "sset", name]
    if percent is not None:
        args.append("%d%%" % percent)
    args.append("cap")
    return run_cmd(args, timeout=5)[0] == 0


# --------------------------------------------------------------------------
# Costruzione del comando ffmpeg
# --------------------------------------------------------------------------

def preview_dims(cfg):
    w = PREVIEW_SIZES.get(cfg.get("preview_size"), 384)
    h = w * 9 // 16 if cfg.get("aspect") == "16:9" else w * 3 // 4
    return w - w % 2, h - h % 2


def build_ffmpeg_cmd(ff, cfg, record_path=None, meter_fd=None, preview_fps=12,
                     use_v4l2ctl=False, title="", segment_seconds=0, monitor_fd=None,
                     tv_controls=True):
    """Comando ffmpeg per anteprima (stdout, PPM) + VU-meter (fd) + registrazione."""
    vdev = cfg["video_device"]
    adev = cfg["audio_device"] or NO_AUDIO
    size = cfg["video_size"] or "720x576"
    fps = FPS_VALUES.get(cfg["framerate"], cfg["framerate"] or "25")
    ch = str(cfg.get("audio_channels") or "2")
    ch = ch if ch in ("1", "2") else "2"
    rate = str(cfg.get("audio_rate") or "48000")
    rate = rate if rate.isdigit() else "48000"
    preset = PRESETS.get(cfg["preset"], PRESETS["mjpeg"])
    copy = cfg["preset"] == "copy" and record_path is not None

    cmd = [ff.path, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
           "-nostats", "-progress", "pipe:2"]
    if record_path and ff.version < (5, 1):
        cmd += ff.fps_mode_args()

    # Ingresso video
    if vdev == TEST_SOURCE:
        cmd += ["-re", "-f", "lavfi", "-i",
                "testsrc2=size=%s:rate=%s" % (size, fps)]
    else:
        cmd += ["-f", "v4l2", "-thread_queue_size", "512"]
        if not use_v4l2ctl and tv_controls:
            if cfg.get("video_input") not in ("", None):
                cmd += ["-channel", str(cfg["video_input"])]
            if cfg.get("standard") and cfg["standard"] != "Automatico":
                cmd += ["-standard", cfg["standard"]]
        if cfg.get("input_format"):
            cmd += ["-input_format", cfg["input_format"]]
        cmd += ["-video_size", size, "-framerate", fps, "-i", vdev]

    # Ingresso audio
    has_audio = adev != NO_AUDIO
    if adev == TEST_SOURCE:
        cmd += ["-re", "-f", "lavfi", "-i",
                "sine=frequency=440:beep_factor=4:sample_rate=%s" % rate]
    elif adev.startswith("alsa:"):
        cmd += ["-f", "alsa", "-thread_queue_size", "1024", "-ac", ch, "-ar", rate,
                "-i", adev[5:]]
    elif adev.startswith("pulse:"):
        cmd += ["-f", "pulse", "-thread_queue_size", "1024", "-ac", ch, "-ar", rate,
                "-i", adev[6:]]
    else:
        has_audio = False

    # Grafo dei filtri
    pw, ph = preview_dims(cfg)
    graph = []
    prev_in = "[0:v]"
    vrec_map = None
    if record_path:
        if copy:
            vrec_map = "0:v"
        else:
            rec_filters = []
            if cfg.get("deinterlace") in ("yadif", "bwdif"):
                rec_filters.append("%s=0:-1:0" % cfg["deinterlace"])
            if cfg.get("head_mask"):
                # Copre con una banda nera il rumore delle testine in fondo al quadro
                rec_filters.append("crop=iw:ih-12:0:0,pad=iw:ih+12:0:0:black")
            graph.append("[0:v]split=2[vr0][vp0]")
            graph.append("[vr0]%s[vrec]" % (",".join(rec_filters) or "null"))
            prev_in = "[vp0]"
            vrec_map = "[vrec]"
    graph.append("%sfps=%s,scale=%d:%d:flags=fast_bilinear,format=rgb24[vprev]"
                 % (prev_in, preview_fps, pw, ph))

    a_outs = []
    if has_audio:
        if record_path:
            a_outs.append("arec")
        if meter_fd is not None:
            a_outs.append("amet")
        if monitor_fd is not None:
            a_outs.append("amon")
        if a_outs:
            labels = "".join("[%s]" % o for o in a_outs)
            if len(a_outs) == 1:
                graph.append("[1:a]aresample=async=1000%s" % labels)
            else:
                graph.append("[1:a]aresample=async=1000,asplit=%d%s" % (len(a_outs), labels))
        if "amet" in a_outs:
            layout = "mono" if ch == "1" else "stereo"
            graph.append("[amet]aresample=8000,aformat=sample_fmts=s16:channel_layouts=%s[ameter]"
                         % layout)
    cmd += ["-filter_complex", ";".join(graph)]

    # Uscita 0: registrazione (deve essere la prima per le statistiche)
    if record_path:
        ext = os.path.splitext(record_path)[1].lower()
        cmd += ["-map", vrec_map]
        if "arec" in a_outs:
            cmd += ["-map", "[arec]"]
        cmd += preset[1]
        if not copy:
            cmd += ["-r", fps]
            if ff.version >= (5, 1):
                cmd += ff.fps_mode_args()
            cmd += ["-aspect", cfg.get("aspect") or "4:3"]
        if "arec" in a_outs:
            cmd += (["-c:a", "pcm_s16le"] if ext == ".avi" else preset[2])
            cmd += ["-ac", ch, "-ar", rate]
        cmd += ["-metadata", "title=" + (title or os.path.basename(record_path)),
                "-metadata", "comment=Digitalizzato con %s %s" % (APP_NAME, VERSION),
                "-metadata", "creation_time=" + datetime.datetime.utcnow().strftime(
                    "%Y-%m-%dT%H:%M:%SZ")]
        if segment_seconds:
            # File spezzati (es. chiavette FAT32, limite 4 GB): record_path contiene %03d
            cmd += ["-f", "segment", "-segment_time", str(int(segment_seconds)),
                    "-segment_format", "avi" if ext == ".avi" else "matroska",
                    "-reset_timestamps", "1"]
        cmd += [record_path]

    # Uscita: anteprima su stdout
    cmd += ["-map", "[vprev]", "-c:v", "ppm", "-f", "image2pipe", "pipe:1"]
    # Uscita: VU-meter
    if "amet" in a_outs:
        cmd += ["-map", "[ameter]", "-c:a", "pcm_s16le", "-f", "s16le",
                "pipe:%d" % meter_fd]
    # Uscita: ascolto dagli altoparlanti. Passa da una pipe letta dal programma e
    # inoltrata a un lettore esterno: se l'audio di sistema dà problemi la
    # registrazione NON si interrompe.
    if "amon" in a_outs:
        cmd += ["-map", "[amon]", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                "-f", "s16le", "pipe:%d" % monitor_fd]
    return cmd


def audio_player_cmd():
    """Lettore audio esterno per l'ascolto (PCM s16le 48 kHz stereo da stdin)."""
    if shutil.which("pacat"):
        return ["pacat", "--playback", "--raw", "--format=s16le", "--rate=48000",
                "--channels=2", "--latency-msec=120", "--client-name=" + APP_NAME]
    if shutil.which("pw-cat"):
        return ["pw-cat", "--playback", "--format=s16", "--rate=48000", "--channels=2", "-"]
    if shutil.which("aplay"):
        return ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", "48000", "-c", "2"]
    return None


ERROR_HINTS = [
    ("Device or resource busy", "Il dispositivo è già in uso da un altro programma "
     "(chiudi webcam, Cheese, OBS, VLC...) oppure da PulseAudio/PipeWire."),
    ("Permission denied", "Permessi insufficienti. Esegui:\n  sudo usermod -aG video,audio $USER\n"
     "e poi esci e rientra nella sessione."),
    ("standard", "Lo standard TV scelto non è supportato da questa scheda: prova "
     "'Automatico' oppure installa v4l-utils."),
    ("No such file or directory", "Dispositivo non trovato: la scheda è collegata? "
     "Premi 'Aggiorna' per rileggere l'elenco."),
    ("Invalid argument", "Formato, risoluzione o frame rate non supportati dalla scheda: "
     "prova un altro formato pixel o 720x576 / 25 fps (PAL)."),
    ("cannot open audio device", "Impossibile aprire la periferica audio: prova un'altra "
     "voce in 'Audio' (ad esempio la sorgente PulseAudio/PipeWire)."),
    ("Unknown encoder", "Codifica non disponibile in questa versione di ffmpeg: "
     "scegli un'altra qualità."),
    ("No space left", "Disco pieno!"),
]


def explain_error(text):
    hints = [h for key, h in ERROR_HINTS if key.lower() in text.lower()]
    return "\n\n".join(hints)


# --------------------------------------------------------------------------
# Motore di acquisizione (processo ffmpeg + thread di lettura)
# --------------------------------------------------------------------------

class CaptureEngine(object):
    def __init__(self):
        self.proc = None
        self.frame = None
        self.frame_id = 0
        self.peak = [0.0, 0.0]
        self.meter_channels = 2
        self.progress = {}
        self.log = collections.deque(maxlen=400)
        self.new_log = collections.deque()
        self.stopping = False
        self.record_path = None
        self.start_time = None
        self.last_frame_time = time.monotonic()
        self.last_progress_time = time.monotonic()
        self._last_line = None
        self._repeat = 0
        self._lock = threading.Lock()

    def start(self, cmd_builder, channels, monitor=False):
        r, w = os.pipe()
        fds = [w]
        mon_r = mon_w = None
        if monitor:
            mon_r, mon_w = os.pipe()
            fds.append(mon_w)
        cmd = cmd_builder(w, mon_w)
        self.meter_channels = 1 if str(channels) == "1" else 2
        self._log("$ " + " ".join(shlex.quote(c) for c in cmd))
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         pass_fds=tuple(fds))
        except OSError as e:
            for fd in fds + [r, mon_r]:
                if fd is not None:
                    os.close(fd)
            raise RuntimeError("Impossibile avviare ffmpeg: %s" % e)
        for fd in fds:
            os.close(fd)
        if mon_r is not None:
            t = threading.Thread(target=self._forward_monitor, args=(os.fdopen(mon_r, "rb", 0),))
            t.daemon = True
            t.start()
        self.start_time = time.monotonic()
        self.last_frame_time = self.start_time
        self.last_progress_time = self.start_time
        for target, arg in ((self._read_frames, self.proc.stdout),
                            (self._read_stderr, self.proc.stderr),
                            (self._read_meter, os.fdopen(r, "rb", 0))):
            t = threading.Thread(target=target, args=(arg,))
            t.daemon = True
            t.start()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        """Ferma ffmpeg con SIGINT (chiude correttamente il file)."""
        if not self.running() or self.stopping:
            return
        self.stopping = True

        def killer(p):
            try:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(15)
                    return
                except subprocess.TimeoutExpired:
                    p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
            except OSError:
                pass
        t = threading.Thread(target=killer, args=(self.proc,))
        t.daemon = True
        t.start()

    def abort(self):
        """Arresto forzato (watchdog): l'uscita viene trattata come inattesa."""
        def killer(p):
            try:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
            except OSError:
                pass
        t = threading.Thread(target=killer, args=(self.proc,))
        t.daemon = True
        t.start()

    def _log(self, line):
        with self._lock:
            self.log.append(line)
            self.new_log.append(line)

    def pop_log(self):
        with self._lock:
            lines = list(self.new_log)
            self.new_log.clear()
        return lines

    def tail(self, n=12):
        with self._lock:
            return "\n".join(l for l in list(self.log)[-n:] if not l.startswith("$ "))

    @staticmethod
    def _drain(f):
        """Continua a svuotare la pipe: ffmpeg non deve mai bloccarsi o ricevere
        un errore di scrittura, altrimenti fermerebbe anche la registrazione."""
        try:
            while f.read(65536):
                pass
        except (OSError, ValueError):
            pass

    def _read_frames(self, f):
        try:
            while True:
                magic = f.readline()
                if not magic:
                    return
                if not magic.startswith(b"P6"):
                    break
                dims = f.readline()
                maxv = f.readline()
                w, h = [int(x) for x in dims.split()]
                data = f.read(w * h * 3)
                if len(data) < w * h * 3:
                    return
                self.frame = (w, h, magic + dims + maxv + data)
                self.frame_id += 1
                self.last_frame_time = time.monotonic()
        except (ValueError, OSError, MemoryError):
            pass
        self._log("Anteprima disattivata per un errore di lettura (la registrazione continua).")
        self._drain(f)

    def _read_stderr(self, f):
        prog = {}
        for raw in iter(f.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            m = re.match(r"^(\w+)=\s*(\S*)$", line)
            if m:
                if m.group(1) == "frame" and prog.get("frame") != m.group(2):
                    self.last_progress_time = time.monotonic()
                prog[m.group(1)] = m.group(2)
                if m.group(1) == "progress":
                    self.progress = dict(prog)
            elif line and "deprecated pixel format" not in line:
                if line == self._last_line:
                    self._repeat += 1
                    if self._repeat in (10, 100, 1000):
                        self._log("(messaggio precedente ripetuto %d volte)" % self._repeat)
                    continue
                self._last_line, self._repeat = line, 0
                self._log(line)

    def _forward_monitor(self, f):
        """Inoltra l'audio al lettore esterno; se il lettore muore si continua
        solo a svuotare la pipe."""
        cmd = audio_player_cmd()
        player = None
        if cmd:
            try:
                player = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
            except OSError:
                player = None
        if not player:
            self._log("Ascolto audio non disponibile (installa pulseaudio-utils o "
                      "alsa-utils). La registrazione non ne risente.")
        try:
            while True:
                data = f.read(9600)
                if not data:
                    break
                if player:
                    try:
                        player.stdin.write(data)
                    except (OSError, ValueError):
                        self._log("Ascolto audio interrotto (la registrazione continua).")
                        player = None
        except (OSError, ValueError):
            self._drain(f)
        finally:
            f.close()
            if player:
                try:
                    player.stdin.close()
                    player.terminate()
                except OSError:
                    pass

    def _read_meter(self, f):
        chunk = 800 * self.meter_channels * 2 // 4 * 4  # ~50 ms a 8 kHz
        swap = sys.byteorder == "big"
        try:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                if len(data) % 2:
                    data = data[:-1]
                a = array.array("h")
                a.frombytes(data)
                if swap:
                    a.byteswap()
                if self.meter_channels == 2:
                    l, r = a[0::2], a[1::2]
                else:
                    l = r = a
                pl = max(max(l), -min(l)) / 32768.0 if len(l) else 0.0
                pr = max(max(r), -min(r)) / 32768.0 if len(r) else 0.0
                self.peak = [max(self.peak[0], pl), max(self.peak[1], pr)]
        except (ValueError, OSError, MemoryError):
            self._drain(f)
        finally:
            f.close()

    def take_peaks(self):
        p = self.peak
        self.peak = [0.0, 0.0]
        return p


def frame_is_uniform(frame):
    """True se il fotogramma è (quasi) a tinta unita: schermo nero o blu = nessun segnale."""
    data = frame[2]
    start = len(data) - frame[0] * frame[1] * 3
    sample = data[start::97]
    n = len(sample)
    if n < 10:
        return False
    s = sum(sample)
    s2 = sum(x * x for x in sample)
    var = s2 / n - (s / n) ** 2
    return var < 40.0


# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        for k in DEFAULTS:
            if k in data:
                cfg[k] = data[k]
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Interfaccia grafica - stile "anni 2000"
# --------------------------------------------------------------------------

def run_gui(ff, cfg):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # Tavolozza "Windows 2000 / XP classico"
    FACE = "#d4d0c8"
    FACE_DARK = "#808080"
    TITLE_A = "#0a246a"
    TITLE_B = "#a6caf0"
    LCD_BG = "#0b1a0b"
    LCD_FG = "#39ff14"
    LCD_DIM = "#1c4d1c"
    SEL = "#0a246a"

    class App(object):
        def __init__(self, root):
            self.root = root
            self.cfg = cfg
            self.engine = None
            self.mode = "idle"          # idle | preview | recording
            self.preview_wanted = True
            self.after_stop = None
            self.video_devices = []
            self.audio_devices = []
            self.rec = None
            self.rec_size = 0
            self.last_duration = None
            self.recover_job = None
            self.inhibitor = SleepInhibitor()
            self.chapters = []
            self.signal_seen_since = None
            self.signal_ok_time = 0.0
            self.nosignal_since = None
            self.nosignal = False
            self.last_frame_id = -1
            self.meter_hold = [0.0, 0.0]
            self.meter_disp = [0.0, 0.0]
            self.clip_until = 0
            self.restart_job = None
            self.blink = False
            self.conv_proc = None
            self.conv_queue = []
            self.conv_duration = 0
            self.use_v4l2ctl = bool(shutil.which("v4l2-ctl"))
            self.vars = {}
            self.lock_widgets = []

            self._setup_style()
            self._build_menu()
            self._build_ui()
            self.refresh_devices(first=True)
            root.protocol("WM_DELETE_WINDOW", self.on_quit)
            root.after(60, self.poll)
            root.after(500, self.slow_poll)
            root.after(300, self.start_preview)

        # ---------------------------------------------------------- stile
        def _setup_style(self):
            r = self.root
            r.configure(bg=FACE)
            fams = set(self._families())
            base = "Tahoma" if "Tahoma" in fams else (
                "Verdana" if "Verdana" in fams else "DejaVu Sans")
            self.font = (base, 9)
            self.font_b = (base, 9, "bold")
            self.font_title = (base, 13, "bold")
            mono = "Courier New" if "Courier New" in fams else "DejaVu Sans Mono"
            self.font_lcd = (mono, 22, "bold")
            self.font_lcd_s = (mono, 9, "bold")
            r.option_add("*Font", self.font)
            r.option_add("*Background", FACE)
            r.option_add("*foreground", "black")
            r.option_add("*activeBackground", "#e4e0d8")
            r.option_add("*selectBackground", SEL)
            r.option_add("*selectForeground", "white")
            r.option_add("*highlightThickness", 0)
            r.option_add("*Entry.Background", "white")
            r.option_add("*Listbox.Background", "white")
            r.option_add("*Text.Background", "white")
            r.option_add("*Menu.relief", "raised")
            r.option_add("*Menu.activeBackground", SEL)
            r.option_add("*Menu.activeForeground", "white")
            r.option_add("*Menu.activeBorderWidth", 0)
            r.option_add("*TCombobox*Listbox.Background", "white")
            r.option_add("*TCombobox*Listbox.selectBackground", SEL)

            st = ttk.Style()
            try:
                st.theme_use("alt")
            except tk.TclError:
                pass
            st.configure(".", background=FACE, font=self.font, foreground="black")
            st.configure("TNotebook", background=FACE, borderwidth=2)
            st.configure("TNotebook.Tab", background=FACE, padding=(10, 3), font=self.font)
            st.map("TNotebook.Tab", background=[("selected", FACE)],
                   expand=[("selected", (2, 2, 2, 0))])
            st.configure("TCombobox", fieldbackground="white", background=FACE,
                         arrowsize=13)
            st.map("TCombobox", fieldbackground=[("readonly", "white"),
                                                 ("disabled", FACE)],
                   foreground=[("disabled", FACE_DARK)])
            st.configure("Horizontal.TProgressbar", background=SEL, troughcolor="white",
                         borderwidth=2, thickness=16)
            st.configure("TCheckbutton", background=FACE)

        def _families(self):
            from tkinter import font as tkfont
            try:
                return tkfont.families(self.root)
            except tk.TclError:
                return []

        def _bevel(self, parent, relief="sunken", bd=2, **kw):
            return tk.Frame(parent, relief=relief, bd=bd, bg=kw.pop("bg", FACE), **kw)

        def _groupbox(self, parent, text):
            return tk.LabelFrame(parent, text=" %s " % text, relief="groove", bd=2,
                                 padx=6, pady=4, font=self.font)

        def _button(self, parent, text, cmd, width=None, **kw):
            b = tk.Button(parent, text=text, command=cmd, relief="raised", bd=2,
                          padx=8, pady=2, activebackground="#e4e0d8", **kw)
            if width:
                b.configure(width=width)
            return b

        # ---------------------------------------------------------- menu
        def _build_menu(self):
            m = tk.Menu(self.root, bd=1)
            f = tk.Menu(m, tearoff=0)
            f.add_command(label="Apri cartella delle registrazioni", command=self.open_folder)
            f.add_command(label="Cambia cartella...", command=self.choose_dir)
            f.add_separator()
            f.add_command(label="Esci", command=self.on_quit, accelerator="Ctrl+Q")
            m.add_cascade(label="File", menu=f, underline=0)
            a = tk.Menu(m, tearoff=0)
            a.add_command(label="Avvia anteprima", command=self.start_preview,
                          accelerator="F5")
            a.add_command(label="Registra", command=self.on_record, accelerator="F9")
            a.add_command(label="Stop", command=self.on_stop, accelerator="F10")
            a.add_command(label="Segna capitolo", command=self.on_mark, accelerator="F8")
            a.add_separator()
            a.add_command(label="Aggiorna dispositivi", command=self.refresh_devices)
            m.add_cascade(label="Acquisizione", menu=a, underline=0)
            h = tk.Menu(m, tearoff=0)
            h.add_command(label="Consigli per le videocassette", command=self.show_tips)
            h.add_command(label="Informazioni su %s..." % APP_NAME, command=self.show_about)
            m.add_cascade(label="?", menu=h)
            self.root.configure(menu=m)
            self.root.bind("<F5>", lambda e: self.start_preview())
            self.root.bind("<F9>", lambda e: self.on_record())
            self.root.bind("<F10>", lambda e: self.on_stop())
            self.root.bind("<F8>", lambda e: self.on_mark())
            self.root.bind("<Control-q>", lambda e: self.on_quit())

        # ---------------------------------------------------------- layout
        def _build_ui(self):
            root = self.root
            # Banner con sfumatura blu, alla maniera dei programmi di masterizzazione
            self.banner = tk.Canvas(root, height=46, bd=0, highlightthickness=0)
            self.banner.pack(fill="x")
            self.banner.bind("<Configure>", self._draw_banner)

            # --- Barra di stato a pannelli incassati
            sb = tk.Frame(root, bd=0)
            sb.pack(fill="x", side="bottom", padx=2, pady=2)
            self.st_main = tk.Label(sb, text="Pronto", anchor="w", relief="sunken", bd=1, width=10,
                                    padx=4)
            self.st_main.pack(side="left", fill="x", expand=True)
            self.st_size = tk.Label(sb, text="", anchor="w", relief="sunken", bd=1,
                                    width=16, padx=4)
            self.st_size.pack(side="left", padx=(2, 0))
            self.st_fps = tk.Label(sb, text="", anchor="w", relief="sunken", bd=1,
                                   width=22, padx=4)
            self.st_fps.pack(side="left", padx=(2, 0))
            self.st_disk = tk.Label(sb, text="", anchor="w", relief="sunken", bd=1,
                                    width=26, padx=4)
            self.st_disk.pack(side="left", padx=(2, 0))

            body = tk.Frame(root, padx=6, pady=6)
            body.pack(fill="both", expand=True)

            left = tk.Frame(body)
            left.pack(side="left", fill="y")
            right = tk.Frame(body, padx=6)
            right.pack(side="left", fill="both", expand=True)

            # --- Monitor video
            mon = self._groupbox(left, "Monitor")
            mon.pack(fill="x")
            sw = PREVIEW_SIZES.get(self.cfg["preview_size"], 384)
            frame = self._bevel(mon, "sunken", 2)
            frame.pack()
            self.canvas = tk.Canvas(frame, width=sw, height=sw * 3 // 4, bg="black",
                                    bd=0, highlightthickness=0)
            self.canvas.pack()
            self.photo = tk.PhotoImage()
            self.img_item = self.canvas.create_image(sw // 2, sw * 3 // 8,
                                                     image=self.photo)
            self.osd_rec = self.canvas.create_text(10, 10, anchor="nw", text="",
                                                   fill="#ff2020", font=self.font_lcd_s)
            self.osd_msg = self.canvas.create_text(sw // 2, sw * 3 // 8, text="NESSUN SEGNALE",
                                                   fill="white", font=self.font_b,
                                                   justify="center")

            opt = tk.Frame(mon)
            opt.pack(fill="x", pady=(4, 0))
            tk.Label(opt, text="Dimensione:").pack(side="left")
            self.vars["preview_size"] = tk.StringVar(value=self.cfg["preview_size"])
            for val, txt in (("small", "Piccola"), ("large", "Grande")):
                tk.Radiobutton(opt, text=txt, value=val, variable=self.vars["preview_size"],
                               command=self.on_preview_size).pack(side="left")
            self.vars["preview_while_recording"] = tk.BooleanVar(
                value=self.cfg["preview_while_recording"])
            tk.Checkbutton(opt, text="Anteprima durante la registrazione",
                           variable=self.vars["preview_while_recording"],
                           command=self.save_vars).pack(side="right")

            # --- Pannello "videoregistratore": timecode LCD + VU-meter
            deck = self._groupbox(left, "Pannello di controllo")
            deck.pack(fill="x", pady=(6, 0))
            lcdf = self._bevel(deck, "sunken", 2, bg=LCD_BG)
            lcdf.pack(fill="x")
            self.lcd = tk.Canvas(lcdf, height=64, bg=LCD_BG, bd=0, highlightthickness=0)
            self.lcd.pack(fill="x")
            self.lcd_time = self.lcd.create_text(10, 30, anchor="w", text="00:00:00",
                                                 fill=LCD_FG, font=self.font_lcd)
            self.lcd_state = self.lcd.create_text(10, 54, anchor="w", text="STOP",
                                                  fill=LCD_FG, font=self.font_lcd_s)
            self.lcd_info = self.lcd.create_text(10, 8, anchor="w", text="",
                                                 fill=LCD_FG, font=self.font_lcd_s)
            # VU-meter a LED
            self.vu_segments = 30
            self.vu_items = [[], []]
            self.vu_x0 = 190
            for c in range(2):
                y = 22 + c * 18
                self.lcd.create_text(self.vu_x0 - 8, y + 4, text="LR"[c], fill=LCD_FG,
                                     font=self.font_lcd_s)
                for i in range(self.vu_segments):
                    x = self.vu_x0 + i * 6
                    it = self.lcd.create_rectangle(x, y, x + 4, y + 9, fill=LCD_DIM,
                                                   outline="")
                    self.vu_items[c].append(it)
            for db in (-40, -20, -12, -6, 0):
                x = self.vu_x0 + self._db_to_seg(db) * 6
                self.lcd.create_text(x, 8, text=str(db), fill=LCD_FG, font=("TkFixedFont", 7))
            self.lcd_clip = self.lcd.create_text(self.vu_x0 + self.vu_segments * 6 + 6, 30,
                                                 anchor="w", text="", fill="#ff3030",
                                                 font=self.font_lcd_s)

            btns = tk.Frame(deck, pady=6)
            btns.pack(fill="x")
            self.btn_prev = self._button(btns, "▶  Anteprima", self.start_preview)
            self.btn_rec = self._button(btns, "●  REGISTRA", self.on_record,
                                        fg="#c00000", font=self.font_b)
            self.btn_stop = self._button(btns, "■  Stop", self.on_stop)
            self.btn_mark = self._button(btns, "⚑  Capitolo", self.on_mark)
            for b in (self.btn_prev, self.btn_rec, self.btn_stop, self.btn_mark):
                b.pack(side="left", padx=2, fill="x", expand=True)

            # --- Schede impostazioni
            self.nb = ttk.Notebook(right)
            self.nb.pack(fill="both", expand=True)
            self._tab_source()
            self._tab_record()
            self._tab_adjust()
            self._tab_convert()
            self._tab_log()

            self._update_buttons()

        def _draw_banner(self, event=None):
            c = self.banner
            c.delete("all")
            w = max(c.winfo_width(), 2)
            h = 46
            a = [int(TITLE_A[i:i + 2], 16) for i in (1, 3, 5)]
            b = [int(TITLE_B[i:i + 2], 16) for i in (1, 3, 5)]
            steps = 64
            for i in range(steps):
                t = i / float(steps - 1)
                col = "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))
                x0 = int(w * i / steps)
                x1 = int(w * (i + 1) / steps) + 1
                c.create_rectangle(x0, 0, x1, h, fill=col, outline="")
            # Icona: una videocassetta stilizzata
            c.create_rectangle(12, 10, 52, 36, fill="#222222", outline="#cccccc")
            c.create_rectangle(18, 15, 46, 27, fill="#eeeeee", outline="")
            c.create_oval(21, 17, 29, 25, fill="#444444", outline="")
            c.create_oval(35, 17, 43, 25, fill="#444444", outline="")
            c.create_rectangle(22, 30, 42, 36, fill="#555555", outline="")
            c.create_text(66, 15, anchor="w", text=APP_NAME, fill="white",
                          font=self.font_title)
            c.create_text(67, 33, anchor="w",
                          text="Digitalizza le tue videocassette  -  versione %s" % VERSION,
                          fill="#dde6ff", font=self.font)
            c.create_line(0, h - 1, w, h - 1, fill="#000000")

        def _row(self, parent, r, label, widget):
            tk.Label(parent, text=label, anchor="w").grid(row=r, column=0, sticky="w",
                                                          pady=2, padx=(0, 6))
            widget.grid(row=r, column=1, sticky="we", pady=2)
            parent.columnconfigure(1, weight=1)

        def _combo(self, parent, key=None, values=(), width=22, cmd=None):
            var = tk.StringVar()
            cb = ttk.Combobox(parent, textvariable=var, values=list(values), width=width,
                              state="readonly")
            if cmd:
                cb.bind("<<ComboboxSelected>>", lambda e: cmd())
            if key:
                self.vars[key] = var
            return cb, var

        def _tab_source(self):
            tab = tk.Frame(self.nb, padx=8, pady=8)
            self.nb.add(tab, text="Sorgente")

            g = self._groupbox(tab, "Video")
            g.pack(fill="x")
            row = tk.Frame(g)
            self.cb_vdev, self.v_vdev = self._combo(row, width=26,
                                                    cmd=self.on_user_video_device)
            self.cb_vdev.pack(side="left", fill="x", expand=True)
            self._button(row, "Aggiorna", self.refresh_devices).pack(side="left", padx=(4, 0))
            self._row(g, 0, "Dispositivo:", row)
            self.cb_input, self.v_input = self._combo(g, cmd=self.on_source_change)
            self._row(g, 1, "Ingresso:", self.cb_input)
            self.cb_std, _ = self._combo(g, "standard", STANDARDS, cmd=self.on_standard)
            self._row(g, 2, "Standard TV:", self.cb_std)
            self.cb_fmt, self.v_fmt = self._combo(g, cmd=self.on_format)
            self._row(g, 3, "Formato pixel:", self.cb_fmt)
            self.cb_size, _ = self._combo(g, "video_size", DEFAULT_SIZES,
                                          cmd=self.on_source_change)
            self._row(g, 4, "Risoluzione:", self.cb_size)
            self.cb_fps, _ = self._combo(g, "framerate", list(FPS_VALUES.keys()),
                                         cmd=self.on_source_change)
            self._row(g, 5, "Fotogrammi/s:", self.cb_fps)

            g = self._groupbox(tab, "Audio")
            g.pack(fill="x", pady=(8, 0))
            self.cb_adev, self.v_adev = self._combo(g, width=30, cmd=self.on_source_change)
            self._row(g, 0, "Dispositivo:", self.cb_adev)
            self.lbl_audio = tk.Label(g, text="", anchor="w", justify="left",
                                      font=(self.font[0], 8), wraplength=330)
            self.lbl_audio.grid(row=1, column=1, sticky="w")
            self.cb_ch, self.v_ch = self._combo(g, values=["Automatico", "Stereo", "Mono"],
                                                cmd=self.on_source_change)
            self._row(g, 2, "Canali:", self.cb_ch)
            self.cb_rate, _ = self._combo(g, "audio_rate",
                                          ["Automatico", "48000", "44100", "32000"],
                                          cmd=self.on_source_change)
            self._row(g, 3, "Frequenza (Hz):", self.cb_rate)
            self.vars["audio_monitor"] = tk.BooleanVar()
            self.chk_mon = tk.Checkbutton(g, text="Ascolta l'audio dagli altoparlanti",
                                          variable=self.vars["audio_monitor"],
                                          command=self.on_source_change)
            self.chk_mon.grid(row=4, column=0, columnspan=2, sticky="w")

            self.lock_widgets += [self.cb_vdev, self.cb_input, self.cb_std, self.cb_fmt,
                                  self.cb_size, self.cb_fps, self.cb_adev, self.cb_ch,
                                  self.cb_rate, self.chk_mon]

        def _tab_record(self):
            tab = tk.Frame(self.nb, padx=8, pady=8)
            self.nb.add(tab, text="Registrazione")

            g = self._groupbox(tab, "File di destinazione")
            g.pack(fill="x")
            row = tk.Frame(g)
            self.vars["output_dir"] = tk.StringVar(value=self.cfg["output_dir"])
            tk.Entry(row, textvariable=self.vars["output_dir"], relief="sunken",
                     bd=2).pack(side="left", fill="x", expand=True)
            self.btn_dir = self._button(row, "Sfoglia...", self.choose_dir)
            self.btn_dir.pack(side="left", padx=(4, 0))
            self._row(g, 0, "Cartella:", row)
            self.vars["name_template"] = tk.StringVar(value=self.cfg["name_template"])
            self.ent_name = tk.Entry(g, textvariable=self.vars["name_template"],
                                     relief="sunken", bd=2)
            self._row(g, 1, "Nome file:", self.ent_name)
            tk.Label(g, text="Es. \"Matrimonio_zia\" - %Y %m %d %H %M = data e ora",
                     fg="#444444", font=(self.font[0], 8)).grid(row=2, column=1, sticky="w")
            self.cb_cont, _ = self._combo(g, "container", ["mkv", "avi"],
                                          cmd=self.save_vars)
            self._row(g, 3, "Contenitore:", self.cb_cont)
            self.cb_split, self.v_split = self._combo(g, values=list(SPLIT_VALUES.values()),
                                                      cmd=self.save_vars)
            self._row(g, 4, "Dividi in più file:", self.cb_split)

            g = self._groupbox(tab, "Qualità")
            g.pack(fill="x", pady=(8, 0))
            self.preset_keys = [k for k, p in PRESETS.items()
                                if p[3] is None or p[3] in ff.encoders]
            self.cb_preset, self.v_preset = self._combo(
                g, values=[PRESETS[k][0] for k in self.preset_keys], width=30,
                cmd=self.save_vars)
            self._row(g, 0, "Codifica:", self.cb_preset)
            self.cb_deint, self.v_deint = self._combo(g, values=list(DEINTERLACE.values()),
                                                      cmd=self.save_vars)
            self._row(g, 1, "Deinterlacciamento:", self.cb_deint)
            self.cb_aspect, _ = self._combo(g, "aspect", ["4:3", "16:9"],
                                            cmd=self.on_aspect)
            self._row(g, 2, "Formato schermo:", self.cb_aspect)
            self.vars["head_mask"] = tk.BooleanVar()
            self.chk_mask = tk.Checkbutton(
                g, text="Nascondi il rumore delle testine (banda in basso)",
                variable=self.vars["head_mask"], command=self.save_vars)
            self.chk_mask.grid(row=3, column=0, columnspan=2, sticky="w")

            g = self._groupbox(tab, "Arresto automatico")
            g.pack(fill="x", pady=(8, 0))
            self.cb_maxdur, self.v_maxdur = self._combo(
                g, values=list(MAX_DURATIONS.values()), cmd=self.save_vars)
            self._row(g, 0, "Durata massima:", self.cb_maxdur)
            row = tk.Frame(g)
            self.vars["auto_stop_nosignal"] = tk.BooleanVar()
            tk.Checkbutton(row, text="Fine nastro: ferma dopo",
                           variable=self.vars["auto_stop_nosignal"],
                           command=self.save_vars).pack(side="left")
            self.vars["nosignal_minutes"] = tk.StringVar()
            tk.Spinbox(row, from_=1, to=30, width=3, textvariable=self.vars["nosignal_minutes"],
                       relief="sunken", bd=2, command=self.save_vars,
                       bg="white").pack(side="left")
            tk.Label(row, text="min di schermo nero/blu").pack(side="left")
            row.grid(row=1, column=0, columnspan=2, sticky="w")
            self.vars["auto_recover"] = tk.BooleanVar()
            tk.Checkbutton(g, text="Se la scheda si blocca, riprendi da sola in un nuovo file",
                           variable=self.vars["auto_recover"],
                           command=self.save_vars).grid(row=2, column=0, columnspan=2,
                                                        sticky="w")

            self.lock_widgets += [self.cb_split, self.btn_dir, self.ent_name, self.cb_cont, self.cb_preset,
                                  self.cb_deint, self.cb_aspect, self.chk_mask]

        def _tab_adjust(self):
            tab = tk.Frame(self.nb, padx=8, pady=8)
            self.nb.add(tab, text="Regolazioni")
            self.adj_frame = self._groupbox(tab, "Immagine (applicate in tempo reale)")
            self.adj_frame.pack(fill="both", expand=True)
            self.adj_audio = self._groupbox(tab, "Volume di registrazione della scheda")
            self.adj_audio.pack(fill="x", pady=(6, 0))
            row = tk.Frame(tab, pady=6)
            row.pack(fill="x")
            self._button(row, "Valori predefiniti", self.reset_controls).pack(side="left")
            self._button(row, "Rileggi", self.build_controls).pack(side="left", padx=4)
            self.ctrl_vars = {}

        def _tab_convert(self):
            tab = tk.Frame(self.nb, padx=8, pady=8)
            self.nb.add(tab, text="Converti")
            tk.Label(tab, justify="left", wraplength=400, anchor="w",
                     text="Crea copie MP4 (H.264 + AAC) leggere e compatibili con TV, "
                          "telefoni e computer, partendo dalle registrazioni d'archivio. "
                          "Funziona in secondo piano a bassa priorità.").pack(fill="x")
            g = self._groupbox(tab, "File da convertire")
            g.pack(fill="both", expand=True, pady=(6, 0))
            lf = self._bevel(g, "sunken", 2)
            lf.pack(fill="both", expand=True)
            self.conv_list = tk.Listbox(lf, height=6, bd=0, activestyle="none")
            self.conv_list.pack(side="left", fill="both", expand=True)
            sbar = tk.Scrollbar(lf, command=self.conv_list.yview)
            sbar.pack(side="right", fill="y")
            self.conv_list.configure(yscrollcommand=sbar.set)
            row = tk.Frame(g, pady=4)
            row.pack(fill="x")
            self._button(row, "Aggiungi...", self.conv_add).pack(side="left")
            self._button(row, "Rimuovi", self.conv_remove).pack(side="left", padx=4)

            g2 = self._groupbox(tab, "Opzioni")
            g2.pack(fill="x", pady=(6, 0))
            self.cb_crf, self.v_crf = self._combo(
                g2, values=["18 - alta", "22 - media", "26 - bassa"], width=20)
            self._row(g2, 0, "Qualità:", self.cb_crf)
            self.cb_speed, _ = self._combo(g2, "conv_speed",
                                           ["ultrafast", "veryfast", "medium", "slow"], width=20)
            self._row(g2, 1, "Velocità:", self.cb_speed)
            self.vars["conv_deinterlace"] = tk.BooleanVar()
            tk.Checkbutton(g2, text="Deinterlaccia (consigliato)",
                           variable=self.vars["conv_deinterlace"]).grid(
                row=2, column=0, columnspan=2, sticky="w")

            row = tk.Frame(tab, pady=6)
            row.pack(fill="x")
            self.btn_conv = self._button(row, "Avvia conversione", self.conv_start)
            self.btn_conv.pack(side="left")
            self.btn_conv_stop = self._button(row, "Annulla", self.conv_cancel)
            self.btn_conv_stop.pack(side="left", padx=4)
            self.conv_pb = ttk.Progressbar(tab, mode="determinate", maximum=1000)
            self.conv_pb.pack(fill="x")
            self.conv_lbl = tk.Label(tab, text="", anchor="w")
            self.conv_lbl.pack(fill="x")

        def _tab_log(self):
            tab = tk.Frame(self.nb, padx=8, pady=8)
            self.nb.add(tab, text="Registro")
            f = self._bevel(tab, "sunken", 2)
            f.pack(fill="both", expand=True)
            self.logtext = tk.Text(f, height=10, width=50, wrap="word", bd=0,
                                   font=("TkFixedFont", 8))
            self.logtext.pack(side="left", fill="both", expand=True)
            sb = tk.Scrollbar(f, command=self.logtext.yview)
            sb.pack(side="right", fill="y")
            self.logtext.configure(yscrollcommand=sb.set)
            self.log("%s %s - ffmpeg %s" % (APP_NAME, VERSION, ff.version_str))
            if not self.use_v4l2ctl:
                self.log("Nota: v4l2-ctl non trovato (pacchetto v4l-utils): ingressi e "
                         "regolazioni immagine limitati.")

        # ---------------------------------------------------------- utilità
        def log(self, line):
            ts = time.strftime("%H:%M:%S")
            self.logtext.insert("end", "[%s] %s\n" % (ts, line))
            if int(self.logtext.index("end-1c").split(".")[0]) > 2000:
                self.logtext.delete("1.0", "500.0")
            self.logtext.see("end")

        def status(self, text):
            self.st_main.configure(text=text)

        def _db_to_seg(self, db):
            return int(round((db + 48) / 48.0 * (self.vu_segments - 1)))

        def current_video(self):
            idx = self.cb_vdev.current()
            if 0 <= idx < len(self.video_devices):
                return self.video_devices[idx]
            return None

        # ---------------------------------------------------------- dispositivi
        def refresh_devices(self, first=False):
            if self.mode == "recording":
                return
            restart = self.mode == "preview" or (self.mode == "idle" and self.preview_wanted)
            if self.engine and self.engine.running():
                self.stop_engine(lambda: self._do_refresh(first, restart))
            else:
                self._do_refresh(first, restart)

        def _do_refresh(self, first, restart):
            self.status("Ricerca dei dispositivi in corso...")
            self.root.update_idletasks()
            self.auto_picked = False
            self.video_devices = list_video_devices(ff)
            self.video_devices.append(VideoDevice(TEST_SOURCE, "Segnale di prova (barre)"))
            self.video_devices[-1].formats["(prova)"] = ("generato", list(DEFAULT_SIZES))
            self.cb_vdev.configure(values=[d.label if d.path != TEST_SOURCE else d.name
                                           for d in self.video_devices])
            sel = -1
            if self.cfg.get("video_device_manual"):
                for i, d in enumerate(self.video_devices):
                    if d.path == self.cfg["video_device"]:
                        sel = i
                        break
            if sel < 0:
                # Scegli da solo la periferica che sembra una scheda di acquisizione
                # (non la webcam del portatile).
                aud = list_audio_devices(ff)
                scores = [d.capture_score(aud) for d in self.video_devices]
                sel = scores.index(max(scores)) if scores else 0
                if self.video_devices[sel].path != self.cfg["video_device"]:
                    self.auto_picked = True
            self.cb_vdev.current(sel)
            n = len(self.video_devices) - 1
            self.log("Trovati %d dispositivi video." % n)
            for d in self.video_devices[:-1]:
                self.log("  %s [%s]: %s" % (d.label, d.driver or "?",
                                            ", ".join(d.formats.keys()) or
                                            "nessun formato di acquisizione"))
            self.log("Sorgente video scelta: " + self.video_devices[sel].label)

            self.audio_devices = list_audio_devices(ff)
            self.cb_adev.configure(values=[a[1] for a in self.audio_devices])
            self._load_vars()
            self.on_video_device(initial=not self.auto_picked)
            self.status("Pronto" if n else "Nessuna scheda di acquisizione trovata: "
                                           "collegala e premi Aggiorna")
            if restart and not first:
                self.start_preview()

        def _load_vars(self):
            c = self.cfg
            for k in ("standard", "video_size", "framerate", "audio_rate", "container",
                      "aspect", "conv_speed"):
                self.vars[k].set(str(c[k]))
            for k in ("audio_monitor", "head_mask", "auto_stop_nosignal",
                      "preview_while_recording", "conv_deinterlace", "auto_recover"):
                self.vars[k].set(bool(c[k]))
            self.cb_split.current(list(SPLIT_VALUES.keys()).index(
                str(c["split_minutes"]) if str(c["split_minutes"]) in SPLIT_VALUES else "0"))
            self.vars["nosignal_minutes"].set(str(c["nosignal_minutes"]))
            self.v_ch.set({"1": "Mono", "2": "Stereo"}.get(str(c["audio_channels"]),
                                                           "Automatico"))
            if not str(c["audio_rate"]).isdigit():
                self.vars["audio_rate"].set("Automatico")
            key = c["preset"] if c["preset"] in self.preset_keys else self.preset_keys[0]
            self.cb_preset.current(self.preset_keys.index(key))
            self.cb_deint.current(list(DEINTERLACE.keys()).index(
                c["deinterlace"] if c["deinterlace"] in DEINTERLACE else "none"))
            self.cb_maxdur.current(list(MAX_DURATIONS.keys()).index(
                str(c["max_minutes"]) if str(c["max_minutes"]) in MAX_DURATIONS else "0"))
            crf = {"18": 0, "22": 1, "26": 2}.get(str(c["conv_crf"]), 1)
            self.cb_crf.current(crf)
            vals = [a[0] for a in self.audio_devices]
            self.cb_adev.current(vals.index(c["audio_device"])
                                 if c["audio_device"] in vals else 0)

        def on_user_video_device(self):
            self.cfg["video_device_manual"] = True   # scelta fatta a mano: va ricordata
            self.on_video_device()

        def on_video_device(self, initial=False):
            d = self.current_video()
            if not d:
                return
            # Ingressi (Composito / S-Video)
            if d.inputs:
                vals = ["%d - %s" % i for i in d.inputs]
            else:
                vals = ["Predefinito", "0", "1", "2", "3"]
            self.cb_input.configure(values=vals)
            want = str(self.cfg["video_input"]) if initial else ""
            idx = 0
            for i, v in enumerate(vals):
                if v.split(" ")[0] == want:
                    idx = i
            if not initial and d.inputs:
                # Preferisci l'ingresso "Composite" se esiste
                for i, (_, nm) in enumerate(d.inputs):
                    if "compos" in nm.lower():
                        idx = i
                        break
            self.cb_input.current(idx)
            # Formati pixel
            fmts = list(d.formats.keys())
            labels = ["Automatico"] + ["%s (%s)" % (k, d.formats[k][0]) for k in fmts]
            self.fmt_keys = [""] + fmts
            self.cb_fmt.configure(values=labels)
            cur = self.cfg["input_format"] if initial else ""
            if not initial:
                # Preferisci il formato grezzo YUYV (qualità piena) se disponibile
                for k in fmts:
                    if k in ("yuyv422", "uyvy422"):
                        cur = k
                        break
            self.cb_fmt.current(self.fmt_keys.index(cur) if cur in self.fmt_keys else 0)
            self.on_format(restart=False, reset_size=not initial)
            if not initial:
                self.vars["framerate"].set(
                    "29.97" if self.vars["standard"].get() in ("NTSC", "PAL-M", "NTSC-JP")
                    else "25")
            if self.cb_adev.current() < 0 and self.audio_devices:
                self.cb_adev.current(0)   # audio della scheda di acquisizione
            self.save_vars()
            self.update_audio_info(log=True)
            self.build_controls()
            if not initial:
                self.on_source_change()

        def on_format(self, restart=True, reset_size=False):
            d = self.current_video()
            if not d:
                return
            idx = self.cb_fmt.current()
            key = self.fmt_keys[idx] if 0 <= idx < len(self.fmt_keys) else ""
            sizes = []
            if key and key in d.formats:
                sizes = d.formats[key][1]
            else:
                for _, (_, s) in d.formats.items():
                    for x in s:
                        if x not in sizes:
                            sizes.append(x)
            sizes = sizes or list(DEFAULT_SIZES)
            self.cb_size.configure(values=sizes)
            if reset_size or self.vars["video_size"].get() not in sizes:
                # Risoluzione nativa delle videocassette, non quella HD della scheda
                pref = "720x480" if self.vars["standard"].get().startswith("NTSC") else "720x576"
                self.vars["video_size"].set(pref if pref in sizes else sizes[0])
            if restart:
                self.on_source_change()

        def on_standard(self):
            std = self.vars["standard"].get()
            sizes = self.cb_size.cget("values")
            if std in ("NTSC", "PAL-M", "NTSC-JP"):
                self.vars["framerate"].set("29.97")
                if "720x480" in sizes:
                    self.vars["video_size"].set("720x480")
            elif std in ("PAL", "SECAM", "PAL-Nc"):
                self.vars["framerate"].set("25")
                if "720x576" in sizes:
                    self.vars["video_size"].set("720x576")
            self.on_source_change()

        def on_aspect(self):
            self.save_vars()
            self.on_preview_size()

        def on_preview_size(self):
            self.save_vars()
            w = PREVIEW_SIZES.get(self.cfg["preview_size"], 384)
            h = w * 3 // 4
            self.canvas.configure(width=w, height=h)
            self.canvas.coords(self.img_item, w // 2, h // 2)
            self.canvas.coords(self.osd_msg, w // 2, h // 2)
            if self.mode == "preview":
                self.schedule_restart()

        def on_source_change(self):
            self.save_vars()
            self.update_audio_info()
            self.build_audio_controls()
            # Riavvia l'anteprima anche se si era fermata per un errore
            if self.mode == "preview" or (self.mode == "idle" and self.preview_wanted):
                self.schedule_restart()

        def schedule_restart(self):
            if self.restart_job:
                self.root.after_cancel(self.restart_job)
            self.restart_job = self.root.after(400, self._restart_preview)

        def _restart_preview(self):
            self.restart_job = None
            if self.mode == "preview":
                self.stop_engine(self.start_preview)
            elif self.mode == "idle" and not self.engine:
                self.start_preview()

        def save_vars(self, *a):
            c = self.cfg
            d = self.current_video()
            if d:
                c["video_device"] = d.path
            inp = self.v_input.get().split(" ")[0]
            c["video_input"] = inp if inp.isdigit() else ""
            for k in ("standard", "video_size", "framerate", "audio_rate", "container",
                      "aspect", "output_dir", "name_template", "preview_size",
                      "nosignal_minutes", "conv_speed"):
                c[k] = self.vars[k].get()
            for k in ("audio_monitor", "head_mask", "auto_stop_nosignal",
                      "preview_while_recording", "conv_deinterlace", "auto_recover"):
                c[k] = bool(self.vars[k].get())
            c["split_minutes"] = list(SPLIT_VALUES.keys())[max(0, self.cb_split.current())]
            idx = self.cb_fmt.current()
            c["input_format"] = self.fmt_keys[idx] if 0 < idx < len(self.fmt_keys) else ""
            if c["input_format"] == "(prova)":
                c["input_format"] = ""
            idx = self.cb_adev.current()
            if 0 <= idx < len(self.audio_devices):
                c["audio_device"] = self.audio_devices[idx][0]
            c["audio_channels"] = {"Mono": "1", "Stereo": "2"}.get(self.v_ch.get(), "auto")
            if not str(c["audio_rate"]).isdigit():
                c["audio_rate"] = "auto"
            idx = self.cb_preset.current()
            if 0 <= idx < len(self.preset_keys):
                c["preset"] = self.preset_keys[idx]
            c["deinterlace"] = list(DEINTERLACE.keys())[max(0, self.cb_deint.current())]
            c["max_minutes"] = list(MAX_DURATIONS.keys())[max(0, self.cb_maxdur.current())]
            c["conv_crf"] = self.v_crf.get().split(" ")[0] or "22"
            save_config(c)

        # ---------------------------------------------------------- regolazioni
        def build_audio_controls(self):
            for w in self.adj_audio.winfo_children():
                w.destroy()
            _, _, _, entry, _ = self.resolve_audio()
            if not entry or not entry[3]:
                tk.Label(self.adj_audio, text="Nessuna scheda audio ALSA selezionata.").grid(
                    row=0, column=0, sticky="w")
                return
            if not shutil.which("amixer"):
                tk.Label(self.adj_audio, text="Installa alsa-utils per regolare il volume.").grid(
                    row=0, column=0, sticky="w")
                return
            card = entry[3]["id"]
            ctrls = [c for c in alsa_capture_controls(card) if c[1] is not None]
            if not ctrls:
                tk.Label(self.adj_audio, text="La scheda non ha un volume di registrazione "
                                              "regolabile.").grid(row=0, column=0, sticky="w")
                return
            for r, (name, pct, _, _) in enumerate(ctrls[:4]):
                tk.Label(self.adj_audio, text=name + ":", anchor="w").grid(
                    row=r, column=0, sticky="w")
                v = tk.IntVar(value=pct)
                tk.Scale(self.adj_audio, from_=0, to=100, orient="horizontal", variable=v,
                         length=230, sliderrelief="raised", bd=1, troughcolor="#bfbbb3",
                         sliderlength=18, width=12,
                         command=lambda val, n=name: alsa_set_capture(
                             card, n, int(float(val)))).grid(row=r, column=1, sticky="we")
            self.adj_audio.columnconfigure(1, weight=1)

        def build_controls(self):
            self.build_audio_controls()
            for w in self.adj_frame.winfo_children():
                w.destroy()
            self.ctrl_vars = {}
            d = self.current_video()
            if not d or d.path == TEST_SOURCE:
                tk.Label(self.adj_frame, text="Nessuna regolazione per questa sorgente.").pack()
                return
            if not self.use_v4l2ctl:
                tk.Label(self.adj_frame, justify="left",
                         text="Per regolare luminosità, contrasto e colore installa\n"
                              "il pacchetto v4l-utils:\n\n   sudo apt install v4l-utils").pack()
                return
            ctrls = v4l2_controls(d.path)
            if not ctrls:
                tk.Label(self.adj_frame, text="La scheda non espone regolazioni.").pack()
                return
            names = {"brightness": "Luminosità", "contrast": "Contrasto",
                     "saturation": "Saturazione", "hue": "Tinta", "sharpness": "Nitidezza",
                     "gamma": "Gamma", "chroma_agc": "Colore automatico",
                     "audio_volume": "Volume audio", "audio_mute": "Audio muto"}
            self.ctrl_info = {}
            for r, (name, typ, mn, mx, step, dflt, val) in enumerate(ctrls[:12]):
                self.ctrl_info[name] = dflt
                label = names.get(name, name.replace("_", " ").capitalize())
                tk.Label(self.adj_frame, text=label + ":", anchor="w").grid(
                    row=r, column=0, sticky="w")
                if typ == "bool":
                    v = tk.BooleanVar(value=bool(val))
                    w = tk.Checkbutton(self.adj_frame, variable=v,
                                       command=lambda n=name, v=v: self.set_control(
                                           n, int(v.get())))
                else:
                    v = tk.IntVar(value=val)
                    w = tk.Scale(self.adj_frame, from_=mn, to=mx, resolution=step,
                                 orient="horizontal", variable=v, length=230,
                                 showvalue=True, sliderrelief="raised", bd=1,
                                 troughcolor="#bfbbb3", sliderlength=18, width=12,
                                 command=lambda s, n=name: self.set_control(n, int(float(s))))
                w.grid(row=r, column=1, sticky="we")
                self.ctrl_vars[name] = v
            self.adj_frame.columnconfigure(1, weight=1)

        def set_control(self, name, value):
            d = self.current_video()
            if not d:
                return
            run_cmd(["v4l2-ctl", "-d", d.path, "-c", "%s=%d" % (name, value)], timeout=3)
            self.cfg["controls"].setdefault(d.name, {})[name] = value
            save_config(self.cfg)

        def reset_controls(self):
            d = self.current_video()
            if not d or not self.ctrl_vars:
                return
            for name, v in self.ctrl_vars.items():
                dflt = self.ctrl_info.get(name, 0)
                v.set(dflt)
                run_cmd(["v4l2-ctl", "-d", d.path, "-c", "%s=%d" % (name, dflt)], timeout=3)
            self.cfg["controls"].pop(d.name, None)
            save_config(self.cfg)

        def apply_device_setup(self, d):
            """Imposta ingresso, standard e regolazioni salvate tramite v4l2-ctl."""
            if not self.use_v4l2ctl or d.path == TEST_SOURCE:
                return
            if d.is_uvc:
                pass   # UVC: nessun ingresso/standard da impostare
            elif self.cfg["video_input"] != "":
                run_cmd(["v4l2-ctl", "-d", d.path, "-i", str(self.cfg["video_input"])])
            std = self.cfg["standard"]
            if std and std != "Automatico" and not d.is_uvc:
                rc, out = run_cmd(["v4l2-ctl", "-d", d.path, "-s", std.lower()])
                if rc != 0:
                    self.log("Impossibile impostare lo standard %s: %s" % (std, out.strip()))
            saved = self.cfg["controls"].get(d.name) or {}
            if saved:
                run_cmd(["v4l2-ctl", "-d", d.path, "-c",
                         ",".join("%s=%d" % kv for kv in saved.items())])

        # ---------------------------------------------------------- audio
        def resolve_audio(self):
            """Audio effettivo da usare: (valore, canali, frequenza, voce, formato nativo)."""
            sel = self.cfg["audio_device"] or CARD_AUDIO
            d = self.current_video()
            entry = None
            if sel == CARD_AUDIO:
                if d is not None and d.path == TEST_SOURCE:
                    value = TEST_SOURCE
                else:
                    entry = find_card_audio(self.audio_devices, d)
                    value = entry[0] if entry else NO_AUDIO
            else:
                value = sel
                for a in self.audio_devices:
                    if a[0] == sel:
                        entry = a
            native = alsa_native_format(entry[3]["num"]) if entry and entry[3] else None
            ch = str(self.cfg["audio_channels"])
            if ch not in ("1", "2"):
                ch = ("1" if native[0] == 1 else "2") if native else "2"
            rate = str(self.cfg["audio_rate"])
            if not rate.isdigit():
                rate = str(native[1]) if native else "48000"
            return value, ch, rate, entry, native

        def update_audio_info(self, log=False):
            value, ch, rate, entry, native = self.resolve_audio()
            auto = (self.cfg["audio_device"] or CARD_AUDIO) == CARD_AUDIO
            if value == NO_AUDIO and auto:
                txt = ("\u26a0 Audio della scheda non trovato: controlla il cavo USB e "
                       "premi Aggiorna, oppure scegli il dispositivo a mano.")
                self.lbl_audio.configure(text=txt, fg="#c00000")
                if log:
                    self.log("ATTENZIONE: audio della scheda di acquisizione non trovato.")
                return
            name = entry[1] if entry else {TEST_SOURCE: "tono di prova",
                                           NO_AUDIO: "nessun audio"}.get(value, value)
            txt = ("\u2192 " if auto else "") + name
            if value != NO_AUDIO:
                txt += "\n%s, %s Hz" % ("mono" if ch == "1" else "stereo", rate)
                if native:
                    txt += "  (formato nativo della scheda: %d canali, %d Hz)" % native
            self.lbl_audio.configure(text=txt, fg="#000080")
            if log and auto and entry:
                self.log("Audio della scheda di acquisizione: %s" % entry[1])

        def prepare_card_audio(self, entry):
            """Attiva l'ingresso di registrazione del mixer della scheda (alcune schede
            partono con la cattura disattivata e registrano silenzio)."""
            if not entry or not entry[3]:
                return
            for name, pct, has_sw, on in alsa_capture_controls(entry[3]["id"]):
                if has_sw and not on:
                    if alsa_set_capture(entry[3]["id"], name):
                        self.log("Attivato l'ingresso audio '%s' della scheda." % name)
                if pct == 0:
                    self.log("Attenzione: il volume di registrazione '%s' è a 0%%: "
                             "alzalo nella scheda Regolazioni." % name)

        # ---------------------------------------------------------- motore
        def launch(self, record_path=None, seg=0, quiet=False):
            d = self.current_video()
            if not d:
                self.status("Nessun dispositivo video selezionato")
                return False
            self.save_vars()
            self.apply_device_setup(d)
            avalue, ach, arate, aentry, _ = self.resolve_audio()
            self.prepare_card_audio(aentry)
            eff = dict(self.cfg, audio_device=avalue, audio_channels=ach, audio_rate=arate)
            eng = CaptureEngine()
            pfps = 12
            if record_path and not self.cfg["preview_while_recording"]:
                pfps = 1
            title = ""
            if record_path:
                title = os.path.splitext(os.path.basename(self.rec["base"]))[0]
            try:
                eng.start(lambda fd, mfd: build_ffmpeg_cmd(
                    ff, eff, record_path, fd, pfps,
                    use_v4l2ctl=self.use_v4l2ctl and d.path != TEST_SOURCE, title=title,
                    tv_controls=not d.is_uvc,
                    segment_seconds=seg, monitor_fd=mfd),
                    ach, monitor=bool(self.cfg["audio_monitor"]) and avalue != NO_AUDIO)
            except RuntimeError as e:
                self.log(str(e))
                if not quiet:
                    messagebox.showerror(APP_NAME, str(e))
                return False
            eng.record_path = record_path
            self.engine = eng
            self.last_frame_id = -1
            self.nosignal_since = None
            self.signal_ok_time = 0.0
            return True

        def stop_engine(self, then=None):
            self.after_stop = then
            if self.engine and self.engine.running():
                self.engine.stop()
            else:
                self._engine_stopped()

        def _engine_stopped(self, recovering=False):
            eng = self.engine
            if eng:
                for line in eng.pop_log():
                    self.log(line)
                if self.rec and eng.record_path:
                    p = eng.progress
                    self.rec["drop"] += int(p.get("drop_frames", "0") or 0)
                    self.rec["dup"] += int(p.get("dup_frames", "0") or 0)
            was = self.mode
            self.mode = "idle"
            self.engine = None
            if was == "recording" and not recovering:
                self._finish_recording()
            self._update_buttons()
            cb, self.after_stop = self.after_stop, None
            if cb:
                cb()

        def start_preview(self):
            if self.mode != "idle":
                return
            self.preview_wanted = True
            self.canvas.itemconfigure(self.osd_msg, text="AVVIO...")
            if self.launch():
                self.mode = "preview"
                self.status("Anteprima in corso - premi REGISTRA e poi PLAY sul videoregistratore")
            self._update_buttons()

        def on_record(self):
            if self.mode in ("recording", "recovering") or (self.engine and self.engine.stopping):
                return
            if self.conv_proc and self.conv_proc.poll() is None:
                if not messagebox.askyesno(
                        APP_NAME, "È in corso una conversione: su un PC lento potrebbe "
                                  "far perdere fotogrammi.\nRegistrare comunque?"):
                    return
            self.save_vars()
            outdir = os.path.expanduser(self.cfg["output_dir"])
            try:
                os.makedirs(outdir, exist_ok=True)
            except OSError as e:
                messagebox.showerror(APP_NAME, "Cartella non valida:\n%s" % e)
                return
            if not os.access(outdir, os.W_OK):
                messagebox.showerror(APP_NAME, "Non hai i permessi di scrittura in:\n" + outdir)
                return
            free = shutil.disk_usage(outdir).free
            if free < 2 * 1024 ** 3 and not messagebox.askyesno(
                    APP_NAME, "Spazio libero ridotto (%s). Continuare?" % fmt_size(free)):
                return
            if self.cfg["preset"] == "copy" and self.cfg["input_format"] not in ("mjpeg", "h264"):
                messagebox.showwarning(
                    APP_NAME, "La 'Copia diretta' funziona solo con formato pixel MJPEG o "
                              "H.264.\nScegli il formato nella scheda Sorgente o cambia codifica.")
                return
            if self.resolve_audio()[0] == NO_AUDIO and self.cfg["audio_device"] != NO_AUDIO:
                if not messagebox.askyesno(
                        APP_NAME, "L'audio della scheda di acquisizione non è stato trovato.\n\n"
                                  "Controlla il collegamento USB e premi 'Aggiorna' nella "
                                  "scheda Sorgente.\n\nRegistrare comunque SENZA audio?",
                        icon="warning"):
                    return
            seg = int(self.cfg["split_minutes"] or 0) * 60
            fst = filesystem_type(outdir)
            if fst in ("vfat", "msdos", "fat") and not seg:
                seg = 30 * 60
                messagebox.showinfo(
                    APP_NAME, "La cartella scelta è su un disco FAT32, che non accetta file "
                              "più grandi di 4 GB.\n\nLa registrazione verrà divisa "
                              "automaticamente in file da 30 minuti.")
            name = self.cfg["name_template"].strip() or DEFAULTS["name_template"]
            try:
                name = time.strftime(name)
            except ValueError:
                pass
            name = re.sub(r"[/\\:*?\"<>|]+", "_", name).strip() or "Cassetta"
            ext = "." + self.cfg["container"]
            base = os.path.join(outdir, name)
            n = 2
            while glob.glob(glob.escape(base) + ext) or glob.glob(glob.escape(base) + "_*" + ext):
                base = os.path.join(outdir, "%s_%d" % (name, n))
                n += 1
            self.preview_wanted = self.mode == "preview" or self.preview_wanted
            self.rec = {"base": base, "ext": ext, "seg": seg, "part": 1, "start": None,
                        "recoveries": 0, "files": [], "drop": 0, "dup": 0}
            self.chapters = []
            self.stop_engine(self._start_part)

        def _part_path(self):
            r = self.rec
            suffix = "" if r["part"] == 1 else "_parte%d" % r["part"]
            if r["seg"]:
                suffix += "_%03d"
            return r["base"] + suffix + r["ext"]

        def rec_files(self):
            files = []
            for pat in (self.rec or {}).get("files", []):
                if "%03d" in pat:
                    files += sorted(glob.glob(glob.escape(pat).replace(
                        "%03d", "[0-9][0-9][0-9]")))
                elif os.path.exists(pat):
                    files.append(pat)
            return files

        def _start_part(self):
            r = self.rec
            if not r:
                return
            path = self._part_path()
            if path not in r["files"]:
                r["files"].append(path)
            if self.launch(path, r["seg"], quiet=r["part"] > 1 or r["recoveries"] > 0):
                self.mode = "recording"
                if r["start"] is None:
                    r["start"] = time.monotonic()
                    if self.inhibitor.start():
                        self.log("Sospensione automatica del PC bloccata durante la registrazione.")
                self.log("Registrazione avviata: " + path)
                self.status("REGISTRAZIONE IN CORSO -> " + os.path.basename(path))
            elif r["start"] is not None:
                self._schedule_recovery("avvio non riuscito")
            else:
                self.rec = None
            self._update_buttons()

        def _schedule_recovery(self, why):
            r = self.rec
            r["recoveries"] += 1
            if r["recoveries"] > 50:
                self.log("Troppi tentativi di ripresa: registrazione terminata.")
                self.mode = "recording"
                self._finish_recording()
                self.mode = "idle"
                self._update_buttons()
                messagebox.showerror(APP_NAME, "La scheda di acquisizione non risponde più.\n"
                                               "Le parti registrate sono state salvate.")
                return
            # Nuova parte solo se quella attuale contiene qualcosa
            files = self.rec_files()
            cur = self._part_path()
            used = [f for f in files if os.path.getsize(f) > 0]
            if used and (cur in used or "%03d" in cur):
                r["part"] += 1
            self.mode = "recovering"
            self.log("ATTENZIONE: acquisizione interrotta (%s). Ripresa automatica fra 3 s "
                     "(parte %d, tentativo %d)." % (why, r["part"], r["recoveries"]))
            self.status("Acquisizione interrotta: ripresa automatica in corso...")
            self._update_buttons()
            self.recover_job = self.root.after(3000, self._recover)

        def _recover(self):
            self.recover_job = None
            if self.mode != "recovering":
                return
            self.mode = "idle"
            self._start_part()
            if self.mode == "idle" and self.rec:
                self.mode = "recovering"

        def _finish_recording(self):
            r = self.rec
            self.inhibitor.stop()
            if not r:
                return
            dur = time.monotonic() - (r["start"] or time.monotonic())
            files = self.rec_files()
            size = sum(os.path.getsize(f) for f in files)
            self.log("Registrazione terminata: %s, %s in %d file" % (
                fmt_hms(dur), fmt_size(size), len(files)))
            for f in files:
                self.log("   " + f)
            if r["recoveries"]:
                self.log("La registrazione è stata ripresa automaticamente %d volte."
                         % r["recoveries"])
            if self.chapters:
                self._write_chapters(r["base"] + r["ext"])
            self.status("Salvato: %s  (%s, %s)" % (os.path.basename(r["base"] + r["ext"]),
                                                   fmt_hms(dur), fmt_size(size)))
            self.last_duration = dur
            self.rec = None

        def _write_chapters(self, path):
            base = os.path.splitext(path)[0]
            try:
                with open(base + ".capitoli.txt", "w") as f:
                    for i, t in enumerate(self.chapters, 1):
                        f.write("%s  Capitolo %d\n" % (fmt_hms(t), i))
                # Formato FFMETADATA, utile per inserire i capitoli con ffmpeg
                with open(base + ".ffmetadata", "w") as f:
                    f.write(";FFMETADATA1\n")
                    marks = [0.0] + self.chapters
                    for i, t in enumerate(marks):
                        end = marks[i + 1] if i + 1 < len(marks) else t + 1
                        f.write("[CHAPTER]\nTIMEBASE=1/1000\nSTART=%d\nEND=%d\n"
                                "title=Capitolo %d\n" % (t * 1000, end * 1000, i + 1))
                self.log("Capitoli salvati in %s.capitoli.txt" % base)
            except OSError as e:
                self.log("Impossibile salvare i capitoli: %s" % e)

        def on_stop(self):
            if self.mode == "recovering":
                if self.recover_job:
                    self.root.after_cancel(self.recover_job)
                    self.recover_job = None
                self.mode = "recording"
                self._finish_recording()
                self.mode = "idle"
                self._after_record_stop()
            elif self.mode == "recording":
                self.status("Chiusura del file in corso...")
                self.stop_engine(self._after_record_stop)
            elif self.mode == "preview":
                self.preview_wanted = False
                self.stop_engine(self._show_idle)
            self._update_buttons()

        def _after_record_stop(self):
            if self.preview_wanted:
                self.root.after(300, self.start_preview)
            else:
                self._show_idle()

        def _show_idle(self):
            self.photo.blank()
            self.canvas.itemconfigure(self.osd_msg, text="STOP")
            self.canvas.itemconfigure(self.osd_rec, text="")

        def on_mark(self):
            if self.mode not in ("recording", "recovering") or not self.rec:
                return
            t = time.monotonic() - self.rec["start"]
            self.chapters.append(t)
            self.log("Capitolo %d segnato a %s" % (len(self.chapters), fmt_hms(t)))
            self.status("Capitolo %d segnato a %s" % (len(self.chapters), fmt_hms(t)))

        def _update_buttons(self):
            rec = self.mode in ("recording", "recovering")
            stopping = bool(self.engine and self.engine.stopping)
            self.btn_prev.configure(state="normal" if self.mode == "idle" else "disabled")
            self.btn_rec.configure(state="disabled" if rec or stopping else "normal",
                                   relief="sunken" if rec else "raised")
            self.btn_stop.configure(state="normal" if self.mode != "idle" else "disabled")
            self.btn_mark.configure(state="normal" if rec else "disabled")
            for w in self.lock_widgets:
                try:
                    if isinstance(w, ttk.Combobox):
                        w.configure(state="disabled" if rec else "readonly")
                    else:
                        w.configure(state="disabled" if rec else "normal")
                except tk.TclError:
                    pass

        # ---------------------------------------------------------- ciclo UI
        def poll(self):
            try:
                self._poll()
            finally:
                self.root.after(60, self.poll)

        def _poll(self):
            eng = self.engine
            now = time.monotonic()
            if eng:
                if eng.frame_id != self.last_frame_id and eng.frame is not None:
                    self.last_frame_id = eng.frame_id
                    self.last_error_key = None
                    frame = eng.frame
                    show = self.mode == "preview" or self.cfg["preview_while_recording"]
                    if show:
                        try:
                            self.photo.configure(data=frame[2], format="PPM")
                        except tk.TclError:
                            pass
                    uniform = frame_is_uniform(frame)
                    if uniform:
                        if self.nosignal_since is None:
                            self.nosignal_since = now
                    else:
                        self.nosignal_since = None
                        self.signal_ok_time = self.signal_ok_time or now
                    self.nosignal = uniform and now - (self.nosignal_since or now) > 1.0
                    msg = "NESSUN SEGNALE" if self.nosignal else ""
                    if not show:
                        msg = "Anteprima disattivata"
                    self.canvas.itemconfigure(self.osd_msg, text=msg)
                    if not show:
                        self.photo.blank()
                peaks = eng.take_peaks()
                self._draw_vu(peaks, now)
                if not eng.running():
                    self._engine_exited(eng)
            else:
                self._draw_vu([0.0, 0.0], now)

        def _draw_vu(self, peaks, now):
            for c in range(2):
                p = peaks[c]
                # caduta lenta stile VU analogico
                self.meter_disp[c] = max(p, self.meter_disp[c] * 0.82)
                if p >= 0.995:
                    self.clip_until = now + 2
                db = 20 * math.log10(self.meter_disp[c]) if self.meter_disp[c] > 1e-5 else -99
                lit = self._db_to_seg(db) + 1 if db > -48 else 0
                for i, it in enumerate(self.vu_items[c]):
                    if i < lit:
                        segdb = -48 + 48.0 * i / (self.vu_segments - 1)
                        col = "#ff3030" if segdb > -3 else ("#ffd800" if segdb > -12 else LCD_FG)
                    else:
                        col = LCD_DIM
                    self.lcd.itemconfigure(it, fill=col)
            self.lcd.itemconfigure(self.lcd_clip, text="CLIP" if now < self.clip_until else "")

        def _engine_exited(self, eng):
            if eng.stopping:
                self._engine_stopped()
                return
            # Uscita inattesa di ffmpeg
            code = eng.proc.returncode
            was = self.mode
            tail = eng.tail()
            self.after_stop = None
            if was == "recording" and self.rec and self.cfg["auto_recover"]:
                self._engine_stopped(recovering=True)
                self._schedule_recovery("codice %s" % code)
                return
            self._engine_stopped()
            hint = explain_error(tail)
            if was == "recording":
                title = "La registrazione si è interrotta!"
                extra = "\n\nLa parte registrata finora è stata salvata."
            else:
                title = "Impossibile avviare l'acquisizione"
                extra = ""
            self.status(title + " - cambia sorgente o impostazioni, oppure premi Anteprima "
                                "per riprovare")
            self.canvas.itemconfigure(self.osd_msg, text="ERRORE\n\nCambia sorgente o\n"
                                                         "premi Anteprima")
            self.log("ERRORE: %s (codice %s)" % (title, code))
            # Non ripetere la stessa finestra di errore a ogni tentativo
            key = (was, hint, re.sub(r"0x[0-9a-f]+|\d+", "", tail[-300:]))
            if was == "recording" or key != getattr(self, "last_error_key", None):
                self.last_error_key = key
                messagebox.showerror(APP_NAME, "%s (codice %s)%s\n\n%s\n\n%s" % (
                    title, code, extra, hint or "Dettagli:", tail[-900:]))

        def slow_poll(self):
            try:
                self._slow_poll()
            finally:
                self.root.after(500, self.slow_poll)

        def _slow_poll(self):
            now = time.monotonic()
            eng = self.engine
            if eng:
                for line in eng.pop_log():
                    self.log(line)
            self.blink = not self.blink
            outdir = os.path.expanduser(self.cfg["output_dir"])
            try:
                free = shutil.disk_usage(outdir).free
            except OSError:
                free = None
            info = "%s %s %s" % (self.cfg["standard"].upper()[:6],
                                 self.cfg["video_size"], self.cfg["framerate"] + "fps")
            self.lcd.itemconfigure(self.lcd_info, text=info)

            r = self.rec
            if self.mode in ("recording", "recovering") and r and r["start"] is not None:
                el = now - r["start"]
                self.lcd.itemconfigure(self.lcd_time, text=fmt_hms(el))
                dot = "●" if self.blink else " "
                if self.mode == "recovering":
                    state = (dot + " RIPRESA IN CORSO...")
                else:
                    state = dot + " REC   " + os.path.basename(r["base"])[:22]
                self.lcd.itemconfigure(self.lcd_state, text=state)
                self.canvas.itemconfigure(self.osd_rec, text=dot + " REC " + fmt_hms(el))
                if self.blink:  # dimensione dei file ogni secondo
                    files = self.rec_files()
                    self.rec_size = sum(os.path.getsize(f) for f in files
                                        if os.path.exists(f))
                    self.st_size.configure(text="File: %s%s" % (
                        fmt_size(self.rec_size),
                        "  (%d parti)" % len(files) if len(files) > 1 else ""))
                drop, dup = r["drop"], r["dup"]
                fps = "-"
                if eng and self.mode == "recording":
                    p = eng.progress
                    drop += int(p.get("drop_frames", "0") or 0)
                    dup += int(p.get("dup_frames", "0") or 0)
                    fps = p.get("fps", "-")
                self.st_fps.configure(text="fps %s  persi %d  dupl. %d" % (fps, drop, dup),
                                      fg="#c00000" if drop > 25 else "black")
                size = getattr(self, "rec_size", 0)
                if free is not None and el > 10 and size > 0:
                    self.st_disk.configure(text="Libero %s (~%s)" % (
                        fmt_size(free), fmt_hms(free / (size / el))))
                if self.mode == "recording" and eng:
                    self._watchdog(eng, now)
                    self._check_autostop(el, now, free)
            else:
                st = {"preview": "ANTEPRIMA", "idle": "STOP"}.get(self.mode, "")
                if eng and eng.stopping:
                    st = "ATTENDERE..."
                self.lcd.itemconfigure(self.lcd_state, text=st)
                self.canvas.itemconfigure(self.osd_rec, text="")
                if self.mode == "idle" and self.last_duration is not None:
                    self.lcd.itemconfigure(self.lcd_time, text=fmt_hms(self.last_duration))
                else:
                    self.lcd.itemconfigure(self.lcd_time, text="00:00:00")
                self.st_size.configure(text="")
                self.st_fps.configure(text="")
                if free is not None:
                    self.st_disk.configure(text="Spazio libero: " + fmt_size(free))
            self._conv_poll()

        def _watchdog(self, eng, now):
            """Se ffmpeg non produce fotogrammi per 20 s la scheda è bloccata:
            si forza il riavvio (la registrazione riprende in un nuovo file)."""
            if eng.stopping or getattr(eng, "aborting", False):
                return
            stalled = now - eng.last_progress_time
            if stalled > 20:
                eng.aborting = True
                self.log("Nessun fotogramma ricevuto da %d s: la scheda sembra bloccata, "
                         "riavvio dell'acquisizione." % stalled)
                eng.abort()

        def _check_autostop(self, el, now, free):
            maxm = int(self.cfg["max_minutes"] or 0)
            if maxm and el >= maxm * 60:
                self.log("Durata massima raggiunta: arresto automatico.")
                self.on_stop()
                return
            if free is not None and free < 300 * 1024 ** 2:
                self.log("Spazio su disco quasi esaurito: arresto automatico.")
                self.on_stop()
                messagebox.showwarning(APP_NAME, "Disco quasi pieno: registrazione fermata.\n"
                                                 "Il file registrato è stato salvato.")
                return
            if self.cfg["auto_stop_nosignal"] and self.signal_ok_time and \
                    self.nosignal_since and now - self.signal_ok_time > 10:
                try:
                    mins = float(self.cfg["nosignal_minutes"])
                except ValueError:
                    mins = 2
                if now - self.nosignal_since >= mins * 60:
                    self.log("Fine nastro rilevata (nessun segnale da %g min): arresto."
                             % mins)
                    self.on_stop()

        # ---------------------------------------------------------- conversione
        def conv_add(self):
            files = filedialog.askopenfilenames(
                parent=self.root, initialdir=os.path.expanduser(self.cfg["output_dir"]),
                title="Scegli le registrazioni",
                filetypes=[("Video", "*.mkv *.avi *.mp4 *.mov *.mpg"), ("Tutti", "*")])
            for f in files or ():
                self.conv_list.insert("end", f)

        def conv_remove(self):
            for i in reversed(self.conv_list.curselection()):
                self.conv_list.delete(i)

        def conv_start(self):
            if self.conv_proc and self.conv_proc.poll() is None:
                return
            if self.mode in ("recording", "recovering"):
                messagebox.showinfo(APP_NAME, "Attendi la fine della registrazione: "
                                              "convertire insieme rallenta il PC.")
                return
            self.conv_queue = list(self.conv_list.get(0, "end"))
            if not self.conv_queue:
                messagebox.showinfo(APP_NAME, "Aggiungi almeno un file da convertire.")
                return
            self.save_vars()
            self._conv_next()

        def _conv_next(self):
            if not self.conv_queue:
                self.conv_lbl.configure(text="Conversione completata.")
                self.conv_pb["value"] = 1000
                self.conv_proc = None
                return
            src = self.conv_queue.pop(0)
            dst = os.path.splitext(src)[0] + "_convertito.mp4"
            n = 2
            while os.path.exists(dst):
                dst = os.path.splitext(src)[0] + "_convertito_%d.mp4" % n
                n += 1
            self.conv_duration = self._probe_duration(src)
            vf = ["yadif=0:-1:0"] if self.cfg["conv_deinterlace"] else []
            vf.append("format=yuv420p")
            cmd = ([shutil.which("nice"), "-n", "15"] if shutil.which("nice") else []) + [
                ff.path, "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
                "-progress", "pipe:1", "-i", src, "-map", "0:v:0", "-map", "0:a?",
                "-vf", ",".join(vf), "-c:v", "libx264", "-preset", self.cfg["conv_speed"],
                "-crf", str(self.cfg["conv_crf"]), "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", dst]
            self.log("Conversione: " + " ".join(shlex.quote(c) for c in cmd))
            try:
                self.conv_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                                  stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE,
                                                  universal_newlines=True, errors="replace")
            except OSError as e:
                self.conv_lbl.configure(text="Errore: %s" % e)
                return
            self.conv_cur = (src, dst)
            self.conv_time = 0.0
            self.conv_err = collections.deque(maxlen=10)

            def reader(p):
                for line in p.stdout:
                    m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                    if m:
                        self.conv_time = int(m.group(1)) / 1e6

            def ereader(p):
                for line in p.stderr:
                    self.conv_err.append(line.rstrip())
            for fn in (reader, ereader):
                t = threading.Thread(target=fn, args=(self.conv_proc,))
                t.daemon = True
                t.start()
            self.conv_lbl.configure(text="Conversione di " + os.path.basename(src))

        def _probe_duration(self, path):
            if ff.ffprobe:
                _, out = run_cmd([ff.ffprobe, "-v", "error", "-show_entries",
                                  "format=duration", "-of", "default=nw=1:nk=1", path])
                try:
                    return float(out.strip().splitlines()[0])
                except (ValueError, IndexError):
                    pass
            _, out = run_cmd([ff.path, "-hide_banner", "-i", path])
            m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out)
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            return 0

        def _conv_poll(self):
            p = self.conv_proc
            if not p:
                return
            if p.poll() is None:
                if self.conv_duration:
                    frac = min(1.0, self.conv_time / self.conv_duration)
                    self.conv_pb["value"] = int(frac * 1000)
                    self.conv_lbl.configure(text="%s: %d%%  (%s / %s)  - in coda: %d" % (
                        os.path.basename(self.conv_cur[0]), frac * 100,
                        fmt_hms(self.conv_time), fmt_hms(self.conv_duration),
                        len(self.conv_queue)))
                return
            if p.returncode == 0:
                self.log("Convertito: " + self.conv_cur[1])
            else:
                self.log("Conversione fallita (%s): %s" % (p.returncode,
                                                           " | ".join(self.conv_err)))
                if self.conv_queue is not None and p.returncode not in (-15, 255):
                    messagebox.showerror(APP_NAME, "Conversione fallita:\n" +
                                         "\n".join(self.conv_err))
            self.conv_proc = None
            if self.conv_queue is not None:
                self._conv_next()

        def conv_cancel(self):
            if self.conv_proc and self.conv_proc.poll() is None:
                self.conv_queue = None
                self.conv_proc.terminate()
                self.conv_lbl.configure(text="Conversione annullata.")
                dst = self.conv_cur[1]
                self.root.after(1500, lambda: os.path.exists(dst) and os.remove(dst))

        # ---------------------------------------------------------- varie
        def choose_dir(self):
            d = filedialog.askdirectory(parent=self.root,
                                        initialdir=os.path.expanduser(self.vars["output_dir"].get()))
            if d:
                self.vars["output_dir"].set(d)
                self.save_vars()

        def open_folder(self):
            d = os.path.expanduser(self.cfg["output_dir"])
            opener = shutil.which("xdg-open")
            if opener and os.path.isdir(d):
                subprocess.Popen([opener, d], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)

        def _dialog(self, title, text, width=60):
            top = tk.Toplevel(self.root)
            top.title(title)
            top.transient(self.root)
            top.resizable(False, False)
            ban = tk.Frame(top, bg=TITLE_A, height=6)
            ban.pack(fill="x")
            t = tk.Label(top, text=text, justify="left", anchor="w", padx=14, pady=12,
                         wraplength=width * 7)
            t.pack(fill="both")
            tk.Frame(top, height=2, bd=1, relief="sunken").pack(fill="x", padx=8)
            self._button(top, "OK", top.destroy, width=10).pack(pady=8)
            top.bind("<Return>", lambda e: top.destroy())
            top.bind("<Escape>", lambda e: top.destroy())
            top.grab_set()

        def show_about(self):
            self._dialog("Informazioni su " + APP_NAME,
                         "%s  versione %s\n\nProgramma per digitalizzare le videocassette "
                         "VHS, VHS-C, Video8 e Hi8 con le schede di acquisizione USB.\n\n"
                         "Motore: ffmpeg %s\nInterfaccia: Tk %s\n\n"
                         "Impostazioni salvate in:\n%s"
                         % (APP_NAME, VERSION, ff.version_str, tk.TkVersion, CONFIG_FILE))

        def show_tips(self):
            self._dialog("Consigli", TIPS, width=75)

        def on_quit(self):
            if self.mode in ("recording", "recovering"):
                if not messagebox.askyesno(APP_NAME, "Registrazione in corso.\n"
                                                     "Fermarla e uscire?"):
                    return
            self.save_vars()
            if self.conv_proc and self.conv_proc.poll() is None:
                self.conv_queue = None
                self.conv_proc.terminate()
            if self.mode == "recovering":
                self.on_stop()
            self.inhibitor.stop()
            if self.engine and self.engine.running():
                self.status("Chiusura in corso...")
                self.stop_engine(self.root.destroy)
                # Uscita forzata se ffmpeg non risponde
                self.root.after(25000, self.root.destroy)
            else:
                self.root.destroy()

    root = tk.Tk()
    root.title("%s %s" % (APP_NAME, VERSION))
    root.minsize(900, 600)
    try:
        icon = tk.PhotoImage(width=16, height=16)
        icon.put("#222222", to=(0, 3, 16, 13))
        icon.put("#eeeeee", to=(2, 5, 14, 9))
        icon.put("#444444", to=(3, 6, 6, 9))
        icon.put("#444444", to=(10, 6, 13, 9))
        root.iconphoto(True, icon)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


TIPS = """\
1. Collega l'uscita VIDEO (gialla) e AUDIO (bianca/rossa) del videoregistratore \
alla scheda USB. Se il videoregistratore ha l'uscita S-Video usala: immagine migliore.

2. Imposta lo standard PAL (Italia/Europa) a 720x576 e 25 fotogrammi/s. \
Le cassette americane/giapponesi sono NTSC 720x480 a 29,97.

3. Per l'archivio usa MJPEG o FFV1 senza deinterlacciare: conservi la qualità \
originale. Crea poi copie leggere con la scheda "Converti".

4. Se la barra di stato mostra molti "fotogrammi persi" il PC non ce la fa: \
scegli MJPEG, rimpicciolisci o disattiva l'anteprima e chiudi gli altri programmi.

5. Pulisci le testine del videoregistratore e riavvolgi completamente la cassetta \
prima di acquisirla. Usa la funzione "Tracking" se l'immagine ha righe di disturbo.

6. Tieni i VU-meter nella zona verde/gialla: se compare CLIP l'audio è distorto.

7. Premi "Capitolo" (F8) durante la registrazione per segnare l'inizio di una \
nuova scena: i tempi vengono salvati accanto al video.

8. Il formato MKV è il più sicuro: se il PC si blocca, il file resta leggibile \
fino all'ultimo secondo registrato."""


# --------------------------------------------------------------------------
# Riga di comando
# --------------------------------------------------------------------------

def cli_list(ff):
    print("ffmpeg: %s" % ff.version_str)
    videos = list_video_devices(ff)
    print("\nDispositivi video:")
    for d in videos:
        print("  %s  %s" % (d.path, d.name))
        for k, (desc, sizes) in d.formats.items():
            print("      formato %-10s %-22s %s" % (k, desc, " ".join(sizes[:8])))
        for i, n in d.inputs:
            print("      ingresso %d: %s" % (i, n))
    print("\nDispositivi audio:")
    audios = list_audio_devices(ff)
    for value, label, _, _ in audios:
        print("  %-45s %s" % (value, label))
    print("\nAudio della scheda di acquisizione:")
    for d in videos:
        if d.formats:
            a = find_card_audio(audios, d)
            print("  %s -> %s" % (d.path, a[1] if a else "non trovato"))


def main():
    ap = argparse.ArgumentParser(description="%s - digitalizza videocassette" % APP_NAME)
    ap.add_argument("--elenco", "--list", action="store_true",
                    help="mostra i dispositivi rilevati ed esci")
    ap.add_argument("--version", action="version", version="%s %s" % (APP_NAME, VERSION))
    args = ap.parse_args()

    ff = FFmpegInfo()
    if args.elenco:
        if not ff.path:
            print("ffmpeg non trovato. Installa con: sudo apt install ffmpeg")
            return 1
        cli_list(ff)
        return 0
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("Manca Tkinter. Installa con: sudo apt install python3-tk")
        return 1
    if not ff.path:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, "ffmpeg non è installato.\n\n"
                                       "Installalo con:\n  sudo apt install ffmpeg")
        return 1
    run_gui(ff, load_config())
    return 0


if __name__ == "__main__":
    sys.exit(main())
