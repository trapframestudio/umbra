"""
The Umbra Setup window: welcome, where to install, installing, done.

All the work happens in install.py on a worker thread; it reports through a
queue that the window empties every 100 ms (Tk is only touched from the main
thread).
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, ttk

from . import VERSION, fetch, install, mo2, packdef, shortcuts, steam, typeface

# Umbra's colours (branding/README.md)
SOOT, PANEL, PANEL2, LINE = "#14130f", "#1c1a15", "#24211b", "#3a352b"
BONE, MUTED, PAPER = "#d9cca6", "#9a907a", "#cdbf99"
UMBER, UMBER_HI, OK, BAD = "#5e4532", "#71533c", "#8fa66b", "#d0614a"
PAD = 12


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / float(dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _covered(kind, x, y, s):
    """Is the point (x, y) of an s-pixel step icon inside its shape?"""
    c = s / 2.0
    d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
    if kind == "todo":                                    # a ring
        return s * 0.27 <= d <= s * 0.39
    if kind == "now":                                     # a dot
        return d <= s * 0.36
    k = s / 14.0                                          # a tick
    return min(_seg_dist(x, y, 2.6 * k, 7.4 * k, 5.8 * k, 10.6 * k),
               _seg_dist(x, y, 5.8 * k, 10.6 * k, 11.6 * k, 3.6 * k)) <= 1.15 * k


def step_icon(kind, fg, bg, size=14):
    """A step mark drawn pixel by pixel, smoothed against bg. The Linux
    program's Tk only has X's core bitmap fonts, which have no ✔ or ●."""
    img = tk.PhotoImage(width=size, height=size)
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    n = 4                                                 # 4x4 samples a pixel
    for y in range(size):
        for x in range(size):
            a = sum(_covered(kind, x + (i + 0.5) / n, y + (j + 0.5) / n, size)
                    for i in range(n) for j in range(n)) / float(n * n)
            if a > 0:
                img.put("#%02x%02x%02x" % tuple(round(b[i] + (f[i] - b[i]) * a) for i in range(3)), (x, y))
    return img


def _cli_profiles():
    """stalker-gamma-cli's profiles (the active one first), from its settings."""
    if os.name == "nt":
        base = os.environ.get("APPDATA", "")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(base, "stalker-gamma", "settings.json"), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return []
    return sorted(s.get("Profiles") or [], key=lambda p: not p.get("Active"))


def _cli_paths(p):
    """A path from those settings, as given or under the home folder."""
    return (p, os.path.join(os.path.expanduser("~"), p)) if p else ()


def detect_gamma():
    """A G.A.M.M.A. MO2 folder from stalker-gamma-cli's settings, if there is one."""
    for p in _cli_profiles():
        for cand in _cli_paths(p.get("Gamma")):
            if mo2.is_instance(cand):
                return os.path.abspath(cand)
    return None


def detect_gamma_cache():
    """stalker-gamma-cli's download cache, if one has archives in it: a new
    install can use it and download only what is missing."""
    for p in _cli_profiles():
        for cand in _cli_paths(p.get("Cache")):
            try:
                names = os.listdir(cand)
            except OSError:
                continue
            if any(n.lower().endswith((".7z", ".zip", ".rar")) for n in names):
                return os.path.abspath(cand)
    return None


