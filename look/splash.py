"""
Umbra's splashes, replacing G.A.M.M.A.'s: the loading screen, the main-menu
logo and the small menu badge.

    python3 splash.py            build into out/full and out/preview

Everything is painted the way the logo is (../branding/weather.py): flat paint
on steel, worn through to rust, shaded by grime, all taken from the base game's
own textures. No noise generators, no gradients-for-the-sake-of-it.

The loading screen's layout is fixed in the engine (monolith,
src/Layers/xrRender/dxApplicationRender.cpp, load_draw_internal):
  - ui\\ui_actor_loadgame_screen: (0,0)-(1024,768) is the 4:3 background,
    drawn centred (1440x1080 on a 1080p screen); (0,772)-(506,776) is the
    progress bar strip, drawn at (260,599) and faded in by vertex alpha.
  - ui\\ui_actor_widescreen_sidepanels: (0,0)-(128,768) left and
    (128,0)-(256,768) right, filling the rest of a 16:9 or 16:10 screen.
  - The level picture (textures\\intro\\intro_<level>_N) covers virtual
    (0,173)-(1024,572); the loading header and tips are text from y 622 down,
    in grey 103.
UVs are divided by a fixed 1024, so both textures can be 2048 or 4096 as long
as the layout scales with them. We design one 16:9 plate in screen pixels and
cut it into the two textures.
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "branding"))
sys.path.insert(0, HERE)

import weather  # noqa: E402  (branding/weather.py)
import dds      # noqa: E402
import paint    # noqa: E402
import vfs      # noqa: E402

OUT = os.path.join(HERE, "out")
MOD = os.path.join(OUT, "full")         # whole files; layer.py makes the Umbra mod's part
PREVIEW = os.path.join(OUT, "preview")
LOGO = os.path.join(REPO, "branding", "png", "umbra-logo-2400.png")
ICON = os.path.join(REPO, "branding", "png", "umbra-icon-512.png")

# Palette (branding/README.md), plus the plate's paint
BONE = "#d9cca6"
UMBER = "#5e4532"
SOOT = "#14130f"
PLATE = "#2a2a22"       # olive-black enamel, the SoC-era UI's dark steel
RECESS = "#1b1a16"

S = 2                   # supersample: author at 2x 1080p (3840x2160)
SW, SH = 1920 * S, 1080 * S


# ---------------------------------------------------------------- geometry helpers
def rect_mask(W, H, box, radius=0):
    im = Image.new("L", (W, H), 0)
    ImageDraw.Draw(im).rounded_rectangle(box, radius=radius, fill=255)
    return np.asarray(im, np.float32) / 255.0


def circle_mask(W, H, centres, r):
    im = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(im)
    for x, y in centres:
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    return np.asarray(im, np.float32) / 255.0


def relief(height, light=(-0.6, -0.8), strength=1.0):
    """Lambert-ish shading of a height field lit from the top left."""
    gy, gx = np.gradient(height)
    return 1.0 + strength * (gx * light[0] + gy * light[1])


def over(dst_rgb, src_rgb, a):
    a = a[..., None]
    return src_rgb * a + dst_rgb * (1 - a)


def paste_rgba(dst_rgb, img, x, y):
    """Composite a straight-alpha RGBA PIL image onto a float RGB canvas."""
    a = np.asarray(img, np.float32) / 255.0
    h, w = a.shape[:2]
    H, W = dst_rgb.shape[:2]
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
    if x1 <= x0 or y1 <= y0:
        return dst_rgb
    sub = a[y0 - y:y1 - y, x0 - x:x1 - x]
    dst_rgb[y0:y1, x0:x1] = over(dst_rgb[y0:y1, x0:x1], sub[..., :3], sub[..., 3])
    return dst_rgb


# ---------------------------------------------------------------- the loading screen
# Screen-space layout on a 1920x1080 16:9 screen (see the module docstring).
PIC = (240, 243, 1680, 804)          # where the level picture is drawn
BAR = (606, 842, 1318, 848)          # the progress bar
TEXT_Y = 875                         # the header line; tips follow


def loading_plate(seed=11):
    """The whole 16:9 loading screen without the picture: float RGB, SHxSWx3."""
    W, H = SW, SH
    s = S
    full = np.ones((H, W), np.float32)

    # base sheet: dark enamel over steel, worn a little, mostly at the edges
    rgb = paint.sheet(full, PLATE, px=1.2 * s, cover=0.035, chip=0.7, edge=14, streaks=1.0,
                      rust_tone=0.55, flakes=0.15, seed=seed)

    # the window: recessed, with a raised rim around it
    px0, py0, px1, py1 = [v * s for v in PIC]
    rim = 18 * s
    win_outer = rect_mask(W, H, (px0 - rim, py0 - rim, px1 + rim, py1 + rim), radius=34 * s)
    win_inner = rect_mask(W, H, (px0 + 6 * s, py0 + 6 * s, px1 - 6 * s, py1 - 6 * s), radius=26 * s)
    rim_mask = np.clip(win_outer - win_inner, 0, 1)
    rim_rgb = paint.sheet(rim_mask, PLATE, px=1.2 * s, cover=0.16, chip=0.5, edge=3, edge_bias=1.6,
                          rust_tone=0.55, flakes=0.0, seed=seed + 1)
    rgb = over(rgb, rim_rgb, rim_mask)

    recess_rgb = paint.sheet(win_inner, RECESS, px=1.2 * s, cover=0.02, chip=0.6, rust_tone=0.4,
                             flakes=0.0, seed=seed + 2)
    rgb = over(rgb, recess_rgb, win_inner)

    # a faint Umbra mark stencilled in the recess (seen when there's no picture)
    icon = Image.open(ICON).convert("RGBA")
    ih = int(300 * s)
    icon = icon.resize((ih, ih), Image.LANCZOS)
    ia = np.asarray(icon, np.float32) / 255.0
    ia[..., 3] *= 0.10
    mark = Image.fromarray(np.round(ia * 255).astype(np.uint8), "RGBA")
    rgb = paste_rgba(rgb, mark, (W - ih) // 2, (py0 + py1 - ih) // 2)

    # the progress bar's slot
    bx0, by0, bx1, by1 = [v * s for v in BAR]
    slot = rect_mask(W, H, (bx0 - 7 * s, by0 - 6 * s, bx1 + 7 * s, by1 + 6 * s), radius=4 * s)
    rgb = over(rgb, rgb * 0.45, slot * 0.9)

    # where the text goes: a darker stamped panel so grey text reads
    tp = rect_mask(W, H, (360 * s, 860 * s, 1560 * s, 1046 * s), radius=10 * s)
    tp_rgb = paint.sheet(tp, "#1e1d18", px=1.2 * s, cover=0.04, chip=0.5, edge=3, edge_bias=1.5,
                         rust_tone=0.45, flakes=0.0, seed=seed + 3)
    rgb = over(rgb, tp_rgb, tp)

    # rivets along the rim and the plate edges
    rv = []
    for x in np.linspace(px0 + 60 * s, px1 - 60 * s, 9):
        rv += [(x, py0 - rim / 2), (x, py1 + rim / 2)]
    for y in (py0 + 70 * s, (py0 + py1) / 2, py1 - 70 * s):
        rv += [(px0 - rim / 2, y), (px1 + rim / 2, y)]
    for x in np.linspace(40 * s, W - 40 * s, 13):
        rv += [(x, 26 * s), (x, H - 26 * s)]
    rivets = circle_mask(W, H, rv, 5 * s)

    # relief: rim up, recess and slot down, text panel slightly down, rivets up
    height = (weather.blur(rim_mask, 2.0 * s) * 6.0
              - weather.blur(win_inner, 1.5 * s) * 3.0
              - weather.blur(slot, 1.0 * s) * 2.5
              - weather.blur(tp, 1.5 * s) * 1.5
              + weather.blur(rivets, 2.2 * s) * 5.0)
    rgb = rgb * np.clip(paint.relief(height, strength=0.9), 0.55, 1.6)[..., None]

    # the logo, painted on the plate above the window
    logo = Image.open(LOGO).convert("RGBA")
    lh = int(176 * s)
    lw = int(logo.width * lh / logo.height)
    logo = logo.resize((lw, lh), Image.LANCZOS)
    rgb = paste_rgba(rgb, logo, (W - lw) // 2, int(34 * s))

    # light falls off toward the corners, as on a lit panel (not a vignette filter:
    # a broad, faint falloff only)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = ((xx - W / 2) / (W / 2)) ** 2 + ((yy - H * 0.45) / (H * 0.75)) ** 2
    rgb = rgb * (1.0 - 0.28 * np.clip(d, 0, 1.5))[..., None]
    return np.clip(rgb, 0, 1)


def progress_strip(w, h):
    """The bar's texture: a bone-white lamp strip, a touch warmer at its rims."""
    y = np.linspace(-1, 1, h)[:, None]
    core = np.clip(1.0 - 0.55 * y ** 2, 0, 1)
    bone = weather.hex_rgb(BONE)
    warm = weather.hex_rgb("#c08a52")
    rgb = bone[None, None, :] * core[..., None] + warm[None, None, :] * (1 - core[..., None])
    rgb = np.repeat(rgb, w, axis=1)
    return np.clip(rgb, 0, 1)


