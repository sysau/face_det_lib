"""You Only Look Once Object Detection v3"""
# pylint: disable=arguments-differ
from __future__ import absolute_import
from __future__ import division

import os, sys
import warnings
import numpy as np
import mxnet as mx
from mxnet import gluon
from mxnet import autograd
from mxnet.gluon import nn
from mxnet.gluon.nn import BatchNorm
from yolo_target import YOLOV3TargetMerger
from loss import YOLOV3Loss

__all__ = ['YOLOV3']

def _conv2d(channel, kernel, padding, stride, norm_layer=BatchNorm, norm_kwargs=None):
    """A common conv-bn-leakyrelu cell"""
    cell = nn.HybridSequential(prefix='')
    cell.add(nn.Conv2D(channel, kernel_size=kernel,
                       strides=stride, padding=padding, use_bias=False))
    cell.add(norm_layer(epsilon=1e-5, momentum=0.9, **({} if norm_kwargs is None else norm_kwargs)))
    cell.add(nn.LeakyReLU(0.1))
    return cell

def _upsample(x, stride=2):
    """Simple upsampling layer by stack pixel alongside horizontal and vertical directions.
    Parameters
    ----------
    x : mxnet.nd.NDArray or mxnet.symbol.Symbol
        The input array.
    stride : int, default is 2
        Upsampling stride
    """
    return x.repeat(axis=-1, repeats=stride).repeat(axis=-2, repeats=stride)


