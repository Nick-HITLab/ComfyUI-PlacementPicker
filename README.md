# ComfyUI-PlacementPicker (PSY-AID)

Custom nodes for the PSY-AID LTX-2.5 avatar workflows (Howest).

- **Placement Picker** – drag-to-place preview for logos, video boxes and objects.
- **Video editor nodes (streaming, low memory)** – edits B/E/C/D, AI-edit splice, render edited video.
- **Find jump cuts** – detects hard jumps for the automatic repair (edit R).
- **Eye anchors (any length)** – re-inserts the reference photo every few seconds.
- **Decode + save video in pieces** – decodes long LTX videos piece by piece and writes the mp4 to disk (low RAM).

Install: ComfyUI Manager → *Install via Git URL* → this repository URL, then restart ComfyUI.
No extra Python packages needed (uses PyAV, numpy, Pillow and torch that come with ComfyUI).
Font: DejaVu Sans Bold (see fonts/DejaVu_LICENSE.txt).
