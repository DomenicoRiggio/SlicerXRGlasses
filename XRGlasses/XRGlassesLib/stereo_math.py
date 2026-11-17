"""Camera geometry for side-by-side stereo. Pure numpy; no Slicer imports.

Conventions
-----------
A camera frame is a 4x4 matrix whose columns are [right, up, backward,
position]. The glasses report their pose in the same convention (OpenGL axes:
x right, y up, z backward), so a head movement expressed in the head's own
frame can be applied directly to a camera frame by post-multiplication.

The scene is presented as if it were a physical model floating at
`virtual_distance_mm` in front of the viewer: Slicer's focal point (the centre
of rotation of the 3D view) sits on the zero-parallax plane, and both the eye
separation and head translation are scaled by focal distance / virtual
distance. Stereo depth therefore stays comfortable at any zoom level.
"""

import math

import numpy as np


def pose_matrix(pose):
    """Convert [px, py, pz, qw, qx, qy, qz] to a 4x4 matrix, or None."""
    px, py, pz, qw, qx, qy, qz = (float(value) for value in pose)
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if not math.isfinite(norm) or norm < 1e-6:
        return None
    qw, qx, qy, qz = qw / norm, qx / norm, qy / norm, qz / norm
    matrix = np.identity(4)
    matrix[0, :3] = (1 - 2 * (qy * qy + qz * qz),
                     2 * (qx * qy - qz * qw),
                     2 * (qx * qz + qy * qw))
    matrix[1, :3] = (2 * (qx * qy + qz * qw),
                     1 - 2 * (qx * qx + qz * qz),
                     2 * (qy * qz - qx * qw))
    matrix[2, :3] = (2 * (qx * qz - qy * qw),
                     2 * (qy * qz + qx * qw),
                     1 - 2 * (qx * qx + qy * qy))
    matrix[:3, 3] = (px, py, pz)
    if not np.all(np.isfinite(matrix)):
        return None
    return matrix


def head_delta(reference, current):
    """Head movement since `reference`, expressed in the reference frame."""
    return np.linalg.inv(reference) @ current


def camera_frame(position, focal_point, view_up):
    """Return (frame, focal_distance) for a look-at camera."""
    position = np.asarray(position, dtype=float)
    forward = np.asarray(focal_point, dtype=float) - position
    distance = float(np.linalg.norm(forward))
    if distance < 1e-9:
        forward, distance = np.array([0.0, 1.0, 0.0]), 1.0
    else:
        forward = forward / distance
    up = np.asarray(view_up, dtype=float)
    right = np.cross(forward, up)
    if np.linalg.norm(right) < 1e-9:
        # view_up parallel to the view direction: pick any perpendicular.
        helper = np.array([0.0, 0.0, 1.0])
        if abs(forward[2]) > 0.9:
            helper = np.array([1.0, 0.0, 0.0])
        right = np.cross(forward, helper)
    right = right / np.linalg.norm(right)
    up = np.cross(right, forward)
    frame = np.identity(4)
    frame[:3, 0] = right
    frame[:3, 1] = up
    frame[:3, 2] = -forward
    frame[:3, 3] = position
    return frame, distance


def eye_cameras(position, focal_point, view_up, view_angle_deg, aspect,
                ipd_mm=63.0, virtual_distance_mm=600.0, depth=1.0,
                delta=None, translate=True, parallel_scale=None):
    """Compute both eye cameras from the centre camera.

    delta: optional head movement (metres) from head_delta(); rotation is
        applied one-to-one, translation is scaled to the scene.
    parallel_scale: set for a parallel-projection camera (its half height).
    Returns {"left": eye, "right": eye, "scale": mm of scene per mm of real
    space}, where eye = (position, focal_point, view_up, window_center_x).
    """
    frame, distance = camera_frame(position, focal_point, view_up)
    scale = distance / max(float(virtual_distance_mm), 1.0)

    if delta is not None:
        movement = np.array(delta, dtype=float)
        if translate:
            movement[:3, 3] *= 1000.0 * scale  # metres -> scene millimetres
        else:
            movement[:3, 3] = 0.0
        frame = frame @ movement

    right, up, position = frame[:3, 0], frame[:3, 1], frame[:3, 3]
    forward = -frame[:3, 2]
    focal_point = position + forward * distance

    half_separation = 0.5 * float(ipd_mm) * scale * max(float(depth), 0.0)
    offset = right * half_separation

    # Off-axis projection: shift each eye's frustum so that both are centred
    # on the focal point. VTK's window centre is in half-widths of the
    # viewport and the view angle is vertical, hence the aspect ratio.
    if parallel_scale:
        half_width = float(parallel_scale) * aspect
    else:
        half_width = (distance * math.tan(math.radians(view_angle_deg) / 2.0)
                      * aspect)
    shift = half_separation / half_width if half_width > 1e-9 else 0.0
    shift = min(shift, 0.5)

    return {
        "left": (position - offset, focal_point - offset, up, shift),
        "right": (position + offset, focal_point + offset, up, -shift),
        "scale": scale,
    }
