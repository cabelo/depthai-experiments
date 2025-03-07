#!/usr/bin/env python3
import json
import os
import tempfile
from pathlib import Path

import cv2
import depthai
from projector_3d import PointCloudVisualizer
import numpy as np
from time import sleep
import time
import open3d as o3d
import multiprocessing

def pixel_coord_np(width, height):
    """
    Pixel in homogenous coordinate
    Returns:
        Pixel coordinate:       [3, width * height]
    """
    x = np.linspace(0, width - 1, width).astype(np.int)
    y = np.linspace(0, height - 1, height).astype(np.int)
    [x, y] = np.meshgrid(x, y)
    return np.vstack((x.flatten(), y.flatten(), np.ones_like(x.flatten())))

def cvt_to_bgr(packet):
    meta = packet.getMetadata()
    w = meta.getFrameWidth()
    h = meta.getFrameHeight()
    # print((h, w))
    packetData = packet.getData()
    yuv420p = packetData.reshape((h * 3 // 2, w))
    return cv2.cvtColor(yuv420p, cv2.COLOR_YUV2BGR_IYUV)

curr_dir = str(Path('.').resolve().absolute())

device = depthai.Device("", False)
pipeline = device.create_pipeline(config={
    'streams': ['right', 'depth', 'color', 'left'],
    'ai': {
        "blob_file": str(Path('./mobilenet-ssd/mobilenet-ssd.blob').resolve().absolute()),
    },
    'camera': {'mono': {'resolution_h': 720, 'fps': 30},
                'rgb':{'resolution_h': 1080, 'fps': 30}},
})

cam_c = depthai.CameraControl.CamId.RGB
device.request_af_mode(depthai.AutofocusMode.AF_MODE_AUTO)
cmd_set_focus = depthai.CameraControl.Command.MOVE_LENS
device.send_camera_control(cam_c, cmd_set_focus, '135')

# sleep(2)
pixel_coords = pixel_coord_np(1280, 720) 

if pipeline is None:
    raise RuntimeError("Error creating a pipeline!")

right = None
pcl_converter = None
color = None
# req resolution in numpy format
req_resolution = (720,1280) # (h,w) -> numpy format. opencv format (w,h)
count = 0

R_inv = np.linalg.inv(np.array(device.get_rgb_rotation()))
T_neg = -1 * np.array(device.get_rgb_translation())
H_inv = np.linalg.inv(np.array(device.get_right_homography()))
M2    = np.array(device.get_right_intrinsic())
M_RGB = np.array(device.get_intrinsic(depthai.CameraControl.CamId.RGB))
scale_width = 1280/1920
m_scale = [[scale_width,      0,   0],
            [0,         scale_width,        0],
            [0,             0,         1]]

M_RGB = np.matmul(m_scale, M_RGB)
K_inv = np.linalg.inv(M2)
inter_conv = np.matmul(K_inv, H_inv)

extrensics = np.hstack((R_inv, np.transpose([T_neg])))
transform_matrix = np.vstack((extrensics, np.array([0, 0, 0, 1])))


while True:
    data_packets = pipeline.get_available_data_packets()

    for packet in data_packets:
        if packet.stream_name == "color":
            color = cvt_to_bgr(packet)
            scale_width = req_resolution[1]/color.shape[1]
            dest_res = (int(color.shape[1] * scale_width), int(color.shape[0] * scale_width)) ## opencv format dimensions
            
            color = cv2.resize(
                color, dest_res, interpolation=cv2.INTER_CUBIC) # can change interpolation if needed to reduce computations

            if color.shape[0] < req_resolution[0]: # height of color < required height of image
                raise RuntimeError("resizeed height of rgb is smaller than required. {0} < {1}".format(
                    color.shape[0], req_resolution[0]))
            del_height = (color.shape[0] - req_resolution[0]) // 2
            ## TODO(sachin): change center crop and use 1080 directly and test
            # print('del_height ->')
            # print(del_height)
            color_center = color[del_height: del_height + req_resolution[0], :]
            cv2.imshow('color resized', color_center)
            color = color_center

        if packet.stream_name == "right":
            right = packet.getData()
            # cv2.imshow(packet.stream_name, right)
        if packet.stream_name == "left":
            left = packet.getData()
            # cv2.imshow(packet.stream_name, left)
        elif packet.stream_name == "depth":
            frame = packet.getData()
            cv2.imshow(packet.stream_name, frame)
            start = time.time()

            temp = frame.copy() # depth in right frame
            cam_coords = np.dot(inter_conv, pixel_coords) * temp.flatten() * 0.1 # [x, y, z]
            del temp

            cam_coords[:, cam_coords[2] > 1500] = float('inf') 
            o3d.utility.set_verbosity_level(o3d.utility.VerbosityLevel.Debug)
            pcd = o3d.geometry.PointCloud()
            pcd.points = o3d.utility.Vector3dVector(cam_coords.transpose())
            pcd.remove_non_finite_points()
            pcd.transform(transform_matrix)

            rgb_frame_ref_cloud = np.asarray(pcd.points).transpose()
            print('shape pf left_frame_ref_cloud')
            print(rgb_frame_ref_cloud.shape)
            rgb_frame_ref_cloud_normalized = rgb_frame_ref_cloud / rgb_frame_ref_cloud[2,:]
            rgb_image_pts = np.matmul(M_RGB, rgb_frame_ref_cloud_normalized)
            rgb_image_pts = rgb_image_pts.astype(np.int16)            
            print("shape is {}".format(rgb_image_pts.shape[1]))            
            u_v_z = np.vstack((rgb_image_pts, rgb_frame_ref_cloud[2, :]))
            
            lft = np.logical_and(0 <= u_v_z[0], u_v_z[0] < 1280)
            rgt = np.logical_and(0 <= u_v_z[1], u_v_z[1] < 720)
            idx_bool = np.logical_and(lft, rgt)
            u_v_z_sampled = u_v_z[:, np.where(idx_bool)]
            y_idx = u_v_z_sampled[1].astype(int)
            x_idx = u_v_z_sampled[0].astype(int)

            depth_rgb = np.full((720, 1280),  65535, dtype=np.uint16)
            depth_rgb[y_idx,x_idx] = u_v_z_sampled[3]*10

            end = time.time()
            print('for loop Convertion time')
            print(end - start)

            # print('creating image')
            cv2.imshow('rgb_depth', depth_rgb)
            
            depth_rgb[depth_rgb == 0] = 65535

            im_color = (65535 // depth_rgb).astype(np.uint8)
            # colorize depth map, comment out code below to obtain grayscale
            im_color = cv2.applyColorMap(im_color, cv2.COLORMAP_HOT)
            
            added_image = cv2.addWeighted(color,0.6,im_color,0.3,0)
            cv2.imshow('RGBD overlay ', added_image)

    
    if cv2.waitKey(1) == ord("q"):
        break


# 1. change 1080 shape. 
# 2. crop the intrinisc matrix approprietly 
# 3. change depth in rectified right using homography to place it back in right frame and then rotate and translate it to rgb
# 4. how to handle this scenario when undistorted using mesh ? should I add distortions back ? 
# 5. What would be the best way to illuminate the lights properly to avoid reflections or bad calibration (Does vicalib overcomes this issue or is it universal for that too) 
# 6. Do we need calib to be in 4K ? I am thinking of doing it only for 1080 
# 7. Any suggestions on best way to handle in when using camera with auto focus ? 
# currently I have set it to a specific distance that helps in better focusing the calibration board with current setting
# we can create api to return the homography to place the depth from rectified right to rgb a.k.a center of the 1098OBC 
# or we can internally use wrap engine to do that before returning (extra load on Mx) 
# Cropping issue - center crop or bottom crop 
# ANother option is we can just find homography between right and rgb

if pcl_converter is not None:
    pcl_converter.close_window()
