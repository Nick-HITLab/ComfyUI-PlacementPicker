"""Streaming video editor nodes (low memory).

The edit nodes only *describe* an edit (a small dict). "Render edited video" then streams the video
frame by frame from disk to disk (PyAV), applies the edits and copies the original audio, so memory
use stays a few hundred MB even for long 1080p / 4K videos.
"""
import os
from fractions import Fraction

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import folder_paths

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "fonts", "DejaVuSans-Bold.ttf")
EDITS = "VIDEO_EDITS"


# ----------------------------------------------------------------------------- helpers
def _src(video):
    """Path (or file object) behind a ComfyUI VIDEO + its trim window."""
    s = video.get_stream_source() if hasattr(video, "get_stream_source") else video
    return s


def _open_video(video):
    return av.open(_src(video), mode="r")


def _to_uint8(img):
    """ComfyUI IMAGE [B,H,W,C] float 0-1 -> uint8 numpy (no batch copy kept)."""
    a = img.detach().cpu().numpy() if hasattr(img, "detach") else np.asarray(img)
    return (np.clip(a, 0, 1) * 255.0 + 0.5).astype(np.uint8)


def _resize(arr, w, h):
    return np.array(Image.fromarray(arr).resize((max(int(w), 1), max(int(h), 1)), Image.LANCZOS))


def _cover(arr, W, H):
    h, w = arr.shape[:2]
    s = max(W / w, H / h)
    r = _resize(arr, round(w * s), round(h * s))
    y0, x0 = (r.shape[0] - H) // 2, (r.shape[1] - W) // 2
    return r[y0:y0 + H, x0:x0 + W]


def _paste(frame, rgb, x, y, alpha=None):
    """Paste rgb (h,w,3) with optional alpha (h,w) 0-1 onto frame in place, clipped to the frame."""
    H, W = frame.shape[:2]
    h, w = rgb.shape[:2]
    x0, y0 = max(int(x), 0), max(int(y), 0)
    x1, y1 = min(int(x) + w, W), min(int(y) + h, H)
    if x1 <= x0 or y1 <= y0:
        return frame
    sx, sy = x0 - int(x), y0 - int(y)
    src = rgb[sy:sy + (y1 - y0), sx:sx + (x1 - x0)]
    if alpha is None:
        frame[y0:y1, x0:x1] = src
    else:
        a = alpha[sy:sy + (y1 - y0), sx:sx + (x1 - x0), None]
        frame[y0:y1, x0:x1] = (src * a + frame[y0:y1, x0:x1] * (1 - a)).astype(np.uint8)
    return frame


class _ClipReader:
    """Gives the frame of a (looping) clip at a given local time, decoding sequentially."""

    def __init__(self, video):
        self.video = video
        self._open()

    def _open(self):
        self.c = _open_video(self.video)
        self.s = self.c.streams.video[0]
        self.it = self.c.decode(self.s)
        self.cur, self.cur_t, self.loop_off = None, -1.0, 0.0
        self.dur = None

    def _next(self):
        try:
            f = next(self.it)
        except StopIteration:
            self.dur = self.cur_t + (1.0 / float(self.s.average_rate or 25))
            self.loop_off += self.dur
            self.c.close()
            self.c = _open_video(self.video)
            self.s = self.c.streams.video[0]
            self.it = self.c.decode(self.s)
            f = next(self.it)
        t = float(f.pts * f.time_base) if f.pts is not None else self.cur_t + 1.0 / float(self.s.average_rate or 25)
        return f, t

    def at(self, t):
        while self.cur is None or self.loop_off + self.cur_t < t - 1e-6:
            f, ft = self._next()
            self.cur, self.cur_t = f.to_ndarray(format="rgb24"), ft
        return self.cur

    def close(self):
        try:
            self.c.close()
        except Exception:
            pass


def _text_rgba(text, W, H, size_pct, color, outline, align):
    px = max(int(H * size_pct / 100.0), 8)
    font = ImageFont.truetype(FONT, px) if os.path.exists(FONT) else ImageFont.load_default()
    stroke = max(px // 14, 1) if outline else 0
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = d.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke, align=align)
    w, h = box[2] - box[0] + 2 * stroke + 4, box[3] - box[1] + 2 * stroke + 4
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).multiline_text((stroke + 2 - box[0], stroke + 2 - box[1]), text, font=font, fill=color,
                                      stroke_width=stroke, stroke_fill="black", align=align)
    a = np.asarray(im)
    return a[..., :3].copy(), a[..., 3].astype(np.float32) / 255.0


