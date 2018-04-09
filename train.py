import logging

import os
from multiprocessing.pool import Pool

import sys
from operator import itemgetter

import cv2
import datetime
import fire
import numpy as np
import tensorflow as tf
from tqdm import tqdm

from checkmate.checkmate import BestCheckpointSaver, get_best_checkpoint
from data_augmentation import get_max_size_of_masks, mask_size_normalize, center_crop
from data_feeder import batch_to_multi_masks, CellImageData, master_dir_test, master_dir_train, \
    CellImageDataManagerValid, CellImageDataManagerTrain, CellImageDataManagerTest, extra1_dir
from hyperparams import HyperParams
from network import Network
from network_basic import NetworkBasic
from network_deeplabv3p import NetworkDeepLabV3p
from network_unet import NetworkUnet
from network_fusionnet import NetworkFusionNet
from network_unet_valid import NetworkUnetValid
from submission import KaggleSubmission, get_multiple_metric, thr_list, get_iou

logger = logging.getLogger('train')
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s')
ch.setFormatter(formatter)
logger.handlers = []
logger.addHandler(ch)


class Trainer:
    def __init__(self):
        self.batchsize = 16
        self.network = None
        self.sess = None

    def set_network(self, model, batchsize=16):
        if model == 'basic':
            self.network = NetworkBasic(batchsize, unet_weight=True)
        elif model == 'simple_unet':
            self.network = NetworkUnet(batchsize, unet_weight=True)
        elif model == 'unet':
            self.network = NetworkUnetValid(batchsize, unet_weight=True)
        elif model == 'deeplabv3p':
            self.network = NetworkDeepLabV3p(batchsize)
        elif model == 'simple_fusion':
            self.network = NetworkFusionNet(batchsize)
        else:
            raise Exception('model name(%s) is not valid' % model)
        self.network.build()
        logger.info('constructing network model: %s' % model)

    def init_session(self):
        if self.sess is not None:
            return
        config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        self.sess = tf.Session(config=config)

    def run(self, model, epoch=600,
            batchsize=16, learning_rate=0.0001, early_rejection=False,
            valid_interval=10, tag='', save_result=True, checkpoint='',
            pretrain=False,
            logdir='/data/public/rw/kaggle-data-science-bowl/logs/',
            **kwargs):
        self.set_network(model, batchsize)
        print(HyperParams.get().__dict__)

        net_output = self.network.get_output()
        net_loss = self.network.get_loss()

        global_step = tf.Variable(0, trainable=False)
        learning_rate_v, train_op = self.network.get_optimize_op(global_step=global_step,
                                                                 learning_rate=learning_rate)

        best_loss_val = 999999
        best_miou_val = 0.0
        name = '%s_%s_lr=%.8f_epoch=%d_bs=%d' % (
            tag if tag else datetime.datetime.now().strftime("%y%m%dT%H%M%f"),
            model,
            learning_rate,
            epoch,
            batchsize,
        )
        model_path = os.path.join(KaggleSubmission.BASEPATH, name, 'model')
        best_ckpt_saver = BestCheckpointSaver(
            save_dir=model_path,
            num_to_keep=100,
            maximize=True
        )

        saver = tf.train.Saver()
        m_epoch = 0
        # tensorboard
        tf.summary.scalar('loss', net_loss, collections=['train', 'valid'])
        s_train = tf.summary.merge_all('train')
        s_valid = tf.summary.merge_all('valid')
        train_writer = tf.summary.FileWriter(logdir + name + '/train', self.sess.graph)
        valid_writer = tf.summary.FileWriter(logdir + name + '/valid', self.sess.graph)

        self.init_session()
        logger.info('training started+')
        if not checkpoint:
            self.sess.run(tf.global_variables_initializer())

            if pretrain:
                global_vars = tf.global_variables()

                from tensorflow.python import pywrap_tensorflow
                reader = pywrap_tensorflow.NewCheckpointReader(self.network.get_pretrain_path())
                var_to_shape_map = reader.get_variable_to_shape_map()
                saved_vars = list(var_to_shape_map.keys())

                var_list = [x for x in global_vars if x.name.replace(':0', '') in saved_vars]
                var_list = [x for x in var_list if 'logit' not in x.name]
                logger.info('pretrained weights(%d) loaded : %s' % (len(var_list), self.network.get_pretrain_path()))

                pretrain_loader = tf.train.Saver(var_list)
                pretrain_loader.restore(self.sess, self.network.get_pretrain_path())
        elif checkpoint == 'best':
            path = get_best_checkpoint(model_path)
            saver.restore(self.sess, path)
            logger.info('restored from best checkpoint, %s' % path)
        elif checkpoint == 'latest':
            path = tf.train.latest_checkpoint(model_path)
            saver.restore(self.sess, path)
            logger.info('restored from latest checkpoint, %s' % path)
        else:
            saver.restore(self.sess, checkpoint)
            logger.info('restored from checkpoint, %s' % checkpoint)

        step = self.sess.run(global_step)
        start_e = (batchsize * step) // CellImageDataManagerTrain().size()

        if epoch > 0:
            try:
                ds_train, ds_valid, ds_valid_full, ds_test = self.network.get_input_flow()
                losses = []
                for e in range(start_e, epoch):
                    loss_val_avg = []
                    train_cnt = 0
                    ds_train.reset_state()
                    ds_train_d = ds_train.get_data()
                    for dp_train in ds_train_d:

                        _, loss_val, summary_train = self.sess.run(
                            [train_op, net_loss, s_train],
                            feed_dict=self.network.get_feeddict(dp_train, True)
                        )
                        loss_val_avg.append(loss_val)
                        train_cnt += 1
                        # for debug
                        # cv2.imshow('train', Network.visualize(dp_train[0][0], dp_train[2][0], None, dp_train[3][0], 'norm1'))
                        # cv2.waitKey(0)
                    ds_train_d.close()

                    step, lr = self.sess.run([global_step, learning_rate_v])
                    loss_val_avg = sum(loss_val_avg) / len(loss_val_avg)
                    logger.info('training %d epoch %d step, lr=%.8f loss=%.4f train_iter=%d' % (
                        e + 1, step, lr, loss_val_avg, train_cnt))
                    losses.append(loss_val)
                    train_writer.add_summary(summary_train, global_step=step)

                    if early_rejection and len(losses) > 100 and losses[len(losses) - 100] * 1.05 < loss_val_avg:
                        logger.info('not improved, stop at %d' % e)
                        break

                    # early rejection
                    if early_rejection and ((e == 50 and loss_val > 0.5) or (e == 200 and loss_val > 0.2)):
                        logger.info('not improved training loss, stop at %d' % e)
                        break

                    m_epoch = e
                    avg = 10.0
                    if loss_val < 0.20 and (e + 1) % valid_interval == 0:
                        avg = []
                        for _ in range(5):
                            ds_valid.reset_state()
                            ds_valid_d = ds_valid.get_data()
                            for dp_valid in ds_valid_d:
                                loss_val, summary_valid = self.sess.run(
                                    [net_loss, s_valid],
                                    feed_dict=self.network.get_feeddict(dp_valid, True)
                                )

                                avg.append(loss_val)
                            ds_valid_d.close()

                        avg = sum(avg) / len(avg)
                        logger.info('validation loss=%.4f' % (avg))
                        if best_loss_val > avg:
                            best_loss_val = avg
                        valid_writer.add_summary(summary_valid, global_step=step)

                    if avg < 0.16 and e > 50 and (e + 1) % valid_interval == 0:
                        cnt_tps = np.array((len(thr_list)), dtype=np.int32),
                        cnt_fps = np.array((len(thr_list)), dtype=np.int32)
                        cnt_fns = np.array((len(thr_list)), dtype=np.int32)
                        pool_args = []
                        ds_valid_full.reset_state()
                        ds_valid_full_d = ds_valid_full.get_data()
                        for idx, dp_valid in tqdm(enumerate(ds_valid_full_d), desc='validate using the iou metric',
                                                  total=len(
                                                      CellImageDataManagerValid.LIST + CellImageDataManagerValid.LIST_EXT1)):
                            image = dp_valid[0]
                            inference_result = self.network.inference(self.sess, image)
                            instances, scores = inference_result['instances'], inference_result['scores']
                            pool_args.append((thr_list, instances, dp_valid[2]))
                        ds_valid_full_d.close()

                        pool = Pool(processes=8)
                        cnt_results = pool.map(do_get_multiple_metric, pool_args)
                        pool.close()
                        pool.join()
                        pool.terminate()
                        for cnt_result in cnt_results:
                            cnt_tps = cnt_tps + cnt_result[0]
                            cnt_fps = cnt_fps + cnt_result[1]
                            cnt_fns = cnt_fns + cnt_result[2]

                        ious = np.divide(cnt_tps, cnt_tps + cnt_fps + cnt_fns)
                        mIou = np.mean(ious)
                        logger.info('validation metric: %.5f' % mIou)
                        if best_miou_val < mIou:
                            best_miou_val = mIou
                        best_ckpt_saver.handle(mIou, self.sess, global_step)  # save & keep best model

                        # early rejection by mIou
                        if early_rejection and e > 50 and best_miou_val < 0.15:
                            break
                        if early_rejection and e > 100 and best_miou_val < 0.25:
                            break
            except KeyboardInterrupt:
                logger.info('interrupted. stop training, start to validate.')

        try:
            chk_path = get_best_checkpoint(model_path, select_maximum_value=True)
            if chk_path:
                logger.info('training is done. Start to evaluate the best model. %s' % chk_path)
                saver.restore(self.sess, chk_path)
        except Exception as e:
            logger.warning('error while loading the best model:' + str(e))

        # show sample in train set : show_train > 0
        kaggle_submit = KaggleSubmission(name)
        logger.info('Start to test on training set.... (may take a while)')
        train_metrics = []
        for single_id in tqdm(CellImageDataManagerTrain.LIST[:20], desc='training set test'):
            result = self.single_id(None, None, single_id, set_type='train', show=False, verbose=False)
            image = result['image']
            labels = result['labels']
            instances = result['instances']
            score = result['score']
            score_desc = result['score_desc']

            img_vis = Network.visualize(image, labels, instances, None)
            kaggle_submit.save_train_image(single_id, img_vis, score=score, score_desc=score_desc)
            train_metrics.append(score)
        logger.info('trainset validation ends. score=%.4f' % np.mean(train_metrics))

        # show sample in valid set : show_valid > 0
        logger.info('Start to test on validation set.... (may take a while)')
        valid_metrics = []
        for single_id in tqdm(CellImageDataManagerValid.LIST, desc='validation set test'):
            result = self.single_id(None, None, single_id, set_type='train', show=False, verbose=False)
            image = result['image']
            labels = result['labels']
            instances = result['instances']
            score = result['score']
            score_desc = result['score_desc']

            img_vis = Network.visualize(image, labels, instances, None)
            kaggle_submit.save_valid_image(single_id, img_vis, score=score, score_desc=score_desc)
            kaggle_submit.valid_instances[single_id] = (instances, result['instance_scores'])
            valid_metrics.append(score)
        logger.info('validation ends. score=%.4f' % np.mean(valid_metrics))

        # show sample in test set
        logger.info('saving...')
        if save_result:
            for single_id in tqdm(CellImageDataManagerTest.LIST, desc='test set evaluation'):
                result = self.single_id(None, None, single_id, set_type='test', show=False, verbose=False)
                image = result['image']
                instances = result['instances']
                img_h, img_w = image.shape[:2]

                img_vis = Network.visualize(image, None, instances, None)

                # save to submit
                instances = Network.resize_instances(instances, (img_h, img_w))
                kaggle_submit.save_image(single_id, img_vis)
                kaggle_submit.test_instances[single_id] = (instances, result['instance_scores'])
                kaggle_submit.add_result(single_id, instances)
            kaggle_submit.save()
        logger.info('done. epoch=%d best_loss_val=%.4f best_mIOU=%.4f name= %s' % (m_epoch, best_loss_val, best_miou_val, name))
        return best_miou_val, name

    def validate(self, network=None, checkpoint=None):
        if network is not None:
            self.set_network(network)

        self.init_session()

        mIOU = []
        self.init_session()
        if checkpoint:
            saver = tf.train.Saver()
            saver.restore(self.sess, checkpoint)
            logger.info('restored from checkpoint, %s' % checkpoint)

        for single_id in CellImageDataManagerValid.LIST:
            result = self.single_id(None, None, single_id, set_type='train', show=False, verbose=True)
            score = result['score']
            mIOU.append(score)
        mIOU = np.mean(mIOU)
        logger.info('mScore = %.5f' % mIOU)
        return mIOU

    def single_id(self, model, checkpoint, single_id, set_type='train', show=True, verbose=True):
        if model:
            self.set_network(model)

        self.init_session()
        if checkpoint:
            saver = tf.train.Saver()
            saver.restore(self.sess, checkpoint)
            if verbose:
                logger.info('restored from checkpoint, %s' % checkpoint)

        if 'TCGA' in single_id:
            d = CellImageData(single_id, extra1_dir, ext='tif')
            # generally, TCGAs have lots of instances -> slow matching performance
            d = center_crop(d, 224, 224, padding=0)
        else:
            d = CellImageData(single_id, (master_dir_train if set_type == 'train' else master_dir_test))
        h, w = d.img.shape[:2]
        shortedge = min(h, w)
        if verbose:
            logger.info('%s image size=(%d x %d)' % (single_id, w, h))

        d = self.network.preprocess(d)

        image = d.image(is_gray=False)

        total_instances = []
        total_scores = []
        total_from_set = []

        inference_result = self.network.inference(self.sess, image)
        instances_pre, scores_pre = inference_result['instances'], inference_result['scores']
        instances_pre = Network.resize_instances(instances_pre, target_size=(h, w))
        total_instances = total_instances + instances_pre
        total_scores = total_scores + scores_pre
        total_from_set = [1] * len(instances_pre)

        # re-inference using flip
        for flip_orientation in range(2):
            flipped = cv2.flip(image.copy(), flip_orientation)
            inference_result = self.network.inference(self.sess, flipped)
            instances_flip, scores_flip = inference_result['instances'], inference_result['scores']
            instances_flip = [cv2.flip(instance.astype(np.uint8), flip_orientation) for instance in instances_flip]
            instances_flip = Network.resize_instances(instances_flip, target_size=(h, w))

            total_instances = total_instances + instances_flip
            total_scores = total_scores + scores_flip
            total_from_set = total_from_set + [2 + flip_orientation] * len(instances_flip)

        # re-inference after rescale image
        def inference_with_scale(image, resize_target):
            image = cv2.resize(image.copy(), None, None, resize_target, resize_target, interpolation=cv2.INTER_AREA)
            inference_result = self.network.inference(self.sess, image)
            instances_rescale, scores_rescale = inference_result['instances'], inference_result['scores']

            instances_rescale = Network.resize_instances(instances_rescale, target_size=(h, w))
            return instances_rescale, scores_rescale

        max_mask = get_max_size_of_masks(instances_pre)
        resize_target = 80.0 / max_mask
        resize_target = min(2.0, resize_target)
        resize_target = max(228.0 / shortedge, resize_target)
        resize_target = max(0.75, resize_target)

        instances_rescale, scores_rescale = inference_with_scale(image, resize_target)
        total_instances = total_instances + instances_rescale
        total_scores = total_scores + scores_rescale
        total_from_set = total_from_set + [4] * len(instances_rescale)

        # re-inference using flip + rescale
        for flip_orientation in range(2):
            flipped = cv2.flip(image.copy(), flip_orientation)
            instances_flip, scores_flip = inference_with_scale(flipped, resize_target)
            instances_flip = [cv2.flip(instance.astype(np.uint8), flip_orientation) for instance in instances_flip]
            instances_flip = Network.resize_instances(instances_flip, target_size=(h, w))

            total_instances = total_instances + instances_flip
            total_scores = total_scores + scores_flip
            total_from_set = total_from_set + [5 + flip_orientation] * len(instances_flip)

        # TODO : Voting?
        voted_idx = []
        for i, x in enumerate(total_instances):
            if np.sum(np.array([get_iou(x, x2) for x2 in total_instances]) > 0.3) > 4:
                voted_idx.append(i)
        if len(voted_idx) > 0:
            ig = itemgetter(*voted_idx)
            total_instances = ig(total_instances)
            total_scores = ig(total_scores)
            total_from_set = ig(total_from_set)
        else:
            total_instances = total_scores = total_from_set = []

        # nms
        instances, scores = Network.nms(total_instances, total_scores, total_from_set)
        instances = Network.remove_overlaps(instances)
        # instances, scores = instances_pre, scores_pre
        # instances, scores = instances_rescale, scores_rescale

        image = cv2.resize(image, (w, h), interpolation=cv2.INTER_AREA)
        score_desc = []
        labels = []
        if len(d.masks) > 0:    # has label masks
            labels = list(d.multi_masks(transpose=False))
            labels = Network.resize_instances(labels, target_size=(h, w))
            tp, fp, fn = get_multiple_metric(thr_list, instances, labels)

            if verbose:
                logger.info('instances=%d, reinf(%.3f) labels=%d' % (len(instances), resize_target, len(labels)))
            for i, thr in enumerate(thr_list):
                desc = 'score=%.3f, tp=%d, fp=%d, fn=%d --- iou %.2f' % (
                    (tp / (tp + fp + fn))[i],
                    tp[i],
                    fp[i],
                    fn[i],
                    thr
                )
                if verbose:
                    logger.info(desc)
                score_desc.append(desc)
            score = np.mean(tp / (tp + fp + fn))
            if verbose:
                logger.info('score=%.3f, tp=%.1f, fp=%.1f, fn=%.1f --- mean' % (
                    score,
                    np.mean(tp),
                    np.mean(fp),
                    np.mean(fn)
                ))
        else:
            score = 0.0

        if show:
            img_vis = Network.visualize(image, labels, instances, None)
            cv2.imshow('valid', img_vis)
            cv2.waitKey(0)
        if not model:
            return {
                'instance_scores': scores,
                'score': score,
                'image': image,
                'instances': instances,
                'labels': labels,
                'score_desc': score_desc
            }


def do_get_multiple_metric(args):
    thr_list, instances, multi_masks_batch = args
    if np.max(multi_masks_batch) == 0:
        # no label
        label = []
    else:
        label = batch_to_multi_masks(multi_masks_batch, transpose=False)
    return get_multiple_metric(thr_list, instances, label)


if __name__ == '__main__':
    fire.Fire(Trainer)
    print(HyperParams.get().__dict__)
