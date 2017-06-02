# -*- coding: utf-8 -*-
import os
import os.path
import time

import numpy as np
import tensorflow as tf
from six.moves import range

from engine.grid_sampler import GridSampler
from engine.input_buffer import DeployInputBuffer
from engine.volume_loader import VolumeLoaderLayer
from layer.input_normalisation import HistogramNormalisationLayer as HistNorm
from utilities.csv_table import CSVTable
from utilities.input_placeholders import ImagePatch


# run on single GPU with single thread
def run(net_class, param, csv_dict, device_str):
    param.queue_length = max(param.queue_length, param.batch_size)
    # expanding a few of the user input parameters
    if param.spatial_rank == 3:
        spatial_padding = ((param.volume_padding_size, param.volume_padding_size),
                       (param.volume_padding_size, param.volume_padding_size),
                       (param.volume_padding_size, param.volume_padding_size))
    else:
        spatial_padding = ((param.volume_padding_size, param.volume_padding_size),
                       (param.volume_padding_size, param.volume_padding_size))
    param_n_channel_out = 1 if not param.output_prob else param.num_classes
    interp_order = (param.image_interp_order,
                    param.label_interp_order,
                    param.w_map_interp_order)

    # read each line of csv files into an instance of Subject
    csv_loader = CSVTable(csv_dict=csv_dict, allow_missing=True)

    # define how to normalise image volumes
    hist_norm = HistNorm(models_filename=param.histogram_ref_file,
                         multimod_mask_type=param.multimod_mask_type,
                         norm_type=param.norm_type,
                         cutoff=(param.cutoff_min, param.cutoff_max),
                         mask_type=param.mask_type)
    # define how to choose training volumes
    volume_loader = VolumeLoaderLayer(
        csv_loader,
        hist_norm,
        is_training=False,
        do_reorientation=param.reorientation,
        do_resampling=param.resampling,
        spatial_padding=spatial_padding,
        do_normalisation=param.normalisation,
        do_whitening=param.whitening,
        interp_order=interp_order)
    print('found {} subjects'.format(len(volume_loader.subject_list)))

    # construct graph
    graph = tf.Graph()
    with graph.as_default(), tf.device("/{}:0".format(device_str)):
        # construct inference queue and graph
        # TODO change batch size param - batch size could be larger in test case
        if param.spatial_rank == 2.5:
            image_shape = [param.image_size] * 2
            label_shape = [param.label_size] * 2
            weight_map_shape = [param.w_map_size] * 2
        else:
            image_shape = [param.image_size] * int(param.spatial_rank)
            label_shape = [param.label_size] * int(param.spatial_rank)
            weight_map_shape = [param.w_map_size] * int(param.spatial_rank)
        patch_holder = ImagePatch(
            image_shape=image_shape,
            label_shape=label_shape,
            weight_map_shape=weight_map_shape,
            info_length=2*param.spatial_rank,
            image_dtype=tf.float32,
            label_dtype=tf.int64,
            weight_map_dtype=tf.float32,
            num_image_modality=volume_loader.num_modality(0),
            num_label_modality=volume_loader.num_modality(1),
            num_weight_map=volume_loader.num_modality(2))

        # `patch` instance with image data only
        spatial_rank = patch_holder.spatial_rank
        sampling_grid_size = patch_holder.image_size - 2 * param.border
        # It should maybe be:
        sampling_grid_size = patch_holder.label_size
        assert sampling_grid_size > 0
        sampler = GridSampler(patch=patch_holder,
                              volume_loader=volume_loader,
                              grid_size=sampling_grid_size,
                              name='grid_sampler')

        net = net_class(num_classes=param.num_classes)
        # construct train queue
        seg_batch_runner = DeployInputBuffer(batch_size=param.batch_size,
                                             capacity=param.queue_length,
                                             sampler=sampler)
        test_pairs = seg_batch_runner.pop_batch_op
        info = test_pairs['info']
        logits = net(test_pairs['images'], is_training=False)
        if param.output_prob:
            logits = tf.nn.softmax(logits)
        else:
            logits = tf.argmax(logits, -1)
        variable_averages = tf.train.ExponentialMovingAverage(0.9)
        variables_to_restore = variable_averages.variables_to_restore()
        saver = tf.train.Saver(var_list=variables_to_restore)
        tf.Graph.finalize(graph)  # no more graph nodes after this line

    # run session
    config = tf.ConfigProto()
    config.log_device_placement = False
    config.allow_soft_placement = True
    # config.gpu_options.allow_growth = True

    start_time = time.time()
    with tf.Session(config=config, graph=graph) as sess:
        root_dir = os.path.abspath(param.model_dir)
        ckpt = tf.train.get_checkpoint_state(root_dir + '/models/')
        if ckpt and ckpt.model_checkpoint_path:
            print('Evaluation from checkpoints')
        model_str = '{}/models/model.ckpt-{}'.format(root_dir, param.pred_iter)
        print('Using model {}'.format(model_str))
        saver.restore(sess, model_str)

        coord = tf.train.Coordinator()
        all_saved_flag = False
        try:
            seg_batch_runner.run_threads(sess, coord, num_threads=1)
            img_id, pred_img, subject_i = None, None, None
            while True:
                local_time = time.time()
                if coord.should_stop():
                    break
                seg_maps, spatial_info = sess.run([logits, info])
                # go through each one in a batch
                for batch_id in range(seg_maps.shape[0]):
                    if spatial_info[batch_id, 0] != img_id:
                        # when subject_id changed
                        # save current map and reset cumulative map variable
                        if subject_i is not None:
                            # In case multiple modalities out, have to swap
                            # the dimensions to ensure modalties in the 5th
                            # dimension (nifty standards)
                            if pred_img.shape[3] > 1:
                                print("Ensuring nifty dimension standards")
                                pred_img = np.swapaxes(pred_img, 4, 3)
                            subject_i.save_network_output(
                                pred_img,
                                param.save_seg_dir,
                                param.output_interp_order)

                        if patch_holder.is_stopping_signal(
                                spatial_info[batch_id]):
                            print('received finishing batch')
                            all_saved_flag = True
                            seg_batch_runner.close_all()
                            break

                        img_id = spatial_info[batch_id, 0]
                        subject_i = volume_loader.get_subject(img_id)
                        pred_img = subject_i.matrix_like_input_data_5d(
                            spatial_rank=spatial_rank,
                            n_channels=param_n_channel_out,
                            interp_order=param.output_interp_order)

                    # try to expand prediction dims to match the output volume
                    predictions = seg_maps[batch_id]
                    while predictions.ndim < pred_img.ndim:
                        predictions = np.expand_dims(predictions, axis=-1)

                    # assign predicted patch to the allocated output volume
                    origin = spatial_info[batch_id, 1:(1 + spatial_rank)]
                    # indexing within the patch
                    if param.border > 0 and patch_holder.label_size == \
                            patch_holder.image_size:
                        s_ = param.border
                        _s = patch_holder.image_size - param.border
                        p_, _p = s_, _s
                    else:
                        s_ = (patch_holder.image_size -
                              patch_holder.label_size)/2
                        _s = s_ + patch_holder.label_size
                        p_, _p = 0, patch_holder.label_size
                    # indexing within the prediction volume
                    dest_start, dest_end = (origin + s_), (origin + _s)

                    assert np.all(dest_start >= 0)
                    assert np.all(dest_end <= pred_img.shape[0:int(np.floor(
                        spatial_rank))])
                    if spatial_rank == 3:
                        x_, y_, z_ = dest_start
                        _x, _y, _z = dest_end
                        pred_img[x_:_x, y_:_y, z_:_z, ...] = \
                            predictions[p_:_p, p_:_p, p_:_p, ...]
                    elif spatial_rank == 2:
                        x_, y_ = dest_start
                        _x, _y = dest_end
                        pred_img[x_:_x, y_:_y, ...] = \
                            predictions[p_:_p, p_:_p, ...]
                    elif spatial_rank == 2.5:
                        x_, y_ = dest_start
                        _x, _y = dest_end
                        z_ = spatial_info[batch_id, 1 + int(np.floor(
                            spatial_rank))]
                        pred_img[x_:_x, y_:_y, z_, ...] = predictions[p_:_p,
                                                                      p_:_p,
                                                                      0, ...]
                    else:
                        raise ValueError("unsupported spatial rank")
                print('processed {} image patches ({:.3f}s)'.format(
                    len(spatial_info), time.time() - local_time))

        except KeyboardInterrupt:
            print('User cancelled training')
        except tf.errors.OutOfRangeError as e:
            pass
        except Exception:
            import sys
            import traceback
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_exception(
                exc_type, exc_value, exc_traceback, file=sys.stdout)
            seg_batch_runner.close_all()
        finally:
            if not all_saved_flag:
                print('stopped early, incomplete predictions')
            print('inference.py time: {:.3f} seconds'.format(
                time.time() - start_time))
            seg_batch_runner.close_all()