# ----------------------------------------------------------------------------- nodes
class VideoInfo:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"video": ("VIDEO",)}}

    RETURN_TYPES = ("FLOAT", "INT", "INT", "FLOAT", "INT")
    RETURN_NAMES = ("fps", "width", "height", "duration", "frame_count")
    FUNCTION = "info"
    CATEGORY = "video/editor"
    DESCRIPTION = "Frame rate, size and length of a video without loading its frames."

    def info(self, video):
        w, h = video.get_dimensions()
        return (float(video.get_frame_rate()), int(w), int(h), float(video.get_duration()), int(video.get_frame_count()))


def _base(extra_required=None, extra_optional=None):
    req = {"on": ("BOOLEAN", {"default": False}),
           "start": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100000.0, "step": 0.1}),
           "end": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100000.0, "step": 0.1})}
    req.update(extra_required or {})
    opt = {"edits": (EDITS,)}
    opt.update(extra_optional or {})
    return {"required": req, "optional": opt}


class _Edit:
    RETURN_TYPES = (EDITS,)
    RETURN_NAMES = ("edits",)
    FUNCTION = "add"
    CATEGORY = "video/editor"

    @staticmethod
    def _chain(edits, item, on):
        out = list(edits or [])
        if on:
            out.append(item)
        return (out,)


class EditAISplice(_Edit):
    DESCRIPTION = "Puts regenerated frames (from the AI edit) back into the video with soft crossfades."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"on": ("BOOLEAN", {"default": False}),
                             "start_frame": ("INT", {"default": 0, "min": 0, "max": 10 ** 7}),
                             "margin": ("INT", {"default": 8, "min": 0, "max": 64}),
                             "crossfade": ("INT", {"default": 8, "min": 0, "max": 64})},
                "optional": {"edits": (EDITS,), "frames": ("IMAGE", {"lazy": True})}}

    def check_lazy_status(self, on, start_frame, margin, crossfade, edits=None, frames=None):
        return ["frames"] if on and frames is None else []

    def add(self, on, start_frame, margin, crossfade, edits=None, frames=None):
        item = None
        if on and frames is not None:
            item = {"type": "ai", "start_frame": int(start_frame), "frames": _to_uint8(frames),
                    "margin": int(margin), "crossfade": int(crossfade)}
        return self._chain(edits, item, item is not None)


class EditCutaway(_Edit):
    DESCRIPTION = "Shows your own clip or image full screen between start and end; the voice keeps playing."

    @classmethod
    def INPUT_TYPES(cls):
        return _base({"use_image": ("BOOLEAN", {"default": False})},
                     {"clip": ("VIDEO",), "image": ("IMAGE", {"lazy": True})})

    def check_lazy_status(self, on, start, end, use_image, edits=None, clip=None, image=None):
        return ["image"] if on and use_image and image is None else []

    def add(self, on, start, end, use_image, edits=None, clip=None, image=None):
        item = {"type": "cutaway", "start": start, "end": end}
        if use_image and image is not None:
            item["image"] = _to_uint8(image[:1])[0]
        elif clip is not None:
            item["clip"] = clip
        else:
            on = False
        return self._chain(edits, item, on)


class EditVideoBox(_Edit):
    DESCRIPTION = "Picture-in-picture: plays a clip in a box at x / y / width (loops if shorter)."

    @classmethod
    def INPUT_TYPES(cls):
        return _base({"x": ("INT", {"default": 60, "min": -16384, "max": 16384}),
                      "y": ("INT", {"default": 60, "min": -16384, "max": 16384}),
                      "width": ("INT", {"default": 560, "min": 8, "max": 16384}),
                      "border": ("INT", {"default": 4, "min": 0, "max": 200}),
                      "border_color": ("STRING", {"default": "#ffffff"})},
                     {"clip": ("VIDEO",)})

    def add(self, on, start, end, x, y, width, border, border_color, edits=None, clip=None):
        item = {"type": "box", "start": start, "end": end, "x": x, "y": y, "width": width, "border": border,
                "border_color": border_color, "clip": clip}
        return self._chain(edits, item, on and clip is not None)


