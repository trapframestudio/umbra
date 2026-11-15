"""
ESRGAN (RRDBNet) models on the GPU through ncnn's Vulkan backend.

    python esrgan.py convert model.pth out_dir     write out_dir/model.param + model.bin

The converter writes ncnn's text graph and weight file straight from the
state dict (read safely by pth.py), so no PyTorch or converter tool is needed.
It understands the classic ESRGAN key layout (model.0, model.1.sub.N.RDBk...,
model.3/6/8/10) used by most community models, 4x with 23 blocks.

Upscaler runs a converted model over an image in overlapping tiles.
"""
import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pth  # noqa: E402


# ---------------------------------------------------------------- graph building
class Graph:
    def __init__(self):
        self.layers = []           # [type, name, inputs, outputs, params]
        self.weights = []          # bytes, in layer order
        self.n = 0

    def blob(self, base="b"):
        self.n += 1
        return "%s%d" % (base, self.n)

    def add(self, typ, inputs, params="", weights=None):
        out = self.blob()
        self.layers.append([typ, "%s_%d" % (typ.lower(), len(self.layers)), list(inputs), [out], params])
        if weights is not None:
            self.weights.append(weights)
        return out

    def conv(self, x, w, b, lrelu):
        o, i, kh, kw = w.shape
        p = "0=%d 1=%d 11=%d 2=1 12=1 3=1 13=1 4=%d 14=%d 5=1 6=%d" % (o, kw, kh, kw // 2, kh // 2, w.size)
        if lrelu:
            p += " 9=2 -23310=1,2.000000e-01"
        blob = struct.pack("<I", 0) + w.astype("<f4").tobytes() + b.astype("<f4").tobytes()
        return self.add("Convolution", [x], p, blob)

    def write(self, param_path, bin_path):
        # a blob used by several layers must go through a Split
        uses = {}
        for L in self.layers:
            for i in L[2]:
                uses[i] = uses.get(i, 0) + 1
        layers = []
        seen = {}
        for L in self.layers:
            ins = []
            for i in L[2]:
                if uses.get(i, 0) > 1:
                    k = seen.get(i, 0)
                    seen[i] = k + 1
                    ins.append("%s_s%d" % (i, k))
                else:
                    ins.append(i)
            layers.append([L[0], L[1], ins, L[3], L[4]])
            for o in L[3]:
                if uses.get(o, 0) > 1:
                    layers.append(["Split", "split_" + o, [o], ["%s_s%d" % (o, k) for k in range(uses[o])], ""])
        blobs = set()
        for L in layers:
            blobs.update(L[2])
            blobs.update(L[3])
        with open(param_path, "w") as f:
            f.write("7767517\n%d %d\n" % (len(layers), len(blobs)))
            for typ, name, ins, outs, params in layers:
                f.write("%-16s %-24s %d %d %s %s %s\n" % (typ, name, len(ins), len(outs), " ".join(ins), " ".join(outs), params))
        with open(bin_path, "wb") as f:
            for w in self.weights:
                f.write(w)


def build_rrdb(sd, nb=None):
    W = lambda k: (sd[k + ".weight"], sd[k + ".bias"])
    if nb is None:
        nb = len({k.split(".")[3] for k in sd if k.startswith("model.1.sub.") and ".RDB" in k})
    g = Graph()
    x = "in0"
    g.layers.append(["Input", "input", [], [x], ""])
    fea = g.conv(x, *W("model.0"), lrelu=False)
    h = fea
    for n in range(nb):
        rrdb_in = h
        for r in (1, 2, 3):
            pre = "model.1.sub.%d.RDB%d" % (n, r)
            xs = [h]
            for c in range(1, 6):
                inp = xs[0] if len(xs) == 1 else g.add("Concat", xs, "0=0")
                y = g.conv(inp, *W("%s.conv%d.0" % (pre, c)), lrelu=c < 5)
                xs.append(y)
            scaled = g.add("BinaryOp", [xs[-1]], "0=2 1=1 2=2.000000e-01")
            h = g.add("BinaryOp", [scaled, h], "0=0")
        scaled = g.add("BinaryOp", [h], "0=2 1=1 2=2.000000e-01")
        h = g.add("BinaryOp", [scaled, rrdb_in], "0=0")
    trunk = g.conv(h, *W("model.1.sub.%d" % nb), lrelu=False)
    h = g.add("BinaryOp", [fea, trunk], "0=0")
    for k in ("model.3", "model.6"):
        h = g.add("Interp", [h], "0=1 1=2.000000e+00 2=2.000000e+00")
        h = g.conv(h, *W(k), lrelu=True)
    h = g.conv(h, *W("model.8"), lrelu=True)
    out = g.conv(h, *W("model.10"), lrelu=False)
    g.layers[-1][3] = ["out0"]
    return g


def classic_keys(sd):
    """BasicSR's newer RRDBNet names -> the classic ESRGAN ones."""
    import re
    if "model.0.weight" in sd or "conv_first.weight" not in sd:
        return sd
    nb = len({k.split(".")[1] for k in sd if k.startswith("body.")})
    out = {}
    for k, v in sd.items():
        m = re.match(r"body\.(\d+)\.rdb(\d)\.conv(\d)\.(weight|bias)$", k)
        if m:
            out["model.1.sub.%s.RDB%s.conv%s.0.%s" % m.groups()] = v
            continue
        base, kind = k.rsplit(".", 1)
        name = {"conv_first": "model.0", "conv_body": "model.1.sub.%d" % nb, "conv_up1": "model.3",
                "conv_up2": "model.6", "conv_hr": "model.8", "conv_last": "model.10"}.get(base)
        if name is None:
            raise SystemExit("unknown key " + k)
        out[name + "." + kind] = v
    return out


def convert(pth_path, out_dir):
    sd = classic_keys(pth.load(pth_path))
    if "model.0.weight" not in sd:
        raise SystemExit("not a classic ESRGAN state dict (no model.0.weight)")
    g = build_rrdb(sd)
    os.makedirs(out_dir, exist_ok=True)
    g.write(os.path.join(out_dir, "model.param"), os.path.join(out_dir, "model.bin"))


# ---------------------------------------------------------------- running
class Upscaler:
    """A converted 4x model. run(rgb) takes float32 HxWx3 in 0..1."""

    scale = 4

    def __init__(self, model_dir, gpu=0, tile=256, pad=16, fp16=True):
        import ncnn
        self.ncnn = ncnn
        self.net = ncnn.Net()
        self.net.opt.use_vulkan_compute = gpu is not None
        if gpu is not None:
            self.net.set_vulkan_device(gpu)
        # ncnn's defaults: fp16 storage and arithmetic. fp16=False turns all
        # three off (turning only arithmetic off is not a supported mix).
        if not fp16:
            self.net.opt.use_fp16_packed = False
            self.net.opt.use_fp16_storage = False
            self.net.opt.use_fp16_arithmetic = False
        self.net.load_param(os.path.join(model_dir, "model.param"))
        self.net.load_model(os.path.join(model_dir, "model.bin"))
        self.tile, self.pad = tile, pad

    def _run_tile(self, chw):
        # ncnn.Mat borrows the numpy buffer without copying: keep `arr`
        # alive until the extraction is done, or the net reads freed memory.
        arr = np.ascontiguousarray(chw, dtype=np.float32)
        mat = self.ncnn.Mat(arr)
        ex = self.net.create_extractor()
        ex.input("in0", mat)
        _, out = ex.extract("out0")
        res = np.array(out)
        del mat, arr
        return res

    def run(self, rgb, progress=None):
        H, W = rgb.shape[:2]
        s, t, p = self.scale, self.tile, self.pad
        src = np.pad(rgb.astype(np.float32), ((p, p), (p, p), (0, 0)), mode="reflect")
        out = np.zeros((H * s, W * s, 3), np.float32)
        tiles = [(y, x) for y in range(0, H, t) for x in range(0, W, t)]
        for n, (y, x) in enumerate(tiles):
            th, tw = min(t, H - y), min(t, W - x)
            patch = src[y:y + th + 2 * p, x:x + tw + 2 * p]
            o = self._run_tile(patch.transpose(2, 0, 1))
            o = o.transpose(1, 2, 0)[p * s:(p + th) * s, p * s:(p + tw) * s]
            out[y * s:(y + th) * s, x * s:(x + tw) * s] = o
            if progress:
                progress(n + 1, len(tiles))
        return np.clip(out, 0, 1)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "convert":
        convert(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
