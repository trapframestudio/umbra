"""
dev/b2_upload.py against a fake Backblaze B2 (native API v3) served from this
process on 127.0.0.1. No real network is used. The pack is a tiny one built
with the real pack builder (tests/test_umbra.py's helpers).

    python3 -m unittest -v tests.test_b2_upload        (from the umbra folder)
"""
import base64
import collections
import contextlib
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dev"))
sys.path.insert(0, os.path.join(ROOT, "installer"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import b2_upload  # noqa: E402
from test_umbra import build, make_source  # noqa: E402


def sha1(b):
    return hashlib.sha1(b).hexdigest()


def sha256(b):
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- the fake B2

class FakeB2:
    """Enough of B2's native API v3 for the uploader, with failures to inject.

    inject[api]        [(status, code), ...] returned by the next calls of that api, in order
    lose_reply[api]    the next N calls of that api are carried out, but the connection drops before the reply
    drop_parts         1-based numbers of b2_upload_part requests that lose the connection half-way
    expire_after       after this many good API calls, every session token expires (401 expired_auth_token)
    parts_outage_after once this many parts are stored, b2_upload_part answers 503 (until set to None)
    """

    KEY_ID = "0047fakeKeyId0000000000a1"
    APP_KEY = "K004fakeAppKeyThatMustNeverBePrinted"

    def __init__(self, bucket="umbra-test", page=2, min_part=500, rec_part=1000):
        self.bucket_name, self.bucket_id = bucket, "fakebucket0123456789ab"
        self.page, self.min_part, self.rec_part = page, min_part, rec_part
        self.restrict = True                 # the key is limited to the bucket (else: b2_list_buckets)
        self.nested = False                  # True: "allowed" as the docs show it; False: flat, as B2 really
                                             # answered on 2026-11-14 (bucketId/bucketName/capabilities in storageApi)
        self.lock = threading.RLock()
        self.files = {}                      # name -> {"meta", "data", "parts": [(length, sha1)] | None}
        self.large = {}                      # fileId -> {"name", "contentType", "fileInfo", "parts": {n: {...}}}
        self.account_tokens = set()
        self.upload_tokens = {}              # token -> upload URL path
        self.issued = []                     # every token handed out
        self.calls = collections.Counter()
        self.completed = []                  # file names, in the order uploads finished
        self.part_log = []                   # (fileId, part number) of every stored part
        self.stored_parts = 0
        self.inject = collections.defaultdict(list)
        self.lose_reply = collections.Counter()  # api -> how many replies to lose after doing the call
        self.drop_parts = set()
        self.part_requests = 0
        self.dropped = 0
        self.expire_after = None
        self.good_calls = 0
        self.parts_outage_after = None
        self.requests = 0
        self.basic_paths = []                # where the Basic (key) header was sent
        self.busy = set()
        self.concurrent_use = 0              # an upload URL used by two requests at once
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda *a: None
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- helpers
    def _token(self, kind):
        t = "4_fake%s_%s" % (kind, uuid.uuid4().hex)
        self.issued.append(t)
        return t

    def _meta(self, name, data, ctype, info, content_sha1, file_id=None):
        return {"accountId": "fakeaccount", "action": "upload", "bucketId": self.bucket_id,
                "contentLength": len(data), "contentSha1": content_sha1, "contentMd5": None,
                "contentType": ctype, "fileId": file_id or "4_zfile_" + uuid.uuid4().hex,
                "fileInfo": dict(info), "fileName": name, "uploadTimestamp": int(time.time() * 1000)}

    @staticmethod
    def _err(status, code, message=""):
        return status, {"status": status, "code": code, "message": message or code}

    def seed(self, name, data, info, ctype="application/octet-stream"):
        with self.lock:
            self.files[name] = {"meta": self._meta(name, data, ctype, info, sha1(data)), "data": data, "parts": None}

    def seed_unfinished(self, name, info, parts):
        fid = "4_zlarge_" + uuid.uuid4().hex
        with self.lock:
            self.large[fid] = {"name": name, "contentType": "application/zip", "fileInfo": dict(info),
                               "parts": {n: {"data": d, "sha1": sha1(d)} for n, d in parts.items()}}
        return fid

    def uploaded_since(self, mark):
        return self.completed[mark:]

    # -- routing
    def handle(self, h, method):
        with self.lock:
            self.requests += 1
        path = urllib.parse.urlsplit(h.path).path
        auth = h.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            with self.lock:
                self.basic_paths.append(path)
        if method == "GET" and path == "/b2api/v3/b2_authorize_account":
            return h.send_json(*self.authorize(auth))
        if method == "GET" and path.startswith("/file/"):
            return self.download(h, path)
        if method == "POST" and path.startswith("/b2api/v3/"):
            return self.api(h, path.rsplit("/", 1)[1], auth)
        if method == "POST" and path.startswith("/upload/"):
            return self.upload(h, path, auth, part=False)
        if method == "POST" and path.startswith("/upload_part/"):
            return self.upload(h, path, auth, part=True)
        h.send_json(*self._err(404, "not_found"))

    def authorize(self, auth):
        with self.lock:
            self.calls["b2_authorize_account"] += 1
            good = "Basic " + base64.b64encode(("%s:%s" % (self.KEY_ID, self.APP_KEY)).encode()).decode()
            if auth != good:
                return self._err(401, "unauthorized", "bad key")
            tok = self._token("acct")
            self.account_tokens.add(tok)
            caps = ["listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"]
            api = {"apiUrl": self.url, "downloadUrl": self.url, "s3ApiUrl": "https://s3.invalid",
                   "infoType": "storageApi", "recommendedPartSize": self.rec_part,
                   "absoluteMinimumPartSize": self.min_part}
            if self.nested:
                api["allowed"] = {"buckets": [{"id": self.bucket_id, "name": self.bucket_name}] if self.restrict
                                  else None, "capabilities": caps, "namePrefix": None}
            else:
                api.update({"bucketId": self.bucket_id if self.restrict else None,
                            "bucketName": self.bucket_name if self.restrict else None,
                            "capabilities": caps, "namePrefix": None})
            return 200, {"accountId": "fakeaccount", "authorizationToken": tok,
                         "applicationKeyExpirationTimestamp": None,
                         "apiInfo": {"storageApi": api}}

    def api(self, h, api, auth):
        raw = h.body()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return h.send_json(*self._err(400, "bad_request", "not JSON"))
        lose = False
        with self.lock:
            self.calls[api] += 1
            if self.expire_after is not None and self.good_calls >= self.expire_after:
                self.account_tokens.clear()
                self.expire_after = None
            if auth not in self.account_tokens:
                res = self._err(401, "expired_auth_token" if auth in self.issued else "bad_auth_token")
            elif self.inject.get(api):
                res = self._err(*self.inject[api].pop(0))
            else:
                self.good_calls += 1
                fn = getattr(self, "api_" + api, None)
                res = fn(body) if fn else self._err(400, "bad_request", "unknown api " + api)
                if self.lose_reply[api] > 0:
                    self.lose_reply[api] -= 1
                    lose = True
        if lose:
            h.close_connection = True
            h.connection.shutdown(socket.SHUT_RDWR)
            return
        h.send_json(*res)

    # -- the JSON API
    def _bucket_ok(self, body):
        return body.get("bucketId") == self.bucket_id

    def api_b2_list_buckets(self, body):
        if body.get("accountId") != "fakeaccount":
            return self._err(400, "bad_request", "accountId")
        bs = [{"accountId": "fakeaccount", "bucketId": self.bucket_id, "bucketName": self.bucket_name,
               "bucketType": "allPublic", "bucketInfo": {}}]
        if body.get("bucketName"):
            bs = [b for b in bs if b["bucketName"] == body["bucketName"]]
        return 200, {"buckets": bs}

    def api_b2_list_file_names(self, body):
        if not self._bucket_ok(body):
            return self._err(400, "bad_request", "bucketId")
        prefix, start = body.get("prefix") or "", body.get("startFileName") or ""
        n = min(int(body.get("maxFileCount", 100)), self.page)
        names = sorted(k for k in self.files if k.startswith(prefix) and k >= start)
        return 200, {"files": [self.files[k]["meta"] for k in names[:n]],
                     "nextFileName": names[n] if len(names) > n else None}

    def api_b2_get_upload_url(self, body):
        if not self._bucket_ok(body):
            return self._err(400, "bad_request", "bucketId")
        path = "/upload/%s/%s" % (self.bucket_id, uuid.uuid4().hex)
        tok = self._token("upl")
        self.upload_tokens[tok] = path
        return 200, {"bucketId": self.bucket_id, "uploadUrl": self.url + path, "authorizationToken": tok}

    def api_b2_start_large_file(self, body):
        if not self._bucket_ok(body) or not body.get("fileName") or not body.get("contentType"):
            return self._err(400, "bad_request")
        fid = "4_zlarge_" + uuid.uuid4().hex
        self.large[fid] = {"name": body["fileName"], "contentType": body["contentType"],
                           "fileInfo": dict(body.get("fileInfo") or {}), "parts": {}}
        return 200, {"accountId": "fakeaccount", "action": "start", "bucketId": self.bucket_id, "fileId": fid,
                     "fileName": body["fileName"], "contentType": body["contentType"],
                     "fileInfo": dict(body.get("fileInfo") or {}), "uploadTimestamp": int(time.time() * 1000)}

    def api_b2_get_upload_part_url(self, body):
        fid = body.get("fileId")
        if fid not in self.large:
            return self._err(400, "bad_request", "no such unfinished file")
        path = "/upload_part/%s/%s" % (fid, uuid.uuid4().hex)
        tok = self._token("part")
        self.upload_tokens[tok] = path
        return 200, {"fileId": fid, "uploadUrl": self.url + path, "authorizationToken": tok}

    def api_b2_finish_large_file(self, body):
        fid = body.get("fileId")
        u = self.large.get(fid)
        if u is None:
            return self._err(400, "bad_request", "No active upload for: %s" % fid)
        nums = sorted(u["parts"])
        if len(nums) < 2 or nums != list(range(1, len(nums) + 1)):
            return self._err(400, "bad_request", "parts missing or fewer than two")
        if [u["parts"][n]["sha1"] for n in nums] != body.get("partSha1Array"):
            return self._err(400, "bad_request", "partSha1Array does not match the parts")
        if any(len(u["parts"][n]["data"]) < self.min_part for n in nums[:-1]):
            return self._err(400, "bad_request", "part too small")
        data = b"".join(u["parts"][n]["data"] for n in nums)
        meta = self._meta(u["name"], data, u["contentType"], u["fileInfo"], "none", fid)
        self.files[u["name"]] = {"meta": meta, "data": data,
                                 "parts": [(len(u["parts"][n]["data"]), u["parts"][n]["sha1"]) for n in nums]}
        del self.large[fid]
        self.completed.append(u["name"])
        return 200, meta

    def api_b2_list_unfinished_large_files(self, body):
        if not self._bucket_ok(body):
            return self._err(400, "bad_request", "bucketId")
        prefix, start = body.get("namePrefix") or "", body.get("startFileId") or ""
        n = min(int(body.get("maxFileCount", 100)), self.page)
        fids = sorted(f for f, u in self.large.items() if u["name"].startswith(prefix) and f >= start)
        files = [{"accountId": "fakeaccount", "action": "start", "bucketId": self.bucket_id, "fileId": f,
                  "fileName": self.large[f]["name"], "contentType": self.large[f]["contentType"],
                  "fileInfo": dict(self.large[f]["fileInfo"]), "uploadTimestamp": 0} for f in fids[:n]]
        return 200, {"files": files, "nextFileId": fids[n] if len(fids) > n else None}

    def api_b2_list_parts(self, body):
        u = self.large.get(body.get("fileId"))
        if u is None:
            return self._err(400, "bad_request", "no such unfinished file")
        start = int(body.get("startPartNumber") or 1)
        n = min(int(body.get("maxPartCount", 100)), self.page)
        nums = sorted(k for k in u["parts"] if k >= start)
        parts = [{"fileId": body["fileId"], "partNumber": k, "contentLength": len(u["parts"][k]["data"]),
                  "contentSha1": u["parts"][k]["sha1"], "uploadTimestamp": 0} for k in nums[:n]]
        return 200, {"parts": parts, "nextPartNumber": nums[n] if len(nums) > n else None}

    def api_b2_cancel_large_file(self, body):
        u = self.large.pop(body.get("fileId"), None)
        if u is None:
            return self._err(400, "bad_request", "no such unfinished file")
        return 200, {"fileId": body["fileId"], "accountId": "fakeaccount", "bucketId": self.bucket_id,
                     "fileName": u["name"]}

    # -- uploads
    def upload(self, h, path, auth, part):
        api = "b2_upload_part" if part else "b2_upload_file"
        n = int(h.headers.get("Content-Length") or 0)
        with self.lock:
            if path in self.busy:
                self.concurrent_use += 1
            self.busy.add(path)
            drop = False
            if part:
                self.part_requests += 1
                drop = self.part_requests in self.drop_parts
        try:
            if drop:                                      # the connection dies half-way through the body
                h.rfile.read(n // 2)
                with self.lock:
                    self.dropped += 1
                h.close_connection = True
                try:
                    h.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            data = h.rfile.read(n) if n else b""
            with self.lock:
                self.calls[api] += 1
                hdr_sha1 = h.headers.get("X-Bz-Content-Sha1")
                if self.upload_tokens.get(auth) != path:
                    res = self._err(401, "expired_auth_token" if auth in self.issued else "bad_auth_token")
                elif self.inject.get(api):
                    res = self._err(*self.inject[api].pop(0))
                elif len(data) != n:
                    res = self._err(400, "bad_request", "short body")
                elif sha1(data) != hdr_sha1:
                    res = self._err(400, "bad_request", "Sha1 did not match data received")
                elif part:
                    res = self._store_part(h, path, data, hdr_sha1)
                else:
                    name = urllib.parse.unquote(h.headers["X-Bz-File-Name"])
                    info = {k[len("x-bz-info-"):].lower(): urllib.parse.unquote(v) for k, v in h.headers.items()
                            if k.lower().startswith("x-bz-info-")}
                    meta = self._meta(name, data, h.headers.get("Content-Type"), info, hdr_sha1)
                    self.files[name] = {"meta": meta, "data": data, "parts": None}
                    self.completed.append(name)
                    res = 200, meta
            h.send_json(*res)
        finally:
            with self.lock:
                self.busy.discard(path)

    def _store_part(self, h, path, data, hdr_sha1):
        fid = path.split("/")[2]
        u = self.large.get(fid)
        if self.parts_outage_after is not None and self.stored_parts >= self.parts_outage_after:
            return self._err(503, "service_unavailable", "fake outage")
        if u is None:
            return self._err(400, "bad_request", "no such unfinished file")
        num = int(h.headers["X-Bz-Part-Number"])
        if not 1 <= num <= 10000:
            return self._err(400, "bad_request", "part number")
        u["parts"][num] = {"data": data, "sha1": hdr_sha1}
        self.part_log.append((fid, num))
        self.stored_parts += 1
        return 200, {"fileId": fid, "partNumber": num, "contentLength": len(data), "contentSha1": hdr_sha1,
                     "uploadTimestamp": int(time.time() * 1000)}

    # -- the friendly download URL: /file/<bucket>/<name>
    def download(self, h, path):
        bits = path.split("/", 3)
        with self.lock:
            self.calls["download"] += 1
            f = None
            if len(bits) == 4 and bits[2] == self.bucket_name:
                f = self.files.get(urllib.parse.unquote(bits[3]))
        if f is None:
            return h.send_json(*self._err(404, "not_found", "File with such name does not exist."))
        data, status = f["data"], 200
        rng = h.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng or "")
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else len(data) - 1
            body, status = data[a:b + 1], 206
        else:
            body = data
        h.send_response(status)
        h.send_header("Content-Type", f["meta"]["contentType"])
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Accept-Ranges", "bytes")
        if f["meta"]["fileInfo"].get("b2-cache-control"):
            h.send_header("Cache-Control", f["meta"]["fileInfo"]["b2-cache-control"])
        if status == 206:
            h.send_header("Content-Range", "bytes %d-%d/%d" % (a, a + len(body) - 1, len(data)))
        h.end_headers()
        h.wfile.write(body)


def _handler(fake):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            fake.handle(self, "GET")

        def do_POST(self):
            fake.handle(self, "POST")

    return H


# ---------------------------------------------------------------- the tests

THRESHOLD = 2500        # bytes: bigger files go up as large files...
PART = 1000             # ...in parts of this size (the fake allows parts down to 500 bytes)


def referenced(pack):
    """objects/... -> entry, for everything the pack names (independently of the uploader's own walk)."""
    objs = [pack["gamma"]["modpack_maker_list"], pack["gamma"]["modlist"], pack["profile"]["modlist"]]
    objs += [v for L in pack["layers"] for v in L["volumes"]]
    return {o["path"]: o for o in objs}


class B2Upload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="umbra_b2_test_")
        toml = make_source(cls.tmp)
        # one bigger file, so one volume goes up in many parts
        with open(os.path.join(cls.tmp, "src", "gamma", "mods", "Visual Mod", "gamedata", "textures", "big.dds"),
                  "wb") as f:
            f.write(os.urandom(12000))
        cls.template = os.path.join(cls.tmp, "dist")
        with contextlib.redirect_stdout(io.StringIO()):
            cls.pack = build(toml, cls.template)
        cls.refs = referenced(cls.pack)
        cls.pack_rel = "packs/umbra-%s.json" % cls.pack["version"]
        cls.expected = set(cls.refs) | {cls.pack_rel, "CREDITS.md", "channels/stable.json"}
        cls.large = sorted(p for p, o in cls.refs.items() if o["size"] > THRESHOLD)
        # every file that goes up in parts (the pack manifest is over the tiny test threshold too)
        cls.large_all = sorted(n for n in cls.expected
                               if os.path.getsize(os.path.join(cls.template, *n.split("/"))) > THRESHOLD)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.dist = os.path.join(self.tmp, "dist-" + uuid.uuid4().hex[:8])
        shutil.copytree(self.template, self.dist)
        self.addCleanup(shutil.rmtree, self.dist, True)
        for name, value in (("BACKOFF", 0.001), ("BACKOFF_CAP", 0.005), ("RETRIES", 3)):
            p = mock.patch.object(b2_upload, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = {"B2_KEY_ID": FakeB2.KEY_ID, "B2_APP_KEY": FakeB2.APP_KEY}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ.pop(k, None)
        self.fake = FakeB2()
        self.addCleanup(self.fake.close)

    # -- helpers
    def run_cli(self, *argv, tty=False, answer=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                mock.patch.object(b2_upload, "_stdin_is_tty", return_value=tty), \
                mock.patch("builtins.input", return_value=answer) as asked:
            code = b2_upload.main(list(argv))
        self.asked = asked.called
        text = out.getvalue()
        self.assert_no_secrets(text)
        return code, text

    def upload_args(self, *extra, threads="4"):
        return ("--dist", self.dist, "--bucket", self.fake.bucket_name, "--api-url", self.fake.url,
                "--large-threshold", str(THRESHOLD), "--part-size", str(PART), "--threads", threads) + extra

    def assert_no_secrets(self, text):
        basic = base64.b64encode(("%s:%s" % (FakeB2.KEY_ID, FakeB2.APP_KEY)).encode()).decode()
        for s in [FakeB2.KEY_ID, FakeB2.APP_KEY, basic] + list(self.fake.issued):
            self.assertNotIn(s, text, "a credential or token was printed")

    def local(self, rel):
        with open(os.path.join(self.dist, *rel.split("/")), "rb") as f:
            return f.read()

    def assert_published(self):
        """Everything is in the fake bucket, byte for byte, with the right type, cache hint and hashes."""
        f = self.fake
        self.assertEqual(set(f.files), self.expected)
        for name in self.expected:
            data, meta = self.local(name), f.files[name]["meta"]
            self.assertEqual(f.files[name]["data"], data, name)
            self.assertEqual(meta["fileInfo"]["sha256"], sha256(data), name)
            ext = os.path.splitext(name)[1]
            self.assertEqual(meta["contentType"], {".zip": "application/zip", ".json": "application/json",
                                                   ".txt": "text/plain", ".md": "text/markdown; charset=utf-8"}[ext])
            self.assertEqual(meta["fileInfo"]["b2-cache-control"],
                             "public, max-age=31536000, immutable" if name.startswith("objects/")
                             else "public, max-age=300", name)
            if f.files[name]["parts"] is None:                       # a plain upload: B2's SHA1 of the file
                self.assertEqual(meta["contentSha1"], sha1(data), name)
            else:                                                    # a large file: each part's SHA1, and the whole
                self.assertEqual(meta["fileInfo"]["large_file_sha1"], sha1(data), name)
                off = 0
                for ln, s in f.files[name]["parts"]:
                    self.assertEqual(s, sha1(data[off:off + ln]), name)
                    off += ln
                self.assertEqual(off, len(data))
        self.assertEqual(f.concurrent_use, 0, "an upload URL was used by two threads at once")
        self.assertEqual(f.basic_paths and set(f.basic_paths), {"/b2api/v3/b2_authorize_account"},
                         "the key went somewhere other than b2_authorize_account")

    def assert_order(self, names, prefix=""):
        """Objects, then packs (and CREDITS.md), then the channel last."""
        names = [n[len(prefix):] for n in names]
        rank = {n: (0 if n.startswith("objects/") else 2 if n.startswith("channels/") else 1) for n in names}
        self.assertEqual([rank[n] for n in names], sorted(rank[n] for n in names), names)
        self.assertEqual(names[-1], "channels/stable.json")

    # -- the tests
    def test_large_files_are_in_the_pack(self):
        """Sanity check of the fixture: the large-file path really is exercised."""
        self.assertGreaterEqual(len(self.large), 2)
        self.assertGreater(len(self.refs) - len(self.large), 2)

    def test_upload_through_failures(self):
        f = self.fake
        f.inject["b2_list_file_names"] = [(503, "service_unavailable")]
        f.inject["b2_upload_file"] = [(503, "service_unavailable"), (401, "expired_auth_token")]
        f.inject["b2_upload_part"] = [(503, "service_unavailable")]
        f.inject["b2_finish_large_file"] = [(500, "internal_error")]
        f.lose_reply["b2_finish_large_file"] = 1             # done, but the reply is lost: the retry is refused
        f.drop_parts = {3}
        f.expire_after = 4
        check = f.url + "/file/" + f.bucket_name
        code, out = self.run_cli(*self.upload_args("--yes", "--base-url-check", check))
        self.assertEqual(code, 0, out)
        # every injected failure was met and got past
        self.assertFalse(any(f.inject.values()), f.inject)
        self.assertFalse(any(f.lose_reply.values()), f.lose_reply)
        self.assertEqual(f.dropped, 1)
        self.assertEqual(f.calls["b2_authorize_account"], 2)              # the expired session signed in again
        self.assertIn("signing in again", out)
        self.assertGreaterEqual(f.calls["b2_get_upload_url"], 3)           # new upload URLs after the failures
        self.assertEqual(f.calls["b2_start_large_file"], len(self.large_all))
        self.assertEqual(f.calls["b2_finish_large_file"], len(self.large_all) + 2)   # a 500 and a lost reply
        self.assertEqual(f.large, {})
        self.assert_published()
        self.assert_order(f.completed)
        self.assertEqual(len(f.completed), len(self.expected))            # nothing went up twice
        # the check through the public URL
        self.assertIn("ok   channels/stable.json: version 0.0.1 (Cache-Control: public, max-age=300)", out)
        self.assertIn("partial downloads work (Cache-Control: public, max-age=31536000, immutable)", out)
        self.assertIn("B2's own address for this pack (public buckets): %s/file/%s" % (f.url, f.bucket_name), out)

    def test_existing_objects_are_skipped(self):
        f = self.fake
        f.restrict = False                                      # a key for every bucket: found by b2_list_buckets
        objs = sorted(self.refs)
        keep, wrong = objs[:4], objs[4]
        for n in keep:                                          # as an earlier run would have left them
            f.seed(n, self.local(n), {"sha256": self.refs[n]["sha256"],
                                      "b2-cache-control": "public, max-age=31536000, immutable"},
                   "application/zip" if n.endswith(".zip") else "text/plain")
        f.seed(wrong, self.local(wrong), {})                    # same name, but no sha256 recorded: sent again
        code, out = self.run_cli(*self.upload_args("--yes"))
        self.assertEqual(code, 0, out)
        self.assertEqual(f.calls["b2_list_buckets"], 1)
        self.assertGreater(f.calls["b2_list_file_names"], 4)    # several pages (the fake gives 2 names a page)
        for n in keep:
            self.assertNotIn(n, f.completed)
        self.assertIn(wrong, f.completed)
        self.assertEqual(set(f.completed), self.expected - set(keep))
        self.assertIn("4 files already there", out)
        self.assert_published()
        self.assert_order(f.completed)
        # again: nothing to send
        mark, calls = len(f.completed), dict(f.calls)
        code, out = self.run_cli(*self.upload_args("--yes"))
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to upload", out)
        self.assertEqual(f.uploaded_since(mark), [])
        for api in ("b2_upload_file", "b2_upload_part", "b2_start_large_file", "b2_get_upload_url"):
            self.assertEqual(f.calls[api], calls.get(api, 0), api)

    def test_interrupted_large_upload_resumes(self):
        f = self.fake
        f.parts_outage_after = 6                               # B2 stops taking parts after six
        code, out = self.run_cli(*self.upload_args("--yes", threads="1"))
        self.assertEqual(code, 1, out)
        self.assertIn("no channel was changed", out)
        self.assertFalse(any(n.startswith(("packs/", "channels/")) for n in f.files), list(f.files))
        cut = {fid: set(u["parts"]) for fid, u in f.large.items() if u["parts"]}
        self.assertEqual(len(cut), 1, f.large)
        fid, before = cut.popitem()
        name = f.large[fid]["name"]
        size = self.refs[name]["size"]
        nparts = -(-size // PART)
        self.assertLess(len(before), nparts)
        # B2 is back
        f.parts_outage_after = None
        log_mark = len(f.part_log)
        code, out = self.run_cli(*self.upload_args("--yes"))
        self.assertEqual(code, 0, out)
        self.assertIn("resuming, %d of %d parts already in B2" % (len(before), nparts), out)
        again = sorted(n for i, n in f.part_log[log_mark:] if i == fid)
        self.assertEqual(again, sorted(set(range(1, nparts + 1)) - before))   # only the missing parts were sent
        self.assertEqual(f.files[name]["meta"]["fileId"], fid)                # the same large file, finished
        self.assertEqual(f.calls["b2_cancel_large_file"], 0)
        self.assert_published()
        self.assertEqual(f.completed[-1], "channels/stable.json")

    def test_unfinished_upload_with_other_parts_is_restarted(self):
        f = self.fake
        name = self.large[0]
        data = self.local(name)
        # an earlier attempt with a different part size: part 1 doesn't fit this run's layout
        old = f.seed_unfinished(name, {"sha256": sha256(data)}, {1: data[:700]})
        code, out = self.run_cli(*self.upload_args("--yes"))
        self.assertEqual(code, 0, out)
        self.assertEqual(f.calls["b2_cancel_large_file"], 1)
        self.assertNotIn(old, f.large)
        self.assertNotEqual(f.files[name]["meta"]["fileId"], old)
        self.assert_published()

    def test_dry_run_makes_no_network_calls(self):
        boom = AssertionError("network used in a dry run")
        with mock.patch("urllib.request.urlopen", side_effect=boom) as urlopen, \
                mock.patch("socket.create_connection", side_effect=boom), \
                mock.patch.object(socket.socket, "connect", side_effect=boom):
            code, out = self.run_cli("--dry-run", *self.upload_args("--base-url-check", self.fake.url))
        self.assertEqual(code, 0, out)
        urlopen.assert_not_called()
        self.assertEqual(self.fake.requests, 0)
        total = sum(len(self.local(n)) for n in self.expected)
        self.assertIn("Total: %d files" % len(self.expected), out)
        self.assertIn("(%s bytes)" % format(total, ","), out)
        pos = {n: out.index(n + "  ") for n in self.expected}               # every file listed...
        self.assertEqual(max(pos, key=pos.get), "channels/stable.json")      # ...the channel last
        self.assertLess(max(pos[n] for n in self.refs), pos[self.pack_rel])
        for n in self.large:
            self.assertIn("%s  " % n, out)
        self.assertIn("large file, %d parts" % -(-self.refs[self.large[0]]["size"] // PART), out)

    def test_no_arguments_prints_help_and_does_nothing(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("usage: b2_upload.py", out)
        self.assertIn("--dry-run", out)
        self.assertEqual(self.fake.requests, 0)

    def test_refuses_a_missing_object(self):
        f = self.fake
        gone = sorted(self.refs)[1]
        data = self.local(gone)
        os.unlink(os.path.join(self.dist, *gone.split("/")))
        code, out = self.run_cli(*self.upload_args("--yes"))
        self.assertEqual(code, 1, out)
        self.assertIn("neither in", out)
        self.assertIn(gone, out)
        self.assertEqual(f.completed, [])
        self.assertEqual(f.calls["b2_upload_file"] + f.calls["b2_start_large_file"], 0)
        code, out = self.run_cli("--dry-run", *self.upload_args())
        self.assertEqual(code, 1, out)
        self.assertIn("MISSING", out)
        # when the bucket already has it, it's fine (and it isn't sent)
        f.seed(gone, data, {"sha256": self.refs[gone]["sha256"],
                            "b2-cache-control": "public, max-age=31536000, immutable"},
               "application/zip" if gone.endswith(".zip") else "text/plain")
        code, out = self.run_cli(*self.upload_args("--yes"))
        self.assertEqual(code, 0, out)
        self.assertNotIn(gone, f.completed)
        self.assertEqual(set(f.files), self.expected)

    def test_refuses_a_damaged_object(self):
        victim = self.large[0]
        p = os.path.join(self.dist, *victim.split("/"))
        with open(p, "r+b") as fh:
            b = fh.read(1)
            fh.seek(0)
            fh.write(bytes([b[0] ^ 0xFF]))
        code, out = self.run_cli(*self.upload_args("--yes"))
        self.assertEqual(code, 1, out)
        self.assertIn("content address", out)
        self.assertIn(victim, out)
        self.assertEqual(self.fake.completed, [])
        self.assertEqual(self.fake.calls["b2_start_large_file"] + self.fake.calls["b2_upload_file"], 0)
        code, out = self.run_cli("--dry-run", *self.upload_args())
        self.assertEqual(code, 1, out)

    def test_refuses_a_pack_that_does_not_match_its_channel(self):
        p = os.path.join(self.dist, *self.pack_rel.split("/"))
        with open(p, "ab") as fh:
            fh.write(b" ")
        code, out = self.run_cli("--dry-run", *self.upload_args())
        self.assertEqual(code, 1, out)
        self.assertIn("does not match the sha256 in channels/stable.json", out)

    def test_asks_before_uploading(self):
        f = self.fake
        code, out = self.run_cli(*self.upload_args(), tty=False)
        self.assertEqual(code, 1, out)
        self.assertIn("Pass --yes", out)
        self.assertFalse(self.asked)
        code, out = self.run_cli(*self.upload_args(), tty=True, answer="n")
        self.assertEqual(code, 1, out)
        self.assertTrue(self.asked)
        self.assertEqual(f.completed, [])
        self.assertEqual(f.calls["b2_upload_file"], 0)
        code, out = self.run_cli(*self.upload_args(), tty=True, answer="yes")
        self.assertEqual(code, 0, out)
        self.assert_published()

    def test_key_limited_to_one_bucket(self):
        f = self.fake
        args = list(self.upload_args("--yes"))
        args[args.index("--bucket") + 1] = "someone-elses"
        code, out = self.run_cli(*args)
        self.assertEqual(code, 1, out)
        self.assertIn("this key only works on bucket 'umbra-test'", out)
        self.assertEqual(f.calls["b2_list_buckets"], 0)
        self.assertEqual(f.completed, [])
        f.nested = True                                         # the documented form works the same
        code, out = self.run_cli(*args)
        self.assertEqual(code, 1, out)
        self.assertIn("this key only works on bucket 'umbra-test'", out)
        code, out = self.run_cli(*self.upload_args("--yes"))
        self.assertEqual(code, 0, out)
        self.assertEqual(f.calls["b2_list_buckets"], 0)
        self.assert_published()

    def test_bad_key_and_no_key(self):
        with mock.patch.dict(os.environ, {"B2_APP_KEY": "K004AnotherSecretThatIsWrong"}):
            code, out = self.run_cli(*self.upload_args("--yes"))
            self.assertNotIn("K004AnotherSecretThatIsWrong", out)
        self.assertEqual(code, 1, out)
        self.assertIn("did not accept the key", out)
        with mock.patch.dict(os.environ):
            del os.environ["B2_APP_KEY"]
            code, out = self.run_cli(*self.upload_args("--yes"))
        self.assertEqual(code, 2, out)
        self.assertIn("B2_KEY_ID and B2_APP_KEY", out)
        self.assertEqual(self.fake.completed, [])

    def test_base_url_check_can_fail(self):
        code, out = self.run_cli(*self.upload_args("--yes", "--base-url-check", self.fake.url + "/file/other"))
        self.assertEqual(code, 3, out)
        self.assertIn("FAIL channels/stable.json", out)
        self.assert_published()

    def test_prefix_and_downgrade(self):
        f = self.fake
        f.seed("umbra/channels/stable.json", b'{"old": true}', {"sha256": "0" * 64, "version": "0.1.0"})
        args = self.upload_args("--yes", "--prefix", "umbra")
        code, out = self.run_cli(*args)
        self.assertEqual(code, 1, out)
        self.assertIn("--allow-downgrade", out)
        self.assertEqual(f.completed, [])
        code, out = self.run_cli(*args, "--allow-downgrade", "--base-url-check", f.url + "/file/" + f.bucket_name
                                 + "/umbra")
        self.assertEqual(code, 0, out)
        self.assertIn("version 0.1.0 -> 0.0.1", out)
        self.assertEqual(set(f.files), {"umbra/" + n for n in self.expected})
        self.assert_order(f.completed, "umbra/")


class Units(unittest.TestCase):
    def test_part_hashes(self):
        d = tempfile.mkdtemp(prefix="umbra_b2_test_")
        try:
            data = os.urandom(5 * 1000 + 123)
            p = os.path.join(d, "x")
            with open(p, "wb") as fh:
                fh.write(data)
            with mock.patch.object(b2_upload, "CHUNK", 777):          # chunks that straddle part edges
                n, s256, s1, parts = b2_upload.hash_file(p, 1000)
            self.assertEqual((n, s256, s1), (len(data), sha256(data), sha1(data)))
            self.assertEqual(parts, [sha1(data[i:i + 1000]) for i in range(0, len(data), 1000)])
            it = b2_upload.Item("objects/x.zip", p, len(data), "object", s256)
            b2_upload.assign_parts([it], 1000, 2500)
            self.assertEqual([x[2] for x in it.layout()], [1000] * 5 + [123])
            b2_upload.assign_parts([it], 1000, 10 ** 9)
            self.assertIsNone(it.part_size)
        finally:
            shutil.rmtree(d)

    def test_sizes_and_scrub(self):
        self.assertEqual(b2_upload.parse_size("100MB"), 100_000_000)
        self.assertEqual(b2_upload.parse_size("64MiB"), 64 << 20)
        self.assertEqual(b2_upload.parse_size("1000"), 1000)
        b2_upload.add_secret("s3cr3t-token")
        self.assertEqual(b2_upload.scrub("a s3cr3t-token b"), "a [hidden] b")
        self.assertTrue(b2_upload.version_older("0.9.1", "0.10.0"))
        self.assertIsNone(b2_upload.version_older("beta", "0.1"))


if __name__ == "__main__":
    unittest.main()