class YOLOOutputV3(gluon.HybridBlock):
    """YOLO output layer V3.
    Parameters
    ----------
    index : int
        Index of the yolo output layer, to avoid naming conflicts only.
    num_class : int
        Number of foreground objects.
    anchors : iterable
        The anchor setting. Reference: https://arxiv.org/pdf/1804.02767.pdf.
    stride : int
        Stride of feature map.
    alloc_size : tuple of int, default is (128, 128)
        For advanced users. Define `alloc_size` to generate large enough anchor
        maps, which will later saved in parameters. During inference, we support arbitrary
        input image by cropping corresponding area of the anchor map. This allow us
        to export to symbol so we can run it in c++, Scalar, etc.
    """
    def __init__(self, index, num_class, anchors, stride,
                 alloc_size=(128, 128), **kwargs):
        super(YOLOOutputV3, self).__init__(**kwargs)
        anchors = np.array(anchors).astype('float32')
        self._classes = num_class
        self._num_pred = 1 + 4 + num_class  # 1 objness + 4 box + num_class
        self._num_anchors = anchors.size // 2
        self._stride = stride
        with self.name_scope():
            all_pred = self._num_pred * self._num_anchors
            self.prediction = nn.Conv2D(all_pred, kernel_size=1, padding=0, strides=1)
            # anchors will be multiplied to predictions
            anchors = anchors.reshape(1, 1, -1, 2)
            self.anchors = self.params.get_constant('anchor_%d'%(index), anchors)
            # offsets will be added to predictions
            grid_x = np.arange(alloc_size[1])
            grid_y = np.arange(alloc_size[0])
            grid_x, grid_y = np.meshgrid(grid_x, grid_y)
            # stack to (n, n, 2)
            offsets = np.concatenate((grid_x[:, :, np.newaxis], grid_y[:, :, np.newaxis]), axis=-1)
            # expand dims to (1, 1, n, n, 2) so it's easier for broadcasting
            offsets = np.expand_dims(np.expand_dims(offsets, axis=0), axis=0)
            self.offsets = self.params.get_constant('offset_%d'%(index), offsets)

    def reset_class(self, classes, reuse_weights=None):
        """Reset class prediction.
        Parameters
        ----------
        classes : type
            Description of parameter `classes`.
        reuse_weights : dict
            A {new_integer : old_integer} mapping dict that allows the new predictor to reuse the
            previously trained weights specified by the integer index.
        Returns
        -------
        type
            Description of returned object.
        """
        self._clear_cached_op()
        # keep old records
        old_classes = self._classes
        old_pred = self.prediction
        old_num_pred = self._num_pred
        ctx = list(old_pred.params.values())[0].list_ctx()
        self._classes = len(classes)
        self._num_pred = 1 + 4 + len(classes)
        all_pred = self._num_pred * self._num_anchors
        # to avoid deferred init, number of in_channels must be defined
        in_channels = list(old_pred.params.values())[0].shape[1]
        self.prediction = nn.Conv2D(
            all_pred, kernel_size=1, padding=0, strides=1,
            in_channels=in_channels, prefix=old_pred.prefix)
        self.prediction.initialize(ctx=ctx)
        if reuse_weights:
            new_pred = self.prediction
            assert isinstance(reuse_weights, dict)
            for old_params, new_params in zip(old_pred.params.values(), new_pred.params.values()):
                old_data = old_params.data()
                new_data = new_params.data()
                for k, v in reuse_weights.items():
                    if k >= self._classes or v >= old_classes:
                        warnings.warn("reuse mapping {}/{} -> {}/{} out of range".format(
                            k, self._classes, v, old_classes))
                        continue
                    for i in range(self._num_anchors):
                        off_new = i * self._num_pred
                        off_old = i * old_num_pred
                        # copy along the first dimension
                        new_data[1 + 4 + k + off_new] = old_data[1 + 4 + v + off_old]
                        # copy non-class weights as well
                        new_data[off_new : 1 + 4 + off_new] = old_data[off_old : 1 + 4 + off_old]
                # set data to new conv layers
                new_params.set_data(new_data)


    def hybrid_forward(self, F, x, anchors, offsets):
        """Hybrid Forward of YOLOV3Output layer.
        Parameters
        ----------
        F : mxnet.nd or mxnet.sym
            `F` is mxnet.sym if hybridized or mxnet.nd if not.
        x : mxnet.nd.NDArray
            Input feature map.
        anchors : mxnet.nd.NDArray
            Anchors loaded from self, no need to supply.
        offsets : mxnet.nd.NDArray
            Offsets loaded from self, no need to supply.
        Returns
        -------
        (tuple of) mxnet.nd.NDArray
            During training, return (bbox, raw_box_centers, raw_box_scales, objness,
            class_pred, anchors, offsets).
            During inference, return detections.
        """
        # prediction flat to (batch, pred per pixel, height * width)
        pred = self.prediction(x).reshape((0, self._num_anchors * self._num_pred, -1))
        # transpose to (batch, height * width, num_anchor, num_pred)
        pred = pred.transpose(axes=(0, 2, 1)).reshape((0, -1, self._num_anchors, self._num_pred))
        # components
        raw_box_centers = pred.slice_axis(axis=-1, begin=0, end=2)
        raw_box_scales = pred.slice_axis(axis=-1, begin=2, end=4)
        objness = pred.slice_axis(axis=-1, begin=4, end=5)
        class_pred = pred.slice_axis(axis=-1, begin=5, end=None)

        # valid offsets, (1, 1, height, width, 2)
        offsets = F.slice_like(offsets, x * 0, axes=(2, 3))
        # reshape to (1, height*width, 1, 2)
        offsets = offsets.reshape((1, -1, 1, 2))

        box_centers = F.broadcast_add(F.sigmoid(raw_box_centers), offsets) * self._stride
        box_scales = F.broadcast_mul(F.exp(raw_box_scales), anchors)
        confidence = F.sigmoid(objness)
        class_score = F.broadcast_mul(F.sigmoid(class_pred), confidence)
        wh = box_scales / 2.0
        bbox = F.concat(box_centers - wh, box_centers + wh, dim=-1)

        if autograd.is_training():
            # during training, we don't need to convert whole bunch of info to detection results
            return (bbox.reshape((0, -1, 4)), raw_box_centers, raw_box_scales,
                    objness, class_pred, anchors, offsets)

        # prediction per class
        bboxes = F.tile(bbox, reps=(self._classes, 1, 1, 1, 1))
        scores = F.transpose(class_score, axes=(3, 0, 1, 2)).expand_dims(axis=-1)
        ids = F.broadcast_add(scores * 0, F.arange(0, self._classes).reshape((0, 1, 1, 1, 1)))
        detections = F.concat(ids, scores, bboxes, dim=-1)
        # reshape to (B, xx, 6)
        detections = F.reshape(detections.transpose(axes=(1, 0, 2, 3, 4)), (0, -1, 6))
        return detections