def style(root):
    """Umbra's ttk look (Setup and the launcher), in Umbra's typefaces."""
    typeface.setup(root)
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure(".", background=SOOT, foreground=BONE, fieldbackground=PANEL2, bordercolor=LINE,
                 lightcolor=LINE, darkcolor=LINE, troughcolor=PANEL, focuscolor=UMBER)
    st.configure("TFrame", background=SOOT)
    st.configure("Card.TFrame", background=PANEL)
    st.configure("TLabel", background=SOOT, foreground=BONE)
    st.configure("Card.TLabel", background=PANEL, foreground=BONE)
    st.configure("Muted.TLabel", background=SOOT, foreground=MUTED)
    st.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED)
    st.configure("Bad.TLabel", background=SOOT, foreground=BAD)
    st.configure("Logo.TLabel", background=SOOT, foreground=BONE, font=typeface.font("head", 40))
    st.configure("CreditsHead.TLabel", background=PANEL, foreground=BONE, font=typeface.font("head", 13))
    st.configure("Head.TLabel", background=SOOT, foreground=BONE, font=typeface.font("head", 16))
    st.configure("TButton", background=PANEL2, foreground=BONE, padding=(16, 8), borderwidth=1,
                 font=typeface.font("head", 11))
    st.map("TButton", background=[("active", LINE), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
    st.configure("Go.TButton", background=UMBER, foreground=PAPER, padding=(22, 9), font=typeface.font("head", 12))
    st.map("Go.TButton", background=[("active", UMBER_HI), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
    st.configure("TEntry", foreground=BONE, insertcolor=BONE, padding=5)
    st.configure("TCheckbutton", background=PANEL, foreground=BONE)
    st.map("TCheckbutton", background=[("active", PANEL)])
    st.configure("TRadiobutton", background=PANEL, foreground=BONE)
    st.map("TRadiobutton", background=[("active", PANEL)])
    st.configure("Card.TRadiobutton", background=PANEL, foreground=BONE, font=typeface.font("head", 12))
    st.map("Card.TRadiobutton", background=[("active", PANEL)])
    st.configure("TCombobox", fieldbackground=PANEL2, foreground=BONE, background=PANEL2, arrowcolor=BONE)
    # clam paints a read-only box light grey: keep it dark, and its list too
    st.map("TCombobox", fieldbackground=[("readonly", PANEL2), ("disabled", PANEL)],
           foreground=[("disabled", MUTED), ("readonly", BONE)], background=[("active", LINE)],
           selectbackground=[("readonly", PANEL2)], selectforeground=[("readonly", BONE)])
    for k, v in (("background", PANEL2), ("foreground", BONE), ("selectBackground", UMBER),
                 ("selectForeground", PAPER)):
        root.option_add("*TCombobox*Listbox." + k, v)
    st.configure("Horizontal.TProgressbar", background=UMBER, troughcolor=PANEL, bordercolor=LINE, thickness=14)


def load_image(name):
    p = install.asset_path(name)
    if not p:
        return None
    try:
        return tk.PhotoImage(file=p)
    except tk.TclError:
        return None


def set_icon(root):
    """The window icon; returns the images (keep them, or Tk drops them)."""
    icons = [i for i in (load_image("umbra-icon-256.png"), load_image("umbra-icon-64.png")) if i]
    if icons:
        try:
            root.iconphoto(True, *icons)
        except tk.TclError:
            pass
    return icons


def credits_window(master, pack):
    """The credits, by role, in a window of their own."""
    w = tk.Toplevel(master, bg=SOOT)
    w.title("Umbra credits")
    w.geometry("720x560")
    t = tk.Text(w, bg=PANEL, fg=BONE, relief="flat", wrap="word", padx=14, pady=12, font="TkTextFont")
    t.pack(fill="both", expand=True, padx=PAD, pady=PAD)
    t.tag_configure("h", font=typeface.font("head", 13), spacing1=10, spacing3=4)
    t.tag_configure("m", foreground=MUTED)
    styles = {"": "Card.TLabel", "h": "CreditsHead.TLabel", "m": "CardMuted.TLabel"}
    t._imgs = []

    def put(text, tag=""):
        # Tk can't draw Umbra's fonts here (typeface.py): each piece goes in drawn, a line at a time
        if typeface.MODE != "image":
            t.insert("end", text, tag)
            return
        for i, line in enumerate(text.split("\n")):
            if i:
                t.insert("end", "\n", tag)
            if line.strip():
                img = typeface.text_image(t, line, styles[tag], wrap=640)
                t._imgs.append(img)
                t.image_create("end", image=img, align="baseline")
                if tag:
                    t.tag_add(tag, "end-2c")      # (a heading's spacing comes with its tag)
            elif line:
                t.insert("end", line, tag)
    if not pack:
        put("The credits come with the pack, which hasn't loaded yet.")
    else:
        roles = [("base", "The base"), ("engine", "Engine"), ("extra", "Mods in Umbra"),
                 ("gamma-optional", "G.A.M.M.A. options Umbra turns on"),
                 ("baked-models", "Models in Zone Gore Overhaul's bake"), ("tool", "Tools")]
        cs = pack.get("credits", [])
        for role, title in roles:
            rows = [c for c in cs if (c.get("role") or "extra") == role]
            if not rows:
                continue
            put(title + "\n", "h")
            for c in rows:
                put("%s  " % c.get("mod", c.get("id")))
                put("by %s" % (", ".join(c.get("authors") or []) or "author unknown"), "m")
                if c.get("url"):
                    put("\n    " + c["url"], "m")
                put("\n")
    t.configure(state="disabled")
    typeface.Button(w, text="Close", command=w.destroy).pack(pady=(0, PAD))
    return w



def open_url(url):
    """The player's browser, on a page (outside this program's own libraries)."""
    from .launcher import child_env
    if os.name == "nt":
        os.startfile(url)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", url], env=child_env(host=True), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

class App:
    def __init__(self, root, source, channel="stable", gamma_cache=None):
        self.root = root
        self.source = source
        self.channel = channel
        self.gamma_cache = gamma_cache or detect_gamma_cache()   # downloads a new install can reuse
        self.q = queue.Queue()
        self.pack = None
        self.plan = None
        self.cancel = threading.Event()
        self.worker = None
        self.closing = False
        self.log_lines = []
        self.state = None
        self._plan_seq = 0
        self._saved_for = None            # the install folder whose last picks the options were set from
        self._style()
        root.title("Umbra Setup")
        root.configure(bg=SOOT)
        root.minsize(860, 640)
        self._icon()
        self.header = tk.Frame(root, bg=SOOT)
        self.header.pack(fill="x", padx=PAD * 2, pady=(PAD * 2, PAD))
        self.logo = self._image("umbra-logo-520.png")
        if self.logo:
            tk.Label(self.header, image=self.logo, bg=SOOT).pack()
        else:
            typeface.Label(self.header, text="UMBRA", style="Logo.TLabel").pack()
        self.body = tk.Frame(root, bg=SOOT)
        self.body.pack(fill="both", expand=True, padx=PAD * 3, pady=(0, PAD))
        self.footer = tk.Frame(root, bg=SOOT)
        self.footer.pack(fill="x", padx=PAD * 3, pady=(0, PAD * 2))
        self.pages = {}
        for name in ("welcome", "where", "install", "done"):
            f = tk.Frame(self.body, bg=SOOT)
            self.pages[name] = f
            getattr(self, "_page_" + name)(f)
        self.show("welcome")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._pump)
        self._load_pack()

    # ------------------------------------------------------------ look
    def _style(self):
        style(self.root)

    def _image(self, name):
        return load_image(name)

    def _icon(self):
        self._icons = set_icon(self.root)

    def _text(self, parent, text, style="TLabel", wrap=760, **kw):
        lab = typeface.Label(parent, text=text, style=style, wraplength=wrap, justify="left", **kw)
        return lab

    def _buttons(self, *specs):
        for w in self.footer.winfo_children():
            w.destroy()
        for side, text, cmd, style, name in specs:
            b = typeface.Button(self.footer, text=text, command=cmd, style=style)
            b.pack(side=side, padx=(PAD // 2, 0) if side == "right" else (0, PAD // 2))
            setattr(self, "b_" + name, b)

    def show(self, name):
        for f in self.pages.values():
            f.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.page = name
        getattr(self, "_enter_" + name, lambda: None)()

    # ------------------------------------------------------------ welcome
    def _page_welcome(self, f):
        self._text(f, "S.T.A.L.K.E.R. Anomaly with G.A.M.M.A., co-op through xrRazom, and a hand-picked set of "
                      "visual, sound and gore mods on top, set up and ready to play.", wrap=780).pack(anchor="w")
        self.l_pack = self._text(f, "Reading the pack...", style="Muted.TLabel")
        self.l_pack.pack(anchor="w", pady=(PAD, 0))
        self._text(f, "Umbra is made of other people's work: the G.A.M.M.A. team, the Anomaly team, GSC Game World, "
                      "xrRazom, and every mod author listed under Credits. Thank you, all of you.",
                   style="Muted.TLabel", wrap=780).pack(anchor="w", pady=(PAD * 2, 0))

    def _enter_welcome(self):
        self._buttons(("left", "Credits", self.show_credits, "TButton", "credits"),
                      ("right", "Next", lambda: self.show("where"), "Go.TButton", "next"),
                      ("right", "Quit", self.on_close, "TButton", "quit"))
        if not self.pack:                 # nothing to install without the pack: Next waits for it
            self.b_next.state(["disabled"])

    def _load_pack(self):
        if not self.source:
            self.l_pack.configure(text="This copy of Umbra Setup has no download address. Start it with --source URL.",
                                  style="Bad.TLabel")
            return

        def work():
            try:
                ch, pack = fetch.load_pack(self.source, self.channel)
                self.q.put(("pack", pack))
            except Exception as e:  # noqa: BLE001
                self.q.put(("pack_error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def on_pack(self, pack):
        self.pack = pack
        n = sum(1 for L in pack["layers"] if L.get("target", "mods") == "mods")
        self.l_pack.configure(text="Umbra %s (%s): G.A.M.M.A. plus %d mods, %s to download for Umbra itself."
                                   % (pack["version"], self.channel, n, packdef.human(packdef.total_download(pack))),
                              style="Muted.TLabel")
        for w in self.f_options.winfo_children():
            w.destroy()
        self.v_opts = {}
        self._saved_for = None
        for o in pack.get("options", []):
            if o.get("choices"):
                # pick one: a row of radio buttons, each with what it adds to the download
                v = tk.StringVar(value=o["default"])
                self.v_opts[o["id"]] = v
                typeface.Label(self.f_options, text=o.get("title") or o["id"], style="Card.TLabel").pack(anchor="w")
                row = ttk.Frame(self.f_options, style="Card.TFrame")
                row.pack(anchor="w", padx=(24, 0))
                for c in o["choices"]:
                    size = packdef.option_bytes(pack, packdef.choice_key(o["id"], c["id"]))
                    typeface.Radiobutton(row, text="%s (+%s)" % (c.get("title") or c["id"], packdef.human(size)),
                                    value=c["id"], variable=v, command=self.replan).pack(side="left", padx=(0, 14))
                if o.get("description"):
                    self._text(self.f_options, o["description"], style="CardMuted.TLabel", wrap=720).pack(
                        anchor="w", padx=(24, 0))
                continue
            v = tk.BooleanVar(value=bool(o.get("default")))
            self.v_opts[o["id"]] = v
            typeface.Checkbutton(self.f_options, text="%s (+%s)" % (o.get("title") or o["id"],
                                                               packdef.human(packdef.option_bytes(pack, o["id"]))),
                            variable=v, command=self.replan).pack(anchor="w")
            if o.get("description"):
                self._text(self.f_options, o["description"], style="CardMuted.TLabel", wrap=720).pack(
                    anchor="w", padx=(24, 0))
        self.replan()

    # ------------------------------------------------------------ where
    def _page_where(self, f):
        self.v_mode = tk.StringVar(value="new")
        self.v_root = tk.StringVar(value=install.default_root())
        found = detect_gamma()
        self.v_mo2 = tk.StringVar(value=found or "")
        if found:
            self.v_mode.set("existing")
        self.v_steam = tk.BooleanVar(value=bool(steam.steam_roots()))
        cards = tk.Frame(f, bg=SOOT)
        cards.pack(fill="x")

        def card(mode, title, text, var, pick):
            c = ttk.Frame(cards, style="Card.TFrame", padding=PAD)
            c.pack(fill="x", pady=(0, PAD))
            typeface.Radiobutton(c, text=title, value=mode, variable=self.v_mode, style="Card.TRadiobutton",
                            command=self.replan).pack(anchor="w")
            self._text(c, text, style="CardMuted.TLabel", wrap=740).pack(anchor="w", padx=(24, 0), pady=(2, 6))
            row = ttk.Frame(c, style="Card.TFrame")
            row.pack(fill="x", padx=(24, 0))
            e = ttk.Entry(row, textvariable=var)
            e.pack(side="left", fill="x", expand=True)
            e.bind("<FocusOut>", lambda ev: self.replan())
            e.bind("<Return>", lambda ev: self.replan())
            typeface.Button(row, text="Browse...", command=pick).pack(side="left", padx=(PAD // 2, 0))
            return c

        c_new = card("new", "Install everything",
                     "G.A.M.M.A. and Umbra into an empty folder. G.A.M.M.A. itself is downloaded from its sources "
                     "by stalker-gamma-cli: about 150 GB and an hour or more.", self.v_root, self.pick_root)
        self.v_reuse = tk.BooleanVar(value=bool(self.gamma_cache))
        if self.gamma_cache:
            typeface.Checkbutton(c_new, text="Reuse the G.A.M.M.A. downloads already in %s (only what is missing is "
                                        "downloaded, into that folder)" % self.gamma_cache,
                            variable=self.v_reuse, command=self.replan).pack(anchor="w", padx=(24, 0), pady=(6, 0))
        card("existing", "I already have G.A.M.M.A.",
             "Add Umbra to your G.A.M.M.A. install (its Mod Organizer 2 folder). Your G.A.M.M.A. profile is kept as "
             "it is; Umbra gets its own profile.", self.v_mo2, self.pick_mo2)
        opts = ttk.Frame(f, style="Card.TFrame", padding=PAD)
        opts.pack(fill="x")
        row = ttk.Frame(opts, style="Card.TFrame")
        row.pack(fill="x")
        typeface.Checkbutton(row, text="Add Umbra to Steam (a shortcut in your library)", variable=self.v_steam,
                        command=self.replan).pack(side="left")
        self.v_proton = tk.StringVar(value="proton_experimental")
        if os.name != "nt":
            roots = steam.steam_roots()
            tools = steam.compat_tools(roots[0]) if roots else [("proton_experimental", "Proton Experimental")]
            self._tools = tools
            typeface.Label(row, text="   runs with", style="CardMuted.TLabel").pack(side="left")
            cb = ttk.Combobox(row, state="readonly", width=26, values=[t[1] for t in tools])
            cb.current(len(tools) - 1)
            cb.bind("<<ComboboxSelected>>", lambda e: self.v_proton.set(tools[cb.current()][0]))
            cb.pack(side="left", padx=(6, 0))
        self.v_shortcuts = tk.BooleanVar(value=True)
        typeface.Checkbutton(opts, text="Add Umbra to %s and the desktop, to start it without Steam too"
                        % shortcuts.where_words(), variable=self.v_shortcuts).pack(anchor="w", pady=(6, 0))
        self.f_options = ttk.Frame(opts, style="Card.TFrame")
        self.f_options.pack(fill="x", pady=(6, 0))
        self.v_opts = {}
        self.l_plan = self._text(f, "", style="Muted.TLabel")
        self.l_plan.pack(anchor="w", pady=(PAD, 0))
        self.l_problems = self._text(f, "", style="Bad.TLabel")
        self.l_problems.pack(anchor="w", pady=(4, 0))

    def _enter_where(self):
        self._buttons(("left", "Back", lambda: self.show("welcome"), "TButton", "back"),
                      ("right", "Install", self.start, "Go.TButton", "install"))
        self.b_install.state(["disabled"])
        self.replan()

    def pick_root(self):
        d = filedialog.askdirectory(title="Where to install Umbra", initialdir=os.path.dirname(self.v_root.get()))
        if d:
            self.v_root.set(os.path.join(d, "Umbra") if os.listdir(d) else d)
            self.v_mode.set("new")
            self.replan()

    def pick_mo2(self):
        d = filedialog.askdirectory(title="Your G.A.M.M.A. folder (with ModOrganizer.exe)")
        if d:
            self.v_mo2.set(d)
            self.v_mode.set("existing")
            self.replan()

    def options(self):
        # an extra profile's option brings its own Steam shortcut when Steam is on
        return install.Options(mode=self.v_mode.get(), root=self.v_root.get().strip() or None,
                               mo2_dir=self.v_mo2.get().strip() or None, source=self.source, channel=self.channel,
                               steam=self.v_steam.get(), compat_tool=self.v_proton.get(),
                               choices=self.picks() if self.pack else None,
                               gamma_cache=self.gamma_cache if self.v_reuse.get() else None,
                               profile_shortcuts=True, shortcuts=self.v_shortcuts.get())

    def picks(self):
        out = set()
        for k, v in self.v_opts.items():
            if isinstance(v, tk.StringVar):
                out.add(packdef.choice_key(k, v.get()))
            elif v.get():
                out.add(k)
        return out

    def replan(self):
        if self.page != "where" or not self.pack:
            return
        self._plan_seq += 1
        seq = self._plan_seq
        opts = self.options()
        self.l_plan.configure(text="Checking...")

        def work():
            try:
                self.q.put(("plan", (seq, install.Installer(opts).plan(pack=self.pack))))
            except Exception as e:  # noqa: BLE001
                self.q.put(("plan", (seq, e)))
        threading.Thread(target=work, daemon=True).start()

    def on_plan(self, arg):
        seq, plan = arg
        if seq != self._plan_seq or self.page != "where":
            return
        if isinstance(plan, Exception):
            self.l_plan.configure(text="")
            self.l_problems.configure(text=str(plan))
            self.b_install.state(["disabled"])
            return
        if plan.saved is not None and self._saved_for != plan.mo2:
            # Umbra is already installed there: start from what the player picked last time
            self._saved_for = plan.mo2
            last = packdef.normalize_choices(self.pack, plan.saved)
            changed = False
            for k, v in self.v_opts.items():
                want = packdef.picked(self.pack, last, k) if isinstance(v, tk.StringVar) else (k in last)
                if v.get() != want:
                    v.set(want)
                    changed = True
            if changed:
                self.replan()
                return
        self.plan = plan
        self.l_plan.configure(text="Needs about %s free; %s free there.%s" % (
            packdef.human(plan.need_bytes), packdef.human(plan.free_bytes),
            ("\n" + "\n".join(plan.warnings)) if plan.warnings else ""))
        self.l_problems.configure(text="\n".join(plan.problems))
        self.b_install.state(["!disabled"] if plan.ok() else ["disabled"])

    # ------------------------------------------------------------ install
    def _page_install(self, f):
        self.step_labels = {}
        self.steps_frame = ttk.Frame(f)
        self.steps_frame.pack(fill="x")
        self.progress = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=(PAD, 4))
        self.l_status = self._text(f, "", style="Muted.TLabel", wrap=780)
        self.l_status.pack(anchor="w")
        box = tk.Frame(f, bg=LINE, padx=1, pady=1)
        box.pack(fill="both", expand=True, pady=(PAD, 0))
        self.t_log = tk.Text(box, height=8, bg=PANEL, fg=MUTED, insertbackground=BONE, relief="flat",
                             wrap="word", font=("TkFixedFont", 9), state="disabled", padx=8, pady=6)
        sb = ttk.Scrollbar(box, command=self.t_log.yview)
        self.t_log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.t_log.pack(side="left", fill="both", expand=True)
        self.t_log.tag_configure("warn", foreground=PAPER)
        self.t_log.tag_configure("error", foreground=BAD)

    def _enter_install(self):
        self._buttons(("right", "Cancel", self.on_cancel, "TButton", "cancel"))

    def start(self):
        if not self.plan or not self.plan.ok():
            return
        for w in self.steps_frame.winfo_children():
            w.destroy()
        self.step_labels = {}
        if not getattr(self, "icons", None):
            self.icons = {"todo": step_icon("todo", MUTED, SOOT), "now": step_icon("now", BONE, SOOT),
                          "done": step_icon("done", OK, SOOT)}
        for sid, title, _ in self.plan.steps:
            lab = typeface.Label(self.steps_frame, text="  " + title, image=self.icons["todo"], compound="left",
                            style="Muted.TLabel")
            lab.pack(anchor="w")
            self.step_labels[sid] = (lab, title)
        self.weights = {s[0]: s[2] for s in self.plan.steps}
        self.done_steps = set()
        self.show("install")
        self.cancel.clear()
        opts = self.options()
        plan = self.plan

        def log(msg, level="info"):
            self.q.put(("log", (level, msg)))

        def progress(step, frac, msg=""):
            self.q.put(("progress", (step, frac, msg)))

        self.elsewhere = []                # folders the player picked a downloaded file from

        def wait_for_download(e):
            self.q.put(("origin_wait", e))
            fetch.wait_for(e.obj, self.cancel.is_set, lambda: fetch.downloads_dirs() + self.elsewhere)
            self.q.put(("origin_found", e))

        def work():
            try:
                st = install.Installer(opts, log, progress, self.cancel, wait_for_download).run(plan)
                self.q.put(("finished", st))
            except fetch.Cancelled:
                self.q.put(("failed", "Cancelled. Start Umbra Setup again to continue where it stopped."))
            except fetch.OriginNeeded as e:
                self.q.put(("origin_needed", e))
            except Exception as e:  # noqa: BLE001
                log(traceback.format_exc(), "debug")
                self.q.put(("failed", str(e)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def on_log(self, arg):
        level, msg = arg
        self.log_lines.append((level, msg))
        if level == "debug":
            return
        self.t_log.configure(state="normal")
        self.t_log.insert("end", msg + "\n", (level,) if level in ("warn", "error") else ())
        self.t_log.see("end")
        self.t_log.configure(state="disabled")

    def on_progress(self, arg):
        step, frac, msg = arg
        if step in self.step_labels:
            for sid, (lab, title) in self.step_labels.items():
                if sid == step:
                    lab.configure(image=self.icons["done" if frac >= 1.0 else "now"], style="TLabel")
                    if frac >= 1.0:
                        self.done_steps.add(sid)
                elif sid in self.done_steps:
                    lab.configure(image=self.icons["done"], style="Muted.TLabel")
            total = sum(self.weights.values()) or 1
            before = sum(self.weights[s] for s in self.done_steps if s != step)
            cur = self.weights.get(step, 0) * (0 if step in self.done_steps and frac < 1 else min(1.0, frac))
            if step in self.done_steps:
                cur = 0
            self.progress["value"] = 1000 * min(1.0, (before + cur + (self.weights.get(step, 0)
                                                                      if step in self.done_steps else 0)) / total)
        self.l_status.configure(text=msg)

    def on_cancel(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.l_status.configure(text="Stopping...")
            self.b_cancel.state(["disabled"])

    def on_finished(self, st):
        self.state = st
        self.show("done")

    def on_failed(self, msg):
        self.on_log(("error", msg))
        self.l_status.configure(text=msg, style="Bad.TLabel")
        self._buttons(("left", "Save log", self.save_log, "TButton", "savelog"),
                      ("right", "Try again", self.start, "Go.TButton", "retry"),
                      ("right", "Close", self.on_close, "TButton", "close"))

    def on_origin_wait(self, e):
        """The author's site wants a browser: the player's opens its download link (the download
        starts there by itself), and Setup carries on once the file is in Downloads."""
        try:
            open_url(e.download)
        except OSError:
            pass
        where = e.folder or "your Downloads folder"
        self.l_status.configure(
            text='%s would not hand "%s" to Setup directly, so your browser is getting it (its author asks that it '
                 'come from %s). If the page asks, press Download. Setup carries on by itself once the file is in %s.'
                 % (e.site, e.name, e.site, where), style="TLabel")

        def elsewhere():
            p = filedialog.askopenfilename(title='Where did you save "%s"?' % e.name,
                                           filetypes=[(e.name, "*" + os.path.splitext(e.name)[1]), ("All", "*")])
            if p:
                self.elsewhere.append(os.path.dirname(p))
        self._buttons(("left", "Open the %s page again" % e.site, lambda: open_url(e.page), "TButton", "openpage"),
                      ("left", "I saved it somewhere else...", elsewhere, "TButton", "elsewhere"),
                      ("right", "Cancel", self.on_cancel, "TButton", "cancel"))

    def on_origin_found(self, e):
        self.l_status.configure(text='Got "%s"' % e.name, style="Muted.TLabel")
        self._enter_install()

    def on_origin_needed(self, e):
        """An author's own download Setup couldn't get by itself: the player gets it from the page."""
        self.on_failed(str(e))
        self._buttons(("left", "Save log", self.save_log, "TButton", "savelog"),
                      ("right", "Try again", self.start, "Go.TButton", "retry"),
                      ("right", "Open the %s page" % e.site, lambda: open_url(e.page), "TButton", "openpage"),
                      ("right", "Close", self.on_close, "TButton", "close"))

    # ------------------------------------------------------------ done
    def _page_done(self, f):
        typeface.Label(f, text="Umbra is installed.", style="Head.TLabel").pack(anchor="w")
        self.l_done = self._text(f, "", wrap=780)
        self.l_done.pack(anchor="w", pady=(PAD, 0))
        self._text(f, "Umbra is made of other people's work. If you enjoy it, tell them: the credits list every "
                      "author with a link to their mod.", style="Muted.TLabel", wrap=780).pack(anchor="w",
                                                                                                pady=(PAD * 2, 0))

    def _enter_done(self):
        st = self.state or {}
        how = []
        launcher = st.get("launcher")
        does = ("press Play to start the game. The launcher also picks the season, checks for updates and repairs "
                "Umbra, and opens Mod Organizer 2 if you want it.")
        menu = [p for p in st.get("shortcuts") or [] if os.path.exists(p)]
        where = ("%s or the desktop" % shortcuts.where_words()) if menu else ""
        first = (" The first Play takes a little longer: the launcher sets up Proton for Umbra."
                 if os.name != "nt" else "")
        if st.get("steam_appid") and launcher:
            how.append('Start "Umbra" from your Steam library%s. It opens the Umbra launcher: '
                       % ((", or from " + where) if where else "") + does + first)
        elif launcher and menu:
            how.append("Start Umbra from %s. It opens the Umbra launcher: %s%s" % (where, does, first))
        elif st.get("steam_appid"):
            how.append('Start "Umbra" from your Steam library. It opens Mod Organizer 2 with the Umbra profile; '
                       'press Run there to play.')
        elif launcher and os.name == "nt":
            how.append("Start the Umbra launcher (%s): %s" % (launcher, does))
        else:
            how.append("Start Mod Organizer 2 (%s). It opens with the Umbra profile; press Run to play."
                       % os.path.join(st.get("mo2", ""), "ModOrganizer.exe"))
        for pr in packdef.chosen_profiles(self.pack, set(st.get("choices", [])))[1:] if self.pack else []:
            run = (pr.get("steam") or {}).get("run") or "Run"
            if pr["id"] in st.get("steam_shortcuts", {}):
                how.append('For %s, start "%s" from Steam.' % (pr["name"], pr["steam"]["title"]))
            else:
                how.append('For %s, pick the "%s" profile in Mod Organizer 2 and run "%s".' % (pr["name"], pr["name"],
                                                                                          run))
        how.append("Your G.A.M.M.A. profile is still there, unchanged, if you want to switch back.")
        self.l_done.configure(text="\n\n".join(how))
        self._buttons(("left", "Credits", self.show_credits, "TButton", "credits"),
                      ("left", "Open folder", lambda: _open(st.get("mo2", "")), "TButton", "open"),
                      ("right", "Close", self.on_close, "Go.TButton", "close"))

    # ------------------------------------------------------------ credits
    def show_credits(self):
        credits_window(self.root, self.pack)

    # ------------------------------------------------------------ plumbing
    def save_log(self):
        p = filedialog.asksaveasfilename(title="Save the log", defaultextension=".txt",
                                         initialfile="umbra-setup-log.txt")
        if p:
            with open(p, "w", encoding="utf-8") as f:
                for level, msg in self.log_lines:
                    f.write("%s: %s\n" % (level, msg))

    def _pump(self):
        items = []
        try:
            while True:
                items.append(self.q.get_nowait())
        except queue.Empty:
            pass
        # downloads report every chunk: of a run of one step's progress, only the last counts
        items = [it for i, it in enumerate(items)
                 if not (it[0] == "progress" and it[1][1] < 1.0 and i + 1 < len(items)
                         and items[i + 1][0] == "progress" and items[i + 1][1][0] == it[1][0])]
        for kind, arg in items:
            if kind == "pack":
                self.on_pack(arg)
                if self.page == "welcome":
                    self.b_next.state(["!disabled"])
            elif kind == "pack_error":
                self.l_pack.configure(text="Could not read the Umbra pack: %s" % arg, style="Bad.TLabel")
            else:
                getattr(self, "on_" + kind)(arg)
        if not self.closing:
            self.root.after(100, self._pump)

    def on_close(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.worker.join(timeout=10)
        self.closing = True
        self.root.destroy()


def _open(path):
    if not path:
        return
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def main(source=None, channel="stable", smoke=None, gamma_cache=None):
    root = tk.Tk()
    app = App(root, source, channel, gamma_cache)
    if smoke:
        def walk():
            app.show("where")
            root.after(int(smoke * 300), lambda: app.show_credits())
        root.after(int(smoke * 300), walk)
        root.after(int(smoke * 1000), app.on_close)
    root.mainloop()
    return 0
