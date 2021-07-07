#!/usr/bin/env python
"""Test semantic segmentation models chunk-by-chunk"""

import os
import os.path as osp
import sys
import argparse
import logging
import time
import socket
import warnings

import numpy as np
import open3d
import torch
from torch.utils.data.dataloader import DataLoader

# Assume that the script is run at the root directory
_ROOT_DIR = os.path.abspath(osp.dirname(__file__) + '/..')
sys.path.insert(0, _ROOT_DIR)
_DEBUG = False

from common.utils.checkpoint import CheckpointerV2
from common.utils.logger import setup_logger
from common.utils.metric_logger import MetricLogger
from common.utils.torch_util import set_random_seed

from safnet.models.build import build_model_mvpnet_3d
from safnet.data.scannet_2d3d import ScanNet2D3DChunksTest
from safnet.evaluate_3d import Evaluator


def parse_args():
    parser = argparse.ArgumentParser(description='PyTorch 3D Deep Learning Test')
    parser.add_argument(
        '--cfg',
        dest='config_file',
        default='',
        metavar='FILE',
        help='path to config file',
        type=str,
    )
    parser.add_argument('--ckpt-path', type=str, help='path to checkpoint file')
    parser.add_argument('--split', type=str, default='val', help='split')
    parser.add_argument('--save', action='store_true', help='save predictions')
    parser.add_argument('--cache-dir', type=str, default='/home/disk4/pickle', help='cache directory')
    parser.add_argument('--image-dir', type=str, default='/home/disk4/scannetimage_resized/scans_resize_160x120', help='image directory')
    parser.add_argument('--chunk-size', type=float, default=1.5, help='chunk size')
    parser.add_argument('--chunk-stride', type=float, default=0.5, help='chunk stride')
    parser.add_argument('--chunk-thresh', type=int, default=1000, help='chunk threshold, must be >= num_centroids')
    parser.add_argument('--min-nb-pts', type=int, default=2048, help='minimum number of points in the chunk')
    # arguments to overwrite (can be different from training), or modify by using opts
    parser.add_argument('--num-views', type=int, help='number of views')
    parser.add_argument('--k', type=int, help='knn')
    parser.add_argument(
        'opts',
        help='Modify config options using the command-line',
        default=None,
        nargs=argparse.REMAINDER,
    )
    parser.add_argument('--similarity', type=str, default='point', help='cache directory')

    args = parser.parse_args()
    return args


