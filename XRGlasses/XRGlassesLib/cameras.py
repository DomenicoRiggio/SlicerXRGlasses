"""Live views of the glasses' tracking cameras inside Slicer.

Each enabled camera gets a volume node, updated as frames arrive, shown in
its own view next to the 3D view. Turning every camera off restores the
layout that was in use before.
"""

import time

import numpy as np
import qt
import slicer
import vtk

from . import device as gd
from . import settings

CAMERAS = {
    gd.CAMERA_LEFT: ("Left grayscale", "XRCameraLeft", "CL"),
    gd.CAMERA_RIGHT: ("Right grayscale", "XRCameraRight", "CR"),
}
LAYOUT_ID = 51150  # arbitrary, high enough not to clash with Slicer's own
CAMERA_TAG = "XRGlasses.Camera"
UPDATE_MS = 33


class CameraViews:
    def __init__(self, session):
        self.session = session
        self.wanted = set()
        self._nodes = {}              # channel -> volume node
        self._counts = {}             # channel -> last frame count shown
        self._history = {}            # channel -> recent frame times
        self._layout_before = None
        self._layout_shown = ()
        self._timer = qt.QTimer()
        self._timer.setInterval(UPDATE_MS)
        self._timer.timeout.connect(self._update)

    # ── public ───────────────────────────────────────────────────

    def set_wanted(self, channel, wanted):
        if wanted:
            self.wanted.add(channel)
        else:
            self.wanted.discard(channel)
        self._sync()

    def frame_rate(self, channel):
        now = time.monotonic()
        return sum(1 for stamp in self._history.get(channel, ())
                   if now - stamp <= 1.0)

    def state_text(self):
        if not self.wanted:
            return ""
        link = self.session._link
        if not self.session.active or link is None:
            return "The cameras start with 3D (they use the glasses' connection)."
        if self.session.status != "running":
            return "Waiting for the glasses…"
        if not link.has_tracking_cameras():
            return "These glasses have no tracking cameras."
        return " · ".join(
            f"{CAMERAS[channel][0]}: {self.frame_rate(channel)} fps"
            for channel in sorted(self.wanted))

    def stop(self):
        """Stop every camera view (the wanted set is kept for next time)."""
        self._timer.stop()
        link = self.session._link
        for channel in CAMERAS:
            if link is not None:
                link.watch_camera(channel, False)
        self._remove_nodes()
        self._restore_layout()

    def _sync(self):
        """Match frame capture, nodes and layout to wanted + session state."""
        link = self.session._link
        live = (self.session.active and link is not None
                and self.session.status == "running"
                and link.has_tracking_cameras())
        if not live or not self.wanted:
            self.stop()
            return
        for channel in CAMERAS:
            link.watch_camera(channel, channel in self.wanted)
        for channel in list(self._nodes):
            if channel not in self.wanted:
                self._remove_node(channel)
        if not self._timer.isActive():
            self._timer.start()
        self.session._notify()

    # ── per frame ────────────────────────────────────────────────

    def _update(self):
        link = self.session._link
        if link is None:
            return
        changed_layout = False
        for channel in sorted(self.wanted):
            count, frame = link.camera_frame(channel)
            if frame is None or count == self._counts.get(channel):
                continue
            self._counts[channel] = count
            history = self._history.setdefault(channel, [])
            now = time.monotonic()
            history.append(now)
            while history and now - history[0] > 1.0:
                history.pop(0)
            node = self._nodes.get(channel)
            if node is None or not slicer.mrmlScene.IsNodePresent(node) \
                    or slicer.util.arrayFromVolume(node).shape[1:] != frame.shape:
                node = self._create_node(channel, frame.shape)
                changed_layout = True
            if settings.get("cameraRotate180"):
                frame = frame[::-1, ::-1]
            slicer.util.arrayFromVolume(node)[0] = frame
            slicer.util.arrayFromVolumeModified(node)
        if changed_layout or tuple(sorted(self._nodes)) != self._layout_shown:
            self._show_layout()

    # ── nodes ────────────────────────────────────────────────────

    def _create_node(self, channel, shape):
        self._remove_node(channel)
        name = f"XR camera {CAMERAS[channel][0].split()[0].lower()}"
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode",
                                                  name)
        node.SetAttribute(CAMERA_TAG, str(channel))
        node.SetSaveWithScene(False)
        height, width = shape
        slicer.util.updateVolumeFromArray(
            node, np.zeros((1, height, width), dtype=np.uint8))
        # Row 0 at the top and column 0 at the left of an axial view.
        directions = vtk.vtkMatrix4x4()
        directions.SetElement(0, 0, -1.0)
        directions.SetElement(1, 1, -1.0)
        node.SetIJKToRASDirectionMatrix(directions)
        node.SetOrigin(0.0, 0.0, 0.0)
        node.SetSpacing(1.0, 1.0, 1.0)
        node.CreateDefaultDisplayNodes()
        display = node.GetDisplayNode()
        display.SetAutoWindowLevel(False)
        display.SetWindowLevel(255.0, 127.5)
        self._nodes[channel] = node
        return node

    def _remove_node(self, channel):
        node = self._nodes.pop(channel, None)
        self._counts.pop(channel, None)
        self._history.pop(channel, None)
        if node is not None and slicer.mrmlScene.IsNodePresent(node):
            slicer.mrmlScene.RemoveNode(node)

    def _remove_nodes(self):
        for channel in list(self._nodes):
            self._remove_node(channel)
        for node in list(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")):
            if node.GetAttribute(CAMERA_TAG):
                slicer.mrmlScene.RemoveNode(node)

    # ── layout ───────────────────────────────────────────────────

    def _show_layout(self):
        layout_manager = slicer.app.layoutManager()
        layout_node = layout_manager.layoutLogic().GetLayoutNode()
        channels = tuple(sorted(self._nodes))
        if not channels:
            return
        if self._layout_before is None:
            self._layout_before = layout_manager.layout
        items = "".join(_camera_view_xml(channel) for channel in channels)
        xml = (
            '<layout type="horizontal" split="true">'
            '<item splitSize="700"><view class="vtkMRMLViewNode" '
            'singletontag="1"><property name="viewlabel" '
            'action="default">1</property></view></item>'
            '<item splitSize="400"><layout type="vertical" split="true">'
            f'{items}</layout></item></layout>')
        if layout_node.IsLayoutDescription(LAYOUT_ID):
            layout_node.SetLayoutDescription(LAYOUT_ID, xml)
        else:
            layout_node.AddLayoutDescription(LAYOUT_ID, xml)
        if layout_manager.layout != LAYOUT_ID:
            layout_manager.setLayout(LAYOUT_ID)
        else:
            layout_node.Modified()
        slicer.app.processEvents()
        for channel in channels:
            widget = layout_manager.sliceWidget(CAMERAS[channel][1])
            if widget is None:
                continue
            logic = widget.sliceLogic()
            logic.GetSliceCompositeNode().SetBackgroundVolumeID(
                self._nodes[channel].GetID())
            logic.GetSliceCompositeNode().SetForegroundVolumeID(None)
            logic.GetSliceCompositeNode().SetLabelVolumeID(None)
            logic.FitSliceToAll()
        self._layout_shown = channels

    def _restore_layout(self):
        if self._layout_before is not None:
            layout_manager = slicer.app.layoutManager()
            if layout_manager.layout == LAYOUT_ID:
                layout_manager.setLayout(self._layout_before)
        self._layout_before = None
        self._layout_shown = ()


def _camera_view_xml(channel):
    _, tag, label = CAMERAS[channel]
    return (
        f'<item><view class="vtkMRMLSliceNode" singletontag="{tag}">'
        '<property name="orientation" action="default">Axial</property>'
        f'<property name="viewlabel" action="default">{label}</property>'
        '<property name="viewcolor" action="default">#7A7A7A</property>'
        '</view></item>')
