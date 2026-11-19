#!/usr/bin/env python3
"""Builds the eve case for the XIAO ESP32S3 Sense (v3) and writes print-ready STLs.

v2 changes vs v0: outer size ~50% bigger (hand-sized), board held in its own
tight pocket so it doesn't rattle, and a deeper, thicker lid lip that seats in
a full-width recess at the top of the body.

    pip install manifold3d numpy
    python make_stl.py            -> eve_body.stl, eve_lid.stl, eve_plate.stl
"""
import os
import struct

import numpy as np
from manifold3d import Manifold

SEG = 64

# ---- Outer size (v0 was 23 x 43 x ~21 mm) ----
out_w, out_l = 34.5, 64.5     # width, length
out_h = 26.0                  # body height (without lid and feet)
corner_r = 5.0                # rounder corners feel better in the hand

# ---- Parts (measure with calipers and adjust) ----
xiao_w, xiao_l, xiao_h = 17.8, 21.0, 15.0   # XIAO + Sense board stacked
btn = 12.0          # tactile switch body
btn_total = 11.0    # switch base -> top of cap
cap_sq = 12.6       # square lid hole for the Omron B32-13x0 12x12 key top (0.3 mm per side)
usb_w, usb_h = 9.6, 4.2
usb_zc = 13.0       # USB-C centre height above inside floor
clear = 0.6         # gap around the board

# ---- Walls and lid ----
wall, floor_t, lid_t = 3.0, 3.0, 3.0
lip_h = 4.0         # how deep the lid lip goes into the body (v0: 1.6)
lip_t = 2.0         # lip wall thickness (v0: 1.2)
lip_clear = 0.2     # gap per side between lip and body: lower = tighter
protrude = 2.0      # how far the button cap sticks out above the lid
divider = 2.0
foot_h = 1.2

in_w, in_l = out_w - 2 * wall, out_l - 2 * wall
pocket_w, pocket_l = xiao_w + 2 * clear, xiao_l + 2 * clear
btn_y0 = wall + pocket_l + divider                 # start of the button cavity
btn_cx, btn_cy = out_w / 2, btn_y0 + 2 + (btn + 3) / 2 + 4
pillar_top = out_h + lid_t + protrude - btn_total + 1.5


def cube(x, y, z, sx, sy, sz):
    return Manifold.cube([sx, sy, sz]).translate([x, y, z])


def cyl(x, y, z, d, h):
    return Manifold.cylinder(h, d / 2, d / 2, SEG).translate([x, y, z])


def rounded_box(sx, sy, sz, r):
    return Manifold.batch_hull([cyl(x, y, 0, 2 * r, sz) for x in (r, sx - r) for y in (r, sy - r)])


def body():
    b = rounded_box(out_w, out_l, out_h, corner_r)
    cuts = [
        # tight pocket for the board, USB-C end against the front wall
        cube(out_w / 2 - pocket_w / 2, wall, floor_t, pocket_w, pocket_l, out_h),
        # big cavity for the button, wires (and a battery later)
        cube(wall, btn_y0, floor_t, in_w, in_l - pocket_l - divider, out_h),
        # wire slot from board pocket to button cavity
        cube(out_w / 2 - 3, wall + pocket_l - 1, floor_t + 4, 6, divider + 2, out_h),
        # full-width recess at the top where the lid lip seats
        cube(wall, wall, out_h - lip_h - 0.4, in_w, in_l, lip_h + 1),
        # USB-C opening
        cube(out_w / 2 - usb_w / 2, -1, floor_t + usb_zc - usb_h / 2, usb_w, wall + 2, usb_h),
    ]
    # mic grille under the board (the mic is on the Sense board, facing down)
    cuts += [cyl(out_w / 2 + dx, wall + pocket_l / 2 + dy, -1, 1.5, floor_t + 2)
             for dx in (-3.5, 0, 3.5) for dy in (-3.5, 0, 3.5)]
    for c in cuts:
        b -= c
    # button pillar with 12 mm pocket and a channel for the legs
    s = btn + 3
    pillar = cube(btn_cx - s / 2, btn_cy - s / 2, 0, s, s, pillar_top)
    pillar -= cube(btn_cx - (btn + 0.4) / 2, btn_cy - (btn + 0.4) / 2, pillar_top - 1.5, btn + 0.4, btn + 0.4, 2)
    pillar -= cube(btn_cx - (btn + 4) / 2, btn_cy - 2, pillar_top - 4, btn + 4, 4, 5)
    b += pillar
    for x in (corner_r, out_w - corner_r):
        for y in (corner_r, out_l - corner_r):
            b += cyl(x, y, -foot_h, 5, foot_h + 0.01)
    return b.translate([0, 0, foot_h])   # sit on the bed


def lid():
    plate = rounded_box(out_w, out_l, lid_t, corner_r)
    lw, ll = in_w - 2 * lip_clear, in_l - 2 * lip_clear
    lip = cube(0, 0, 0, lw, ll, lip_h) - cube(lip_t, lip_t, -1, lw - 2 * lip_t, ll - 2 * lip_t, lip_h + 2)
    l = plate + lip.translate([wall + lip_clear, wall + lip_clear, lid_t])
    l -= cube(btn_cx - cap_sq / 2, btn_cy - cap_sq / 2, -1, cap_sq, cap_sq, lid_t + lip_h + 2)  # button hole
    l -= cyl(out_w / 2, wall + pocket_l / 2, -1, 2.5, lid_t + 2)      # LED window
    return l


def write_stl(m: Manifold, path: str):
    mesh = m.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3]
    t = np.asarray(mesh.tri_verts)
    tri = v[t]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    with open(path, "wb") as f:
        f.write(b"eve case v3".ljust(80, b" "))
        f.write(struct.pack("<I", len(t)))
        rec = np.zeros(len(t), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
        rec["n"], rec["v"] = n, tri
        f.write(rec.tobytes())


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    b, l = body(), lid()
    write_stl(b, os.path.join(here, "eve_body.stl"))
    write_stl(l, os.path.join(here, "eve_lid.stl"))
    write_stl(b + l.translate([out_w + 8, 0, 0]), os.path.join(here, "eve_plate.stl"))
    for name, m in (("body", b), ("lid", l)):
        bb = m.bounding_box()
        print(f"{name}: {bb[3]-bb[0]:.1f} x {bb[4]-bb[1]:.1f} x {bb[5]-bb[2]:.1f} mm, "
              f"volume {m.volume()/1000:.2f} cm3, genus {m.genus()}, status {m.status()}")
    print(f"assembled height: {foot_h + out_h + lid_t:.1f} mm, cap top {pillar_top - 1.5 + btn_total + foot_h:.1f} mm")