def cut_loading(plate, res=2048):
    """Cut the 16:9 plate into (atlas RGBA, side panels RGBA), both res x res."""
    k = res / 1024.0
    W = plate.shape[1]
    s = W / 1920.0
    img = Image.fromarray(np.round(plate * 255).astype(np.uint8), "RGB")
    # background: screen x 240..1680 -> atlas (0,0)-(1024,768)
    bg = img.crop((int(240 * s), 0, int(1680 * s), plate.shape[0])).resize((int(1024 * k), int(768 * k)), Image.LANCZOS)
    atlas = Image.new("RGBA", (res, res), (0, 0, 0, 0))
    atlas.paste(bg, (0, 0))
    strip = progress_strip(int(506 * k), int(4 * k))
    sim = Image.fromarray(np.round(strip * 255).astype(np.uint8), "RGB")
    atlas.paste(sim, (0, int(772 * k)))
    # side panels: screen x 0..240 and 1680..1920 -> (0..128) and (128..256)
    side = Image.new("RGBA", (res, res), (0, 0, 0, 0))
    left = img.crop((0, 0, int(240 * s), plate.shape[0])).resize((int(128 * k), int(768 * k)), Image.LANCZOS)
    right = img.crop((int(1680 * s), 0, W, plate.shape[0])).resize((int(128 * k), int(768 * k)), Image.LANCZOS)
    side.paste(left, (0, 0))
    side.paste(right, (int(128 * k), 0))
    return np.asarray(atlas), np.asarray(side)