class EditImageOverlay(_Edit):
    DESCRIPTION = "Puts a picture / logo (PNG with transparency) on the video at x / y / width."

    @classmethod
    def INPUT_TYPES(cls):
        return _base({"x": ("INT", {"default": 1680, "min": -16384, "max": 16384}),
                      "y": ("INT", {"default": 40, "min": -16384, "max": 16384}),
                      "width": ("INT", {"default": 180, "min": 8, "max": 16384})},
                     {"image": ("IMAGE",), "mask": ("MASK",)})

    def add(self, on, start, end, x, y, width, edits=None, image=None, mask=None):
        if image is None:
            return self._chain(edits, None, False)
        rgb = _to_uint8(image[:1])[0]
        h, w = rgb.shape[:2]
        alpha = np.ones((h, w), np.float32)
        if mask is not None:
            m = mask[0].detach().cpu().numpy() if hasattr(mask, "detach") else np.asarray(mask[0])
            if m.shape == (h, w):
                alpha = 1.0 - m.astype(np.float32)  # LoadImage mask = 1 - alpha
        nh = max(int(round(h * width / w)), 1)
        rgb_s = _resize(rgb, width, nh)
        a_s = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).resize((width, nh), Image.LANCZOS)).astype(np.float32) / 255.0
        item = {"type": "image", "start": start, "end": end, "x": x, "y": y, "rgb": rgb_s, "alpha": a_s}
        return self._chain(edits, item, on)


class EditText(_Edit):
    DESCRIPTION = "Text / title / caption between start and end."

    @classmethod
    def INPUT_TYPES(cls):
        return _base({"text": ("STRING", {"default": "Your text here", "multiline": True}),
                      "font_size": ("FLOAT", {"default": 5.0, "min": 1.0, "max": 30.0, "step": 0.5,
                                              "tooltip": "Percent of the video height."}),
                      "color": ("STRING", {"default": "#ffffff"}),
                      "position": (["bottom", "top", "center"],),
                      "align": (["center", "left", "right"],),
                      "outline": ("BOOLEAN", {"default": True})})

    def add(self, on, start, end, text, font_size, color, position, align, outline, edits=None):
        item = {"type": "text", "start": start, "end": end, "text": text, "font_size": font_size, "color": color,
                "position": position, "align": align, "outline": outline}
        return self._chain(edits, item, on and bool(text.strip()))


