"""Nodes for long LTX-2.5 avatar videos (minutes): eye anchors over any length + low-memory decode-and-save."""
import os
from fractions import Fraction

import av
import numpy as np
import torch

import folder_paths
import nodes as comfy_nodes


def _out(res):
    """io.NodeOutput or tuple -> tuple."""
    for attr in ("result", "args"):
        if hasattr(res, attr) and getattr(res, attr) is not None:
            return tuple(getattr(res, attr))
    return tuple(res)


class EyeAnchors:
    """Re-inserts the reference photo lightly every N seconds (LTXVAddGuide in a loop)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "positive": ("CONDITIONING",), "negative": ("CONDITIONING",), "vae": ("VAE",), "latent": ("LATENT",),
            "image": ("IMAGE",),
            "strength": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 1.0, "step": 0.01}),
            "every_seconds": ("FLOAT", {"default": 3.3, "min": 1.0, "max": 30.0, "step": 0.1,
                                        "tooltip": "Distance between anchors. 3.3 s = 18 anchors per minute (tested)."}),
            "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0}),
        }}

    RETURN_TYPES = ("CONDITIONING", "CONDITIONING", "LATENT", "INT")
    RETURN_NAMES = ("positive", "negative", "latent", "anchors")
    FUNCTION = "add"
    CATEGORY = "video/ltx long"
    DESCRIPTION = "Adds light 'eye anchors' (the reference photo) at regular intervals over the whole video, for any length."

    def add(self, positive, negative, vae, latent, image, strength, every_seconds, fps):
        from comfy_extras.nodes_lt import LTXVAddGuide
        n_lat = latent["samples"].shape[2]
        frames = (n_lat - 1) * 8 + 1
        step = max(8, int(round(every_seconds * fps / 8.0)) * 8)
        idxs = list(range(step, frames - 8, step))
        img = image[:1]
        for f in idxs:
            positive, negative, latent = _out(LTXVAddGuide.execute(positive=positive, negative=negative, vae=vae, latent=latent,
                                                                   image=img, frame_idx=int(f), strength=float(strength)))[:3]
        return (positive, negative, latent, len(idxs))


class DecodeSaveVideo:
    """Decodes the latent video in overlapping pieces and writes the mp4 straight to disk (+ voice). Low RAM for any length."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "samples": ("LATENT",), "vae": ("VAE",), "audio": ("AUDIO",),
            "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0}),
            "filename_prefix": ("STRING", {"default": "avatar/AVATAR_long"}),
            "crf": ("INT", {"default": 14, "min": 0, "max": 51}),
            "final_height": ("INT", {"default": 1080, "min": 64, "max": 8192, "tooltip": "Centre-crop to this height (1088 -> 1080)."}),
            "piece_latents": ("INT", {"default": 24, "min": 4, "max": 256, "tooltip": "Latent frames per piece (24 = ~8 s). Lower = less RAM."}),
            "warmup_latents": ("INT", {"default": 2, "min": 1, "max": 8}),
            "tile_size": ("INT", {"default": 512, "min": 256, "max": 4096, "step": 64}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("file",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "video/ltx long"
    DESCRIPTION = "Decode + save in pieces: the full video never sits in memory (a 6 min 1080p video would otherwise need ~200 GB RAM)."

    def run(self, samples, vae, audio, fps, filename_prefix, crf, final_height, piece_latents, warmup_latents, tile_size):
        lat = samples["samples"]
        n = lat.shape[2]
        out_dir = folder_paths.get_output_directory()
        full_dir, fname, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, out_dir)
        os.makedirs(full_dir, exist_ok=True)
        out_name = f"{fname}_{counter:05}_.mp4"
        path = os.path.join(full_dir, out_name)
        rate = Fraction(fps).limit_denominator(1000)
        dec = comfy_nodes.VAEDecodeTiled()

        container = av.open(path, mode="w")
        vstream = None
        astream = None
        # audio first (small)
        try:
            wf = audio["waveform"][0].detach().cpu().float().numpy()  # [C, N]
            sr = int(audio["sample_rate"])
            if wf.ndim == 1:
                wf = wf[None]
            wf = wf[:2]
            astream = container.add_stream("aac", rate=sr)
            astream.bit_rate = 192000
            astream.layout = "stereo" if wf.shape[0] == 2 else "mono"
        except Exception as e:
            print("[DecodeSaveVideo] no audio:", e)
            astream = None

        written = 0
        s = 0
        while s < n:
            e = min(n, s + piece_latents)
            a = max(0, s - warmup_latents) if s > 0 else 0
            piece = {"samples": lat[:, :, a:e]}
            imgs = _out(dec.decode(vae, piece, tile_size, 64, 64, 16))[0]  # [F,H,W,3]
            if s == 0:
                keep = imgs
            else:
                w = s - a
                start = 1 + (w - 1) * 8
                keep = imgs[start:start + (e - s) * 8]
            H, W = keep.shape[1], keep.shape[2]
            top = max(0, (H - final_height) // 2)
            keep = keep[:, top:top + min(final_height, H)]
            arr = (keep.clamp(0, 1) * 255.0 + 0.5).to(torch.uint8).cpu().numpy()
            if vstream is None:
                vstream = container.add_stream("libx264", rate=rate)
                vstream.width, vstream.height = arr.shape[2], arr.shape[1]
                vstream.pix_fmt = "yuv420p"
                vstream.options = {"crf": str(int(crf)), "preset": "medium"}
            for fr in arr:
                vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(fr), format="rgb24")
                vf.pts = written
                vf.time_base = Fraction(1, 1) / rate
                for p in vstream.encode(vf):
                    container.mux(p)
                written += 1
            del imgs, keep, arr
            print(f"[DecodeSaveVideo] latents {s}-{e} of {n} -> {written} frames")
            s = e
        for p in vstream.encode():
            container.mux(p)
        if astream is not None:
            sr = astream.rate
            dur = written / float(rate)
            wf = wf[:, : int(dur * sr)]
            if astream.layout.name == "stereo" and wf.shape[0] == 1:
                wf = np.repeat(wf, 2, axis=0)
            chunk = 1024
            pts = 0
            for i in range(0, wf.shape[1], chunk):
                block = np.ascontiguousarray(wf[:, i:i + chunk].astype(np.float32))
                af = av.AudioFrame.from_ndarray(block, format="fltp", layout=astream.layout.name)
                af.sample_rate = sr
                af.pts = pts
                pts += block.shape[1]
                for p in astream.encode(af):
                    container.mux(p)
            for p in astream.encode():
                container.mux(p)
        container.close()
        return {"ui": {"text": [f"Saved {out_name}: {written} frames ({written / float(rate):.1f} s)"],
                       "images": [{"filename": out_name, "subfolder": subfolder, "type": "output"}], "animated": (True,)},
                "result": (path,)}


NODE_CLASS_MAPPINGS = {"PSYEyeAnchors": EyeAnchors, "PSYDecodeSaveVideo": DecodeSaveVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"PSYEyeAnchors": "Eye anchors (any length)", "PSYDecodeSaveVideo": "Decode + save video in pieces (low memory)"}