def preview_loading(plate, picture=None, header=None, text=True):
    """How a 1080p screen would look: the plate, a level picture and the text
    (text=False leaves the text off, for fonts.py to draw with a game font)."""
    if header is None:
        header = loading_header()
    img = Image.fromarray(np.round(plate * 255).astype(np.uint8), "RGB").resize((1920, 1080), Image.LANCZOS)
    if picture is not None:
        pic = Image.fromarray(picture, "RGBA")
        pic = pic.crop((0, 0, 2048, 798)).resize((PIC[2] - PIC[0], PIC[3] - PIC[1]), Image.LANCZOS)
        img.paste(pic, (PIC[0], PIC[1]), pic)
    # the bar, 60% loaded
    strip = progress_strip(BAR[2] - BAR[0], BAR[3] - BAR[1])
    n = int(0.6 * strip.shape[1])
    bar = Image.fromarray(np.round(strip[:, :n] * 255).astype(np.uint8), "RGB")
    img.paste(bar, (BAR[0], BAR[1]))
    if not text:
        return img
    d = ImageDraw.Draw(img)
    grey = (103, 103, 103)
    d.text((960, TEXT_Y), header, fill=grey, anchor="ma")
    d.text((960, TEXT_Y + 34), "Manual, Lore, and Stories #12", fill=grey, anchor="ma")
    d.text((960, TEXT_Y + 60), "Guns must be repaired to progress since you can't buy them from traders.", fill=grey, anchor="ma")
    return img


def build_loading(picture_path=None):
    os.makedirs(PREVIEW, exist_ok=True)
    plate = loading_plate()
    atlas, side = cut_loading(plate)
    ui = os.path.join(MOD, "gamedata", "textures", "ui")
    os.makedirs(ui, exist_ok=True)
    dds.write_rgba(os.path.join(ui, "ui_actor_loadgame_screen.dds"), atlas)
    dds.write_rgba(os.path.join(ui, "ui_actor_widescreen_sidepanels.dds"), side)
    pic = dds.read(picture_path) if picture_path else None
    preview_loading(plate, pic).save(os.path.join(PREVIEW, "loading-1080p.png"))
    preview_loading(plate, None).save(os.path.join(PREVIEW, "loading-1080p-nopicture.png"))




