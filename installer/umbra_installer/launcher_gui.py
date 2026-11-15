"""
The Umbra launcher window (launcher.py has the logic and says how Steam starts
it). Like Setup's window, the work happens on a worker thread that reports
through a queue the window empties every 100 ms.
"""
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import traceback
from tkinter import ttk

from . import fetch, install, mo2, packdef, typeface
from .gui import BAD, BONE, LINE, MUTED, OK, PAD, PANEL, PANEL2, PAPER, UMBER, UMBER_HI, SOOT, credits_window, \
    load_image, open_url, set_icon, style
from .launcher import NO_PROTON, Install, child_env, names

QUICK_EXIT = 20       # seconds: MO2 ending sooner than this after Play went wrong


def open_folder(path):
    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", path], env=child_env(host=True), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


WM_CLASS = "umbra"   # the window's class is "Umbra": the menu entry's StartupWMClass (shortcuts.py)


class Window:
    def __init__(self, root, inst, steam_cmd=None):
        self.root, self.inst, self.steam_cmd = root, inst, steam_cmd
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.prog = None                  # (step, fraction, message) from the worker, latest only
        self.proc = None                  # MO2 (and the game), while it runs
        self.proc_what, self.proc_t0 = None, 0
        self.pending = None               # (Installer, plan) waiting for the player's yes
        self._after_runtime = None        # what Play does once the game's runtime is in place
        self._after_restore = None        # what Play does once Umbra's engine is back
        self.closing = False
        self.vars = {}
        style(root)
        st = ttk.Style(root)
        st.configure("Play.TButton", background=UMBER, foreground=PAPER, padding=(80, 14),
                     font=typeface.font("head", 18))
        st.map("Play.TButton", background=[("active", UMBER_HI), ("disabled", PANEL)],
               foreground=[("disabled", MUTED)])
        st.configure("Ok.TLabel", background=SOOT, foreground=OK)
        st.configure("CardHead.TLabel", background=PANEL, foreground=BONE, font=typeface.font("head", 12))
        st.configure("TMenubutton", background=PANEL2, foreground=BONE, padding=(16, 8), arrowcolor=BONE,
                     font=typeface.font("head", 11))
        st.map("TMenubutton", background=[("active", LINE), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
        root.title("Umbra")
        root.configure(bg=SOOT)
        root.minsize(780, 600)
        self._icons = set_icon(root)
        head = tk.Frame(root, bg=SOOT)
        head.pack(fill="x", padx=PAD * 2, pady=(PAD * 2, PAD))
        self.logo = load_image("umbra-logo-520.png")
        if self.logo:
            tk.Label(head, image=self.logo, bg=SOOT).pack()
        else:
            typeface.Label(head, text="UMBRA", style="Logo.TLabel").pack()
        self.sub = typeface.Label(head, style="Muted.TLabel")
        self.sub.pack(pady=(PAD // 2, 0))
        self.body = tk.Frame(root, bg=SOOT)
        self.body.pack(fill="both", expand=True, padx=PAD * 3, pady=(0, PAD * 2))
        self.build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._pump)
        if inst.found and not inst.pack and inst.source:
            self._fetch_installed_pack()

    # ------------------------------------------------------------ the page
    def build(self):
        for w in self.body.winfo_children():
            w.destroy()
        inst = self.inst
        self.sub.configure(text=("Umbra %s - G.A.M.M.A. with xrRazom co-op" % inst.version) if inst.version
                           else "G.A.M.M.A. with xrRazom co-op")
        if not inst.found:
            typeface.Label(self.body, style="TLabel", wraplength=700, justify="left",
                      text="There is no Umbra install here. Run Umbra Setup to install Umbra, or to repair it "
                           "(it puts this launcher in the install).").pack(pady=PAD * 2)
            typeface.Button(self.body, text="Close", command=self.on_close).pack()
            return
        # Play, and which engine
        play = tk.Frame(self.body, bg=SOOT)
        play.pack(pady=(PAD, PAD))
        self.play_img = load_image("umbra-play-104.png")     # the logo's lettering (branding/build.py)
        self.b_play = typeface.Button(play, text="PLAY", image=self.play_img or "", style="Play.TButton",
                                 command=self.play)
        self.b_play.pack()
        eng = tk.Frame(self.body, bg=SOOT)
        eng.pack(pady=(0, PAD * 2))
        self.engines = inst.engines()
        typeface.Label(eng, text="Renderer", style="Muted.TLabel").pack(side="left", padx=(0, PAD))
        self.v_engine = tk.StringVar(value=inst.engine(self.engines) or "")
        self.c_engine = ttk.Combobox(eng, textvariable=self.v_engine, state="readonly", width=28,
                                     values=[t for t, _ in self.engines])
        self.c_engine.pack(side="left")
        # the picks
        self.card = ttk.Frame(self.body, style="Card.TFrame", padding=PAD + 4)
        self.card.pack(fill="x")
        self.build_options()
        # the rest
        tools = tk.Frame(self.body, bg=SOOT)
        tools.pack(pady=(PAD * 2, PAD))
        self.b_update = typeface.Button(tools, text="Check for updates", command=self.check)
        self.b_mo2 = typeface.Button(tools, text="Open Mod Organizer 2", command=self.open_mo2)
        self.b_credits = typeface.Button(tools, text="Credits", command=lambda: credits_window(self.root, self.inst.pack))
        self.b_folders = typeface.Menubutton(tools, text="Folders")
        menu = tk.Menu(self.b_folders, tearoff=0, bg=PANEL2, fg=BONE, activebackground=LINE,
                       activeforeground=BONE, relief="flat")
        for label, path in inst.folders():
            menu.add_command(label=label, command=lambda p=path: self.open_folder(p))
        self.b_folders["menu"] = menu
        for b in (self.b_update, self.b_mo2, self.b_credits, self.b_folders):
            b.pack(side="left", padx=PAD // 2)
        # what is happening
        self.status = typeface.Label(self.body, style="Muted.TLabel", wraplength=700, justify="left")
        self.status.pack(fill="x", pady=(PAD // 2, 0))
        self.bar = ttk.Progressbar(self.body, mode="determinate", maximum=1.0)
        self.actions = tk.Frame(self.body, bg=SOOT)
        self.actions.pack(pady=(PAD // 2, 0))
        self.idle()

    def build_options(self):
        for w in self.card.winfo_children():
            w.destroy()
        self.vars = {}
        pack = self.inst.pack
        if not pack:
            typeface.Label(self.card, style="CardMuted.TLabel", wraplength=680,
                      text="Umbra's options appear here once its pack has been read.").pack(anchor="w")
            self.b_apply = None
            return
        opts = pack.get("options", [])
        if not opts:
            self.card.pack_forget()
            self.b_apply = None
            return
        cur = self.inst.picks()
        for o in opts:
            typeface.Label(self.card, text=o["title"], style="CardHead.TLabel").pack(anchor="w")
            row = ttk.Frame(self.card, style="Card.TFrame")
            row.pack(anchor="w", pady=(6, 4))
            if o.get("choices"):
                v = tk.StringVar(value=packdef.picked(pack, cur, o["id"]))
                for c in o["choices"]:
                    typeface.Radiobutton(row, text=c.get("title") or c["id"], value=c["id"], variable=v).pack(
                        side="left", padx=(0, PAD + 4))
            else:
                v = tk.BooleanVar(value=o["id"] in cur)
                typeface.Checkbutton(row, text="On", variable=v).pack(side="left")
            v.trace_add("write", lambda *_: self.idle())
            self.vars[o["id"]] = v
            if o.get("description"):
                typeface.Label(self.card, text=o["description"], style="CardMuted.TLabel", wraplength=680,
                          justify="left").pack(anchor="w", pady=(0, PAD))
        foot = ttk.Frame(self.card, style="Card.TFrame")
        foot.pack(fill="x")
        self.b_apply = typeface.Button(foot, text="Apply", style="Go.TButton", command=self.apply)
        self.b_apply.pack(side="right")

    def picks(self):
        pack = self.inst.pack
        out = set()
        for o in pack.get("options", []):
            v = self.vars[o["id"]].get()
            if o.get("choices"):
                out.add(packdef.choice_key(o["id"], v))
            elif v:
                out.add(o["id"])
        return packdef.normalize_choices(pack, out)

    # ------------------------------------------------------------ states
    def say(self, text, style="Muted.TLabel"):
        self.status.configure(text=text, style=style)

    def _set_actions(self, *specs):
        for w in self.actions.winfo_children():
            w.destroy()
        for text, cmd, st in specs:
            typeface.Button(self.actions, text=text, command=cmd, style=st).pack(side="left", padx=PAD // 2)

    def idle(self):
        """Nothing running: what can be pressed, and why not."""
        if not self.inst.found or self.worker:
            return
        changed = bool(self.inst.pack and self.vars) and self.picks() != self.inst.picks()
        can_run = os.name == "nt" or self.inst.command(self.steam_cmd) is not None
        for b in (self.b_update, self.b_credits, self.b_folders):
            b.state(["!disabled"])
        for b in (self.b_play, self.b_mo2):
            b.state(["!disabled"] if can_run else ["disabled"])
        if self.engines:
            self.c_engine.state(["!disabled", "readonly"])
        else:
            self.b_play.state(["disabled"])
        if self.b_apply:
            self.b_apply.state(["!disabled"] if changed else ["disabled"])
        if self.pending:
            return
        self.bar.pack_forget()
        if not can_run:
            self.say(NO_PROTON, "Bad.TLabel")
        elif not self.engines:
            self.say("Mod Organizer 2's Run list has no entry that starts Anomaly's engine "
                     "(bin/AnomalyDX11.exe), so Play can't start the game. Open Mod Organizer 2 to check it.",
                     "Bad.TLabel")
        elif changed:
            n = self.inst.download_for(self.picks())
            self.say("Apply installs the change; it downloads about %s." % packdef.human(n) if n else
                     "Apply installs the change. Everything it needs is here already, so it takes a moment.")
        elif not self.status.cget("text") or self.status.cget("style") == "Bad.TLabel":
            self.say("")

    def busy(self, text):
        self._lock()
        self.say(text)
        self.bar.configure(value=0)
        self.bar.pack(fill="x", pady=(PAD // 2, 0), before=self.actions)
        self._set_actions(("Cancel", self.cancel_work, "TButton"))

    # ------------------------------------------------------------ Play and MO2
    def play(self):
        title = self.v_engine.get()
        if title not in [t for t, _ in self.engines]:
            self.say("Pick a renderer first.", "Bad.TLabel")
            return
        self.inst.save_settings(engine=title)
        self._ready_then(lambda: self._start(self.inst.command(self.steam_cmd, title), "game"))

    def open_mo2(self):
        self._ready_then(lambda: self._start(self.inst.command(self.steam_cmd), "mo2"))

    def _ready_then(self, go, restored=False):
        """Umbra's engine and what the game needs from Microsoft first, then go. A G.A.M.M.A.
        install or update puts its own engine over xrRazom's, and xrRazom's scripts then stop
        the game at load; the first start on Linux has no Proton prefix set up yet."""
        if self.proc or self.worker:
            return
        try:
            changed = {} if restored else self.inst.anomaly_changed()
        except Exception:  # noqa: BLE001 - can't tell: let the game say
            traceback.print_exc()
            changed = {}
        if changed:
            files = sorted({f for fs in changed.values() for f in fs})
            self._after_restore = go
            self.cancel = threading.Event()

            def prog(d, t, rel):
                self._progress("layers", (d / t) if t else 1.0, rel)
            self._work(lambda: ("anomaly_restored", self.inst.anomaly_restore(self._log, prog, self.cancel.is_set)),
                       "Umbra's engine was replaced (%s%s; a G.A.M.M.A. install or update does that). Putting "
                       "it back..." % (", ".join(files[:2]), " and %d more" % (len(files) - 2) if len(files) > 2
                                       else ""))
            return
        try:
            missing = self.inst.runtime_missing(self.steam_cmd)
        except Exception:  # noqa: BLE001 - can't tell: let the game say
            traceback.print_exc()
            missing = []
        if not missing:
            go()
            return
        self._after_runtime = go
        self.cancel = threading.Event()

        def prog(done, total):
            self._progress("runtime", 0.8 * ((done / total) if total else 1.0),
                           "downloading, %s of %s" % (packdef.human(done), packdef.human(total)))

        def log(msg, level="info"):                   # each stage after the download, as it starts
            self._log(msg, level)
            if level == "info":
                self._progress("runtime", 0.9, msg)
        text = ("Setting up Proton for Umbra: Microsoft's Visual C++ runtime and DirectX files "
                "(the first start only; it takes a minute)..." if os.name != "nt" else
                "Installing what the game needs from Microsoft (Visual C++ runtime, DirectX files)...")
        self._work(lambda: ("runtime_ready", self.inst.runtime_fix(self.steam_cmd, log, prog,
                                                                   self.cancel.is_set)), text)

    def on_anomaly_restored(self, _):
        self._done_working()
        go, self._after_restore = self._after_restore, None
        if go:
            self._ready_then(go, restored=True)

    def on_runtime_ready(self, _):
        self._done_working()
        go, self._after_runtime = self._after_runtime, None
        self.say("")
        if go:
            go()

    def _lock(self):
        for b in (self.b_play, self.b_mo2, self.b_update):
            b.state(["disabled"])
        if self.b_apply:
            self.b_apply.state(["disabled"])
        self.c_engine.state(["disabled"])

    def _start(self, cmd, what):
        if not cmd or self.proc:
            return
        if mo2.running():
            self.say("Mod Organizer 2 is already open. Start the game from there, or close it and press "
                     "Play here.", "Bad.TLabel")
            return
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.inst.mo2, env=self.inst.env(self.steam_cmd))
        except OSError as e:
            self.say("Could not start Mod Organizer 2: %s" % e, "Bad.TLabel")
            return
        self.proc_what, self.proc_t0 = what, time.time()
        self._lock()
        self.say("Starting..." if what == "game" else "Mod Organizer 2 is open.")
        # out of the way once MO2 is up; back when it closes (with the game)
        self.root.after(1500, lambda: self.proc and self.root.withdraw())
        self.root.after(1000, self._watch)

    def _watch(self):
        if self.closing:
            return
        rc = self.proc.poll()
        if rc is None:
            self.root.after(1000, self._watch)
            return
        quick = time.time() - self.proc_t0 < QUICK_EXIT
        self.proc = None
        self.root.deiconify()
        self.root.lift()
        if quick and self.proc_what == "game":
            self.say("The game closed right after it started%s. Its log is under Folders > Game logs (and Mod "
                     "Organizer 2's in logs/mo_interface.log in its folder); Check for updates > Repair puts "
                     "back anything missing." % (" (exit code %s)" % rc if rc else ""), "Bad.TLabel")
        elif rc and quick:
            self.say("Mod Organizer 2 stopped at once (exit code %s). Its log is logs/mo_interface.log in the "
                     "Mod Organizer 2 folder (Folders)." % rc, "Bad.TLabel")
        else:
            self.say("")
        self.idle()

    # ------------------------------------------------------------ installing
    def apply(self):
        picks = self.picks()
        installer = self._installer(choices=picks)
        self._work(lambda: ("planned_apply", (installer, installer.plan(pack=self.inst.pack))),
                   "Getting the change ready...")

    def check(self):
        installer = self._installer(repair=True)
        self._work(lambda: ("checked", (installer, installer.plan())), "Checking for updates...")

    def _installer(self, **kw):
        self.cancel = threading.Event()
        cancel = self.cancel

        def wait_for_download(e):
            self.q.put(("origin_wait", e))
            fetch.wait_for(e.obj, cancel.is_set)
        return install.Installer(self.inst.options(**kw), log=self._log, progress=self._progress,
                                 cancel=self.cancel, wait_for_download=wait_for_download)

    def _log(self, msg, level="info"):
        self.q.put(("log", (msg, level)))

    def _progress(self, step, frac, msg=""):
        self.prog = (step, frac, msg)

    def _work(self, fn, busy_text):
        """Run fn on the worker; fn returns (kind, arg) for on_<kind>."""
        self.busy(busy_text)

        def go():
            try:
                self.q.put(fn())
            except fetch.Cancelled:
                self.q.put(("cancelled", None))
            except fetch.OriginNeeded as e:
                self.q.put(("origin_needed", e))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.q.put(("failed", str(e) or type(e).__name__))
        self.worker = threading.Thread(target=go, daemon=True)
        self.worker.start()

    def cancel_work(self):
        self.cancel.set()
        self.say("Stopping safely...")

    def _done_working(self):
        self.worker = None
        self.prog = None
        self.bar.pack_forget()
        self._set_actions()

    def on_planned_apply(self, arg):
        installer, plan = arg
        self._done_working()
        if not plan.ok():
            self.say("; ".join(plan.problems), "Bad.TLabel")
            self.idle()
            return
        self._run(installer, plan, "Installing the change...")

    def on_checked(self, arg):
        installer, plan = arg
        self._done_working()
        if not plan.ok():
            self.say("; ".join(plan.problems), "Bad.TLabel")
            self.idle()
            return
        new, have = plan.pack["version"], self.inst.version
        if new == have and plan.changed:
            text = "Umbra %s has new versions of %s. %s" % (
                have, names(plan.changed),
                ("Update downloads about %s." % packdef.human(plan.download_bytes)) if plan.download_bytes
                else "Everything it needs is downloaded already.")
            go = "Update"
        elif new != have:
            text = "Umbra %s is out (you have %s). %s" % (
                new, have or "an older version",
                ("It downloads about %s." % packdef.human(plan.download_bytes)) if plan.download_bytes
                else "Everything it needs is downloaded already.")
            go = "Update"
        else:
            text = ("Umbra %s is the newest. Repair checks every Umbra mod and puts back anything missing%s."
                    % (have, (", downloading about %s" % packdef.human(plan.download_bytes))
                       if plan.download_bytes else ""))
            go = "Repair"
        if plan.warnings:
            text += "\n" + "\n".join(plan.warnings)
        self.pending = (installer, plan)
        self.say(text, "TLabel")
        self._set_actions((go, self.confirm, "Go.TButton"), ("Not now", self.decline, "TButton"))

    def confirm(self):
        installer, plan = self.pending
        self.pending = None
        self._set_actions()
        self._run(installer, plan, "Updating Umbra..." if plan.pack["version"] != self.inst.version or plan.changed
                  else "Repairing Umbra...")

    def decline(self):
        self.pending = None
        self._set_actions()
        self.say("")
        self.idle()

    def _run(self, installer, plan, text):
        self._work(lambda: ("installed", installer.run(plan)), text)

    def on_installed(self, state):
        self._done_working()
        old = self.inst.version
        self.inst.reload()
        self.build()
        if self.inst.version != old:
            self.say("Umbra is up to date (%s)." % self.inst.version, "Ok.TLabel")
        else:
            self.say("Done. Everything is in place.", "Ok.TLabel")

    def _fetch_installed_pack(self):
        """An install from before the launcher has no pack.json: read the pack, and use it for the
        options if it is the installed version (quietly: without it they just aren't shown)."""
        def go():
            try:
                self.q.put(("fetched_pack", fetch.load_pack(self.inst.source, self.inst.channel)[1]))
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=go, daemon=True).start()

    def on_fetched_pack(self, pack):
        if pack.get("version") != self.inst.version or self.inst.pack:
            return
        self.inst.save_pack(pack)
        if not self.worker and not self.proc:
            self.build_options()
            self.idle()

    def on_cancelled(self, _):
        self._done_working()
        if self._after_runtime or self._after_restore:
            self._after_runtime = self._after_restore = None
            self.say("Stopped. Play sets it up again.")
            self.idle()
            return
        self.say("Stopped. Nothing half-done was left behind; Check for updates finishes it.")
        self.idle()

    def on_failed(self, msg):
        self._done_working()
        self._after_runtime = self._after_restore = None
        self.say("That didn't work: %s\nThe details are in Umbra Setup's logs (Folders)." % msg, "Bad.TLabel")
        self.idle()

    def on_origin_wait(self, e):
        try:
            open_url(e.download)
        except OSError:
            pass
        self.prog = ("origins", 0.0, '%s would not hand "%s" to the launcher, so your browser is getting it (its '
                                     'author asks that it come from %s). If the page asks, press Download. The '
                                     'update carries on by itself once the file is in %s.'
                     % (e.site, e.name, e.site, e.folder or "your Downloads folder"))

    def on_origin_needed(self, e):
        """An author's own download the launcher couldn't get: their page opens in the browser."""
        self._done_working()
        self._after_runtime = self._after_restore = None
        try:
            open_url(e.page)
            opened = " Your browser has opened the page."
        except OSError:
            opened = ""
        self.say("%s%s" % (e, opened), "Bad.TLabel")
        self.idle()

    def on_log(self, arg):
        msg, level = arg
        if level in ("warn", "error") and self.worker:
            self.say(msg, "Bad.TLabel" if level == "error" else "Muted.TLabel")

    # ------------------------------------------------------------ plumbing
    def open_folder(self, path):
        if not os.path.isdir(path):
            self.say("%s isn't there yet." % path, "Bad.TLabel")
            return
        try:
            open_folder(path)
        except OSError as e:
            self.say("Could not open %s: %s" % (path, e), "Bad.TLabel")

    def _pump(self):
        try:
            while True:
                kind, arg = self.q.get_nowait()
                getattr(self, "on_" + kind)(arg)
        except queue.Empty:
            pass
        p = self.prog
        if p and self.worker:
            step, frac, msg = p
            title = {s[0]: s[1] for s in install.STEPS}.get(step, step)
            self.bar.configure(value=max(0.0, min(1.0, frac)))
            self.say("%s: %s" % (title, msg) if msg else title)
        if not self.closing:
            self.root.after(100, self._pump)

    def on_close(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.worker.join(timeout=10)
        self.closing = True
        self.root.destroy()


def main(work=None, steam_cmd=None, source=None, smoke=None):
    from .launcher import default_work
    root = tk.Tk(className=WM_CLASS)
    inst = Install(work or default_work(), source)
    if steam_cmd:
        try:
            inst.remember_steam(steam_cmd)         # for starts from the menu, without Steam
        except OSError:
            traceback.print_exc()
    win = Window(root, inst, steam_cmd)
    if smoke:
        root.after(int(smoke * 1000), win.on_close)
    root.mainloop()
    return 0