class YOLODetectionBlockV1(gluon.HybridBlock):
    """YOLO V1 Detection Block which does the following:
    - add a few conv layers
    - return the output
    - have a branch that do yolo detection.
    Parameters
    ----------
    channel : int
        Number of channels for 1x1 conv. 3x3 Conv will have 2*channel.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    def __init__(self, channel, norm_layer=BatchNorm, norm_kwargs=None, **kwargs):
        super(YOLODetectionBlockV1, self).__init__(**kwargs)
        assert channel % 2 == 0, "channel {} cannot be divided by 2".format(channel)
        with self.name_scope():
            self.body = nn.HybridSequential(prefix='')
            # 1x1 reduce
            self.body.add(_conv2d(channel, 1, 0, 1,
                                  norm_layer=norm_layer, norm_kwargs=norm_kwargs))
            # 3x3 expand
            self.body.add(_conv2d(channel * 2, 3, 1, 1,
                                  norm_layer=norm_layer, norm_kwargs=norm_kwargs))
            self.body.add(_conv2d(channel, 1, 0, 1,
                                  norm_layer=norm_layer, norm_kwargs=norm_kwargs))
            self.tip = _conv2d(channel * 2, 3, 1, 1, norm_layer=norm_layer, norm_kwargs=norm_kwargs)

    # pylint: disable=unused-argument
    def hybrid_forward(self, F, x):
        route = self.body(x)
        tip = self.tip(route)
        return route, tip

class YOLODetectionBlockV3(gluon.HybridBlock):
    """YOLO V3 Detection Block which does the following:
    - add a few conv layers
    - return the output
    - have a branch that do yolo detection.
    Parameters
    ----------
    channel : int
        Number of channels for 1x1 conv. 3x3 Conv will have 2*channel.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    def __init__(self, channel, norm_layer=BatchNorm, norm_kwargs=None, **kwargs):
        super(YOLODetectionBlockV3, self).__init__(**kwargs)
        assert channel % 2 == 0, "channel {} cannot be divided by 2".format(channel)
        with self.name_scope():
            self.body = nn.HybridSequential(prefix='')
            for _ in range(2):
                # 1x1 reduce
                self.body.add(_conv2d(channel, 1, 0, 1,
                                      norm_layer=norm_layer, norm_kwargs=norm_kwargs))
                # 3x3 expand
                self.body.add(_conv2d(channel * 2, 3, 1, 1,
                                      norm_layer=norm_layer, norm_kwargs=norm_kwargs))
            self.body.add(_conv2d(channel, 1, 0, 1,
                                  norm_layer=norm_layer, norm_kwargs=norm_kwargs))
            self.tip = _conv2d(channel * 2, 3, 1, 1, norm_layer=norm_layer, norm_kwargs=norm_kwargs)

    # pylint: disable=unused-argument
    def hybrid_forward(self, F, x):
        route = self.body(x)
        tip = self.tip(route)
        return route, tip