# ---------------------------------------------------------------- the main menu
# ui_mm_main_16.xml (G.A.M.M.A. UI) draws, in 1024x768 virtual units stretched
# to the screen:
#   GAMMAMainMenuLogo.dds (2048x1024) at (185,2) 650x375
#   LogoGAMMA.dds (640x640, G.A.M.M.A.'s Discord QR code) at (180,666) 55x64
#   ui\ui_background_new (an Ogg Theora video, 1920x1080) over the whole screen
# On a 16:9 screen a virtual unit is 1.875 px wide and 1.406 px tall, so art
# is drawn squeezed sideways by (h_px / tex_h) / (w_px / tex_w) to look right.
def squeeze(tex_w, tex_h, box_w, box_h):
    return (box_h * 1.40625 / tex_h) / (box_w * 1.875 / tex_w)


def menu_logo():
    """GAMMAMainMenuLogo.dds: the weathered logo, centred, with a soft shadow."""
    W, H = 2048, 1024
    k = squeeze(W, H, 650, 375)                  # ~0.865
    logo = Image.open(LOGO).convert("RGBA")
    lh = int(H * 0.56)
    lw = int(logo.width * lh / logo.height * k)
    logo = logo.resize((lw, lh), Image.LANCZOS)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # a soft dark shadow so it reads over bright frames of the video
    a = np.asarray(logo, np.float32)[..., 3] / 255.0
    pad = np.zeros((H, W), np.float32)
    x, y = (W - lw) // 2, (H - lh) // 2
    pad[y + 10:y + 10 + lh, x + 6:x + 6 + lw] = a
    sh = weather.blur(pad, 22) * 0.55
    shadow = np.zeros((H, W, 4), np.float32)
    shadow[..., 3] = sh
    canvas = Image.fromarray(np.round(shadow * 255).astype(np.uint8), "RGBA")
    canvas.alpha_composite(logo, (x, y))
    return np.asarray(canvas)


