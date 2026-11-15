"""
The HD characters batch: every character colour texture the game loads from
textures/act/, upscaled with hd.guarded() and block-compressed with mips.

    <venv>/bin/python -I batch.py              run (or resume) the whole batch
    <venv>/bin/python -I batch.py --plan       list what would be done, do nothing
    <venv>/bin/python -I batch.py --only textures/act/x.dds ... [--out DIR] [--keep-png DIR]

What gets done (see README.md):
  * only the winning file for each path (vfs.py), only colour textures: no
    _bump/_bump# maps, no normal maps (_n, _nm, _nmap, _normal), nothing a .thm
    calls a bump/normal map or names as a bump, nothing that isn't a DDS;
  * 1K sources -> 4K (the model's 4x), 2K sources -> 4K (the model's 4x,
    averaged down); smaller ones are left alone, 4K+ ones too;
  * BC1 (DXT1) when the source has no alpha worth keeping, BC7 when it does;
    full mip chain averaged in linear light.

Resumable: a texture whose output exists and is newer than its source is
skipped. Each texture is written to a .part file and renamed when complete,
so stopping the batch at any moment (even kill -9) loses at most the texture
in progress. A texture that fails is logged and the batch moves on.
"""
import argparse
import os
import re
import shutil
import signal
import struct
import sys
import time
import traceback

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
LOOK = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, LOOK)

import bcn      # noqa: E402
import dds      # noqa: E402
import hd       # noqa: E402
import vfs      # noqa: E402

PREFIX = "textures/act/"
MODEL = os.path.expanduser("~/.cache/umbra-look/models/realistic-rescaler")
OUT = os.path.join(LOOK, "out", "Umbra HD Characters")
LOG = os.path.join(LOOK, "out", "upscale-batch.log")
MANIFEST = os.path.join(LOOK, "out", "upscale-batch-manifest.tsv")
TARGET = 4096                 # longest side of the output
MIN_START_GB = 40             # refuse to start with less free space than this
MIN_RUN_GB = 15               # stop when free space falls below this

NOT_COLOUR = re.compile(r"(bump|_nmap$|_normal$|_nm$|_n$)")


# ---------------------------------------------------------------- .thm
THM_TEXTUREPARAM, THM_TEXTURE_TYPE, THM_BUMP = 0x0812, 0x0814, 0x0817
THM_TYPES = ["image", "cube", "bump", "normal", "terrain"]


def read_thm(data):
    """The bits of an X-Ray .thm we need: texture type and bump name."""
    out, o = {}, 0
    while o + 8 <= len(data):
        cid, size = struct.unpack_from("<II", data, o)
        body = data[o + 8:o + 8 + size]
        cid &= 0x7FFFFFFF
        if cid == THM_TEXTURE_TYPE and len(body) >= 4:
            t = struct.unpack_from("<I", body)[0]
            out["type"] = THM_TYPES[t] if t < len(THM_TYPES) else str(t)
        elif cid == THM_BUMP and len(body) > 8:
            out["bump"] = body[8:].split(b"\0")[0].decode("latin1")
        o += 8 + size
    return out


# ---------------------------------------------------------------- the job list
def target_size(w, h):
    """4K on the longest side, each side a power of two (a 2048x2049 source
    becomes 4096x4096: the UVs span the whole texture either way)."""
    k = TARGET / max(w, h)
    p2 = lambda n: 1 << max(2, int(round(np.log2(max(1, n * k)))))
    return p2(w), p2(h)


def source_mtime(src):
    return os.path.getmtime(src.path if src.path else src.archive.path)


def plan(v, min_side=1024):
    """[(rel, src, w, h, fmt)] to do, and {reason: [rel]} skipped."""
    win = v.winners(PREFIX)
    thms = {}
    for rel, s in win.items():
        if rel.endswith(".thm"):
            try:
                thms[rel[:-4]] = read_thm(s.read())
            except Exception:                       # a broken .thm is just no .thm
                pass
    bumps = {("textures/" + t["bump"]).lower().replace("\\", "/") for t in thms.values() if t.get("bump")}
    jobs, skipped = [], {}
    skip = lambda why, rel: skipped.setdefault(why, []).append(rel)
    for rel, s in sorted(win.items()):
        if not rel.endswith(".dds"):
            continue
        base = rel[:-4]
        if NOT_COLOUR.search(os.path.basename(base)):
            skip("bump or normal map (name)", rel)
            continue
        if base in bumps:
            skip("bump map (named by a .thm)", rel)
            continue
        t = thms.get(base, {}).get("type")
        if t and t != "image":
            skip("%s map (.thm)" % t, rel)
            continue
        try:
            head = s.read()[:148] if s.path is None else open(s.path, "rb").read(148)
            w, h, _mips, fmt = dds.info(head)
        except Exception:
            skip("not a DDS", rel)
            continue
        side = max(w, h)
        if side < min_side:
            skip("smaller than %d" % min_side, rel)
        elif side > TARGET // 2 + 1:
            skip("already 4K or more", rel)
        else:
            jobs.append((rel, s, w, h, fmt))
    return jobs, skipped


