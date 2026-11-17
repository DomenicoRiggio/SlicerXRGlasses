"""Run with any Python that has numpy:  python test_stereo_math.py"""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "XRGlassesLib"))
import stereo_math as sm  # noqa: E402


def project_x(eye, point, view_angle_deg, aspect):
    """Normalised horizontal image coordinate (-1..1) of `point` for an eye."""
    position, focal_point, up, shift = eye
    frame, _ = sm.camera_frame(position, focal_point, up)
    local = np.linalg.inv(frame) @ np.append(point, 1.0)
    half_width = -local[2] * math.tan(math.radians(view_angle_deg) / 2) * aspect
    return local[0] / half_width - shift


def test_identity_pose():
    matrix = sm.pose_matrix([1, 2, 3, 1, 0, 0, 0])
    assert np.allclose(matrix[:3, :3], np.identity(3))
    assert np.allclose(matrix[:3, 3], [1, 2, 3])
    assert sm.pose_matrix([0, 0, 0, 0, 0, 0, 0]) is None


def test_focal_point_has_zero_parallax():
    fp = np.array([10.0, 20.0, 30.0])
    eyes = sm.eye_cameras([10, -480, 30], fp, [0, 0, 1], 30.0, 16 / 9)
    left = project_x(eyes["left"], fp, 30.0, 16 / 9)
    right = project_x(eyes["right"], fp, 30.0, 16 / 9)
    assert abs(left) < 1e-9 and abs(right) < 1e-9
    # A nearer point must appear further right for the left eye (crossed).
    near = fp + np.array([0.0, -100.0, 0.0])
    assert project_x(eyes["left"], near, 30.0, 16 / 9) > \
        project_x(eyes["right"], near, 30.0, 16 / 9)


def test_separation_scales_with_distance():
    for distance in (60.0, 600.0, 6000.0):
        eyes = sm.eye_cameras([0, -distance, 0], [0, 0, 0], [0, 0, 1],
                              30.0, 16 / 9, ipd_mm=63.0,
                              virtual_distance_mm=600.0)
        separation = np.linalg.norm(eyes["right"][0] - eyes["left"][0])
        assert abs(separation - 63.0 * distance / 600.0) < 1e-6


def test_head_yaw_turns_view_about_the_eye():
    reference = sm.pose_matrix([0, 0, 0, 1, 0, 0, 0])
    angle = math.radians(20)  # yaw to the left about +y (up)
    current = sm.pose_matrix(
        [0, 0, 0, math.cos(angle / 2), 0, math.sin(angle / 2), 0])
    delta = sm.head_delta(reference, current)
    eyes = sm.eye_cameras([0, -600, 0], [0, 0, 0], [0, 0, 1], 30.0, 16 / 9,
                          delta=delta, depth=0.0)
    position, focal_point, _, _ = eyes["left"]
    assert np.allclose(position, [0, -600, 0])
    assert focal_point[0] < -100  # now looking towards -x (the viewer's left)


def test_head_translation_scales_to_scene():
    reference = sm.pose_matrix([0, 0, 0, 1, 0, 0, 0])
    current = sm.pose_matrix([0.1, 0, 0, 1, 0, 0, 0])  # 10 cm to the right
    delta = sm.head_delta(reference, current)
    eyes = sm.eye_cameras([0, -1200, 0], [0, 0, 0], [0, 0, 1], 30.0, 16 / 9,
                          virtual_distance_mm=600.0, delta=delta, depth=0.0)
    assert np.allclose(eyes["left"][0], [200.0, -1200.0, 0.0])


if __name__ == "__main__":
    for name, function in sorted(globals().items()):
        if name.startswith("test_"):
            function()
            print("ok ", name)