def menu_badge():
    """LogoGAMMA.dds: the Umbra icon where G.A.M.M.A.'s QR code was."""
    W = H = 640
    k = squeeze(W, H, 55, 64)                    # ~0.87
    icon = Image.open(ICON).convert("RGBA")
    ih = 600
    iw = int(ih * k)
    icon = icon.resize((iw, ih), Image.LANCZOS)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.alpha_composite(icon, ((W - iw) // 2, (H - ih) // 2))
    return np.asarray(canvas)


MENU_SHOT = "textures/intro/intro_l09_deadcity_3.dds"   # placeholder: a CS Eden loading picture
FOG = "textures/pfx/pfx_blurry_cloud.dds"


def menu_still(v):
    """The placeholder's base frame: a loading picture, cropped to 16:9 and graded
    into Umbra's palette (a little warmer, darker, less saturated)."""
    a = dds.read(v.find(MENU_SHOT).read())[:798]
    # the picture's opaque part is x 28..2021, y 28..769; take a 16:9 window
    y0, y1 = 40, 760
    w = int((y1 - y0) * 16 / 9)
    x0 = 120
    img = Image.fromarray(a[y0:y1, x0:x0 + w, :3]).resize((1920, 1080), Image.LANCZOS)
    f = np.asarray(img, np.float32) / 255.0
    g = weather.lum(f)[..., None]
    f = f * 0.75 + g * 0.25                                   # less saturated
    f = f * np.array([1.04, 1.0, 0.92], np.float32)           # a touch warmer
    f = np.clip(f, 0, 1) ** 1.12 * 0.86                       # darker
    return f


def fog_sprites(v, n=11, seed=5):
    a = dds.read(v.find(FOG).read()).astype(np.float32) / 255.0
    rs = np.random.RandomState(seed)
    sprites = []
    for i in range(n):
        size = int(rs.uniform(520, 980))
        im = Image.fromarray(np.round(a * 255).astype(np.uint8), "RGBA").resize((int(size * 1.9), size), Image.LANCZOS)
        if rs.rand() < 0.5:
            im = im.transpose(Image.FLIP_LEFT_RIGHT)
        sprites.append(dict(img=np.asarray(im, np.float32) / 255.0,
                            y=rs.uniform(380, 1000) - size / 2,
                            x0=rs.uniform(0, 1),
                            laps=int(rs.choice([1, 1, 2])),     # whole laps per loop: seamless
                            alpha=rs.uniform(0.05, 0.11)))
    return sprites


def menu_frame(base, sprites, t):
    """One frame at loop phase t in [0, 1)."""
    f = base.copy()
    H, W = f.shape[:2]
    for sp in sprites:
        img = sp["img"]
        sh, sw = img.shape[:2]
        span = W + sw
        x = int(((sp["x0"] + t * sp["laps"]) % 1.0) * span) - sw
        y = int(sp["y"])
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + sw, W), min(y + sh, H)
        if x1 <= x0 or y1 <= y0:
            continue
        sub = img[y0 - y:y1 - y, x0 - x:x1 - x]
        al = sub[..., 3:4] * sp["alpha"]
        fogc = np.array([0.62, 0.6, 0.55], np.float32) * (0.6 + 0.4 * sub[..., :3].mean(-1, keepdims=True))
        f[y0:y1, x0:x1] = f[y0:y1, x0:x1] * (1 - al) + fogc * al
    return f


def build_menu_video(v, seconds=24, fps=30, out=None):
    import subprocess
    base = menu_still(v)
    sprites = fog_sprites(v)
    out = out or os.path.join(MOD, "gamedata", "textures", "ui", "ui_background_new.ogm")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    n = seconds * fps
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "1920x1080",
           "-r", str(fps), "-i", "-", "-c:v", "libtheora", "-q:v", "7", "-f", "ogg", out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        fr = menu_frame(base, sprites, i / n)
        p.stdin.write(np.round(np.clip(fr, 0, 1) * 255).astype(np.uint8).tobytes())
        if i == 0:
            Image.fromarray(np.round(np.clip(fr, 0, 1) * 255).astype(np.uint8)).save(os.path.join(PREVIEW, "menu-video-frame0.png"))
    p.stdin.close()
    if p.wait():
        raise SystemExit("ffmpeg failed")
    return base


def preview_menu(frame_rgb, logo, badge):
    """Roughly how the main menu looks at 1080p (buttons omitted)."""
    img = Image.fromarray(np.round(np.clip(frame_rgb, 0, 1) * 255).astype(np.uint8)).convert("RGBA")
    def place(arr, vx, vy, vw, vh):
        im = Image.fromarray(arr, "RGBA").resize((int(vw * 1.875), int(vh * 1.40625)), Image.LANCZOS)
        img.alpha_composite(im, (int(vx * 1.875), int(vy * 1.40625)))
    place(logo, 185, 2, 650, 375)
    place(badge, 180, 666, 55, 64)
    return img.convert("RGB")


# The main menu's highlight (the "shniaga" behind the selected button):
# ui_inGame2_slider in ui\ui_actor_main_menu, atlas pixels (5,773) 274x61
# (configs/ui/textures_descr/ui_actor_main_menu.xml), drawn stretched to
# 219x61 UI units = 410x86 px at 1080p (ui_mm_main_16.xml). G.A.M.M.A.'s is a
# green-lit tube with stone end caps.
SLIDER_RECT = (5, 773, 274, 61)
SLIDER_SCREEN = (round(219 * 1920 / 1024), round(61 * 1080 / 768))     # 411 x 86


def menu_slider(seed=21):
    """The highlight as an RGBA atlas cell: a strip of umber enamel (the logo's
    bar) set in a bolted, olive-black steel bracket. Painted at its on-screen
    size x4, so the engine's stretch gives back round rivets, then fitted to
    the atlas cell."""
    k = 4
    W, H = SLIDER_SCREEN[0] * k, SLIDER_SCREEN[1] * k
    cy = H / 2
    fx0, fx1, fh = 68 * k, W - 68 * k, 44 * k                # the steel bracket
    frame = rect_mask(W, H, (fx0, cy - fh / 2, fx1, cy + fh / 2), radius=7 * k)
    bx0, bx1, bh = fx0 + 20 * k, fx1 - 20 * k, 32 * k        # the enamel strip inside it
    band = rect_mask(W, H, (bx0, cy - bh / 2, bx1, cy + bh / 2), radius=3 * k)
    bolts = circle_mask(W, H, [(fx0 + 10 * k, cy), (fx1 - 10 * k, cy)], 4.2 * k)

    steel = paint.sheet(frame, PLATE, px=1.0 * k, cover=0.14, chip=0.45, edge=2.5, edge_bias=1.7,
                        rust_tone=0.55, flakes=0.0, seed=seed)
    enamel = paint.sheet(band, UMBER, px=1.0 * k, cover=0.07, chip=0.4, edge=2.0, edge_bias=1.8,
                         rust_tone=0.6, flakes=0.1, seed=seed + 1)
    rgb = over(steel, enamel, band)
    rgb = over(rgb, rgb * 0.0 + weather.hex_rgb("#3a382f")[None, None, :], bolts)
    height = (weather.blur(frame, 1.6 * k) * 4.0 - weather.blur(band, 1.0 * k) * 1.8
              + weather.blur(bolts, 1.4 * k) * 4.0)
    rgb = rgb * np.clip(paint.relief(height, strength=1.1), 0.5, 1.7)[..., None]

    # a soft shadow under the bracket, so it sits on the picture behind
    shadow = np.roll(weather.blur(frame, 3.0 * k), (3 * k, 2 * k), axis=(0, 1)) * 0.55
    alpha = np.maximum(frame, shadow)
    rgb = rgb * (frame / np.maximum(alpha, 1e-6))[..., None]          # shadow is black
    img = Image.fromarray(np.round(np.concatenate([rgb * alpha[..., None], alpha[..., None]], -1) * 255)
                          .astype(np.uint8), "RGBA")                  # premultiplied for resizing
    cw, ch = SLIDER_RECT[2], SLIDER_RECT[3]
    small = np.asarray(img.resize((cw, ch), Image.LANCZOS), np.float32) / 255.0
    a = small[..., 3:4]
    small[..., :3] = np.where(a > 0, small[..., :3] / np.maximum(a, 1e-6), 0)
    return np.round(np.clip(small, 0, 1) * 255).astype(np.uint8)


# G.A.M.M.A. UI's green lab picture, 1280x768, sits in two atlases:
#   ui_actor_main_menu_one (0,1024): the new-game faction screen
#     (ui_mm_faction_select*.xml) and xrRazom's co-op faction screen
#     (ui_mm_xrr_peer_faction.xml) draw it full-screen;
#   ui_actor_main_menu_one (0,0) and ui_actor_main_menu (0,0): debug screens,
#     and the unused ui_inGame2_background.
# Each also holds a copy of the green slider in the slider's cell.
BACKDROP_SIZE = (1280, 768)


def menu_backdrop(v):
    """Umbra's backdrop for those screens: the main menu's still, a bit darker
    (panels and text go over it), squeezed into 1280x768 so the engine's
    full-screen stretch gives back 16:9."""
    f = menu_still(v) * 0.85
    img = Image.fromarray(np.round(np.clip(f, 0, 1) * 255).astype(np.uint8), "RGB").resize(BACKDROP_SIZE, Image.LANCZOS)
    rgba = np.full((BACKDROP_SIZE[1], BACKDROP_SIZE[0], 4), 255, np.uint8)
    rgba[..., :3] = np.asarray(img)
    return rgba


def repaint_atlas(v, name, backdrops):
    """The winning atlas `name` with the slider's cell and the backdrop
    spots (top-left corners) repainted; everything else as it is."""
    atlas = dds.read(v.find("textures/ui/%s.dds" % name).read()).copy()
    x, y, w, h = SLIDER_RECT
    atlas[y:y + h, x:x + w] = menu_slider()
    if backdrops:
        bd = menu_backdrop(v)
        for bx, by in backdrops:
            atlas[by:by + bd.shape[0], bx:bx + bd.shape[1]] = bd
    return atlas


def menu_atlas(v):
    return repaint_atlas(v, "ui_actor_main_menu", [(0, 0)])


def menu_atlas_one(v):
    return repaint_atlas(v, "ui_actor_main_menu_one", [(0, 0), (0, 1024)])


def build_menu():
    v = vfs.VFS(prefix="textures/")
    tex = os.path.join(MOD, "gamedata", "textures")
    os.makedirs(os.path.join(tex, "ui"), exist_ok=True)
    logo, badge = menu_logo(), menu_badge()
    dds.write_rgba(os.path.join(tex, "GAMMAMainMenuLogo.dds"), logo)
    dds.write_rgba(os.path.join(tex, "LogoGAMMA.dds"), badge)
    dds.write_rgba(os.path.join(tex, "ui", "ui_actor_main_menu.dds"), menu_atlas(v))
    one = menu_atlas_one(v)
    dds.write_rgba(os.path.join(tex, "ui", "ui_actor_main_menu_one.dds"), one)
    # how the new-game screen's background will look (stretched full-screen)
    Image.fromarray(one[1024:1024 + 768, :1280, :3]).resize((1920, 1080), Image.LANCZOS).save(
        os.path.join(PREVIEW, "newgame-background-1080p.png"))
    base = menu_still(v)
    preview_menu(base, logo, badge).save(os.path.join(PREVIEW, "menu-1080p.png"))


# ---------------------------------------------------------------- strings
# The menu's version line and the loading screen's header say
# "S.T.A.L.K.E.R. G.A.M.M.A. 0.9.x". We copy the winning string files for
# every language and change only those two strings, so G.A.M.M.A.'s loading
# tips and translations stay exactly as they are.
STRING_FILES = {"_game_version.xml": "ui_st_game_version", "ui_st_loadscreen.xml": "ls_header"}


def pack_version():
    import tomllib
    with open(os.path.join(REPO, "pack", "umbra.toml"), "rb") as f:
        return tomllib.load(f)["pack"]["version"]


def umbra_title(old):
    import re
    m = re.search(r"G\.A\.M\.M\.A\.\s*([0-9][0-9.]*)", old)
    gamma = (" (G.A.M.M.A. %s)" % m.group(1)) if m else ""
    return "S.T.A.L.K.E.R. Umbra %s%s" % (pack_version(), gamma)


def patch_string(text, sid):
    """Replace the <text> of string id sid; returns (new text, old value)."""
    import re
    pat = re.compile(r'(<string\s+id="%s"\s*>\s*<text>)(.*?)(</text>)' % re.escape(sid), re.S)
    m = pat.search(text)
    if not m:
        return text, None
    new = umbra_title(m.group(2))
    return text[:m.start(2)] + new + text[m.end(2):], m.group(2)


def loading_header():
    """The loading screen's header as the game will show it (from the winning English file)."""
    v = vfs.VFS(prefix="configs/text/eng/", archives=True)
    src = v.find("configs/text/eng/ui_st_loadscreen.xml")
    _, old = patch_string(src.read().decode("cp1251"), "ls_header") if src else (None, None)
    return umbra_title(old or "")


def build_strings():
    v = vfs.VFS(prefix="configs/text/", archives=True)
    done = []
    for rel, src in sorted(v.winners("configs/text/").items()):
        name = rel.rsplit("/", 1)[-1]
        if name not in STRING_FILES:
            continue
        raw = src.read()
        enc = "cp1251"
        text = raw.decode(enc)
        new, old = patch_string(text, STRING_FILES[name])
        if old is None:
            continue
        out = os.path.join(MOD, "gamedata", rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(new.encode(enc))
        done.append((rel, src.label, old.strip(), umbra_title(old)))
    for rel, label, old, new in done:
        print("  %s (from %s): %r -> %r" % (rel, label, old, new))
    return done


if __name__ == "__main__":
    what = sys.argv[1:] or ["loading", "menu", "video", "strings"]
    if "loading" in what:
        _v = vfs.VFS(prefix="textures/intro/")
        build_loading(None)
        # preview with a real level picture
        os.makedirs(PREVIEW, exist_ok=True)
        preview_loading(loading_plate(), dds.read(_v.find("textures/intro/intro_l01_escape_1.dds").read())).save(
            os.path.join(PREVIEW, "loading-1080p.png"))
    if "menu" in what:
        build_menu()
    if "video" in what:     # the background loop (slow; the logo isn't in it)
        build_menu_video(vfs.VFS(prefix="textures/"))
    if "strings" in what:
        build_strings()
