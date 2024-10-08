#!/usr/bin/env python
from __future__ import annotations
import open3d as o3d
import json
import os
import glob
import random
from collections import defaultdict
from pathlib import Path
from typing import Dict, Union
from argparse import ArgumentParser
import logging
import cv2
import numpy as np
import torch
import torchvision
import time
from tqdm import tqdm
import copy

import sys

sys.path.append("..")
import colormaps
from autoencoder.model import Autoencoder
from openclip_encoder import OpenCLIPNetwork
from utils_eval import smooth, colormap_saving, vis_mask_save, polygon_to_mask, stack_mask, show_result
from scene.gaussian_model import GaussianModel


def get_logger(name, log_file=None, log_level=logging.INFO, file_mode='w'):
    logger = logging.getLogger(name)
    stream_handler = logging.StreamHandler()
    handlers = [stream_handler]

    if log_file is not None:
        file_handler = logging.FileHandler(log_file, file_mode)
        handlers.append(file_handler)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.setLevel(log_level)
        logger.addHandler(handler)
    logger.setLevel(log_level)
    return logger


def eval_gt_lerfdata(json_folder: Union[str, Path] = None, ouput_path: Path = None) -> Dict:
    """
    organise lerf's gt annotations
    gt format:
        file name: frame_xxxxx.json
        file content: labelme format
    return:
        gt_ann: dict()
            keys: str(int(idx))
            values: dict()
                keys: str(label)
                values: dict() which contain 'bboxes' and 'mask'
    """
    gt_json_paths = sorted(glob.glob(os.path.join(str(json_folder), 'frame_*.json')))
    img_paths = sorted(glob.glob(os.path.join(str(json_folder), 'frame_*.jpg')))
    gt_ann = {}
    for js_path in gt_json_paths:
        img_ann = defaultdict(dict)
        with open(js_path, 'r') as f:
            gt_data = json.load(f)

        h, w = gt_data['info']['height'], gt_data['info']['width']
        idx = int(gt_data['info']['name'].split('_')[-1].split('.jpg')[0]) - 1
        for prompt_data in gt_data["objects"]:
            label = prompt_data['category']
            box = np.asarray(prompt_data['bbox']).reshape(-1)  # x1y1x2y2
            mask = polygon_to_mask((h, w), prompt_data['segmentation'])
            if img_ann[label].get('mask', None) is not None:
                mask = stack_mask(img_ann[label]['mask'], mask)
                img_ann[label]['bboxes'] = np.concatenate(
                    [img_ann[label]['bboxes'].reshape(-1, 4), box.reshape(-1, 4)], axis=0)
            else:
                img_ann[label]['bboxes'] = box
            img_ann[label]['mask'] = mask

            # # save for visulsization
            save_path = ouput_path / 'gt' / gt_data['info']['name'].split('.jpg')[0] / f'{label}.jpg'
            save_path.parent.mkdir(exist_ok=True, parents=True)
            vis_mask_save(mask, save_path)
        gt_ann[f'{idx}'] = img_ann

    return gt_ann, (h, w), img_paths


