"""
Read a PyTorch .pth state dict into numpy WITHOUT PyTorch and WITHOUT running
anything in it.

A .pth is a Python pickle, and unpickling a file from the internet can run
arbitrary code. This reader only lets the pickle build an OrderedDict of
tensors: find_class() allows exactly the handful of names a plain state dict
uses and refuses everything else.

Both formats are handled: the zip format (torch >= 1.6) and the legacy one
(magic number, protocol, sys info, the object, the storage keys, then raw
storage data).
"""
import collections
import io
import pickle
import struct
import sys
import zipfile

import numpy as np

DTYPES = {
    "FloatStorage": np.float32, "HalfStorage": np.float16, "DoubleStorage": np.float64,
    "BFloat16Storage": None, "LongStorage": np.int64, "IntStorage": np.int32,
    "ByteStorage": np.uint8, "BoolStorage": np.bool_,
}
LEGACY_MAGIC = 0x1950A86A20F9469CFC6C


class _StorageType:
    def __init__(self, name):
        self.name = name
        self.dtype = DTYPES[name]


class _Tensor:
    def __init__(self, storage, offset, size, stride):
        self.storage, self.offset, self.size, self.stride = storage, offset, tuple(size), tuple(stride)


def _rebuild_tensor_v2(storage, storage_offset, size, stride, requires_grad=False, backward_hooks=None, *a):
    return _Tensor(storage, storage_offset, size, stride)


def _rebuild_parameter(data, requires_grad=False, backward_hooks=None, *a):
    return data


class _Unpickler(pickle.Unpickler):
    def __init__(self, f, storages):
        super().__init__(f)
        self.storages = storages

    def find_class(self, module, name):
        if (module, name) == ("collections", "OrderedDict"):
            return collections.OrderedDict
        if (module, name) == ("torch._utils", "_rebuild_tensor_v2"):
            return _rebuild_tensor_v2
        if (module, name) == ("torch._utils", "_rebuild_parameter"):
            return _rebuild_parameter
        if module == "torch" and name in DTYPES:
            return _StorageType(name)
        raise pickle.UnpicklingError("refusing %s.%s (a plain state dict never needs it)" % (module, name))

    def persistent_load(self, pid):
        # legacy: ('storage', type, key, location, numel, view_metadata)
        # zip:    ('storage', type, key, location, numel)
        if not isinstance(pid, tuple) or pid[0] != "storage":
            raise pickle.UnpicklingError("unexpected persistent id")
        st, key = pid[1], str(pid[2])
        if not isinstance(st, _StorageType):
            raise pickle.UnpicklingError("unexpected storage type")
        self.storages.setdefault(key, {"dtype": st.dtype, "numel": pid[4]})
        return key


def _materialise(obj, data):
    if isinstance(obj, dict):
        return collections.OrderedDict((k, _materialise(v, data)) for k, v in obj.items())
    if isinstance(obj, _Tensor):
        buf = data[obj.storage]
        item = buf.itemsize
        return np.lib.stride_tricks.as_strided(
            buf[obj.offset:], shape=obj.size, strides=[s * item for s in obj.stride]).copy()
    return obj


def load(path):
    """{name: numpy array}. Unwraps a top-level 'params'/'params_ema'/'state_dict'."""
    storages = {}
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        root = z.namelist()[0].split("/")[0]
        obj = _Unpickler(io.BytesIO(z.read(root + "/data.pkl")), storages).load()
        data = {}
        for key, s in storages.items():
            raw = z.read("%s/data/%s" % (root, key))
            data[key] = np.frombuffer(raw, dtype=s["dtype"])
    else:
        with open(path, "rb") as f:
            safe = lambda: _Unpickler(f, storages).load()
            if safe() != LEGACY_MAGIC:
                raise ValueError("not a PyTorch file")
            safe()                     # protocol version
            safe()                     # sys info (a dict of ints and bools)
            obj = safe()
            keys = safe()
            data = {}
            for key in keys:
                s = storages[str(key)]
                n, = struct.unpack("<q", f.read(8))
                item = np.dtype(s["dtype"]).itemsize
                data[str(key)] = np.frombuffer(f.read(n * item), dtype=s["dtype"])
    for k in ("params_ema", "params", "state_dict", "model"):
        if isinstance(obj, dict) and k in obj and isinstance(obj[k], dict):
            obj = obj[k]
            break
    return _materialise(obj, data)


if __name__ == "__main__":
    sd = load(sys.argv[1])
    total = 0
    for k, v in sd.items():
        total += v.size
    print(len(sd), "tensors,", total, "parameters")
    for k in list(sd)[:6] + list(sd)[-6:]:
        print(" ", k, sd[k].shape, sd[k].dtype)