# ---------------------------------------------------------------- one texture
def keeps_alpha(a):
    """Is there alpha worth keeping (anything visibly below opaque)?"""
    return bool((a < 250).mean() > 1e-4)


def infer(src, up):
    """The GPU half: the source decoded, and the model's output at the target size."""
    rgba = dds.read(src.read())
    H, W = rgba.shape[:2]
    tw, th = target_size(W, H)
    model = up.run(rgba[..., :3].astype(np.float32) / 255.0)   # the model's 4x
    if model.shape[:2] != (th, tw):
        model = hd.down(model, (tw, th))                       # 2K: averaged down to 4K
    return rgba, model


def finish(rel, rgba, model, out_root, keep_png=None):
    """The CPU half: guarded() blend, alpha, mips, compression, the file.
    Returns a dict for the manifest."""
    th, tw = model.shape[:2]
    rgb = rgba[..., :3].astype(np.float32) / 255.0
    out, trust = hd.guarded(rgb, model)
    del model
    res = np.empty((th, tw, 4), np.uint8)
    res[..., :3] = np.clip(np.rint(out * 255.0), 0, 255)
    del out
    alpha = keeps_alpha(rgba[..., 3])
    res[..., 3] = hd.alpha_up(rgba[..., 3], (tw, th)) if alpha else 255
    fmt = bcn.BC7 if alpha else bcn.BC1
    path = os.path.join(out_root, "gamedata", rel)
    size = bcn.write(path, res, fmt)
    if keep_png:
        p = os.path.join(keep_png, rel[len(PREFIX):].replace("/", "__")[:-4] + ".png")
        os.makedirs(keep_png, exist_ok=True)
        Image.fromarray(res).save(p, compress_level=1)
    return {"out_w": tw, "out_h": th, "out_fmt": bcn.NAMES[fmt], "bytes": size,
            "trust": float(trust.mean())}


def produce(todo, up, q, stop):
    """Thread: run the model on each texture in turn and queue the result, so
    the GPU works on the next texture while the CPU finishes this one."""
    for job in todo:
        if stop:
            break
        t0 = time.time()
        try:
            rgba, model = infer(job[1], up)
            q.put((job, rgba, model, time.time() - t0, None))
        except Exception as e:
            q.put((job, None, None, time.time() - t0, (e, traceback.format_exc())))
    q.put(None)


# ---------------------------------------------------------------- logging
class Log:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.f = open(path, "a", buffering=1, encoding="utf-8")

    def __call__(self, msg):
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        self.f.write(line + "\n")
        print(line, flush=True)


MANIFEST_COLS = ["rel", "source", "src_w", "src_h", "src_fmt", "out_w", "out_h", "out_fmt",
                 "bytes", "seconds", "trust", "status"]


def manifest_row(path, row):
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("\t".join(MANIFEST_COLS) + "\n")
        f.write("\t".join(str(row.get(c, "")) for c in MANIFEST_COLS) + "\n")


def free_gb(path):
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return shutil.disk_usage(path).free / 1e9