def activate_stream(sem_map,
                    clip_model,
                    point_xyz,
                    output_path,
                    generate_mask,
                    index,
                    gaussians_list=None,
                    thresh: float = 0.5,
                    colormap_options=None):
    valid_map = clip_model.get_max_across(sem_map)  # 3xkx832x1264
    n_head, n_prompt, h, w = valid_map.shape

    # positive prompts
    chosen_iou_list, chosen_lvl_list = [], []
    for k in range(n_prompt):
        iou_lvl = np.zeros(n_head)
        mask_lvl = np.zeros((n_head, h, w))
        output_dir = os.path.join(output_path, 'heatmap')
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        for i in range(n_head):
            # NOTE 加滤波结果后的激活值图中找最大值点
            # scale = 30
            # kernel = np.ones((scale,scale)) / (scale**2)
            # np_relev = valid_map[i][k].cpu().numpy()
            # avg_filtered = cv2.filter2D(np_relev, -1, kernel)
            # avg_filtered = torch.from_numpy(avg_filtered).to(valid_map.device)
            # valid_map[i][k] = 0.5 * (avg_filtered + valid_map[i][k])

            rgb_language = colormap_saving(valid_map[i][k].unsqueeze(-1), colormap_options, None)

            if gaussians_list is not None:
                gaussians = gaussians_list[i]
                mask = (valid_map[i][k] > 0.65).squeeze()
                rgb_language = rgb_language[mask.detach().cpu().numpy()]
                gaussians_temp = copy.deepcopy(gaussians)
                gaussians_temp._features_dc = gaussians_temp._features_dc[mask]
                gaussians_temp._features_rest = gaussians_temp._features_rest[mask]
                gaussians_temp._language_feature = gaussians_temp._language_feature[mask]
                gaussians_temp._opacity = gaussians_temp._opacity[mask]
                gaussians_temp._rotation = gaussians_temp._rotation[mask]
                gaussians_temp._scaling = gaussians_temp._scaling[mask]
                gaussians_temp._xyz = gaussians_temp._xyz[mask]
                gaussians_temp.max_radii2D = gaussians_temp.max_radii2D[mask]
                gaussians_temp.save_ply(output_path + '/point_cloud/' + f'exclude_{clip_model.positives[k]}_{i}.ply')
                # torch.save((gaussians_temp.capture(True), 30000), output_path + "/chkpnt/" + f'exclude_{clip_model.positives[k]}_{i}.pth')
                pcd = o3d.geometry.PointCloud()
                pcd.points = o3d.utility.Vector3dVector(point_xyz[mask.detach().cpu().numpy()])

                pcd.colors = o3d.utility.Vector3dVector(rgb_language.reshape(rgb_language.shape[0], 3))
                save_path = os.path.join(output_dir,
                                         output_path.split('/')[-1] + f'exclude_{clip_model.positives[k]}_{i}.ply')
                o3d.io.write_point_cloud(save_path, pcd)
            elif generate_mask:
                mask = valid_map[i][k] > 0.58
                mask_path = os.path.join(output_path, "masks", clip_model.positives[k], str(i), f'{index}.png')
                torchvision.utils.save_image(mask.float(), mask_path)
            else:
                pcd = o3d.geometry.PointCloud()
                pcd.points = o3d.utility.Vector3dVector(point_xyz)

                pcd.colors = o3d.utility.Vector3dVector(rgb_language.reshape(rgb_language.shape[0], 3))
                save_path = os.path.join(output_dir,
                                         output_path.split('/')[-1] + f'exclude_{clip_model.positives[k]}_{i}.ply')
                o3d.io.write_point_cloud(save_path, pcd)
            # NOTE 与lerf一致，激活值低于0.5的认为是背景
            # p_i = torch.clip(valid_map[i][k] - 0.5, 0, 1).unsqueeze(-1)
            # valid_composited = colormaps.apply_colormap(p_i / (p_i.max() + 1e-6), colormaps.ColormapOptions("turbo"))
            # mask = (valid_map[i][k] < 0.5).squeeze()
            # valid_composited[mask, :] = image[mask, :] * 0.3
            # output_path_compo = image_name / 'composited' / f'{clip_model.positives[k]}_{i}'
            # output_path_compo.parent.mkdir(exist_ok=True, parents=True)
            # colormap_saving(valid_composited, colormap_options, output_path_compo)
            #
            # # truncate the heatmap into mask
            # output = valid_map[i][k]
            # output = output - torch.min(output)
            # output = output / (torch.max(output) + 1e-9)
            # output = output * (1.0 - (-1.0)) + (-1.0)
            # output = torch.clip(output, 0, 1)
            #
            # mask_pred = (output.cpu().numpy() > thresh).astype(np.uint8)
            # mask_pred = smooth(mask_pred)
            # mask_lvl[i] = mask_pred
            # mask_gt = img_ann[clip_model.positives[k]]['mask'].astype(np.uint8)
            #
            # # calculate iou
            # intersection = np.sum(np.logical_and(mask_gt, mask_pred))
            # union = np.sum(np.logical_or(mask_gt, mask_pred))
            # iou = np.sum(intersection) / np.sum(union)
            # iou_lvl[i] = iou

        # score_lvl = torch.zeros((n_head,), device=valid_map.device)
        # for i in range(n_head):
        #     score = valid_map[i, k].max()
        #     score_lvl[i] = score
        # chosen_lvl = torch.argmax(score_lvl)
        #
        # chosen_iou_list.append(iou_lvl[chosen_lvl])
        # chosen_lvl_list.append(chosen_lvl.cpu().numpy())
        #
        # # save for visulsization
        # save_path = image_name / f'chosen_{clip_model.positives[k]}.png'
        # vis_mask_save(mask_lvl[chosen_lvl], save_path)