class RenderEditedVideo:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"video": ("VIDEO",),
                             "filename_prefix": ("STRING", {"default": "avatar/EDITED"}),
                             "crf": ("INT", {"default": 14, "min": 0, "max": 51,
                                             "tooltip": "Quality: lower = better / bigger file. 12-18 is very good."})},
                "optional": {"edits": (EDITS,)}}

    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("video",)
    OUTPUT_NODE = True
    FUNCTION = "render"
    CATEGORY = "video/editor"
    DESCRIPTION = "Streams the video frame by frame, applies all edits in order and keeps the original audio (low memory)."

    def render(self, video, filename_prefix, crf, edits=None):
        edits = list(edits or [])
        out_dir = folder_paths.get_output_directory()
        full_dir, fname, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, out_dir)
        os.makedirs(full_dir, exist_ok=True)
        out_name = f"{fname}_{counter:05}_.mp4"
        out_path = os.path.join(full_dir, out_name)

        inp = _open_video(video)
        vin = inp.streams.video[0]
        ain = inp.streams.audio[0] if inp.streams.audio else None
        rate = vin.average_rate or Fraction(24)
        fps = float(rate)
        W, H = vin.codec_context.width, vin.codec_context.height

        out = av.open(out_path, mode="w")
        vout = out.add_stream("libx264", rate=rate)
        vout.width, vout.height, vout.pix_fmt = W, H, "yuv420p"
        vout.options = {"crf": str(int(crf)), "preset": "medium"}
        aout = None
        if ain is not None:
            aout = out.add_stream("aac", rate=ain.codec_context.sample_rate or 48000)
            aout.bit_rate = 192000

        # prepare edits
        readers = {}
        for k, e in enumerate(edits):
            if e["type"] in ("cutaway", "box") and e.get("clip") is not None:
                readers[k] = _ClipReader(e["clip"])
            if e["type"] == "cutaway" and e.get("image") is not None:
                e["_img"] = _cover(e["image"], W, H)
            if e["type"] == "text":
                rgb, a = _text_rgba(e["text"], W, H, e["font_size"], e["color"], e["outline"], e["align"])
                h, w = rgb.shape[:2]
                ax = {"left": int(W * 0.05), "center": (W - w) // 2, "right": W - w - int(W * 0.05)}[e["align"]]
                ay = {"top": int(H * 0.06), "center": (H - h) // 2, "bottom": H - h - int(H * 0.08)}[e["position"]]
                e["_rgb"], e["_a"], e["_xy"] = rgb, a, (ax, ay)

        def active(e, t):
            return e["start"] <= t < e["end"]

        def process(i, frame):
            t = i / fps
            for k, e in enumerate(edits):
                ty = e["type"]
                if ty == "ai":
                    s, fr, m, cf = e["start_frame"], e["frames"], e["margin"], e["crossfade"]
                    j = i - s
                    n = len(fr)
                    if m <= j < n - m:
                        new = fr[j]
                        if new.shape[:2] != (H, W):
                            new = _resize(new, W, H)
                        if j < m + cf:
                            a = (j - m + 1) / (cf + 1)
                        elif j >= n - m - cf:
                            a = (n - m - j) / (cf + 1)
                        else:
                            a = 1.0
                        frame = new.copy() if a >= 1.0 else (new * a + frame * (1 - a)).astype(np.uint8)
                elif not active(e, t):
                    continue
                elif ty == "cutaway":
                    frame = e["_img"].copy() if "_img" in e else _cover(readers[k].at(t - e["start"]), W, H)
                elif ty == "box":
                    clip = readers[k].at(t - e["start"])
                    ch, cw = clip.shape[:2]
                    bw = int(e["width"]); bh = max(int(round(ch * bw / cw)), 1)
                    b = int(e["border"])
                    if b > 0:
                        col = Image.new("RGB", (1, 1), e["border_color"]).getpixel((0, 0))
                        frame[max(e["y"], 0):max(e["y"] + bh + 2 * b, 0), max(e["x"], 0):max(e["x"] + bw + 2 * b, 0)] = col
                    frame = _paste(frame, _resize(clip, bw, bh), e["x"] + b, e["y"] + b)
                elif ty == "image":
                    frame = _paste(frame, e["rgb"], e["x"], e["y"], e["alpha"])
                elif ty == "text":
                    frame = _paste(frame, e["_rgb"], e["_xy"][0], e["_xy"][1], e["_a"])
            return frame

        i = 0
        streams = [vin] + ([ain] if ain is not None else [])
        for pkt in inp.demux(*streams):
            if pkt.stream.type == "video":
                for f in pkt.decode():
                    arr = np.array(f.to_ndarray(format="rgb24"))
                    arr = np.ascontiguousarray(process(i, arr))
                    vf = av.VideoFrame.from_ndarray(arr, format="rgb24")
                    vf.pts = i
                    vf.time_base = Fraction(1, 1) / rate
                    for p in vout.encode(vf):
                        out.mux(p)
                    i += 1
            elif aout is not None:
                for af in pkt.decode():
                    af.pts = None
                    for p in aout.encode(af):
                        out.mux(p)
        for p in vout.encode():
            out.mux(p)
        if aout is not None:
            for p in aout.encode():
                out.mux(p)
        out.close()
        inp.close()
        for r in readers.values():
            r.close()

        try:
            from comfy_api.latest import InputImpl
            result = InputImpl.VideoFromFile(out_path)
        except Exception:
            result = None
        return {"ui": {"images": [{"filename": out_name, "subfolder": subfolder, "type": "output"}], "animated": (True,)},
                "result": (result,)}



class DetectCuts:
    """Finds hard jump cuts (a single frame where the picture suddenly changes) so they can be repaired."""
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"video": ("VIDEO",),
                             "enabled": ("BOOLEAN", {"default": True}),
                             "sensitivity": ("FLOAT", {"default": 6.0, "min": 3.0, "max": 20.0, "step": 0.5,
                                                       "tooltip": "A jump must be this many times bigger than a normal frame change. Lower = finds more."}),
                             "half_window": ("FLOAT", {"default": 1.2, "min": 0.5, "max": 3.0, "step": 0.1,
                                                       "tooltip": "Seconds regenerated before and after each jump."})}}

    RETURN_TYPES = ("FLOAT", "FLOAT", "BOOLEAN", "FLOAT", "FLOAT", "BOOLEAN", "FLOAT", "FLOAT", "BOOLEAN", "INT", "STRING")
    RETURN_NAMES = ("cut1_start", "cut1_end", "cut1_on", "cut2_start", "cut2_end", "cut2_on", "cut3_start", "cut3_end", "cut3_on", "count", "report")
    FUNCTION = "detect"
    CATEGORY = "video/editor"
    OUTPUT_NODE = True
    DESCRIPTION = "Finds up to 3 hard jump cuts in the video (low memory) and gives the time window to regenerate around each one."

    def detect(self, video, enabled, sensitivity, half_window):
        inp = _open_video(video)
        vin = inp.streams.video[0]
        fps = float(vin.average_rate or 24)
        dur = float(video.get_duration()) if hasattr(video, "get_duration") else 0.0
        diffs, prev = [], None
        if enabled:
            for f in inp.decode(vin):
                g = f.reformat(width=160, height=90, format="gray").to_ndarray().astype(np.float32)
                if prev is not None:
                    diffs.append(float(np.abs(g - prev).mean()))
                prev = g
        inp.close()
        found = []
        if diffs:
            d = np.array(diffs); med = float(np.median(d)) + 1e-6
            cands = []
            for i in range(len(d)):
                nb = [d[j] for j in (i - 2, i - 1, i + 1, i + 2) if 0 <= j < len(d)]
                if d[i] / med > sensitivity and d[i] >= 2.0 * max(nb):
                    cands.append((d[i] / med, (i + 1) / fps))
            for strength, t in sorted(cands, reverse=True):
                if all(abs(t - u) > 2 * half_window + 0.5 for _, u in found):
                    found.append((strength, t))
            found = sorted(found, key=lambda x: x[1])[:3] if len(found) <= 3 else sorted(sorted(found, reverse=True)[:3], key=lambda x: x[1])
        out, lines = [], []
        for k in range(3):
            if k < len(found):
                strength, t = found[k]
                a, b = max(t - half_window, 0.0), t + half_window
                if dur:
                    b = min(b, dur)
                out += [a, b, True]
                lines.append(f"jump {k + 1} at {t:.2f} s ({strength:.0f}x a normal frame change) -> repair {a:.1f}-{b:.1f} s")
            else:
                out += [0.0, 2.0, False]
        report = ("Cut repair is OFF." if not enabled else
                  ("No hard jumps found." if not found else "\n".join(lines)))
        return {"ui": {"text": [report]}, "result": tuple(out + [len(found), report])}


NODE_CLASS_MAPPINGS = {
    "PSYDetectCuts": DetectCuts,
    "PSYVideoInfo": VideoInfo,
    "PSYEditAISplice": EditAISplice,
    "PSYEditCutaway": EditCutaway,
    "PSYEditVideoBox": EditVideoBox,
    "PSYEditImageOverlay": EditImageOverlay,
    "PSYEditText": EditText,
    "PSYRenderEditedVideo": RenderEditedVideo,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "PSYDetectCuts": "Find jump cuts (for automatic repair)",
    "PSYVideoInfo": "Video info (fps, size, length)",
    "PSYEditAISplice": "Edit A: put AI frames back",
    "PSYEditCutaway": "Edit B: cut-away",
    "PSYEditVideoBox": "Edit E: video box",
    "PSYEditImageOverlay": "Edit C: picture / logo",
    "PSYEditText": "Edit D: text",
    "PSYRenderEditedVideo": "Render edited video (streaming, low memory)",
}