def test(similarity,cfg, args, output_dir='', run_name=''):
    logger = logging.getLogger('safnet.test')

    # build safnet model
    model = build_model_mvpnet_3d(similarity,cfg)[0]
    model = model.cuda()

    # build checkpointer
    checkpointer = CheckpointerV2(model, save_dir=output_dir, logger=logger)
    if args.ckpt_path:
        # load weight if specified
        weight_path = args.ckpt_path.replace('@', output_dir)
        checkpointer.load(weight_path, resume=False)
    else:
        # load last checkpoint
        checkpointer.load(None, resume=True)

    # build dataset
    k = args.k or cfg.DATASET.ScanNet2D3DChunks.k
    num_views = args.num_views or cfg.DATASET.ScanNet2D3DChunks.num_rgbd_frames
    test_dataset = ScanNet2D3DChunksTest(cache_dir=args.cache_dir,
                                         image_dir=args.image_dir,
                                         split=args.split,
                                         chunk_size=(args.chunk_size, args.chunk_size),
                                         chunk_stride=args.chunk_stride,
                                         chunk_thresh=args.chunk_thresh,
                                         num_rgbd_frames=num_views,
                                         resize=cfg.DATASET.ScanNet2D3DChunks.resize,
                                         image_normalizer=cfg.DATASET.ScanNet2D3DChunks.image_normalizer,
                                         k=k,
                                         to_tensor=True,
                                         )
    test_dataloader = DataLoader(test_dataset,
                                 batch_size=1,
                                 shuffle=False,
                                 num_workers=10,
                                 collate_fn=lambda x: x[0])

    # evaluator
    class_names = test_dataset.class_names
    evaluator = Evaluator(class_names)
    num_classes = len(class_names)
    submit_dir = None
    if args.save:
        submit_dir = osp.join(output_dir, 'submit', run_name)
        os.makedirs(submit_dir)
        logits_dir = osp.join(submit_dir, 'logits')
        os.makedirs(logits_dir)

    # ---------------------------------------------------------------------------- #
    # Test
    # ---------------------------------------------------------------------------- #
    model.eval()
    set_random_seed(cfg.RNG_SEED)
    test_meters = MetricLogger(delimiter='  ')

    with torch.no_grad():
        start_time = time.time()
        start_time_scan = time.time()
        for scan_idx, data_dict_list in enumerate(test_dataloader):
            # fetch data
            scan_id = test_dataset.scan_ids[scan_idx]
            points = test_dataset.data[scan_idx]['points'].astype(np.float32)
            seg_label = None
            if args.split != 'test':
                seg_label = test_dataset.data[scan_idx]['seg_label']
                seg_label = test_dataset.nyu40_to_scannet[seg_label]
            data_time = time.time() - start_time_scan

            # prepare outputs
            num_points = len(points)
            pred_logit_whole_scene = np.zeros([num_points, num_classes], dtype=np.float32)
            num_pred_per_point = np.zeros(num_points, dtype=np.uint8)

            # iterate over chunks
            tic = time.time()
            for data_dict in data_dict_list:
                chunk_ind = data_dict.pop('chunk_ind')

                # padding for chunks with points less than min-nb-pts
                # It is required since farthest point sampling requires more points than centroids.
                chunk_points = data_dict['points']
                chunk_nb_pts = chunk_points.shape[1]  # note that already transposed
                if chunk_nb_pts < args.min_nb_pts:
                    print('Too sparse chunk in {} with {} points.'.format(scan_id, chunk_nb_pts))
                    pad = np.random.randint(chunk_nb_pts, size=args.min_nb_pts - chunk_nb_pts)
                    choice = np.hstack([np.arange(chunk_nb_pts), pad])
                    data_dict['points'] = data_dict['points'][:, choice]
                    data_dict['knn_indices'] = data_dict['knn_indices'][choice]

                data_batch = {k: torch.tensor([v]) for k, v in data_dict.items()}
                data_batch = {k: v.cuda(non_blocking=True) for k, v in data_batch.items()}
                # forward
                preds,_,_2 = model(data_batch)
                seg_logit = preds['seg_logit'].squeeze(0).cpu().numpy().T
                seg_logit = seg_logit[:len(chunk_ind)]
                # update
                pred_logit_whole_scene[chunk_ind] += seg_logit
                num_pred_per_point[chunk_ind] += 1
            forward_time = time.time() - tic

            pred_logit_whole_scene = pred_logit_whole_scene / np.maximum(num_pred_per_point[:, np.newaxis], 1)
            pred_label_whole_scene = np.argmax(pred_logit_whole_scene, axis=1)

            no_pred_mask = num_pred_per_point == 0
            no_pred_indices = np.nonzero(no_pred_mask)[0]
            if no_pred_indices.size > 0:
                logger.warning('{:s}: There are {:d} points without prediction.'.format(scan_id, no_pred_mask.sum()))
                pred_label_whole_scene[no_pred_indices] = num_classes

            if _DEBUG:
                # DEBUG: visualize scene
                from safnet.utils.visualize import visualize_labels
                visualize_labels(points, pred_label_whole_scene)
                visualize_labels(points, seg_label)

            # evaluate
            tic = time.time()
            if seg_label is not None:
                evaluator.update(pred_label_whole_scene, seg_label)
            metric_time = time.time() - tic

            batch_time = time.time() - start_time_scan
            test_meters.update(time=batch_time)
            test_meters.update(data=data_time,
                               forward_time=forward_time,
                               metric_time=metric_time,
                               )

            # save prediction
            if submit_dir:
                remapped_pred_labels = test_dataset.scannet_to_nyu40[pred_label_whole_scene]
                np.savetxt(osp.join(submit_dir, scan_id + '.txt'), remapped_pred_labels, '%d')
                np.save(osp.join(logits_dir, scan_id + '.npy'), pred_logit_whole_scene, '%d')

            logger.info(
                test_meters.delimiter.join(
                    [
                        '{:d}/{:d}({:s})',
                        'acc: {acc:.2f}',
                        'IoU: {iou:.2f}',
                        '{meters}',
                    ]
                ).format(
                    scan_idx, len(test_dataset), scan_id,
                    acc=evaluator.overall_acc * 100.0,
                    iou=evaluator.overall_iou * 100.0,
                    meters=str(test_meters),
                )
            )
            start_time_scan = time.time()

        test_time = time.time() - start_time
        logger.info('Test {}  test time: {:.2f}s'.format(test_meters.summary_str, test_time))

    # evaluate
    logger.info('overall accuracy={:.2f}%'.format(100.0 * evaluator.overall_acc))
    logger.info('overall IOU={:.2f}'.format(100.0 * evaluator.overall_iou))
    logger.info('class-wise accuracy and IoU.\n{}'.format(evaluator.print_table()))
    evaluator.save_table(osp.join(output_dir, 'eval.{}.tsv'.format(run_name)))


def main():
    args = parse_args()

    # load the configuration
    # import on-the-fly to avoid overwriting cfg
    from common.config import purge_cfg
    from safnet.config.mvpnet_3d import cfg
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    purge_cfg(cfg)
    cfg.freeze()

    output_dir = cfg.OUTPUT_DIR
    # replace '@' with config path
    if output_dir:
        config_path = osp.splitext(args.config_file)[0]
        output_dir = output_dir.replace('@', config_path.replace('configs', 'outputs'))
        if not osp.isdir(output_dir):
            warnings.warn('Make a new directory: {}'.format(output_dir))
            os.makedirs(output_dir)

    # run name
    timestamp = time.strftime('%m-%d_%H-%M-%S')
    hostname = socket.gethostname()
    run_name = '{:s}.{:s}'.format(timestamp, hostname)

    logger = setup_logger('safnet', output_dir, comment='test.{:s}'.format(run_name))
    logger.info('{:d} GPUs available'.format(torch.cuda.device_count()))
    logger.info(args)

    from common.utils.misc import collect_env_info
    logger.info('Collecting env info (might take some time)\n' + collect_env_info())

    logger.info('Loaded configuration file {:s}'.format(args.config_file))
    logger.info('Running with config:\n{}'.format(cfg))

    assert cfg.TASK == 'mvpnet_3d'
    test(args.similarity,cfg, args, output_dir, run_name)


if __name__ == '__main__':
    main()