def lerf_localization(sem_map, image, clip_model, image_name, img_ann):
    output_path_loca = image_name / 'localization'
    output_path_loca.mkdir(exist_ok=True, parents=True)

    valid_map = clip_model.get_max_across(sem_map)  # 3xkx832x1264
    n_head, n_prompt, h, w = valid_map.shape

    # positive prompts
    acc_num = 0
    positives = list(img_ann.keys())
    for k in range(len(positives)):
        select_output = valid_map[:, k]

        # NOTE 平滑后的激活值图中找最大值点
        scale = 30
        kernel = np.ones((scale, scale)) / (scale ** 2)
        np_relev = select_output.cpu().numpy()
        avg_filtered = cv2.filter2D(np_relev.transpose(1, 2, 0), -1, kernel)

        score_lvl = np.zeros((n_head,))
        coord_lvl = []
        for i in range(n_head):
            score = avg_filtered[..., i].max()
            coord = np.nonzero(avg_filtered[..., i] == score)
            score_lvl[i] = score
            coord_lvl.append(np.asarray(coord).transpose(1, 0)[..., ::-1])

        selec_head = np.argmax(score_lvl)
        coord_final = coord_lvl[selec_head]

        for box in img_ann[positives[k]]['bboxes'].reshape(-1, 4):
            flag = 0
            x1, y1, x2, y2 = box
            x_min, x_max = min(x1, x2), max(x1, x2)
            y_min, y_max = min(y1, y2), max(y1, y2)
            for cord_list in coord_final:
                if (cord_list[0] >= x_min and cord_list[0] <= x_max and
                        cord_list[1] >= y_min and cord_list[1] <= y_max):
                    acc_num += 1
                    flag = 1
                    break
            if flag != 0:
                break

        # NOTE 将平均后的结果与原结果相加，抑制噪声并保持激活边界清晰
        avg_filtered = torch.from_numpy(avg_filtered[..., selec_head]).unsqueeze(-1).to(select_output.device)
        torch_relev = 0.5 * (avg_filtered + select_output[selec_head].unsqueeze(-1))
        p_i = torch.clip(torch_relev - 0.5, 0, 1)
        valid_composited = colormaps.apply_colormap(p_i / (p_i.max() + 1e-6), colormaps.ColormapOptions("turbo"))
        mask = (torch_relev < 0.5).squeeze()
        valid_composited[mask, :] = image[mask, :] * 0.3

        save_path = output_path_loca / f"{positives[k]}.png"
        show_result(valid_composited.cpu().numpy(), coord_final,
                    img_ann[positives[k]]['bboxes'], save_path)
    return acc_num


def evaluate(feat_dir, output_path, ae_ckpt_path, generate_mask, mask_thresh, encoder_hidden_dims, decoder_hidden_dims,
             compressed_sem_feats, point_xyz, gaussians_list=None):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    colormap_options = colormaps.ColormapOptions(
        colormap="turbo",
        normalize=True,
        colormap_min=-1.0,
        colormap_max=1.0,
    )

    # gt_ann, image_shape, image_paths = eval_gt_lerfdata(Path(json_folder), Path(output_path))
    if gaussians_list is None and generate_mask:
        compressed_sem_feats = np.zeros((len(feat_dir), 177, 730, 988, 3), dtype=np.float32)
        for i in range(len(feat_dir)):
            feat_paths_lvl = sorted(glob.glob(os.path.join(feat_dir[i], '*.npy')),
                                    key=lambda file_name: int(os.path.basename(file_name).split(".npy")[0]))
            for j, idx in enumerate(feat_paths_lvl):
                compressed_sem_feats[i][j] = np.load(idx)

    # instantiate autoencoder and openclip
    clip_model = OpenCLIPNetwork(device)
    checkpoint = torch.load(ae_ckpt_path, map_location=device)
    model = Autoencoder(encoder_hidden_dims, decoder_hidden_dims).to(device)
    model.load_state_dict(checkpoint)
    model.eval()

    # chosen_iou_all, chosen_lvl_list = [], []
    # acc_num = 0
    for j in tqdm(range(compressed_sem_feats.shape[1])):
        sem_feat = compressed_sem_feats[:, j, ...]
        sem_feat = torch.from_numpy(sem_feat).float().to(device)

        with torch.no_grad():
            lvl, h, w, _ = sem_feat.shape
            restored_feat = model.decode(sem_feat.flatten(0, 2))
            restored_feat = restored_feat.view(lvl, h, w, -1)  # 3x832x1264x512

        img_ann = ['stuffed bear', 'coffee mug', 'bag of cookies', 'sheep', 'apple', 'paper napkin', 'plate',
                   'tea in a glass', 'bear nose', 'three cookies', 'coffee', 'dall-e brand', 'yellow pouf', 'hooves']
        # img_ann = ['apple','tea in a glass']
        if generate_mask:
            for folder_name in img_ann:
                for i in range(len(feat_dir)):
                    folder_path = os.path.join(output_path, "masks", folder_name, str(i))
                    os.makedirs(folder_path, exist_ok=True)
        clip_model.set_positives(img_ann)

        activate_stream(restored_feat, clip_model, point_xyz, output_path, generate_mask, j,
                        gaussians_list=gaussians_list,
                        thresh=mask_thresh, colormap_options=colormap_options)
        # chosen_iou_all.extend(c_iou_list)
        # chosen_lvl_list.extend(c_lvl)
        #
        # acc_num_img = lerf_localization(restored_feat, rgb_img, clip_model, image_name, img_ann)
        # acc_num += acc_num_img

    # # # iou
    # mean_iou_chosen = sum(chosen_iou_all) / len(chosen_iou_all)
    # logger.info(f'trunc thresh: {mask_thresh}')
    # logger.info(f"iou chosen: {mean_iou_chosen:.4f}")
    # logger.info(f"chosen_lvl: \n{chosen_lvl_list}")
    #
    # # localization acc
    # total_bboxes = 0
    # for img_ann in gt_ann.values():
    #     total_bboxes += len(list(img_ann.keys()))
    # acc = acc_num / total_bboxes
    # logger.info("Localization accuracy: " + f'{acc:.4f}')


