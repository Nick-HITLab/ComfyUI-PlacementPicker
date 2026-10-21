"""Placement Picker: drag & resize an overlay (video or image) on a frame of your video.
Outputs x, y, width in video pixels. Pure UI helper; it does no image processing itself."""


class PlacementPicker:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "time": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100000.0, "step": 0.1,
                                   "tooltip": "Which moment of the background video to show in the preview (seconds)."}),
                "x": ("INT", {"default": 24, "min": 0, "max": 16384, "tooltip": "Left edge in video pixels (set by dragging)."}),
                "y": ("INT", {"default": 24, "min": 0, "max": 16384, "tooltip": "Top edge in video pixels (set by dragging)."}),
                "width": ("INT", {"default": 200, "min": 8, "max": 16384, "tooltip": "Width in video pixels (drag the corner)."}),
            },
            "optional": {
                "background": ("VIDEO", {"tooltip": "Connect the video you are editing (Load Video)."}),
                "overlay_video": ("VIDEO", {"tooltip": "Connect the clip that goes in the box (Load Video)."}),
                "overlay_image": ("IMAGE", {"tooltip": "Or connect the picture / logo / object (Load Image)."}),
                "end": ("FLOAT", {"forceInput": True, "tooltip": "Optional: connect the end time - the preview then shows when the item is visible."}),
            },
        }

    RETURN_TYPES = ("INT", "INT", "INT")
    RETURN_NAMES = ("x", "y", "width")
    FUNCTION = "pick"
    CATEGORY = "image/placement"
    DESCRIPTION = "Drag the box on the preview to choose where an overlay goes; drag the corner to resize."

    def pick(self, time, x, y, width, background=None, overlay_video=None, overlay_image=None, end=None):
        return (x, y, width)


NODE_CLASS_MAPPINGS = {"PlacementPicker": PlacementPicker}
NODE_DISPLAY_NAME_MAPPINGS = {"PlacementPicker": "Placement Picker (drag to place)"}

from .video_editor_nodes import NODE_CLASS_MAPPINGS as _VE, NODE_DISPLAY_NAME_MAPPINGS as _VEN
NODE_CLASS_MAPPINGS.update(_VE)
NODE_DISPLAY_NAME_MAPPINGS.update(_VEN)
WEB_DIRECTORY = "./web"

from .ltx_long_nodes import NODE_CLASS_MAPPINGS as _LL, NODE_DISPLAY_NAME_MAPPINGS as _LLN
NODE_CLASS_MAPPINGS.update(_LL)
NODE_DISPLAY_NAME_MAPPINGS.update(_LLN)