class YOLOV3(gluon.HybridBlock):
    """YOLO V3 detection network.
    Reference: https://arxiv.org/pdf/1804.02767.pdf.
    Parameters
    ----------
    stages : mxnet.gluon.HybridBlock
        Staged feature extraction blocks.
        For example, 3 stages and 3 YOLO output layers are used original paper.
    channels : iterable
        Number of conv channels for each appended stage.
        `len(channels)` should match `len(stages)`.
    num_class : int
        Number of foreground objects.
    anchors : iterable
        The anchor setting. `len(anchors)` should match `len(stages)`.
    strides : iterable
        Strides of feature map. `len(strides)` should match `len(stages)`.
    alloc_size : tuple of int, default is (128, 128)
        For advanced users. Define `alloc_size` to generate large enough anchor
        maps, which will later saved in parameters. During inference, we support arbitrary
        input image by cropping corresponding area of the anchor map. This allow us
        to export to symbol so we can run it in c++, Scalar, etc.
    nms_thresh : float, default is 0.45.
        Non-maximum suppression threshold. You can specify < 0 or > 1 to disable NMS.
    nms_topk : int, default is 400
        Apply NMS to top k detection results, use -1 to disable so that every Detection
         result is used in NMS.
    post_nms : int, default is 100
        Only return top `post_nms` detection results, the rest is discarded. The number is
        based on COCO dataset which has maximum 100 objects per image. You can adjust this
        number if expecting more objects. You can use -1 to return all detections.
    pos_iou_thresh : float, default is 1.0
        IOU threshold for true anchors that match real objects.
        'pos_iou_thresh < 1' is not implemented.
    ignore_iou_thresh : float
        Anchors that has IOU in `range(ignore_iou_thresh, pos_iou_thresh)` don't get
        penalized of objectness score.
    norm_layer : object
        Normalization layer used (default: :class:`mxnet.gluon.nn.BatchNorm`)
        Can be :class:`mxnet.gluon.nn.BatchNorm` or :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    norm_kwargs : dict
        Additional `norm_layer` arguments, for example `num_devices=4`
        for :class:`mxnet.gluon.contrib.nn.SyncBatchNorm`.
    """
    def __init__(self, stages, channels, anchors, strides, classes, alloc_size=(128, 128),
                 nms_thresh=0.45, nms_topk=400, post_nms=100, pos_iou_thresh=1.0,
                 ignore_iou_thresh=0.7, norm_layer=BatchNorm, norm_kwargs=None, **kwargs):
        super(YOLOV3, self).__init__(**kwargs)
        self._classes = classes
        self.nms_thresh = nms_thresh
        self.nms_topk = nms_topk
        self.post_nms = post_nms
        self._pos_iou_thresh = pos_iou_thresh
        self._ignore_iou_thresh = ignore_iou_thresh
        if pos_iou_thresh >= 1:
            self._target_generator = YOLOV3TargetMerger(len(classes), ignore_iou_thresh)
        else:
            raise NotImplementedError(
                "pos_iou_thresh({}) < 1.0 is not implemented!".format(pos_iou_thresh))
        self._loss = YOLOV3Loss()
        with self.name_scope():
            self.stages = nn.HybridSequential()
            self.transitions = nn.HybridSequential()
            self.yolo_blocks = nn.HybridSequential()
            self.yolo_outputs = nn.HybridSequential()
            # note that anchors and strides should be used in reverse order
            for i, stage, channel, anchor, stride in zip(
                    range(len(stages)), stages, channels, anchors[::-1], strides[::-1]):
                self.stages.add(stage)
                block = YOLODetectionBlockV3(
                    channel, norm_layer=norm_layer, norm_kwargs=norm_kwargs)
                # block = YOLODetectionBlockV1(
                #     channel, norm_layer=norm_layer, norm_kwargs=norm_kwargs)
                self.yolo_blocks.add(block)
                output = YOLOOutputV3(i, len(classes), anchor, stride, alloc_size=alloc_size)
                self.yolo_outputs.add(output)
                if i > 0:
                    self.transitions.add(_conv2d(channel, 1, 0, 1,
                                                 norm_layer=norm_layer, norm_kwargs=norm_kwargs))

    @property
    def num_class(self):
        """Number of (non-background) categories.
        Returns
        -------
        int
            Number of (non-background) categories.
        """
        return self._num_class

    @property
    def classes(self):
        """Return names of (non-background) categories.
        Returns
        -------
        iterable of str
            Names of (non-background) categories.
        """
        return self._classes

    def hybrid_forward(self, F, x, *args):
        """YOLOV3 network hybrid forward.
        Parameters
        ----------
        F : mxnet.nd or mxnet.sym
            `F` is mxnet.sym if hybridized or mxnet.nd if not.
        x : mxnet.nd.NDArray
            Input data.
        *args : optional, mxnet.nd.NDArray
            During training, extra inputs are required:
            (gt_boxes, obj_t, centers_t, scales_t, weights_t, clas_t)
            These are generated by YOLOV3PrefetchTargetGenerator in dataloader transform function.
        Returns
        -------
        (tuple of) mxnet.nd.NDArray
            During inference, return detections in shape (B, N, 6)
            with format (cid, score, xmin, ymin, xmax, ymax)
            During training, return losses only: (obj_loss, center_loss, scale_loss, cls_loss).
        """
        all_box_centers = []
        all_box_scales = []
        all_objectness = []
        all_class_pred = []
        all_anchors = []
        all_offsets = []
        all_feat_maps = []
        all_detections = []
        routes = []
        for stage, block, output in zip(self.stages, self.yolo_blocks, self.yolo_outputs):
            x = stage(x)
            routes.append(x)

        # the YOLO output layers are used in reverse order, i.e., from very deep layers to shallow
        for i, block, output in zip(range(len(routes)), self.yolo_blocks, self.yolo_outputs):
            x, tip = block(x)
            if autograd.is_training():
                dets, box_centers, box_scales, objness, class_pred, anchors, offsets = output(tip)
                all_box_centers.append(box_centers.reshape((0, -3, -1)))
                all_box_scales.append(box_scales.reshape((0, -3, -1)))
                all_objectness.append(objness.reshape((0, -3, -1)))
                all_class_pred.append(class_pred.reshape((0, -3, -1)))
                all_anchors.append(anchors)
                all_offsets.append(offsets)
                # here we use fake featmap to reduce memory consuption, only shape[2, 3] is used
                fake_featmap = F.zeros_like(tip.slice_axis(
                    axis=0, begin=0, end=1).slice_axis(axis=1, begin=0, end=1))
                all_feat_maps.append(fake_featmap)
            else:
                dets = output(tip)
            all_detections.append(dets)
            if i >= len(routes) - 1:
                break
            # add transition layers
            x = self.transitions[i](x)
            # upsample feature map reverse to shallow layers
            upsample = _upsample(x, stride=2)
            route_now = routes[::-1][i + 1]
            x = F.concat(F.slice_like(upsample, route_now * 0, axes=(2, 3)), route_now, dim=1)

        if autograd.is_training():
            # during training, the network behaves differently since we don't need detection results
            if autograd.is_recording():
                # generate losses and return them directly
                box_preds = F.concat(*all_detections, dim=1)
                all_preds = [F.concat(*p, dim=1) for p in [
                    all_objectness, all_box_centers, all_box_scales, all_class_pred]]
                all_targets = self._target_generator(box_preds, *args)
                return self._loss(*(all_preds + all_targets))

            # return raw predictions, this is only used in DataLoader transform function.
            return (F.concat(*all_detections, dim=1), all_anchors, all_offsets, all_feat_maps,
                    F.concat(*all_box_centers, dim=1), F.concat(*all_box_scales, dim=1),
                    F.concat(*all_objectness, dim=1), F.concat(*all_class_pred, dim=1))

        # concat all detection results from different stages
        result = F.concat(*all_detections, dim=1)
        # apply nms per class
        if self.nms_thresh > 0 and self.nms_thresh < 1:
            result = F.contrib.box_nms(
                result, overlap_thresh=self.nms_thresh, valid_thresh=0.01,
                topk=self.nms_topk, id_index=0, score_index=1, coord_start=2, force_suppress=False)
            if self.post_nms > 0:
                result = result.slice_axis(axis=1, begin=0, end=self.post_nms)
        ids = result.slice_axis(axis=-1, begin=0, end=1)
        scores = result.slice_axis(axis=-1, begin=1, end=2)
        bboxes = result.slice_axis(axis=-1, begin=2, end=None)
        return ids, scores, bboxes

    def set_nms(self, nms_thresh=0.45, nms_topk=400, post_nms=100):
        """Set non-maximum suppression parameters.
        Parameters
        ----------
        nms_thresh : float, default is 0.45.
            Non-maximum suppression threshold. You can specify < 0 or > 1 to disable NMS.
        nms_topk : int, default is 400
            Apply NMS to top k detection results, use -1 to disable so that every Detection
             result is used in NMS.
        post_nms : int, default is 100
            Only return top `post_nms` detection results, the rest is discarded. The number is
            based on COCO dataset which has maximum 100 objects per image. You can adjust this
            number if expecting more objects. You can use -1 to return all detections.
        Returns
        -------
        None
        """
        self._clear_cached_op()
        self.nms_thresh = nms_thresh
        self.nms_topk = nms_topk
        self.post_nms = post_nms

    def reset_class(self, classes, reuse_weights=None):
        """Reset class categories and class predictors.
        Parameters
        ----------
        classes : iterable of str
            The new categories. ['apple', 'orange'] for example.
        reuse_weights : dict
            A {new_integer : old_integer} or mapping dict or {new_name : old_name} mapping dict,
            or a list of [name0, name1,...] if class names don't change.
            This allows the new predictor to reuse the
            previously trained weights specified.

        Example
        -------
        >>> net = gluoncv.model_zoo.get_model('yolo3_darknet53_voc', pretrained=True)
        >>> # use direct name to name mapping to reuse weights
        >>> net.reset_class(classes=['person'], reuse_weights={'person':'person'})
        >>> # or use interger mapping, person is the 14th category in VOC
        >>> net.reset_class(classes=['person'], reuse_weights={0:14})
        >>> # you can even mix them
        >>> net.reset_class(classes=['person'], reuse_weights={'person':14})
        >>> # or use a list of string if class name don't change
        >>> net.reset_class(classes=['person'], reuse_weights=['person'])

        """
        self._clear_cached_op()
        old_classes = self._classes
        self._classes = classes
        if self._pos_iou_thresh >= 1:
            self._target_generator = YOLOV3TargetMerger(len(classes), self._ignore_iou_thresh)
        if isinstance(reuse_weights, (dict, list)):
            if isinstance(reuse_weights, dict):
                # trying to replace str with indices
                for k, v in reuse_weights.items():
                    if isinstance(v, str):
                        try:
                            v = old_classes.index(v)  # raise ValueError if not found
                        except ValueError:
                            raise ValueError(
                                "{} not found in old class names {}".format(v, old_classes))
                        reuse_weights[k] = v
                    if isinstance(k, str):
                        try:
                            new_idx = self._classes.index(k)  # raise ValueError if not found
                        except ValueError:
                            raise ValueError(
                                "{} not found in new class names {}".format(k, self._classes))
                        reuse_weights.pop(k)
                        reuse_weights[new_idx] = v
            else:
                new_map = {}
                for x in reuse_weights:
                    try:
                        new_idx = self._classes.index(x)
                        old_idx = old_classes.index(x)
                        new_map[new_idx] = old_idx
                    except ValueError:
                        warnings.warn("{} not found in old: {} or new class names: {}".format(
                            x, old_classes, self._classes))
                reuse_weights = new_map

        for outputs in self.yolo_outputs:
            outputs.reset_class(classes, reuse_weights=reuse_weights)