def fmt_time(s):
    s = int(s)
    return "%dh%02dm" % (s // 3600, s % 3600 // 60) if s >= 3600 else "%dm%02ds" % (s // 60, s % 60)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true", help="list the work and the skips, do nothing")
    ap.add_argument("--only", nargs="+", help="just these paths (under gamedata/)")
    ap.add_argument("--out", default=OUT, help="output layer folder (default: %(default)s)")
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--keep-png", help="also save each uncompressed result as PNG here (checks)")
    ap.add_argument("--min-side", type=int, default=1024, help="skip sources smaller than this (default 1024)")
    ap.add_argument("--force", action="store_true", help="redo textures that are already done")
    args = ap.parse_args()

    v = vfs.VFS(prefix=PREFIX)
    jobs, skipped = plan(v, args.min_side)
    if args.only:
        want = {r.lower().replace("\\", "/") for r in args.only}
        jobs = [j for j in jobs if j[0] in want]
        missing = want - {j[0] for j in jobs}
        if missing:
            print("not in the job list (skipped or not found): %s" % ", ".join(sorted(missing)))

    if args.plan:
        for rel, s, w, h, fmt in jobs:
            tw, th = target_size(w, h)
            print("%s\t%dx%d %s -> %dx%d\t%s" % (rel, w, h, fmt, tw, th, s.label))
        n1 = sum(1 for j in jobs if max(j[2], j[3]) <= 1024)
        print("\n%d to do (%d at 1K, %d at 2K)" % (len(jobs), n1, len(jobs) - n1))
        for why, rels in sorted(skipped.items()):
            print("skipped, %s: %d" % (why, len(rels)))
        return 0

    log = Log(args.log)
    gb = free_gb(args.out)
    if gb < MIN_START_GB:
        log("only %.0f GB free on the output disk (want %d): not starting" % (gb, MIN_START_GB))
        return 1

    todo = []
    for rel, s, w, h, fmt in jobs:
        path = os.path.join(args.out, "gamedata", rel)
        if os.path.exists(path + ".part"):
            os.remove(path + ".part")                  # left by a stopped run
        if not args.force and os.path.exists(path) and os.path.getmtime(path) > source_mtime(s):
            continue
        todo.append((rel, s, w, h, fmt))
    log("start: %d textures, %d already done, %d to do; %.0f GB free; pid %d"
        % (len(jobs), len(jobs) - len(todo), len(todo), gb, os.getpid()))
    if not todo:
        log("nothing to do")
        return 0

    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append("SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: stop.append("SIGINT"))

    import queue
    import threading
    import esrgan
    up = esrgan.Upscaler(MODEL)
    q = queue.Queue(maxsize=1)                   # one texture waits while the GPU does the next
    gpu = threading.Thread(target=produce, args=(todo, up, q, stop), daemon=True)
    gpu.start()
    # pipeline cost per texture (the slower of the two halves), by source size class
    spent = {1: [], 2: []}
    guess = {1: 6.0, 2: 12.0}
    t_start, done, failed, i = time.time(), 0, 0, 0
    while True:
        item = q.get()
        if item is None:
            break
        (rel, s, w, h, fmt), rgba, model, t_model, err = item
        i += 1
        row = {"rel": rel, "source": s.label, "src_w": w, "src_h": h, "src_fmt": fmt}
        try:
            if err:
                raise RuntimeError(err[0]) from None
            if i % 20 == 1 and free_gb(args.out) < MIN_RUN_GB:
                log("less than %d GB free: stopping" % MIN_RUN_GB)
                stop.append("disk")
                return 1
            t0 = time.time()
            r = finish(rel, rgba, model, args.out, args.keep_png)
            del rgba, model
            t_finish = time.time() - t0
            row.update(r)
            row["seconds"] = "%.1f" % (t_model + t_finish)
            row["trust"] = "%.2f" % r["trust"]
            row["status"] = "ok"
            done += 1
            cls = 1 if max(w, h) <= 1024 else 2
            spent[cls].append(max(t_model, t_finish))
            left = todo[i:]
            avg = lambda c: sum(spent[c][-50:]) / len(spent[c][-50:]) if spent[c] else guess[c]
            eta = sum(avg(1 if max(j[2], j[3]) <= 1024 else 2) for j in left)
            log("[%d/%d] %s %dx%d %s -> %dx%d %s %.1f MB (model %.1fs, rest %.1fs, trust %.2f), ETA %s"
                % (i, len(todo), rel, w, h, fmt, r["out_w"], r["out_h"], r["out_fmt"], r["bytes"] / 1e6,
                   t_model, t_finish, r["trust"], fmt_time(eta)))
        except Exception as e:
            failed += 1
            row["status"] = "error: %s" % str(e).replace("\t", " ").replace("\n", " ")[:200]
            tb = err[1] if err else traceback.format_exc()
            log("[%d/%d] %s FAILED: %s\n%s" % (i, len(todo), rel, e, tb))
        manifest_row(args.manifest, row)
        if stop:
            log("stopped (%s) after %s; run again to resume" % (stop[0], rel))
            return 0
    log("finished: %d written, %d failed, in %s" % (done, failed, fmt_time(time.time() - t_start)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