def seed_everything(seed_value):
    random.seed(seed_value)
    np.random.seed(seed_value)
    torch.manual_seed(seed_value)
    os.environ['PYTHONHASHSEED'] = str(seed_value)

    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed_value)
        torch.cuda.manual_seed_all(seed_value)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = True


if __name__ == "__main__":
    seed_num = 42
    seed_everything(seed_num)

    parser = ArgumentParser(description="prompt any label")
    parser.add_argument("--dataset_name", type=str, default=None)
    parser.add_argument("--ckpt_path", type=str, default=None)
    parser.add_argument('--feat_dir', type=str, default=None)
    parser.add_argument("--ae_ckpt_dir", type=str, default=None)
    parser.add_argument("--output_dir", type=str, default=None)
    parser.add_argument("--json_folder", type=str, default=None)
    parser.add_argument("--mask_thresh", type=float, default=0.4)
    #直接剔除高斯点
    parser.add_argument("--exclude_objects", action="store_true", help='3d query')
    #利用render.py渲染的2d language_feature,生成mask
    parser.add_argument("--generate_mask", action="store_true",help='2d query')
    parser.add_argument('--encoder_dims',
                        nargs='+',
                        type=int,
                        default=[256, 128, 64, 32, 3],
                        )
    parser.add_argument('--decoder_dims',
                        nargs='+',
                        type=int,
                        default=[16, 32, 64, 128, 256, 256, 512],
                        )
    args = parser.parse_args()

    # NOTE config setting
    dataset_name = args.dataset_name
    mask_thresh = args.mask_thresh
    feat_dir = [os.path.join(args.feat_dir, dataset_name + f"_{i}", "train/ours_None/renders_npy") for i in range(1, 4)]
    ckpt_dir = [os.path.join(args.feat_dir, dataset_name + f"_{i}", "chkpnt30000.pth") for i in range(1, 4)]
    output_path = os.path.join(args.output_dir, dataset_name)
    ae_ckpt_path = os.path.join(args.ae_ckpt_dir, dataset_name, "best_ckpt.pth")
    json_folder = os.path.join(args.json_folder, dataset_name)
    gaussians = GaussianModel(3)
    checkpoint = ckpt_dir[0]
    (model_params, first_iter) = torch.load(checkpoint)
    gaussians.restore(model_params, args, mode='test')
    gaussians_list = [copy.deepcopy(gaussians)]
    compressed_sem_feats = np.zeros((3, 1, gaussians.get_language_feature.shape[0], 1, 3), dtype=np.float32)
    compressed_sem_feats[0] = gaussians.get_language_feature.detach().cpu().numpy().reshape(1,
                                                                                            gaussians.get_language_feature.shape[
                                                                                                0], 1, 3)
    point_xyz = gaussians.get_xyz.detach().cpu().numpy()
    for i in range(1, 3):
        checkpoint = ckpt_dir[i]
        (model_params, first_iter) = torch.load(checkpoint)
        gaussians.restore(model_params, args, mode='test')
        gaussians_list.append(copy.deepcopy(gaussians))
        compressed_sem_feats[i] = gaussians.get_language_feature.detach().cpu().numpy().reshape(1,
                                                                                                gaussians.get_language_feature.shape[
                                                                                                    0], 1, 3)
    # NOTE logger
    # timestamp = time.strftime('%Y%m%d_%H%M%S', time.localtime())
    # os.makedirs(output_path, exist_ok=True)
    # log_file = os.path.join(output_path, f'{timestamp}.log')
    # logger = get_logger(f'{dataset_name}', log_file=log_file, log_level=logging.INFO)
    if args.exclude_objects:
        evaluate(feat_dir, output_path, ae_ckpt_path, args.generate_mask, mask_thresh, args.encoder_dims,
                 args.decoder_dims,
                 compressed_sem_feats, point_xyz, gaussians_list=gaussians_list)
    else:
        evaluate(feat_dir, output_path, ae_ckpt_path, args.generate_mask, mask_thresh, args.encoder_dims,
                 args.decoder_dims,
                 compressed_sem_feats, point_xyz)
